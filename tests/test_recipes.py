"""The three Phase 1 recipes (D11/D12/D25). Fixtures only — no DB, no network.

What these pin: that R:R is DERIVED from structure rather than configured (so
D12's gates can bind), that a refusal always names the condition that failed,
and that the recipes read features rather than re-measuring.
"""

from __future__ import annotations

from datetime import date

import pytest

from core import config
from features.catalyst import CatalystFeatures, EarningsSurprise, InsiderCluster
from features.evidence import EvidenceDepth
from features.fundamental import FundamentalFeatures, RevisionMomentum
from features.technical import (
    EarningsReaction,
    MAStack,
    PriceStructure,
    TechnicalFeatures,
    WeekStructure,
)
from scoring.recipes import breakout, drift, evaluate_all, pullback
from scoring.recipes.base import build_plan

AS_OF = date(2026, 10, 21)
CFG = config.scoring()


def tech(**kw) -> TechnicalFeatures:
    ma = MAStack(
        **{
            "fast": 100.0,
            "mid": 95.0,
            "slow": 90.0,
            "stacked": True,
            "fast_rising": True,
            "mid_rising": True,
            "price_above_fast": True,
            "price_above_slow": True,
            "pct_from_fast": 0.5,
            **kw.pop("ma", {}),
        }
    )
    structure = PriceStructure(
        **{
            "swing_high": 120.0,
            "swing_low": 94.0,
            "pullback_low": 97.0,
            "base_high": 120.0,
            "base_low": 100.0,
            "base_depth_pct": 16.7,
            "base_weeks": 8.0,
            "gap_up_pct": 0.0,
            "gap_day_low": 99.0,
            "prior_close": 100.0,
            **kw.pop("structure", {}),
        }
    )
    week = WeekStructure(120.0, 80.0, -16.7, 25.0, 0.5, False, 252)
    base = {
        "symbol": "TEST",
        "session_date": AS_OF,
        "close": 100.0,
        "ma": ma,
        "rsi": 45.0,
        "atr": 2.0,
        "atr_pct": 2.0,
        "relative_volume": 0.7,
        # Coiled by default (O13): volatility in the quietest 15% of its own
        # year, which is what `base_breakout` asks for.
        "squeeze_percentile": 0.15,
        "week52": week,
        "rs_vs_sector": 5.0,
        "structure": structure,
        "bars_available": 300,
    }
    base.update(kw)
    return TechnicalFeatures(**base)


def cata(**kw) -> CatalystFeatures:
    insiders = InsiderCluster(False, 0, 0, 0.0, None, 90, EvidenceDepth(30, 90))
    base = {
        "symbol": "TEST",
        "as_of": AS_OF,
        "days_to_earnings": 30,
        "days_since_earnings": 60,
        "days_to_ex_dividend": 40,
        "next_dividend_amount": None,
        "insiders": insiders,
        # O12: the last reported quarter\'s beat, as a fact. The default is
        # the honest one — unverified, which the recipe refuses rather than
        # reads as a miss.
        "surprise": EarningsSurprise(None, None, None, None, None),
    }
    base.update(kw)
    return CatalystFeatures(**base)


def fund(**kw) -> FundamentalFeatures:
    revision = RevisionMomentum(None, None, None, None, None, None, EvidenceDepth(0, 0))
    base = {
        "symbol": "TEST",
        "as_of": AS_OF,
        "fiscal_date_ending": date(2026, 6, 30),
        "revenue_growth_yoy": 20.0,
        "eps_growth_yoy": 25.0,
        "revenue_acceleration": None,
        "eps_acceleration": None,
        "gross_margin": 40.0,
        "operating_margin": 20.0,
        "net_margin": 15.0,
        "gross_margin_trend": None,
        "operating_margin_trend": None,
        "net_margin_trend": None,
        "roe": None,
        "roic": None,
        "debt_to_equity": None,
        "revision": revision,
        "quarters_available": 8,
    }
    base.update(kw)
    return FundamentalFeatures(**base)


# ══════════════════════════════════════════════════════════════════════════
# The plan geometry
# ══════════════════════════════════════════════════════════════════════════


def test_risk_reward_is_derived_from_structure_not_configured():
    """THE design point. If TP2 were set at "3R", R:R would be a CONSTANT and
    D12's "Tier A needs 2:1, Tier B 3:1" would be satisfied by construction on
    every single idea — a gate that can never bind."""
    near = build_plan(
        entry_reference=100.0,
        structural_stop=98.0,
        target_1=104.0,
        target_2=108.0,
        features=tech(),
        horizon_days=(2, 15),
    )[0]
    far = build_plan(
        entry_reference=100.0,
        structural_stop=98.0,
        target_1=110.0,
        target_2=130.0,
        features=tech(),
        horizon_days=(2, 15),
    )[0]
    assert near.risk_reward != pytest.approx(far.risk_reward)
    assert far.risk_reward > near.risk_reward


def test_risk_reward_is_quoted_to_tp2_not_tp1():
    """TP1 is the trim that protects the trade (D18 moves the stop to breakeven
    there); TP2 is what the idea is for. Quoting TP1 would make every plan look
    roughly 1:1 and the D12 gates meaningless."""
    plan = build_plan(
        entry_reference=100.0,
        structural_stop=97.0,
        target_1=103.0,
        target_2=112.0,
        features=tech(),
        horizon_days=(2, 15),
    )[0]
    assert plan.risk_reward > plan.risk_reward_tp1
    assert plan.risk_reward_tp1 == pytest.approx(1.0, abs=0.3)


def test_the_entry_is_a_zone_not_a_price():
    plan = build_plan(
        entry_reference=100.0,
        structural_stop=96.0,
        target_1=110.0,
        target_2=120.0,
        features=tech(),
        horizon_days=(2, 15),
    )[0]
    assert plan.entry_low < plan.entry < plan.entry_high
    width = plan.entry_high - plan.entry_low
    assert width == pytest.approx(2 * CFG.geometry.entry_zone_atr * 2.0)


def test_the_stop_sits_below_the_structural_level_by_an_atr_buffer():
    plan = build_plan(
        entry_reference=100.0,
        structural_stop=96.0,
        target_1=110.0,
        target_2=120.0,
        features=tech(),
        horizon_days=(2, 15),
    )[0]
    assert plan.stop == pytest.approx(96.0 - CFG.geometry.stop_atr_buffer * 2.0)


def test_a_stop_inside_the_noise_is_refused():
    """A flattering R:R the market takes out on an ordinary day."""
    plan, why = build_plan(
        entry_reference=100.0,
        structural_stop=100.2,
        target_1=110.0,
        target_2=120.0,
        features=tech(),
        horizon_days=(2, 15),
    )
    assert plan is None
    assert "noise floor" in why or "at or above" in why


def test_no_atr_means_no_plan():
    """Every distance in a plan is measured in ATR."""
    plan, why = build_plan(
        entry_reference=100.0,
        structural_stop=96.0,
        target_1=110.0,
        target_2=120.0,
        features=tech(atr=None),
        horizon_days=(2, 15),
    )
    assert plan is None
    assert "ATR" in why


def test_a_target_behind_the_price_is_not_a_bad_trade_but_no_trade():
    plan, why = build_plan(
        entry_reference=100.0,
        structural_stop=96.0,
        target_1=99.0,
        target_2=120.0,
        features=tech(),
        horizon_days=(2, 15),
    )
    assert plan is None
    assert "not above the entry" in why


# ══════════════════════════════════════════════════════════════════════════
# Recipe 1: pullback to trend
# ══════════════════════════════════════════════════════════════════════════


def test_a_clean_pullback_fires_with_the_SHIPPED_v3_GEOMETRY():
    """The shipped default is v3 (D56), so this describes v3 and would fail if
    the variant drifted back: the stop hangs on the PULLBACK low rather than
    the 60-day swing low, there is no far target at all, and the remainder
    trails. v1's and v2's geometries keep their own named tests against their
    own config fixtures."""
    result = pullback.evaluate(tech(), cata())
    assert result.triggered, result.reason
    assert result.plan is not None
    assert result.plan.target_1 == pytest.approx(120.0), "TP1 is the swing high"
    assert result.plan.stop == pytest.approx(96.0), (
        "the pullback low (97.0) less the 0.5-ATR buffer — not the 60-day low, "
        "which is what v1 used and what made it timeout-dominated"
    )
    assert result.plan.target_2 is None, "v3 has no far target by design"
    assert result.plan.trail_ma == 20
    assert result.plan.horizon_days[1] == 45, "O20's swept net, not the 15-day swing horizon"


def test_a_pullback_without_an_uptrend_does_not_fire():
    result = pullback.evaluate(tech(ma={"stacked": False}), cata())
    assert not result.triggered
    assert "uptrend" in result.reason


def test_an_rsi_outside_the_reset_band_does_not_fire():
    result = pullback.evaluate(tech(rsi=65.0), cata())
    assert not result.triggered
    assert "reset band" in result.reason


def test_earnings_inside_the_window_is_a_hard_refusal():
    """A 2-15 day horizon straddling earnings is a coin flip wearing a trade
    plan — a refusal, not a score penalty."""
    result = pullback.evaluate(tech(), cata(days_to_earnings=3))
    assert not result.triggered
    assert "exclusion" in result.reason


def test_an_UNKNOWN_earnings_date_also_refuses():
    """D19's rules cannot be satisfied by ignorance."""
    result = pullback.evaluate(tech(), cata(days_to_earnings=None))
    assert not result.triggered
    assert "cannot be verified" in result.reason


def test_a_pullback_already_above_its_reclaim_does_not_fire():
    result = pullback.evaluate(tech(close=125.0), cata())
    assert not result.triggered
    assert "already at or above" in result.reason


def test_volume_that_has_not_dried_up_does_not_fire():
    result = pullback.evaluate(tech(relative_volume=1.8), cata())
    assert not result.triggered
    assert "dry-up" in result.reason


# ══════════════════════════════════════════════════════════════════════════
# Recipe 2: base breakout
# ══════════════════════════════════════════════════════════════════════════


def breakout_tech(**kw) -> TechnicalFeatures:
    """A symbol that HAS broken out: above the trigger, on volume."""
    structure = {
        "base_high": 100.0,
        "base_low": 85.0,
        "base_depth_pct": 15.0,
        "base_weeks": 8.0,
    }
    structure.update(kw.pop("structure", {}))
    base = {"close": 104.0, "relative_volume": 2.0, "structure": structure}
    base.update(kw)
    return tech(**base)


# `base_breakout` still ships DISABLED. O13's three defects are fixed — the
# mixed price basis, the base window that included the break bar, and the
# contraction statistic centred on 1.00 — but "fixed" is not "earns its place":
# the flag moves on the sweep and a certifying run, not on the repair. These
# tests exercise the logic with the flag back on;
# `test_the_shipped_config_keeps_base_breakout_dormant` pins the shipped value,
# which is the thing that must not drift silently.
ENABLED_CFG = config.ScoringConfig.model_validate(
    {
        **CFG.model_dump(),
        "recipes": {
            **CFG.recipes.model_dump(),
            "base_breakout": {**CFG.recipes.base_breakout.model_dump(), "enabled": True},
        },
    }
)


def test_the_shipped_config_keeps_base_breakout_dormant():
    """O13. Switched off by decision, so the logs say "disabled" rather than
    "fired zero times" — which look identical and are different facts."""
    assert config.scoring().recipes.base_breakout.enabled is False
    result = breakout.evaluate(breakout_tech())
    assert result.triggered is False
    assert "disabled" in result.reason


def test_a_confirmed_breakout_fires():
    result = breakout.evaluate(breakout_tech(), cfg=ENABLED_CFG)
    assert result.triggered, result.reason
    depth = 100.0 - 85.0
    assert result.plan.target_1 == pytest.approx(
        104.0 + depth * CFG.geometry.base_breakout.tp1_depth_multiple
    )


def test_a_base_too_short_does_not_fire():
    result = breakout.evaluate(breakout_tech(structure={"base_weeks": 2.0}), cfg=ENABLED_CFG)
    assert not result.triggered
    assert "week minimum" in result.reason


def test_a_base_too_deep_does_not_fire():
    """A deep base is a downtrend in a base's clothing."""
    result = breakout.evaluate(breakout_tech(structure={"base_depth_pct": 60.0}), cfg=ENABLED_CFG)
    assert not result.triggered
    assert "deep" in result.reason


def test_price_below_the_trigger_does_not_fire():
    result = breakout.evaluate(breakout_tech(close=99.0), cfg=ENABLED_CFG)
    assert not result.triggered
    assert "has not broken out" in result.reason


def test_an_unconfirmed_breakout_does_not_fire():
    """The unconfirmed break is the one that fails."""
    result = breakout.evaluate(breakout_tech(relative_volume=1.0), cfg=ENABLED_CFG)
    assert not result.triggered
    assert "volume" in result.reason


def test_a_sector_laggard_does_not_fire():
    result = breakout.evaluate(breakout_tech(rs_vs_sector=-8.0), cfg=ENABLED_CFG)
    assert not result.triggered
    assert "lags its sector" in result.reason


def test_a_missing_sector_composite_refuses_rather_than_assuming():
    result = breakout.evaluate(breakout_tech(rs_vs_sector=None), cfg=ENABLED_CFG)
    assert not result.triggered
    assert "no sector composite" in result.reason


# ══════════════════════════════════════════════════════════════════════════
# Recipe 3: post-earnings drift
# ══════════════════════════════════════════════════════════════════════════


def reaction(**kw) -> EarningsReaction:
    """A completed reaction window: 100.00 before the report, 105.00 after.

    The agnostic shape — two sessions, reference close(T-1) — because no
    provider publishes a release time, so that is what the engine actually
    gets for nearly every report.
    """
    base = {
        "reported_on": date(2026, 10, 19),
        "release_time": "unknown",
        "reference_close": 100.0,
        "end_session": date(2026, 10, 21),
        "sessions": 2,
        "reaction_pct": 5.0,
        "reaction_points": 5.0,
        "window_low": 99.0,
        "complete": True,
    }
    base.update(kw)
    return EarningsReaction(**base)


def drift_cata(**kw) -> CatalystFeatures:
    """A catalyst with a VERIFIED beat, which is what the shipped mode needs.

    Before D48 the bare fixture could leave the surprise unverified and the
    recipe still fired on the growth proxy; now an unverifiable beat is a
    refusal, so the default has to carry one.
    """
    kw.setdefault("days_since_earnings", 4)
    return beat(**kw)


def drift_tech(**kw) -> TechnicalFeatures:
    structure = {"gap_up_pct": 4.0, "gap_day_low": 99.0, "prior_close": 100.0}
    structure.update(kw.pop("structure", {}))
    base = {
        "close": 105.0,
        "structure": structure,
        "reaction": kw.pop("reaction", reaction()),
    }
    base.update(kw)
    return tech(**base)


def test_a_held_reaction_fires():
    result = drift.evaluate(drift_tech(), fund(), drift_cata())
    assert result.triggered, result.reason
    points = 5.0
    assert result.plan.target_2 == pytest.approx(
        105.0 + points * CFG.geometry.post_earnings_drift.tp2_gap_multiple
    )
    assert result.plan.stop < 99.0, "stopped below the reaction's low"


def test_an_incomplete_window_does_not_fire():
    """ "Evaluate at the close that completes the window" — a reaction judged on
    a later session would re-enter the same idea for days."""
    result = drift.evaluate(drift_tech(reaction=reaction(complete=False)), fund(), drift_cata())
    assert not result.triggered
    assert "not the latest session" in result.reason


def test_no_knowable_report_means_no_reaction_to_judge():
    """With no release time, a report dated today has not necessarily been
    released — so there is nothing to measure, rather than a zero."""
    result = drift.evaluate(drift_tech(reaction=None), fund(), drift_cata(days_since_earnings=0))
    assert not result.triggered
    assert "no measurable reaction window" in result.reason


def test_a_reaction_that_filled_is_not_a_reaction_that_held():
    """Measured to the window's CLOSE, so a gap given back reads as a small
    number instead of needing a separate hold test."""
    result = drift.evaluate(
        drift_tech(reaction=reaction(reaction_pct=0.1, reaction_points=0.1)),
        fund(),
        drift_cata(),
    )
    assert not result.triggered
    assert "filled is not a reaction that held" in result.reason


def test_a_close_on_the_reaction_low_is_a_rejection():
    result = drift.evaluate(
        drift_tech(close=99.0, reaction=reaction(window_low=99.0)),
        fund(),
        drift_cata(),
    )
    assert not result.triggered
    assert "being rejected" in result.reason


def test_a_confirmed_pre_open_report_uses_a_one_session_window():
    """The bmo tightening: reaction complete at T's close, so a plan may enter
    at T+1 instead of T+2."""
    result = drift.evaluate(
        drift_tech(reaction=reaction(release_time="bmo", sessions=1)),
        fund(),
        drift_cata(days_since_earnings=1),
    )
    assert result.triggered, result.reason
    assert "release bmo" in result.reason


def test_the_unverifiable_guidance_check_is_reported_as_failed_not_skipped():
    """`require_raised_guidance` has no data source. An unverified condition is
    not a satisfied one, and a recipe that quietly skips one of its three
    stated conditions is a different recipe."""
    result = drift.evaluate(drift_tech(), fund(), drift_cata())
    assert "raised_guidance_UNVERIFIED" in result.checks
    assert result.checks["raised_guidance_UNVERIFIED"] is False
    assert "UNVERIFIED" in result.reason


def test_the_days_since_earnings_gate_is_gone_and_the_window_is_the_gate():
    """D49 retired `max_days_since_earnings`: the recipe judges each report ONCE,
    at the close that completes its reaction window, so "is the report recent"
    is answered by `reaction.complete` rather than by a number.

    A stale report cannot produce a complete window — its end session is in the
    past — so the old gate had nothing left to refuse. This pins that the
    recipe no longer consults the day count at all.
    """
    stale = drift.evaluate(drift_tech(), fund(), drift_cata(days_since_earnings=60))
    assert stale.triggered, "the day count is no longer consulted"
    assert "recent_earnings" not in stale.checks

    incomplete = drift.evaluate(
        drift_tech(reaction=reaction(complete=False)), fund(), drift_cata(days_since_earnings=1)
    )
    assert not incomplete.triggered
    assert "not the latest session" in incomplete.reason


def test_a_window_spanning_a_trading_halt_is_refused_by_the_measurement():
    """The sanity bound that replaced the retired gate, and it lives in the
    FEATURE because it is about data integrity, not strategy."""
    import pandas as pd

    from features.technical import MAX_WINDOW_SPAN_DAYS, earnings_reaction

    days = [date(2026, 10, 19), date(2026, 10, 20), date(2026, 11, 10)]
    bars = pd.DataFrame(
        {
            "session_date": days,
            "open": [100.0, 100.0, 120.0],
            "high": [101.0, 101.0, 121.0],
            "low": [99.0, 99.0, 119.0],
            "close": [100.0, 100.0, 120.0],
            "volume": [1_000_000] * 3,
        }
    )
    assert (days[-1] - date(2026, 10, 20)).days > MAX_WINDOW_SPAN_DAYS
    assert earnings_reaction(bars, reported_on=date(2026, 10, 20)) is None


def test_the_single_bar_gap_is_no_longer_what_the_recipe_reads():
    """`structure.gap_up_pct` measures the LATEST bar's gap, which is the
    reaction only if you know the release time. The recipe reads the window
    instead, and these two tests replaced the ones that gated on the bar:
    `test_a_reaction_that_filled_is_not_a_reaction_that_held` and
    `test_a_close_on_the_reaction_low_is_a_rejection`."""
    result = drift.evaluate(
        drift_tech(structure={"gap_up_pct": 0.0, "gap_day_low": 50.0}),
        fund(),
        drift_cata(),
    )
    assert result.triggered, "the window, not the bar, decides"
    assert result.plan.stop < 99.0, "and the window's low is the stop"


def test_no_eps_growth_does_not_fire_under_the_proxy():
    """And FIRES under the shipped `verified` mode, which is the population D48
    unlocked: a company can beat the quarter while year-over-year growth is
    negative, and the proxy refused every one of those."""
    refused = drift.evaluate(drift_tech(), fund(eps_growth_yoy=-5.0), drift_cata(), cfg=PROXY_CFG)
    assert not refused.triggered
    assert "EPS growth" in refused.reason

    fires = drift.evaluate(drift_tech(), fund(eps_growth_yoy=-5.0), drift_cata())
    assert fires.triggered, "a verified beat does not care what last year did"


# ══════════════════════════════════════════════════════════════════════════
# All three together
# ══════════════════════════════════════════════════════════════════════════


def test_evaluate_all_runs_every_recipe_and_always_gives_a_reason():
    results = evaluate_all(tech(), fund(), cata())
    assert len(results) == 3
    assert {r.recipe for r in results} == {
        "pullback_to_trend",
        "base_breakout",
        "post_earnings_drift",
    }
    assert all(r.reason for r in results), "a refusal must always name a condition"


def test_recipes_take_features_not_scores():
    """Passing pillar scores in would let a mapping-scale change silently move
    a recipe's trigger."""
    import inspect

    assert "pillars" not in inspect.signature(evaluate_all).parameters
    assert "pillars" not in inspect.signature(breakout.evaluate).parameters


def test_a_range_that_is_not_coiled_does_not_fire():
    """O13's gate, on the statistic that replaced the contraction ratio: a base
    whose volatility sits mid-range in its own year is not a coiled spring, and
    the recipe's premise is the coil."""
    result = breakout.evaluate(breakout_tech(squeeze_percentile=0.62), cfg=ENABLED_CFG)
    assert not result.triggered
    assert "not coiled" in result.reason
    assert "62%" in result.reason, "the reading itself belongs in the reason"


def test_an_unmeasurable_squeeze_refuses_rather_than_assuming():
    """ "Cannot establish" is not "not coiled". A percentile needs a year of
    history, and a young listing has no reading rather than a bad one — the
    standing `features/` convention, enforced at the gate."""
    result = breakout.evaluate(breakout_tech(squeeze_percentile=None), cfg=ENABLED_CFG)
    assert not result.triggered
    assert "cannot be established" in result.reason


def test_the_squeeze_cap_is_read_from_config_not_hard_coded():
    """A reading that fires under the shipped cap must stop firing when the cap
    tightens below it, or the gate is decoration."""
    tight = config.ScoringConfig.model_validate(
        {
            **ENABLED_CFG.model_dump(),
            "recipes": {
                **ENABLED_CFG.recipes.model_dump(),
                "base_breakout": {
                    **ENABLED_CFG.recipes.base_breakout.model_dump(),
                    "max_squeeze_percentile": 0.10,
                },
            },
        }
    )
    assert breakout.evaluate(breakout_tech(squeeze_percentile=0.15), cfg=ENABLED_CFG).triggered
    assert not breakout.evaluate(breakout_tech(squeeze_percentile=0.15), cfg=tight).triggered


# ── O12: the verified beat ──────────────────────────────────────────────────


def _with_beat_source(mode: str) -> config.ScoringConfig:
    return config.ScoringConfig.model_validate(
        {
            **CFG.model_dump(),
            "recipes": {
                **CFG.recipes.model_dump(),
                "post_earnings_drift": {
                    **CFG.recipes.post_earnings_drift.model_dump(),
                    "beat_source": mode,
                },
            },
        }
    )


# `verified` is the SHIPPED mode from D48, so it is what the bare `drift.evaluate`
# calls below exercise. The proxy keeps its own config because it is still a
# supported mode and the two answer different questions.
VERIFIED_CFG = _with_beat_source("verified")
PROXY_CFG = _with_beat_source("growth_proxy")


def beat(**kw) -> CatalystFeatures:
    """A drift-window catalyst carrying a reported quarter."""
    surprise = EarningsSurprise(
        reported_on=date(2026, 10, 18),
        eps_actual=kw.pop("eps_actual", 1.20),
        eps_estimate=kw.pop("eps_estimate", 1.00),
        revenue_actual=kw.pop("revenue_actual", 1_100.0),
        revenue_estimate=kw.pop("revenue_estimate", 1_000.0),
    )
    return cata(days_since_earnings=kw.pop("days_since_earnings", 4), surprise=surprise, **kw)


def test_the_shipped_config_uses_the_verified_beat():
    """ADOPTED by D48 against the certifying run `242301c` / scoring-config
    hash `18319ad9d328`, on pre-registered criteria: the verified check won all
    three cuts (all plans +0.27R vs +0.16R, R:R>=2 +0.43 vs +0.29, R:R>=3 +0.68
    vs +0.34 on 211 closed). The mode moved on a decision, not on the way
    past."""
    assert config.scoring().recipes.post_earnings_drift.beat_source == "verified"


def test_a_verified_beat_fires():
    result = drift.evaluate(drift_tech(), fund(), beat(), cfg=VERIFIED_CFG)
    assert result.triggered, result.reason
    assert result.checks["eps_beat_verified"] is True
    assert "beat by +20.0%" in result.reason


def test_a_verified_miss_does_not_fire_even_when_growth_is_positive():
    """THE WHOLE POINT OF O12. The proxy passes this — year-over-year growth is
    positive — while the quarter actually missed, which is the opposite of the
    surprise the drift premise rests on."""
    missed = beat(eps_actual=0.90, eps_estimate=1.00)

    assert drift.evaluate(drift_tech(), fund(), missed, cfg=VERIFIED_CFG).triggered is False
    assert drift.evaluate(drift_tech(), fund(), missed, cfg=PROXY_CFG).triggered is True, (
        "the proxy passes it — which is the whole reason D48 moved off it"
    )


def test_an_unverifiable_beat_refuses_rather_than_passing():
    """`None` is "we cannot establish it", never "no beat" — the same asymmetry
    `pullback_to_trend` applies to an unknown earnings date."""
    result = drift.evaluate(drift_tech(), fund(), beat(eps_actual=None), cfg=VERIFIED_CFG)
    assert result.triggered is False
    assert "cannot be verified" in result.reason


def test_a_missing_revenue_actual_is_not_read_as_an_eps_miss():
    """Folding the two together would turn a data gap into a verdict about
    earnings — revenue actuals are missing more often than EPS."""
    result = drift.evaluate(drift_tech(), fund(), beat(revenue_actual=None), cfg=VERIFIED_CFG)
    assert result.triggered is False
    assert result.checks["eps_beat_verified"] is True, "EPS was verified and did beat"
    assert "revenue" in result.reason


def test_a_loss_making_company_beating_a_negative_estimate_scores_positive():
    """Dividing by the signed estimate would flip the sign on every loss-maker,
    which is a large share of exactly the universe where surprises are big."""
    surprise = EarningsSurprise(date(2026, 10, 18), -0.05, -0.10, 100.0, 90.0)
    assert surprise.eps_beat is True
    assert surprise.eps_surprise_pct == pytest.approx(50.0)


def test_a_symbol_with_no_reported_quarter_has_an_unverified_surprise():
    from features.catalyst import last_earnings_surprise

    surprise = last_earnings_surprise([], as_of=date(2026, 10, 21))
    assert surprise.eps_beat is None
    assert surprise.reported_on is None
    assert "unverified" in surprise.verdict


def test_the_surprise_comes_from_the_most_recent_REPORTED_quarter():
    """Not the next scheduled one — a forward row carries an estimate and no
    actual, and reading it would make every pre-announcement date unverifiable."""
    from features.catalyst import last_earnings_surprise

    events = [
        {
            "event_type": "earnings",
            "event_date": date(2026, 7, 20),
            "eps_actual": 1.0,
            "eps_estimate": 0.9,
        },
        {
            "event_type": "earnings",
            "event_date": date(2026, 10, 18),
            "eps_actual": 1.2,
            "eps_estimate": 1.1,
        },
        {
            "event_type": "earnings",
            "event_date": date(2027, 1, 20),
            "eps_actual": None,
            "eps_estimate": 1.3,
        },
    ]
    surprise = last_earnings_surprise(events, as_of=date(2026, 10, 21))
    assert surprise.reported_on == date(2026, 10, 18)
    assert surprise.eps_beat is True


# ══════════════════════════════════════════════════════════════════════════
# Recipe 1's geometry: v1 vs v2 (the stop and the targets, not the entry)
# ══════════════════════════════════════════════════════════════════════════


def _with_geometry(**kw) -> config.ScoringConfig:
    geometry = {**CFG.geometry.pullback_to_trend.model_dump(), **kw}
    return config.ScoringConfig.model_validate(
        {
            **CFG.model_dump(),
            "geometry": {**CFG.geometry.model_dump(), "pullback_to_trend": geometry},
        }
    )


V1 = _with_geometry(variant="v1")
V2 = _with_geometry(variant="v2")


def test_v1_hangs_the_stop_on_the_sixty_day_low():
    """What shipped, pinned before it is compared to anything. ATR is 2.0 and
    `stop_atr_buffer` is 0.5, so the stop is the swing low minus 1.0."""
    plan = pullback.evaluate(tech(), cata(), cfg=V1).plan
    assert plan.stop == pytest.approx(94.0 - 1.0)
    assert plan.target_1 == pytest.approx(120.0)
    assert plan.target_2 == pytest.approx(100.0 + (120.0 - 100.0) * 1.6)


def test_v2_hangs_the_stop_on_the_low_the_pullback_itself_made():
    """The 60-day low on a trending stock is a level from a different episode.
    The pullback's own low is the one the thesis actually breaks at."""
    plan = pullback.evaluate(tech(), cata(), cfg=V2).plan
    assert plan.stop == pytest.approx(97.0 - 1.0)
    assert plan.risk < pullback.evaluate(tech(), cata(), cfg=V1).plan.risk


def test_v2_projects_the_recovered_leg_beyond_the_reclaim():
    plan = pullback.evaluate(tech(), cata(), cfg=V2).plan
    assert plan.target_1 == pytest.approx(120.0)
    assert plan.target_2 == pytest.approx(120.0 + (120.0 - 97.0) * 1.0)


def test_v1s_target_moves_with_todays_close_and_v2s_does_not():
    """THE DEFECT, PINNED. v1's TP2 is anchored on the distance from TODAY'S
    CLOSE to the swing high, so a DEEPER pullback gets a FARTHER target: the
    trades carrying the most risk are handed the most ambitious target, which
    is how a recipe becomes timeout-dominated. v2's TP2 is structure the market
    made and does not know where price sits today."""
    shallow = tech(close=105.0)
    deep = tech(close=99.0)

    v1_shallow = pullback.evaluate(shallow, cata(), cfg=V1).plan
    v1_deep = pullback.evaluate(deep, cata(), cfg=V1).plan
    assert v1_deep.target_2 > v1_shallow.target_2, "deeper pullback, farther target"

    v2_shallow = pullback.evaluate(shallow, cata(), cfg=V2).plan
    v2_deep = pullback.evaluate(deep, cata(), cfg=V2).plan
    assert v2_deep.target_2 == pytest.approx(v2_shallow.target_2)


def test_v2_improves_the_r_to_r_on_the_same_structure():
    """Not a claim that v2 is better — that is the backtest's question. Only
    that the geometry change does what it says: the same setup, measured the
    same way, produces a plan whose reward is a larger multiple of its risk."""
    v1 = pullback.evaluate(tech(), cata(), cfg=V1).plan
    v2 = pullback.evaluate(tech(), cata(), cfg=V2).plan
    assert v2.risk_reward > v1.risk_reward
    assert v2.risk_reward_tp1 > v1.risk_reward_tp1


def test_v2_refuses_rather_than_falling_back_to_the_swing_low():
    """A variant that silently used v1's level would report v1's geometry under
    v2's name, and the backtest comparing them would compare nothing."""
    result = pullback.evaluate(tech(structure={"pullback_low": None}), cata(), cfg=V2)
    assert not result.triggered
    assert "will not fall back to the swing low" in result.reason


def test_the_variant_changes_nothing_about_the_entry_conditions():
    """The measured problem is the geometry, so the RSI band, the MA stack, the
    dry-up and the earnings exclusion must read identically under both."""
    for refusal in (
        tech(rsi=70.0),
        tech(ma={"stacked": False}),
        tech(relative_volume=1.8),
    ):
        first = pullback.evaluate(refusal, cata(), cfg=V1)
        second = pullback.evaluate(refusal, cata(), cfg=V2)
        assert first.reason == second.reason
        assert first.triggered == second.triggered is False


def test_the_plan_says_which_geometry_produced_it():
    """Two arms in one Shadow Book would otherwise be indistinguishable."""
    assert "[v1]" in pullback.evaluate(tech(), cata(), cfg=V1).reason
    assert "[v2]" in pullback.evaluate(tech(), cata(), cfg=V2).reason


V3 = _with_geometry(variant="v3")


def test_v3_keeps_v2s_entry_and_stop_and_trims_at_the_reclaim():
    """The explicit version of what v2 did by accident: same reclaim entry,
    same structural stop, and the reclaim becomes the TRIM rather than a
    waypoint on the way to a far target."""
    plan = pullback.evaluate(tech(), cata(), cfg=V3).plan
    v2 = pullback.evaluate(tech(), cata(), cfg=V2).plan
    assert plan.stop == pytest.approx(v2.stop)
    assert plan.entry == pytest.approx(v2.entry)
    assert plan.target_1 == pytest.approx(120.0), "the reclaim"


def test_v3_has_no_far_target_at_all():
    """No TP2 by design. A trailing plan carrying one would be two exit rules
    for one remainder, and the backtest could not say which it measured."""
    plan = pullback.evaluate(tech(), cata(), cfg=V3).plan
    assert plan.target_2 is None
    assert plan.trails is True
    assert plan.trail_ma == 20, "from exits.swing.runner_trail, which said ma_20 all along"


def test_v3_quotes_r_to_tp1_per_d12s_amendment():
    """D11 quotes R:R to TP2 because quoting to TP1 would make every plan look
    like 1:1. A trailing plan has no TP2, so it quotes to TP1 and its gate is
    1.0R — at or above which the trim plus the breakeven stop mean the trade
    cannot lose money once TP1 is hit."""
    plan = pullback.evaluate(tech(), cata(), cfg=V3).plan
    assert plan.risk_reward == pytest.approx(plan.risk_reward_tp1)
    assert plan.risk_reward == pytest.approx((120.0 - 100.0) / (100.0 - 96.0))


def test_a_trailing_plan_carries_O20s_NET_as_its_horizon_not_the_swing_clock():
    """THE DEFECT THAT DECIDED v3's FIRST CERTIFYING RUN.

    The tracker enforces `proj_horizon_days or exits.swing.max_holding_days`,
    so whatever horizon travels with the plan IS the clock. v3 went out
    carrying the recipe's own `horizon_days: [2, 15]`, which closed a third of
    its trades — 89% of them winners averaging +2.52R — before the 20-day-MA
    trail could act, and criterion (6) then failed on a design nobody had
    measured. A trailing plan must carry the swept net instead.
    """
    plan = pullback.evaluate(tech(), cata(), cfg=V3).plan
    net = V3.geometry.pullback_to_trend.trail_timeout_days
    assert plan.horizon_days[1] == net
    assert net > CFG.recipes.pullback_to_trend.horizon_days[1], (
        "a net at or below the recipe's own horizon is not a net"
    )


def test_a_FIXED_TARGET_plan_keeps_the_recipes_own_horizon():
    """The net is v3's only. v1 and v2 reach for a far target inside the
    framework's swing horizon, and lengthening their clock would change what
    they are while pretending to fix a trailing recipe."""
    for arm in (V1, V2):
        plan = pullback.evaluate(tech(), cata(), cfg=arm).plan
        assert plan.horizon_days == tuple(CFG.recipes.pullback_to_trend.horizon_days)


def test_v3_refuses_when_the_framework_says_the_runner_does_not_trail():
    """`runner_trail: none` is a legal configuration and makes v3 impossible:
    a trailing variant without a trail would leave the remainder with no exit,
    and inventing one would be a different strategy."""
    from core import config as core_config

    no_trail = core_config.RiskConfig.model_validate(
        {
            **core_config.risk().model_dump(),
            "exits": {
                **core_config.risk().exits.model_dump(),
                "swing": {
                    **core_config.risk().exits.swing.model_dump(),
                    "runner_trail": "none",
                },
            },
        }
    )
    import core.config as module

    original = module.risk
    module.risk = lambda: no_trail
    try:
        result = pullback.evaluate(tech(), cata(), cfg=V3)
    finally:
        module.risk = original
    assert not result.triggered
    assert "would have no exit at all" in result.reason


def test_an_unparseable_trail_is_rejected_by_the_loader():
    """A trail nobody can parse is a trail nobody runs — which is what
    `runner_trail` was for its first three days of existence."""
    from core import config as core_config

    with pytest.raises(Exception, match="not a rule the engine implements"):
        core_config.RiskConfig.model_validate(
            {
                **core_config.risk().model_dump(),
                "exits": {
                    **core_config.risk().exits.model_dump(),
                    "swing": {
                        **core_config.risk().exits.swing.model_dump(),
                        "runner_trail": "chandelier",
                    },
                },
            }
        )


def test_a_fixed_target_plan_still_needs_its_tp2():
    """The other direction: only a trailing plan may omit TP2."""
    from scoring.recipes.base import build_plan

    plan, why = build_plan(
        entry_reference=100.0,
        structural_stop=96.0,
        target_1=110.0,
        target_2=None,
        features=tech(),
        horizon_days=(5, 45),
        cfg=CFG,
    )
    assert plan is None
    assert "only a trailing plan may omit it" in why


def test_a_trailing_plan_must_not_also_carry_a_tp2():
    from scoring.recipes.base import build_plan

    plan, why = build_plan(
        entry_reference=100.0,
        structural_stop=96.0,
        target_1=110.0,
        target_2=130.0,
        features=tech(),
        horizon_days=(5, 45),
        cfg=CFG,
        trail_ma=20,
    )
    assert plan is None
    assert "must not also carry a TP2" in why
