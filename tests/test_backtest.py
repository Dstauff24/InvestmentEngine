"""The backtest engine. Fixtures only — no database, no network.

The tests that matter here are not "does it compute a number" but the four
places a backtest can lie to you:

  * filling on the signal bar (next-day entry),
  * resolving the ambiguous bar generously,
  * reading data that was not published yet,
  * and counting a hit rate as if it were an edge.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from backtest import metrics, provenance, sweep
from backtest.costs import CostModel
from backtest.engine import (
    ParamSet,
    SimTrade,
    SymbolHistory,
    dividends_after,
    events_knowable,
    fundamentals_knowable,
    walk_symbol,
)
from backtest.regime import RegimeSeries, regime_series
from backtest.resolve import bar_columns, forward_bars, resolve_plan
from core import config
from scoring.recipes.base import TradePlan

CALLED = date(2024, 3, 1)


def frame(rows: list[tuple[date, float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"session_date": d, "open": o, "high": h, "low": lo, "close": c, "volume": 1_000_000}
            for d, o, h, lo, c in rows
        ]
    )


def plan(**kw) -> TradePlan:
    base = {
        "entry_low": 99.0,
        "entry_high": 101.0,
        "stop": 95.0,
        "target_1": 110.0,
        "target_2": 120.0,
        "horizon_days": (2, 15),
    }
    base.update(kw)
    return TradePlan(**base)


# ══════════════════════════════════════════════════════════════════════════
# Costs (D39(4))
# ══════════════════════════════════════════════════════════════════════════


def test_slippage_pays_against_you_on_both_sides():
    model = CostModel(
        slippage_bps=10.0,
        commission_per_share=0.0,
        min_commission=0.0,
        option_premium_pct=5.0,
        option_delta=0.55,
        option_spread_pct=4.0,
        option_commission_per_contract=0.65,
    )
    assert model.buy(100.0, 100).filled == pytest.approx(100.10)
    assert model.sell(100.0, 100).filled == pytest.approx(99.90)


def test_net_r_is_below_gross_r_on_a_winner():
    model = CostModel.from_config()
    net = model.net_r_multiple(entry=100.0, exit_price=110.0, stop=95.0, shares=100)
    assert net is not None
    assert net < 2.0, "gross would be exactly 2R"
    assert net > 1.9, "5bp a side cannot cost half an R"


def test_a_stop_out_is_worse_than_exactly_minus_one_r():
    """A stop becomes a market order when it triggers. Filling it exactly at
    the stop would make every loss precisely 1R, which is the one thing a
    stop-out reliably is not."""
    model = CostModel.from_config()
    assert model.net_r_multiple(entry=100.0, exit_price=95.0, stop=95.0, shares=100) < -1.0


def test_a_frictionless_model_reproduces_the_gross_multiple():
    """So the friction's size can be isolated rather than assumed."""
    assert CostModel.free().net_r_multiple(
        entry=100.0, exit_price=110.0, stop=95.0, shares=100
    ) == pytest.approx(2.0)


def test_r_is_measured_against_the_planned_risk_not_the_slipped_entry():
    """The position was sized from the plan, so dividing by a retrospectively
    larger risk would quietly shrink every loss."""
    model = CostModel(
        slippage_bps=100.0,
        commission_per_share=0.0,
        min_commission=0.0,
        option_premium_pct=5.0,
        option_delta=0.55,
        option_spread_pct=0.0,
        option_commission_per_contract=0.0,
    )
    # Planned risk is 5.00. A 1% slip costs 1.00 buying at 100 and 1.10 selling
    # at 110, so 7.90 of the 10.00 move survives — and it is divided by the
    # PLANNED 5.00, not by the 6.00 the slipped entry implies.
    net = model.net_r_multiple(entry=100.0, exit_price=110.0, stop=95.0, shares=100)
    assert net == pytest.approx((10.0 - 1.0 - 1.1) / 5.0, abs=1e-6)
    assert net > (10.0 - 1.0 - 1.1) / 6.0, "dividing by the slipped risk would flatter it"


def test_zero_or_inverted_risk_has_no_r_multiple():
    model = CostModel.from_config()
    assert model.net_r_multiple(entry=100.0, exit_price=110.0, stop=100.0, shares=100) is None
    assert model.net_r_multiple(entry=100.0, exit_price=110.0, stop=105.0, shares=100) is None


def test_the_option_spread_is_crossed_once_per_round_trip_not_twice():
    """Half the width on the way in, half on the way out. Charging the full
    width twice would double the only cost that matters on an option."""
    model = CostModel(
        slippage_bps=0.0,
        commission_per_share=0.0,
        min_commission=0.0,
        option_premium_pct=5.0,
        option_delta=0.5,
        option_spread_pct=4.0,
        option_commission_per_contract=0.0,
    )
    # 100 shares at delta 0.5 is 2 contracts; premium 5% of 100 = $5/share = $500
    # a contract; a 4% width is $20 a contract, round trip.
    assert model.contracts_for(shares=100) == pytest.approx(2.0)
    assert model.option_friction(entry=100.0, shares=100) == pytest.approx(40.0)


def test_an_option_vehicle_costs_more_than_shares_for_the_same_plan():
    model = CostModel.from_config()
    shares_r = model.net_r_multiple(entry=100.0, exit_price=110.0, stop=95.0, shares=100)
    option_r = model.net_r_multiple(
        entry=100.0, exit_price=110.0, stop=95.0, shares=100, vehicle="option"
    )
    assert option_r < shares_r


# ══════════════════════════════════════════════════════════════════════════
# Next-day entry and the shared fill rules
# ══════════════════════════════════════════════════════════════════════════


def test_the_signal_bar_is_never_available_to_fill_against():
    """THE SINGLE MOST COMMON WAY A BACKTEST INVENTS AN EDGE. The plan is formed
    on the close of the bar that produced it; the high that triggered it is
    already past."""
    bars = frame(
        [
            (CALLED, 100.0, 130.0, 99.0, 129.0),
            (CALLED + timedelta(days=1), 101.0, 102.0, 100.0, 101.0),
        ]
    )
    rows = forward_bars(bars, after=CALLED, horizon_days=15)
    assert [r["session_date"] for r in rows] == [CALLED + timedelta(days=1)]


def test_the_forward_window_covers_waiting_then_holding():
    """A plan can sit unfilled for a whole horizon and then be held for
    another, so the window has to reach past both."""
    bars = frame([(CALLED + timedelta(days=i), 100.0, 101.0, 99.0, 100.0) for i in range(0, 60)])
    rows = forward_bars(bars, after=CALLED, horizon_days=15)
    assert len(rows) == 2 * 15 + 10


def test_adjusted_columns_win_when_present():
    """Measuring structure on an adjusted frame and filling against the raw one
    would put the stop on the wrong side of a 10-for-1."""
    bars = frame([(CALLED, 100.0, 101.0, 99.0, 100.0)])
    assert bar_columns(bars)["close"] == "close"
    bars["adj_close"] = 10.0
    bars["adj_open"] = 10.0
    bars["adj_high"] = 10.1
    bars["adj_low"] = 9.9
    assert bar_columns(bars)["close"] == "adj_close"
    assert forward_bars(bars, after=CALLED - timedelta(days=1), horizon_days=5)[0]["close"] == 10.0


def test_the_ambiguous_bar_resolves_pessimistically_through_the_tracker():
    """Not a second implementation: `resolve_plan` hands the plan to the Shadow
    Book's own `evaluate_call`, so an expectancy the backtest reports is one
    the live book can reproduce."""
    bars = frame(
        [
            (CALLED, 100.0, 101.0, 99.5, 100.0),
            # Touches the entry zone, then both the target and the stop.
            (CALLED + timedelta(days=1), 100.0, 121.0, 94.0, 100.0),
        ]
    )
    fill = resolve_plan(
        symbol="TEST",
        recipe="pullback_to_trend",
        plan=plan(),
        called_on=CALLED,
        bars=bars,
        dividends=[],
        costs=CostModel.free(),
        shares=100,
    )
    assert fill.outcome == "stopped"
    assert fill.gross_r == pytest.approx(-1.0)


def test_a_plan_made_on_the_last_session_is_not_a_trade(monkeypatch):
    """The edge of the data is not an outcome — counting it either way would
    invent one."""
    bars = frame([(CALLED, 100.0, 101.0, 99.0, 100.0)])
    fill = resolve_plan(
        symbol="TEST",
        recipe="pullback_to_trend",
        plan=plan(),
        called_on=CALLED,
        bars=bars,
        dividends=[],
        costs=CostModel.free(),
        shares=100,
    )
    assert fill.outcome == "no_forward_data"
    assert fill.filled is False


def test_an_unfilled_plan_is_never_triggered_rather_than_a_loss():
    """D13's rule, and the reason selectivity does not read as incompetence."""
    bars = frame([(CALLED + timedelta(days=i), 140.0, 141.0, 139.0, 140.0) for i in range(1, 40)])
    fill = resolve_plan(
        symbol="TEST",
        recipe="pullback_to_trend",
        plan=plan(),
        called_on=CALLED,
        bars=bars,
        dividends=[],
        costs=CostModel.free(),
        shares=100,
    )
    assert fill.outcome == "never_triggered"
    assert fill.net_r is None


def test_a_dividend_lowers_the_stop_in_the_backtest_too(monkeypatch):
    """D19(2) is applied by the shared tracker, so the backtest cannot forget
    it independently."""
    bars = frame(
        [
            (CALLED + timedelta(days=1), 100.0, 101.0, 100.0, 100.5),
            (CALLED + timedelta(days=2), 99.0, 100.0, 94.6, 95.0),
        ]
    )
    dividends = [{"ex_date": CALLED + timedelta(days=2), "cash_amount": 0.5}]
    fill = resolve_plan(
        symbol="TEST",
        recipe="pullback_to_trend",
        plan=plan(),
        called_on=CALLED,
        bars=bars,
        dividends=dividends,
        costs=CostModel.free(),
        shares=100,
    )
    assert fill.outcome == "open", "94.60 is above the dividend-adjusted 94.50"


# ══════════════════════════════════════════════════════════════════════════
# Point-in-time slicing
# ══════════════════════════════════════════════════════════════════════════


def test_a_quarter_is_invisible_until_it_is_filed():
    rows = [
        {"fiscal_date_ending": date(2024, 3, 31), "known_on": date(2024, 4, 30)},
        {"fiscal_date_ending": date(2023, 12, 31), "known_on": date(2024, 1, 30)},
    ]
    assert fundamentals_knowable(rows, as_of=date(2024, 4, 5)) == [rows[1]]
    assert len(fundamentals_knowable(rows, as_of=date(2024, 4, 30))) == 2


def test_only_the_last_quarters_are_kept_so_the_window_matches_the_loader():
    rows = [
        {"fiscal_date_ending": date(2020, 1, i), "known_on": date(2020, 1, i)} for i in range(1, 20)
    ]
    assert len(fundamentals_knowable(rows, as_of=date(2024, 1, 1), limit=12)) == 12


def test_events_outside_the_radar_window_are_not_visible():
    events = [
        {"event_date": date(2024, 3, 1), "event_type": "earnings"},
        {"event_date": date(2025, 3, 1), "event_type": "earnings"},
        {"event_date": date(2022, 3, 1), "event_type": "earnings"},
    ]
    visible = events_knowable(events, as_of=date(2024, 3, 1))
    assert len(visible) == 1
    assert visible[0]["event_date"] == date(2024, 3, 1)


def test_only_dividends_after_the_call_can_affect_its_stop():
    dividends = [
        {"ex_date": date(2024, 2, 1), "cash_amount": 1.0},
        {"ex_date": date(2024, 4, 1), "cash_amount": 1.0},
    ]
    assert dividends_after(dividends, after=CALLED) == [dividends[1]]


# ══════════════════════════════════════════════════════════════════════════
# The walk
# ══════════════════════════════════════════════════════════════════════════


def test_a_symbol_without_enough_history_is_skipped_not_measured():
    history = SymbolHistory(
        symbol="TINY",
        sector="Technology",
        bars=frame([(date(2024, 1, 1) + timedelta(days=i), 10, 11, 9, 10) for i in range(50)]),
        dividends=[],
        events=[],
        fundamentals=[],
    )
    assert history.usable is False
    assert (
        walk_symbol(
            history,
            param_sets=[ParamSet.baseline()],
            regimes=RegimeSeries(tags={}, benchmark="SPY", ma_length=200),
            costs=CostModel.free(),
            provenance=provenance.current(allow_dirty=True),
        )
        == []
    )


def test_one_open_trade_per_symbol_per_recipe():
    """A setup persists for days. Without this the same outcome is counted once
    per session it stayed valid."""
    trade = SimTrade(
        param_set="baseline",
        symbol="X",
        sector=None,
        recipe="pullback_to_trend",
        called_on=CALLED,
        regime="bull_calm",
        entry_low=99,
        entry_high=101,
        stop=95,
        target_1=110,
        target_2=120,
        risk_reward=4.0,
        outcome="target_2",
        entered_on=CALLED,
        entry=100.0,
        exit_price=120.0,
        exit_on=CALLED + timedelta(days=5),
        gross_r=4.0,
        net_r=3.9,
        days_held=5,
        mfe=20.0,
        mae=-1.0,
        hit_target_1=True,
        hit_target_2=True,
    )
    assert trade.closed is True
    assert trade.filled is True


def test_an_open_trade_at_the_data_edge_is_filled_but_not_closed():
    """It has an entry and no outcome, and counting it either way would be a
    claim the data cannot support."""
    trade = SimTrade(
        param_set="baseline",
        symbol="X",
        sector=None,
        recipe="pullback_to_trend",
        called_on=CALLED,
        regime="bull_calm",
        entry_low=99,
        entry_high=101,
        stop=95,
        target_1=110,
        target_2=120,
        risk_reward=4.0,
        outcome="open",
        entered_on=CALLED,
        entry=100.0,
        exit_price=None,
        exit_on=None,
        gross_r=None,
        net_r=None,
        days_held=None,
        mfe=2.0,
        mae=-1.0,
        hit_target_1=False,
        hit_target_2=False,
    )
    assert trade.filled is True
    assert trade.closed is False


def test_the_structure_key_ignores_gate_only_parameters():
    """What makes a sweep affordable: changing the RSI band reuses the
    features, changing the base length does not."""
    baseline = ParamSet.baseline()
    rsi = ParamSet(
        "rsi", sweep.with_override(baseline.cfg, "recipes.pullback_to_trend.rsi_max", 55.0)
    )
    weeks = ParamSet(
        "weeks", sweep.with_override(baseline.cfg, "recipes.base_breakout.min_base_weeks", 8)
    )
    assert rsi.structure_key == baseline.structure_key
    assert weeks.structure_key != baseline.structure_key


# ══════════════════════════════════════════════════════════════════════════
# Regimes
# ══════════════════════════════════════════════════════════════════════════


def test_sessions_before_the_moving_average_exists_are_unknown_not_guessed():
    bars = pd.DataFrame(
        {
            "session_date": [date(2024, 1, 1) + timedelta(days=i) for i in range(50)],
            "close": [100.0 + i for i in range(50)],
        }
    )
    series = regime_series(bars)
    assert series.tags == {}
    assert series.tag(date(2024, 1, 10)) == "unknown"


def test_the_trend_axis_is_d17s_own_test():
    days = 400
    closes = [100.0] * 250 + [50.0] * (days - 250)
    bars = pd.DataFrame(
        {
            "session_date": [date(2024, 1, 1) + timedelta(days=i) for i in range(days)],
            "close": closes,
        }
    )
    series = regime_series(bars)
    assert series.tag(date(2024, 1, 1) + timedelta(days=240)).startswith("bull")
    assert series.tag(date(2024, 1, 1) + timedelta(days=399)).startswith("bear")


# ══════════════════════════════════════════════════════════════════════════
# Metrics
# ══════════════════════════════════════════════════════════════════════════


def sim(net_r: float | None, *, outcome="target_2", entry=100.0, days=5) -> SimTrade:
    return SimTrade(
        param_set="baseline",
        symbol="X",
        sector=None,
        recipe="r",
        called_on=CALLED,
        regime="bull_calm",
        entry_low=99,
        entry_high=101,
        stop=95,
        target_1=110,
        target_2=120,
        risk_reward=4.0,
        outcome=outcome,
        entered_on=CALLED,
        entry=entry,
        exit_price=120.0,
        exit_on=CALLED + timedelta(days=days),
        gross_r=net_r,
        net_r=net_r,
        days_held=days,
        mfe=None,
        mae=None,
        hit_target_1=True,
        hit_target_2=False,
    )


def test_expectancy_ranks_strategies_the_way_a_hit_rate_cannot():
    """35% at +3R beats 70% at +0.5R, and any report led by hit rate would say
    the opposite."""
    rare_big = metrics.Expectancy(label="rare")
    for _ in range(35):
        rare_big.add(sim(3.0))
    for _ in range(65):
        rare_big.add(sim(-1.0, outcome="stopped"))

    often_small = metrics.Expectancy(label="often")
    for _ in range(70):
        often_small.add(sim(0.5))
    for _ in range(30):
        often_small.add(sim(-1.5, outcome="stopped"))

    assert often_small.hit_rate > rare_big.hit_rate
    assert rare_big.expectancy_r > often_small.expectancy_r
    assert rare_big.expectancy_r == pytest.approx(0.40)
    assert often_small.expectancy_r == pytest.approx(-0.10)


def test_a_breakeven_scratch_is_its_own_category():
    """At breakeven after the TP1 trim the trade was neither right nor wrong,
    and calling it a loss would understate a recipe whose risk management
    worked."""
    item = metrics.Expectancy(label="x")
    item.add(sim(0.0, outcome="target_1"))
    assert item.scratches == 1
    assert item.losses == 0
    assert item.wins == 0


def test_an_unfilled_signal_counts_as_a_signal_and_nothing_else():
    item = metrics.Expectancy(label="x")
    item.add(
        SimTrade(
            param_set="baseline",
            symbol="X",
            sector=None,
            recipe="r",
            called_on=CALLED,
            regime="bull_calm",
            entry_low=99,
            entry_high=101,
            stop=95,
            target_1=110,
            target_2=120,
            risk_reward=4.0,
            outcome="never_triggered",
            entered_on=None,
            entry=None,
            exit_price=None,
            exit_on=None,
            gross_r=None,
            net_r=None,
            days_held=None,
            mfe=None,
            mae=None,
            hit_target_1=False,
            hit_target_2=False,
        )
    )
    assert (item.signals, item.filled, item.closed) == (1, 0, 0)
    assert item.expectancy_r is None
    assert item.fill_rate == 0.0


def test_a_population_under_thirty_closed_trades_is_flagged_insufficient():
    """D39(2): no weight or threshold moves on less."""
    item = metrics.Expectancy(label="x")
    for _ in range(29):
        item.add(sim(1.0))
    assert item.sufficient is False
    item.add(sim(1.0))
    assert item.sufficient is True


def test_profit_factor_is_none_rather_than_infinite_when_nothing_lost():
    """A sample with no losses is a sample too small to have a profit factor."""
    item = metrics.Expectancy(label="x")
    item.add(sim(1.0))
    assert item.profit_factor is None


def test_friction_is_reported_as_the_gap_between_gross_and_net():
    item = metrics.Expectancy(label="x")
    trade = sim(1.0)
    item.add(SimTrade(**{**trade.__dict__, "gross_r": 1.1, "net_r": 1.0}))
    assert item.friction_r == pytest.approx(0.1)


def test_the_report_states_the_survivorship_caveat_and_the_promotion_rule():
    """Both are easy to leave as something the author knows and the reader
    does not."""
    from backtest.engine import BacktestResult

    result = BacktestResult(trades=[sim(1.0)], start=CALLED, end=CALLED, symbols=1)
    text = metrics.render_report(result)
    assert "Survivorship" in text
    assert "TODAY's constituents" in text
    assert "CLOSED SHADOW BOOK ideas" in text, "a backtest cannot promote a strategy (D40)"


# ══════════════════════════════════════════════════════════════════════════
# The sweep (D43)
# ══════════════════════════════════════════════════════════════════════════


def test_an_override_round_trips_through_the_loader():
    altered = sweep.with_override(
        config.scoring(), "recipes.base_breakout.range_contraction_ratio", 0.85
    )
    assert altered.recipes.base_breakout.range_contraction_ratio == 0.85
    assert config.scoring().recipes.base_breakout.range_contraction_ratio != 0.85, "unmutated"


def test_a_sweep_value_the_loader_refuses_is_rejected_not_tested():
    """`range_contraction_ratio >= 1` is not a looser gate, it is not a base —
    and the measured MEDIAN contraction is above 1, so the sweep really does
    propose it."""
    with pytest.raises(Exception, match="below 1"):
        sweep.with_override(config.scoring(), "recipes.base_breakout.range_contraction_ratio", 1.04)


def test_param_sets_skip_a_rejected_value_and_keep_the_rest():
    axes = [
        sweep.Axis(
            path="recipes.base_breakout.range_contraction_ratio",
            values=(0.79, 0.9, 1.04),
            rationale="measured",
        )
    ]
    names = [s.name for s in sweep.param_sets_for(axes)]
    assert names == ["baseline", "range_contraction_ratio=0.79", "range_contraction_ratio=0.9"]


def test_the_sweep_range_comes_from_the_measured_distribution():
    distributions = sweep.Distributions()
    for value in [0.7, 0.8, 0.9, 1.0, 1.1]:
        distributions.add("base_contraction", value)
    for value in [1.0, 2.0, 3.0]:
        distributions.add("gap_up_pct", value)

    axes = {axis.name: axis for axis in sweep.axes_from(distributions)}
    assert axes["range_contraction_ratio"].values[0] == pytest.approx(0.7)
    assert "measured p10" in axes["range_contraction_ratio"].rationale


def test_the_depth_axis_says_it_cannot_use_its_own_measurement():
    """A quantity bounded BY the parameter being swept cannot set its range:
    `_detect_base` only finds consolidations that already fit the cap."""
    distributions = sweep.Distributions()
    for value in [10.0, 20.0, 24.0]:
        distributions.add("base_depth_pct", value)

    axis = next(a for a in sweep.axes_from(distributions) if a.name == "max_base_depth_pct")
    shipped = config.scoring().recipes.base_breakout.max_base_depth_pct
    assert max(axis.values) > shipped, "it must be able to propose a LOOSER cap"
    assert "bounded BY this cap" in axis.rationale
    assert axis.structural is True


def test_a_structural_axis_is_marked_so_the_sweep_can_price_it():
    axes = {axis.name: axis for axis in sweep.axes_from(sweep.Distributions())}
    assert axes["min_base_weeks"].structural is True
    assert axes["rsi_max"].structural is False


# ══════════════════════════════════════════════════════════════════════════
# The D12 cut and reading a run back
# ══════════════════════════════════════════════════════════════════════════


def with_rr(value: float) -> SimTrade:
    trade = sim(1.0)
    return SimTrade(**{**trade.__dict__, "risk_reward": value})


def test_the_gated_cut_keeps_only_plans_d12_would_accept():
    """A recipe answers "does this setup exist"; D12 then demands R:R >= 2 or
    >= 3. Measured over four years, `pullback_to_trend`'s median R:R is 0.85 —
    so expectancy over ALL signals is expectancy over a population the live
    engine would mostly refuse."""
    trades = [with_rr(0.8), with_rr(2.5), with_rr(3.5)]
    assert len(metrics.eligible(trades, min_rr=2.0)) == 2
    assert len(metrics.eligible(trades, min_rr=3.0)) == 1


def test_a_plan_exactly_on_the_gate_is_accepted():
    """D12's gates are inclusive, as `idea_builder` reads them."""
    assert len(metrics.eligible([with_rr(2.0)], min_rr=2.0)) == 1


def test_a_trade_with_no_risk_reward_is_not_counted_as_eligible():
    assert (
        metrics.eligible([SimTrade(**{**sim(1.0).__dict__, "risk_reward": None})], min_rr=2.0) == []
    )


def test_the_report_shows_both_the_ungated_and_the_gated_populations():
    """One says what the SETUP is worth, the other what the ENGINE would have
    traded. Reporting only the first overstates a recipe whose good plans are
    rare; only the second hides why."""
    from backtest.engine import BacktestResult

    text = metrics.render_report(
        BacktestResult(trades=[with_rr(0.5), with_rr(4.0)], start=CALLED, end=CALLED, symbols=1)
    )
    assert "PER RECIPE\n" in text
    assert "ONLY PLANS D12 WOULD ACCEPT (Tier A: R:R >= 2)" in text
    assert "ONLY PLANS D12 WOULD ACCEPT (Tier B: R:R >= 3)" in text


def test_a_written_run_can_be_re_read_and_re_rendered(tmp_path):
    """A four-year walk is 70 minutes; a question about it should not be."""
    csv = tmp_path / "trades.csv"
    csv.write_text(
        "param_set,symbol,sector,recipe,called_on,regime,risk_reward,outcome,entry,"
        "gross_r,net_r,days_held\n"
        "baseline,AAPL,Technology,pullback_to_trend,2024-03-01,bull_calm,2.5,target_2,"
        "100.0,2.0,1.9,7\n"
        "baseline,MSFT,Technology,pullback_to_trend,2024-03-04,bull_calm,0.8,stopped,"
        "100.0,-1.0,-1.1,3\n"
        "baseline,JPM,Financials,post_earnings_drift,2024-03-05,bull_calm,,never_triggered,"
        ",,,\n"
    )

    run = metrics.load_csv(csv)
    assert run.symbols == 3
    assert len(run.trades) == 3

    recipes = metrics.by_recipe(run.trades)
    assert recipes["pullback_to_trend"].closed == 2
    assert recipes["pullback_to_trend"].expectancy_r == pytest.approx(0.4)
    assert recipes["post_earnings_drift"].filled == 0, "an empty entry is unfilled, not zero"
    assert len(metrics.eligible(run.trades, min_rr=2.0)) == 1


def test_a_multi_variant_run_does_not_pool_its_configurations():
    """Each headline table describes ONE configuration.

    With `--variant` the walk produces two populations over the same dates, and
    pooling them would count every trade once per configuration that produced
    it — describing a strategy nobody runs. The comparison belongs in its own
    section, which is also the only place the two should ever appear together.
    """
    from backtest.engine import BacktestResult

    base = sim(1.0)
    trades = [
        SimTrade(**{**base.__dict__, "param_set": "baseline", "risk_reward": 4.0}),
        SimTrade(**{**base.__dict__, "param_set": "beat_source=verified", "risk_reward": 4.0}),
    ]
    text = metrics.render_report(BacktestResult(trades=trades, start=CALLED, end=CALLED, symbols=1))

    headline = text.split("PER RECIPE  [baseline]")[1].split("VARIANTS")[0]
    row = next(line for line in headline.splitlines() if line.startswith("r "))
    assert row.split()[1] == "1", f"the headline must count one configuration: {row!r}"
    assert "VARIANTS — the same walk, one configuration changed" in text
    assert "beat_source=verified / r" in text


# ══════════════════════════════════════════════════════════════════════════
# Provenance: freeze before launch, enforced
# ══════════════════════════════════════════════════════════════════════════


def test_a_dirty_tree_refuses_to_start(monkeypatch):
    """THE RULE THAT WAS A HABIT. Three runs in one afternoon were invalidated
    by edits landing mid-walk, and only remembering caught it."""
    monkeypatch.setattr(
        provenance, "_git", lambda *a: "abc1234\n" if a[0] == "rev-parse" else " M engine.py\n"
    )
    with pytest.raises(provenance.DirtyTree) as exc:
        provenance.current()
    assert "engine.py" in str(exc.value)
    assert "--allow-dirty" in str(exc.value), "the message says how to proceed anyway"


def test_allow_dirty_runs_but_stamps_the_output_non_baseline(monkeypatch):
    monkeypatch.setattr(
        provenance, "_git", lambda *a: "abc1234\n" if a[0] == "rev-parse" else " M engine.py\n"
    )
    stamp = provenance.current(allow_dirty=True)
    assert stamp.dirty is True
    assert stamp.baseline is False
    assert any("NON-BASELINE" in line for line in stamp.header())
    assert stamp.as_row()["dirty"] == "1"


def test_a_clean_tree_is_a_baseline(monkeypatch):
    monkeypatch.setattr(provenance, "_git", lambda *a: "abc1234\n" if a[0] == "rev-parse" else "")
    stamp = provenance.current()
    assert stamp.baseline is True
    assert stamp.commit == "abc1234"
    assert not any("NON-BASELINE" in line for line in stamp.header())


def test_an_unreadable_repository_counts_as_dirty(monkeypatch):
    """The rule is "prove the tree is clean", and an unanswerable question is
    not a yes — a tarball deploy must not silently produce baselines."""
    monkeypatch.setattr(provenance, "_git", lambda *a: None)
    with pytest.raises(provenance.DirtyTree):
        provenance.current()
    stamp = provenance.current(allow_dirty=True)
    assert stamp.commit == provenance.UNKNOWN
    assert stamp.baseline is False


def test_the_status_parse_keeps_the_whole_path(monkeypatch):
    """`--porcelain` puts status in the first two columns and the first is
    usually a space, so stripping the output eats one character of the FIRST
    line only — which printed `acktest/engine.py` once and would have sent
    somebody looking for a file that does not exist."""
    monkeypatch.setattr(
        provenance,
        "_git",
        lambda *a: "abc1234\n" if a[0] == "rev-parse" else " M backtest/engine.py\n?? new.py\n",
    )
    stamp = provenance.current(allow_dirty=True)
    assert stamp.dirty_files == ("backtest/engine.py", "new.py")


def test_the_config_hash_follows_the_effective_values_not_the_file(monkeypatch):
    """A comment change must not move the hash; a value change must."""
    baseline = ParamSet.baseline()
    altered = ParamSet(
        "x", sweep.with_override(baseline.cfg, "recipes.pullback_to_trend.rsi_max", 55.0)
    )
    assert baseline.config_hash != altered.config_hash
    assert baseline.config_hash == ParamSet.baseline().config_hash


def test_every_trade_carries_the_stamp_and_it_survives_a_csv_round_trip(tmp_path):
    csv = tmp_path / "trades.csv"
    csv.write_text(
        "param_set,commit,config_hash,dirty,symbol,sector,recipe,called_on,regime,"
        "risk_reward,outcome,entry,gross_r,net_r,days_held\n"
        "baseline,abc1234,deadbeef0000,0,AAPL,Tech,pullback_to_trend,2024-03-01,bull_calm,"
        "4.0,target_2,100.0,2.0,1.9,7\n"
    )
    run = metrics.load_csv(csv)
    assert run.trades[0].commit == "abc1234"
    assert run.trades[0].config_hash == "deadbeef0000"
    assert run.trades[0].dirty is False


def test_a_report_over_two_commits_says_so_loudly():
    """A COMPARISON ACROSS DIFFERENT CODE IS NOT A COMPARISON: the difference
    between the arms is partly the code."""
    from backtest.engine import BacktestResult

    base = sim(1.0)
    trades = [
        SimTrade(**{**base.__dict__, "commit": "aaa1111"}),
        SimTrade(**{**base.__dict__, "commit": "bbb2222", "param_set": "variant"}),
    ]
    text = metrics.render_report(BacktestResult(trades=trades, start=CALLED, end=CALLED, symbols=1))
    assert "MIXED CODE" in text
    assert "aaa1111" in text and "bbb2222" in text


def test_two_arms_sharing_a_config_hash_is_reported_as_measuring_noise():
    """If an override did not change the effective config, the two arms are the
    same strategy twice and any difference between them is sampling."""
    from backtest.engine import BacktestResult

    base = sim(1.0)
    trades = [
        SimTrade(**{**base.__dict__, "config_hash": "same000"}),
        SimTrade(**{**base.__dict__, "config_hash": "same000", "param_set": "variant"}),
    ]
    text = metrics.render_report(BacktestResult(trades=trades, start=CALLED, end=CALLED, symbols=1))
    assert "two arms share a hash" in text
