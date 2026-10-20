"""Clock tests. The trading-day boundary is where off-by-one-day bugs live."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from core import clock


def test_utc_now_is_aware_and_utc():
    now = clock.utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_et_now_is_aware():
    assert clock.et_now().tzinfo is not None


def test_session_date_rejects_a_naive_datetime():
    """The DTZ lint rules and this check exist for the same reason."""
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.session_date(datetime(2026, 10, 20, 16, 30))  # noqa: DTZ001


def test_the_post_close_run_files_under_the_same_et_day():
    """16:30 ET is 20:30 UTC in EDT — already "tomorrow" is a real risk at the
    other end of the day, so this is the case the cron actually hits."""
    at = datetime(2026, 10, 20, 20, 30, tzinfo=UTC)
    assert clock.session_date(at) == date(2026, 10, 20)


def test_late_utc_evening_still_belongs_to_the_et_day_that_started_it():
    """23:00 ET on the 20th is 03:00 UTC on the 21st. Asking UTC for "today"
    would file a backfill or a retry under the wrong trading session."""
    at = datetime(2026, 10, 21, 3, 0, tzinfo=UTC)
    assert clock.session_date(at) == date(2026, 10, 20)


def test_early_utc_morning_is_the_previous_et_day():
    at = datetime(2026, 10, 20, 2, 0, tzinfo=UTC)
    assert clock.session_date(at) == date(2026, 10, 19)


def test_session_close_is_sixteen_hundred_eastern_expressed_in_utc():
    ts = clock.session_close_utc(date(2026, 10, 20))
    assert ts.tzinfo is not None
    assert ts.utcoffset() == timedelta(0)
    assert ts.hour == 20  # EDT: UTC-4
    assert ts.date() == date(2026, 10, 20)


def test_session_close_tracks_the_dst_change():
    """The host clock is ET precisely so the close stays at a stable local time;
    the UTC instant therefore shifts by an hour across the boundary."""
    edt = clock.session_close_utc(date(2026, 10, 20))  # daylight time
    est = clock.session_close_utc(date(2026, 12, 15))  # standard time
    assert edt.hour == 20
    assert est.hour == 21


def test_session_close_round_trips_through_session_date():
    for day in (date(2026, 1, 15), date(2026, 6, 15), date(2026, 11, 2)):
        assert clock.session_date(clock.session_close_utc(day)) == day


@pytest.mark.parametrize(
    ("day", "weekday"),
    [
        (date(2026, 10, 19), True),  # Monday
        (date(2026, 10, 23), True),  # Friday
        (date(2026, 10, 24), False),  # Saturday
        (date(2026, 10, 25), False),  # Sunday
    ],
)
def test_is_weekday(day, weekday):
    assert clock.is_weekday(day) is weekday


def test_there_is_no_holiday_calendar():
    """Deliberate: a holiday shows up as a job writing zero rows, not a failure.

    If this ever changes, the orchestrator can start telling "market closed"
    apart from "the feed is broken" — but until then nothing should pretend to.
    """
    assert clock.is_weekday(date(2026, 12, 25)) is True  # Christmas, a Friday
