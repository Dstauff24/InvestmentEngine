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

    # `min_members` lowered: this test is about WEIGHTING, not membership.
    composite = tech.sector_composite({"BIG": big, "SMALL": small}, min_members=2)
    assert len(composite) == 30
    assert composite.iloc[0] == pytest.approx(100.0), "normalised to 100 at the start"
    # BIG ends ~+29%, SMALL flat -> an equal-weighted mean lands near +14.5%.
    assert composite.iloc[-1] == pytest.approx(114.5, abs=0.6)


def test_a_date_below_the_membership_floor_is_excluded():
    """Replaces an intersection test. The composite no longer requires EVERY
    member on every date — it requires `min_member_fraction` of them, so a
    member's missing day narrows the average rather than deleting the date,
    while a date where too few members have data is dropped.

    Ten members, six with full history and four listing late, so the FRACTION
    is the binding constraint rather than the `min_members` hard floor.
    """
    members = {}
    for i in range(6):
        frame = ps.frame(ps.flat(100, 100.0))
        frame["adj_close"] = frame["close"]
        members[f"OLD{i}"] = frame
    for i in range(4):
        frame = ps.frame(ps.flat(30, 100.0))
        frame["session_date"] = ps.sessions(100)[-30:]
        frame["adj_close"] = frame["close"]
        members[f"NEW{i}"] = frame

    everyone = tech.sector_composite(members, min_member_fraction=1.0)
    most = tech.sector_composite(members, min_member_fraction=0.6)

    assert len(everyone) == 30, "a 100% floor admits only the dates all ten share"
    assert len(most) == 100, "six of ten clears a 60% floor, so the full span counts"


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


def test_a_short_history_member_does_not_truncate_the_sector_index():
    """PER-DATE MEMBERSHIP. The first version intersected dates, so one recent
    listing shortened the index for everybody.

    Observed live 2026-10-21: SPCX, listed June 2026, cut the Industrials index
    from 278 bars to 91 while every sector without a newcomer got 278. Dropping
    short members fixed the symptom and still ended the index at the shortest
    SURVIVOR, while discarding the newcomer entirely once it did have history.
    """
    members = {}
    for name in ("A", "B", "C", "D"):
        frame = ps.frame(ps.rising(200))
        frame["adj_close"] = frame["close"]
        members[name] = frame

    newcomer = ps.frame(ps.rising(20))
    newcomer["adj_close"] = newcomer["close"]

    without = tech.sector_composite(members, min_member_fraction=0.8)
    with_new = tech.sector_composite({**members, "NEW": newcomer}, min_member_fraction=0.8)

    assert len(without) == 200
    assert len(with_new) == 200, "the newcomer joins late; it does not truncate"


def test_the_newcomer_IS_used_on_the_dates_it_exists():
    """The point of per-date membership rather than dropping it: once a member
    has data, it counts."""
    members = {}
    for name in ("A", "B", "C", "D"):
        frame = ps.frame(ps.flat(200, 100.0))
        frame["adj_close"] = frame["close"]
        members[name] = frame

    # A newcomer that doubles over its short life, sharing the LAST dates.
    tail = ps.frame(ps.rising(20, start=100.0, step=5.0))
    tail["session_date"] = ps.sessions(200)[-20:]
    tail["adj_close"] = tail["close"]

    flat_only = tech.sector_composite(members, min_member_fraction=0.8)
    with_new = tech.sector_composite({**members, "NEW": tail}, min_member_fraction=0.8)

    assert flat_only.iloc[-1] == pytest.approx(100.0)
    assert with_new.iloc[-1] > 100.0, "the newcomer lifts the index where it exists"


def test_a_member_joining_is_not_a_price_event():
    """Each member is normalised to 100 at its own first date, so a newcomer
    entering at a raw price of 5 against members at 300 cannot step the index."""
    members = {}
    for name in ("A", "B", "C", "D"):
        frame = ps.frame(ps.flat(100, 300.0))
        frame["adj_close"] = frame["close"]
        members[name] = frame

    cheap = ps.frame(ps.flat(30, 5.0))
    cheap["session_date"] = ps.sessions(100)[-30:]
    cheap["adj_close"] = cheap["close"]

    index = tech.sector_composite({**members, "CHEAP": cheap}, min_member_fraction=0.8)
    assert index.max() == pytest.approx(100.0, abs=0.01)
    assert index.min() == pytest.approx(100.0, abs=0.01), "no step at the join date"


def test_the_membership_floor_excludes_dates_with_too_few_members():
    """On a date where only 3 of 40 had listed, the "sector" would be those
    three — and relative strength against three stocks is not relative
    strength against a sector."""
    long_members = {}
    for name in ("A", "B"):
        frame = ps.frame(ps.rising(200))
        frame["adj_close"] = frame["close"]
        long_members[name] = frame
    for name in ("C", "D", "E"):
        frame = ps.frame(ps.rising(50))
        frame["session_date"] = ps.sessions(200)[-50:]
        frame["adj_close"] = frame["close"]
        long_members[name] = frame

    strict = tech.sector_composite(long_members, min_member_fraction=1.0)
    loose = tech.sector_composite(long_members, min_member_fraction=0.4)
    assert len(strict) == 50, "every member must be present"
    # 0.4 of five is two, but `min_members`=3 is a HARD floor over the
    # fraction — so the early dates where only two members exist are still
    # excluded. My first expectation here was 200, which ignored that.
    assert len(loose) == 50, "min_members floors the fraction, not the reverse"

    relaxed = tech.sector_composite(long_members, min_member_fraction=0.4, min_members=2)
    assert len(relaxed) == 200, "with the hard floor lowered too, two is enough"


def test_a_sector_below_the_member_floor_has_no_index():
    frames = {}
    for name in ("A", "B"):
        frame = ps.frame(ps.rising(200))
        frame["adj_close"] = frame["close"]
        frames[name] = frame
    assert tech.sector_composite(frames, min_members=3).empty


# ══════════════════════════════════════════════════════════════════════════
# The earnings reaction window (D48/D49)
# ══════════════════════════════════════════════════════════════════════════

REPORT = date(2026, 10, 20)


def reaction_bars(closes: list[float], lows: list[float] | None = None) -> pd.DataFrame:
    """Five sessions: 10-16, 10-19, 10-20 (T), 10-21, 10-22."""
    days = [date(2026, 10, 16), date(2026, 10, 19), REPORT, date(2026, 10, 21), date(2026, 10, 22)]
    lows = lows or [c - 1.0 for c in closes]
    return pd.DataFrame(
        {
            "session_date": days[: len(closes)],
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": lows,
            "close": closes,
            "volume": [1_000_000] * len(closes),
        }
    )


def test_an_unknown_release_time_spans_the_close_before_through_the_day_after():
    """THE LOOKAHEAD-FREE WINDOW. A pre-open report gaps on T and an
    after-close one gaps on T+1; no provider publishes which, so the window
    contains the gap either way."""
    bars = reaction_bars([98.0, 100.0, 105.0, 106.0])
    r = tech.earnings_reaction(bars, reported_on=REPORT, release_time="unknown")

    assert r is not None
    assert r.reference_close == pytest.approx(100.0), "close(T-1)"
    assert r.end_session == date(2026, 10, 21), "close(T+1)"
    assert r.sessions == 2
    assert r.reaction_pct == pytest.approx(6.0)
    assert r.reaction_points == pytest.approx(6.0)
    assert r.complete is True


def test_a_pre_open_report_completes_on_the_report_date_itself():
    """`bmo`: the news was out before T's open, so T's close completes the
    reaction and a plan may enter a day earlier."""
    bars = reaction_bars([98.0, 100.0, 105.0])
    r = tech.earnings_reaction(bars, reported_on=REPORT, release_time="bmo")

    assert r.reference_close == pytest.approx(100.0)
    assert r.end_session == REPORT
    assert r.sessions == 1
    assert r.reaction_pct == pytest.approx(5.0)
    assert r.complete is True


def test_an_after_close_report_uses_the_report_days_close_as_the_reference():
    """`amc`: T's own bar is PRE-news, so close(T) is the exact reference and
    including T's move would attribute it to a report that had not happened."""
    bars = reaction_bars([98.0, 100.0, 101.0, 107.0])
    r = tech.earnings_reaction(bars, reported_on=REPORT, release_time="amc")

    assert r.reference_close == pytest.approx(101.0), "close(T), not close(T-1)"
    assert r.end_session == date(2026, 10, 21)
    assert r.sessions == 1
    assert r.reaction_pct == pytest.approx((107.0 - 101.0) / 101.0 * 100)


def test_during_market_hours_is_treated_like_an_unknown_time():
    """The news lands with the session half traded, so the reaction runs into
    the next one."""
    bars = reaction_bars([98.0, 100.0, 105.0, 106.0])
    assert tech.earnings_reaction(bars, reported_on=REPORT, release_time="dmt").sessions == 2


def test_the_window_is_incomplete_until_its_last_session_is_the_latest_bar():
    """What stops the recipe judging a reaction before it has finished, and
    what stops it re-judging the same one for days afterwards."""
    only_t = reaction_bars([98.0, 100.0, 105.0])
    assert tech.earnings_reaction(only_t, reported_on=REPORT, release_time="unknown") is None

    through_t1 = reaction_bars([98.0, 100.0, 105.0, 106.0])
    assert tech.earnings_reaction(through_t1, reported_on=REPORT).complete is True

    past_t1 = reaction_bars([98.0, 100.0, 105.0, 106.0, 107.0])
    assert tech.earnings_reaction(past_t1, reported_on=REPORT).complete is False


def test_the_window_low_is_the_lowest_low_inside_it_not_the_report_days():
    """For an after-close report the gap day IS the second session, and with an
    unknown time it could be either — so the lowest low in the window is the
    level that invalidates the move whichever session carried it."""
    bars = reaction_bars([98.0, 100.0, 105.0, 106.0], lows=[97.0, 99.0, 101.0, 100.5])
    r = tech.earnings_reaction(bars, reported_on=REPORT)
    assert r.window_low == pytest.approx(100.5)
    assert r.window_low > 99.0, "the pre-report session's low is outside the window"


def test_a_report_dated_on_a_weekend_anchors_on_the_next_session():
    """Providers do date reports on days the market was shut."""
    bars = reaction_bars([98.0, 100.0, 105.0, 106.0])
    r = tech.earnings_reaction(bars, reported_on=date(2026, 10, 18), release_time="unknown")
    assert r is not None
    assert r.reported_on == date(2026, 10, 18)
    # T is the 10-19 session, so the window runs close(10-16)..close(10-20).
    assert r.reference_close == pytest.approx(98.0)
    assert r.end_session == REPORT
    assert r.complete is False, "the latest bar is 10-21, a session past the window"


def test_a_reaction_with_no_session_before_the_report_is_none_not_zero():
    """Nothing to measure the move against."""
    bars = reaction_bars([105.0, 106.0, 107.0])
    assert tech.earnings_reaction(bars, reported_on=date(2026, 10, 16)) is None


def test_a_negative_reaction_is_measured_rather_than_hidden():
    """The recipe rejects it; the FEATURE reports it. A module that returned
    None for a drop would make "the market hated it" and "we could not see"
    the same reading."""
    bars = reaction_bars([98.0, 100.0, 94.0, 93.0])
    r = tech.earnings_reaction(bars, reported_on=REPORT)
    assert r.reaction_pct == pytest.approx(-7.0)


def test_compute_only_measures_a_reaction_when_a_report_anchors_one():
    bars = reaction_bars([98.0, 100.0, 105.0, 106.0])
    assert tech.compute("T", bars).reaction is None
    assert tech.compute("T", bars, reported_on=REPORT).reaction is not None


# ══════════════════════════════════════════════════════════════════════════
# The squeeze (O13), the price basis, and where a base ends
# ══════════════════════════════════════════════════════════════════════════


def _bars(ranges: list[float], *, price: float = 100.0) -> pd.DataFrame:
    """Bars whose daily range is `ranges[i]` percent of a constant close.

    Volatility is the only thing that varies, which is what makes the squeeze
    percentile hand-checkable: with no gaps, true range is exactly high-low.
    """
    days = ps.sessions(len(ranges))
    return pd.DataFrame(
        {
            "session_date": days,
            "open": [price] * len(ranges),
            "high": [price * (1 + r / 200.0) for r in ranges],
            "low": [price * (1 - r / 200.0) for r in ranges],
            "close": [price] * len(ranges),
            "volume": [1_000_000.0] * len(ranges),
        }
    )


def test_unchanging_volatility_sits_mid_range_not_at_a_one_year_high():
    """TIES ARE NOT EXTREMES. The reading is a member of its own reference
    window, so counting `<=` would put a tape whose range never changes at
    1.00 — and a flat tape is mid-range by definition, not a high."""
    assert tech.squeeze_percentile(_bars([2.0] * 300)) == pytest.approx(0.5)


def test_a_quiet_fortnight_after_a_loud_year_reads_as_coiled():
    quiet = _bars([4.0] * 290 + [0.5] * 10)
    reading = tech.squeeze_percentile(quiet)
    assert reading is not None and reading < 0.05, reading


def test_an_expanding_range_reads_as_the_opposite_of_coiled():
    loud = _bars([1.0] * 290 + [6.0] * 10)
    reading = tech.squeeze_percentile(loud)
    assert reading is not None and reading > 0.95, reading


def test_the_squeeze_is_none_under_a_year_of_history():
    """The standing convention: a percentile against 30 observations is a
    plausible-looking number for a question that needs a year."""
    assert tech.squeeze_percentile(_bars([2.0] * 200)) is None
    assert tech.squeeze_percentile(_bars([2.0] * 263)) is not None


def test_the_break_bar_is_excluded_from_its_own_squeeze_reading():
    """O13's design choice, and the reason the recipe can fire at all: the coil
    is a PRE-breakout condition, and the breakout bar is the release of it.
    Letting it into the window would have the very expansion the recipe buys
    disqualify the setup that produced it."""
    coiled = _bars([4.0] * 290 + [0.5] * 10)
    broke_out = pd.concat([coiled, _bars([9.0])], ignore_index=True)
    assert tech.squeeze_percentile(broke_out) == pytest.approx(
        tech.squeeze_percentile(coiled, exclude_today=False)
    )
    including = tech.squeeze_percentile(broke_out, exclude_today=False)
    assert including > tech.squeeze_percentile(broke_out), "the break bar expands the window"


def test_the_squeeze_is_scale_invariant():
    """Normalised by close, so a stock that doubled over the reference year is
    not read as permanently expanding."""
    shape = [4.0] * 290 + [0.5] * 10
    assert tech.squeeze_percentile(_bars(shape, price=10.0)) == pytest.approx(
        tech.squeeze_percentile(_bars(shape, price=1000.0))
    )


# ── one basis per frame ─────────────────────────────────────────────────────


def _reverse_split_frame(n: int = 300) -> pd.DataFrame:
    """A 1-for-5 reverse split on the last bar, adjusted at read time (D24).

    AMCR's real shape: `prices_daily` holds raw bars, so every bar before the
    ex-date is scaled UP by five in adjusted terms.
    """
    from features.adjustment import adjust_bars

    bars = ps.frame(ps.flat(n))
    actions = pd.DataFrame(
        [
            {
                "symbol": "TEST",
                "ex_date": bars["session_date"].iloc[-1],
                "action_type": "split",
                "cash_amount": None,
                "split_ratio": 0.2,
            }
        ]
    )
    return adjust_bars(bars, actions, mode="split_only", as_of=bars["session_date"].iloc[-1])


def test_adjusted_columns_are_promoted_over_the_raw_ones():
    frame = _reverse_split_frame()
    promoted = tech.on_adjusted_basis(frame)
    assert promoted["high"].iloc[0] == pytest.approx(frame["adj_high"].iloc[0])
    assert frame["high"].iloc[0] != pytest.approx(frame["adj_high"].iloc[0]), "fixture is flat"


def test_a_reverse_split_does_not_invent_a_breakout():
    """THE AMCR REGRESSION. `compute` took the closes from `adj_close` while
    handing the same frame to `price_structure`, which reads `high`/`low` — so
    the adjusted close sat 4.3x above a raw `base_high` and `base_breakout`'s
    trigger was satisfied on 806 consecutive sessions. Those were the only
    firings that recipe ever recorded."""
    features = tech.compute("TEST", _reverse_split_frame())
    assert features.close is not None and features.structure.base_high is not None
    assert features.close <= features.structure.base_high * 1.0001, (
        f"close {features.close} above base_high {features.structure.base_high} — "
        f"the two are on different price bases"
    )


def test_the_risk_unit_is_measured_on_the_same_basis_as_the_price():
    """D17 sizes every position off ATR, so a split inside the 14-bar period
    moved the stop distance on a real plan."""
    frame = _reverse_split_frame()
    features = tech.compute("TEST", frame)
    assert features.atr == pytest.approx(tech.atr(tech.on_adjusted_basis(frame)))
    assert features.atr != pytest.approx(tech.atr(frame)), "the raw frame reads differently"


# ── a base cannot include the bar that breaks it ────────────────────────────


def test_the_base_is_measured_through_the_previous_close():
    """`_detect_base` walks backward from the last bar it is given, so handing
    it today's bar makes `base_high >= today's high >= today's close` and the
    breakout trigger ARITHMETICALLY UNREACHABLE — measured over 2,810 base
    sessions on a clean basis, not one satisfied it."""
    bars = ps.frame([*ps.flat(60), 130.0])
    structure = tech.price_structure(bars, max_base_depth_pct=25.0, min_base_sessions=20)
    assert structure.base_high == pytest.approx(100.0 * 1.01), "the pre-break high"
    assert float(bars["close"].iloc[-1]) > structure.base_high, "today can clear it"


def test_a_rising_tape_never_has_a_base_high_above_todays_high():
    """The invariant the old code violated, stated as a test: on one basis, a
    base measured through yesterday is clearable; a base including today is
    not."""
    bars = ps.frame(ps.rising(80))
    structure = tech.price_structure(bars, max_base_depth_pct=25.0, min_base_sessions=20)
    assert structure.base_high is not None
    assert structure.base_high < float(bars["high"].iloc[-1])


def test_structure_atr_and_the_close_a_recipe_reads_are_all_one_basis():
    """THE WHOLE-CONSUMER ASSERTION. A recipe hangs a plan on three readings —
    the close it compares, the structural levels it puts the stop and targets
    on, and the ATR that buffers them (D17 then sizes off that ATR). They must
    come off ONE price series, or the plan mixes units: before this, `close`
    was `adj_close` while `price_structure` and `atr` read the raw columns.

    Checked against the promoted frame AND against the raw one, because
    equality with the adjusted values only proves the basis if the two bases
    actually differ in the fixture.
    """
    frame = _reverse_split_frame()
    promoted = tech.on_adjusted_basis(frame)
    features = tech.compute("TEST", frame)

    expected = tech.price_structure(
        promoted,
        max_base_depth_pct=25.0,
        min_base_sessions=4 * 5,
    )
    assert features.close == pytest.approx(float(promoted["close"].iloc[-1]))
    assert features.structure.base_high == pytest.approx(expected.base_high)
    assert features.structure.base_low == pytest.approx(expected.base_low)
    assert features.structure.swing_high == pytest.approx(expected.swing_high)
    assert features.structure.prior_close == pytest.approx(expected.prior_close)
    assert features.atr == pytest.approx(tech.atr(promoted))
    assert features.squeeze_percentile == pytest.approx(tech.squeeze_percentile(promoted))

    raw_structure = tech.price_structure(frame, max_base_depth_pct=25.0, min_base_sessions=20)
    assert features.structure.base_high != pytest.approx(raw_structure.base_high), (
        "the fixture must actually have two different bases, or this test proves nothing"
    )


def test_only_named_modules_read_raw_bars_and_each_says_why():
    """The consumer side of the same rule. `prices_daily` is RAW (D24), so a
    module that queries it and skips the adjustment layer is the bug this
    section is about — and it is invisible in testing, because it is wrong only
    for symbols with a split in the window.

    An allowlist with a reason per module, rather than a lint rule: three
    modules read raw bars legitimately and the reasons are not interchangeable.
    """
    import re
    from pathlib import Path

    # A SQL usage, not a prose mention: `regime.py`'s docstring now explains why
    # it no longer reads the table, and a guard that fired on the explanation
    # would push people to delete the explanation.
    queries = re.compile(r"(?:from|into|update|join)\s+prices_daily", re.IGNORECASE)

    allowed = {
        # Reads raw, then hands every frame through `adjust_bars` and
        # `on_adjusted_basis`. The single door for measurement.
        "features/technical.py",
        # Reads raw and rebases forward into the CALL's units
        # (`in_call_basis`), because the stored projections are frozen (D13).
        "shadow_book/outcome_tracker.py",
        # Reads raw ON PURPOSE: its whole job is to prove a stored series
        # STEPS at a split ex-date, which is the only evidence that
        # `prices_daily` is still raw.
        "jobs/price_basis_check.py",
        # Writers. D24 defines the table as raw, so these are what makes it so.
        "ingest/schwab_prices.py",
        "ingest/fmp_history.py",
        "db/init.py",
        # IS the adjustment layer: it reads raw bars and the action log and
        # returns the factors. It also hosts `BASIS_CHECK_SQL`, which must see
        # raw closes to prove the table still holds them.
        "features/adjustment.py",
    }
    root = Path(__file__).resolve().parent.parent
    offenders = sorted(
        str(path.relative_to(root))
        for folder in ("features", "scoring", "decision", "shadow_book", "backtest", "jobs")
        for path in (root / folder).rglob("*.py")
        if queries.search(path.read_text()) and str(path.relative_to(root)) not in allowed
    )
    assert offenders == [], (
        f"these query prices_daily without going through the adjustment layer: {offenders} — "
        f"add the module to the allowlist WITH ITS REASON, or read bars via "
        f"features.technical.load_bars"
    )


def test_the_pullback_low_is_the_short_windows_low_not_the_swings():
    """`swing_low` is the 60-day low; on a trending stock that is a level from
    a different episode. The pullback's own low is what its thesis breaks at."""
    closes = [*ps.rising(50, start=100.0), *ps.falling(6, start=150.0, step=2.0)]
    bars = ps.frame(closes)
    structure = tech.price_structure(bars, swing_lookback=60, pullback_lookback=10)
    assert structure.pullback_low == pytest.approx(min(bars["low"].iloc[-10:]))
    assert structure.swing_low == pytest.approx(min(bars["low"]))
    assert structure.pullback_low > structure.swing_low


def test_the_pullback_low_is_none_when_the_window_is_not_covered():
    """A "pullback low" from three bars would be today's low under another
    name."""
    bars = ps.frame(ps.flat(6))
    assert tech.price_structure(bars, pullback_lookback=10).pullback_low is None
    assert tech.price_structure(bars, pullback_lookback=5).pullback_low is not None


def test_the_configured_swing_lookback_actually_moves_the_measurement():
    """#71 WAS CONFIGURED AND NEVER READ until 2026-10-23: `price_structure`
    used its own default of 60 while the config said 60 too, so the duplication
    was invisible and changing the config moved nothing."""
    bars = ps.frame(ps.rising(120, start=100.0))
    short = tech.price_structure(bars, swing_lookback=20)
    long = tech.price_structure(bars, swing_lookback=100)
    assert short.swing_low > long.swing_low, "a shorter window sees a higher low"

    from core import config

    features = tech.compute(
        "TEST",
        bars,
        geometry_cfg=config.ScoringConfig.model_validate(
            {
                **config.scoring().model_dump(),
                "geometry": {
                    **config.scoring().geometry.model_dump(),
                    "pullback_to_trend": {
                        **config.scoring().geometry.pullback_to_trend.model_dump(),
                        "swing_lookback_days": 20,
                    },
                },
            }
        ).geometry.pullback_to_trend,
    )
    assert features.structure.swing_low == pytest.approx(short.swing_low)
