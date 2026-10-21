"""Post-close orchestrator (D15). Runs the day's jobs in dependency order.

    ALLOW_LIVE_CALLS=1 python -m jobs.daily_close
    python -m jobs.daily_close --dry-run          # print the plan, run nothing
    ALLOW_LIVE_CALLS=1 python -m jobs.daily_close --only fmp_events finnhub_news
    ALLOW_LIVE_CALLS=1 python -m jobs.daily_close --strict

CLAUDE.md: "The orchestrator refuses to mark a day complete if a snapshot job
failed." That refusal is the whole reason this file exists, and it is enforced
**against the database, never against a hard-coded job list**:
`core.jobs.day_is_complete` reads `v_day_status`, which asks whether every row
flagged `is_critical` for this session date recorded `success`. So a snapshot
that failed in an earlier run of the same evening still blocks completion, a
crash between steps cannot produce a false "complete", and adding a critical job
later cannot leave this rule out of date.

Exit codes, because cron reads them:

    0   every runnable step succeeded and the day is complete
    0   the day is incomplete ONLY because of a KNOWN GAP — Schwab not
        provisioned, or the estimates endpoint plan-gated (D29). Reported as
        DEGRADED, so the nightly mail is not noise for a gap we already decided
        to accept.
    1   a step failed, or the day is incomplete for any other reason
    1   any incompleteness at all, under --strict

Anything reaching exit 1 also emails ALERT_EMAIL_ONFAIL (D16), deduped so a
repeated failure on one day sends one message.
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from alerts import digest
from alerts import notify as notifier
from core import clock, config, db, settings
from core.budget import BudgetExhausted, DayBudget
from core.jobs import clear_stale_runs, day_is_complete, failed_critical_jobs, job_run
from ingest import (
    edgar_insiders,
    finnhub_news,
    fmp_estimates,
    fmp_events,
    fmp_fundamentals,
    schwab_chains,
    schwab_prices,
)
from ingest.fmp_estimates import EstimatesPlanBlocked
from ingest.schwab_client import SchwabTokenError, check_token
from jobs import scoring_pass
from shadow_book import outcome_tracker

log = logging.getLogger(__name__)

JOB_NAME = "daily_close"


@dataclass
class Step:
    """One unit of the pipeline. `run` writes its own `job_runs` row."""

    name: str
    run: Callable[[], object]
    # Mirrors the is_critical flag the step itself records, so --dry-run can
    # show the plan without touching the database. `day_complete` is NOT
    # computed from this — it comes from v_day_status.
    critical: bool = False
    depends_on: tuple[str, ...] = ()
    description: str = ""
    # Exceptions that mean "a gap we already decided to accept", not a failure.
    known_gaps: tuple[type[BaseException], ...] = ()


@dataclass
class StepResult:
    name: str
    status: str  # success | failed | skipped | known_gap
    detail: str = ""
    error: str | None = None


@dataclass
class RunReport:
    session_date: date
    results: list[StepResult] = field(default_factory=list)
    day_complete: bool = False
    blocked_by: list[dict] = field(default_factory=list)
    expected_skips: list[str] = field(default_factory=list)

    @property
    def failed(self) -> list[StepResult]:
        return [r for r in self.results if r.status == "failed"]

    @property
    def known_gaps(self) -> list[StepResult]:
        return [r for r in self.results if r.status == "known_gap"]

    @property
    def degraded(self) -> bool:
        """Incomplete, but only for reasons we already accepted."""
        if self.day_complete or self.failed:
            return False
        return bool(self.expected_skips or self.known_gaps)


def build_plan() -> list[Step]:
    """The pipeline, in dependency order.

    Ordering constraints that are real, not stylistic:

      * `universe` is NOT here — it runs weekly (see the README cron block), and
        every other table FKs to `tickers`. The preflight checks it is populated
        instead of rebuilding it nightly under a bad provider response.
      * `schwab_iv` reads the chain rows `schwab_chains` just wrote, so it must
        follow and is skipped if that failed — deriving a volatility surface
        from a half-written chain would store a confident-looking wrong number.
      * `fmp_fundamentals` follows `fmp_events`, because its staleness check
        asks `events` whether a symbol has reported since the last pull.
      * `fmp_events` runs BEFORE fundamentals for a second reason: it is the job
        the D29 budget reservation protects, and spending its three calendar
        calls early means a fundamentals crawl cannot crowd it out even if the
        reservation were ever misconfigured.
      * `finnhub_news` is last: it is the slowest (one call per symbol at 60/min)
        and the only one nothing else depends on, so a timeout there costs least.
    """
    return [
        Step(
            "schwab_prices",
            lambda: schwab_prices.run(),
            description="daily OHLCV, raw and unadjusted (D15/D24)",
        ),
        Step(
            "schwab_chains",
            lambda: schwab_chains.snapshot_chains(),
            critical=True,
            description="D7/D22 option-chain snapshot — SACRED",
        ),
        Step(
            "schwab_iv",
            lambda: schwab_chains.derive_iv(),
            critical=True,
            depends_on=("schwab_chains",),
            description="aggregate IV derived from today's chains — SACRED",
        ),
        Step(
            "fmp_estimates",
            lambda: fmp_estimates.run(),
            critical=True,
            description="D7 dated estimate snapshot — SACRED (revision clock since 2026-10-21)",
            known_gaps=(EstimatesPlanBlocked,),
        ),
        Step(
            "fmp_events",
            lambda: fmp_events.run(),
            description="earnings/dividend/split calendars + corporate actions",
        ),
        Step(
            "fmp_fundamentals",
            lambda: fmp_fundamentals.run(),
            depends_on=("fmp_events",),
            description="quarterly financials + ratios, stale symbols only",
        ),
        Step(
            "edgar_insiders",
            lambda: edgar_insiders.run(),
            # NON-CRITICAL, and the reason is the opposite of the chain jobs':
            # EDGAR filings are permanent public records, re-fetchable at any
            # time, so a failure costs FRESHNESS rather than history. The
            # snapshot jobs are critical precisely because their data cannot be
            # bought back.
            description="Form 4 insider transactions (Pillar C, point-in-time)",
        ),
        Step(
            "finnhub_news",
            lambda: finnhub_news.run(),
            description="rolling 7-day company news (Pillar D corpus)",
        ),
        # ── derived layers ──────────────────────────────────────────────────
        # All three NON-CRITICAL, and for a reason that is the inverse of the
        # snapshot jobs': every one of these is recomputable from data already
        # stored. A failed scoring pass costs an evening; a failed chain
        # snapshot costs a day of history nobody can sell back.
        Step(
            "scoring_pass",
            lambda: scoring_pass.run(),
            depends_on=("schwab_prices",),
            description="score the universe, rank, log surfaced ideas (D33/D41/D42)",
        ),
        Step(
            "outcome_tracker",
            lambda: outcome_tracker.run(),
            depends_on=("schwab_prices",),
            description="mark open Shadow Book calls against today's bars (D13/D19)",
        ),
        Step(
            "daily_digest",
            lambda: _send_digest(),
            # AFTER the tracker, so the digest can report yesterday's outcome
            # changes alongside today's ideas (D16).
            depends_on=("outcome_tracker",),
            description="the post-close digest, on every configured channel (D16)",
        ),
    ]


def _send_digest() -> int:
    """Render and send the digest from this process's scoring pass.

    Reads the pass in-process rather than re-deriving: the two steps run
    seconds apart in the same pipeline, and scoring 433 symbols again to render
    them would double the run for nothing.
    """
    result = scoring_pass.latest()
    digest.run(result.cards, result.candidates, refusals=result.refusals)
    return len(result.cards) or len(result.candidates)


# ── preflight ───────────────────────────────────────────────────────────────


def preflight(day: date) -> tuple[list[str], list[str]]:
    """Return (hard_errors, expected_skips). Runs before anything is written.

    The split matters for alerting: a hard error emails, an expected skip does
    not. "Schwab is not provisioned yet" is a state we already know about and
    mailing about it nightly would train the operator to ignore the alert that
    matters.
    """
    errors: list[str] = []
    skips: list[str] = []

    problems = config.validate_all()
    if problems:
        errors.append("config is invalid:\n" + "\n".join(problems))

    if not db.ping():
        errors.append("cannot reach the database")
        return errors, skips  # nothing below this works without a DB

    from db.init import check as schema_check

    schema_problems = schema_check()
    if schema_problems:
        errors.append("schema is not ready: " + "; ".join(schema_problems))

    symbols = db.active_symbols()
    if not symbols:
        errors.append(
            "the ticker universe is empty — run `python -m ingest.universe` "
            "before the first daily close"
        )
    else:
        log.info("universe: %d active symbols", len(symbols))

    # Schwab is expected to be missing until gate 2. Detect it here so the
    # dependent steps are skipped cleanly rather than each raising in turn.
    try:
        check_token()
    except SchwabTokenError as exc:
        skips.append(f"schwab: {exc}")

    # EDGAR needs a declared contact address before it may be called at all.
    # An expected skip, not an error: it is a state we know about, and mailing
    # nightly about it would train the operator to ignore the alert that
    # matters. Scoped to `edgar` so it cannot skip another provider's steps.
    try:
        settings.sec_edgar_user_agent()
    except settings.SettingsError as exc:
        skips.append(f"edgar: {exc}")

    budget = DayBudget.for_fmp()
    remaining = budget.remaining(day)
    if remaining == 0:
        log.warning(
            "FMP day budget already exhausted (%d/%d spent); FMP steps will decline",
            budget.spent(day),
            budget.budget,
        )
    else:
        log.info("FMP budget: %d of %d calls remaining today", remaining, budget.budget)

    if not clock.is_weekday(day):
        log.warning("%s is a weekend; running anyway (cron normally skips it)", day)

    return errors, skips


# ── execution ───────────────────────────────────────────────────────────────


def run_pipeline(
    *,
    only: list[str] | None = None,
    unprovisioned: frozenset[str] = frozenset(),
    day: date | None = None,
) -> RunReport:
    """Run the plan. `unprovisioned` names PROVIDERS whose steps to skip.

    A set of provider names, not a boolean. The previous signature took
    `skip_schwab=bool(expected_skips)`, which worked only while Schwab was the
    only provider that could be unprovisioned: adding EDGAR meant an
    unconfigured SEC User-Agent would set that flag and skip the SACRED chain
    and IV jobs, failing the day over an unrelated provider. Step names are
    `provider_thing`, so the prefix is the provider.
    """
    day = day or clock.session_date()
    report = RunReport(session_date=day)
    plan = build_plan()
    if only:
        plan = [step for step in plan if step.name in set(only)]

    succeeded: set[str] = set()
    for step in plan:
        provider = step.name.split("_", 1)[0]
        if provider in unprovisioned:
            report.results.append(
                StepResult(step.name, "skipped", f"{provider} is not provisioned")
            )
            continue

        unmet = [dep for dep in step.depends_on if dep not in succeeded]
        if unmet:
            report.results.append(
                StepResult(step.name, "skipped", f"depends on {', '.join(unmet)}")
            )
            continue

        log.info("--- %s: %s", step.name, step.description)
        try:
            outcome = step.run()
        except step.known_gaps as exc:
            # A gap we already decided to accept (D29). The step records its own
            # non-critical, skipped job_runs row, so v_day_status is unaffected.
            log.info("%s: known gap — %s", step.name, exc)
            report.results.append(
                StepResult(step.name, "known_gap", detail=str(exc).splitlines()[0])
            )
            continue
        except BudgetExhausted as exc:
            # Declined spend, not a failure (D29).
            log.info("%s: stopped on the FMP day budget — %s", step.name, exc)
            report.results.append(
                StepResult(step.name, "known_gap", detail="FMP day budget exhausted")
            )
            continue
        except Exception as exc:
            log.exception("%s failed", step.name)
            report.results.append(
                StepResult(
                    step.name,
                    "failed",
                    error=f"{type(exc).__name__}: {exc}",
                    detail=traceback.format_exc()[-1500:],
                )
            )
            # Deliberately no `break`: a failed news pull must not cost us the
            # estimate snapshot. Dependency edges handle the cases that matter.
            continue

        succeeded.add(step.name)
        report.results.append(StepResult(step.name, "success", detail=_describe(outcome)))

    # THE refusal: read from the database, not from the results above.
    report.day_complete = day_is_complete(day)
    report.blocked_by = failed_critical_jobs(day)
    return report


def _describe(outcome: object) -> str:
    if isinstance(outcome, int):
        return f"{outcome} rows"
    if isinstance(outcome, dict):
        return ", ".join(f"{k}={v}" for k, v in outcome.items())
    return ""


def format_report(report: RunReport) -> str:
    lines = [f"session {report.session_date}", ""]
    width = max((len(r.name) for r in report.results), default=12)
    marker = {"success": "ok", "failed": "FAIL", "skipped": "skip", "known_gap": "gap"}
    for result in report.results:
        detail = result.error or (result.detail.splitlines()[0] if result.detail else "")
        lines.append(f"  {marker[result.status]:<5} {result.name:<{width}}  {detail[:80]}")

    lines.append("")
    if report.day_complete:
        lines.append("  DAY COMPLETE — every critical snapshot job succeeded")
    else:
        lines.append("  DAY NOT COMPLETE")
        for row in report.blocked_by:
            lines.append(
                f"    blocked by {row['job_name']} ({row['status']}): {row['error'] or ''}"
            )
        for gap in report.known_gaps:
            lines.append(f"    known gap: {gap.name} — {gap.detail}")
        for skip in report.expected_skips:
            lines.append(f"    not provisioned: {skip.splitlines()[0]}")
    return "\n".join(lines)


def notify_failure(report: RunReport) -> None:
    """Alert on the failure summary (D16). A snapshot job must never fail silently.

    Fans out to every configured channel. Telegram is the one that reaches a
    phone before the next session, which for an unbuyable snapshot series is
    the difference that matters.
    """
    failed = ", ".join(r.name for r in report.failed) or "day incomplete"
    delivery = notifier.notify(
        f"daily_close {report.session_date}: {failed}",
        format_report(report)
        + "\n\n"
        + "\n\n".join(f"--- {r.name} ---\n{r.detail}" for r in report.failed),
        alert_type="job_failure",
        severity="critical",
        dedupe_key=f"daily_close:{report.session_date}:{failed}",
    )
    if not delivery.any_sent:
        # Printed as well as logged: cron mails stdout, which may be the only
        # route left if every alert channel is down.
        print(f"WARNING: failure alert reached nobody ({delivery})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="print the plan, run nothing")
    parser.add_argument("--only", nargs="*", help="run just these steps")
    parser.add_argument(
        "--strict", action="store_true", help="treat ANY incompleteness as a failure"
    )
    parser.add_argument("--no-email", action="store_true", help="do not send the failure alert")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.dry_run:
        print("pipeline order:\n")
        for index, step in enumerate(build_plan(), start=1):
            flags = " [CRITICAL]" if step.critical else ""
            gaps = (
                f"  (known gap: {', '.join(g.__name__ for g in step.known_gaps)})"
                if step.known_gaps
                else ""
            )
            deps = f"  (after {', '.join(step.depends_on)})" if step.depends_on else ""
            print(f"  {index}. {step.name}{flags}{deps}{gaps}\n     {step.description}")
        print(
            "\nday_complete is read from v_day_status, never from this list — see the\n"
            "module docstring."
        )
        return 0

    day = clock.session_date()

    # A run killed hard leaves 'running' rows that block every future run of
    # those jobs via uq_job_runs_inflight. Release them before starting.
    released = clear_stale_runs()
    if released:
        log.warning("released %d abandoned job run(s) from a previous crash", released)

    errors, expected_skips = preflight(day)
    if errors:
        body = "daily_close preflight failed:\n\n" + "\n\n".join(errors)
        print(body)
        # Real failures only: an expected skip never reaches here.
        if not args.no_email:
            notifier.notify(
                f"daily_close {day}: preflight failed",
                body,
                dedupe_key=f"daily_close_preflight:{day}",
            )
        return 1

    with job_run(JOB_NAME, session_date=day, triggered_by="cron") as job:
        # Each skip line is "provider: detail", so the provider names come
        # straight from preflight rather than being inferred from a count.
        unprovisioned = frozenset(s.split(":", 1)[0].strip() for s in expected_skips)
        report = run_pipeline(only=args.only, unprovisioned=unprovisioned, day=day)
        report.expected_skips = expected_skips

        job.add_rows(sum(1 for r in report.results if r.status == "success"))
        job.add_failure(len(report.failed))
        job.note(
            steps={r.name: r.status for r in report.results},
            day_complete=report.day_complete,
            blocked_by=[r["job_name"] for r in report.blocked_by],
            known_gaps=[r.name for r in report.known_gaps],
            expected_skips=[s.splitlines()[0] for s in expected_skips],
        )

        print("\n" + format_report(report))

        if report.failed or (not report.day_complete and (args.strict or not report.degraded)):
            if not args.no_email:
                notify_failure(report)
            return 1

        if report.degraded:
            print(
                "\n  exit 0: incomplete only because of a gap we already accepted.\n"
                "  Use --strict to treat this as a failure."
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
