"""FMP client transport: URL shape, error classification, payload coercion.

No network — a recording fake stands in for `requests.Session`.

These matter because the parsers are keyed on FIELD names while the client is
keyed on URL shapes. Every parser test would keep passing against a client that
requested entirely wrong URLs, which is precisely the bug an API-generation
difference introduces.
"""

from __future__ import annotations

from datetime import date

import pytest

from core.budget import NullBudget
from core.ratelimit import NullRateLimiter
from ingest.fmp_client import (
    FMPAuthError,
    FMPClient,
    FMPError,
    FMPPlanError,
    FMPTransientError,
    _rows,
)

_INVALID = object()


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or str(payload)
        self.ok = 200 <= status_code < 400

    def json(self):
        if self._payload is _INVALID:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Records every call and replays queued responses."""

    def __init__(self, *responses):
        self.responses = list(responses) or [FakeResponse([])]
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    @property
    def last(self) -> tuple[str, dict]:
        return self.calls[-1]


@pytest.fixture(autouse=True)
def _stub_transport(stubbed_transport):
    """Every test here injects a FakeSession, so no socket is ever opened.

    The D27 gate sits ahead of the session and would otherwise refuse these
    calls — correctly, since it cannot tell a stub from a real client.
    Bypassing it here is explicit; tests/test_netguard.py proves the gate still
    fires everywhere else.
    """


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    """Run the retry paths at full speed.

    `FMPClient.get` is wrapped by tenacity with exponential backoff up to 30s;
    without this the retry tests alone cost ~40 seconds, and a slow suite is a
    suite that stops being run. The retry COUNT is still asserted.
    """
    monkeypatch.setattr(FMPClient.get.retry, "sleep", lambda _seconds: None)


def client(*responses) -> tuple[FMPClient, FakeSession]:
    """A client with no rate limit, no day budget and a recording session.

    `NullBudget` keeps these tests DB-free: the real DayBudget writes to
    `api_budget` on every call, which is correct in production and wrong in a
    unit test. tests/test_budget_db.py covers the real one.
    """
    session = FakeSession(*responses)
    return (
        FMPClient(
            api_key="TESTKEY",
            base_url="https://fmp.example",
            limiter=NullRateLimiter(),
            budget=NullBudget(),
            session=session,
        ),
        session,
    )


# ── the /stable/ shape ──────────────────────────────────────────────────────


def test_the_key_travels_as_apikey_and_is_never_in_the_path():
    api, session = client(FakeResponse([{"symbol": "AAPL"}]))
    api.quotes(["AAPL"])
    url, params = session.last
    assert params["apikey"] == "TESTKEY"
    assert "TESTKEY" not in url


def test_symbols_travel_in_the_query_not_the_path():
    """The core /api/v3/ -> /stable/ difference. `/api/v3/quote/AAPL` is now
    `/stable/quote?symbol=AAPL`, and getting it wrong yields a silent 404."""
    api, session = client(FakeResponse([{"symbol": "AAPL"}]))
    api.quotes(["AAPL"])
    url, params = session.last
    assert url == "https://fmp.example/stable/quote"
    assert params["symbol"] == "AAPL"


def test_many_symbols_use_the_batch_endpoint_with_its_own_parameter_name():
    """`/stable/` splits single and batch quotes, and the split is SILENT.

    `/stable/quote?symbol=A,B,C` returns HTTP 200 and `[]` — not an error. The
    free tier answered that same request with a 402, hiding the mistake behind
    a plan gate for the whole free-tier period; the Premium upgrade turned the
    loud refusal into a silent empty result. Found live 2026-10-21.
    """
    api, session = client(FakeResponse([{"symbol": "AAPL"}, {"symbol": "MSFT"}]))
    api.quotes(["AAPL", "MSFT"])
    url, params = session.last
    assert url == "https://fmp.example/stable/batch-quote"
    assert params["symbols"] == "AAPL,MSFT"  # `symbols`, plural — `symbol` is a 400
    assert "symbol" not in params


def test_a_totally_empty_quote_response_is_an_error_not_an_empty_pass():
    """Index constituents: at least one resolves. Total silence is the bug."""
    api, _ = client(FakeResponse([]))
    with pytest.raises(FMPError, match="returned no rows at all"):
        api.quotes(["AAPL", "MSFT", "NVDA"])


def test_a_partial_quote_response_is_still_accepted():
    """A delisted name legitimately returns nothing; that must not fail a run."""
    api, _ = client(FakeResponse([{"symbol": "AAPL"}]))
    assert len(api.quotes(["AAPL", "DELISTED"])) == 1


@pytest.mark.parametrize(
    ("call", "path", "expected_params"),
    [
        (lambda a: a.sp500_constituents(), "/stable/sp500-constituent", {}),
        (lambda a: a.nasdaq100_constituents(), "/stable/nasdaq-constituent", {}),
        (
            lambda a: a.income_statement("AAPL", period="quarter", limit=4),
            "/stable/income-statement",
            {"symbol": "AAPL", "period": "quarter", "limit": 4},
        ),
        (
            lambda a: a.balance_sheet("AAPL", period="annual", limit=5),
            "/stable/balance-sheet-statement",
            {"symbol": "AAPL", "period": "annual", "limit": 5},
        ),
        (
            lambda a: a.cash_flow("AAPL", period="quarter", limit=4),
            "/stable/cash-flow-statement",
            {"symbol": "AAPL"},
        ),
        (
            lambda a: a.ratios("AAPL", period="quarter", limit=4),
            "/stable/ratios",
            {"symbol": "AAPL"},
        ),
        (
            lambda a: a.key_metrics("AAPL", period="quarter", limit=4),
            "/stable/key-metrics",
            {"symbol": "AAPL"},
        ),
        (
            lambda a: a.analyst_estimates("AAPL", period="quarter", limit=8),
            "/stable/analyst-estimates",
            {"symbol": "AAPL", "period": "quarter", "limit": 8},
        ),
        (
            lambda a: a.earnings_calendar(date(2026, 10, 1), date(2026, 10, 8)),
            "/stable/earnings-calendar",
            {"from": "2026-10-01", "to": "2026-10-08"},
        ),
        (
            lambda a: a.dividend_calendar(date(2026, 10, 1), date(2026, 10, 8)),
            "/stable/dividends-calendar",
            {"from": "2026-10-01", "to": "2026-10-08"},
        ),
        (
            lambda a: a.split_calendar(date(2026, 10, 1), date(2026, 10, 8)),
            "/stable/splits-calendar",
            {"from": "2026-10-01", "to": "2026-10-08"},
        ),
        (lambda a: a.historical_dividends("AAPL"), "/stable/dividends", {"symbol": "AAPL"}),
        (lambda a: a.historical_splits("AAPL"), "/stable/splits", {"symbol": "AAPL"}),
    ],
)
def test_every_endpoint_uses_its_stable_path(call, path, expected_params):
    api, session = client(FakeResponse([]))
    call(api)
    url, params = session.last
    assert url == f"https://fmp.example{path}"
    for key, value in expected_params.items():
        assert params[key] == value


def test_no_legacy_api_v3_path_is_ever_requested():
    api, session = client(FakeResponse([]))
    for call in (
        lambda: api.quotes(["AAPL"]),
        lambda: api.nasdaq100_constituents(),
        lambda: api.ratios("AAPL", period="quarter", limit=1),
        lambda: api.daily_bars("AAPL", days=20),
        lambda: api.historical_splits("AAPL"),
    ):
        call()
    assert all("/api/v3/" not in url for url, _ in session.calls)
    assert all("/stable/" in url for url, _ in session.calls)


def test_daily_bars_asks_for_a_date_window_not_a_timeseries_count():
    """`/stable/` dropped /api/v3/'s `timeseries=N` in favour of from/to."""
    api, session = client(FakeResponse([]))
    api.daily_bars("AAPL", days=20)
    url, params = session.last
    assert url == "https://fmp.example/stable/historical-price-eod/full"
    assert "timeseries" not in params
    span = date.fromisoformat(params["to"]) - date.fromisoformat(params["from"])
    # 20 sessions need more than 20 calendar days; weekends alone cost ~40%.
    assert span.days >= 30


def test_none_valued_params_are_dropped():
    api, session = client(FakeResponse([]))
    api.get("/stable/quote", symbol="AAPL", period=None)
    assert "period" not in session.last[1]


def test_an_empty_symbol_list_makes_no_call():
    api, session = client(FakeResponse([]))
    assert api.quotes([]) == []
    assert session.calls == []


# ── 402 vs 404 ──────────────────────────────────────────────────────────────


def test_402_is_a_plan_gate_with_its_own_error():
    """D29: the five gated endpoints have correct paths; only the plan is missing."""
    api, _ = client(FakeResponse(status_code=402, text="Payment Required"))
    with pytest.raises(FMPPlanError, match="does not include this endpoint"):
        api.analyst_estimates("AAPL", period="quarter", limit=4)


def test_the_402_message_distinguishes_itself_from_a_path_bug():
    api, _ = client(FakeResponse(status_code=402))
    with pytest.raises(FMPPlanError) as excinfo:
        api.ratios("AAPL", period="quarter", limit=1)
    assert "path is correct" in str(excinfo.value)


def test_404_is_a_hard_error_naming_the_status():
    """A wrong path must surface loudly, and differently from a plan gate."""
    api, session = client(FakeResponse(status_code=404, text="Not Found"))
    with pytest.raises(FMPError) as excinfo:
        api.quotes(["AAPL"])
    assert not isinstance(excinfo.value, FMPPlanError)
    assert "404" in str(excinfo.value)
    assert len(session.calls) == 1  # not retried


def test_a_plan_error_is_not_retried():
    """It inherits FMPAuthError, which the retry decorator does not catch."""
    assert issubclass(FMPPlanError, FMPAuthError)
    assert not issubclass(FMPPlanError, FMPTransientError)
    api, session = client(FakeResponse(status_code=402))
    with pytest.raises(FMPPlanError):
        api.ratios("AAPL", period="quarter", limit=1)
    assert len(session.calls) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_fails_immediately(status):
    api, session = client(FakeResponse(status_code=status))
    with pytest.raises(FMPAuthError, match="rejected the API key"):
        api.quotes(["AAPL"])
    assert len(session.calls) == 1  # no retry storm on a dead key


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_transient_statuses_are_retried(status):
    api, session = client(FakeResponse(status_code=status))
    with pytest.raises(FMPTransientError):
        api.quotes(["AAPL"])
    assert len(session.calls) == 4  # stop_after_attempt(4)


def test_a_quota_message_in_a_200_body_is_still_an_auth_error():
    """FMP signals an exhausted allowance with a 200 and an error string.

    The 200 is what makes it dangerous: it reads as a successful request that
    happened to return an unhelpful body. Live on 2026-10-20 this was mistaken
    for a per-symbol data gap 225 times in a single run.
    """
    api, _ = client(FakeResponse({"Error Message": "Limit Reach. Please upgrade your plan."}))
    with pytest.raises(FMPAuthError, match="daily allowance spent"):
        api.quotes(["AAPL"])
    # and the verdict is recorded, so nothing re-discovers it today
    assert api.budget.provider_exhausted() is not None


def test_an_ordinary_plan_message_is_not_an_allowance_marker():
    """A plan complaint that is not about the daily allowance must not mark."""
    api, _ = client(FakeResponse({"Error Message": "Exclusive endpoint. Upgrade required."}))
    with pytest.raises(FMPAuthError):
        api.quotes(["AAPL"])
    # "upgrade required" is a PLAN statement, not "today's allowance is gone".
    # Marking on it would silence every FMP call for the rest of the day over an
    # endpoint this engine may not even use again.
    assert api.budget.provider_exhausted() is None


def test_a_pacing_429_stays_retryable_and_does_not_mark():
    """A 429 about pacing is transient; only one about the plan is terminal."""
    from ingest.fmp_client import FMPTransientError

    api, _ = client(FakeResponse(_INVALID, status_code=429, text="Too Many Requests: slow down"))
    with pytest.raises(FMPTransientError):
        api.quotes(["AAPL"])
    assert api.budget.provider_exhausted() is None


def test_an_allowance_429_marks_and_is_not_retryable():
    api, _ = client(
        FakeResponse(_INVALID, status_code=429, text="Limit Reach . Please upgrade your plan")
    )
    with pytest.raises(FMPAuthError, match="daily allowance spent"):
        api.quotes(["AAPL"])
    assert api.budget.provider_exhausted() is not None


def test_non_json_is_reported_as_such():
    api, _ = client(FakeResponse(_INVALID, text="<html>maintenance</html>"))
    with pytest.raises(FMPError, match="non-JSON"):
        api.quotes(["AAPL"])


def test_an_index_endpoint_returning_the_wrong_shape_fails_loudly():
    """A broken constituents pull must not quietly become an empty universe."""
    api, _ = client(FakeResponse({"unexpected": "object"}))
    with pytest.raises(FMPError, match="expected a list"):
        api.nasdaq100_constituents()


# ── payload coercion across both generations ────────────────────────────────


def test_rows_accepts_a_bare_stable_array():
    assert _rows([{"a": 1}, {"a": 2}]) == [{"a": 1}, {"a": 2}]


def test_rows_accepts_the_legacy_historical_wrapper():
    """So a migration in either direction does not break the parsers."""
    assert _rows({"symbol": "AAPL", "historical": [{"a": 1}]}) == [{"a": 1}]


def test_rows_tolerates_an_empty_or_junk_payload():
    assert _rows([]) == []
    assert _rows(None) == []
    assert _rows("nope") == []


def test_rows_drops_non_dict_entries():
    assert _rows([{"a": 1}, "junk", None]) == [{"a": 1}]


def test_a_symbol_with_no_data_returns_empty_rather_than_raising():
    """One thin ticker must not fail a 500-symbol run."""
    api, _ = client(FakeResponse([]))
    assert api.income_statement("NODATA", period="quarter", limit=4) == []


def test_historical_dividends_are_tagged_with_the_symbol():
    """/stable/dividends rows may omit it; the parser filters on it."""
    api, _ = client(FakeResponse([{"date": "2026-10-05", "dividend": 0.26}]))
    assert api.historical_dividends("AAPL")[0]["symbol"] == "AAPL"


def test_a_symbol_already_present_in_the_row_is_not_overwritten():
    api, _ = client(FakeResponse([{"symbol": "BRK.B", "date": "2026-10-05", "dividend": 1.0}]))
    assert api.historical_dividends("AAPL")[0]["symbol"] == "BRK.B"
