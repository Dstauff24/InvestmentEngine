"""One-time deep history backfill: five years of daily bars and earnings dates.

    ALLOW_LIVE_CALLS=1 python -m ingest.fmp_history --years 5
    ALLOW_LIVE_CALLS=1 python -m ingest.fmp_history --bars --symbols AAPL MSFT
    ALLOW_LIVE_CALLS=1 python -m ingest.fmp_history --earnings --years 5

O7 asks for 3-5 years of daily history before a backtest is trusted.
`schwab_prices` collects forward from the day it was first run, so the database
held ~13 months (278 bars/symbol) — enough for a 200-day MA, not enough to
walk a strategy forward through a cycle. FMP's EOD endpoint returns ~1,254 bars
in ONE call per symbol, which is the cheapest deep history in the stack.

TWO DATASETS, because bars alone do not make a backtest:

  * **Bars** -> `prices_daily`, one call per symbol.
  * **Earnings dates** -> `events`, one call per symbol, ten years deep.
    Without them two of the three recipes cannot fire at all:
    `post_earnings_drift` reads "days since the last earnings" and
    `pullback_to_trend` REFUSES when the earnings date is unknown (D19's rules
    cannot be satisfied by ignorance). On 2026-10-21 the events table held 76
    earnings rows for 433 symbols and not one in the past — not because the
    window was too short but because the market-wide calendar is capped at
    4,000 rows and says nothing when it truncates (see `FMPTruncatedResponse`).

**THE SEAM IS AT A KNOWN DATE AND NEVER INSIDE A SESSION.** Two providers
disagree about an OHLC bar at the cent level, and a series stitched bar by bar
would carry those disagreements as price action that never happened. So a
session that another provider already wrote is never overwritten: it keeps the
provider that got there first, `prices_daily.source` records which, and the
overlap is measured and reported rather than merged. Sessions this job wrote
itself ARE rewritten, which is what makes it idempotent and what let a wrong
first run be corrected in place.

**D24 NEEDS THE RAW ENDPOINT, AND THE NAMES LIE ABOUT WHICH ONE THAT IS.**
`/stable/historical-price-eod/full` is SPLIT-ADJUSTED: live on 2026-10-21,
LRCX's 10-for-1 showed no price jump across its ex-date, and the first run of
this job stored 416,117 already-adjusted bars that `features/adjustment.py`
would have adjusted a second time. The raw series is on
`/stable/historical-price-eod/non-split-adjusted`, whose fields are named
`adjOpen`/`adjHigh`/`adjLow`/`adjClose` and carry the UNADJUSTED prices, with
no `close` key at all. Trusting the field names gets it exactly backwards in
both directions.

Which is also why `--backfill-actions` must land FIRST: a five-year price
series whose corporate actions reach back thirteen months is silently wrong for
every bar before the earliest action on record.

NOT `is_critical`, and deliberately a separate job from the daily pipeline:
history that is five years old will still be for sale tomorrow.
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta

from core import clock, db
from core.budget import BudgetExhausted
from core.jobs import job_run
from ingest.fmp_client import FMPClient
from ingest.fmp_events import refresh_symbol_earnings
from ingest.runner import as_float, as_int, for_each_symbol

log = logging.getLogger(__name__)

BARS_JOB_NAME = "fmp_history_bars"
EARNINGS_JOB_NAME = "fmp_history_earnings"
REPAIR_JOB_NAME = "fmp_history_split_basis_repair"

DEFAULT_YEARS = 5

UPSERT_BAR = """
    INSERT INTO prices_daily (
        ts, session_date, symbol, open, high, low, close, volume, source
    ) VALUES (
        %(ts)s, %(session_date)s, %(symbol)s, %(open)s, %(high)s, %(low)s,
        %(close)s, %(volume)s, 'fmp'
    )
    ON CONFLICT (symbol, ts) DO UPDATE SET
        session_date = EXCLUDED.session_date,
        open         = EXCLUDED.open,
        high         = EXCLUDED.high,
        low          = EXCLUDED.low,
        close        = EXCLUDED.close,
        volume       = EXCLUDED.volume,
        source       = EXCLUDED.source,
        ingested_at  = now()
"""


def _price(row: dict, field: str) -> float | None:
    """One OHLC field, under either of the two names FMP uses.

    `historical-price-eod/full` says `close`; `non-split-adjusted` says
    `adjClose` and omits `close` entirely — while carrying the UNADJUSTED
    price. The name is not the basis, so the caller chooses the ENDPOINT and
    this only has to find the number.
    """
    value = row.get(field)
    if value is None:
        value = row.get(f"adj{field.capitalize()}")
    return as_float(value)


def parse_bars(rows: list[dict], symbol: str) -> list[dict]:
    """FMP EOD rows -> `prices_daily` params, oldest first.

    FMP dates a daily bar with the SESSION's calendar date, so unlike Schwab's
    epoch-millisecond candles there is no timezone arithmetic to get wrong —
    but `ts` still comes from `clock.session_close_utc` so both providers' rows
    land on the same instant for the same session (dual time keying).
    """
    out = []
    for row in rows:
        session = _as_date(row.get("date"))
        close = _price(row, "close")
        if session is None or close is None:
            continue
        out.append(
            {
                "ts": clock.session_close_utc(session),
                "session_date": session,
                "symbol": symbol,
                "open": _price(row, "open"),
                "high": _price(row, "high"),
                "low": _price(row, "low"),
                "close": close,
                "volume": as_int(row.get("volume")) or 0,
            }
        )
    out.sort(key=lambda r: r["session_date"])
    return out


@dataclass
class Overlap:
    """How far the two providers disagree where both have the same session.

    Free to compute — the five-year window covers the stored period anyway —
    and the only way to know whether the seam this job creates is a cent-level
    rounding difference or a different series altogether.
    """

    sessions: int = 0
    diffs_pct: list[float] = field(default_factory=list)

    @property
    def median_pct(self) -> float | None:
        return statistics.median(self.diffs_pct) if self.diffs_pct else None

    @property
    def max_pct(self) -> float | None:
        return max(self.diffs_pct) if self.diffs_pct else None

    def __str__(self) -> str:
        if not self.diffs_pct:
            return "no overlapping sessions to compare"
        return (
            f"{self.sessions} overlapping session(s): median |Δclose| "
            f"{self.median_pct:.4f}%, max {self.max_pct:.4f}%"
        )


SOURCE_NAME = "fmp"


def stored_bars(symbol: str) -> dict[date, tuple[float, str]]:
    """Every stored session for one symbol: close and who wrote it."""
    rows = db.fetch_all(
        "SELECT session_date, close, source FROM prices_daily WHERE symbol = %s",
        (symbol,),
    )
    return {r["session_date"]: (float(r["close"]), r["source"] or "") for r in rows}


def compare_overlap(
    parsed: list[dict], stored: dict[date, tuple[float, str]], into: Overlap
) -> None:
    """Accumulate the close disagreement where ANOTHER provider holds the session.

    Sessions this job wrote itself are skipped: comparing a series to a
    previous copy of itself measures nothing, and would bury a real provider
    disagreement under tens of thousands of exact zeros.
    """
    for row in parsed:
        held = stored.get(row["session_date"])
        if held is None:
            continue
        theirs, source = held
        if source == SOURCE_NAME or theirs == 0:
            continue
        into.sessions += 1
        into.diffs_pct.append(abs(row["close"] - theirs) / theirs * 100.0)


# How much history one request may ask for. **FMP TRUNCATES SILENTLY AND THIS
# REPO HAS BEEN BITTEN BY IT**: every calendar endpoint caps at 4,000 rows and
# returns an arbitrary slice of the window without saying so (measured
# 2026-10-21 — a 30-day ask came back as 11 days from the MIDDLE). The EOD
# endpoints have never been pushed past five years here, and O19 asks for
# eighteen, so the request is CHUNKED into windows small enough that a cap
# could not bite and each chunk's coverage is checked against what it asked
# for. Four years is ~1,008 sessions, a quarter of the known cap.
CHUNK_YEARS = 4


def chunks(start: date, end: date, *, years: int = CHUNK_YEARS) -> list[tuple[date, date]]:
    """`[(from, to), ...]` covering `start..end`, newest LAST.

    Newest last so a partial failure leaves the OLDEST part missing, which is
    the half that matters least and is visible as a short series — the other
    order would leave a hole in the recent history the engine scores against.
    """
    out: list[tuple[date, date]] = []
    span = timedelta(days=365 * years)
    lower = start
    while lower < end:
        upper = min(lower + span, end)
        out.append((lower, upper))
        lower = upper + timedelta(days=1)
    return out


def load_symbol_bars(
    client: FMPClient,
    symbol: str,
    *,
    start: date,
    end: date,
    overlap: Overlap,
    replace_sources: frozenset[str] = frozenset({SOURCE_NAME}),
) -> int:
    """Backfill one symbol. Never overwrites another provider's session.

    `replace_sources` names the sources this run is allowed to rewrite.
    Defaulting to our own is the seam rule; `repair_split_basis` widens it,
    which is the only reason it is a parameter rather than a constant.
    """
    stored = stored_bars(symbol)
    rows: list[dict] = []
    for lower, upper in chunks(start, end):
        batch = parse_bars(client.historical_bars_raw(symbol, start=lower, end=upper), symbol)
        if not batch:
            log.debug("%s: no bars in %s..%s", symbol, lower, upper)
            continue
        # TRUNCATION CHECK, per chunk. A provider that silently returns a slice
        # of what was asked for is how the engine ends up with a series that
        # looks complete and is not; the only defence is comparing the oldest
        # bar returned against the oldest one requested. Weekends and holidays
        # make an exact match wrong, so the tolerance is generous and the test
        # is for a MISSING YEAR rather than a missing week.
        oldest = min(r["session_date"] for r in batch)
        if (oldest - lower).days > 370:
            log.warning(
                "%s: asked for %s..%s and the oldest bar returned is %s — a year or more "
                "is missing, which is what a silent cap looks like. Treating the chunk as "
                "partial rather than complete",
                symbol,
                lower,
                upper,
                oldest,
            )
        rows.extend(batch)
    if not rows:
        log.warning("%s: FMP returned no bars for %s..%s", symbol, start, end)
        return 0
    # One session can only come back once; a chunk boundary that overlaps would
    # otherwise upsert the same row twice in one statement.
    rows = list({r["session_date"]: r for r in rows}.values())

    compare_overlap(rows, stored, overlap)
    # THE SEAM. A session another provider wrote keeps that provider's bar;
    # replacing it would restate bars the engine has already measured and
    # scored. Our own rows are rewritten, which is what makes this idempotent.
    rows = [
        r for r in rows if stored.get(r["session_date"], (0.0, SOURCE_NAME))[1] in replace_sources
    ]
    if not rows:
        return 0
    return db.executemany(UPSERT_BAR, rows)


def backfill_bars(
    client: FMPClient | None = None,
    *,
    symbols: list[str] | None = None,
    years: int = DEFAULT_YEARS,
    as_of: date | None = None,
) -> tuple[int, Overlap]:
    """Five years of bars for the universe AND the benchmarks. One call each."""
    client = client or FMPClient()
    today = as_of or clock.session_date()
    start = today - timedelta(days=365 * years + 7)
    universe = symbols if symbols is not None else db.active_symbols() + db.benchmark_symbols()
    if not universe:
        raise RuntimeError("no active tickers; run `python -m ingest.universe` first")

    overlap = Overlap()
    with job_run(
        BARS_JOB_NAME, session_date=today, is_critical=False, triggered_by="manual"
    ) as job:
        outcome = for_each_symbol(
            universe,
            lambda s: load_symbol_bars(client, s, start=start, end=today, overlap=overlap),
            run=job,
            label="history bars",
            stop_on=BudgetExhausted,
        )
        job.note(
            window=f"{start}..{today}",
            years=years,
            symbols=len(universe),
            declined=outcome.declined,
            failed_symbols=sorted(outcome.failed)[:50],
            overlap_sessions=overlap.sessions,
            overlap_median_pct=(
                round(overlap.median_pct, 6) if overlap.median_pct is not None else None
            ),
            overlap_max_pct=(round(overlap.max_pct, 6) if overlap.max_pct is not None else None),
        )
    log.info("provider overlap: %s", overlap)
    return outcome.rows, overlap


SPLIT_BASIS_SQL = """
    WITH held AS (
        SELECT symbol, min(session_date) AS lo, max(session_date) AS hi
        FROM   prices_daily
        WHERE  source <> 'fmp'
        GROUP  BY symbol
    )
    SELECT h.symbol, h.lo, h.hi, count(*) AS splits, min(c.ex_date) AS first_split
    FROM   held h
    JOIN   corporate_actions c
      ON   c.symbol = h.symbol AND c.action_type = 'split'
     AND   c.ex_date >= h.lo AND c.ex_date <= h.hi
    GROUP  BY h.symbol, h.lo, h.hi
    ORDER  BY h.symbol
"""

JUMP_SQL = """
    SELECT (SELECT close FROM prices_daily
            WHERE symbol = %(symbol)s AND session_date < %(ex_date)s
            ORDER BY session_date DESC LIMIT 1) AS before,
           (SELECT close FROM prices_daily
            WHERE symbol = %(symbol)s AND session_date >= %(ex_date)s
            ORDER BY session_date LIMIT 1) AS after
"""


def split_basis_suspects() -> list[dict]:
    """Symbols whose stored series spans a split another provider adjusted away.

    A provider that restates its own history leaves no trace in the row — the
    only evidence is that the series does NOT jump at an ex-date the
    `corporate_actions` log says it should.
    """
    return db.fetch_all(SPLIT_BASIS_SQL)


def observed_jump(symbol: str, ex_date: date) -> float | None:
    """Stored close before an ex-date divided by the close on or after it.

    On a raw series this is the split ratio. On an adjusted one it is ~1.
    """
    row = db.fetch_one(JUMP_SQL, {"symbol": symbol, "ex_date": ex_date})
    if not row or row["before"] is None or not row["after"]:
        return None
    return float(row["before"]) / float(row["after"])


def repair_split_basis(
    client: FMPClient | None = None,
    *,
    symbols: list[str] | None = None,
    years: int = DEFAULT_YEARS,
    as_of: date | None = None,
) -> list[dict]:
    """Re-source the sessions a provider silently restated. One call per symbol.

    **WHY THIS EXISTS.** Schwab's daily candles are SPLIT-ADJUSTED: live on
    2026-10-21, all 14 splits inside the stored window — including BKNG's
    25-for-1 — showed no price jump at all. D24 defines `prices_daily` as raw
    and applies split factors at READ time, so an already-adjusted bar gets
    adjusted a second time: for BKNG every pre-April bar would have been
    divided by 25 again.

    It was DORMANT until today. `corporate_actions` held no splits at all
    before `--backfill-actions` ran, so the read-time factor was 1.0 and the
    live features were unharmed — the O7 prerequisite that makes a backtest
    trustworthy is what armed the bug.

    Only sessions spanning a split are re-sourced. A symbol that did not split
    inside the window has bars that are raw by construction, and replacing
    those would discard the engine's own live record for no gain.
    """
    client = client or FMPClient()
    today = as_of or clock.session_date()
    start = today - timedelta(days=365 * years + 7)
    suspects = split_basis_suspects()
    if symbols is not None:
        suspects = [s for s in suspects if s["symbol"] in set(symbols)]
    if not suspects:
        log.info("no symbol holds a split inside another provider's window")
        return []

    overlap = Overlap()
    report: list[dict] = []
    with job_run(
        REPAIR_JOB_NAME, session_date=today, is_critical=False, triggered_by="manual"
    ) as job:
        for suspect in suspects:
            symbol = suspect["symbol"]
            splits = db.fetch_all(
                "SELECT ex_date, split_ratio FROM corporate_actions "
                "WHERE symbol = %s AND action_type = 'split' AND ex_date BETWEEN %s AND %s "
                "ORDER BY ex_date",
                (symbol, suspect["lo"], suspect["hi"]),
            )
            before = {s["ex_date"]: observed_jump(symbol, s["ex_date"]) for s in splits}
            rows = load_symbol_bars(
                client,
                symbol,
                start=start,
                end=today,
                overlap=overlap,
                # The whole point: rewrite the restated sessions, whoever wrote
                # them. Narrowed to these symbols by `split_basis_suspects`.
                replace_sources=frozenset({SOURCE_NAME, "schwab"}),
            )
            for split in splits:
                report.append(
                    {
                        "symbol": symbol,
                        "ex_date": str(split["ex_date"]),
                        "ratio": float(split["split_ratio"] or 0),
                        "jump_before": before[split["ex_date"]],
                        "jump_after": observed_jump(symbol, split["ex_date"]),
                        "rows_rewritten": rows,
                    }
                )
            log.info("%s: %d row(s) re-sourced across %d split(s)", symbol, rows, len(splits))

        job.add_rows(sum(r["rows_rewritten"] for r in report))
        job.note(symbols=[s["symbol"] for s in suspects], splits=len(report))
    return report


def backfill_earnings(
    client: FMPClient | None = None,
    *,
    years: int = DEFAULT_YEARS,
    as_of: date | None = None,
    limit: int | None = None,
) -> int:
    """Deep earnings history, PER SYMBOL. One call each, no row cap.

    **THIS WAS A WINDOWED WALK OVER THE MARKET-WIDE CALENDAR AND IT DID NOT
    WORK.** 21 calls for five years looked like the obvious design; the
    endpoint 402s on a 90-day window, and the windows that do answer are capped
    at 4,000 rows and silently return an arbitrary slice of the days asked for
    (measured: a 30-day request came back as 11 days from the middle). So the
    backfill and the daily refresh are now the same operation at a different
    depth — `years` is converted to quarters, and 40 (ten years) costs exactly
    what 4 costs.
    """
    client = client or FMPClient()
    today = as_of or clock.session_date()
    universe = db.active_symbols()
    if not universe:
        raise RuntimeError("no active tickers; run `python -m ingest.universe` first")

    quarters = limit if limit is not None else max(4, years * 4 + 2)
    with job_run(
        EARNINGS_JOB_NAME,
        session_date=today,
        is_critical=False,
        triggered_by="manual",
        clean_stop=BudgetExhausted,
    ) as job:
        written = refresh_symbol_earnings(client, universe, limit=quarters, run=job)
        job.note(symbols=len(universe), quarters_per_symbol=quarters, years=years)
    return written


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS, help="history depth")
    parser.add_argument("--bars", action="store_true", help="bars only")
    parser.add_argument("--earnings", action="store_true", help="earnings calendar only")
    parser.add_argument(
        "--repair-split-basis",
        action="store_true",
        help="re-source sessions a provider restated for a split (D24; one call/symbol)",
    )
    parser.add_argument("--symbols", nargs="*", help="limit the bar backfill (debugging)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.repair_split_basis:
        report = repair_split_basis()
        print(
            f"split-basis repair: {len(report)} split(s) across "
            f"{len({r['symbol'] for r in report})} symbol(s)"
        )
        for row in report:
            print(
                f"  {row['symbol']:6} {row['ex_date']} ratio {row['ratio']:>7.3f}  "
                f"jump {_fmt(row['jump_before'])} -> {_fmt(row['jump_after'])}"
            )
        return 0

    # Neither flag means both, because the two together are what O7 asks for.
    do_bars = args.bars or not args.earnings
    do_earnings = args.earnings or not args.bars

    if do_bars:
        rows, overlap = backfill_bars(symbols=args.symbols, years=args.years)
        print(f"prices_daily: {rows} historical bar(s) written")
        print(f"provider overlap: {overlap}")
    if do_earnings:
        print(f"events: {backfill_earnings(years=args.years)} earnings row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
