"""The D27 consent gate. No network — that is the point.

Background: an earlier guard refused to build a client when an API key still
read `CHANGE_ME`. Real keys were present in the process environment, so nothing
looked like a placeholder, and running the orchestrator merely to check its exit
codes spent live Finnhub quota fetching 401 real articles. Consent has to be its
own signal, independent of whether a usable credential happens to be around.
"""

from __future__ import annotations

from datetime import date

import pytest

from core import netguard
from core.netguard import FLAG, PYTEST_MARKER, LiveCallBlocked
from core.ratelimit import NullRateLimiter

# ── the flag ────────────────────────────────────────────────────────────────
# `env` is injected so the flag logic can be exercised from inside pytest, where
# the real environment always answers no.


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " 1 "])
def test_truthy_values_grant_consent(value):
    assert netguard.live_calls_allowed({FLAG: value}) is True


@pytest.mark.parametrize("value", ["0", "", "false", "no", "off", "maybe", "2"])
def test_everything_else_withholds_it(value):
    assert netguard.live_calls_allowed({FLAG: value}) is False


def test_an_absent_flag_withholds_consent():
    """The default must be refusal, not permission."""
    assert netguard.live_calls_allowed({}) is False


def test_a_working_api_key_is_not_consent():
    """The exact failure this replaces: real credentials, unintended calls."""
    env = {"FMP_API_KEY": "a-real-working-key", "FINNHUB_API_KEY": "also-real"}
    assert netguard.live_calls_allowed(env) is False


# ── pytest is an absolute block ─────────────────────────────────────────────


def test_the_flag_cannot_re_enable_calls_inside_the_test_suite():
    """CLAUDE.md: no network in tests. Enforced, not trusted.

    A stray export in a shell profile or a CI runner must not be able to turn
    the suite into something that talks to the internet.
    """
    assert netguard.live_calls_allowed({FLAG: "1", PYTEST_MARKER: "some_test"}) is False


def test_the_real_environment_refuses_while_this_test_runs():
    assert netguard.live_calls_allowed() is False


def test_the_in_test_message_says_the_flag_will_not_help():
    with pytest.raises(LiveCallBlocked, match="cannot be overridden"):
        netguard.require_live_calls("FMP", "/stable/quote")


def test_the_error_names_the_provider_and_the_endpoint():
    with pytest.raises(LiveCallBlocked) as excinfo:
        netguard.require_live_calls("Finnhub", "/company-news")
    assert "Finnhub" in str(excinfo.value)
    assert "/company-news" in str(excinfo.value)


# ── the client is gated ─────────────────────────────────────────────────────


def fmp_client():
    from ingest.fmp_client import FMPClient

    return FMPClient(api_key="k", base_url="https://x", limiter=NullRateLimiter())


def test_fmp_refuses():
    with pytest.raises(LiveCallBlocked, match="FMP"):
        fmp_client().quotes(["AAPL"])


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.quotes(["AAPL"]),
        lambda c: c.sp500_constituents(),
        lambda c: c.nasdaq100_constituents(),
        lambda c: c.income_statement("AAPL", period="quarter", limit=1),
        lambda c: c.ratios("AAPL", period="quarter", limit=1),
        lambda c: c.analyst_estimates("AAPL", period="quarter", limit=1),
        lambda c: c.earnings_calendar(date(2026, 10, 1), date(2026, 10, 8)),
        lambda c: c.dividend_calendar(date(2026, 10, 1), date(2026, 10, 8)),
        lambda c: c.historical_dividends("AAPL"),
        lambda c: c.daily_bars("AAPL", days=20),
    ],
)
def test_every_client_method_is_gated(call):
    """One ungated method would be enough to leak quota."""
    with pytest.raises(LiveCallBlocked):
        call(fmp_client())


def test_the_gate_precedes_the_rate_limiter():
    """A blocked call must not consume a slot from the shared budget."""
    limiter = NullRateLimiter()
    from ingest.fmp_client import FMPClient

    client = FMPClient(api_key="k", base_url="https://x", limiter=limiter)
    with pytest.raises(LiveCallBlocked):
        client.quotes(["AAPL"])
    assert limiter.total_calls == 0


def test_the_gate_precedes_the_http_session():
    """Nothing reaches the transport, so no socket is opened at all."""
    from ingest.fmp_client import FMPClient

    class ExplodingSession:
        def get(self, *a, **k):
            raise AssertionError("the session must never be reached")

    client = FMPClient(
        api_key="k", base_url="https://x", limiter=NullRateLimiter(), session=ExplodingSession()
    )
    with pytest.raises(LiveCallBlocked):
        client.quotes(["AAPL"])


# ── the probe asks too ──────────────────────────────────────────────────────


def test_the_probe_refuses_without_consent(capsys):
    """Running it IS the consent, but it should be visible in shell history."""
    import importlib

    probe = importlib.import_module("scripts.fmp_probe")
    assert probe.main() == 2
    assert FLAG in capsys.readouterr().out
