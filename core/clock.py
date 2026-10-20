"""Time helpers. All storage is UTC; all trading semantics are Eastern.

CLAUDE.md: timestamps are UTC in the database, displayed in America/New_York.
The trading calendar is the other half of that — "which session is this row
for" is an Eastern-time question, and answering it inline from a UTC timestamp
is where off-by-one-day bugs live. Every job takes its `session_date` from here,
and ruff's DTZ rules exist to stop anyone reaching for `date.today()` instead.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


def utc_now() -> datetime:
    return datetime.now(UTC)


def et_now() -> datetime:
    return datetime.now(EASTERN)


def session_date(at: datetime | None = None) -> date:
    """The ET calendar date a run belongs to.

    Post-close jobs run ~16:30 ET, so the session is simply today in Eastern. A
    job that slips past midnight UTC — which happens every evening, since 16:30
    ET is 20:30 or 21:30 UTC — would file its rows under the wrong day if it
    asked UTC for "today".
    """
    moment = at or et_now()
    if moment.tzinfo is None:
        raise ValueError("session_date() needs a timezone-aware datetime")
    return moment.astimezone(EASTERN).date()


def session_close_utc(day: date) -> datetime:
    """The 16:00 ET close of `day` as a UTC instant. The `ts` on daily bars."""
    return datetime.combine(day, MARKET_CLOSE, tzinfo=EASTERN).astimezone(UTC)


def is_weekday(day: date) -> bool:
    """Monday-Friday. NOT a holiday calendar — see the note below."""
    return day.weekday() < 5


# Market holidays are deliberately absent. Cron runs Mon-Fri (README), and a
# holiday shows up as a job that writes zero rows rather than one that fails. A
# real calendar belongs here once the orchestrator needs to tell "market closed"
# apart from "the feed is broken".
