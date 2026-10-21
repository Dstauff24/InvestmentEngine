"""Pillar A measurements (D2/D32 weight 40). Fixtures only — no DB, no network.

Each assertion is arithmetic someone can check by hand, which is the only way
a number this load-bearing stays trustworthy: a test written against real
market data asserts whatever the code happened to produce.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from features import technical as tech
from tests.fixtures import price_series as ps

# ══════════════════════════════════════════════════════════════════════════
# Moving averages
# ══════════════════════════════════════════════════════════════════════════


def test_sma_of_a_flat_series_is_the_price():
    assert tech.sma(pd.Series(ps.flat(30)), 20) == pytest.approx(100.0)


def test_sma_of_a_known_ramp():
    """1..20 averages to 10.5, and only the LAST 20 may be used."""
    closes = pd.Series([float(i) for i in range(1, 21)])
    assert tech.sma(closes, 20) == pytest.approx(10.5)
    longer = pd.Series([float(i) for i in range(1, 41)])
    assert tech.sma(longer, 20) == pytest.approx(30.5)  # 21..40


def test_an_ma_with_too_little_history_is_none_not_a_number():
    """A 200-day MA from 40 bars is a plausible-looking wrong answer."""
    assert tech.sma(pd.Series(ps.flat(40)), 200) is None


def test_the_ma_stack_on_a_rising_series_is_stacked_and_rising():
    closes = pd.Series(ps.rising(260))
    stack = tech.ma_stack(closes, fast=20, mid=50, slow=200)
    assert stack.stacked is True
    assert stack.fast > stack.mid > stack.slow
    assert stack.fast_rising is True
    assert stack.mid_rising is True
    assert stack.price_above_fast is True
    assert stack.price_above_slow is True


def test_the_ma_stack_on_a_falling_series_is_not_stacked():
    stack = tech.ma_stack(pd.Series(ps.falling(260)), fast=20, mid=50, slow=200)
    assert stack.stacked is False
    assert stack.fast_rising is False
    assert stack.price_above_slow is False


def test_a_short_history_leaves_stacked_unknown_rather_than_false():
    """ "We cannot see an uptrend" is not "this is a downtrend".

    A recipe gating on an uptrend must not read a young listing as confirmed
    weakness — that would quietly reject every recent IPO on false grounds.
    """
    stack = tech.ma_stack(pd.Series(ps.rising(60)), fast=20, mid=50, slow=200)
    assert stack.slow is None
    assert stack.stacked is None
    assert stack.fast is not None  # what IS knowable is still reported


def test_pct_from_fast_is_signed_and_in_percent():
    closes = pd.Series([*ps.flat(20, 100.0), 110.0])
    stack = tech.ma_stack(closes, fast=20, mid=50, slow=200)
    # The last 20 closes are nineteen 100s and one 110 -> mean 100.5
    assert stack.fast == pytest.approx(100.5)
    assert stack.pct_from_fast == pytest.approx((110 - 100.5) / 100.5 * 100)


def test_rising_is_measured_on_the_average_not_on_price():
    """Price can sit above a FALLING average — the two readings are independent.

    My first version of this asserted that a single spike "must not fake a
    rising MA" and used a +109 jump, which genuinely DOES turn a 20-day average
    up. The claim was false: a big enough day moves the average, and reporting
    that is correct. What is worth pinning is the weaker, true property — that
    `fast_rising` is computed from the average while `price_above_fast` is
    computed from price, so a modest bounce in a downtrend sets one without the
    other.

    Downtrend of 1/day from 200 ends at 141; a bounce to 161 clears the 20-day
    mean (150.55) while leaving it below its value five bars earlier (154.5).
    """
    closes = pd.Series([*ps.falling(60, start=200.0), 161.0])
    stack = tech.ma_stack(closes, fast=20, mid=50, slow=200, rising_lookback=5)
    assert stack.fast == pytest.approx(150.55)
    assert stack.price_above_fast is True
    assert stack.fast_rising is False


def test_a_large_enough_single_day_does_turn_the_average_up():
    """The companion to the above: this is correct, not a bug."""
    closes = pd.Series([*ps.falling(60, start=200.0), 250.0])
    stack = tech.ma_stack(closes, fast=20, mid=50, slow=200, rising_lookback=5)
    assert stack.fast_rising is True


# ══════════════════════════════════════════════════════════════════════════
# RSI
# ══════════════════════════════════════════════════════════════════════════


def test_rsi_of_an_unbroken_advance_is_100():
    """No down-closes: RSI is 100 by definition, not a divide-by-zero."""
    assert tech.rsi_wilder(pd.Series(ps.rising(40)), 14) == pytest.approx(100.0)


def test_rsi_of_an_unbroken_decline_is_zero():
    assert tech.rsi_wilder(pd.Series(ps.falling(40)), 14) == pytest.approx(0.0)


def test_rsi_of_a_flat_series_is_100_because_nothing_fell():
    """Degenerate but real: avg_loss is 0, so RS is undefined upward."""
    assert tech.rsi_wilder(pd.Series(ps.flat(40)), 14) == pytest.approx(100.0)


def test_rsi_of_an_alternating_series_sits_near_50():
    closes = pd.Series([100.0 + (1.0 if i % 2 else 0.0) for i in range(60)])
    value = tech.rsi_wilder(closes, 14)
    assert 40 < value < 60, f"balanced gains and losses should sit mid-range, got {value}"


def test_rsi_uses_wilder_smoothing_not_a_simple_mean():
    """The two disagree by several points, and recipe 1's 40-50 band was
    written against the figure a charting package shows — Wilder's."""
    closes = pd.Series([*ps.rising(20, start=100.0), *ps.falling(10, start=120.0, step=2.0)])
    wilder = tech.rsi_wilder(closes, 14)

    delta = closes.diff().dropna()
    gains = delta.clip(lower=0).tail(14).mean()
    losses = (-delta).clip(lower=0).tail(14).mean()
    simple = 100 - 100 / (1 + gains / losses)

    assert abs(wilder - simple) > 1.0, (
        "if these agree the implementation is probably the simple variant, "
        "which would shift the recipe band without anyone editing config"
    )


def test_rsi_needs_period_plus_one_closes():
    assert tech.rsi_wilder(pd.Series(ps.rising(14)), 14) is None
    assert tech.rsi_wilder(pd.Series(ps.rising(15)), 14) is not None


# ══════════════════════════════════════════════════════════════════════════
# ATR
# ══════════════════════════════════════════════════════════════════════════


def test_atr_on_a_flat_series_is_the_high_low_band():
    """Fixtures straddle the close by 1% each way, so true range is 2%."""
    bars = ps.frame(ps.flat(40, 100.0))
    assert tech.atr(bars, 14) == pytest.approx(2.0, rel=1e-6)


def test_atr_pct_is_atr_over_the_last_close():
    bars = ps.frame(ps.flat(40, 100.0))
    assert tech.atr_pct(bars, 14) == pytest.approx(2.0, rel=1e-6)


def test_atr_includes_the_gap_not_just_the_daily_range():
    """high-low alone understates risk on exactly the names that gap — which
    are the ones a stop most needs to survive."""
    closes = [*ps.flat(20, 100.0), 150.0, *ps.flat(19, 150.0)]
    bars = ps.frame(closes)
    gapped = tech.atr(bars, 14)

    no_gap = tech.atr(ps.frame(ps.flat(40, 150.0)), 14)
    assert gapped > no_gap, "the 50-point gap must widen ATR"


def test_atr_needs_enough_bars():
    assert tech.atr(ps.frame(ps.flat(10)), 14) is None


# ══════════════════════════════════════════════════════════════════════════
# Relative volume
# ══════════════════════════════════════════════════════════════════════════


def test_relative_volume_of_a_steady_tape_is_one():
    bars = ps.frame(ps.flat(30), volumes=[1_000_000.0] * 30)
    assert tech.relative_volume(bars["volume"], window=20) == pytest.approx(1.0)


def test_relative_volume_excludes_today_from_its_own_baseline():
    """Including today dilutes the spike being measured: a true 3x reads as
    ~2.7x on a 20-day window, and recipe 2 gates on >= 1.5x."""
    volumes = [1_000_000.0] * 20 + [3_000_000.0]
    bars = ps.frame(ps.flat(21), volumes=volumes)
    assert tech.relative_volume(bars["volume"], window=20) == pytest.approx(3.0), (
        "a 3x day must read as 3.0, not 2.7"
    )


def test_relative_volume_needs_a_full_prior_window():
    bars = ps.frame(ps.flat(20), volumes=[1_000_000.0] * 20)
    assert tech.relative_volume(bars["volume"], window=20) is None


def test_zero_baseline_volume_is_none_not_infinity():
    volumes = [0.0] * 20 + [500_000.0]
    bars = ps.frame(ps.flat(21), volumes=volumes)
    assert tech.relative_volume(bars["volume"], window=20) is None


# ══════════════════════════════════════════════════════════════════════════
# 52-week structure
# ══════════════════════════════════════════════════════════════════════════


def test_a_new_high_is_at_the_top_of_its_range():
    structure = tech.fifty_two_week_structure(pd.Series(ps.rising(252)))
    assert structure.is_new_high is True
    assert structure.range_position == pytest.approx(1.0)
    assert structure.pct_from_high == pytest.approx(0.0)


def test_a_new_low_is_at_the_bottom_of_its_range():
    structure = tech.fifty_two_week_structure(pd.Series(ps.falling(252)))
    assert structure.is_new_high is False
    assert structure.range_position == pytest.approx(0.0)


def test_the_midpoint_of_a_v_sits_mid_range():
    closes = ps.v_shape(100, 50, start=200.0)  # 200 -> 100 -> 150
    structure = tech.fifty_two_week_structure(pd.Series(closes), min_bars=100)
    assert structure.high == pytest.approx(200.0)
    assert structure.low == pytest.approx(100.0)
    assert structure.range_position == pytest.approx(0.5)


def test_a_short_history_reports_nothing_but_says_how_short():
    """Otherwise every recent listing looks like it is breaking out of a range
    it has not lived through."""
    structure = tech.fifty_two_week_structure(pd.Series(ps.rising(60)))
    assert structure.high is None
    assert structure.range_position is None
    assert structure.bars_used == 60


def test_pct_from_high_is_negative_below_the_high():
    closes = [*ps.rising(200, start=100.0), *ps.falling(52, start=299.0)]
    structure = tech.fifty_two_week_structure(pd.Series(closes))
    assert structure.pct_from_high < 0


# ══════════════════════════════════════════════════════════════════════════
# Relative strength vs sector (D33)
# ══════════════════════════════════════════════════════════════════════════


def test_outperforming_the_sector_is_positive():
    mine = pd.Series(ps.rising(80, start=100.0, step=1.0))  # +~79%
    theirs = pd.Series(ps.rising(80, start=100.0, step=0.2))  # +~15.8%
    assert tech.relative_strength_vs_sector(mine, theirs, window=63) > 0


def test_underperforming_the_sector_is_negative():
    mine = pd.Series(ps.flat(80, 100.0))
    theirs = pd.Series(ps.rising(80, start=100.0, step=1.0))
    assert tech.relative_strength_vs_sector(mine, theirs, window=63) < 0


def test_matching_the_sector_is_zero():
    series = pd.Series(ps.rising(80))
    assert tech.relative_strength_vs_sector(series, series.copy(), window=63) == pytest.approx(0.0)


def test_relative_strength_is_a_difference_not_a_ratio():
    """A ratio blows up when the sector return approaches zero, which is the
    quiet-sector case where relative strength matters most."""
    mine = pd.Series([*ps.flat(63, 100.0), 110.0])
    flat_sector = pd.Series(ps.flat(64, 100.0))
    value = tech.relative_strength_vs_sector(mine, flat_sector, window=63)
    assert value == pytest.approx(10.0), "a flat sector must not produce a division blow-up"


def test_a_sector_series_that_is_too_short_yields_none():
    """A composite starting halfway through the window would read as
    outperformance for every one of its members."""
    mine = pd.Series(ps.rising(80))
    assert tech.relative_strength_vs_sector(mine, pd.Series(ps.rising(20)), window=63) is None


# ══════════════════════════════════════════════════════════════════════════
# Sector composite
# ══════════════════════════════════════════════════════════════════════════


def test_the_composite_is_equal_weighted():
    """Cap-weighting a sector with one mega-cap charts that one name, and
    every other member then looks weak against it."""
    big = ps.frame(ps.rising(30, start=1000.0, step=10.0))
    small = ps.frame(ps.flat(30, 5.0))
    for frame in (big, small):
        frame["adj_close"] = frame["close"]

    composite = tech.sector_composite({"BIG": big, "SMALL": small})
    assert len(composite) == 30
    assert composite.iloc[0] == pytest.approx(100.0), "normalised to 100 at the start"
    # BIG ends ~+29%, SMALL flat -> an equal-weighted mean lands near +14.5%.
    assert composite.iloc[-1] == pytest.approx(114.5, abs=0.6)


def test_the_composite_uses_only_dates_every_member_shares():
    """One member's missing day must not move the index on its own."""
    full = ps.frame(ps.flat(30, 100.0))
    short = ps.frame(ps.flat(30, 100.0)).iloc[10:].reset_index(drop=True)
    for frame in (full, short):
        frame["adj_close"] = frame["close"]

    composite = tech.sector_composite({"FULL": full, "SHORT": short})
    assert len(composite) == 20


def test_an_empty_sector_yields_an_empty_series():
    assert tech.sector_composite({}).empty


# ══════════════════════════════════════════════════════════════════════════
# The bundle, and the ordering trap
# ══════════════════════════════════════════════════════════════════════════


def test_compute_fills_every_reading_on_a_full_history():
    bars = ps.frame(ps.rising(300))
    sector = pd.Series(ps.rising(300, step=0.5))
    features = tech.compute("TEST", bars, sector_closes=sector)

    assert features.symbol == "TEST"
    assert isinstance(features.session_date, date)
    assert features.close is not None
    assert features.ma.stacked is True
    assert features.rsi is not None
    assert features.atr is not None
    assert features.relative_volume is not None
    assert features.week52.high is not None
    assert features.rs_vs_sector is not None
    assert features.bars_available == 300


def test_compute_on_a_young_listing_returns_nones_not_guesses():
    features = tech.compute("NEW", ps.frame(ps.rising(30)))
    assert features.ma.slow is None
    assert features.week52.high is None
    assert features.rs_vs_sector is None, "no sector series was supplied"
    assert features.close is not None, "what IS knowable is still reported"


def test_a_descending_frame_is_refused_rather_than_measured_backwards():
    """THE trap: a reversed frame yields a complete set of plausible numbers
    describing a year ago, and nothing downstream would catch it."""
    bars = ps.frame(ps.rising(60)).iloc[::-1].reset_index(drop=True)
    with pytest.raises(ValueError, match="ascending session_date order"):
        tech.compute("TEST", bars)


def test_compute_prefers_adjusted_closes_when_present():
    """D24: split-adjusted prices are what the MAs must be built from."""
    bars = ps.frame(ps.flat(260, 100.0))
    bars["adj_close"] = 50.0  # a 2:1 split halving the stored series
    features = tech.compute("TEST", bars)
    assert features.close == pytest.approx(50.0)
    assert features.ma.fast == pytest.approx(50.0)


def test_an_empty_frame_does_not_raise():
    features = tech.compute("TEST", ps.frame([]))
    assert features.close is None
    assert features.bars_available == 0


def test_as_dict_is_flat_enough_to_log():
    features = tech.compute("TEST", ps.frame(ps.rising(300)))
    out = features.as_dict()
    assert out["symbol"] == "TEST"
    assert isinstance(out["ma"], dict)
    assert "stacked" in out["ma"]
    assert "range_position" in out["week52"]
