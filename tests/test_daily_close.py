"""Orchestrator tests. Stub steps, no network, no DB.

The behaviour under test is the refusal in CLAUDE.md — "the orchestrator refuses
to mark a day complete if a snapshot job failed" — plus the dependency rules and
the exit-code policy that decides whether cron wakes anyone up.
"""

from __future__ import annotations

from datetime import date

import pytest

from core.budget import BudgetExhausted
from ingest.fmp_estimates import EstimatesPlanBlocked
from jobs import daily_close
from jobs.daily_close import RunReport, Step, StepResult, build_plan, format_report

DAY = date(2026, 10, 20)


def report(results, *, complete=False, blocked=(), skips=()):
    return RunReport(
        session_date=DAY,
        results=list(results),
        day_complete=complete,
        blocked_by=list(blocked),
        expected_skips=list(skips),
    )


# ══════════════════════════════════════════════════════════════════════════
# The plan
# ══════════════════════════════════════════════════════════════════════════


def test_the_three_sacred_jobs_are_marked_critical():
    """D7: the chain snapshot, the IV derived from it, and the estimates."""
    assert {s.name for s in build_plan() if s.critical} == {
        "schwab_chains",
        "schwab_iv",
        "fmp_estimates",
    }


def test_prices_are_not_critical():
    """Schwab still sells yesterday's bar tomorrow; a chain snapshot is gone."""
    assert next(s for s in build_plan() if s.name == "schwab_prices").critical is False


def test_iv_derivation_runs_after_the_chain_pull():
    plan = {s.name: s for s in build_plan()}
    assert plan["schwab_iv"].depends_on == ("schwab_chains",)
    names = [s.name for s in build_plan()]
    assert names.index("schwab_chains") < names.index("schwab_iv")


def test_fundamentals_run_after_events():
    """Its staleness check asks `events` whether a symbol has just reported, and
    events is also the job the D29 budget reservation protects."""
    plan = {s.name: s for s in build_plan()}
    assert plan["fmp_fundamentals"].depends_on == ("fmp_events",)
    names = [s.name for s in build_plan()]
    assert names.index("fmp_events") < names.index("fmp_fundamentals")


def test_news_is_the_last_INGESTION_step():
    """It is the slowest and nothing ingested depends on it.

    No longer the last step overall: the derived layers (scoring, tracking,
    digest) follow every ingestion step, because they read what was ingested.
    """
    names = [s.name for s in build_plan()]
    derived = {"scoring_pass", "outcome_tracker", "daily_digest"}
    ingestion = [n for n in names if n not in derived]
    assert ingestion[-1] == "finnhub_news"


def test_the_derived_layers_come_after_all_ingestion_and_in_order():
    """The digest must run AFTER the tracker (D16): it reports yesterday's
    outcome changes alongside today's ideas."""
    names = [s.name for s in build_plan()]
    derived = {"scoring_pass", "outcome_tracker", "daily_digest"}
    first_derived = min(names.index(n) for n in derived)
    assert all(names.index(n) >= first_derived for n in derived)
    assert names.index("outcome_tracker") < names.index("daily_digest")


def test_the_derived_layers_are_all_non_critical():
    """The inverse of the snapshot jobs' reason: every one is recomputable
    from data already stored. A failed scoring pass costs an evening; a failed
    chain snapshot costs a day of history nobody can sell back."""
    plan = {s.name: s for s in build_plan()}
    for name in ("scoring_pass", "outcome_tracker", "daily_digest"):
        assert plan[name].critical is False, f"{name} must not gate the day"


def test_only_estimates_declares_a_known_gap():
    gaps = {s.name: s.known_gaps for s in build_plan() if s.known_gaps}
    assert gaps == {"fmp_estimates": (EstimatesPlanBlocked,)}


def test_every_step_has_a_description():
    assert all(s.description for s in build_plan())


def test_the_universe_rebuild_is_not_in_the_nightly_pipeline():
    """It runs weekly; everything else FKs to `tickers` (see the README cron)."""
    assert "universe" not in {s.name for s in build_plan()}


# ══════════════════════════════════════════════════════════════════════════
# Execution and dependencies
# ══════════════════════════════════════════════════════════════════════════


def stub_plan(monkeypatch, steps, *, complete=True, blocked=()):
    monkeypatch.setattr(daily_close, "build_plan", lambda: steps)
    monkeypatch.setattr(daily_close, "day_is_complete", lambda _day: complete)
    monkeypatch.setattr(daily_close, "failed_critical_jobs", lambda _day: list(blocked))


def test_steps_run_in_order(monkeypatch):
    order = []
    stub_plan(
        monkeypatch,
        [
            Step("a", lambda: order.append("a") or 1, description="x"),
            Step("b", lambda: order.append("b") or 2, description="x"),
        ],
    )
    result = daily_close.run_pipeline(day=DAY)
    assert order == ["a", "b"]
    assert [r.status for r in result.results] == ["success", "success"]


def test_one_failing_step_does_not_stop_the_others(monkeypatch):
    """A broken news pull must not cost us the estimate snapshot."""
    ran = []

    def boom():
        raise RuntimeError("provider down")

    stub_plan(
        monkeypatch,
        [
            Step("news", boom, description="x"),
            Step("estimates", lambda: ran.append("e") or 5, critical=True, description="x"),
        ],
    )
    result = daily_close.run_pipeline(day=DAY)
    assert ran == ["e"]
    assert [r.status for r in result.results] == ["failed", "success"]


def test_a_dependent_step_is_skipped_when_its_dependency_fails(monkeypatch):
    """Deriving a volatility surface from a half-written chain would store a
    confident-looking wrong number."""

    def boom():
        raise RuntimeError("chain pull died")

    ran = []
    stub_plan(
        monkeypatch,
        [
            Step("schwab_chains", boom, critical=True, description="x"),
            Step(
                "schwab_iv",
                lambda: ran.append("iv") or 1,
                critical=True,
                depends_on=("schwab_chains",),
                description="x",
            ),
        ],
    )
    result = daily_close.run_pipeline(day=DAY)
    assert ran == []
    assert result.results[1].status == "skipped"
    assert "depends on schwab_chains" in result.results[1].detail


def test_schwab_steps_are_skipped_wholesale_when_unprovisioned(monkeypatch):
    ran = []
    stub_plan(
        monkeypatch,
        [
            Step("schwab_prices", lambda: ran.append("p") or 1, description="x"),
            Step("fmp_events", lambda: ran.append("e") or 1, description="x"),
        ],
    )
    result = daily_close.run_pipeline(day=DAY, unprovisioned=frozenset({"schwab"}))
    assert ran == ["e"]
    assert result.results[0].status == "skipped"


def test_one_unprovisioned_provider_does_not_skip_another(monkeypatch):
    """The coupling this replaced: `skip_schwab=bool(expected_skips)` meant an
    unconfigured SEC User-Agent would skip the SACRED chain and IV jobs,
    failing the day over an unrelated provider."""
    ran = []
    stub_plan(
        monkeypatch,
        [
            Step(
                "schwab_chains", lambda: ran.append("chains") or 1, critical=True, description="x"
            ),
            Step("edgar_insiders", lambda: ran.append("edgar") or 1, description="x"),
        ],
    )
    result = daily_close.run_pipeline(day=DAY, unprovisioned=frozenset({"edgar"}))

    assert ran == ["chains"], "an unprovisioned EDGAR must not touch Schwab"
    statuses = {r.name: r.status for r in result.results}
    assert statuses["schwab_chains"] == "success"
    assert statuses["edgar_insiders"] == "skipped"


def test_only_runs_the_named_steps(monkeypatch):
    ran = []
    stub_plan(
        monkeypatch,
        [
            Step("a", lambda: ran.append("a") or 1, description="x"),
            Step("b", lambda: ran.append("b") or 1, description="x"),
        ],
    )
    daily_close.run_pipeline(day=DAY, only=["b"])
    assert ran == ["b"]


# ══════════════════════════════════════════════════════════════════════════
# THE refusal: read from the database, never from this run
# ══════════════════════════════════════════════════════════════════════════


def test_completion_is_read_from_the_database_not_from_this_run(monkeypatch):
    """A snapshot that failed in an EARLIER run this same evening still blocks.

    This is why day_complete comes from v_day_status rather than from the step
    results in memory: a crash between steps must not be able to produce a
    false "complete".
    """
    stub_plan(
        monkeypatch,
        [Step("a", lambda: 1, description="x")],
        complete=False,
        blocked=[{"job_name": "schwab_chains", "status": "failed", "error": "earlier run"}],
    )
    result = daily_close.run_pipeline(day=DAY)
    assert all(r.status == "success" for r in result.results)  # this run was clean
    assert result.day_complete is False  # the day is not
    assert result.blocked_by[0]["job_name"] == "schwab_chains"


def test_the_orchestrator_never_computes_completion_from_its_own_step_list():
    """Asserted on the source: a future edit must not "optimise" this into a
    local calculation, which would drift from the database the moment a critical
    job is added or an earlier run of the evening failed."""
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "jobs" / "daily_close.py").read_text()
    tree = ast.parse(source)
    assigns = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "day_complete"
    ]
    assert assigns, "day_complete should be assigned somewhere"
    # The only assignment must come from day_is_complete(...).
    assert "report.day_complete = day_is_complete(day)" in source
    assert source.count("report.day_complete =") == 1


def test_the_dry_run_names_where_completion_comes_from(capsys):
    assert daily_close.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "v_day_status" in out
    assert "never from this list" in out


# ══════════════════════════════════════════════════════════════════════════
# Known gaps vs failures (D29)
# ══════════════════════════════════════════════════════════════════════════


def test_a_plan_gated_estimates_run_is_a_known_gap_not_a_failure(monkeypatch):
    """D29: while the endpoint 402s, the day can still complete."""

    def gated():
        raise EstimatesPlanBlocked("analyst-estimates is 402 on this plan")

    stub_plan(
        monkeypatch,
        [
            Step(
                "fmp_estimates",
                gated,
                critical=True,
                known_gaps=(EstimatesPlanBlocked,),
                description="x",
            )
        ],
        complete=True,
    )
    result = daily_close.run_pipeline(day=DAY)
    assert [r.status for r in result.results] == ["known_gap"]
    assert result.failed == []
    assert result.day_complete is True
    assert result.degraded is False  # complete is complete


def test_the_day_can_complete_with_estimates_plan_gated(monkeypatch):
    """The whole point: a billing decision must not hold the D7 chain clock
    hostage. The estimates job records a NON-critical skipped row while gated,
    so v_day_status is unaffected."""

    def gated():
        raise EstimatesPlanBlocked("402")

    stub_plan(
        monkeypatch,
        [
            Step("schwab_chains", lambda: 500, critical=True, description="x"),
            Step(
                "fmp_estimates",
                gated,
                critical=True,
                known_gaps=(EstimatesPlanBlocked,),
                description="x",
            ),
        ],
        complete=True,
    )
    result = daily_close.run_pipeline(day=DAY)
    assert result.day_complete is True
    assert [r.name for r in result.known_gaps] == ["fmp_estimates"]


def test_a_budget_exhausted_step_is_a_known_gap(monkeypatch):
    """Declined spend, not a failure (D29)."""

    def exhausted():
        raise BudgetExhausted("no calls left today")

    stub_plan(monkeypatch, [Step("fmp_fundamentals", exhausted, description="x")])
    result = daily_close.run_pipeline(day=DAY)
    assert [r.status for r in result.results] == ["known_gap"]
    assert result.failed == []


def test_an_unexpected_exception_from_a_gap_declaring_step_is_still_a_failure(monkeypatch):
    """Only the DECLARED exception is a gap. A timeout is a real failure."""

    def boom():
        raise RuntimeError("connection reset")

    stub_plan(
        monkeypatch,
        [
            Step(
                "fmp_estimates",
                boom,
                critical=True,
                known_gaps=(EstimatesPlanBlocked,),
                description="x",
            )
        ],
        complete=False,
    )
    result = daily_close.run_pipeline(day=DAY)
    assert [r.status for r in result.results] == ["failed"]


# ══════════════════════════════════════════════════════════════════════════
# Degraded vs failed
# ══════════════════════════════════════════════════════════════════════════


def test_incomplete_because_unprovisioned_is_degraded_not_failed():
    """Nightly mail about a gap we already accepted is noise, not signal."""
    r = report(
        [StepResult("schwab_chains", "skipped", "Schwab is not provisioned")],
        complete=False,
        skips=["schwab: no token"],
    )
    assert r.degraded is True
    assert r.failed == []


def test_incomplete_because_of_a_known_gap_is_degraded():
    r = report([StepResult("fmp_estimates", "known_gap", "402")], complete=False)
    assert r.degraded is True


def test_incomplete_with_a_real_failure_is_not_degraded():
    r = report(
        [StepResult("fmp_estimates", "failed", error="boom")],
        complete=False,
        skips=["schwab: no token"],
    )
    assert r.degraded is False
    assert [x.name for x in r.failed] == ["fmp_estimates"]


def test_incomplete_with_no_explanation_is_not_degraded():
    """Something broke that we did not anticipate. That deserves an email."""
    assert report([StepResult("a", "success")], complete=False).degraded is False


def test_a_complete_day_is_never_degraded():
    r = report([StepResult("a", "success")], complete=True, skips=["schwab: no token"])
    assert r.degraded is False


# ══════════════════════════════════════════════════════════════════════════
# The report humans read at 5pm
# ══════════════════════════════════════════════════════════════════════════


def test_a_complete_day_says_so():
    text = format_report(
        report([StepResult("fmp_estimates", "success", "412 rows")], complete=True)
    )
    assert "DAY COMPLETE" in text
    assert "412 rows" in text


def test_an_incomplete_day_names_what_blocked_it():
    text = format_report(
        report(
            [StepResult("schwab_chains", "failed", error="token expired")],
            complete=False,
            blocked=[{"job_name": "schwab_chains", "status": "failed", "error": "token expired"}],
        )
    )
    assert "DAY NOT COMPLETE" in text
    assert "blocked by schwab_chains" in text
    assert "token expired" in text


def test_the_report_distinguishes_a_known_gap_from_a_failure():
    text = format_report(
        report([StepResult("fmp_estimates", "known_gap", "402 on this plan")], complete=False)
    )
    assert "known gap: fmp_estimates" in text
    assert "FAIL" not in text


def test_the_report_names_unprovisioned_providers():
    text = format_report(
        report([StepResult("schwab_chains", "skipped")], complete=False, skips=["schwab: no token"])
    )
    assert "not provisioned" in text


@pytest.mark.parametrize("outcome", [12, {"earnings": 3, "splits": 1}, None])
def test_step_outcomes_are_summarised_without_crashing(outcome):
    assert isinstance(daily_close._describe(outcome), str)
