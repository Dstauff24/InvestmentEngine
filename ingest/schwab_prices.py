"""Daily OHLCV ingestion from Schwab (D3/D15) -> `prices_daily`.

    ALLOW_LIVE_CALLS=1 python -m ingest.schwab_prices
    ALLOW_LIVE_CALLS=1 python -m ingest.schwab_prices --backfill-days 1825
    ALLOW_LIVE_CALLS=1 python -m ingest.schwab_prices --symbols AAPL MSFT

D15: daily bars only in Phase 1. D24: bars are stored EXACTLY as reported and
never restated — adjusted series are computed at read time from
`corporate_actions` by `features/adjustment.py`.

NOT `is_critical`: unlike the D7 snapshots, Schwab will still sell us yesterday's
daily bar tomorrow, so a failed run costs freshness rather than history. The
orchestrator still treats it as a hard dependency for scoring — see
jobs/daily_close.py — it just does not permanently damage the record.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, date, datetime, timedelta

from core import clock, db
from core.jobs import job_run
from ingest.runner import as_float, as_int, for_each_symbol
from ingest.schwab_client import SchwabClient

log = logging.getLogger(__name__)

JOB_NAME = "schwab_prices"

# Enough history for a 200-day MA plus 52-week structure with room to spare.
DEFAULT_BACKFILL_DAYS = 400
# Re-pull a few sessions of overlap on every incremental run: Schwab corrects
# late prints, and the upsert makes an overlap free.
OVERLAP_DAYS = 5

UPSERT = """
    INSERT INTO prices_daily (
        ts, session_date, symbol, open, high, low, close, volume, source
    ) VALUES (
        %(ts)s, %(session_date)s, %(symbol)s, %(open)s, %(high)s, %(low)s,
        %(close)s, %(volume)s, 'schwab'
    )
    ON CONFLICT (symbol, ts) DO UPDATE SET
        session_date = EXCLUDED.session_date,
        open         = EXCLUDED.open,
        high         = EXCLUDED.high,
        low          = EXCLUDED.low,
        close        = EXCLUDED.close,
        volume       = EXCLUDED.volume,
        ingested_at  = now()
"""


def parse_candles(payload: dict, symbol: str) -> list[dict]:
    """Schwab price-history candles -> `prices_daily` rows.

    Schwab timestamps a daily candle at midnight ET of the session. That instant
    is stored as `ts` in UTC, and the ET calendar date it belongs to is stored
    separately as `session_date` — deriving one from the other in every query is
    how a bar ends up filed under the wrong trading day (dual time keying).

    No adj_* columns are produced. D24: adjustment happens at read time.
    """
    rows = []
    for candle in payload.get("candles") or []:
        epoch_ms = as_float(candle.get("datetime"))
        close = as_float(candle.get("close"))
        if epoch_ms is None or close is None:
            continue
        try:
            moment = datetime.fromtimestamp(epoch_ms / 1000.0, tz=UTC)
        except (OverflowError, OSError, ValueError):
            continue
        session_date = clock.session_date(moment)
        rows.append(
            {
                "ts": clock.session_close_utc(session_date),
                "session_date": session_date,
                "symbol": symbol,
                "open": as_float(candle.get("open")),
                "high": as_float(candle.get("high")),
                "low": as_float(candle.get("low")),
                "close": close,
                "volume": as_int(candle.get("volume")) or 0,
            }
        )
    return rows


LATEST_SPLIT_SQL = """
    SELECT max(ex_date) AS ex_date
    FROM   corporate_actions
    WHERE  symbol = %s AND action_type = 'split' AND ex_date <= %s
"""


def start_date_for(symbol: str, *, today: date, backfill_days: int) -> date:
    """Where to start pulling: just before the newest stored bar, or a backfill.

    **CLAMPED AT THE MOST RECENT SPLIT EX-DATE, because Schwab restates its own
    history.** Verified live on 2026-10-21: all 14 splits inside the stored
    window — BKNG's 25-for-1 among them — showed no price jump in Schwab's
    candles at all. D24 defines `prices_daily` as raw and applies split factors
    at READ time, so one adjusted bar gets adjusted twice.

    Clamping means Schwab only ever writes sessions on the CURRENT basis, which
    are raw by construction. Anything older comes from
    `ingest/fmp_history.py`'s non-split-adjusted endpoint. Without the clamp the
    5-session overlap re-pull would quietly restate five good bars every time a
    split landed inside it.
    """
    row = db.fetch_one(
        "SELECT max(session_date) AS newest FROM prices_daily WHERE symbol = %s", (symbol,)
    )
    newest = row["newest"] if row else None
    start = (
        today - timedelta(days=backfill_days)
        if newest is None
        else newest - timedelta(days=OVERLAP_DAYS)
    )

    split = db.fetch_one(LATEST_SPLIT_SQL, (symbol, today))
    ex_date = split["ex_date"] if split else None
    if ex_date is not None and ex_date > start:
        log.info(
            "%s: start clamped from %s to the %s split ex-date (Schwab restates "
            "pre-split history; D24 wants raw)",
            symbol,
            start,
            ex_date,
        )
        return ex_date
    return start


def load_symbol(client: SchwabClient, symbol: str, *, today: date, backfill_days: int) -> int:
    start = start_date_for(symbol, today=today, backfill_days=backfill_days)
    payload = client.price_history_daily(
        symbol,
        start=datetime.combine(start, datetime.min.time(), tzinfo=UTC),
        end=datetime.combine(today, datetime.max.time(), tzinfo=UTC),
    )
    if payload.get("empty"):
        return 0
    rows = parse_candles(payload, symbol)
    return db.executemany(UPSERT, rows) if rows else 0


def run(
    client: SchwabClient | None = None,
    *,
    symbols: list[str] | None = None,
    backfill_days: int = DEFAULT_BACKFILL_DAYS,
) -> int:
    client = client or SchwabClient()
    today = clock.session_date()
    # THE ONE JOB THAT UNIONS BENCHMARKS IN. SPY is not a candidate (it is out
    # of `v_active_universe`, so nothing scores it), but its bars have to stay
    # current or D17's risk-off rule and D39's regime tagging read a series
    # that stopped the day the backfill ran.
    universe = symbols if symbols is not None else db.active_symbols() + db.benchmark_symbols()
    if not universe:
        raise RuntimeError("no active tickers; run `python -m ingest.universe` first")

    with job_run(JOB_NAME, session_date=today, triggered_by="manual") as job:
        outcome = for_each_symbol(
            universe,
            lambda s: load_symbol(client, s, today=today, backfill_days=backfill_days),
            run=job,
            label="prices",
        )
        job.note(failed_symbols=sorted(outcome.failed)[:50], backfill_days=backfill_days)
    return outcome.rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--backfill-days",
        type=int,
        default=DEFAULT_BACKFILL_DAYS,
        help="history depth for symbols with no stored bars",
    )
    parser.add_argument("--symbols", nargs="*", help="override the universe (debugging)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    print(f"prices_daily: {run(symbols=args.symbols, backfill_days=args.backfill_days)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
