"""The one function the rest of the engine calls to raise an alarm (D16).

Jobs should not know or care which channels exist. `notify()` fans out to every
channel that is CONFIGURED, so adding Telegram to a VM is an `.env` change
rather than a code change, and a dev box with neither configured still runs.

Returns which channels accepted the alert rather than a bare bool, because
"the email went but Telegram did not" is a materially different situation from
"nothing went anywhere" and the caller — or a human reading a log — needs to be
able to tell.

NEVER RAISES, and that is load-bearing: almost every caller is already handling
a failure when it reaches here. An alerting path that can throw turns one
problem into two and loses the first one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from alerts import mailer, store, telegram
from core import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Delivery:
    """Which channels took the alert, and which were tried and failed."""

    sent: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = field(default=())

    @property
    def any_sent(self) -> bool:
        return bool(self.sent)

    def __str__(self) -> str:
        bits = []
        if self.sent:
            bits.append("sent via " + ", ".join(self.sent))
        if self.failed:
            bits.append("FAILED via " + ", ".join(self.failed))
        if self.skipped:
            bits.append("not configured: " + ", ".join(self.skipped))
        return "; ".join(bits) or "no channels configured"


def notify(
    subject: str,
    body: str,
    *,
    alert_type: str = "job_failure",
    severity: str = "critical",
    dedupe_key: str | None = None,
    channels: tuple[str, ...] | None = None,
) -> Delivery:
    """Raise an alarm on every configured channel. Never raises.

    `channels` overrides the automatic selection, for a test or for a caller
    that deliberately wants one channel (`scripts/telegram_test.py`).

    Each channel dedupes independently on a namespaced key, so one channel
    being down does not suppress the other for the same problem.
    """
    wanted = channels if channels is not None else ("email", "telegram")
    sent: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []

    for channel in wanted:
        try:
            if channel == "email":
                if not _email_configured():
                    skipped.append("email")
                    continue
                ok = mailer.send(
                    subject,
                    body,
                    alert_type=alert_type,
                    severity=severity,
                    dedupe_key=dedupe_key,
                )
            elif channel == "telegram":
                if not settings.telegram_configured():
                    skipped.append("telegram")
                    continue
                ok = telegram.send(
                    subject,
                    body,
                    alert_type=alert_type,
                    severity=severity,
                    dedupe_key=dedupe_key,
                )
            else:
                log.error("unknown alert channel %r — ignoring", channel)
                skipped.append(channel)
                continue
        except Exception as exc:
            # Both transports promise never to raise. This is the belt for that
            # braces: a bug in a transport must not escape the alerting path and
            # replace the failure the caller was already reporting.
            log.exception("alert channel %r raised, which it must not: %s", channel, exc)
            failed.append(channel)
            continue

        (sent if ok else failed).append(channel)

    delivery = Delivery(tuple(sent), tuple(failed), tuple(skipped))
    if not delivery.any_sent:
        # Worth a loud line of its own: every channel failed or none exists, so
        # this alarm reached nobody and only the log records that it happened.
        log.error("ALERT REACHED NOBODY: %r — %s", subject, delivery)
    else:
        log.info("alert %r %s", subject, delivery)
    return delivery


def _email_configured() -> bool:
    """SMTP host plus a recipient. Mirrors `settings.telegram_configured`."""
    try:
        return bool(settings.get("SMTP_HOST") and settings.alert_email_onfail())
    except settings.SettingsError:
        return False


def job_failure_key(job_name: str, day: date | None = None) -> str:
    return store.job_failure_key(job_name, day)
