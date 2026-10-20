"""Schwab price-history parsing and the token lifecycle. Fixtures only."""

from __future__ import annotations

import os
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from ingest.schwab_client import (
    REFRESH_TOKEN_LIFETIME,
    SchwabTokenError,
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
