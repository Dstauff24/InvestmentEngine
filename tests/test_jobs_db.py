"""The job_runs writer, against a real database.

Excluded from the default run; see tests/dbguard.py for the wipe guard.

    INTEGRATION_DB_OK=1 pytest -m integration

What is here is the behaviour a unit test cannot establish: that the row really
closes from `finally` on every kind of exit, that the partial unique index really
blocks a concurrent run, and that `clear_stale_runs` really unwedges a job whose
process was killed before it could run any code.
"""

from __future__ import annotations

import contextlib
from datetime import date

import psycopg
import pytest

from core import clock, db
from core.jobs import (
    JobAlreadyRunning,
    clear_stale_runs,
    day_is_complete,
    failed_critical_jobs,
    job_run,
    last_run,
)
from tests.dbguard import assert_safe_database

pytestmark = pytest.mark.integration

DAY = date(2026, 10, 20)


@pytest.fixture(scope="module", autouse=True)
def _guard_database():
    from core import settings

    return assert_safe_database(settings.database_url())


@pytest.fixture(autouse=True)
def clean():
    assert_safe_database(db.settings.database_url())
    db.execute("DELETE FROM job_runs WHERE true")
    yield
    db.execute("DELETE FROM job_runs WHERE true")


def row(job_name: str) -> dict:
    return db.fetch_one(
        "SELECT * FROM job_runs WHERE job_name = %s ORDER BY id DESC LIMIT 1", (job_name,)
    )


# ── the happy path ──────────────────────────────────────────────────────────


def test_a_successful_run_records_its_counters():
    with job_run("demo", session_date=DAY) as run:
        run.add_rows(1234)
        run.symbol_ok(10)
        run.note(source="fixture")

    r = row("demo")
    assert r["status"] == "success"
    assert r["rows_written"] == 1234
    assert r["symbols_ok"] == 10
    assert r["finished_at"] is not None
    assert r["duration_ms"] is not None
    assert r["details"] == {"source": "fixture"}
    assert r["hostname"] and r["pid"]


def test_partial_is_recorded_when_some_symbols_failed():
    with job_run("demo", session_date=DAY) as run:
        run.add_rows(5)
        run.symbol_failed(2)
    assert row("demo")["status"] == "partial"


def test_skip_is_recorded_with_its_reason():
    with job_run("demo", session_date=DAY) as run:
        run.skip("endpoint is plan-gated (D29)")
    r = row("demo")
    assert r["status"] == "skipped"
    assert "plan-gated" in r["error"]


# ── the row closes from finally, on every kind of exit ──────────────────────


def test_an_ordinary_exception_still_closes_the_row():
    with pytest.raises(RuntimeError), job_run("demo", session_date=DAY) as run:
        run.add_rows(3)
        raise RuntimeError("provider exploded")

    r = row("demo")
    assert r["status"] == "failed"
    assert "RuntimeError: provider exploded" in r["error"]
    assert r["rows_written"] == 3  # counters up to the failure are kept
    assert r["finished_at"] is not None
    assert "traceback" in r["details"]


def test_a_keyboard_interrupt_still_closes_the_row():
    """Why the handler catches BaseException, not Exception.

    An unattended job left in 'running' wedges every later run of itself via
    uq_job_runs_inflight — so a Ctrl-C must not be able to leave that behind.
    """
    with pytest.raises(KeyboardInterrupt), job_run("demo", session_date=DAY):
        raise KeyboardInterrupt

    r = row("demo")
    assert r["status"] == "failed"
    assert "KeyboardInterrupt" in r["error"]


def test_a_system_exit_still_closes_the_row():
    with pytest.raises(SystemExit), job_run("demo", session_date=DAY):
        raise SystemExit(1)
    assert row("demo")["status"] == "failed"


def test_no_row_is_ever_left_running_after_the_block_exits():
    for raiser in (RuntimeError, KeyboardInterrupt, SystemExit):
        with pytest.raises(raiser), job_run(f"demo_{raiser.__name__}", session_date=DAY):
            raise raiser()
    stuck = db.fetch_all("SELECT job_name FROM job_runs WHERE status = 'running'")
    assert stuck == []


# ── concurrency ─────────────────────────────────────────────────────────────


def test_a_second_concurrent_run_is_refused_with_a_clear_error():
    """`with A, B, C:` nests exactly as three nested `with`s would: the outer
    run is still in flight when the inner one is attempted."""
    with (
        job_run("demo", session_date=DAY),
        pytest.raises(JobAlreadyRunning, match="already has a run in flight"),
        job_run("demo", session_date=DAY),
    ):
        pass


def test_the_error_names_the_recovery_command():
    with (
        job_run("demo", session_date=DAY),
        pytest.raises(JobAlreadyRunning) as excinfo,
        job_run("demo", session_date=DAY),
    ):
        pass
    assert "--clear-stale" in str(excinfo.value)


def test_a_retry_after_a_failure_is_allowed():
    with pytest.raises(RuntimeError), job_run("demo", session_date=DAY):
        raise RuntimeError("first attempt")
    with job_run("demo", session_date=DAY) as run:  # must not be blocked
        run.add_rows(1)
    assert row("demo")["status"] == "success"


def test_different_jobs_on_the_same_day_do_not_collide():
    with job_run("a", session_date=DAY), job_run("b", session_date=DAY):
        pass
    assert row("a")["status"] == "success"
    assert row("b")["status"] == "success"


# ── clear_stale_runs ────────────────────────────────────────────────────────


def test_clear_stale_runs_releases_a_row_abandoned_by_a_hard_kill():
    """A kill -9 never runs the `finally`, so this is the only recovery."""
    db.execute(
        "INSERT INTO job_runs (job_name, session_date, status, started_at) "
        "VALUES ('wedged', %s, 'running', now() - interval '9 hours')",
        (DAY,),
    )
    with pytest.raises(JobAlreadyRunning), job_run("wedged", session_date=DAY):
        pass

    assert clear_stale_runs(older_than_hours=6) == 1
    r = row("wedged")
    assert r["status"] == "failed"
    assert "abandoned" in r["error"]

    with job_run("wedged", session_date=DAY) as run:  # unwedged
        run.add_rows(1)


def test_clear_stale_runs_leaves_a_recent_run_alone():
    """A job that is genuinely still working must not be stolen from it."""
    with job_run("demo", session_date=DAY):
        assert clear_stale_runs(older_than_hours=6) == 0
        assert row("demo")["status"] == "running"


def test_clear_stale_runs_is_safe_to_call_when_nothing_is_stuck():
    assert clear_stale_runs() == 0


# ── the day-complete rule ───────────────────────────────────────────────────


def test_a_failed_critical_job_blocks_the_day_and_appears_in_the_veto_list():
    with job_run("snapshot", session_date=DAY, is_critical=True) as run:
        run.add_rows(100)
    assert day_is_complete(DAY) is True

    with pytest.raises(RuntimeError), job_run("estimates", session_date=DAY, is_critical=True):
        raise RuntimeError("token expired")

    assert day_is_complete(DAY) is False
    veto = failed_critical_jobs(DAY)
    assert [v["job_name"] for v in veto] == ["estimates"]
    assert "token expired" in veto[0]["error"]


def test_a_non_critical_failure_does_not_block_the_day():
    with job_run("snapshot", session_date=DAY, is_critical=True) as run:
        run.add_rows(1)
    with pytest.raises(RuntimeError), job_run("news", session_date=DAY, is_critical=False):
        raise RuntimeError("finnhub flaky")
    assert day_is_complete(DAY) is True
    assert failed_critical_jobs(DAY) == []


def test_a_skipped_critical_job_blocks_the_day():
    """A snapshot that was skipped is a snapshot we do not have."""
    with job_run("snapshot", session_date=DAY, is_critical=True) as run:
        run.skip("plan-gated")
    assert day_is_complete(DAY) is False


def test_last_run_returns_the_most_recent_attempt():
    with pytest.raises(RuntimeError), job_run("demo", session_date=DAY):
        raise RuntimeError("one")
    with job_run("demo", session_date=DAY) as run:
        run.add_rows(7)
    assert last_run("demo")["rows_written"] == 7
    assert last_run("nonexistent") is None


# ── a bookkeeping failure must not mask the real error ──────────────────────


def test_a_broken_finish_write_does_not_swallow_the_jobs_own_exception(monkeypatch):
    """The job's RuntimeError is what the operator needs to see, not a psycopg
    error from the audit trail."""
    import core.jobs as jobs_module

    def broken_execute(*a, **k):
        raise psycopg.OperationalError("database went away")

    with job_run("demo", session_date=DAY):
        monkeypatch.setattr(jobs_module.db, "execute", broken_execute)
        with (
            pytest.raises(RuntimeError, match="the real problem"),
            job_run("demo2", session_date=DAY),
        ):
            raise RuntimeError("the real problem")


# ── a critical snapshot job must never record success having done nothing ────
#
# Found live on 2026-10-20. With `stop_on=BudgetExhausted` added, a spent budget
# stopped the estimates loop cleanly — and the job then recorded
# `status=success, is_critical=true, rows_written=0, symbols_ok=0`, on the
# strength of which `v_day_status` certified `day_complete=true`. The day's
# estimate snapshot had not been taken by anyone.


def test_budget_stop_before_any_write_is_a_skip_not_a_success():
    from core.budget import BudgetExhausted
    from ingest import fmp_estimates

    class Spent:
        """Allows the plan probe, then has nothing left for the real work."""

        def __init__(self):
            self.calls = 0

        def analyst_estimates(self, *a, **k):
            self.calls += 1
            if self.calls == 1:
                return []  # the probe succeeds: not plan-gated
            raise BudgetExhausted("fmp daily call budget is exhausted")

    with pytest.raises(BudgetExhausted):
        fmp_estimates.run(Spent(), symbols=["AAPL", "MSFT", "NVDA"])

    r = row(fmp_estimates.JOB_NAME)
    assert r["status"] == "skipped", "a job that wrote nothing must not report success"
    assert r["is_critical"] is True
    assert r["rows_written"] == 0
    # And therefore the day is NOT complete on the strength of this row.
    assert not day_is_complete(clock.session_date())


def test_no_budget_even_to_probe_is_a_critical_skip():
    """ "Could not ask" stays a gate; only a confirmed 402 stops being one."""
    from core.budget import BudgetExhausted
    from ingest import fmp_estimates

    class Broke:
        def analyst_estimates(self, *a, **k):
            raise BudgetExhausted("fmp daily call budget is exhausted")

    with pytest.raises(BudgetExhausted):
        fmp_estimates.run(Broke(), symbols=["AAPL"])

    r = row(fmp_estimates.JOB_NAME)
    assert r["status"] == "skipped"
    assert r["is_critical"] is True, (
        "a spent budget means we never learned whether the plan covers this; "
        "defaulting that to 'not a gate' would let a broken job certify a day"
    )
    assert not day_is_complete(clock.session_date())


def test_a_confirmed_402_is_a_non_critical_skip_and_the_day_can_complete():
    """Contrast: a standing billing answer IS a decision already taken (D29)."""
    from ingest import fmp_estimates
    from ingest.fmp_client import FMPPlanError

    class Gated:
        def analyst_estimates(self, *a, **k):
            raise FMPPlanError("402 for /stable/analyst-estimates")

    with pytest.raises(fmp_estimates.EstimatesPlanBlocked):
        fmp_estimates.run(Gated(), symbols=["AAPL"])

    r = row(fmp_estimates.JOB_NAME)
    assert r["status"] == "skipped"
    assert r["is_critical"] is False


def test_clean_stop_does_not_mask_an_unrelated_crash():
    """A job that skips and then crashes for another reason is still FAILED.

    This is why `clean_stop` is an explicit allowlist rather than "honour
    skip() if it was called": a real bug must not inherit a skip's immunity.
    """
    from core.budget import BudgetExhausted

    with (
        pytest.raises(ZeroDivisionError),
        job_run("canary", session_date=DAY, clean_stop=BudgetExhausted) as run,
    ):
        run.skip("decided to stop")
        raise ZeroDivisionError("an unrelated bug")

    assert row("canary")["status"] == "failed"


def test_clean_stop_records_skipped_for_a_named_exception():
    from core.budget import BudgetExhausted

    with (
        pytest.raises(BudgetExhausted),
        job_run("canary", session_date=DAY, clean_stop=BudgetExhausted) as run,
    ):
        run.skip("budget gone")
        raise BudgetExhausted("spent")

    r = row("canary")
    assert r["status"] == "skipped"
    assert r["error"] == "budget gone"


def test_an_unnamed_exception_is_still_failed_without_clean_stop():
    """Regression: the default must be unchanged for every existing caller."""
    from core.budget import BudgetExhausted

    with pytest.raises(BudgetExhausted), job_run("canary", session_date=DAY) as run:
        run.skip("budget gone")
        raise BudgetExhausted("spent")

    assert row("canary")["status"] == "failed"


# ── a successful retry must RECOVER the day ──────────────────────────────────
#
# Found live 2026-10-21: a `daily_close` killed by a timeout left estimates
# abandoned and chains partial. The re-run succeeded at EVERY step and
# `day_complete` stayed false, because the view counted the superseded rows.
# Gate 4 starts the D7 clock on the first `day_complete = true`, so one bad
# first night would have stopped the clock starting while every step showed
# green and the operator was told the day had failed.


def _run(job_name: str, *, status: str, critical: bool) -> None:
    """Write one closed job_runs row with the given outcome."""
    if status == "success":
        with job_run(job_name, session_date=DAY, is_critical=critical):
            pass
        return
    if status == "skipped":
        with job_run(job_name, session_date=DAY, is_critical=critical) as run:
            run.skip("deliberate")
        return
    with (
        contextlib.suppress(RuntimeError),
        job_run(job_name, session_date=DAY, is_critical=critical),
    ):
        raise RuntimeError("deliberate failure")


def test_a_successful_retry_recovers_the_day():
    _run("snapshot", status="failed", critical=True)
    assert not day_is_complete(DAY)

    _run("snapshot", status="success", critical=True)
    assert day_is_complete(DAY), "the latest run succeeded; the day has its data"
    assert failed_critical_jobs(DAY) == []


def test_the_retry_history_is_not_hidden():
    """The day recovered, but it must not look like it went smoothly."""
    _run("snapshot", status="failed", critical=True)
    _run("snapshot", status="success", critical=True)

    row = db.fetch_one("SELECT * FROM v_day_status WHERE session_date = %s", (DAY,))
    assert row["day_complete"] is True
    assert row["jobs_run"] == 1, "one job, judged by its latest run"
    assert row["attempts"] == 2, "both attempts stay visible"
    assert row["jobs_failed"] == 0, "the failure is superseded, not current"
    # and the full audit trail is still in job_runs
    assert (
        db.fetch_one("SELECT count(*) AS n FROM job_runs WHERE session_date = %s", (DAY,))["n"] == 2
    )


def test_a_failure_AFTER_a_success_still_blocks():
    """Latest-run-wins cuts both ways; it is not 'any success anywhere'."""
    _run("snapshot", status="success", critical=True)
    assert day_is_complete(DAY)
    _run("snapshot", status="failed", critical=True)
    assert not day_is_complete(DAY)
    assert [r["job_name"] for r in failed_critical_jobs(DAY)] == ["snapshot"]


def test_a_partial_critical_job_still_fails_the_day():
    """D7/D22: an incomplete snapshot is a snapshot we do not have."""
    with job_run("snapshot", session_date=DAY, is_critical=True) as run:
        run.symbol_ok()
        run.symbol_failed()
    assert (
        db.fetch_one(
            "SELECT status FROM job_runs WHERE job_name='snapshot' ORDER BY id DESC LIMIT 1"
        )["status"]
        == "partial"
    )
    assert not day_is_complete(DAY)


def test_a_retried_job_appears_once_in_the_veto_list():
    """Two failed attempts are one blocked job, not two."""
    _run("snapshot", status="failed", critical=True)
    _run("snapshot", status="failed", critical=True)
    assert [r["job_name"] for r in failed_critical_jobs(DAY)] == ["snapshot"]


def test_a_non_critical_failure_never_blocks_the_day():
    _run("snapshot", status="success", critical=True)
    _run("extras", status="failed", critical=False)
    assert day_is_complete(DAY)
