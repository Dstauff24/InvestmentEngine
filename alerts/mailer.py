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
from email.message import EmailMessage

from alerts import store
from core import settings

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
    """Insert a pending alert row. Returns None if this alert already went out.

    Thin wrapper over `alerts.store`, which Telegram shares. The dedupe key is
    namespaced to the email channel so the same problem can also page over
    Telegram without one suppressing the other.
    """
    return store.record(
        subject, body, alert_type, severity, store.channel_key(dedupe_key, "email"), "email"
    )


def _mark(alert_id: int | None, status: str, error: str | None) -> None:
    store.mark(alert_id, status, error)


# Re-exported: callers have always imported it from here.
job_failure_key = store.job_failure_key
