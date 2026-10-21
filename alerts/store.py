"""The `alerts` table, shared by every channel (D16).

Extracted from `alerts/mailer.py` when Telegram arrived on 2026-10-21. Both
channels need identical dedupe and status bookkeeping, and two copies of it
would drift — the copy that matters most is the one nobody looks at until an
alert failed to arrive.

ONE ROW PER CHANNEL, each with its own `dedupe_key`. `dedupe_key` is unique, so
a single shared row would mean the first channel to record suppressed the
second. Worse, a Telegram outage would then suppress the email for the same
problem. Channels must fail independently.
"""

from __future__ import annotations

import logging
from datetime import date

import psycopg

from core import clock, db

log = logging.getLogger(__name__)

# `_record` returns this when the database itself is unreachable: the alert is
# sent UNRECORDED rather than dropped, because a database that is down is
# exactly when the alert matters most.
UNRECORDED = -1


def channel_key(dedupe_key: str | None, channel: str) -> str | None:
    """Namespace a dedupe key to one channel."""
    return None if dedupe_key is None else f"{dedupe_key}:{channel}"


def record(
    subject: str,
    body: str,
    alert_type: str,
    severity: str,
    dedupe_key: str | None,
    channel: str,
) -> int | None:
    """Insert a pending alert row. None if this alert already went out."""
    try:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO alerts (alert_type, severity, subject, body, channel,
                                    status, dedupe_key)
                VALUES (%s, %s, %s, %s, %s, 'pending', %s)
                ON CONFLICT (dedupe_key) DO NOTHING
                RETURNING id
                """,
                (alert_type, severity, subject, body, channel, dedupe_key),
            )
            row = cur.fetchone()
            return row["id"] if row else None
    except psycopg.Error as exc:
        log.error("could not record %s alert (sending anyway): %s", channel, exc)
        return UNRECORDED


def mark(alert_id: int | None, status: str, error: str | None) -> None:
    """Close out an alert row. Never raises."""
    if not alert_id or alert_id < 0:
        return
    try:
        db.execute(
            "UPDATE alerts SET status = %s, sent_at = now(), error = %s WHERE id = %s",
            (status, error, alert_id),
        )
    except psycopg.Error as exc:
        log.error("could not update alert %s: %s", alert_id, exc)


def job_failure_key(job_name: str, day: date | None = None) -> str:
    """A dedupe key stable for one job on one trading day."""
    return f"job_failure:{job_name}:{day or clock.session_date()}"
