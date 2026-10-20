"""Earnings + dividend + split calendars -> `events` and `corporate_actions`.

    ALLOW_LIVE_CALLS=1 python -m ingest.fmp_events
    ALLOW_LIVE_CALLS=1 python -m ingest.fmp_events --backfill-actions

Two tables, two purposes, from overlapping sources:

  * `events` is the EVENT RADAR (Pillar C). Forward-looking: days-to-earnings,
    the D19 rule against opening a swing long into an ex-date, and the
    assignment-risk check on short calls.
  * `corporate_actions` is the ADJUSTMENT LOG (D24). Backward-looking and
    immutable: it is what turns raw `prices_daily` bars into a total-return
    series at read time.

A dividend lands in BOTH — the same fact serving two jobs. A round-trip test
proves the parser's output is exactly what `features/adjustment.py` consumes.
Splits go to both as well, because a split moves every price level in an open
trade plan and the human should see it coming.

Calendar endpoints are market-wide, so the daily run costs THREE calls
regardless of universe size. `--backfill-actions` is the expensive one (one call
per symbol per type) and is a deliberate, occasional operation: run it once
before the first backtest (O7), because `corporate_actions` must reach back at
least as far as the price history the backtest reads or the total-return series
is silently wrong for every earlier bar.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

from core import clock, config, db
from core.budget import BudgetExhausted
from core.jobs import job_run
from ingest.fmp_client import FMPClient
from ingest.runner import as_float, for_each_symbol

log = logging.getLogger(__name__)

JOB_NAME = "fmp_events"
BACKFILL_JOB_NAME = "fmp_corporate_actions_backfill"

UPSERT_EVENT = """
    INSERT INTO events (
        symbol, event_type, event_date, event_time, is_confirmed, title,
        eps_estimate, eps_actual, revenue_estimate, revenue_actual,
        dividend_amount, declaration_date, record_date, payment_date, source
    ) VALUES (
        %(symbol)s, %(event_type)s, %(event_date)s, %(event_time)s, %(is_confirmed)s,
        %(title)s, %(eps_estimate)s, %(eps_actual)s, %(revenue_estimate)s,
        %(revenue_actual)s, %(dividend_amount)s, %(declaration_date)s,
        %(record_date)s, %(payment_date)s, 'fmp'
    )
    ON CONFLICT (symbol, event_type, event_date) DO UPDATE SET
        event_time       = COALESCE(EXCLUDED.event_time, events.event_time),
        is_confirmed     = EXCLUDED.is_confirmed OR events.is_confirmed,
        title            = COALESCE(EXCLUDED.title, events.title),
        eps_estimate     = COALESCE(EXCLUDED.eps_estimate, events.eps_estimate),
        eps_actual       = COALESCE(EXCLUDED.eps_actual, events.eps_actual),
        revenue_estimate = COALESCE(EXCLUDED.revenue_estimate, events.revenue_estimate),
        revenue_actual   = COALESCE(EXCLUDED.revenue_actual, events.revenue_actual),
        dividend_amount  = COALESCE(EXCLUDED.dividend_amount, events.dividend_amount),
        declaration_date = COALESCE(EXCLUDED.declaration_date, events.declaration_date),
        record_date      = COALESCE(EXCLUDED.record_date, events.record_date),
        payment_date     = COALESCE(EXCLUDED.payment_date, events.payment_date),
        updated_at       = now()
"""
# is_confirmed is OR-ed, never overwritten: once actuals have landed, a later
# re-pull of the forward calendar must not un-confirm a reported quarter.

UPSERT_ACTION = """
    INSERT INTO corporate_actions (
        symbol, action_type, ex_date, split_ratio, cash_amount, currency,
        declaration_date, record_date, payment_date, source
    ) VALUES (
        %(symbol)s, %(action_type)s, %(ex_date)s, %(split_ratio)s, %(cash_amount)s,
        %(currency)s, %(declaration_date)s, %(record_date)s, %(payment_date)s, 'fmp'
    )
    ON CONFLICT (symbol, action_type, ex_date) DO UPDATE SET
        split_ratio      = COALESCE(EXCLUDED.split_ratio, corporate_actions.split_ratio),
        cash_amount      = COALESCE(EXCLUDED.cash_amount, corporate_actions.cash_amount),
        declaration_date = COALESCE(EXCLUDED.declaration_date,
                                    corporate_actions.declaration_date),
        record_date      = COALESCE(EXCLUDED.record_date, corporate_actions.record_date),
        payment_date     = COALESCE(EXCLUDED.payment_date, corporate_actions.payment_date),
        updated_at       = now()
"""


def _as_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _event_time(raw: object) -> str:
    """FMP reports 'bmo' / 'amc' / '--' / None. Map to the schema CHECK values."""
    value = str(raw or "").strip().lower()
    return value if value in ("bmo", "amc", "dmt") else "unknown"


def _blank_event(symbol: str, event_type: str, event_date: date) -> dict:
    """All `events` parameters, defaulted. Keeps every builder in step with the
    SQL — a missing key would be a psycopg error at executemany time."""
    return {
        "symbol": symbol,
        "event_type": event_type,
        "event_date": event_date,
        "event_time": "unknown",
        "is_confirmed": False,
        "title": None,
        "eps_estimate": None,
        "eps_actual": None,
        "revenue_estimate": None,
        "revenue_actual": None,
        "dividend_amount": None,
        "declaration_date": None,
        "record_date": None,
        "payment_date": None,
    }


def parse_earnings(rows: list[dict], universe: set[str]) -> list[dict]:
    """FMP `/stable/earnings-calendar` -> `events` params, filtered to the universe.

    `is_confirmed` is True once actuals exist: a date with a reported EPS is a
    fact, while a forward date is FMP's estimate and can move.
    """
    out = []
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        event_date = _as_date(row.get("date"))
        if not symbol or symbol not in universe or event_date is None:
            continue
        eps_actual = as_float(row.get("eps"))
        revenue_actual = as_float(row.get("revenue"))
        out.append(
            {
                **_blank_event(symbol, "earnings", event_date),
                "event_time": _event_time(row.get("time")),
                "is_confirmed": eps_actual is not None or revenue_actual is not None,
                "title": f"{symbol} earnings",
                "eps_estimate": as_float(row.get("epsEstimated")),
                "eps_actual": eps_actual,
                "revenue_estimate": as_float(row.get("revenueEstimated")),
                "revenue_actual": revenue_actual,
            }
        )
    return out


def parse_dividends(rows: list[dict], universe: set[str]) -> tuple[list[dict], list[dict]]:
    """FMP dividend rows -> (`events`, `corporate_actions`) params.

    The same distribution feeds the radar (D19(3): no new swing long within 2
    days of an ex-date) and the adjustment log (D24: the total-return series).
    """
    events, actions = [], []
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        ex_date = _as_date(row.get("date")) or _as_date(row.get("exDate"))
        amount = as_float(
            row.get("dividend") if row.get("dividend") is not None else row.get("adjDividend")
        )
        if not symbol or symbol not in universe or ex_date is None or amount is None:
            continue
        declaration = _as_date(row.get("declarationDate"))
        record = _as_date(row.get("recordDate"))
        payment = _as_date(row.get("paymentDate"))

        events.append(
            {
                **_blank_event(symbol, "ex_dividend", ex_date),
                "event_time": "bmo",  # a stock goes ex at the open
                "is_confirmed": True,
                "title": f"{symbol} ex-dividend ${amount:g}",
                "dividend_amount": amount,
                "declaration_date": declaration,
                "record_date": record,
                "payment_date": payment,
            }
        )
        actions.append(
            {
                "symbol": symbol,
                "action_type": "cash_dividend",
                "ex_date": ex_date,
                "split_ratio": None,
                "cash_amount": amount,
                "currency": "USD",
                "declaration_date": declaration,
                "record_date": record,
                "payment_date": payment,
            }
        )
    return events, actions


def parse_splits(rows: list[dict], universe: set[str]) -> tuple[list[dict], list[dict]]:
    """FMP split rows -> (`events`, `corporate_actions`).

    FMP gives `numerator`/`denominator` for a "numerator-for-denominator" split.
    `corporate_actions.split_ratio` is shares held AFTER per share held BEFORE,
    so a 4-for-1 stores 4.0 and a 1-for-10 reverse stores 0.1.
    """
    events, actions = [], []
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        ex_date = _as_date(row.get("date"))
        numerator = as_float(row.get("numerator"))
        denominator = as_float(row.get("denominator"))
        if not symbol or symbol not in universe or ex_date is None:
            continue
        if not numerator or not denominator:
            continue
        ratio = numerator / denominator
        if ratio <= 0:
            continue

        events.append(
            {
                **_blank_event(symbol, "split", ex_date),
                "event_time": "bmo",
                "is_confirmed": True,
                "title": f"{symbol} {numerator:g}-for-{denominator:g} split",
            }
        )
        actions.append(
            {
                "symbol": symbol,
                "action_type": "split",
                "ex_date": ex_date,
                "split_ratio": ratio,
                "cash_amount": None,
                "currency": "USD",
                "declaration_date": None,
                "record_date": None,
                "payment_date": None,
            }
        )
    return events, actions


def flag_dividend_cuts() -> int:
    """Mark a distribution smaller than the prior one (D19(5)).

    A dividend cut is an automatic thesis-review trigger, so the comparison is
    stored on the row rather than recomputed every time alerting asks.
    """
    return db.execute(
        """
        WITH ranked AS (
            SELECT symbol, ex_date, cash_amount,
                   lag(cash_amount) OVER (PARTITION BY symbol ORDER BY ex_date) AS prior
            FROM   corporate_actions
            WHERE  action_type = 'cash_dividend'
        )
        UPDATE corporate_actions ca
        SET    prior_cash_amount = r.prior,
               is_dividend_cut   = (r.prior IS NOT NULL AND r.cash_amount < r.prior),
               updated_at        = now()
        FROM   ranked r
        WHERE  ca.symbol = r.symbol
          AND  ca.ex_date = r.ex_date
          AND  ca.action_type = 'cash_dividend'
          AND  (ca.prior_cash_amount IS DISTINCT FROM r.prior
                OR ca.is_dividend_cut IS NULL)
        """
    )


def run(client: FMPClient | None = None) -> dict[str, int]:
    client = client or FMPClient()
    scope = config.universe().events
    today = clock.session_date()
    start = today - timedelta(days=scope.backward_days)
    end = today + timedelta(days=scope.forward_days)
    universe = set(db.active_symbols())
    if not universe:
        raise RuntimeError("no active tickers; run `python -m ingest.universe` first")

    counts = {"earnings": 0, "dividends": 0, "splits": 0, "actions": 0, "cuts": 0}

    # A spent day budget is declined spend, not a failure (D29), so the row
    # records as skipped rather than leaving a plan-gated free tier showing a
    # failed events job every single day.
    with job_run(
        JOB_NAME, session_date=today, triggered_by="manual", clean_stop=BudgetExhausted
    ) as job:
        earnings = parse_earnings(client.earnings_calendar(start, end), universe)
        counts["earnings"] = db.executemany(UPSERT_EVENT, earnings)

        div_events, div_actions = parse_dividends(client.dividend_calendar(start, end), universe)
        counts["dividends"] = db.executemany(UPSERT_EVENT, div_events)

        split_events, split_actions = parse_splits(client.split_calendar(start, end), universe)
        counts["splits"] = db.executemany(UPSERT_EVENT, split_events)

        counts["actions"] = db.executemany(UPSERT_ACTION, div_actions + split_actions)
        counts["cuts"] = flag_dividend_cuts()

        job.add_rows(counts["earnings"] + counts["dividends"] + counts["splits"])
        job.note(window=f"{start}..{end}", **counts)
    return counts


def backfill_actions(client: FMPClient | None = None, *, symbols: list[str] | None = None) -> int:
    """Pull the FULL split and dividend history, one call per symbol per type.

    `corporate_actions` must reach back at least as far as the price history the
    backtests use, or the total-return series (D24) is wrong for every bar
    before the earliest action on record — silently, with no error. Expensive;
    run it once (O7 prerequisite), then let the daily calendar job keep it
    current.
    """
    client = client or FMPClient()
    universe = symbols if symbols is not None else db.active_symbols()
    wanted = set(universe)

    def one(symbol: str) -> int:
        _, div_actions = parse_dividends(client.historical_dividends(symbol), wanted)
        _, split_actions = parse_splits(client.historical_splits(symbol), wanted)
        rows = div_actions + split_actions
        return db.executemany(UPSERT_ACTION, rows) if rows else 0

    with job_run(
        BACKFILL_JOB_NAME, session_date=clock.session_date(), triggered_by="manual"
    ) as job:
        outcome = for_each_symbol(
            universe, one, run=job, label="corporate actions", stop_on=BudgetExhausted
        )
        cuts = flag_dividend_cuts()
        job.note(failed_symbols=sorted(outcome.failed)[:50], dividend_cuts_flagged=cuts)
    return outcome.rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--backfill-actions",
        action="store_true",
        help="pull full split/dividend history per symbol (slow; run once, O7 prerequisite)",
    )
    parser.add_argument("--symbols", nargs="*", help="limit the backfill (debugging)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.backfill_actions:
        print(f"corporate actions backfilled: {backfill_actions(symbols=args.symbols)} rows")
        return 0

    counts = run()
    print(
        "events: {earnings} earnings, {dividends} ex-dividends, {splits} splits; "
        "{actions} corporate actions ({cuts} dividend-cut flags)".format(**counts)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
