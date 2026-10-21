"""Schwab price-history parsing and the token lifecycle. Fixtures only."""

from __future__ import annotations

import os
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import netguard
from core.ratelimit import NullRateLimiter
from ingest import schwab_prices
from ingest.schwab_client import (
    REFRESH_TOKEN_LIFETIME,
    SchwabClient,
    SchwabResponseError,
    SchwabTokenError,
    SchwabTransientError,
    check_token,
    token_age,
    token_expires_at,
)
from ingest.schwab_prices import parse_candles
from tests.fixtures import schwab_payloads as fx

# ── candle parsing ──────────────────────────────────────────────────────────


def test_candles_become_daily_rows():
    rows = parse_candles(fx.price_history(days=5), "TEST")
    assert len(rows) == 5
    assert rows[0]["symbol"] == "TEST"
    assert rows[0]["open"] == 100.0
    assert rows[0]["close"] == 100.5
    assert rows[0]["volume"] == 1_000_000


def test_the_session_date_is_the_eastern_trading_day():
    """A candle stamped 04:00 UTC is midnight ET of the SAME session."""
    rows = parse_candles(fx.price_history(days=1, start=date(2026, 10, 13)), "TEST")
    assert rows[0]["session_date"] == date(2026, 10, 13)


def test_ts_is_stored_as_the_utc_session_close():
    """CLAUDE.md: UTC in the DB. The hypertable dimension must be a real instant."""
    rows = parse_candles(fx.price_history(days=1), "TEST")
    ts = rows[0]["ts"]
    assert ts.tzinfo is not None
    assert ts.utcoffset() == timedelta(0)
    assert ts.date() == date(2026, 10, 13)  # 16:00 ET = 20:00 UTC, same day


def test_dual_time_keying_is_internally_consistent():
    """session_date and ts must describe the same trading day."""
    from core import clock

    for row in parse_candles(fx.price_history(days=5), "TEST"):
        assert clock.session_date(row["ts"]) == row["session_date"]


def test_no_adjusted_columns_are_produced():
    """D24: prices_daily stores raw bars; adjustment happens at read time."""
    row = parse_candles(fx.price_history(days=1), "TEST")[0]
    assert not [key for key in row if key.startswith("adj_")]
    assert set(row) == {"ts", "session_date", "symbol", "open", "high", "low", "close", "volume"}


def test_an_empty_response_yields_nothing():
    assert parse_candles(fx.EMPTY_PRICE_HISTORY, "TEST") == []
    assert parse_candles({}, "TEST") == []


def test_candles_missing_a_timestamp_or_close_are_dropped():
    payload = {
        "candles": [
            {"datetime": None, "close": 100.0},
            {"datetime": 1_792_000_000_000, "close": None},
            {
                "datetime": 1_792_000_000_000,
                "close": 100.0,
                "open": 99.0,
                "high": 101.0,
                "low": 98.0,
                "volume": 5,
            },
        ]
    }
    assert len(parse_candles(payload, "TEST")) == 1


def test_a_nonsense_epoch_is_dropped_not_raised():
    assert parse_candles({"candles": [{"datetime": 9e30, "close": 100.0}]}, "TEST") == []


def test_missing_volume_becomes_zero_not_null():
    payload = {"candles": [{"datetime": 1_792_000_000_000, "close": 100.0}]}
    assert parse_candles(payload, "TEST")[0]["volume"] == 0


# ── token lifecycle (D21 / O8) ──────────────────────────────────────────────


def write_token(tmp_path: Path, *, age_days: float = 0.0) -> Path:
    path = tmp_path / "schwab_token.json"
    path.write_text('{"creation_timestamp": 0, "token": {}}')
    if age_days:
        when = (datetime.now(UTC) - timedelta(days=age_days)).timestamp()
        os.utime(path, (when, when))
    return path


def test_a_missing_token_names_the_fix(tmp_path):
    with pytest.raises(SchwabTokenError, match=re.escape("scripts/schwab_auth.py")):
        check_token(tmp_path / "nope.json")


def test_a_fresh_token_passes(tmp_path):
    check_token(write_token(tmp_path))


def test_a_token_past_its_refresh_lifetime_is_rejected(tmp_path):
    """The weekly re-auth is a schedule, so this failure is expected, not exotic."""
    path = write_token(tmp_path, age_days=REFRESH_TOKEN_LIFETIME.days + 1)
    with pytest.raises(SchwabTokenError, match="past the 7d refresh lifetime"):
        check_token(path)


def test_a_token_just_inside_the_window_still_passes(tmp_path):
    check_token(write_token(tmp_path, age_days=REFRESH_TOKEN_LIFETIME.days - 0.5))


def test_token_age_and_expiry_are_reportable(tmp_path):
    path = write_token(tmp_path, age_days=2)
    assert token_age(path) >= timedelta(days=2)
    assert token_expires_at(path) - datetime.now(UTC) < REFRESH_TOKEN_LIFETIME


def test_the_age_of_an_absent_token_is_none(tmp_path):
    assert token_age(tmp_path / "nope.json") is None
    assert token_expires_at(tmp_path / "nope.json") is None


def test_the_refresh_lifetime_matches_schwabs_seven_days():
    """schwab-py's own easy_client default is 561600s = 6.5 days, half a day of
    margin under the same 7-day limit."""
    lifetime = REFRESH_TOKEN_LIFETIME
    assert lifetime.days == 7
    assert lifetime == timedelta(days=7)


# ── auth is via client_from_token_file, never easy_client ───────────────────


def test_the_client_never_uses_easy_client():
    """easy_client falls back to an interactive browser login, which on a
    headless cron box hangs until the callback timeout instead of failing."""
    source = (Path(__file__).resolve().parent.parent / "ingest" / "schwab_client.py").read_text()
    assert "client_from_token_file" in source
    assert "easy_client(" not in source


# ── transient Schwab errors must be retried, not turned into holes ───────────
#
# Observed live 2026-10-21: a chain crawl drew HTTP 429 on 2 of 433 symbols on
# the third heavy run of the hour — and a DIFFERENT two than the run before.
# With no retry those became a permanent gap in an unbuyable series: chains and
# IV are critical and SACRED (D7/D22), `partial` correctly fails the day, and
# the engine would have been beaten by a rate limit it was already respecting.


@pytest.fixture(autouse=True)
def _no_backoff_in_tests():
    """Keep the real backoff out of the unit suite.

    The client waits 2/4/8s between attempts, which is right in production and
    turns these five tests into half a minute of sleeping. Only the WAIT is
    neutralised — the retry count, the predicate and the limiter accounting are
    exactly what ships, which is what the tests are actually about.
    """
    from tenacity import wait_none

    original = SchwabClient._call.retry.wait
    SchwabClient._call.retry.wait = wait_none()
    yield
    SchwabClient._call.retry.wait = original


class CountingRateLimiter(NullRateLimiter):
    def __init__(self):
        super().__init__()
        self.acquired = 0

    def acquire(self, n: int = 1) -> None:
        self.acquired += n


class FlakyRaw:
    """Fails with `status` for the first `fail_times` calls, then succeeds."""

    def __init__(self, status: int, fail_times: int, payload=None):
        self.status = status
        self.fail_times = fail_times
        self.payload = payload if payload is not None else {"ok": True}
        self.calls = 0

    def get_quotes(self, *a, **k):
        self.calls += 1
        if self.calls <= self.fail_times:
            return SimpleNamespace(status_code=self.status, text="rate limited", json=lambda: {})
        return SimpleNamespace(status_code=200, text="", json=lambda: self.payload)


def _client(raw, monkeypatch):
    monkeypatch.setattr(netguard, "require_live_calls", lambda *a, **k: None)
    return SchwabClient(client=raw, limiter=NullRateLimiter())


def test_a_429_is_retried_until_it_succeeds(monkeypatch):
    raw = FlakyRaw(429, fail_times=2, payload={"SPY": {"symbol": "SPY"}})
    client = _client(raw, monkeypatch)
    assert client.quotes(["SPY"]) == {"SPY": {"symbol": "SPY"}}
    assert raw.calls == 3, "two refusals then the real answer"


def test_every_attempt_takes_a_limiter_slot(monkeypatch):
    """A retry is a real request and must be paced like one."""
    raw = FlakyRaw(503, fail_times=2, payload={"SPY": {}})
    limiter = CountingRateLimiter()
    monkeypatch.setattr(netguard, "require_live_calls", lambda *a, **k: None)
    SchwabClient(client=raw, limiter=limiter).quotes(["SPY"])
    assert limiter.acquired == 3


def test_a_401_is_never_retried(monkeypatch):
    """A stale refresh token is not transient — a human must re-auth."""
    raw = FlakyRaw(401, fail_times=99)
    client = _client(raw, monkeypatch)
    with pytest.raises(SchwabTokenError):
        client.quotes(["SPY"])
    assert raw.calls == 1, "retrying a token failure just delays the alert"


def test_a_404_is_never_retried(monkeypatch):
    raw = FlakyRaw(404, fail_times=99)
    client = _client(raw, monkeypatch)
    with pytest.raises(SchwabResponseError):
        client.quotes(["SPY"])
    assert raw.calls == 1


def test_retries_eventually_give_up_rather_than_loop(monkeypatch):
    raw = FlakyRaw(429, fail_times=99)
    client = _client(raw, monkeypatch)
    with pytest.raises(SchwabTransientError):
        client.quotes(["SPY"])
    assert raw.calls == 4, "stop_after_attempt(4)"


# ── the split clamp (D24) ───────────────────────────────────────────────────


def _patch_queries(monkeypatch, *, newest, latest_split):
    """Stand in for the two reads `start_date_for` makes."""

    def fetch_one(sql, params=None):
        if "max(session_date)" in sql:
            return {"newest": newest}
        return {"ex_date": latest_split}

    monkeypatch.setattr(schwab_prices.db, "fetch_one", fetch_one)


def test_an_incremental_pull_starts_just_before_the_newest_stored_bar(monkeypatch):
    _patch_queries(monkeypatch, newest=date(2026, 10, 20), latest_split=None)
    assert schwab_prices.start_date_for(
        "AAPL", today=date(2026, 10, 21), backfill_days=400
    ) == date(2026, 10, 20) - timedelta(days=schwab_prices.OVERLAP_DAYS)


def test_a_symbol_with_no_bars_gets_the_full_backfill_window(monkeypatch):
    _patch_queries(monkeypatch, newest=None, latest_split=None)
    assert schwab_prices.start_date_for(
        "AAPL", today=date(2026, 10, 21), backfill_days=400
    ) == date(2026, 10, 21) - timedelta(days=400)


def test_the_pull_never_reaches_back_past_a_split_ex_date(monkeypatch):
    """SCHWAB RESTATES ITS OWN HISTORY. Verified live 2026-10-21: all 14 splits
    inside the stored window, BKNG's 25-for-1 included, showed no price jump in
    Schwab's candles. D24 keeps `prices_daily` raw and adjusts at read time, so
    one restated bar gets adjusted twice — and the five-session overlap re-pull
    would have quietly restated five good bars every time a split landed in it.
    """
    _patch_queries(monkeypatch, newest=date(2026, 10, 21), latest_split=date(2026, 10, 19))
    assert schwab_prices.start_date_for(
        "AAPL", today=date(2026, 10, 21), backfill_days=400
    ) == date(2026, 10, 19)


def test_an_old_split_does_not_move_a_recent_window(monkeypatch):
    """The clamp is a floor, not a jump: a 2022 split must not drag a routine
    incremental pull back four years."""
    _patch_queries(monkeypatch, newest=date(2026, 10, 20), latest_split=date(2022, 6, 6))
    assert schwab_prices.start_date_for(
        "AAPL", today=date(2026, 10, 21), backfill_days=400
    ) == date(2026, 10, 20) - timedelta(days=schwab_prices.OVERLAP_DAYS)


def test_a_fresh_symbol_whose_backfill_window_spans_a_split_starts_at_the_split(monkeypatch):
    """Its pre-split history has to come from the raw endpoint instead."""
    _patch_queries(monkeypatch, newest=None, latest_split=date(2026, 9, 3))
    assert schwab_prices.start_date_for(
        "NEWCO", today=date(2026, 10, 21), backfill_days=400
    ) == date(2026, 9, 3)
