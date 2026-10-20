"""SMTP transport for alerts (D16). Sending only — no formatting, no scoring.

CLAUDE.md: the sacred snapshot jobs "must never silently fail. Log + email on
failure." This is the email half. The daily idea digest (D16) will grow its own
module; this stays the plumbing both use.

Two properties that matter for something running unattended:

  * `send` NEVER raises. A mail server outage must not turn a job that succeeded
    into a job that failed, nor mask the original error in one that did fail.
    It returns False and records the failure.
  * Every send is recorded in `alerts` with a `dedupe_key`, so the same failure
    firing on three consecutive retries produces one email, not three.
"""

from __future__ import annotations

import logging
import smtplib
from datetime import date
from email.message import EmailMessage

import psycopg

from core import clock, db, settings

log = logging.getLogger(__name__)

SMTP_TIMEOUT_S = 30


def send(
    subject: str,
    body: str,
    *,
    to: str | None = None,
    alert_type: str = "job_failure",
    severity: str = "critical",
    dedupe_key: str | None = None,
    record: bool = True,
) -> bool:
    """Send one plain-text email. Returns True on success; never raises.

    `dedupe_key` makes the send idempotent: if a row with that key already
    exists in `alerts`, nothing is sent. Callers build one that is stable for
    "the same problem on the same day".
    """
    recipient = to or settings.alert_email_onfail()
    subject = f"[{settings.engine_env()}] {subject}"

    alert_id = None
    if record:
        alert_id = _record(subject, body, alert_type, severity, dedupe_key)
        if alert_id is None:
            log.info("suppressed duplicate alert: %s", dedupe_key)
            return True

    try:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = settings.alert_email_from()
        message["To"] = recipient
        message.set_content(body)

        with smtplib.SMTP(settings.smtp_host(), settings.smtp_port(), timeout=SMTP_TIMEOUT_S) as s:
            s.starttls()
            s.login(settings.smtp_user(), settings.smtp_password())
            s.send_message(message)
    except (TimeoutError, smtplib.SMTPException, OSError, settings.SettingsError) as exc:
        # Log loudly, but do NOT raise: the caller is usually already handling a
        # more important failure than "the mail server was down".
        log.error("could not send alert %r: %s", subject, exc)
        _mark(alert_id, "failed", str(exc))
        return False

    log.info("sent alert %r to %s", subject, recipient)
    _mark(alert_id, "sent", None)
    return True


def _record(
    subject: str, body: str, alert_type: str, severity: str, dedupe_key: str | None
) -> int | None:
    """Insert a pending alert row. Returns None if this alert already went out."""
    try:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO alerts (alert_type, severity, subject, body, channel,
                                    status, dedupe_key)
                VALUES (%s, %s, %s, %s, 'email', 'pending', %s)
                ON CONFLICT (dedupe_key) DO NOTHING
                RETURNING id
                """,
                (alert_type, severity, subject, body, dedupe_key),
            )
            row = cur.fetchone()
            return row["id"] if row else None
    except psycopg.Error as exc:
        # A database that is down is exactly when we most want the email to go
        # out, so fall through and send it unrecorded.
        log.error("could not record alert (sending anyway): %s", exc)
        return -1


def _mark(alert_id: int | None, status: str, error: str | None) -> None:
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
