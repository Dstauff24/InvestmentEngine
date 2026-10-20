"""Database access — one place that knows how to reach Postgres.

Thin on purpose. Jobs get a connection and write plain SQL; there is no ORM and
no query builder. Anything that runs unattended benefits from SQL you can paste
straight into psql when something looks wrong at 5 AM.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from core import settings


@contextmanager
def connect(*, dsn: str | None = None, autocommit: bool = False) -> Iterator[Connection]:
    """Open a connection to the engine database.

    Commits on clean exit, rolls back on exception, always closes. Rows come
    back as dicts because positional tuple indexing is how silent
    column-order bugs get shipped.
    """
    conn = psycopg.connect(dsn or settings.database_url(), row_factory=dict_row)
    conn.autocommit = autocommit
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except BaseException:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def fetch_all(sql: str, params: Sequence[Any] | dict[str, Any] | None = None) -> list[dict]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(sql: str, params: Sequence[Any] | dict[str, Any] | None = None) -> dict | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute(sql: str, params: Sequence[Any] | dict[str, Any] | None = None) -> int:
    """Run a single statement and return the affected row count."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def executemany(sql: str, rows: Sequence[Sequence[Any] | dict[str, Any]]) -> int:
    """Batch-apply one statement. Used by every ingestion upsert.

    Returns the number of rows SENT, not changed: with `ON CONFLICT DO UPDATE`
    Postgres reports those identically anyway, and the send count is what
    `job_runs.rows_written` is meant to record.
    """
    if not rows:
        return 0
    with connect() as conn, conn.cursor() as cur:
        cur.executemany(sql, rows)
        return len(rows)


def active_symbols() -> list[str]:
    """The current universe (D14), as every ingestion job should iterate it."""
    return [r["symbol"] for r in fetch_all("SELECT symbol FROM v_active_universe")]


def ping() -> bool:
    """Cheap connectivity check for health checks and orchestrator preflight."""
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            return cur.fetchone() == {"ok": 1}
    except psycopg.Error:
        return False
