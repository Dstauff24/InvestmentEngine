"""Release-time stamping (D49). Fixtures only — no network, no database.

The one column a second provider owns. What is pinned here is the care it
takes: a time is written only onto a row we already hold, only when the
provider actually knows it, and never as `unknown` over a time an earlier run
established.
"""

from __future__ import annotations

from datetime import date

import pytest

from ingest.finnhub_earnings import HOUR_MAP, StampResult, parse_times

UNIVERSE = {"AAPL", "MSFT"}


def row(**kw) -> dict:
    base = {"symbol": "AAPL", "date": "2026-10-28", "hour": "bmo", "epsEstimate": 1.2}
    base.update(kw)
    return base


def test_a_known_hour_becomes_an_event_time():
    assert parse_times([row()], UNIVERSE) == [
        {"symbol": "AAPL", "event_date": date(2026, 10, 28), "event_time": "bmo"}
    ]


def test_finnhubs_vocabulary_maps_onto_the_schemas():
    """`dmh` is their third value and the column's CHECK accepts `dmt`."""
    assert HOUR_MAP == {"bmo": "bmo", "amc": "amc", "dmh": "dmt"}
    assert parse_times([row(hour="dmh")], UNIVERSE)[0]["event_time"] == "dmt"


def test_a_blank_hour_is_kept_as_none_because_the_date_is_useful_alone():
    """The DATE sets the effective report date (D49) whether or not the hour is
    known, and Finnhub knows the date far more often than the hour — 100% of
    rows against 15%. `None` rather than "unknown" so a blank can never
    overwrite a time an earlier run established."""
    for blank in ("", None, "whenever"):
        parsed = parse_times([row(hour=blank)], UNIVERSE)
        assert len(parsed) == 1
        assert parsed[0]["event_time"] is None
        assert parsed[0]["event_date"] == date(2026, 10, 28)


def test_symbols_outside_the_universe_are_dropped():
    """The calendar is market-wide: 3,162 rows for a 433-symbol universe."""
    assert parse_times([row(symbol="NOTOURS")], UNIVERSE) == []


def test_a_lowercase_or_padded_hour_still_maps():
    assert parse_times([row(hour=" AMC ")], UNIVERSE)[0]["event_time"] == "amc"


def test_a_malformed_date_is_dropped_rather_than_raising():
    assert parse_times([row(date="not-a-date")], UNIVERSE) == []
    assert parse_times([row(date=None)], UNIVERSE) == []


def test_the_result_separates_coverage_from_agreement():
    """Two different facts, and conflating them would hide either one.

    `hour_coverage` is how often Finnhub knows the time at all — the ceiling on
    how much of the universe can get the one-day tightening. `match_rate` is
    how often its date agrees with the date FMP gave us, and a systematic
    one-day disagreement (measured live: 56% match) shows up ONLY here.
    """
    result = StampResult(
        rows_seen=1000,
        with_hour=150,
        in_universe=100,
        matched=56,
        near_matched=40,
        later_in_finnhub=22,
        changed=50,
    )
    assert result.hour_coverage == pytest.approx(0.15)
    assert result.match_rate == pytest.approx(0.56)
    assert "56 dated the same day" in str(result)
    assert "40 disagreed" in str(result)
    assert "22 later in Finnhub" in str(result), (
        "the direction is the point: only a Finnhub date LATER than FMP's moves "
        "the effective report date, and only an FMP date that is too early can "
        "hide a lookahead"
    )


def test_an_empty_run_reports_no_coverage_rather_than_zero_percent():
    """0% means "asked and got nothing"; None means "nothing was asked"."""
    result = StampResult()
    assert result.hour_coverage is None
    assert result.match_rate is None
