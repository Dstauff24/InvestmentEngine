"""The job_runs writer, against a real database.

Excluded from the default run; see tests/dbguard.py for the wipe guard.

    INTEGRATION_DB_OK=1 pytest -m integration

What is here is the behaviour a unit test cannot establish: that the row really
closes from `finally` on every kind of exit, that the partial unique index really
blocks a concurrent run, and that `clear_stale_runs` really unwedges a job whose
process was killed before it could run any code.
"""

from __future__ import annotations

from datetime import date

import psycopg
import pytest

from core import db
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
