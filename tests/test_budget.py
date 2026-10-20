"""Budget grant semantics. DB-free — the real counter is tested in
tests/test_budget_db.py."""

from __future__ import annotations

from datetime import date

import pytest

from core.budget import BudgetExhausted, DayBudget, Grant, NullBudget

DAY = date(2026, 10, 20)


def test_a_grant_reports_what_was_declined():
    grant = Grant("fmp", DAY, requested=10, granted=4, spent_after=250, budget=250)
    assert grant.declined == 6
    assert grant.partial is True
    assert grant.exhausted is False


def test_a_fully_granted_request_declines_nothing():
    grant = Grant("fmp", DAY, requested=5, granted=5, spent_after=100, budget=250)
    assert grant.declined == 0
    assert grant.partial is False
    assert grant.exhausted is False


def test_an_exhausted_grant_is_not_partial():
    """Nothing was granted at all — the distinction matters for reporting."""
    grant = Grant("fmp", DAY, requested=5, granted=0, spent_after=250, budget=250)
    assert grant.exhausted is True
    assert grant.partial is False
    assert grant.declined == 5


def test_a_grant_renders_readably_for_a_log_line():
    text = str(Grant("fmp", DAY, requested=10, granted=4, spent_after=250, budget=250))
    assert "granted 4/10" in text
    assert "250/250" in text


@pytest.mark.parametrize("bad", [-1, -100])
def test_a_negative_budget_is_rejected(bad):
    with pytest.raises(ValueError, match="budget must be non-negative"):
        DayBudget("fmp", bad)


def test_a_negative_reserve_is_rejected():
    with pytest.raises(ValueError, match="reserve must be non-negative"):
        DayBudget("fmp", 250, reserve=-1)


def test_spending_a_negative_number_is_rejected():
    with pytest.raises(ValueError, match="negative number of calls"):
        NullBudget().spend(-1)


# ── the events reservation ──────────────────────────────────────────────────


def test_for_fmp_honours_the_events_reserve_by_default():
    budget = DayBudget.for_fmp()
    assert budget.budget == 250
    assert budget.reserve == 3


def test_the_events_job_itself_sees_no_reserve():
    """It is the thing the reservation exists to protect, so it must be able to
    spend into the floor."""
    budget = DayBudget.for_fmp(honour_events_reserve=False)
    assert budget.reserve == 0


# ── the null budget ─────────────────────────────────────────────────────────


def test_the_null_budget_grants_everything_but_still_counts():
    """So a test can assert the call sites DO draw, without needing a database."""
    budget = NullBudget()
    for _ in range(5):
        grant = budget.spend(2)
        assert grant.granted == 2
        assert grant.exhausted is False
    assert budget.total_requested == 10
    assert budget.spent() == 10
    assert budget.declined() == 0


def test_the_null_budget_never_raises():
    budget = NullBudget()
    for _ in range(1000):
        budget.spend_or_raise(1)
    assert budget.total_requested == 1000


# ── the exhaustion message ──────────────────────────────────────────────────


def test_budget_exhausted_is_not_a_provider_error():
    """It must not be caught by a provider-error handler, nor retried: nothing
    is wrong with the API, there is simply no quota left."""
    from ingest.fmp_client import FMPBudgetExhausted, FMPError, FMPTransientError

    assert FMPBudgetExhausted is BudgetExhausted
    assert not issubclass(BudgetExhausted, FMPError)
    assert not issubclass(BudgetExhausted, FMPTransientError)
