"""Pillar C measurements (D32 weight 20). Fixtures only — no DB, no network."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from core import config
from features import catalyst as cat
from features.evidence import EMPTY, depth_from_dates

AS_OF = date(2026, 10, 21)


def event(kind: str, when: date, **extra) -> dict:
    row = {"symbol": "TEST", "event_type": kind, "event_date": when, "dividend_amount": None}
    row.update(extra)
    return row


def purchase(
    insider: str,
    filed: date,
    value: float,
    *,
    code: str = "P",
    derivative: bool = False,
    transaction: date | None = None,
) -> dict:
    return {
        "insider_cik": insider,
        "insider_name": insider,
        "filed_at": datetime.combine(filed, datetime.min.time(), tzinfo=UTC).replace(hour=18),
        "filed_session": filed,
        "transaction_date": transaction or filed,
        "transaction_code": code,
        "value_usd": value,
        "is_derivative": derivative,
        "is_10b5_1": False,
    }


def rule(**overrides) -> config.InsiderCluster:
    base = {
        "min_distinct_insiders": 3,
        "window_days": 90,
        "min_aggregate_usd": 250_000.0,
        "purchase_code": "P",
        "exclude_derivatives": True,
    }
    base.update(overrides)
    return config.InsiderCluster(**base)


def depth(days: int = 120):
    return depth_from_dates([AS_OF - timedelta(days=i) for i in range(days)])


# ══════════════════════════════════════════════════════════════════════════
# Event proximity
# ══════════════════════════════════════════════════════════════════════════


def test_days_to_the_next_earnings_date():
    events = [event("earnings", AS_OF + timedelta(days=9))]
    assert cat.days_to_earnings(events, as_of=AS_OF) == 9


def test_earnings_today_is_zero_days_not_none():
    assert cat.days_to_earnings([event("earnings", AS_OF)], as_of=AS_OF) == 0


def test_the_nearest_upcoming_earnings_date_wins():
    events = [
        event("earnings", AS_OF + timedelta(days=90)),
        event("earnings", AS_OF + timedelta(days=5)),
    ]
    assert cat.days_to_earnings(events, as_of=AS_OF) == 5


def test_no_earnings_on_record_is_none_not_a_large_number():
    """ "We do not know when earnings are" must not read as "earnings are far
    away" — recipe 1 gates on proximity and would take that as a green light."""
    assert cat.days_to_earnings([], as_of=AS_OF) is None
    assert cat.days_to_earnings([event("ex_dividend", AS_OF)], as_of=AS_OF) is None


def test_a_past_earnings_date_is_not_upcoming():
    events = [event("earnings", AS_OF - timedelta(days=3))]
    assert cat.days_to_earnings(events, as_of=AS_OF) is None
    assert cat.days_since_earnings(events, as_of=AS_OF) == 3


def test_days_since_earnings_takes_the_most_recent_past_date():
    events = [
        event("earnings", AS_OF - timedelta(days=200)),
        event("earnings", AS_OF - timedelta(days=8)),
    ]
    assert cat.days_since_earnings(events, as_of=AS_OF) == 8


def test_ex_dividend_proximity_feeds_the_two_day_rule():
    """D19(3): no new swing long within two days of an ex-date."""
    events = [event("ex_dividend", AS_OF + timedelta(days=1), dividend_amount=0.25)]
    assert cat.ex_dividend_proximity(events, as_of=AS_OF) == 1


def test_an_absent_ex_date_cannot_satisfy_the_two_day_rule():
    """None has to propagate: the rule must not be met by ignorance."""
    assert cat.ex_dividend_proximity([], as_of=AS_OF) is None


def test_the_next_dividend_amount_skips_an_entry_with_no_amount():
    events = [
        event("ex_dividend", AS_OF + timedelta(days=2)),
        event("ex_dividend", AS_OF + timedelta(days=40), dividend_amount=0.50),
    ]
    assert cat._next_dividend_amount(events, as_of=AS_OF) == pytest.approx(0.50)


# ══════════════════════════════════════════════════════════════════════════
# Insider clusters — the rule is config
# ══════════════════════════════════════════════════════════════════════════


def test_three_distinct_insiders_above_the_floor_is_a_cluster():
    buys = [
        purchase("A", AS_OF - timedelta(days=10), 100_000),
        purchase("B", AS_OF - timedelta(days=20), 100_000),
        purchase("C", AS_OF - timedelta(days=30), 100_000),
    ]
    result = cat.insider_cluster(buys, as_of=AS_OF, cfg=rule(), series_depth=depth())
    assert result.is_cluster is True
    assert result.distinct_insiders == 3
    assert result.aggregate_usd == pytest.approx(300_000)
    assert result.shortfall is None


def test_one_insider_filing_four_times_is_not_a_cluster():
    """THE point of 'distinct': one director's opinion reported four times
    would otherwise look like a consensus."""
    buys = [purchase("A", AS_OF - timedelta(days=i), 200_000) for i in (5, 10, 15, 20)]
    result = cat.insider_cluster(buys, as_of=AS_OF, cfg=rule(), series_depth=depth())
    assert result.purchases == 4
    assert result.distinct_insiders == 1
    assert result.is_cluster is False


def test_an_award_is_not_a_purchase():
    """Code 'A' is a grant on a schedule somebody set months ago. Counting it
    builds a signal that measures payroll rather than opinion."""
    buys = [
        purchase("A", AS_OF - timedelta(days=5), 500_000, code="A"),
        purchase("B", AS_OF - timedelta(days=6), 500_000, code="A"),
        purchase("C", AS_OF - timedelta(days=7), 500_000, code="A"),
    ]
    result = cat.insider_cluster(buys, as_of=AS_OF, cfg=rule(), series_depth=depth())
    assert result.distinct_insiders == 0
    assert result.is_cluster is False


def test_an_option_exercise_is_not_a_purchase():
    buys = [purchase(x, AS_OF - timedelta(days=5), 500_000, code="M") for x in "ABC"]
    assert (
        cat.insider_cluster(buys, as_of=AS_OF, cfg=rule(), series_depth=depth()).is_cluster is False
    )


def test_a_sale_is_not_a_purchase():
    buys = [purchase(x, AS_OF - timedelta(days=5), 500_000, code="S") for x in "ABC"]
    assert (
        cat.insider_cluster(buys, as_of=AS_OF, cfg=rule(), series_depth=depth()).is_cluster is False
    )


def test_a_derivative_purchase_is_excluded():
    buys = [purchase(x, AS_OF - timedelta(days=5), 500_000, derivative=True) for x in "ABC"]
    assert (
        cat.insider_cluster(buys, as_of=AS_OF, cfg=rule(), series_depth=depth()).is_cluster is False
    )


def test_the_dollar_floor_stops_three_token_purchases():
    buys = [purchase(x, AS_OF - timedelta(days=5), 2_000) for x in "ABC"]
    result = cat.insider_cluster(buys, as_of=AS_OF, cfg=rule(), series_depth=depth())
    assert result.distinct_insiders == 3
    assert result.is_cluster is False
    assert "$6,000" in result.shortfall


def test_purchases_outside_the_window_do_not_count():
    buys = [
        purchase("A", AS_OF - timedelta(days=10), 200_000),
        purchase("B", AS_OF - timedelta(days=20), 200_000),
        purchase("C", AS_OF - timedelta(days=200), 200_000),  # outside 90 days
    ]
    result = cat.insider_cluster(buys, as_of=AS_OF, cfg=rule(), series_depth=depth())
    assert result.distinct_insiders == 2
    assert result.is_cluster is False


def test_the_window_is_measured_on_FILING_time_not_transaction_time():
    """THE point-in-time requirement, at the feature layer.

    A purchase TRANSACTED inside the window but FILED outside it was not
    knowable; one filed inside but transacted earlier was. Keying on
    transaction_date would admit the first and reject the second.
    """
    # transacted 89 days ago, filed 200 days ago is impossible — so test the
    # realistic direction: transacted long ago, filed recently.
    buys = [
        purchase("A", AS_OF - timedelta(days=2), 200_000, transaction=AS_OF - timedelta(days=120)),
        purchase("B", AS_OF - timedelta(days=3), 200_000, transaction=AS_OF - timedelta(days=130)),
        purchase("C", AS_OF - timedelta(days=4), 200_000, transaction=AS_OF - timedelta(days=140)),
    ]
    result = cat.insider_cluster(buys, as_of=AS_OF, cfg=rule(), series_depth=depth())
    assert result.is_cluster is True, (
        "all three were FILED inside the window, so all three were knowable — "
        "keying on transaction_date would have excluded every one"
    )


def test_a_filing_after_the_as_of_date_is_not_visible():
    buys = [
        purchase("A", AS_OF - timedelta(days=1), 200_000),
        purchase("B", AS_OF - timedelta(days=1), 200_000),
        purchase("C", AS_OF + timedelta(days=1), 200_000),  # tomorrow's filing
    ]
    result = cat.insider_cluster(buys, as_of=AS_OF, cfg=rule(), series_depth=depth())
    assert result.distinct_insiders == 2


def test_the_rule_is_config_so_changing_it_changes_the_verdict():
    """D25: the numbers live in config, and this is what that buys."""
    buys = [purchase(x, AS_OF - timedelta(days=5), 100_000) for x in "AB"]
    strict = cat.insider_cluster(buys, as_of=AS_OF, cfg=rule(), series_depth=depth())
    loose = cat.insider_cluster(
        buys,
        as_of=AS_OF,
        cfg=rule(min_distinct_insiders=2, min_aggregate_usd=150_000),
        series_depth=depth(),
    )
    assert strict.is_cluster is False
    assert loose.is_cluster is True


# ── depth: the series is our own ────────────────────────────────────────────


def test_a_young_series_answers_none_rather_than_false():
    """Before the job has ever run there is no evidence either way, and False
    would read as "nobody bought"."""
    result = cat.insider_cluster([], as_of=AS_OF, cfg=rule(), series_depth=EMPTY)
    assert result.is_cluster is None
    assert result.shortfall == "insider series too young to judge"


def test_a_started_series_with_no_purchases_is_a_genuine_false():
    """Once the series exists, "no cluster" is a claim we have earned."""
    result = cat.insider_cluster([], as_of=AS_OF, cfg=rule(), series_depth=depth(30))
    assert result.is_cluster is False


def test_depth_is_reported_beside_the_verdict_not_folded_into_it():
    buys = [purchase(x, AS_OF - timedelta(days=5), 200_000) for x in "ABC"]
    thin = cat.insider_cluster(buys, as_of=AS_OF, cfg=rule(), series_depth=depth(3))
    thick = cat.insider_cluster(buys, as_of=AS_OF, cfg=rule(), series_depth=depth(400))
    assert thin.is_cluster is thick.is_cluster is True
    assert thin.depth.observations == 3
    assert thick.depth.observations == 400


# ══════════════════════════════════════════════════════════════════════════
# The bundle
# ══════════════════════════════════════════════════════════════════════════


def test_compute_combines_events_and_insiders():
    events = [
        event("earnings", AS_OF + timedelta(days=12)),
        event("ex_dividend", AS_OF + timedelta(days=3), dividend_amount=0.24),
    ]
    buys = [purchase(x, AS_OF - timedelta(days=5), 200_000) for x in "ABC"]
    features = cat.compute("TEST", events, buys, as_of=AS_OF, series_depth=depth())

    assert features.days_to_earnings == 12
    assert features.days_to_ex_dividend == 3
    assert features.next_dividend_amount == pytest.approx(0.24)
    assert features.insiders.is_cluster is True


def test_short_interest_is_an_explicit_none_not_an_omission():
    """No provider this engine implements exposes it. Carried as None so the
    gap is visible in every logged row rather than silently absent."""
    features = cat.compute("TEST", [], [], as_of=AS_OF)
    assert features.short_interest_pct is None
    assert "short_interest_pct" in features.as_dict()


def test_compute_on_nothing_abstains_everywhere():
    features = cat.compute("TEST", [], [], as_of=AS_OF)
    assert features.days_to_earnings is None
    assert features.days_to_ex_dividend is None
    assert features.insiders.is_cluster is None


def test_as_dict_flattens_the_nested_depth():
    features = cat.compute("TEST", [], [], as_of=AS_OF, series_depth=depth())
    out = features.as_dict()
    assert isinstance(out["insiders"]["depth"], dict)
    assert "observations" in out["insiders"]["depth"]


# ══════════════════════════════════════════════════════════════════════════
# D48: can the beat check see the actual before it was published?
# ══════════════════════════════════════════════════════════════════════════

REPORTED = date(2026, 10, 20)


def earnings_row(**kw) -> dict:
    base = {
        "event_type": "earnings",
        "event_date": REPORTED,
        "event_time": "unknown",
        "alt_event_date": None,
        "eps_actual": 1.20,
        "eps_estimate": 1.00,
        "revenue_actual": 1_100.0,
        "revenue_estimate": 1_000.0,
    }
    base.update(kw)
    return base


def test_an_actual_is_not_knowable_on_the_report_date_without_a_release_time():
    """THE LOOKAHEAD THIS CLOSES. No FMP endpoint publishes a release time any
    more (verified 2026-10-22), so a report dated today may have been released
    after the close — five minutes after the bar `gap_up_pct` is measured on.
    Crediting that surprise to that bar reads a number published after the
    price it is supposed to explain."""
    surprise = cat.last_earnings_surprise([earnings_row()], as_of=REPORTED)

    assert surprise.eps_beat is None
    assert surprise.withheld_report_on == REPORTED
    assert "not knowable until the next session" in surprise.verdict


def test_the_same_actual_is_knowable_the_next_session():
    surprise = cat.last_earnings_surprise([earnings_row()], as_of=REPORTED + timedelta(days=1))
    assert surprise.eps_beat is True
    assert surprise.withheld_report_on is None


def test_a_confirmed_pre_open_report_is_knowable_on_the_report_date():
    """Kept and tested although nothing populates `bmo` today: the distinction
    is load-bearing, and the day a source appears the rule should already be
    right rather than need rediscovering."""
    surprise = cat.last_earnings_surprise([earnings_row(event_time="bmo")], as_of=REPORTED)
    assert surprise.eps_beat is True


def test_an_after_close_report_is_withheld_on_the_report_date():
    surprise = cat.last_earnings_surprise([earnings_row(event_time="amc")], as_of=REPORTED)
    assert surprise.eps_beat is None
    assert surprise.withheld_report_on == REPORTED


def test_a_withheld_report_does_not_fall_back_to_the_previous_quarter():
    """ "Did they beat" means the report that just moved the price. Answering
    with the quarter before would be a different claim wearing the same field."""
    events = [
        earnings_row(event_date=date(2026, 7, 21), eps_actual=0.50, eps_estimate=0.90),
        earnings_row(),
    ]
    surprise = cat.last_earnings_surprise(events, as_of=REPORTED)
    assert surprise.eps_beat is None
    assert surprise.eps_actual is None, "not the July quarter's miss either"


def test_our_own_snapshot_replaces_the_vendor_estimate_and_says_so():
    """The vendor's historical rows are MUTABLE — one carried `lastUpdated`
    sixteen months after the report — so only a snapshot this engine wrote on a
    date is point-in-time by construction."""
    surprise = cat.last_earnings_surprise(
        [earnings_row()], as_of=REPORTED + timedelta(days=1), own_estimate=1.15
    )
    assert surprise.eps_estimate == pytest.approx(1.15)
    assert surprise.estimate_source == "own_snapshot"
    assert surprise.eps_surprise_pct == pytest.approx((1.20 - 1.15) / 1.15 * 100)


def test_without_a_snapshot_the_vendor_figure_is_used_and_labelled():
    surprise = cat.last_earnings_surprise([earnings_row()], as_of=REPORTED + timedelta(days=1))
    assert surprise.eps_estimate == pytest.approx(1.00)
    assert surprise.estimate_source == "vendor"


def test_knowability_is_decided_per_event_not_per_symbol():
    assert cat.actual_is_knowable(earnings_row(), as_of=REPORTED) is False
    assert cat.actual_is_knowable(earnings_row(), as_of=REPORTED + timedelta(days=1)) is True
    assert cat.actual_is_knowable(earnings_row(event_time="bmo"), as_of=REPORTED) is True


# ── D49 correction: T is the LATER of the two providers' dates ──────────────


def test_the_effective_report_date_is_the_later_of_the_two():
    """THE WINDOW ABSORBS A LATE DATE AND NOT AN EARLY ONE.

    The reaction window assumes the actual is public by the close of T+1, which
    holds when T is the real release date or later. If FMP dates a report a day
    EARLY, the true release can fall after the close of T+1 and the engine
    would read an actual that was not yet public.
    """
    later_elsewhere = earnings_row(event_date=REPORTED, alt_event_date=REPORTED + timedelta(days=1))
    assert cat.effective_report(later_elsewhere)[0] == REPORTED + timedelta(days=1)

    later_here = earnings_row(event_date=REPORTED, alt_event_date=REPORTED - timedelta(days=1))
    assert cat.effective_report(later_here)[0] == REPORTED


def test_a_disagreement_delays_knowability_by_a_day():
    """The whole point of the correction: with FMP a day early, the actual is
    not knowable at the close of FMP's T+1."""
    row = earnings_row(event_date=REPORTED, alt_event_date=REPORTED + timedelta(days=1))

    assert cat.actual_is_knowable(row, as_of=REPORTED + timedelta(days=1)) is False
    assert cat.actual_is_knowable(row, as_of=REPORTED + timedelta(days=2)) is True

    agreed = earnings_row(event_date=REPORTED)
    assert cat.actual_is_knowable(agreed, as_of=REPORTED + timedelta(days=1)) is True


def test_the_surprise_reports_the_effective_date_not_the_providers():
    surprise = cat.last_earnings_surprise(
        [earnings_row(alt_event_date=REPORTED + timedelta(days=1))],
        as_of=REPORTED + timedelta(days=2),
    )
    assert surprise.reported_on == REPORTED + timedelta(days=1)
    assert surprise.eps_beat is True


def test_a_release_time_only_applies_to_the_date_that_published_it():
    """Finnhub publishes the hour, so it describes `alt_event_date`. Using it
    on a date Finnhub did not give us would attribute a fact to the wrong
    session."""
    # Finnhub is later: its hour describes the winning date, so it applies.
    finnhub_later = earnings_row(
        event_date=REPORTED, alt_event_date=REPORTED + timedelta(days=1), event_time="bmo"
    )
    assert cat.effective_report(finnhub_later) == (REPORTED + timedelta(days=1), "bmo")

    # FMP is later: nobody published a time for THAT date.
    finnhub_earlier = earnings_row(
        event_date=REPORTED, alt_event_date=REPORTED - timedelta(days=1), event_time="bmo"
    )
    assert cat.effective_report(finnhub_earlier) == (REPORTED, "unknown")

    # They agree: the hour belongs to the shared date.
    agreed = earnings_row(event_date=REPORTED, alt_event_date=REPORTED, event_time="amc")
    assert cat.effective_report(agreed) == (REPORTED, "amc")


def test_with_no_second_opinion_the_providers_date_stands():
    assert cat.effective_report(earnings_row()) == (REPORTED, "unknown")
    assert cat.effective_report(earnings_row(event_time="bmo")) == (REPORTED, "bmo")


def test_the_latest_report_is_chosen_on_the_effective_date():
    """A disagreement can reorder two reports that are days apart."""
    events = [
        earnings_row(event_date=date(2026, 10, 19), alt_event_date=date(2026, 10, 22)),
        earnings_row(event_date=date(2026, 10, 20), eps_actual=9.9),
    ]
    latest = cat.last_knowable_earnings(events, as_of=date(2026, 10, 23))
    assert cat.effective_report(latest)[0] == date(2026, 10, 22)


# ── the live loader must fetch every field `effective_report` reads ─────────
#
# D49's safeguard was populated, read by the function, and NOT SELECTED by the
# live query for three evenings, so `alt_event_date` was None on every live row
# and the mechanism was never in force. `backtest/engine.py` selected it all
# along, which made the backtest and the live engine disagree about what date a
# report landed on.


def test_the_live_events_query_selects_the_fields_D49_depends_on():
    """A static check, because the failure was invisible at runtime: a missing
    column reads as `None`, which is indistinguishable from "no second opinion
    exists" — the overwhelmingly common case."""
    from features.catalyst import EVENTS_SQL

    for column in ("event_date", "event_time", "alt_event_date"):
        assert column in EVENTS_SQL, (
            f"`effective_report` reads {column!r}; a query that does not fetch it "
            f"makes the safeguard silently inert"
        )


def test_the_live_and_backtest_event_queries_agree_on_those_fields():
    """The two paths must not be able to date the same report differently.
    Nothing would ever have said which was right."""
    from backtest.engine import EVENTS_SQL as BACKTEST_SQL
    from features.catalyst import EVENTS_SQL as LIVE_SQL

    for column in ("event_time", "alt_event_date"):
        assert (column in LIVE_SQL) == (column in BACKTEST_SQL), (
            f"{column!r} is selected by one path and not the other"
        )


def test_the_later_provider_date_wins_through_the_loaded_shape():
    """End to end on the dict the loader actually produces, not a hand-built
    one — the bug lived in the gap between those two."""
    from features.catalyst import effective_report

    loaded = {
        "symbol": "AON",
        "event_type": "earnings",
        "event_date": date(2026, 10, 28),
        "alt_event_date": date(2026, 10, 29),
        "event_time": "bmo",
        "eps_estimate": 1.0,
        "eps_actual": None,
    }
    assert effective_report(loaded) == (date(2026, 10, 29), "bmo")

    # And with the two columns absent — the pre-fix shape — it silently falls
    # back to FMP's earlier date, which is the behaviour that was live.
    stripped = {k: v for k, v in loaded.items() if k not in ("alt_event_date", "event_time")}
    assert effective_report(stripped) == (date(2026, 10, 28), "unknown")
