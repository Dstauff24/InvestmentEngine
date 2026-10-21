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
from dataclasses import dataclass
from datetime import date, timedelta

from core import clock, config, db
from core.budget import BudgetExhausted
from core.jobs import job_run
from ingest.fmp_client import FMPClient
from ingest.runner import as_float, for_each_symbol

log = logging.getLogger(__name__)

JOB_NAME = "fmp_events"
BACKFILL_JOB_NAME = "fmp_corporate_actions_backfill"
# Quarters of earnings history per symbol. 40 is ten years, which costs the
# same single call as four and makes the backfill and the daily refresh the
# same operation.
EARNINGS_LIMIT = 40

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


def parse_symbol_earnings(rows: list[dict], symbol: str) -> list[dict]:
    """FMP `/stable/earnings?symbol=X` -> `events` params.

    A SECOND parser rather than a shared one, because the field names differ
    from the calendar's for the same facts: `epsActual` here against `eps`
    there, `revenueActual` against `revenue`. Reading one payload with the
    other's names yields rows that parse cleanly and carry nothing — the
    quietest kind of wrong — so the two shapes are named separately and the
    `events` parameter dict they build is the only thing they share.
    """
    out = []
    for row in rows:
        event_date = _as_date(row.get("date"))
        if event_date is None:
            continue
        eps_actual = as_float(row.get("epsActual"))
        revenue_actual = as_float(row.get("revenueActual"))
        out.append(
            {
                **_blank_event(symbol, "earnings", event_date),
                "is_confirmed": eps_actual is not None or revenue_actual is not None,
                "title": f"{symbol} earnings",
                "eps_estimate": as_float(row.get("epsEstimated")),
                "eps_actual": eps_actual,
                "revenue_estimate": as_float(row.get("revenueEstimated")),
                "revenue_actual": revenue_actual,
            }
        )
    return out


def refresh_symbol_earnings(
    client: FMPClient,
    symbols: list[str],
    *,
    limit: int = EARNINGS_LIMIT,
    run: object = None,
) -> int:
    """Earnings dates PER SYMBOL. One call each, no row cap.

    **WHY NOT THE MARKET-WIDE CALENDAR.** It costs one call instead of 433 and
    was the original design, but it is capped at 4,000 rows and says nothing
    when it truncates: over the 190-day window this job asks for, it returned
    the last 48 days and dropped every past earnings date. Live on 2026-10-21
    that left 76 earnings rows for 433 symbols, zero of them in the past, which
    is why `post_earnings_drift` could not fire and `pullback_to_trend` refused
    every symbol for an unverifiable earnings exclusion.

    The cost is the point: 433 calls buys a radar that is actually complete.
    """

    def one(symbol: str) -> int:
        rows = parse_symbol_earnings(client.symbol_earnings(symbol, limit=limit), symbol)
        return db.executemany(UPSERT_EVENT, rows) if rows else 0

    outcome = for_each_symbol(symbols, one, run=run, label="earnings", stop_on=BudgetExhausted)
    return outcome.rows


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


@dataclass(frozen=True)
class CutScan:
    """What one pass of `flag_dividend_cuts` actually did."""

    cuts: int
    """Rows this pass flagged as a cut. THE number the report means."""

    rows_touched: int
    """Rows whose prior/flag changed. On a fresh database, every dividend."""

    without_prior: int
    """Rows with no earlier dividend to compare against — not cuts."""

    def __str__(self) -> str:
        return (
            f"{self.cuts} cut(s) from {self.rows_touched} row(s) examined "
            f"({self.without_prior} had no prior dividend on record)"
        )


def flag_dividend_cuts() -> CutScan:
    """Mark a distribution smaller than the prior one (D19(5)).

    A dividend cut is an automatic thesis-review trigger, so the comparison is
    stored on the row rather than recomputed every time alerting asks.

    RETURNS THE CUT COUNT, not the rowcount. The first production run on
    2026-10-21 reported `cuts=84` against `actions=84` on a fresh database,
    which read as "every dividend in the S&P 500 was cut". Nothing was wrong
    with the comparison — `prior IS NOT NULL` already excludes a first
    observation, and the true count was zero — but this function returned
    `db.execute`'s ROWCOUNT, and on an empty database every row is written for
    the first time. The giveaway was the second run reporting `cuts=0`: a
    change-count, not a measurement.

    A metric that reads alarming and means nothing is worse than no metric. It
    trains the operator to ignore the number, which is the one thing a
    thesis-review trigger cannot afford.
    """
    row = db.fetch_one(
        """
        WITH ranked AS (
            SELECT symbol, ex_date, cash_amount,
                   lag(cash_amount) OVER (PARTITION BY symbol ORDER BY ex_date) AS prior
            FROM   corporate_actions
            WHERE  action_type = 'cash_dividend'
        ),
        updated AS (
            UPDATE corporate_actions ca
            SET    prior_cash_amount = r.prior,
                   is_dividend_cut   = (r.prior IS NOT NULL AND r.cash_amount < r.prior),
                   updated_at        = now()
            FROM   ranked r
            WHERE  ca.symbol = r.symbol
              AND  ca.ex_date = r.ex_date
              AND  ca.action_type = 'cash_dividend'
              -- Re-evaluate when EITHER the prior changed OR the stored flag
              -- disagrees with what the data now implies. Testing only the
              -- prior (plus `is_dividend_cut IS NULL`) left a stale flag
              -- whenever the provider RESTATED an amount: the prior is
              -- unchanged, the flag is already set, so the row was skipped and
              -- a corrected-upward dividend kept its "cut" flag for ever — a
              -- false D19(5) thesis-review trigger with no way to clear.
              -- `IS DISTINCT FROM` also covers the NULL case this replaces.
              AND  (ca.prior_cash_amount IS DISTINCT FROM r.prior
                    OR ca.is_dividend_cut IS DISTINCT FROM
                       (r.prior IS NOT NULL AND r.cash_amount < r.prior))
            RETURNING ca.is_dividend_cut, ca.prior_cash_amount
        )
        SELECT count(*) FILTER (WHERE is_dividend_cut)        AS cuts,
               count(*)                                      AS rows_touched,
               count(*) FILTER (WHERE prior_cash_amount IS NULL) AS without_prior
        FROM   updated
        """
    )
    return CutScan(
        cuts=int(row["cuts"]),
        rows_touched=int(row["rows_touched"]),
        without_prior=int(row["without_prior"]),
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
        counts["earnings"] = refresh_symbol_earnings(client, sorted(universe), run=job)

        div_events, div_actions = parse_dividends(client.dividend_calendar(start, end), universe)
        counts["dividends"] = db.executemany(UPSERT_EVENT, div_events)

        split_events, split_actions = parse_splits(client.split_calendar(start, end), universe)
        counts["splits"] = db.executemany(UPSERT_EVENT, split_events)

        counts["actions"] = db.executemany(UPSERT_ACTION, div_actions + split_actions)
        scan = flag_dividend_cuts()
        counts["cuts"] = scan.cuts
        log.info("dividend cut scan: %s", scan)

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
        scan = flag_dividend_cuts()
        job.note(
            failed_symbols=sorted(outcome.failed)[:50],
            dividend_cuts_flagged=scan.cuts,
            dividend_rows_examined=scan.rows_touched,
            dividends_without_prior=scan.without_prior,
        )
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
