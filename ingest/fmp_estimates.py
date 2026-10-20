"""Daily analyst-estimate snapshot (D7). **SACRED JOB — must never silently fail.**

    ALLOW_LIVE_CALLS=1 python -m ingest.fmp_estimates

D7: snapshot FMP estimates daily, and after 60-90 days we own estimate-revision
momentum. The signal is not the estimate; it is the *change* between consecutive
`snapshot_date`s. That makes this output impossible to reconstruct after the
fact — FMP sells today's consensus, not last Tuesday's — so a missed day is a
day of signal gone for good.

Consequences that are load-bearing rather than stylistic:

  * `is_critical=True`, so a failure blocks the orchestrator from marking the
    day complete.
  * The write is `ON CONFLICT ... DO UPDATE` on the full key INCLUDING
    `snapshot_date`, so a re-run refreshes today and leaves every prior day
    untouched. `tests/test_snapshot_immutability.py` enforces that no code path
    in this package can ever DELETE from the table.

**D29 status: EXPECTED-BLOCKED.** `/stable/analyst-estimates` is 402 on the free
tier. `run()` raises `EstimatesPlanBlocked`, which the orchestrator treats as a
known gap rather than a failure — `day_complete` can still be true. It flips
back to a hard failure the day a probe shows the endpoint passing, and **the
estimate-revision clock starts that day**: record it.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from core import clock, config, db
from core.budget import BudgetExhausted
from core.jobs import job_run
from ingest.fmp_client import FMPClient, FMPPlanError
from ingest.runner import as_float, as_int, for_each_symbol

log = logging.getLogger(__name__)

JOB_NAME = "fmp_estimates"

UPSERT = """
    INSERT INTO estimate_snapshots (
        snapshot_date, symbol, period_type, fiscal_period_end,
        estimated_revenue_avg, estimated_revenue_low, estimated_revenue_high,
        estimated_eps_avg, estimated_eps_low, estimated_eps_high,
        estimated_ebitda_avg, estimated_net_income_avg,
        analyst_count_revenue, analyst_count_eps, source
    ) VALUES (
        %(snapshot_date)s, %(symbol)s, %(period_type)s, %(fiscal_period_end)s,
        %(revenue_avg)s, %(revenue_low)s, %(revenue_high)s,
        %(eps_avg)s, %(eps_low)s, %(eps_high)s,
        %(ebitda_avg)s, %(net_income_avg)s,
        %(analysts_revenue)s, %(analysts_eps)s, 'fmp'
    )
    ON CONFLICT (symbol, snapshot_date, period_type, fiscal_period_end) DO UPDATE SET
        estimated_revenue_avg    = EXCLUDED.estimated_revenue_avg,
        estimated_revenue_low    = EXCLUDED.estimated_revenue_low,
        estimated_revenue_high   = EXCLUDED.estimated_revenue_high,
        estimated_eps_avg        = EXCLUDED.estimated_eps_avg,
        estimated_eps_low        = EXCLUDED.estimated_eps_low,
        estimated_eps_high       = EXCLUDED.estimated_eps_high,
        estimated_ebitda_avg     = EXCLUDED.estimated_ebitda_avg,
        estimated_net_income_avg = EXCLUDED.estimated_net_income_avg,
        analyst_count_revenue    = EXCLUDED.analyst_count_revenue,
        analyst_count_eps        = EXCLUDED.analyst_count_eps,
        ingested_at              = now()
"""


class EstimatesPlanBlocked(RuntimeError):
    """The estimates endpoint is plan-gated (D29).

    Distinct from a failure: the orchestrator records this as a KNOWN GAP so
    `day_complete` is not held hostage to a billing decision. The moment the
    endpoint passes a probe this becomes an ordinary critical failure again.
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


def parse_estimates(
    rows: list[dict],
    *,
    symbol: str,
    period_type: str,
    snapshot_date: date,
    limit: int,
) -> list[dict]:
    """Map FMP `/stable/analyst-estimates` rows to `estimate_snapshots` params.

    **Only future (or current) fiscal periods are kept.** An "estimate" for a
    period that has already reported is a historical curiosity, not a forward
    expectation, and diffing it would manufacture revision signal out of FMP
    backfilling its own history.
    """
    parsed: list[dict] = []
    for row in rows:
        period_end = _as_date(row.get("date"))
        if period_end is None or period_end < snapshot_date:
            continue
        parsed.append(
            {
                "snapshot_date": snapshot_date,
                "symbol": symbol,
                "period_type": period_type,
                "fiscal_period_end": period_end,
                "revenue_avg": as_float(row.get("estimatedRevenueAvg")),
                "revenue_low": as_float(row.get("estimatedRevenueLow")),
                "revenue_high": as_float(row.get("estimatedRevenueHigh")),
                "eps_avg": as_float(row.get("estimatedEpsAvg")),
                "eps_low": as_float(row.get("estimatedEpsLow")),
                "eps_high": as_float(row.get("estimatedEpsHigh")),
                "ebitda_avg": as_float(row.get("estimatedEbitdaAvg")),
                "net_income_avg": as_float(row.get("estimatedNetIncomeAvg")),
                "analysts_revenue": as_int(row.get("numberAnalystEstimatedRevenue")),
                "analysts_eps": as_int(row.get("numberAnalystsEstimatedEps")),
            }
        )
    parsed.sort(key=lambda r: r["fiscal_period_end"])
    return parsed[:limit]


def snapshot_symbol(
    client: FMPClient, symbol: str, *, snapshot_date: date, scope: config.EstimateScope
) -> int:
    """Pull and store one symbol's estimates for every configured period type."""
    rows: list[dict] = []
    for period_type in scope.period_types:
        limit = scope.quarters_ahead if period_type == "quarter" else scope.annuals_ahead
        # Ask for more than we keep: FMP returns history and future together,
        # and parse_estimates drops the past.
        payload = client.analyst_estimates(symbol, period=period_type, limit=limit * 4)
        rows.extend(
            parse_estimates(
                payload,
                symbol=symbol,
                period_type=period_type,
                snapshot_date=snapshot_date,
                limit=limit,
            )
        )
    return db.executemany(UPSERT, rows) if rows else 0


def plan_blocks_estimates(client: FMPClient) -> bool:
    """Is the estimates endpoint plan-gated right now? One cheap call.

    True ONLY on a 402. Any other error means the endpoint might well work, so
    the job stays critical and fails normally — a transient network problem must
    not be mistaken for a billing decision and quietly let the day complete.
    """
    try:
        client.analyst_estimates("AAPL", period="quarter", limit=1)
    except FMPPlanError:
        return True
    except BudgetExhausted:
        # NOT a billing answer — we could not afford to ask. Swallowing this
        # into `return False` would report "the plan covers it" on the strength
        # of a question never put, leaving the job critical and about to fail
        # for a reason that has nothing to do with the plan. The caller decides.
        raise
    except Exception:
        return False
    return False


def run(client: FMPClient | None = None, *, symbols: list[str] | None = None) -> int:
    client = client or FMPClient()
    scope = config.universe().estimates
    day = clock.session_date()
    universe = symbols if symbols is not None else db.active_symbols()

    if not universe:
        raise RuntimeError(
            "no active tickers; run `python -m ingest.universe` before the snapshot jobs"
        )

    # Check the plan gate ONCE rather than failing 500 times, and BEFORE opening
    # the job_runs row — because the answer decides whether this job is a gate.
    #
    # While the endpoint 402s the job is recorded NON-critical. That is not a
    # loosening of D7: a job that cannot possibly succeed is not a gate, and
    # holding `day_complete` hostage to a billing decision would mean the chain
    # snapshot clock could never start either. The flag flips back the moment a
    # probe passes, which is also the day the estimate-revision clock starts.
    try:
        blocked = plan_blocks_estimates(client)
    except BudgetExhausted as exc:
        # The day's FMP allowance is gone, so this job cannot run AT ALL — it
        # cannot even find out whether it is allowed to.
        #
        # Recorded CRITICAL and skipped, unlike the plan-gated case. The
        # difference is knowledge, not inconvenience: a confirmed 402 is a
        # standing answer that the snapshot is unobtainable on this plan, so
        # treating it as a non-gate is a decision already taken (D29). A spent
        # budget tells us only that we could not ask today, and defaulting
        # "don't know" to "not a gate" would let a genuinely broken estimates
        # job certify a complete day. A skipped critical job keeps
        # `day_complete` false while the orchestrator still exits 0 on the
        # known gap, which is the honest reading: nothing is broken, and the
        # snapshot was not taken.
        with job_run(
            JOB_NAME,
            session_date=day,
            is_critical=True,
            triggered_by="manual",
            clean_stop=BudgetExhausted,
        ) as job:
            job.skip(f"no FMP budget left to probe or snapshot estimates: {exc}")
        raise

    with job_run(
        JOB_NAME,
        session_date=day,
        is_critical=not blocked,
        triggered_by="manual",
        clean_stop=(EstimatesPlanBlocked, BudgetExhausted),
    ) as job:
        if blocked:
            job.skip("analyst-estimates is plan-gated (D29); estimate clock not started")
            raise EstimatesPlanBlocked(
                "/stable/analyst-estimates is 402 on this plan. The D7 estimate-revision "
                "history cannot start until the plan is upgraded; the orchestrator treats "
                "this as a known gap, not a failure. Re-run scripts/fmp_probe.py after an "
                "upgrade, and RECORD THE DATE — that is when the clock starts."
            )

        outcome = for_each_symbol(
            universe,
            lambda s: snapshot_symbol(client, s, snapshot_date=day, scope=scope),
            run=job,
            label="estimates",
            # D29: a spent day budget is DECLINED SPEND, not 500 bad symbols.
            # Without this the shared counter running dry makes every symbol
            # "fail", `for_each_symbol` raises TooManyFailures on the all-failed
            # rule, and a critical job fails for a reason the design calls
            # normal — which blocks `day_complete` for the whole day.
            stop_on=BudgetExhausted,
        )
        job.note(failed_symbols=sorted(outcome.failed)[:50], snapshot_date=str(day))

        if outcome.stopped_early and not outcome.rows:
            # Stopped cleanly on the budget before writing a single snapshot.
            # `stop_on` keeps that from being counted as N symbol failures (D29),
            # but the job still collected NOTHING, and a critical job reporting
            # `success / 0 rows` is the silent zero-row success the whole design
            # is built to prevent — `v_day_status` would then certify a
            # `day_complete` that rests on a snapshot nobody took.
            job.skip(f"no estimates written: {outcome.stopped_early}")
            raise BudgetExhausted(
                f"estimates stopped on the FMP day budget before writing anything "
                f"({outcome.declined} symbol(s) declined). Treated as a known gap, "
                f"not a failure (D29) — but the day is NOT complete, because this "
                f"snapshot was not taken."
            )

    return outcome.rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--symbols", nargs="*", help="override the universe (debugging)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        rows = run(symbols=args.symbols)
    except EstimatesPlanBlocked as exc:
        print(f"SKIPPED (known gap): {exc}")
        return 0
    print(f"estimate snapshot for {clock.session_date()}: {rows} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
