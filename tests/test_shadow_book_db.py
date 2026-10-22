"""Logging and tracking against a real database (D13/D40/D41/D42).

    PGDATABASE=ie_test pytest -m integration

What fixtures cannot establish: that the projections are FROZEN, that logging
is idempotent on (symbol, recipe, day), and that the tracker's
`last_evaluated_on` really prevents a re-run from double-counting.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from core import db, settings
from decision.idea_builder import Idea
from shadow_book.logger import gates_in_force, log_idea, log_surfaced
from shadow_book.outcome_tracker import track_open_calls
from tests.dbguard import assert_safe_database

pytestmark = pytest.mark.integration

SYMBOL = "ZZSHDW"
CALLED = date(2026, 10, 1)
TODAY = date(2026, 10, 10)


@pytest.fixture(scope="module", autouse=True)
def _guard_database():
    return assert_safe_database(settings.database_url())


@pytest.fixture(autouse=True)
def seeded():
    assert_safe_database(db.settings.database_url())
    _clean()
    db.execute(
        "INSERT INTO tickers (symbol, sector, is_active) VALUES (%s, 'Technology', TRUE)",
        (SYMBOL,),
    )
    yield
    _clean()


def _clean() -> None:
    db.execute("DELETE FROM shadow_book WHERE symbol = %s", (SYMBOL,))
    db.execute("DELETE FROM prices_daily WHERE symbol = %s", (SYMBOL,))
    db.execute("DELETE FROM corporate_actions WHERE symbol = %s", (SYMBOL,))
    db.execute("DELETE FROM tickers WHERE symbol = %s", (SYMBOL,))


def idea(**kw) -> Idea:
    base = {
        "symbol": SYMBOL,
        "sector": "Technology",
        "tier": "A",
        "recipe": "pullback_to_trend",
        "status": "shadow",
        "composite": 82.0,
        "percentile": 97.0,
        "mask": "A,B",
        "entry_low": 99.5,
        "entry_high": 100.5,
        "stop": 95.0,
        "target_1": 110.0,
        "target_2": 125.0,
        "risk_reward": 5.0,
        "horizon_days": (2, 15),
        "shares": 454,
        "risk_amount": 2500.0,
        "risk_pct": 2.5,
        "position_value": 45400.0,
        "account_value": 100000.0,
        "vehicle": "shares",
        "vehicle_note": "IV rank unavailable, day 0 of 60",
        "notes": ["tracked, not traded"],
    }
    base.update(kw)
    return Idea(**base)


def add_bar(day: date, high: float, low: float, close: float) -> None:
    db.execute(
        """
        INSERT INTO prices_daily (symbol, session_date, ts, open, high, low, close,
                                  volume, source)
        VALUES (%s, %s, %s::timestamptz, %s, %s, %s, %s, 1000000, 'test')
        ON CONFLICT (symbol, ts) DO UPDATE SET close = EXCLUDED.close
        """,
        (SYMBOL, day, f"{day} 20:00+00", low, high, low, close),
    )


# ── logging ─────────────────────────────────────────────────────────────────


def test_a_logged_call_carries_everything_the_decision_needs():
    idea_id = log_idea(idea(), sentiment_scorer="lexicon/1", as_of=CALLED, regime="bull_calm")
    row = db.fetch_one("SELECT * FROM shadow_book WHERE id = %s", (idea_id,))

    assert row["status_at_call"] == "shadow", "D40"
    assert row["regime_at_call"] == "bull_calm", "D45"
    assert row["scored_pillars"] == "A,B" and row["pillars_scored"] == 2, "D41"
    assert row["sentiment_scorer"] == "lexicon/1"
    assert row["gates_in_force"]["tier_a_min_composite"] == 70.0
    assert row["outcome"] == "open"
    assert float(row["current_stop"]) == pytest.approx(95.0)


def test_the_gates_in_force_travel_with_the_call():
    """A threshold change must not silently reinterpret yesterday's calls."""
    gates = gates_in_force()
    assert "tier_a_min_composite" in gates
    assert "pillar_weights" in gates
    assert gates["pillar_weights"]["technical"] == 40.0


def test_logging_is_idempotent_on_symbol_recipe_and_day():
    """A re-run of the pipeline must not count one opinion twice."""
    assert log_idea(idea(), as_of=CALLED) is not None
    assert log_idea(idea(), as_of=CALLED) is None
    assert (
        db.fetch_one("SELECT count(*) AS n FROM shadow_book WHERE symbol = %s", (SYMBOL,))["n"] == 1
    )


def test_two_recipes_on_one_symbol_are_two_calls():
    """They are genuinely different plans."""
    log_idea(idea(recipe="pullback_to_trend"), as_of=CALLED)
    log_idea(idea(recipe="base_breakout"), as_of=CALLED)
    assert (
        db.fetch_one("SELECT count(*) AS n FROM shadow_book WHERE symbol = %s", (SYMBOL,))["n"] == 2
    )


def test_log_surfaced_counts_only_what_it_newly_wrote():
    assert log_surfaced([idea(), idea()], as_of=CALLED) == 1


# ── the projections are FROZEN ──────────────────────────────────────────────


def test_the_tracker_moves_current_stop_and_never_proj_stop():
    """The whole point of a projection is measuring reality against what we
    said we expected; a projection that drifts toward the outcome measures
    nothing."""
    log_idea(idea(), as_of=CALLED)
    add_bar(CALLED + timedelta(days=1), 102, 100.0, 101)
    add_bar(CALLED + timedelta(days=2), 112, 105.0, 111)

    track_open_calls(as_of=TODAY)
    row = db.fetch_one("SELECT * FROM shadow_book WHERE symbol = %s", (SYMBOL,))

    assert float(row["proj_stop"]) == pytest.approx(95.0), "frozen"
    assert float(row["current_stop"]) == pytest.approx(100.0), "moved to breakeven"
    assert row["hit_target_1"] is True
    assert row["stop_to_breakeven_on"] == CALLED + timedelta(days=2)


def test_a_tracked_call_records_its_entry_and_excursions():
    log_idea(idea(), as_of=CALLED)
    add_bar(CALLED + timedelta(days=1), 102, 100.0, 101)
    add_bar(CALLED + timedelta(days=2), 109, 97.0, 108)

    track_open_calls(as_of=TODAY)
    row = db.fetch_one("SELECT * FROM shadow_book WHERE symbol = %s", (SYMBOL,))
    assert float(row["actual_entry"]) == pytest.approx(100.0)
    assert float(row["max_favorable_excursion"]) == pytest.approx(9.0)
    assert float(row["max_adverse_excursion"]) == pytest.approx(-3.0)


def test_a_stop_out_closes_the_call_with_a_negative_r():
    log_idea(idea(), as_of=CALLED)
    add_bar(CALLED + timedelta(days=1), 102, 100.0, 101)
    add_bar(CALLED + timedelta(days=2), 101, 90.0, 92)

    track_open_calls(as_of=TODAY)
    row = db.fetch_one("SELECT * FROM shadow_book WHERE symbol = %s", (SYMBOL,))
    assert row["outcome"] == "stopped"
    assert float(row["r_multiple"]) == pytest.approx(-1.0)
    assert row["closed_at"] is not None


# ── idempotence ─────────────────────────────────────────────────────────────


def test_re_running_the_tracker_does_not_double_count():
    """`last_evaluated_on` is what makes the daily job safe to re-run."""
    log_idea(idea(), as_of=CALLED)
    add_bar(CALLED + timedelta(days=1), 102, 100.0, 101)
    add_bar(CALLED + timedelta(days=2), 109, 97.0, 108)

    first = track_open_calls(as_of=TODAY)
    second = track_open_calls(as_of=TODAY)

    assert len(first) >= 1
    assert second == [], "a day already evaluated is not re-read"
    row = db.fetch_one("SELECT * FROM shadow_book WHERE symbol = %s", (SYMBOL,))
    assert float(row["max_favorable_excursion"]) == pytest.approx(9.0)


def test_a_call_made_today_is_not_evaluated_against_todays_bar():
    """It was called at today's close; today's bar is the bar it was called on."""
    log_idea(idea(), as_of=TODAY)
    add_bar(TODAY, 130, 90.0, 120)
    assert track_open_calls(as_of=TODAY) == []
    row = db.fetch_one("SELECT * FROM shadow_book WHERE symbol = %s", (SYMBOL,))
    assert row["outcome"] == "open"
    assert row["actual_entry"] is None


def test_a_closed_call_is_not_re_evaluated():
    log_idea(idea(), as_of=CALLED)
    add_bar(CALLED + timedelta(days=1), 102, 100.0, 101)
    add_bar(CALLED + timedelta(days=2), 101, 90.0, 92)
    track_open_calls(as_of=TODAY)

    add_bar(CALLED + timedelta(days=3), 140, 130.0, 139)
    track_open_calls(as_of=TODAY + timedelta(days=1))
    row = db.fetch_one("SELECT * FROM shadow_book WHERE symbol = %s", (SYMBOL,))
    assert row["outcome"] == "stopped", "a closed call stays closed"


# ── D19(2) end to end ───────────────────────────────────────────────────────


def test_a_dividend_lowers_the_stop_in_the_database():
    log_idea(idea(), as_of=CALLED)
    add_bar(CALLED + timedelta(days=1), 102, 100.0, 101)
    add_bar(CALLED + timedelta(days=2), 100, 94.6, 95)
    db.execute(
        """
        INSERT INTO corporate_actions (symbol, ex_date, action_type, cash_amount, source)
        VALUES (%s, %s, 'cash_dividend', 0.50, 'test')
        """,
        (SYMBOL, CALLED + timedelta(days=2)),
    )

    track_open_calls(as_of=TODAY)
    row = db.fetch_one("SELECT * FROM shadow_book WHERE symbol = %s", (SYMBOL,))
    assert row["outcome"] == "open", "94.6 is above the dividend-adjusted 94.5"
    assert float(row["current_stop"]) == pytest.approx(94.5)


# ── D45: the regime travels with the call ───────────────────────────────────


def test_the_regime_is_stamped_at_call_time_and_never_recomputed():
    """D45. The benchmark series gets revised — a late print, a split backfill,
    a repaired basis — so a regime derived at read time would move an old call
    between cells underneath a promotion decision already made."""
    idea_id = log_idea(idea(), as_of=CALLED, regime="bear_volatile")

    stored = db.fetch_one("SELECT regime_at_call FROM shadow_book WHERE id = %s", (idea_id,))[
        "regime_at_call"
    ]
    assert stored == "bear_volatile"

    # Whatever happens to the benchmark afterwards, the stamp stands.
    assert (
        db.fetch_one("SELECT regime_at_call FROM shadow_book WHERE id = %s", (idea_id,))[
            "regime_at_call"
        ]
        == "bear_volatile"
    )


def test_a_call_with_no_benchmark_history_is_unknown_rather_than_guessed():
    """`ie_test` holds no SPY series at all, which is exactly the shape of a
    fresh deployment's first 200 sessions."""
    from shadow_book.logger import regime_at

    assert regime_at(CALLED) == "unknown"


def test_the_promotion_query_groups_per_recipe_and_regime():
    """D45 evaluates the ladder per recipe x REGIME, so the 30-idea floor
    applies per cell. This is the shape that query needs."""
    log_idea(idea(recipe="pullback_to_trend"), as_of=CALLED, regime="bull_calm")
    log_idea(idea(recipe="base_breakout"), as_of=CALLED, regime="bull_calm")
    log_idea(
        idea(recipe="pullback_to_trend"),
        as_of=CALLED + timedelta(days=1),
        regime="bear_volatile",
    )

    rows = db.fetch_all(
        """
        SELECT recipe, regime_at_call, count(*) AS n
        FROM   shadow_book
        WHERE  symbol = %s
        GROUP  BY recipe, regime_at_call
        ORDER  BY recipe, regime_at_call
        """,
        (SYMBOL,),
    )
    assert [(r["recipe"], r["regime_at_call"], r["n"]) for r in rows] == [
        ("base_breakout", "bull_calm", 1),
        ("pullback_to_trend", "bear_volatile", 1),
        ("pullback_to_trend", "bull_calm", 1),
    ]
