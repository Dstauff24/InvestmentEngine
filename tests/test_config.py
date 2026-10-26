"""Config loader tests. Fixtures only — no network, no database.

Three jobs:

1. **Named decision tests.** Prove the SHIPPED config carries the exact numbers
   the decision log locked. Each test names its decision, so editing a value
   fails a test that tells you which decision you just contradicted (D25).
2. **Prove the loader rejects bad config.** A validator nobody has watched
   reject anything is not a validator. Each of the three layers is exercised.
3. **Keep docs/open-parameters.md in sync.** 31 rows, 31 tags, matching numbers
   AND matching parameter names.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from core import config as cfg

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
OPEN_PARAMS_DOC = REPO_ROOT / "docs" / "open-parameters.md"


def raw(filename: str) -> dict:
    return yaml.safe_load((CONFIG_DIR / filename).read_text())


def mutate(filename: str, path: list[str], value) -> dict:
    """Deep-copy the shipped config with one value replaced."""
    data = copy.deepcopy(raw(filename))
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return data


def expect_error(tmp_path: Path, filename: str, data: dict, match: str) -> None:
    (tmp_path / filename).write_text(yaml.safe_dump(data))
    with pytest.raises(cfg.ConfigError, match=match):
        cfg.load_file(filename, config_dir=tmp_path)


# ══════════════════════════════════════════════════════════════════════════
# 1. The shipped config matches the decision log
# ══════════════════════════════════════════════════════════════════════════


def test_d2_pillar_weights():
    """SUPERSEDED by D32 — see `test_d32_pillar_weights`.

    D2 set Technical 35 / Fundamental 25 / Catalyst 20 / Sentiment 20. D32
    reweighted to 40/30/20/10 on Oct 20 2026. This test is kept as a pointer
    rather than deleted, because D2 is still the decision that establishes the
    four-pillar model and a reader grepping for `D2` should land somewhere that
    tells them the weights moved, not find nothing.
    """
    w = cfg.scoring().pillar_weights
    assert (w.technical, w.fundamental, w.catalyst, w.sentiment) != (35, 25, 20, 20), (
        "the D2 weights are back in config — either D32 was reverted without "
        "updating the decision log, or this pointer is stale"
    )


def test_d32_pillar_weights():
    """D32: Technical 40 / Fundamental 30 / Catalyst 20 / Sentiment 10."""
    w = cfg.scoring().pillar_weights
    assert (w.technical, w.fundamental, w.catalyst, w.sentiment) == (40, 30, 20, 10)
    assert sum(w.as_fractions().values()) == pytest.approx(1.0)
    # The demotion is only safe while the conflict veto still bites: D32 demoted
    # Sentiment, it did not disable it. A pillar below `conflict_below` caps the
    # composite regardless of how little weight it carries.
    assert cfg.scoring().composite.conflict_below > cfg.scoring().composite.scale_min
    assert w.sentiment < w.technical


def test_d40_every_strategy_starts_in_shadow():
    """D40: new strategies always enter SHADOW. Nothing has 30 closed ideas yet.

    A backtest is uncontaminated evidence only if nobody looked at it first,
    and somebody always did — so a good backtest is not grounds for skipping a
    rung.
    """
    status = cfg.scoring().promotion.status
    assert status.pullback_to_trend == "shadow"
    assert status.base_breakout == "shadow"
    assert status.post_earnings_drift == "shadow"


def test_d40_every_configured_recipe_has_a_status():
    """A recipe producing ideas the ladder does not know about is the gap."""
    recipes = cfg.scoring().recipes
    status = cfg.scoring().promotion.status
    for name in type(recipes).model_fields:
        assert status.of(name) in ("shadow", "paper", "live")


def test_d40_an_unknown_recipe_raises_rather_than_defaulting_to_shadow():
    """Defaulting would LOOK safe and would hide the real problem."""
    with pytest.raises(cfg.ConfigError, match="no D40 promotion status"):
        cfg.scoring().promotion.status.of("some_new_recipe")


def test_d40_the_sample_floor_is_the_d39_floor():
    """D40 and D39(2) both say 30; they must not drift apart."""
    p = cfg.scoring().promotion
    assert p.shadow_to_paper.min_closed_ideas == 30
    assert p.paper_to_live.min_closed_ideas == 30
    assert cfg.scoring().sentiment_reinstatement.min_sample == 30


def test_d40_demotion_is_quicker_than_promotion():
    """Removing a broken strategy should not be slower than adding a good one."""
    p = cfg.scoring().promotion
    assert p.demotion.window_days <= p.paper_to_live.sustained_window_days
    assert p.demotion.min_closed_ideas <= p.shadow_to_paper.min_closed_ideas


def test_d40_a_live_bar_below_the_paper_bar_is_rejected(tmp_path):
    expect_error(
        tmp_path,
        "scoring.yaml",
        mutate("scoring.yaml", ["promotion", "paper_to_live", "min_expectancy_r"], 0.05),
        "must demand at least as much",
    )


def test_d40_a_demotion_threshold_that_would_oscillate_is_rejected(tmp_path):
    """Demote-above-promote means a strategy is demoted as it is promoted."""
    expect_error(
        tmp_path,
        "scoring.yaml",
        mutate("scoring.yaml", ["promotion", "demotion", "expectancy_r_below"], 0.50),
        "oscillating for ever",
    )


def test_d40_a_demotion_sample_floor_above_the_promotion_floor_is_rejected(tmp_path):
    """Otherwise a bad run could never remove what a good run added."""
    expect_error(
        tmp_path,
        "scoring.yaml",
        mutate("scoring.yaml", ["promotion", "demotion", "min_closed_ideas"], 99),
        "could be promoted on evidence",
    )


def test_d40_an_invalid_status_is_rejected(tmp_path):
    expect_error(
        tmp_path,
        "scoring.yaml",
        mutate("scoring.yaml", ["promotion", "status", "base_breakout"], "actionable"),
        "base_breakout",
    )


def test_d33_cross_sectional_ranking():
    """D33: rank by percentile within the universe; cap the digest by rank."""
    r = cfg.scoring().ranking
    assert r.rank_within_universe is True
    assert r.max_ideas_per_day == 5  # [OPEN] #32
    # Ranking selects AMONG ideas that already cleared a D12 gate; it must not
    # become a second, looser route to surfacing.
    assert cfg.scoring().min_surfaceable_composite >= cfg.scoring().tiers.b.min_composite - 1e-9


def test_d35_barbell_sleeves_and_ratchet():
    """D35: Core 65 / Growth 35, separate benchmarks, one-way ratchet."""
    s = cfg.risk().sleeves
    assert (s.core.target_pct, s.growth.target_pct) == (65, 35)
    assert s.core.benchmark == "blend"
    assert s.growth.benchmark == "spy_total_return"
    assert set(s.core.buckets) == {"etf", "hold"}
    assert set(s.growth.buckets) == {"swing", "option", "cash"}
    # The asymmetry IS the decision: gains sweep out permanently, drawdowns are
    # never topped back up mid-quarter.
    assert s.ratchet.permanent is True
    assert s.ratchet.replenish_drawdowns is False
    assert s.ratchet.trigger_excess_points == 3.0  # [OPEN] #35
    assert s.ratchet.sweep_fraction == 0.5  # [OPEN] #36


def test_d36_iv_rank_selects_the_vehicle():
    """D36: three bands, always defined risk, always event-checked."""
    v = cfg.risk().vehicle_by_iv_rank
    assert v.enabled is True
    assert v.iv_vehicle_low == 30  # [OPEN] #33
    assert v.iv_vehicle_high == 50  # [OPEN] #34
    assert v.iv_vehicle_low < v.iv_vehicle_high, "the shares-by-default band must exist"
    assert v.defined_risk_only is True  # D17
    assert v.check_binary_events_in_expiration is True  # D19


def test_d37_pyramiding_never_raises_initial_risk():
    """D37: adds are funded by locked-in safety, not by raising the ante."""
    p = cfg.risk().pyramiding
    assert p.require_stop_at_breakeven is True
    assert p.cap_at_original_allocation is True


def test_d38_sector_correlation_guard():
    """D38: two swings per sector; the third surfaces watch-only."""
    g = cfg.risk().sector_guard
    assert g.max_concurrent_swings_per_sector == 2
    # Flagged, not suppressed: that the sector is working is information worth
    # keeping even when the concentration is refused.
    assert g.surface_excess_as_watch_only is True
    # D38 must actually bind before the D17 swing cap does, or it is decorative.
    assert g.max_concurrent_swings_per_sector < cfg.risk().max_concurrent_swings


def test_d32_sentiment_reinstatement_condition():
    """D32: the route back to weight 20, recorded as a test not a mood."""
    s = cfg.scoring().sentiment_reinstatement
    assert s.min_sentiment_score == 60
    assert s.min_sample == 30  # [OPEN] #37
    assert s.restore_toward == 20
    # D39(2) sets the same floor for any weight change; the two must not drift.
    assert s.min_sample >= 30


def test_d12_tier_gates():
    """D12: Tier A >=3 pillars and composite >=70; Tier B 2 pillars or 55-69."""
    tiers = cfg.scoring().tiers
    assert tiers.a.min_pillars_agreeing == 3
    assert tiers.a.min_composite == 70
    assert tiers.b.min_pillars_agreeing == 2
    assert tiers.b.min_composite == 55
    # max_composite is an EXCLUSIVE upper bound, so D12's written "55-69" band
    # is 55 <= composite < 70. Encoded exclusively so no fractional composite
    # falls into a dead zone between the tiers.
    assert tiers.b.max_composite == 70


def test_d17_minimum_risk_reward():
    """D17: min R:R 2:1 for Tier A, 3:1 for Tier B."""
    assert cfg.scoring().tiers.a.min_risk_reward == 2.0
    assert cfg.scoring().tiers.b.min_risk_reward == 3.0
    assert cfg.risk().min_risk_reward.tier_a == 2.0
    assert cfg.risk().min_risk_reward.tier_b == 3.0


def test_d17_per_trade_risk():
    """D17: Tier A 2.5% per trade; Tier B 0.75% per idea."""
    assert cfg.risk().per_trade_risk_pct.tier_a == 2.5
    assert cfg.risk().per_trade_risk_pct.tier_b == 0.75


def test_d17_heat_and_concurrency():
    """D17: max 6 concurrent swings; total open heat <=10%, active trades only."""
    r = cfg.risk()
    assert r.max_concurrent_swings == 6
    assert r.heat.max_open_heat_pct == 10.0
    assert set(r.heat.counted_buckets) == {"swing", "option"}
    assert r.stable_buckets.excluded_from_heat is True


def test_d17_options_budget():
    """D17: defined-risk only, 5%/mo realised-loss budget, 3% max per position."""
    o = cfg.risk().options
    assert o.defined_risk_only is True
    assert o.monthly_realized_loss_budget_pct == 5.0
    assert o.max_risk_per_position_pct == 3.0


def test_d17_circuit_breaker():
    """D17: 2 losing weeks or -5% month -> Tier B paused, Tier A halved."""
    cb = cfg.risk().circuit_breaker
    assert cb.consecutive_losing_weeks == 2
    assert cb.monthly_drawdown_pct == -5.0
    assert cb.on_trip.pause_tier_b is True
    assert cb.on_trip.tier_a_size_multiplier == 0.5
    assert cb.resets_after == "winning_week"


def test_d17_concentration_and_thesis_triggers():
    """D17: single name <=10%, sector <=30%, review at ~-15%, no hard stops."""
    s = cfg.risk().stable_buckets
    assert s.max_single_name_pct == 10.0
    assert s.max_sector_pct == 30.0
    assert s.thesis_review_drawdown_pct == -15.0
    assert s.use_hard_stops is False


def test_d17_bucket_targets_and_risk_off():
    """D17: ETF 35 / holds 30 / swings 20 / options 10 / cash 5;
    risk-off (SPY<200MA + VIX>25) -> swings 10, options 5, cash 20+."""
    a = cfg.risk().allocation_targets
    assert (a.normal.etf, a.normal.hold, a.normal.swing, a.normal.option, a.normal.cash) == (
        35,
        30,
        20,
        10,
        5,
    )
    assert a.risk_off.swing == 10
    assert a.risk_off.option == 5
    assert a.risk_off.cash >= 20
    assert a.risk_off_spy_below_ma == 200
    assert a.risk_off_vix_above == 25


def test_d17_percentages_are_off_current_account_value():
    """D17: '% computed off current account value (self-throttling)'."""
    assert cfg.risk().percent_basis == "current_account_value"


def test_d18_tier_b_hybrid_budget():
    """D18: 4% at-entry base, 50% gain replenish, 6% ceiling, calendar month."""
    b = cfg.risk().tier_b_budget
    assert b.base_monthly_pct == 4.0
    assert b.gain_replenish_rate == 0.5
    assert b.hard_ceiling_pct == 6.0
    assert b.measured_as == "risk_at_entry"
    assert b.resets_on == "calendar_month"


def test_d18_heat_releases_continuously():
    """D18: heat resets on exit and drops to ~0 once the stop is at breakeven."""
    h = cfg.risk().heat
    assert h.release_on_exit is True
    assert h.zero_risk_after_stop_at_breakeven is True
    assert cfg.risk().exits.swing.move_stop_to_breakeven_at == "target_1"


def test_d19_dividend_rules():
    """D19: total return, dividend-adjusted stops, no new swing long near ex-date."""
    d = cfg.risk().dividends
    assert d.total_return_everywhere is True
    assert d.dividend_adjusted_stops is True
    assert d.no_new_swing_long_within_days_of_ex == 2
    assert d.core_etf_drip is True
    assert d.hold_dividends_to_cash_bucket is True
    assert cfg.risk().stable_buckets.dividend_cut_triggers_review is True


def test_d19_assignment_check_is_enabled():
    """D19(4): flag short-call assignment risk near an ex-date."""
    assert cfg.risk().options.assignment_risk_check is True


def test_d11_exit_structure_leaves_a_runner():
    """D11: partials protect breakeven-or-better; the runner makes the true win."""
    e = cfg.risk().exits.swing
    assert e.trim_at_target_1 + e.trim_at_target_2 < 1.0
    assert e.runner_trail == "ma_20"


def test_d14_universe_and_liquidity():
    """D14: S&P 500 + Nasdaq 100, avg volume >1M over 20 days, options required."""
    u = cfg.universe()
    assert u.sources.sp500 is True
    assert u.sources.nasdaq100 is True
    assert u.liquidity.min_avg_volume == 1_000_000
    assert u.liquidity.avg_volume_lookback_days == 20
    assert u.liquidity.require_options is True
    assert u.sources.watchlist_file == "config/watchlist.txt"


def test_d22_chain_snapshot_scope():
    """D22: strikes within +/-15% of spot x nearest 4 MONTHLY expirations + agg IV."""
    c = cfg.universe().option_chains
    assert c.strike_range_pct == 15
    assert c.monthly_expirations == 4
    assert c.monthly_only is True
    assert c.aggregate_iv is True
    assert c.include_calls is True
    assert c.include_puts is True


def test_d7_iv_rank_needs_history_first():
    """D7: IV rank only becomes real after ~60-90 days of our own snapshots."""
    c = cfg.universe().option_chains
    assert c.iv_rank_min_history_days == 60
    assert c.iv_rank_lookback_days == 252


def test_d29_static_fallback_survives_the_premium_upgrade():
    """D29 free-tier MODE retired 2026-10-21; the fallback it built remains.

    The constituents endpoints work again, so the static file is no longer the
    normal path — but it stays configured and tested. A plan can lapse, a
    billing failure can 402 an endpoint overnight, and the fallback is the
    difference between a stale universe and no universe. The call budget's
    assertion moved to `test_premium_call_budget`, because 250 was a free-tier
    number and is no longer a D29 mitigation.
    """
    u = cfg.universe()
    assert u.sources.allow_static_fallback is True
    assert u.sources.static_fallback_file == "config/constituents_static.txt"


def test_premium_call_budget():
    """Premium from 2026-10-21. Sized from measurement, not taste.

    A cold start costs 6,759 calls: universe 527 + estimates 1,039 +
    fundamentals 5,190 + events 3. The budget must clear that in ONE day or a
    first run can never complete, and it keeps a reserve for the event radar.
    Raised to 12,000 for O7's one-time history backfill, which adds ~1,321
    (actions 866 + bars 434 + earnings windows 21) on top of a cold start.
    """
    f = cfg.universe().fmp
    assert f.daily_call_budget == 12000  # [OPEN] #31
    assert f.daily_call_budget > 6759 + 1321, (
        "a cold start plus the one-time history backfill could not complete in one day"
    )
    assert f.reserve_for_events == 3


def test_recipe_parameters_from_the_decision_log():
    """The three Phase 1 recipes carry the parameters the log specifies."""
    r = cfg.scoring().recipes
    p = r.pullback_to_trend
    assert (p.ma_fast, p.ma_mid, p.ma_slow) == (20, 50, 200)
    assert (p.rsi_min, p.rsi_max) == (40, 50)
    assert p.no_earnings_within_days == 5
    assert r.base_breakout.min_relative_volume_on_break == 1.5
    assert r.post_earnings_drift.require_eps_beat
    assert r.post_earnings_drift.require_revenue_beat
    assert r.post_earnings_drift.require_raised_guidance


def test_d25_approved_open_values():
    """D25 names these approvals explicitly, including the three Sep 1 changes."""
    s, r, u = cfg.scoring(), cfg.risk(), cfg.universe()
    assert s.pillar_agreement.min_score == 60
    assert s.composite.conflict_below == 30
    assert s.composite.capped_at == 69
    assert s.recipes.base_breakout.min_base_weeks == 4
    # D25 approved 2.0% on Sep 1; **D46 superseded it with 0.57%** on Oct 22
    # from the D43 sweep — 231 signals at +0.26R against 96 at +0.16R, more
    # trades AND better ones. Asserted here because an [OPEN] value a decision
    # has since MOVED must not drift back silently either.
    assert s.recipes.post_earnings_drift.min_gap_up_pct == 0.57
    assert u.liquidity.min_price == 5.00
    assert r.exits.swing.max_holding_days == 45
    # The three Sep 1 adjustments (O9).
    # `max_days_since_earnings` was 10 until D49 RETIRED it; see
    # `test_the_retired_drift_window_parameter_is_gone`.
    assert r.options.assignment_extrinsic_buffer == 1.25
    assert u.maintenance.deactivate_after_absent_days == 5


# ══════════════════════════════════════════════════════════════════════════
# 2a. Layer 1 — unknown keys fail loudly
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("filename", ["scoring.yaml", "risk.yaml", "universe.yaml"])
def test_unknown_key_is_rejected(tmp_path, filename):
    data = raw(filename)
    data["definitely_not_a_real_key"] = 1
    expect_error(tmp_path, filename, data, "unknown key")


def test_misspelled_pillar_is_rejected_not_defaulted(tmp_path):
    """The failure this whole module exists to prevent.

    `techncial: 35` would otherwise leave technical unset, and a model with a
    default would silently score every idea on a broken weighting.
    """
    data = raw("scoring.yaml")
    data["pillar_weights"]["techncial"] = data["pillar_weights"].pop("technical")
    (tmp_path / "scoring.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(cfg.ConfigError) as excinfo:
        cfg.load_file("scoring.yaml", config_dir=tmp_path)
    message = str(excinfo.value)
    assert "techncial" in message  # the typo is named
    assert "technical" in message  # so is the field it failed to set


def test_missing_required_key_is_rejected(tmp_path):
    data = raw("risk.yaml")
    del data["per_trade_risk_pct"]
    expect_error(tmp_path, "risk.yaml", data, "per_trade_risk_pct")


def test_missing_file_names_the_path(tmp_path):
    with pytest.raises(cfg.ConfigError, match="missing config file"):
        cfg.load_file("risk.yaml", config_dir=tmp_path)


def test_empty_file_is_rejected(tmp_path):
    (tmp_path / "risk.yaml").write_text("")
    with pytest.raises(cfg.ConfigError, match="is empty"):
        cfg.load_file("risk.yaml", config_dir=tmp_path)


def test_malformed_yaml_is_rejected(tmp_path):
    (tmp_path / "risk.yaml").write_text("per_trade_risk_pct: [1, 2\n")
    with pytest.raises(cfg.ConfigError, match="not valid YAML"):
        cfg.load_file("risk.yaml", config_dir=tmp_path)


def test_non_mapping_top_level_is_rejected(tmp_path):
    (tmp_path / "risk.yaml").write_text("- just\n- a\n- list\n")
    with pytest.raises(cfg.ConfigError, match="must be a mapping"):
        cfg.load_file("risk.yaml", config_dir=tmp_path)


# ══════════════════════════════════════════════════════════════════════════
# 2b. Layer 2 — types and bounds
# ══════════════════════════════════════════════════════════════════════════


def test_string_where_a_number_belongs_is_rejected(tmp_path):
    expect_error(
        tmp_path,
        "risk.yaml",
        mutate("risk.yaml", ["per_trade_risk_pct", "tier_a"], "two"),
        "tier_a",
    )


def test_negative_risk_is_rejected(tmp_path):
    expect_error(
        tmp_path, "risk.yaml", mutate("risk.yaml", ["per_trade_risk_pct", "tier_a"], -2.5), "tier_a"
    )


def test_risk_above_100_percent_is_rejected(tmp_path):
    expect_error(
        tmp_path, "risk.yaml", mutate("risk.yaml", ["per_trade_risk_pct", "tier_a"], 250), "tier_a"
    )


def test_unknown_enum_value_is_rejected(tmp_path):
    expect_error(
        tmp_path, "risk.yaml", mutate("risk.yaml", ["percent_basis"], "vibes"), "percent_basis"
    )


def test_unknown_bucket_name_is_rejected(tmp_path):
    expect_error(
        tmp_path,
        "risk.yaml",
        mutate("risk.yaml", ["heat", "counted_buckets"], ["swing", "crypto"]),
        "counted_buckets",
    )


def test_a_positive_drawdown_is_rejected(tmp_path):
    """-5% means a loss. A positive value would never trip the breaker."""
    expect_error(
        tmp_path,
        "risk.yaml",
        mutate("risk.yaml", ["circuit_breaker", "monthly_drawdown_pct"], 5.0),
        "monthly_drawdown_pct",
    )


def test_a_zero_expiration_count_is_rejected(tmp_path):
    expect_error(
        tmp_path,
        "universe.yaml",
        mutate("universe.yaml", ["option_chains", "monthly_expirations"], 0),
        "monthly_expirations",
    )


# ══════════════════════════════════════════════════════════════════════════
# 2c. Layer 3 — cross-field invariants
# individually well-formed, collectively wrong
# ══════════════════════════════════════════════════════════════════════════


def test_pillar_weights_that_do_not_total_100_are_rejected(tmp_path):
    expect_error(
        tmp_path,
        "scoring.yaml",
        # D32 weights: technical is 40, so 30 leaves 90.
        mutate("scoring.yaml", ["pillar_weights", "technical"], 30),
        "must total 100, got 90",
    )


def test_tier_b_cannot_demand_a_lower_risk_reward_than_tier_a(tmp_path):
    expect_error(
        tmp_path,
        "scoring.yaml",
        mutate("scoring.yaml", ["tiers", "b", "min_risk_reward"], 1.5),
        "must be at least",
    )


def test_overlapping_tier_bands_are_rejected(tmp_path):
    expect_error(
        tmp_path,
        "scoring.yaml",
        mutate("scoring.yaml", ["tiers", "b", "max_composite"], 80),
        "bands overlap",
    )


def test_empty_tier_b_band_is_rejected(tmp_path):
    expect_error(
        tmp_path,
        "scoring.yaml",
        mutate("scoring.yaml", ["tiers", "b", "min_composite"], 75),
        "must be below",
    )


def test_unreachable_tier_b_surfacing_threshold_is_rejected(tmp_path):
    expect_error(
        tmp_path,
        "scoring.yaml",
        mutate("scoring.yaml", ["min_surfaceable_composite"], 65),
        "could ever surface",
    )


def test_a_conflict_cap_that_still_reaches_tier_a_is_rejected(tmp_path):
    """capped_at >= 70 would let a conflicted idea take full Tier A size."""
    expect_error(
        tmp_path,
        "scoring.yaml",
        mutate("scoring.yaml", ["composite", "capped_at"], 75),
        "could still reach Tier A",
    )


def test_a_pillar_threshold_above_the_composite_gate_is_rejected(tmp_path):
    expect_error(
        tmp_path,
        "scoring.yaml",
        mutate("scoring.yaml", ["pillar_agreement", "min_score"], 85),
        "clear the whole composite gate",
    )


def test_ma_stack_out_of_order_is_rejected(tmp_path):
    expect_error(
        tmp_path,
        "scoring.yaml",
        mutate("scoring.yaml", ["recipes", "pullback_to_trend", "ma_fast"], 100),
        "ma_fast < ma_mid < ma_slow",
    )


def test_pullback_to_an_ma_the_recipe_never_computes_is_rejected(tmp_path):
    expect_error(
        tmp_path,
        "scoring.yaml",
        mutate("scoring.yaml", ["recipes", "pullback_to_trend", "pullback_to"], [20, 34]),
        "references an MA",
    )


def test_a_squeeze_cap_that_admits_everything_is_rejected(tmp_path):
    """A percentile cap at or above 1 admits every observation, so it is not a
    squeeze — the same shape of refusal the retired contraction ratio had, on
    the statistic that replaced it."""
    expect_error(
        tmp_path,
        "scoring.yaml",
        mutate("scoring.yaml", ["recipes", "base_breakout", "max_squeeze_percentile"], 1.2),
        "between 0 and 1",
    )


def test_the_retired_contraction_parameter_is_gone(tmp_path):
    """O13 RETIRED `range_contraction_ratio`, and a retired parameter must be
    REJECTED rather than ignored.

    Same reason as the drift window below: a config still carrying the old key
    would load silently while nothing reads it, which is how an operator comes
    to believe a gate is in force when it is not. And this one would be
    especially quiet — the parameter it replaces gated a measurement that no
    longer exists, so a revived key could never fire anything.
    """
    assert "range_contraction_ratio" not in cfg.BreakoutRecipe.model_fields

    revived = copy.deepcopy(raw("scoring.yaml"))
    revived["recipes"]["base_breakout"]["range_contraction_ratio"] = 0.6
    expect_error(tmp_path, "scoring.yaml", revived, "unknown key")


def test_the_retired_drift_window_parameter_is_gone(tmp_path):
    """D49 RETIRED `max_days_since_earnings`, and a retired parameter must be
    REJECTED rather than ignored.

    `extra="forbid"` on the config models is what makes this a hard error: a
    config still carrying the old key would otherwise load silently while the
    recipe no longer reads it, which is the quietest possible way for an
    operator to believe a gate is in force when it is not.
    """
    assert "max_days_since_earnings" not in cfg.DriftRecipe.model_fields

    revived = copy.deepcopy(raw("scoring.yaml"))
    revived["recipes"]["post_earnings_drift"]["max_days_since_earnings"] = 10
    expect_error(tmp_path, "scoring.yaml", revived, "unknown key")


def test_tier_b_risk_above_tier_a_is_rejected(tmp_path):
    """D12: reduced size IS the safety valve for Tier B."""
    expect_error(
        tmp_path,
        "risk.yaml",
        mutate("risk.yaml", ["per_trade_risk_pct", "tier_b"], 3.0),
        "must be below tier_a",
    )


def test_heat_counting_stable_buckets_is_rejected(tmp_path):
    """D17: holds and ETFs are explicitly excluded from the heat cap."""
    expect_error(
        tmp_path,
        "risk.yaml",
        mutate("risk.yaml", ["heat", "counted_buckets"], ["swing", "option", "hold"]),
        "ACTIVE book only",
    )


def test_a_heat_cap_that_can_never_bind_is_rejected(tmp_path):
    """REQUIRED INVARIANT: max_concurrent_swings x tier_a must EXCEED the cap.

    6 swings x 2.5% = 15% against a 10% cap, so the cap genuinely binds. Drop
    Tier A to 1% and six swings total 6% — the cap becomes decorative and one of
    the two numbers must be wrong.
    """
    expect_error(
        tmp_path,
        "risk.yaml",
        mutate("risk.yaml", ["per_trade_risk_pct", "tier_a"], 1.0),
        "could never bind",
    )


def test_the_shipped_config_lets_the_heat_cap_bind():
    r = cfg.risk()
    assert r.max_concurrent_swings * r.per_trade_risk_pct.tier_a > r.heat.max_open_heat_pct


def test_fewer_swings_than_the_cap_allows_is_also_rejected(tmp_path):
    """The same invariant from the other side."""
    expect_error(
        tmp_path, "risk.yaml", mutate("risk.yaml", ["max_concurrent_swings"], 3), "could never bind"
    )


def test_tier_b_ceiling_below_its_base_budget_is_rejected(tmp_path):
    expect_error(
        tmp_path,
        "risk.yaml",
        mutate("risk.yaml", ["tier_b_budget", "hard_ceiling_pct"], 3.0),
        "is below",
    )


def test_disabling_defined_risk_only_is_rejected(tmp_path):
    """D17 permits defined-risk option structures only. Not a preference."""
    expect_error(
        tmp_path,
        "risk.yaml",
        mutate("risk.yaml", ["options", "defined_risk_only"], False),
        "cannot be false",
    )


def test_position_risk_above_the_monthly_option_budget_is_rejected(tmp_path):
    expect_error(
        tmp_path,
        "risk.yaml",
        mutate("risk.yaml", ["options", "max_risk_per_position_pct"], 8.0),
        "exceeds the monthly loss budget",
    )


def test_bucket_targets_that_do_not_total_100_are_rejected(tmp_path):
    expect_error(
        tmp_path,
        "risk.yaml",
        mutate("risk.yaml", ["allocation_targets", "normal", "cash"], 10),
        "must total 100",
    )


def test_a_risk_off_regime_that_increases_the_active_book_is_rejected(tmp_path):
    data = copy.deepcopy(raw("risk.yaml"))
    data["allocation_targets"]["risk_off"]["swing"] = 30
    data["allocation_targets"]["risk_off"]["cash"] = 0
    expect_error(tmp_path, "risk.yaml", data, "reduces the active book")


def test_trims_that_leave_no_runner_are_rejected(tmp_path):
    """D11: the runner is what makes the true win possible."""
    data = copy.deepcopy(raw("risk.yaml"))
    data["exits"]["swing"]["trim_at_target_1"] = 0.5
    data["exits"]["swing"]["trim_at_target_2"] = 0.5
    expect_error(tmp_path, "risk.yaml", data, "leaving no runner")


def test_hard_stops_on_stable_buckets_are_rejected(tmp_path):
    expect_error(
        tmp_path,
        "risk.yaml",
        mutate("risk.yaml", ["stable_buckets", "use_hard_stops"], True),
        "thesis-invalidation triggers, not stops",
    )


def test_disabling_total_return_is_rejected(tmp_path):
    expect_error(
        tmp_path,
        "risk.yaml",
        mutate("risk.yaml", ["dividends", "total_return_everywhere"], False),
        "cannot be false",
    )


# ── per-strategy exits shapes ───────────────────────────────────────────────


def test_the_holding_period_timeout_applies_to_swings_only():
    """Options are bounded by expiration; long holds have no clock at all (D17)."""
    e = cfg.risk().exits
    assert e.swing.max_holding_days == 45
    assert e.option.timeout == "expiration"
    assert e.option.max_holding_days is None
    assert e.hold.timeout == "thesis_trigger"
    assert e.hold.max_holding_days is None


def test_a_day_count_timeout_on_options_is_a_hard_error(tmp_path):
    """A second clock alongside expiration would mislabel Shadow Book outcomes."""
    expect_error(
        tmp_path,
        "risk.yaml",
        mutate("risk.yaml", ["exits", "option", "max_holding_days"], 45),
        "bounded by its own expiration",
    )


def test_a_day_count_timeout_on_holds_is_a_hard_error(tmp_path):
    """D17: long holds exit on thesis invalidation, never on a timer."""
    expect_error(
        tmp_path,
        "risk.yaml",
        mutate("risk.yaml", ["exits", "hold", "max_holding_days"], 180),
        "not a clock",
    )


def test_swing_exits_still_require_a_holding_period(tmp_path):
    """Without it a swing could never be labelled `timeout` in the Shadow Book."""
    data = copy.deepcopy(raw("risk.yaml"))
    del data["exits"]["swing"]["max_holding_days"]
    expect_error(tmp_path, "risk.yaml", data, "max_holding_days")


def test_an_option_timeout_other_than_expiration_is_rejected(tmp_path):
    expect_error(
        tmp_path, "risk.yaml", mutate("risk.yaml", ["exits", "option", "timeout"], "45d"), "timeout"
    )


def test_early_exit_dte_is_declared_for_phase_3():
    """[OPEN] #17: validated so a typo fails at startup, but nothing reads it yet."""
    assert cfg.risk().exits.option.early_exit_dte == 21


# ── universe invariants ─────────────────────────────────────────────────────


def test_a_universe_with_no_index_source_is_rejected(tmp_path):
    data = copy.deepcopy(raw("universe.yaml"))
    data["sources"]["sp500"] = False
    data["sources"]["nasdaq100"] = False
    expect_error(tmp_path, "universe.yaml", data, "neither sp500 nor nasdaq100")


def test_a_chain_scope_without_puts_is_rejected(tmp_path):
    """Skew is a put-vs-call difference; calls alone cannot produce it."""
    expect_error(
        tmp_path,
        "universe.yaml",
        mutate("universe.yaml", ["option_chains", "include_puts"], False),
        "cannot be computed from calls alone",
    )


def test_an_iv_rank_that_could_never_compute_is_rejected(tmp_path):
    expect_error(
        tmp_path,
        "universe.yaml",
        mutate("universe.yaml", ["option_chains", "iv_rank_min_history_days"], 500),
        "never become computable",
    )


def test_an_event_radar_shorter_than_the_longest_horizon_is_rejected(tmp_path):
    """The engine must not be able to open a position blind to a nearby event."""
    expect_error(
        tmp_path,
        "universe.yaml",
        mutate("universe.yaml", ["events", "forward_days"], 30),
        "longest recipe horizon",
    )


def test_a_call_budget_too_small_for_one_symbol_is_rejected(tmp_path):
    """D29: fundamentals cost 5 calls/symbol; below that nothing could refresh.

    4 rather than 3, so this trips the budget-floor rule rather than the
    reserve rule (3 would collide with reserve_for_events=3 first).
    """
    expect_error(
        tmp_path,
        "universe.yaml",
        mutate("universe.yaml", ["fmp", "daily_call_budget"], 4),
        "no symbol could ever be refreshed",
    )


def test_the_events_reserve_matches_the_number_of_calendar_endpoints():
    """Not an [OPEN] guess: it is exactly the calendars the events job calls.

    If a fourth market-wide calendar is added, this fails and the reservation
    has to move with it — otherwise the event radar silently loses a call to a
    fundamentals crawl that ran earlier in the evening.
    """
    import inspect

    from ingest import fmp_client

    calendars = [
        name
        for name, _ in inspect.getmembers(fmp_client.FMPClient, inspect.isfunction)
        # Public methods only: `_calendar` is the shared transport they all go
        # through (it is what raises on a truncated response), not a fourth
        # endpoint, and counting it would inflate the reserve.
        if name.endswith("_calendar") and not name.startswith("_")
    ]
    assert sorted(calendars) == ["dividend_calendar", "earnings_calendar", "split_calendar"]
    assert cfg.universe().fmp.reserve_for_events == len(calendars) == 3


def test_a_reserve_that_consumes_the_whole_budget_is_rejected(tmp_path):
    expect_error(
        tmp_path,
        "universe.yaml",
        # Equal to the budget: the reserve would leave nothing in the pool.
        mutate("universe.yaml", ["fmp", "reserve_for_events"], 12000),
        "must be below",
    )


def test_a_reserve_above_the_budget_is_rejected(tmp_path):
    expect_error(
        tmp_path,
        "universe.yaml",
        mutate("universe.yaml", ["fmp", "reserve_for_events"], 12500),
        "must be below",
    )


def test_a_zero_reserve_is_allowed(tmp_path):
    """On a paid plan the reservation stops mattering; zero must stay legal."""
    data = mutate("universe.yaml", ["fmp", "reserve_for_events"], 0)
    (tmp_path / "universe.yaml").write_text(yaml.safe_dump(data))
    loaded = cfg.load_file("universe.yaml", config_dir=tmp_path)
    assert loaded.fmp.reserve_for_events == 0


def test_empty_estimate_period_types_are_rejected(tmp_path):
    expect_error(
        tmp_path,
        "universe.yaml",
        mutate("universe.yaml", ["estimates", "period_types"], []),
        "period_types",
    )


# ══════════════════════════════════════════════════════════════════════════
# 3. docs/open-parameters.md stays in sync
# ══════════════════════════════════════════════════════════════════════════
# A catalogue of provisional values is only useful while it is complete. Tag a
# new value and forget the doc, and the next reader assumes an untested guess
# was a decision.

# `[a-z0-9_]`, not `[a-z_]`. The first version could not match a parameter
# name containing a DIGIT, so `tp1_depth_multiple` and `tp2_gap_multiple` were
# invisible to this scanner — they could carry an [OPEN] tag with no doc row
# and the sync test would have reported everything in agreement. Found when
# five geometry tags silently failed to appear in the count.
TAG = re.compile(r"^\s*([a-z0-9_]+):.*#\s*\[OPEN\]\s*#(\d+)")


def tagged_parameters() -> dict[int, str]:
    """{doc row number: leaf parameter name} for every `# [OPEN] #N` tag.

    The tag must sit on its parameter's own line, which is what makes the
    association unambiguous.
    """
    found: dict[int, str] = {}
    for path in sorted(CONFIG_DIR.glob("*.yaml")):
        for line in path.read_text().splitlines():
            match = TAG.match(line)
            if match:
                name, number = match.group(1), int(match.group(2))
                assert number not in found, f"duplicate [OPEN] #{number}"
                found[number] = name
    return found


def documented_parameters() -> dict[int, str]:
    """{row number: leaf parameter name} from the doc's tables."""
    doc = OPEN_PARAMS_DOC.read_text()
    return {
        int(m.group(1)): m.group(2).rsplit(".", 1)[-1]
        for m in re.finditer(r"^\|\s*(\d+)\s*\|\s*`([^`]+)`\s*\|", doc, re.MULTILINE)
    }


def retired_parameters() -> set[int]:
    """Row numbers whose parameter is GONE, kept as tombstones.

    The number is never reused and never renumbered: `[OPEN] #N` is cited by
    number in CLAUDE.md and the decision log, so renumbering would make every
    historical reference wrong. A tombstone names the parameter in plain text
    rather than backticks, which is what keeps it out of
    `documented_parameters`.
    """
    doc = OPEN_PARAMS_DOC.read_text()
    return {int(m.group(1)) for m in re.finditer(r"^\|\s*(\d+)\s*\|\s*RETIRED", doc, re.MULTILINE)}


def test_the_doc_lists_exactly_84_live_parameters_and_two_tombstones():
    assert len(documented_parameters()) == 84
    # #12 retired by D49 (max_days_since_earnings), #8 by O13's redesign
    # (range_contraction_ratio). Numbers are never reused.
    assert retired_parameters() == {8, 12}


def test_every_tagged_value_is_documented():
    tagged, documented = tagged_parameters(), documented_parameters()
    assert set(tagged) == set(documented), (
        f"row numbers disagree — only in config: {sorted(set(tagged) - set(documented))}, "
        f"only in the doc: {sorted(set(documented) - set(tagged))}"
    )


def test_each_tag_points_at_the_parameter_the_doc_names():
    """Catches a tag applied to the wrong line, which a count alone would miss."""
    tagged, documented = tagged_parameters(), documented_parameters()
    mismatched = {n: (tagged[n], documented[n]) for n in tagged if tagged[n] != documented[n]}
    assert mismatched == {}, f"[OPEN] #N tagged on the wrong parameter: {mismatched}"


def test_the_tag_numbering_is_contiguous_across_live_and_retired_rows():
    """A gap means a row was deleted from one place and not the other.

    Tombstones count: a retired parameter keeps its number, so contiguity is
    checked over tagged rows PLUS retired ones. That is what lets a row be
    removed without renumbering the 68 below it.
    """
    numbers = sorted(set(tagged_parameters()) | retired_parameters())
    assert numbers == list(range(1, len(numbers) + 1))


def test_the_doc_headline_count_matches_reality():
    doc = OPEN_PARAMS_DOC.read_text()
    assert "D42" in doc  # the doc states the decision range it was built against
    assert len(tagged_parameters()) == 84
    assert len(tagged_parameters()) + len(retired_parameters()) == 86


# ══════════════════════════════════════════════════════════════════════════
# Loader mechanics
# ══════════════════════════════════════════════════════════════════════════


def test_validate_all_passes_on_the_shipped_config():
    assert cfg.validate_all() == []


def test_configs_are_cached_and_reloadable():
    first = cfg.risk()
    assert cfg.risk() is first
    cfg.reload()
    assert cfg.risk() is not first


def test_configs_are_immutable():
    """A job must not be able to talk itself into a bigger position at runtime."""
    with pytest.raises(ValidationError):
        cfg.risk().per_trade_risk_pct.tier_a = 99.0  # type: ignore[misc]


def test_watchlist_ignores_comments_blanks_and_duplicates(tmp_path):
    watchlist = tmp_path / "watchlist.txt"
    watchlist.write_text("# a comment\n\nSOFI\n  pltr  \nNVDA # inline comment\nSOFI\n")
    assert cfg.load_watchlist(watchlist) == ["SOFI", "PLTR", "NVDA"]


def test_a_missing_watchlist_is_not_an_error(tmp_path):
    assert cfg.load_watchlist(tmp_path / "nope.txt") == []


def test_the_shipped_watchlist_is_readable():
    """It is all comments today; it must still parse to a list."""
    assert isinstance(cfg.load_watchlist(), list)


def test_the_tag_scanner_can_see_a_parameter_name_with_a_digit():
    """The scanner's own blind spot, pinned.

    Its regex was `[a-z_]+`, which cannot match `tp1_depth_multiple`. Five
    geometry tags were therefore invisible: they carried an [OPEN] number with
    no doc row, and this file reported everything in agreement. A guard with a
    hole in it is worse than no guard, because it is believed.
    """
    assert TAG.match("    tp1_depth_multiple: 1.0   # [OPEN] #73")
    assert TAG.match("    tp2_gap_multiple: 2.0     # [OPEN] #76")
    assert TAG.match("  rsi_period: 14              # [OPEN] #4")
    # ...and the shipped config actually contains such names.
    assert any(any(ch.isdigit() for ch in name) for name in tagged_parameters().values())


def test_an_unknown_pullback_geometry_variant_is_rejected(tmp_path):
    """A mode, like `beat_source`: a typo must fail the loader rather than
    silently selecting the default geometry."""
    expect_error(
        tmp_path,
        "scoring.yaml",
        mutate("scoring.yaml", ["geometry", "pullback_to_trend", "variant"], "v9"),
        "variant",
    )


def test_a_pullback_low_window_as_long_as_the_swing_window_is_rejected(tmp_path):
    """At or above the swing lookback the pullback's low and the swing low are
    the same measurement twice — which is the v1 geometry v2 exists to
    replace, wearing v2's name."""
    expect_error(
        tmp_path,
        "scoring.yaml",
        mutate("scoring.yaml", ["geometry", "pullback_to_trend", "pullback_low_lookback_days"], 60),
        "must be SHORTER than",
    )


def test_the_shipped_geometry_is_v3_AND_its_evidence_is_on_record():
    """ADOPTED by D56 on the certifying run `f572337` / arm `4608e1c347ed`,
    which passed all six pre-registered criteria out of sample on trimmed R.

    The flag may not drift without a decision and a side-by-side backtest
    (D43/D50), so this pins the variant AND the citation together: a switch
    with no evidence block naming the run that justified it fails here.
    """
    scoring = cfg.scoring()
    assert scoring.geometry.pullback_to_trend.variant == "v3"
    evidence = scoring.evidence_for("pullback_to_trend")
    assert evidence is not None, "a variant switch has to cite the run that earned it"
    assert "f572337" in evidence.citation
    assert evidence.basis == "trimmed", "O20 certifies a trailing recipe on trimmed R"


def test_the_regime_split_on_record_says_the_edge_is_CONCENTRATED():
    """D45's expectation, pinned so nobody reads v3's +0.21R as uniform. The
    out-of-sample edge is almost all `bull_calm`; `bull_normal` measured
    NEGATIVE on 432 closed, which is a real sample and not a thin one."""
    evidence = cfg.scoring().evidence_for("pullback_to_trend")
    calm = evidence.of("bull_calm")
    normal = evidence.of("bull_normal")

    assert calm.expectancy_r > 0.4 and calm.closed >= 500
    assert normal.expectancy_r < 0 and normal.closed >= 400
    assert normal.is_counter_evidence, (
        "negative on a real sample, so a call there is counter-evidence"
    )
    assert evidence.best[0] == "bull_calm"
    # bear_normal looks best of all and is 18 trades: NO evidence, not good evidence.
    assert not evidence.of("bear_normal").sufficient


# ══════════════════════════════════════════════════════════════════════════
# Every configured field is read by something
# ══════════════════════════════════════════════════════════════════════════


def _unread_config_fields() -> dict[str, list[str]]:
    """{model: [fields nothing reads]}, where READ has a deliberate definition.

    A field counts as read when something accesses it BY NAME — `.field` or
    `["field"]` — either outside `core/config.py` or inside a function body
    within it. The second arm is not a loophole: a validator that refuses a
    contradictory ladder, or an accessor like `certification_of`, IS the rule
    being in force, and that is what the guard is asking about.

    A BARE QUOTED LITERAL DOES NOT COUNT, and the reason is a bug this guard
    had. It used to accept `"field"` anywhere in the tree, so `return "etf"` in
    the positions sync — a `positions.bucket` VALUE that merely shares a word
    with `BucketTargets.etf` — read as a config access, and the whole bucket
    block went quiet because its field names are short common words. Matching
    only the access forms costs nothing real: a dict-shaped config is still
    read by subscript.
    """
    import ast

    root = Path(__file__).resolve().parent.parent
    source = "\n".join(
        path.read_text()
        for folder in (
            "alerts",
            "backtest",
            "core",
            "db",
            "decision",
            "features",
            "ingest",
            "jobs",
            "scoring",
            "scripts",
            "shadow_book",
        )
        for path in (root / folder).rglob("*.py")
        if path.name != "config.py"
    )
    tree = ast.parse((root / "core" / "config.py").read_text())
    enforced = "\n".join(
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    )

    def is_read(name: str) -> bool:
        access = rf"""\.{name}\b|\[["']{name}["']\]"""
        return bool(re.search(access, source) or re.search(access, enforced))

    out: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for statement in node.body:
            if not isinstance(statement, ast.AnnAssign) or not isinstance(
                statement.target, ast.Name
            ):
                continue
            name = statement.target.id
            if not name.startswith("_") and not is_read(name):
                out.setdefault(node.name, []).append(name)
    return out


# Config fields whose READER DOES NOT EXIST YET, each against the open item
# that will read it. Every one is a rule from the decision log written down
# ahead of the layer that will enforce it, which is a legitimate state and a
# measurable one: 35 parameters of D17/D18/D35 risk and allocation discipline
# are declared and inert, and D52 calls that layer the primary product.
#
# PER FIELD, not per block, because the blocks are mixed. `Accounts` holds two
# fields the positions sync reads tonight and one that waits on a sleeve that
# does not exist; `ScoringConfig.certification` decides D53's gate on every
# idea while `sentiment_reinstatement` waits on O18. Exempting either block
# whole would have hidden the live field behind the inert one, and a list that
# can hide a live rule is worse than no list.
PENDING_LAYERS = {
    # O17, the portfolio-adviser layer: heat release, buckets, sleeves, the
    # ratchet, drift, the circuit breaker, dividend handling.
    "AllocationTargets.drift_alert_pct": "O17",
    "AllocationTargets.risk_off_vix_above": "O17",
    "Cash.has_risk_profile": "O17",
    "CircuitBreaker.on_trip": "O17",
    "CircuitBreaker.resets_after": "O17",
    "CircuitBreakerAction.pause_tier_b": "O17",
    "CircuitBreakerAction.tier_a_size_multiplier": "O17",
    "Dividends.dividend_adjusted_stops": "O17 (D19 at the position level)",
    "Dividends.core_etf_drip": "O17",
    "Dividends.hold_dividends_to_cash_bucket": "O17",
    "Heat.release_on_exit": "O17",
    "Heat.zero_risk_after_stop_at_breakeven": "O17",
    "OptionExits.early_exit_dte": "O17",
    "OptionsRisk.assignment_risk_check": "O17 (D19's short-call check)",
    "OptionsRisk.assignment_extrinsic_buffer": "O17",
    "Ratchet.cadence": "O17",
    "Ratchet.trigger_excess_points": "O17",
    "RiskConfig.percent_basis": "O17",
    "RiskConfig.pyramiding": "O17 (D37's adds)",
    "RiskConfig.sleeves": "O17 (D35's two scoreboards)",
    "Sleeve.benchmark": "O17",
    "Sleeves.ratchet": "O17",
    "StableBuckets.thesis_review_drawdown_pct": "O17",
    "StableBuckets.dividend_cut_triggers_review": "O17",
    "TierBBudget.measured_as": "O17 (the month-to-date Tier B tracker)",
    "VehicleByIVRank.check_binary_events_in_expiration": "O17",
    # D55: the sleeve cannot be funded until a strategy is past PAPER, so the
    # minimum is stated now and enforced by the layer that will size the sleeve.
    "Accounts.minimum_sleeve_usd": "D55, enforced when the sleeve is funded (O17)",
    # O18, the research lab: reinstating a demoted pillar needs the calibration
    # work, and ranking alternatives need the forward-return tables.
    "Ranking.rank_within_universe": "O18",
    "ScoringConfig.sentiment_reinstatement": "O18",
    "SentimentReinstatement.min_sentiment_score": "O18",
    "SentimentReinstatement.min_sample": "O18",
    "SentimentReinstatement.restore_toward": "O18",
    "PaperToLive.max_calibration_error_pct": "O18 (the calibration curve, D39(1))",
    # Shapes not yet built.
    "PullbackRecipe.require_reversal_candle": "a condition recipe 1 never implemented",
    "OptionChainScope.aggregate_iv": "aggregate IV is derived in schwab_chains "
    "without reading the flag",
}


def test_no_config_field_is_unread_in_a_layer_that_exists():
    """THREE TIMES NOW, a value has sat in config that nothing read.

    `range_contraction_ratio` gated a measurement centred on 1.00 (retired,
    O13). `swing_lookback_days` agreed with `price_structure`'s own default of
    60, so changing it moved nothing (wired, O15). `runner_trail: ma_20` has
    said "the runner trails the 20-day MA" since the framework was written and
    nothing has ever trailed anything (v3's job, O20).

    "A key that LOADS while nothing reads it is how an operator comes to
    believe a rule is in force when it is not" — the decision log's words about
    the retired key, and this is the guard that would have caught all three.

    Fields whose reader does not exist yet are listed in `PENDING_LAYERS` with
    the open item that will read them. That is not an exemption list to grow
    casually: adding a name means admitting a rule is inert, and the list
    shrinks as the layers land.
    """
    unread = {
        f"{model}.{field}" for model, fields in _unread_config_fields().items() for field in fields
    } - set(PENDING_LAYERS)
    assert unread == set(), (
        f"these config fields are loaded and read by nothing: {sorted(unread)} — wire "
        f"them, retire them, or add each one to PENDING_LAYERS with the open item that will"
    )


def test_the_pending_layers_list_only_names_fields_that_are_really_inert():
    """The other direction: a field that HAS been wired must leave the list, or
    the list becomes a place where live rules hide."""
    unread = {
        f"{model}.{field}" for model, fields in _unread_config_fields().items() for field in fields
    }
    wired = sorted(set(PENDING_LAYERS) - unread)
    assert wired == [], (
        f"these fields are in PENDING_LAYERS but something now reads them: {wired} — "
        f"remove them from the list"
    )


# ══════════════════════════════════════════════════════════════════════════
# D53: certification, and the citation the loader insists on
# ══════════════════════════════════════════════════════════════════════════


def test_a_trailing_net_at_or_below_the_recipes_own_horizon_is_REFUSED(tmp_path):
    """A net shorter than the trail it protects reproduces the exact defect it
    is named after. v3's first run was decided by a 15-session clock, so a
    `trail_timeout_days` of 15 under a trailing variant must not load."""
    shipped = raw("scoring.yaml")["geometry"]["pullback_to_trend"]
    for value in (10, 15):
        expect_error(
            tmp_path,
            "scoring.yaml",
            mutate(
                "scoring.yaml",
                ["geometry", "pullback_to_trend"],
                {**shipped, "variant": "v3", "trail_timeout_days": value},
            ),
            "must EXCEED the recipe's own horizon_days",
        )


def test_the_shipped_certification_matches_the_evidence_on_record():
    """Only `post_earnings_drift` has ever passed a pre-registered certifying
    run out of sample (D48 refreshed, D46's gap tested). v1 was never
    certified, v2 was refused under O15, and `base_breakout` is disabled."""
    certification = cfg.scoring().certification
    assert certification["post_earnings_drift"].status == "certified"
    assert certification["post_earnings_drift"].min_risk_reward == pytest.approx(2.0)
    assert "1625c95" in certification["post_earnings_drift"].evidence
    assert certification["pullback_to_trend"].status == "uncertified"
    assert certification["base_breakout"].status == "uncertified"


def test_certified_without_a_citation_is_refused(tmp_path):
    """'Certified' with no commit and config hash is an opinion, and this is
    the one flag in the repo that can let a plan past a gate."""
    expect_error(
        tmp_path,
        "scoring.yaml",
        mutate("scoring.yaml", ["certification", "pullback_to_trend"], {"status": "certified"}),
        "min_risk_reward",
    )
    expect_error(
        tmp_path,
        "scoring.yaml",
        mutate(
            "scoring.yaml",
            ["certification", "pullback_to_trend"],
            {"status": "certified", "min_risk_reward": 2.0},
        ),
        "evidence",
    )


def test_a_recipe_with_no_certification_entry_is_refused(tmp_path):
    """A recipe defaulting to `uncertified` would look like the safe choice and
    would hide that nobody has decided; defaulting to `certified` would undo
    D53 in silence. So neither default exists."""
    raw_config = copy.deepcopy(raw("scoring.yaml"))
    del raw_config["certification"]["base_breakout"]
    expect_error(tmp_path, "scoring.yaml", raw_config, "no D53 certification entry")


def test_a_certification_for_a_recipe_that_does_not_exist_is_refused(tmp_path):
    """A typo here would leave a real recipe uncovered."""
    raw_config = copy.deepcopy(raw("scoring.yaml"))
    raw_config["certification"]["pullback_to_trends"] = {"status": "uncertified"}
    expect_error(tmp_path, "scoring.yaml", raw_config, "not a recipe")
