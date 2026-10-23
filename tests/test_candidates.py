"""D42 amended: the day's ranked candidates, and their forward returns.

Fixtures only — no DB, no network. The boundary these tests defend is that a
candidate is a SCORE: it has no entry, no stop, no R multiple and no outcome,
and nothing D40's ladder reads may ever see one.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from core import config
from jobs import candidate_returns
from scoring import runner
from scoring.composite import ScoredSymbol, UniverseRun, rank_universe
from tests.fixtures import price_series as ps

AS_OF = date(2026, 10, 23)
CFG = config.scoring()


def scored(symbol: str, composite: float | None, **kw) -> ScoredSymbol:
    """A scored symbol with abstaining pillars — the writer reads the score,
    the mask and the recipes, and none of this needs a real pillar set."""
    item = ScoredSymbol(
        symbol=symbol, pillars=runner.abstaining_pillars(symbol), composite=composite
    )
    for key, value in kw.items():
        setattr(item, key, value)
    return item


class _Result:
    """A RecipeResult stand-in: only what the writer reads."""

    def __init__(self, recipe: str, reason: str, triggered: bool = False, plan=None):
        self.recipe = recipe
        self.reason = reason
        self.triggered = triggered
        self.plan = plan


class _Plan:
    def __init__(self, risk_reward: float):
        self.risk_reward = risk_reward


# ── which rows are kept ─────────────────────────────────────────────────────


def test_the_cut_is_by_rank_over_the_whole_universe():
    """RANKED, NOT QUALIFYING. A high score that fired no setup is the entire
    point — a cut on `tier is not None` would keep only the ideas the gates
    already liked and could never ask whether the gates add value."""
    universe = [scored(f"S{i}", 90.0 - i) for i in range(20)]
    rank_universe(universe, CFG)
    rows = runner.candidate_rows(UniverseRun(scored=universe), day=AS_OF, cfg=CFG)

    assert len(rows) == CFG.ranking.max_ideas_per_day
    assert [r["rank"] for r in rows] == list(range(1, len(rows) + 1))
    assert rows[0]["symbol"] == "S0", "the best composite ranks first"
    assert all(r["tier"] is None for r in rows), "none of these cleared a gate"


def test_an_abstaining_symbol_is_not_a_candidate():
    """No composite means no rank, and a row with a null score could not
    contribute to the question the table exists for."""
    universe = [scored("GOOD", 80.0), scored("DARK", None)]
    rank_universe(universe, CFG)
    rows = runner.candidate_rows(UniverseRun(scored=universe), day=AS_OF, cfg=CFG)
    assert [r["symbol"] for r in rows] == ["GOOD"]


def test_the_mask_and_the_percentile_travel_with_the_score():
    """D41: 72 from four pillars and 72 from two are different claims, and
    whether thin composites predict worse is the calibration question."""
    universe = [scored("AAPL", 72.0)]
    rank_universe(universe, CFG)
    [row] = runner.candidate_rows(UniverseRun(scored=universe), day=AS_OF, cfg=CFG)
    assert row["pillars_scored"] == 0, "the fixture abstains on all four"
    assert row["percentile"] == pytest.approx(0.0)
    assert row["composite"] == pytest.approx(72.0)


def test_the_gates_in_force_are_recorded_with_the_row():
    """A threshold change must not silently reinterpret what yesterday's rank
    meant — the same reason they travel on `daily_scoring_stats`."""
    universe = [scored("AAPL", 72.0)]
    rank_universe(universe, CFG)
    [row] = runner.candidate_rows(UniverseRun(scored=universe), day=AS_OF, cfg=CFG)
    assert row["gates_in_force"]["tier_a_min_composite"] == CFG.tiers.a.min_composite
    assert row["gates_in_force"]["max_ideas_per_day"] == CFG.ranking.max_ideas_per_day


# ── the reason no plan exists ───────────────────────────────────────────────


def test_every_recipes_refusal_is_recorded_not_just_the_first():
    """A score of 78 that ran into three different walls is a different fact
    from one whose single recipe refused on a gate, and the shape of these
    strings across a few hundred rows is what says whether the recipes or the
    gates are what the composite keeps hitting."""
    item = scored(
        "AAPL",
        78.0,
        recipes=[
            _Result("pullback_to_trend", "not in an uptrend (MAs not stacked)"),
            _Result("base_breakout", "recipe disabled in config"),
            _Result("post_earnings_drift", "no knowable report"),
        ],
    )
    rank_universe([item], CFG)
    [row] = runner.candidate_rows(UniverseRun(scored=[item]), day=AS_OF, cfg=CFG)
    assert "pullback_to_trend: not in an uptrend" in row["no_plan_reason"]
    assert "base_breakout: recipe disabled" in row["no_plan_reason"]
    assert "post_earnings_drift: no knowable report" in row["no_plan_reason"]
    assert row["recipe"] is None and row["risk_reward"] is None


def test_a_plan_that_exists_records_the_recipe_and_leaves_the_reason_null():
    item = scored(
        "AAPL",
        78.0,
        recipes=[_Result("post_earnings_drift", "gap held", triggered=True, plan=_Plan(3.25))],
    )
    rank_universe([item], CFG)
    [row] = runner.candidate_rows(UniverseRun(scored=[item]), day=AS_OF, cfg=CFG)
    assert row["no_plan_reason"] is None
    assert row["recipe"] == "post_earnings_drift"
    assert row["risk_reward"] == pytest.approx(3.25)


def test_a_symbol_with_no_recipe_results_says_exactly_that():
    item = scored("AAPL", 78.0, recipes=[])
    rank_universe([item], CFG)
    [row] = runner.candidate_rows(UniverseRun(scored=[item]), day=AS_OF, cfg=CFG)
    assert row["no_plan_reason"] == "no recipe produced a result at all"


def test_a_very_long_reason_is_clipped_rather_than_refused():
    item = scored("AAPL", 78.0, recipes=[_Result("r", "x" * 2000)])
    rank_universe([item], CFG)
    [row] = runner.candidate_rows(UniverseRun(scored=[item]), day=AS_OF, cfg=CFG)
    assert len(row["no_plan_reason"]) == runner.MAX_REASON_CHARS
    assert row["no_plan_reason"].endswith("…")


# ── forward returns ─────────────────────────────────────────────────────────


def _bars(closes: list[float]) -> pd.DataFrame:
    return ps.frame(closes)


def test_a_horizon_is_counted_in_SESSIONS_not_calendar_days():
    """A holiday week would otherwise shorten the window it claims to measure.
    The fixture's sessions are weekdays, so the 5-session mark is five ROWS on,
    whatever the calendar did."""
    bars = _bars([100.0] * 1 + [101.0, 102.0, 103.0, 104.0, 110.0] + [120.0] * 20)
    start = bars["session_date"].iloc[0]
    measured = candidate_returns.forward_returns(bars, from_date=start)
    value, when = measured[5]
    assert value == pytest.approx(10.0), "100 -> 110 is +10%"
    assert when == bars["session_date"].iloc[5]


def test_a_horizon_the_bars_do_not_reach_is_none_not_zero():
    """None is "the sessions have not passed"; 0.0 would read as "it went
    nowhere", and the difference is the whole point of a young table."""
    bars = _bars([100.0] * 8)
    start = bars["session_date"].iloc[0]
    measured = candidate_returns.forward_returns(bars, from_date=start)
    assert measured[5] is not None
    assert measured[10] is None
    assert measured[20] is None


def test_a_scoring_date_with_no_bar_measures_nothing():
    bars = _bars([100.0] * 30)
    assert candidate_returns.forward_returns(bars, from_date=date(1999, 1, 1)) == {
        5: None,
        10: None,
        20: None,
    }


def test_the_return_is_measured_from_the_scoring_sessions_close():
    bars = _bars([100.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0] + [100.0] * 20)
    second = bars["session_date"].iloc[1]
    value, _ = candidate_returns.forward_returns(bars, from_date=second)[5]
    assert value == pytest.approx(100.0), "50 -> 100 is +100%, not 0% from the first bar"


def test_a_negative_horizon_return_is_reported_as_it_is():
    bars = _bars([100.0, 99.0, 98.0, 97.0, 96.0, 80.0] + [80.0] * 20)
    start = bars["session_date"].iloc[0]
    value, _ = candidate_returns.forward_returns(bars, from_date=start)[5]
    assert value == pytest.approx(-20.0)


# ── the boundary: scores are not trades ─────────────────────────────────────


def test_the_writer_records_nothing_a_promotion_decision_reads():
    """D13's expectancy and D40's ladder count CLOSED SHADOW BOOK IDEAS. A
    candidate has no entry, stop, target, R multiple or outcome, and the row
    this builds must not carry one under any name."""
    item = scored("AAPL", 78.0)
    rank_universe([item], CFG)
    [row] = runner.candidate_rows(UniverseRun(scored=[item]), day=AS_OF, cfg=CFG)
    forbidden = {
        "entry",
        "entry_low",
        "entry_high",
        "stop",
        "target_1",
        "target_2",
        "r_multiple",
        "outcome",
        "status_at_call",
        "regime_at_call",
    }
    assert forbidden.isdisjoint(row.keys()), "a candidate row must not look like a Shadow Book call"


def test_the_job_only_ever_fills_a_null():
    """A repaired basis or a late bar must not silently move a number a study
    has already read, so the UPDATE coalesces rather than assigns."""
    assert "COALESCE(fwd_5_pct" in candidate_returns.FILL
    assert "COALESCE(fwd_20_on" in candidate_returns.FILL
    assert "SET\n        fwd_5_pct  = %" not in candidate_returns.FILL


def test_the_job_never_writes_to_the_shadow_book():
    """Static: the module that fills forward returns must not be able to touch
    the table D40 counts."""
    from pathlib import Path

    source = Path(candidate_returns.__file__).read_text()
    assert "shadow_book" not in source.replace(
        "D40's ladder count CLOSED SHADOW BOOK IDEAS", ""
    ).lower().replace("shadow book", "")


def test_an_accepted_idea_is_recorded_wherever_it_ranks():
    """On 2026-10-21 the one idea that passed every stage sat at RANK 51. A cut
    at the top N by rank would leave the day's only call out of the table whose
    purpose is relating a composite to what happened next."""
    items = [scored(f"S{i}", 95.0 - i) for i in range(20)]
    rank_universe(items, CFG)
    deep = items[12]
    deep.surfaced = True

    rows = runner.candidate_rows(UniverseRun(scored=items), day=AS_OF, cfg=CFG)
    by_symbol = {row["symbol"]: row for row in rows}
    assert deep.symbol in by_symbol, "the accepted idea must be in the table"
    assert by_symbol[deep.symbol]["surfaced"] is True
    assert by_symbol[deep.symbol]["rank"] == 13
    assert by_symbol[deep.symbol]["refused_stage"] is None, "it was not refused"
