"""The `job_runs` writer. Every job in this repo uses it — no exceptions.

CLAUDE.md: "Every job writes a row to `job_runs` (start, end, status,
rows_written, error). The orchestrator refuses to mark a day complete if a
snapshot job failed."

Used as a context manager so the finish row is written by `finally`, not by the
happy path. A job that crashes must still leave a `failed` row behind: a
snapshot job that dies silently and leaves no trace is exactly the failure D7
says must never happen.

    with job_run("fmp_estimates", is_critical=True) as run:
        run.add_rows(len(rows))

`is_critical=True` marks the sacred snapshot jobs (D7). The orchestrator reads
that flag out of `v_day_status` rather than carrying its own list of job names,
so adding a critical job cannot leave the day-complete rule out of date.
"""

from __future__ import annotations

import os
import socket
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any

import psycopg

from core import clock, db


class JobAlreadyRunning(RuntimeError):
    """Another run of this job for this session date is still in flight.

    Raised via the partial unique index `uq_job_runs_inflight`. Usually an
    earlier cron fire still going, or a previous run that died hard enough to
    leave its row stuck in 'running' — see `clear_stale_runs`.
    """


class JobRun:
    """Handle for an in-flight job. Counters are written when the block exits."""

    def __init__(self, run_id: int, job_name: str, session_date: date) -> None:
        self.run_id = run_id
        self.job_name = job_name
        self.session_date = session_date
        self.rows_written = 0
        self.rows_failed = 0
        self.symbols_ok = 0
        self.symbols_failed = 0
        self.details: dict[str, Any] = {}
        self._skipped_reason: str | None = None

    def add_rows(self, n: int) -> None:
        self.rows_written += n

    def add_failure(self, n: int = 1) -> None:
        self.rows_failed += n

    def symbol_ok(self, n: int = 1) -> None:
        self.symbols_ok += n

    def symbol_failed(self, n: int = 1) -> None:
        self.symbols_failed += n

    def note(self, **fields: Any) -> None:
        """Attach arbitrary context to `job_runs.details`."""
        self.details.update(fields)

    def skip(self, reason: str) -> None:
        """Mark this run 'skipped' rather than 'success'. Not an error.

        A skip does NOT satisfy the day-complete check for a critical job: a
        snapshot that was skipped is a snapshot we do not have.
        """
        self._skipped_reason = reason

    @property
    def status(self) -> str:
        if self._skipped_reason is not None:
            return "skipped"
        if self.rows_failed or self.symbols_failed:
            return "partial"
        return "success"


@contextmanager
def job_run(
    job_name: str,
    *,
    session_date: date | None = None,
    is_critical: bool = False,
    triggered_by: str = "manual",
) -> Iterator[JobRun]:
    """Open a `job_runs` row, yield a handle, and ALWAYS close the row."""
    day = session_date or clock.session_date()
    run_id = _start(job_name, day, is_critical=is_critical, triggered_by=triggered_by)
    run = JobRun(run_id, job_name, day)
    try:
        yield run
    except BaseException as exc:
        # BaseException, not Exception: a KeyboardInterrupt or a SystemExit must
        # still close the row. An unattended job that leaves 'running' behind
        # wedges every later run of itself via uq_job_runs_inflight.
        _finish(
            run,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            details={**run.details, "traceback": traceback.format_exc()[-4000:]},
        )
        raise
    else:
        _finish(run, status=run.status, error=run._skipped_reason, details=run.details)


def _start(job_name: str, day: date, *, is_critical: bool, triggered_by: str) -> int:
    sql = """
        INSERT INTO job_runs (job_name, session_date, status, is_critical,
                              hostname, pid, triggered_by)
        VALUES (%s, %s, 'running', %s, %s, %s, %s)
        RETURNING id
    """
    params = (job_name, day, is_critical, socket.gethostname(), os.getpid(), triggered_by)
    try:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()["id"]
    except psycopg.errors.UniqueViolation as exc:
        raise JobAlreadyRunning(
            f"{job_name} already has a run in flight for {day}. If a previous run "
            f"died, clear it with: python -m core.jobs --clear-stale"
        ) from exc


def _finish(run: JobRun, *, status: str, error: str | None, details: dict) -> None:
    sql = """
        UPDATE job_runs
        SET    finished_at    = now(),
               duration_ms    = EXTRACT(EPOCH FROM (now() - started_at)) * 1000,
               status         = %s,
               rows_written   = %s,
               rows_failed    = %s,
               symbols_ok     = %s,
               symbols_failed = %s,
               error          = %s,
               details        = %s
        WHERE  id = %s
    """
    params = (
        status,
        run.rows_written,
        run.rows_failed,
        run.symbols_ok,
        run.symbols_failed,
        error,
        psycopg.types.json.Jsonb(details) if details else None,
        run.run_id,
    )
    try:
        db.execute(sql, params)
    except psycopg.Error:
        # Never let bookkeeping mask the real failure the job is already raising.
        traceback.print_exc()


# ── queries the orchestrator and health checks use ──────────────────────────


def day_is_complete(day: date) -> bool:
    """True when every critical job for `day` succeeded, and at least one ran.

    Reads `v_day_status`, so "critical" is defined by the data rather than by a
    job-name list that drifts out of date.
    """
    row = db.fetch_one("SELECT day_complete FROM v_day_status WHERE session_date = %s", (day,))
    return bool(row and row["day_complete"])


def failed_critical_jobs(day: date) -> list[dict]:
    """Critical jobs for `day` that did not succeed. The orchestrator's veto list."""
    return db.fetch_all(
        """
        SELECT job_name, status, error, rows_written
        FROM   job_runs
        WHERE  session_date = %s AND is_critical AND status <> 'success'
        ORDER  BY job_name
        """,
        (day,),
    )


def last_run(job_name: str) -> dict | None:
    return db.fetch_one(
        "SELECT * FROM job_runs WHERE job_name = %s ORDER BY started_at DESC LIMIT 1",
        (job_name,),
    )


def clear_stale_runs(older_than_hours: int = 6) -> int:
    """Mark long-abandoned 'running' rows as failed so the job can start again.

    A hard kill (OOM, `kill -9`, VM reboot) leaves a row in 'running' forever,
    and the partial unique index then blocks every future run of that job. The
    `finally` close in `job_run` handles every in-process exit; this handles the
    ones where the process never got to run code at all.
    """
    return db.execute(
        """
        UPDATE job_runs
        SET    status      = 'failed',
               finished_at = now(),
               error       = 'abandoned: no completion recorded'
        WHERE  status = 'running'
          AND  started_at < now() - make_interval(hours => %s)
        """,
        (older_than_hours,),
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="job_runs maintenance")
    parser.add_argument("--clear-stale", action="store_true", help="release abandoned runs")
    parser.add_argument("--hours", type=int, default=6, help="staleness threshold")
    parser.add_argument("--status", action="store_true", help="show today's job status")
    args = parser.parse_args(argv)

    if args.clear_stale:
        n = clear_stale_runs(args.hours)
        print(f"released {n} abandoned run(s) older than {args.hours}h")
        return 0

    day = clock.session_date()
    rows = db.fetch_all(
        "SELECT job_name, status, rows_written, error FROM job_runs "
        "WHERE session_date = %s ORDER BY started_at",
        (day,),
    )
    print(f"session {day}: {'complete' if day_is_complete(day) else 'NOT complete'}")
    for row in rows:
        error = f"  {row['error']}" if row["error"] else ""
        print(f"  {row['status']:<8} {row['job_name']:<26} {row['rows_written']:>7} rows{error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
