"""The Telegram alert channel (D16, amended 2026-10-21). No network, no DB.

The four properties worth testing are the ones that only matter when something
else has already gone wrong:

  * `send` NEVER raises — it is called from inside failure handlers.
  * the bot token never reaches a log, a message, or an exception string.
  * a message over Telegram's 4096-character limit is SPLIT, not rejected.
  * D27 consent is enforced, and inside pytest it refuses regardless.
"""

from __future__ import annotations

import logging

import pytest
import requests

from alerts import notify, store, telegram
from core import netguard, settings

TOKEN = "123456789:AAFakeTokenWithEnoughCharactersToLookReal_xyz"


class FakeResponse:
    def __init__(self, status_code=200, text='{"ok":true}'):
        self.status_code = status_code
        self.text = text
        self.ok = 200 <= status_code < 400


class FakeSession:
    """Records posts; never touches a socket."""

    def __init__(self, *responses, raises=None):
        self.responses = list(responses) or [FakeResponse()]
        self.raises = raises
        self.posts: list[tuple[str, dict]] = []

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json or {}))
        if self.raises is not None:
            raise self.raises
        return self.responses[min(len(self.posts) - 1, len(self.responses) - 1)]


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    """A configured bot, consent granted, and no database."""
    monkeypatch.setattr(settings, "telegram_bot_token", lambda: TOKEN)
    monkeypatch.setattr(settings, "telegram_chat_id", lambda: "-100123")
    monkeypatch.setattr(settings, "telegram_configured", lambda: True)
    monkeypatch.setattr(settings, "engine_env", lambda: "test")
    # The `alerts` table is covered by the integration suite; here the store is
    # stubbed so these stay unit tests.
    monkeypatch.setattr(store, "record", lambda *a, **k: 1)
    monkeypatch.setattr(store, "mark", lambda *a, **k: None)
    monkeypatch.setattr(netguard, "require_live_calls", lambda *a, **k: None)


# ── the consent gate (D27) ──────────────────────────────────────────────────


def test_it_refuses_without_consent(monkeypatch, caplog):
    """D27: alerting is still an outbound call and still needs consent."""
    monkeypatch.undo()
    monkeypatch.setattr(settings, "telegram_bot_token", lambda: TOKEN)
    monkeypatch.setattr(settings, "telegram_chat_id", lambda: "-100123")
    monkeypatch.setattr(settings, "engine_env", lambda: "test")
    monkeypatch.setattr(store, "record", lambda *a, **k: 1)
    monkeypatch.setattr(store, "mark", lambda *a, **k: None)

    session = FakeSession()
    with caplog.at_level(logging.ERROR):
        assert telegram.send("subject", "body", session=session) is store.Outcome.FAILED
    assert session.posts == [], "the gate must fire BEFORE the transport"


def test_inside_pytest_the_gate_refuses_even_with_the_flag(monkeypatch):
    """The flag cannot buy a live call from the test suite."""
    monkeypatch.setenv("ALLOW_LIVE_CALLS", "1")
    assert netguard.live_calls_allowed() is False


# ── never raises ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "boom",
    [
        requests.ConnectionError("connection refused"),
        requests.Timeout("timed out"),
        requests.RequestException("something else"),
    ],
)
def test_a_dead_telegram_returns_false_and_never_raises(boom, caplog):
    session = FakeSession(raises=boom)
    with caplog.at_level(logging.ERROR):
        assert telegram.send("subject", "body", session=session) is store.Outcome.FAILED


def test_an_http_error_returns_false_and_never_raises(caplog):
    session = FakeSession(FakeResponse(403, '{"ok":false,"description":"bot blocked"}'))
    with caplog.at_level(logging.ERROR):
        assert telegram.send("subject", "body", session=session) is store.Outcome.FAILED


def test_an_unconfigured_bot_returns_false_and_never_raises(monkeypatch, caplog):
    monkeypatch.setattr(
        settings, "telegram_bot_token", lambda: (_ for _ in ()).throw(settings.SettingsError("x"))
    )
    with caplog.at_level(logging.ERROR):
        assert telegram.send("subject", "body", session=FakeSession()) is store.Outcome.FAILED


# ── token redaction ─────────────────────────────────────────────────────────


def test_the_token_is_redacted_from_a_transport_error(caplog):
    """The token lives in the URL, so `str(exc)` can carry it into a log file."""
    session = FakeSession(
        raises=requests.ConnectionError(f"failed to reach /bot{TOKEN}/sendMessage")
    )
    with caplog.at_level(logging.ERROR):
        telegram.send("subject", "body", session=session)
    logged = caplog.text
    assert TOKEN not in logged
    assert "<redacted>" in logged


def test_the_token_is_redacted_from_an_http_error_body(caplog):
    session = FakeSession(FakeResponse(401, f"Unauthorized for {TOKEN}"))
    with caplog.at_level(logging.ERROR):
        telegram.send("subject", "body", session=session)
    assert TOKEN not in caplog.text


@pytest.mark.parametrize(
    "text",
    [
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        f"token={TOKEN}",
        f"  {TOKEN}  ",
    ],
)
def test_redact_catches_the_token_in_several_shapes(text):
    assert TOKEN not in telegram.redact(text)


def test_redact_leaves_ordinary_text_alone():
    assert telegram.redact("chains: 431 ok, 2 failed") == "chains: 431 ok, 2 failed"


# ── 4096-character chunking ─────────────────────────────────────────────────


def test_a_short_message_is_one_part():
    session = FakeSession()
    assert telegram.send("subject", "body", session=session) is store.Outcome.DELIVERED
    assert len(session.posts) == 1
    assert "(part" not in session.posts[0][1]["text"]


def test_an_over_long_message_is_split_and_every_part_fits():
    body = "\n".join(f"line {i:05d}: " + "y" * 70 for i in range(400))
    session = FakeSession()
    assert telegram.send("subject", body, session=session) is store.Outcome.DELIVERED
    assert len(session.posts) > 1
    for _, payload in session.posts:
        assert len(payload["text"]) <= telegram.MAX_MESSAGE_CHARS, (
            "Telegram REJECTS an over-limit message rather than truncating, so a "
            "part that is itself too long means the alert never arrives"
        )


def test_the_parts_are_labelled_so_a_reader_knows_it_is_partial():
    body = "\n".join("z" * 200 for _ in range(60))
    session = FakeSession()
    telegram.send("subject", body, session=session)
    total = len(session.posts)
    assert total > 1
    assert f"(part 1/{total})" in session.posts[0][1]["text"]
    assert f"(part {total}/{total})" in session.posts[-1][1]["text"]


def test_splitting_prefers_line_boundaries():
    """A mid-line break reads badly on a phone; these are line-oriented alerts."""
    lines = [f"line {i:04d}" for i in range(2000)]
    parts = telegram.split_message("\n".join(lines), limit=500)
    assert len(parts) > 1
    for part in parts[:-1]:
        assert part.endswith("\n"), "a part should end at a line boundary"


def test_a_single_over_long_line_is_hard_cut_rather_than_dropped():
    """A minified payload inside an error message has no line to break on."""
    parts = telegram.split_message("x" * 2500, limit=500)
    assert len(parts) == 5
    assert all(len(p) <= 500 for p in parts)
    assert "".join(parts) == "x" * 2500


def test_an_empty_body_still_sends_the_subject():
    session = FakeSession()
    assert telegram.send("just the subject", "", session=session) is store.Outcome.DELIVERED
    assert "just the subject" in session.posts[0][1]["text"]


# ── dedupe ──────────────────────────────────────────────────────────────────


def test_a_duplicate_is_suppressed_and_says_so_rather_than_claiming_a_send(monkeypatch):
    """IT USED TO RETURN TRUE. That is how the VM logged "suppressed duplicate
    telegram alert" and then "sent via email, telegram" on an evening when the
    digest reached nobody."""
    monkeypatch.setattr(store, "record", lambda *a, **k: None)  # key already present
    session = FakeSession()
    outcome = telegram.send("subject", "body", dedupe_key="k", session=session)
    assert outcome is store.Outcome.SUPPRESSED
    assert outcome is not store.Outcome.DELIVERED
    assert session.posts == [], "a suppressed alert must not reach the API"


def test_the_dedupe_key_is_namespaced_per_channel():
    """One shared row would let the first channel suppress the second."""
    assert store.channel_key("job_failure:x:2026-10-21", "telegram") != store.channel_key(
        "job_failure:x:2026-10-21", "email"
    )
    assert store.channel_key(None, "telegram") is None


# ── the notify facade ───────────────────────────────────────────────────────


def test_notify_reports_which_channels_took_it(monkeypatch):
    monkeypatch.setattr(notify, "_email_configured", lambda: True)
    monkeypatch.setattr(notify.mailer, "send", lambda *a, **k: store.Outcome.DELIVERED)
    monkeypatch.setattr(notify.telegram, "send", lambda *a, **k: store.Outcome.FAILED)

    delivery = notify.notify("subject", "body")
    assert delivery.sent == ("email",)
    assert delivery.failed == ("telegram",)
    assert delivery.any_sent is True


def test_notify_reports_a_suppressed_channel_as_suppressed_not_sent(monkeypatch):
    """THE 2026-10-22 BUG, at the facade. A suppression must never appear in
    `sent`, and the summary line must not say "sent via" a channel that sent
    nothing."""
    monkeypatch.setattr(notify, "_email_configured", lambda: True)
    monkeypatch.setattr(notify.mailer, "send", lambda *a, **k: store.Outcome.SUPPRESSED)
    monkeypatch.setattr(notify.telegram, "send", lambda *a, **k: store.Outcome.SUPPRESSED)

    delivery = notify.notify("subject", "body", dedupe_key="daily_digest:2026-10-22")

    assert delivery.sent == ()
    assert delivery.suppressed == ("email", "telegram")
    assert delivery.failed == ()
    assert delivery.any_sent is False, "nothing was delivered now"
    assert delivery.reached_someone is True, "but the reader already has it"
    assert "sent via" not in str(delivery)
    assert "suppressed as duplicate: email, telegram" in str(delivery)


def test_a_mixed_delivery_separates_all_four_outcomes(monkeypatch):
    monkeypatch.setattr(notify, "_email_configured", lambda: True)
    monkeypatch.setattr(notify.mailer, "send", lambda *a, **k: store.Outcome.SUPPRESSED)
    monkeypatch.setattr(notify.telegram, "send", lambda *a, **k: store.Outcome.DELIVERED)

    delivery = notify.notify("subject", "body")
    assert delivery.sent == ("telegram",)
    assert delivery.suppressed == ("email",)
    summary = str(delivery)
    assert "sent via telegram" in summary
    assert "suppressed as duplicate: email" in summary


def test_an_alert_that_is_wholly_suppressed_is_not_logged_as_reaching_nobody(monkeypatch, caplog):
    """ "REACHED NOBODY" is for a real outage. A duplicate reached them the
    first time, and crying wolf here trains the reader to ignore the line."""
    monkeypatch.setattr(notify, "_email_configured", lambda: True)
    monkeypatch.setattr(notify.mailer, "send", lambda *a, **k: store.Outcome.SUPPRESSED)
    monkeypatch.setattr(settings, "telegram_configured", lambda: False)

    with caplog.at_level("ERROR"):
        notify.notify("subject", "body")
    assert "REACHED NOBODY" not in caplog.text


def test_an_alert_that_only_failed_is_logged_as_reaching_nobody(monkeypatch, caplog):
    monkeypatch.setattr(notify, "_email_configured", lambda: True)
    monkeypatch.setattr(notify.mailer, "send", lambda *a, **k: store.Outcome.FAILED)
    monkeypatch.setattr(settings, "telegram_configured", lambda: False)

    with caplog.at_level("ERROR"):
        notify.notify("subject", "body")
    assert "ALERT REACHED NOBODY" in caplog.text


def test_notify_skips_a_channel_that_is_not_configured(monkeypatch):
    monkeypatch.setattr(notify, "_email_configured", lambda: False)
    monkeypatch.setattr(settings, "telegram_configured", lambda: False)

    delivery = notify.notify("subject", "body")
    assert delivery.skipped == ("email", "telegram")
    assert delivery.any_sent is False


def test_notify_never_raises_even_if_a_transport_does(monkeypatch, caplog):
    """Belt for the transports' braces: a bug there must not escape here."""
    monkeypatch.setattr(notify, "_email_configured", lambda: True)
    monkeypatch.setattr(settings, "telegram_configured", lambda: True)

    def boom(*a, **k):
        raise RuntimeError("a transport bug")

    monkeypatch.setattr(notify.mailer, "send", boom)
    monkeypatch.setattr(notify.telegram, "send", boom)

    with caplog.at_level(logging.ERROR):
        delivery = notify.notify("subject", "body")
    assert delivery.failed == ("email", "telegram")
    assert "REACHED NOBODY" in caplog.text


def test_notify_logs_loudly_when_an_alarm_reaches_nobody(monkeypatch, caplog):
    monkeypatch.setattr(notify, "_email_configured", lambda: True)
    monkeypatch.setattr(settings, "telegram_configured", lambda: True)
    monkeypatch.setattr(notify.mailer, "send", lambda *a, **k: False)
    monkeypatch.setattr(notify.telegram, "send", lambda *a, **k: False)

    with caplog.at_level(logging.ERROR):
        assert notify.notify("subject", "body").any_sent is False
    assert "ALERT REACHED NOBODY" in caplog.text


def test_email_is_not_configured_when_its_credentials_are_placeholders(monkeypatch):
    """`.env.example` SHIPS `SMTP_HOST=smtp.gmail.com` and
    `ALERT_EMAIL_ONFAIL=you@example.com`, so a presence check called email a
    live channel on a machine that never filled SMTP in — and the first send
    then failed with "SMTP_USER is still the .env.example placeholder".

    A channel whose credentials are placeholders is not configured; it is
    unconfigured in a way that LOOKS configured, which is why the VM's digest
    could claim an email path that was never going to work.
    """
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_USER", "CHANGE_ME")
    monkeypatch.setenv("SMTP_PASSWORD", "CHANGE_ME")
    monkeypatch.setenv("ALERT_EMAIL_FROM", "engine@example.com")
    monkeypatch.setenv("ALERT_EMAIL_ONFAIL", "you@example.com")

    assert notify._email_configured() is False


def test_email_is_configured_once_the_values_are_real(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.fastmail.com")
    monkeypatch.setenv("SMTP_USER", "engine@real.example")
    monkeypatch.setenv("SMTP_PASSWORD", "a-real-app-password")
    monkeypatch.setenv("ALERT_EMAIL_FROM", "engine@real.example")
    monkeypatch.setenv("ALERT_EMAIL_ONFAIL", "me@real.example")

    assert notify._email_configured() is True


def test_an_unconfigured_email_channel_is_skipped_not_reported_as_sent(monkeypatch):
    """The whole point: a channel that cannot work must appear under `skipped`,
    where a reader can see it, and never under `sent`."""
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_USER", "CHANGE_ME")
    monkeypatch.setenv("SMTP_PASSWORD", "CHANGE_ME")
    monkeypatch.setattr(settings, "telegram_configured", lambda: False)

    delivery = notify.notify("subject", "body")
    assert delivery.skipped == ("email", "telegram")
    assert delivery.sent == ()
    assert "not configured: email, telegram" in str(delivery)


def test_the_placeholder_rule_has_one_definition(monkeypatch):
    """`settings.require`, the deploy check and the alerting layer must agree
    about what "filled in" means, or a value passes one gate and fails another."""
    from scripts import env_contract

    assert env_contract.PLACEHOLDERS is settings.PLACEHOLDERS
    assert settings.looks_like_placeholder("CHANGE_ME_NOW") is True
    assert settings.looks_like_placeholder("you@example.com") is True
    assert settings.looks_like_placeholder("engine@real.example") is False
    assert settings.looks_like_placeholder("") is False
    assert settings.looks_like_placeholder(None) is False
