"""Telegram transport for alerts (D16, amended 2026-10-21). Sending only.

Email is the digest channel; Telegram is the one that reaches a phone. The
engine runs unattended on a VM at 16:30 ET, and a failed SACRED snapshot job
(D7/D22) is worth a push notification rather than an email read the next
morning — the snapshot history cannot be re-fetched at any price, so the hours
between "the job failed" and "someone noticed" are the entire cost.

The same two properties as `alerts/mailer.py`, for the same reasons:

  * `send` NEVER raises. A Telegram outage must not turn a job that succeeded
    into one that failed, nor mask the original error in one that did fail.
  * Every send is recorded in `alerts` with a channel-namespaced `dedupe_key`,
    so the same failure on three retries pages once — and an email for the same
    problem is NOT suppressed by a Telegram row, nor the reverse.

Two things specific to this transport:

  * **The bot token is in the URL.** `https://api.telegram.org/bot<TOKEN>/...`
    means any exception, log line or traceback that quotes the URL leaks a
    credential that can send messages as the bot. Everything outbound goes
    through `redact` and the token never reaches a log.
  * **4096 characters is a hard API limit.** A digest or a traceback will
    exceed it, and Telegram rejects the whole message rather than truncating,
    so a long alert would silently never arrive. Messages are split on line
    boundaries where possible, each part labelled so an operator reading a
    phone knows whether they have the whole thing.
"""

from __future__ import annotations

import logging
import re
from datetime import date

import requests

from alerts import store
from core import netguard, settings

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
TIMEOUT_S = 15

# Telegram's documented ceiling. Headroom is left for the "(part 1/3)" label
# this module appends, so a split part can never itself be over the limit.
MAX_MESSAGE_CHARS = 4096
PART_LABEL_HEADROOM = 32
CHUNK_LIMIT = MAX_MESSAGE_CHARS - PART_LABEL_HEADROOM


def redact(text: str) -> str:
    """Strip bot tokens out of anything about to be logged or raised.

    A Telegram bot token is `<digits>:<35-ish base64-ish chars>`, and it appears
    inside the request URL, so the obvious `str(exc)` on a connection error can
    carry it into a log file. Both the `bot<token>` URL form and a bare token
    are covered — requests quotes the URL in several different shapes depending
    on which layer raised.
    """
    text = re.sub(r"(/bot)\d+:[A-Za-z0-9_-]+", r"\1<redacted>", text)
    return re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b", "<redacted>", text)


def split_message(body: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Split `body` into parts no longer than `limit`.

    Prefers line boundaries, because the things this sends — tracebacks, job
    reports, digests — are line-oriented and a mid-line split is much harder to
    read on a phone. Falls back to a hard character cut for a single line that
    is itself longer than the limit (a minified payload in an error message),
    since the alternative is not sending it.
    """
    if not body:
        return [""]
    if len(body) <= limit:
        return [body]

    parts: list[str] = []
    current = ""
    for line in body.splitlines(keepends=True):
        while len(line) > limit:
            # A single over-long line: emit what we have, then hard-cut.
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            parts.append(current)
            current = line
        else:
            current += line
    if current:
        parts.append(current)
    return parts


def send(
    subject: str,
    body: str,
    *,
    alert_type: str = "job_failure",
    severity: str = "critical",
    dedupe_key: str | None = None,
    record: bool = True,
    session: requests.Session | None = None,
) -> store.Outcome:
    """Send one Telegram message. NEVER raises.

    Returns DELIVERED, SUPPRESSED (a row with this key already exists) or
    FAILED — unreachable, unconfigured, or the consent gate refusing, all of
    which are reported and none of which propagate. **Not a bool**: True used
    to mean both "delivered" and "suppressed", so `notify` reported a
    suppression as a send.
    """
    subject = f"[{settings.engine_env()}] {subject}"

    alert_id = None
    if record:
        alert_id = store.record(
            subject,
            body,
            alert_type,
            severity,
            store.channel_key(dedupe_key, "telegram"),
            "telegram",
        )
        if alert_id is None:
            log.info("suppressed duplicate telegram alert: %s", dedupe_key)
            return store.Outcome.SUPPRESSED

    try:
        token = settings.telegram_bot_token()
        chat_id = settings.telegram_chat_id()
    except settings.SettingsError as exc:
        log.error("telegram is not configured: %s", redact(str(exc)))
        store.mark(alert_id, "failed", redact(str(exc)))
        return store.Outcome.FAILED

    # D27: consent before the transport, exactly as the data clients do. Under
    # pytest `require_live_calls` always refuses, flag or not.
    try:
        netguard.require_live_calls("Telegram", "sendMessage")
    except netguard.LiveCallBlocked as exc:
        log.error("telegram send blocked: %s", redact(str(exc)))
        store.mark(alert_id, "failed", redact(str(exc)))
        return store.Outcome.FAILED

    text = f"{subject}\n\n{body}" if body else subject
    parts = split_message(text)
    http = session or requests.Session()
    total = len(parts)

    for index, part in enumerate(parts, start=1):
        label = f"\n\n(part {index}/{total})" if total > 1 else ""
        try:
            response = http.post(
                f"{API_BASE}/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": part + label},
                timeout=TIMEOUT_S,
            )
        except requests.RequestException as exc:
            # Log loudly, never raise: the caller is usually already handling a
            # more important failure than "Telegram was unreachable".
            message = redact(f"{type(exc).__name__}: {exc}")
            log.error("could not send telegram alert %r: %s", subject, message)
            store.mark(alert_id, "failed", message)
            return store.Outcome.FAILED

        if not response.ok:
            message = redact(f"HTTP {response.status_code}: {response.text[:200]}")
            log.error("telegram rejected alert %r: %s", subject, message)
            store.mark(alert_id, "failed", message)
            return store.Outcome.FAILED

    log.info("sent telegram alert %r (%d part(s))", subject, total)
    store.mark(alert_id, "sent", None)
    return store.Outcome.DELIVERED


def job_failure_key(job_name: str, day: date | None = None) -> str:
    return store.job_failure_key(job_name, day)
