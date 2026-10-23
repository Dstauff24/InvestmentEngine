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
    """What each channel actually DID. Four outcomes, never conflated.

    `sent` means delivered JUST NOW. A suppressed duplicate is not a send: on
    2026-10-22 the VM logged "suppressed duplicate telegram alert" and then
    "sent via email, telegram", because both transports returned True for a
    suppression and this class had nowhere else to put it. The evening's digest
    reached nobody and the summary line said otherwise.
    """

    sent: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = field(default=())
    suppressed: tuple[str, ...] = field(default=())
    """Already delivered earlier under the same dedupe key — NOT a send."""

    @property
    def any_sent(self) -> bool:
        """Whether anything was delivered NOW. Suppressions do not count."""
        return bool(self.sent)

    @property
    def reached_someone(self) -> bool:
        """Whether the reader has this alert, now or from an earlier delivery.

        The distinction `any_sent` cannot make: a wholly suppressed alert went
        nowhere this minute and yet nothing is wrong, because the same message
        already arrived.
        """
        return bool(self.sent or self.suppressed)

    def __str__(self) -> str:
        bits = []
        if self.sent:
            bits.append("sent via " + ", ".join(self.sent))
        if self.suppressed:
            bits.append("suppressed as duplicate: " + ", ".join(self.suppressed))
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
    suppressed: list[str] = []

    for channel in wanted:
        try:
            if channel == "email":
                if not _email_configured():
                    skipped.append("email")
                    continue
                outcome = mailer.send(
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
                outcome = telegram.send(
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

        # A SUPPRESSION IS NOT A SEND. Three outcomes, three buckets.
        if outcome == store.Outcome.DELIVERED:
            sent.append(channel)
        elif outcome == store.Outcome.SUPPRESSED:
            suppressed.append(channel)
        else:
            failed.append(channel)

    delivery = Delivery(tuple(sent), tuple(failed), tuple(skipped), tuple(suppressed))
    if not delivery.reached_someone:
        # Worth a loud line of its own: every channel failed or none exists, so
        # this alarm reached nobody and only the log records that it happened.
        log.error("ALERT REACHED NOBODY: %r — %s", subject, delivery)
    else:
        log.info("alert %r %s", subject, delivery)
    return delivery


def _email_configured() -> bool:
    """Whether email is USABLE, not merely mentioned.

    It used to test `SMTP_HOST and alert_email_onfail()` for presence, and
    `.env.example` SHIPS `SMTP_HOST=smtp.gmail.com` and
    `ALERT_EMAIL_ONFAIL=you@example.com` — so a machine that copied the example
    and never filled in SMTP reported email as a live channel, then failed on
    the first send with "SMTP_USER is still the .env.example placeholder". A
    channel whose credentials are placeholders is not configured; it is
    unconfigured in a way that looks configured.

    Asking the real accessors is what keeps this honest: each one refuses a
    placeholder (`settings.looks_like_placeholder`), so "no SettingsError"
    means every value email needs is actually filled in.
    """
    try:
        settings.smtp_host()
        settings.smtp_user()
        settings.smtp_password()
        settings.alert_email_from()
        settings.alert_email_onfail()
    except settings.SettingsError as exc:
        log.info("email channel unavailable: %s", exc)
        return False
    return True


def job_failure_key(job_name: str, day: date | None = None) -> str:
    return store.job_failure_key(job_name, day)
