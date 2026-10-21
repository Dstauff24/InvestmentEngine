"""The day-level budget counter, against a real database.

    INTEGRATION_DB_OK=1 pytest -m integration

The invariant under test is the one that cannot be established without a shared
store: TOTAL calls per calendar day never exceed `fmp.daily_call_budget`, no
matter how many separate jobs draw from it. The earlier per-job caps satisfied
each job's own arithmetic and jointly blew the day.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from core import db, settings
from core.budget import BudgetExhausted, DayBudget
from tests.dbguard import assert_safe_database

pytestmark = pytest.mark.integration

DAY = date(2026, 10, 20)
NEXT_DAY = DAY + timedelta(days=1)


@pytest.fixture(scope="module", autouse=True)
def _guard_database():
    return assert_safe_database(settings.database_url())


@pytest.fixture(autouse=True)
def clean():
    assert_safe_database(settings.database_url())
    db.execute("DELETE FROM api_budget WHERE true")
    yield
    db.execute("DELETE FROM api_budget WHERE true")


def budget(total=10, reserve=0, provider="fmp") -> DayBudget:
    return DayBudget(provider, total, reserve=reserve)


# ── basic accounting ────────────────────────────────────────────────────────


def test_a_first_spend_creates_the_row():
    grant = budget().spend(3, day=DAY)
    assert grant.granted == 3
    assert grant.spent_after == 3
    assert budget().spent(DAY) == 3


def test_spends_accumulate():
    b = budget(total=10)
    b.spend(3, day=DAY)
    b.spend(4, day=DAY)
    assert b.spent(DAY) == 7
    assert b.remaining(DAY) == 3


def test_a_request_beyond_the_budget_is_partially_granted():
    b = budget(total=10)
    b.spend(8, day=DAY)
    grant = b.spend(5, day=DAY)
    assert grant.granted == 2
    assert grant.declined == 3
    assert grant.partial is True
    assert b.spent(DAY) == 10


def test_the_budget_is_never_exceeded():
    b = budget(total=10)
    for _ in range(20):
        b.spend(3, day=DAY)
    assert b.spent(DAY) == 10  # THE invariant


def test_declined_calls_are_recorded_for_reporting():
    """A run that stopped on budget must look different from a quiet run."""
    b = budget(total=5)
    b.spend(5, day=DAY)
    b.spend(7, day=DAY)
    assert b.declined(DAY) == 7
    assert b.spent(DAY) == 5


def test_an_exhausted_budget_grants_nothing():
    b = budget(total=3)
    b.spend(3, day=DAY)
    grant = b.spend(1, day=DAY)
    assert grant.granted == 0
    assert grant.exhausted is True


def test_spend_or_raise_raises_only_when_nothing_was_granted():
    b = budget(total=3)
    assert b.spend_or_raise(2, day=DAY).granted == 2
    assert b.spend_or_raise(5, day=DAY).granted == 1  # partial is fine
    with pytest.raises(BudgetExhausted, match="declined spend, not a failure"):
        b.spend_or_raise(1, day=DAY)


def test_spending_zero_is_a_no_op():
    b = budget(total=10)
    assert b.spend(0, day=DAY).granted == 0
    assert b.spent(DAY) == 0


# ── THE cross-job invariant ─────────────────────────────────────────────────


def test_two_jobs_in_one_day_cannot_jointly_exceed_the_budget():
    """The bug this replaces: universe enrichment capped its own pass and
    fundamentals capped its own, so each stayed "within budget" while together
    they spent twice it.

    Two independent DayBudget instances stand in for two `python -m` processes.
    """
    universe_job = DayBudget("fmp", 10)
    fundamentals_job = DayBudget("fmp", 10)

    universe_spent = sum(universe_job.spend(1, day=DAY).granted for _ in range(7))
    fundamentals_spent = sum(fundamentals_job.spend(1, day=DAY).granted for _ in range(7))

    assert universe_spent == 7
    assert fundamentals_spent == 3  # only what was left
    assert universe_spent + fundamentals_spent == 10
    assert DayBudget("fmp", 10).spent(DAY) == 10


def test_three_jobs_share_one_budget():
    jobs = [DayBudget("fmp", 12) for _ in range(3)]
    granted = sum(j.spend(5, day=DAY).granted for j in jobs)
    assert granted == 12


def test_the_counter_survives_a_fresh_instance():
    """Stands in for a new `python -m` invocation: an in-process counter would
    have reset here, which is the whole reason this lives in the database."""
    DayBudget("fmp", 10).spend(6, day=DAY)
    assert DayBudget("fmp", 10).remaining(DAY) == 4


def test_providers_have_independent_budgets():
    DayBudget("fmp", 10).spend(10, day=DAY)
    assert DayBudget("finnhub", 10).remaining(DAY) == 10


# ── the ET date boundary ────────────────────────────────────────────────────


def test_the_counter_resets_on_the_session_date_boundary():
    """Keyed on the ET trading date, so it rolls over with the session rather
    than at UTC midnight — the same reason every job takes its date from
    core.clock."""
    b = budget(total=10)
    b.spend(10, day=DAY)
    assert b.spent(DAY) == 10
    assert b.remaining(DAY) == 0

    assert b.spent(NEXT_DAY) == 0
    assert b.remaining(NEXT_DAY) == 10
    assert b.spend(4, day=NEXT_DAY).granted == 4

    # Yesterday's figure is untouched — the history is a record, not a counter.
    assert b.spent(DAY) == 10


def test_each_day_keeps_its_own_row():
    b = budget(total=10)
    b.spend(3, day=DAY)
    b.spend(7, day=NEXT_DAY)
    rows = db.fetch_all("SELECT session_date, calls_spent FROM api_budget ORDER BY session_date")
    assert [(r["session_date"], r["calls_spent"]) for r in rows] == [(DAY, 3), (NEXT_DAY, 7)]


def test_an_unspent_day_reports_zero_rather_than_missing():
    assert budget().spent(date(2020, 1, 1)) == 0
    assert budget().declined(date(2020, 1, 1)) == 0


# ── the events reservation ──────────────────────────────────────────────────


def test_a_reserve_is_held_back_from_other_jobs():
    """The event radar must not be crowded out by a fundamentals crawl."""
    general = DayBudget("fmp", 10, reserve=3)
    for _ in range(20):
        general.spend(1, day=DAY)
    assert general.spent(DAY) == 7  # stopped at budget - reserve
    assert general.remaining(DAY) == 0


def test_the_events_job_can_spend_into_the_reserve():
    general = DayBudget("fmp", 10, reserve=3)
    events = DayBudget("fmp", 10, reserve=0)

    for _ in range(20):
        general.spend(1, day=DAY)
    assert general.spent(DAY) == 7

    assert events.spend(3, day=DAY).granted == 3  # the three calendar calls
    assert DayBudget("fmp", 10).spent(DAY) == 10  # still never exceeded


def test_the_reserve_cannot_push_total_spend_over_the_budget():
    events = DayBudget("fmp", 10, reserve=0)
    events.spend(10, day=DAY)
    assert events.spend(5, day=DAY).granted == 0
    assert DayBudget("fmp", 10).spent(DAY) == 10


def test_the_shipped_config_reserves_the_three_calendar_calls():
    """Premium from 2026-10-21: 9000/day, still with the 3-call events floor.

    Asserted as budget-minus-reserve rather than a literal, so a future budget
    change does not need this test edited — what matters is that the floor is
    exactly the three market-wide calendar endpoints and that the events job
    itself can spend into it.
    """
    b = DayBudget.for_fmp()
    events = DayBudget.for_fmp(honour_events_reserve=False)
    assert b.reserve == 3
    assert b.budget - b.reserve == b.budget - 3
    assert events.budget == b.budget
    assert events.reserve == 0


# ── the client draws from it ────────────────────────────────────────────────


def test_the_fmp_client_draws_one_call_per_request():
    """The chokepoint: every call site is covered because the client spends."""
    import requests

    from core.ratelimit import NullRateLimiter
    from ingest.fmp_client import FMPClient

    class FakeResponse:
        status_code = 200
        ok = True
        text = "[]"

        def json(self):
            return []

    class FakeSession:
        def __init__(self):
            self.calls = 0

        def get(self, *a, **k):
            self.calls += 1
            return FakeResponse()

    session = FakeSession()
    client = FMPClient(
        api_key="k",
        base_url="https://x",
        limiter=NullRateLimiter(),
        budget=DayBudget("fmp", 3),
        session=session,
    )

    # The D27 gate would refuse these, so bypass it the way the stub tests do.
    import core.netguard as netguard_module

    original = netguard_module.require_live_calls
    try:
        netguard_module.require_live_calls = lambda *a, **k: None
        for _ in range(3):
            client.quotes(["AAPL"])
        with pytest.raises(BudgetExhausted):
            client.quotes(["AAPL"])
    finally:
        netguard_module.require_live_calls = original

    assert session.calls == 3  # the fourth never reached the transport
    assert DayBudget("fmp", 3).spent() == 3
    assert isinstance(requests.Session(), requests.Session)


# ── the provider-exhausted marker ─────────────────────────────────────────────
#
# `calls_spent` counts what THIS engine spent and cannot know what the provider
# already served elsewhere. On 2026-10-20 the first live universe run began
# believing it had 247 calls while FMP's allowance was nearly gone, and spent
# 225 of them discovering that one symbol at a time — each answered with HTTP
# 200, so each looked like a fresh request worth making.


class CountingSession:
    """A transport that refuses to be used, and counts attempts."""

    def __init__(self, response=None):
        self.calls = 0
        self.response = response

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        if self.response is None:
            raise AssertionError("transport was called when it should not have been")
        return self.response


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)
        self.ok = 200 <= status_code < 400

    def json(self):
        return self._payload


def test_a_second_job_after_the_marker_makes_zero_transport_calls(stubbed_transport):
    """THE point of the marker: the day's second job must not re-discover it.

    `stubbed_transport` stands down the D27 consent gate, which otherwise fires
    first and hides the behaviour under test. Nothing here reaches a socket —
    the whole assertion is that the second job does not try to.
    """
    from ingest.fmp_client import FMPClient

    spent_body = {"Error Message": "Limit Reach . Please upgrade your plan"}

    # Job one: a generous budget, and the provider says no anyway.
    session_one = CountingSession(FakeResponse(spent_body))
    job_one = FMPClient(budget=budget(total=250), session=session_one)
    with pytest.raises(Exception, match="daily allowance spent"):
        job_one.quotes(["AAPL"])
    assert session_one.calls == 1, "job one must actually reach the provider once"

    # Job two: a brand-new client and budget object, as a separate `python -m`
    # invocation from cron would have. It reads the marker from the database.
    session_two = CountingSession(response=None)  # asserts if touched
    job_two = FMPClient(budget=budget(total=250), session=session_two)
    with pytest.raises(BudgetExhausted, match="reports its own daily allowance spent"):
        job_two.quotes(["MSFT"])

    assert session_two.calls == 0, (
        "the second job opened a socket to rediscover what the first job "
        "already recorded — which is the 225-wasted-call bug"
    )


def test_the_marker_is_scoped_to_its_session_date():
    b = budget(total=10)
    b.mark_provider_exhausted("allowance gone", day=DAY)
    assert b.provider_exhausted(DAY) == "allowance gone"
    assert b.provider_exhausted(NEXT_DAY) is None
    # Tomorrow still spends normally.
    assert b.spend(3, day=NEXT_DAY).granted == 3
    assert b.spend(3, day=DAY).granted == 0


def test_the_marker_keeps_the_first_observation():
    b = budget(total=10)
    b.mark_provider_exhausted("first", day=DAY)
    b.mark_provider_exhausted("second", day=DAY)
    assert b.provider_exhausted(DAY) == "first"


def test_the_marker_still_records_declined_spend():
    """A job stopping on the marker must report a shortfall, not a clean run."""
    b = budget(total=10)
    b.mark_provider_exhausted("allowance gone", day=DAY)
    grant = b.spend(4, day=DAY)
    assert grant.granted == 0
    assert grant.declined == 4
    assert grant.provider_exhausted is True
    assert b.declined(DAY) == 4


def test_the_marker_does_not_invent_spend():
    """`calls_spent` must keep answering "what did WE spend", not "the cap"."""
    b = budget(total=250)
    b.spend(7, day=DAY)
    b.mark_provider_exhausted("allowance gone", day=DAY)
    b.spend(50, day=DAY)
    assert b.spent(DAY) == 7, (
        "collapsing the two numbers would erase the divergence between our "
        "accounting and the provider's, which is the thing worth seeing"
    )
