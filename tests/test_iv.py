"""Aggregate IV maths (D22). Fixtures only — no network, no DB."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from core import config
from features import iv
from ingest.schwab_chains import parse_chain
from tests.fixtures import schwab_payloads as fx

SNAPSHOT_TS = datetime(2026, 10, 20, 20, 0, tzinfo=UTC)


@pytest.fixture
def contracts():
    """The stored-row shape `derive_iv` reads back out of the database."""
    return parse_chain(
        fx.CHAIN,
        symbol="TEST",
        snapshot_ts=SNAPSHOT_TS,
        session_date=fx.AS_OF,
        scope=config.universe().option_chains,
    )


def row(expiration, strike, option_type, **over):
    base = {
        "expiration": expiration,
        "strike": strike,
        "option_type": option_type,
        "implied_volatility": 0.25,
        "delta": 0.5,
        "mark": 5.0,
        "bid": 4.9,
        "ask": 5.1,
        "open_interest": 1,
        "volume": 1,
        "underlying_price": 100.0,
    }
    return {**base, **over}


# ══════════════════════════════════════════════════════════════════════════
# ATM = NEAREST STRIKE, not delta-0.50
# ══════════════════════════════════════════════════════════════════════════


def test_atm_is_the_nearest_listed_strike(contracts):
    assert iv.atm_strike(contracts, 100.0, fx.MONTHLIES[0]) == 100.0
    assert iv.atm_strike(contracts, 103.0, fx.MONTHLIES[0]) == 105.0
    assert iv.atm_strike(contracts, 102.4, fx.MONTHLIES[0]) == 100.0


def test_atm_is_stable_when_skew_moves_the_fifty_delta_strike():
    """Why nearest-strike and not delta-0.50.

    These two chains have identical strikes and spot but different deltas. A
    delta-0.50 definition would pick a different strike on each, putting a
    spurious wobble into a series whose whole purpose is day-over-day comparison
    (D7). Nearest-strike does not move.
    """
    day_one = [row(fx.MONTHLIES[0], s, "C", delta=d) for s, d in [(95.0, 0.50), (100.0, 0.42)]]
    day_two = [row(fx.MONTHLIES[0], s, "C", delta=d) for s, d in [(95.0, 0.61), (100.0, 0.50)]]
    assert iv.atm_strike(day_one, 100.0, fx.MONTHLIES[0]) == 100.0
    assert iv.atm_strike(day_two, 100.0, fx.MONTHLIES[0]) == 100.0


def test_atm_iv_blends_call_and_put(contracts):
    """At one strike they should agree by parity; averaging cancels a stale quote."""
    blended, call, put = iv.atm_iv(contracts, 100.0, fx.MONTHLIES[0])
    assert call == pytest.approx(0.25)
    assert put == pytest.approx(0.29)  # fixture puts carry +4 vol points
    assert blended == pytest.approx(0.27)


def test_atm_iv_falls_back_to_whichever_side_has_a_number():
    blended, call, put = iv.atm_iv(
        [row(fx.MONTHLIES[0], 100.0, "C", implied_volatility=0.30)], 100.0, fx.MONTHLIES[0]
    )
    assert (blended, call, put) == (pytest.approx(0.30), pytest.approx(0.30), None)


def test_atm_iv_ignores_a_zero_or_null_volatility():
    rows = [
        row(fx.MONTHLIES[0], 100.0, "C", implied_volatility=0.0),
        row(fx.MONTHLIES[0], 100.0, "P", implied_volatility=0.31),
    ]
    blended, call, put = iv.atm_iv(rows, 100.0, fx.MONTHLIES[0])
    assert call is None
    assert put == pytest.approx(0.31)
    assert blended == pytest.approx(0.31)


def test_an_empty_expiration_yields_nothing(contracts):
    assert iv.atm_iv(contracts, 100.0, date(2030, 1, 18)) == (None, None, None)


# ══════════════════════════════════════════════════════════════════════════
# Constant-maturity interpolation, with NO extrapolation
# ══════════════════════════════════════════════════════════════════════════


def test_iv30_interpolates_between_the_bracketing_expirations():
    """Halfway between 20d/20% and 40d/30% is 25%."""
    assert iv.interpolate_iv30([(20, 0.20), (40, 0.30)]) == pytest.approx(0.25)


def test_iv30_is_exact_when_a_point_sits_on_thirty_days():
    assert iv.interpolate_iv30([(30, 0.27), (60, 0.31)]) == pytest.approx(0.27)


def test_iv30_refuses_to_extrapolate_past_the_ends_of_the_curve():
    """A straight-line guess past the last listed expiry is fiction, not data."""
    assert iv.interpolate_iv30([(45, 0.30), (75, 0.34)]) == pytest.approx(0.30)
    assert iv.interpolate_iv30([(5, 0.40), (12, 0.36)]) == pytest.approx(0.36)


def test_iv30_extrapolation_would_have_been_wrong_in_a_specific_direction():
    """Shows what the refusal protects against: a linear extension of a steep
    curve produces a number the market never priced."""
    points = [(45, 0.30), (75, 0.34)]
    slope = (0.34 - 0.30) / (75 - 45)
    naive = 0.30 + slope * (30 - 45)  # = 0.28, below every observed point
    assert naive < min(v for _, v in points)
    assert iv.interpolate_iv30(points) == pytest.approx(0.30)


def test_iv30_handles_a_single_point():
    assert iv.interpolate_iv30([(17, 0.25)]) == pytest.approx(0.25)


def test_iv30_ignores_junk_points():
    assert iv.interpolate_iv30([(20, None), (20, 0.0), (40, 0.30)]) == pytest.approx(0.30)
    assert iv.interpolate_iv30([]) is None


def test_a_constant_maturity_series_does_not_drift_with_the_calendar():
    """THE reason iv30 exists, demonstrated exactly.

    Take a market whose true volatility curve is unchanged across two weeks —
    here IV = 0.20 + 0.001 x DTE. The EXPIRATIONS have rolled a week closer, so
    the raw front-month reading moves (0.224 -> 0.217) purely from the calendar.
    Interpolating to a fixed 30-day horizon returns 0.230 on both days, which is
    the curve's true 30-day value.

    Without this, an IV-rank series built on the front month would be measuring
    the calendar rather than the market.
    """

    def curve(dte: int) -> float:
        return 0.20 + 0.001 * dte

    week_one = [(24, curve(24)), (52, curve(52))]
    week_two = [(17, curve(17)), (45, curve(45))]

    # The raw front-month reading drifted...
    assert week_one[0][1] != pytest.approx(week_two[0][1])

    # ...but the constant-maturity reading did not, and both equal the truth.
    assert iv.interpolate_iv30(week_one) == pytest.approx(curve(30))
    assert iv.interpolate_iv30(week_two) == pytest.approx(curve(30))


# ══════════════════════════════════════════════════════════════════════════
# Skew, term slope, expected move
# ══════════════════════════════════════════════════════════════════════════


def test_skew_is_positive_when_puts_are_bid_up(contracts):
    """The normal equity state: downside protection costs more than upside."""
    assert iv.skew_25_delta(contracts, fx.MONTHLIES[0]) > 0


def test_skew_needs_both_sides():
    calls_only = [row(fx.MONTHLIES[0], 110.0, "C", delta=0.25)]
    assert iv.skew_25_delta(calls_only, fx.MONTHLIES[0]) is None


def test_skew_picks_the_contract_nearest_a_quarter_delta():
    rows = [
        row(fx.MONTHLIES[0], 110.0, "C", implied_volatility=0.22, delta=0.24),
        row(fx.MONTHLIES[0], 120.0, "C", implied_volatility=0.20, delta=0.05),
        row(fx.MONTHLIES[0], 90.0, "P", implied_volatility=0.32, delta=-0.26),
        row(fx.MONTHLIES[0], 80.0, "P", implied_volatility=0.40, delta=-0.05),
    ]
    assert iv.skew_25_delta(rows, fx.MONTHLIES[0]) == pytest.approx(0.10)


def test_expected_move_is_the_atm_straddle_over_spot(contracts):
    move = iv.expected_move_pct(contracts, 100.0, fx.MONTHLIES[0])
    assert 0 < move < 0.5


def test_expected_move_needs_both_legs():
    one_leg = [row(fx.MONTHLIES[0], 100.0, "C")]
    assert iv.expected_move_pct(one_leg, 100.0, fx.MONTHLIES[0]) is None


def test_expected_move_falls_back_to_the_bid_ask_midpoint():
    rows = [
        row(fx.MONTHLIES[0], 100.0, "C", mark=None, bid=4.0, ask=6.0),
        row(fx.MONTHLIES[0], 100.0, "P", mark=None, bid=4.0, ask=6.0),
    ]
    assert iv.expected_move_pct(rows, 100.0, fx.MONTHLIES[0]) == pytest.approx(0.10)


# ══════════════════════════════════════════════════════════════════════════
# Rank and percentile, gated on iv_rank_min_history_days (D7)
# ══════════════════════════════════════════════════════════════════════════


def test_iv_rank_places_current_in_the_historical_range():
    assert iv.iv_rank(0.25, [0.20, 0.30]) == pytest.approx(0.5)
    assert iv.iv_rank(0.20, [0.20, 0.30]) == pytest.approx(0.0)
    assert iv.iv_rank(0.30, [0.20, 0.30]) == pytest.approx(1.0)


def test_iv_rank_clamps_a_new_extreme():
    assert iv.iv_rank(0.40, [0.20, 0.30]) == pytest.approx(1.0)
    assert iv.iv_rank(0.10, [0.20, 0.30]) == pytest.approx(0.0)


def test_a_flat_history_gives_no_rank():
    """Every value would read "100%" — a division artefact, not a signal."""
    assert iv.iv_rank(0.25, [0.25, 0.25, 0.25]) is None


def test_iv_percentile_counts_the_history_below_current():
    assert iv.iv_percentile(0.28, [0.20, 0.25, 0.30, 0.35]) == pytest.approx(0.5)


def test_percentile_is_more_robust_to_one_spike_than_rank():
    """A single panic print permanently flattens a rank series; percentile shrugs."""
    calm = [0.20 + i * 0.002 for i in range(20)]  # 0.200 .. 0.238
    spiked = [*calm, 1.50]
    current = 0.236

    assert iv.iv_rank(current, spiked) < 0.03  # rank crushed by the outlier
    assert iv.iv_percentile(current, spiked) > 0.85  # percentile still says "high"
    assert iv.iv_rank(current, calm) > 0.85  # and agrees without the spike


def test_rank_and_percentile_are_none_without_history():
    assert iv.iv_rank(0.25, []) is None
    assert iv.iv_percentile(0.25, []) is None


# ══════════════════════════════════════════════════════════════════════════
# The whole summary row
# ══════════════════════════════════════════════════════════════════════════


def test_summarize_produces_every_iv_daily_column(contracts):
    row_out = iv.summarize(contracts, as_of=fx.AS_OF, min_history_days=60)
    assert set(row_out) == {
        "underlying_price",
        "iv30",
        "iv_atm_front",
        "iv_call_atm",
        "iv_put_atm",
        "skew_25d",
        "term_slope",
        "expected_move_front_pct",
        "iv_rank_252",
        "iv_percentile_252",
        "history_days",
        "total_open_interest",
        "total_option_volume",
        "put_call_oi_ratio",
    }


def test_term_slope_is_positive_in_contango(contracts):
    """Fixture runs 25% front to 30% back — the normal upward-sloping curve."""
    row_out = iv.summarize(contracts, as_of=fx.AS_OF, min_history_days=60)
    assert row_out["term_slope"] > 0
    assert row_out["iv_atm_front"] == pytest.approx(0.27)


def test_rank_stays_null_until_the_configured_history_exists(contracts):
    """D7: IV rank means nothing until enough of our OWN snapshots exist.

    The threshold is config (`option_chains.iv_rank_min_history_days`), not a
    literal here — features/iv.py takes it as a required argument.
    """
    minimum = config.universe().option_chains.iv_rank_min_history_days
    short = iv.summarize(
        contracts, as_of=fx.AS_OF, min_history_days=minimum, history=[0.2] * (minimum - 1)
    )
    assert short["iv_rank_252"] is None
    assert short["iv_percentile_252"] is None
    assert short["history_days"] == minimum - 1


def test_rank_appears_once_the_history_threshold_is_crossed(contracts):
    minimum = config.universe().option_chains.iv_rank_min_history_days
    history = [0.20 + i * 0.001 for i in range(minimum)]
    row_out = iv.summarize(contracts, as_of=fx.AS_OF, min_history_days=minimum, history=history)
    assert row_out["iv_rank_252"] is not None
    assert row_out["iv_percentile_252"] is not None
    assert row_out["history_days"] == minimum


def test_the_shipped_threshold_is_sixty_days():
    assert config.universe().option_chains.iv_rank_min_history_days == 60


def test_open_interest_and_volume_are_totalled(contracts):
    row_out = iv.summarize(contracts, as_of=fx.AS_OF, min_history_days=60)
    assert row_out["total_open_interest"] == sum(c["open_interest"] for c in contracts)
    assert row_out["total_option_volume"] == sum(c["volume"] for c in contracts)
    assert row_out["put_call_oi_ratio"] == pytest.approx(1.0)  # symmetric fixture


def test_summarize_returns_nothing_rather_than_a_row_of_nulls():
    """An absent row is honest; a row of nulls looks like an observation of nothing."""
    assert iv.summarize([], as_of=fx.AS_OF, min_history_days=60) is None
    no_spot = [row(fx.MONTHLIES[0], 100.0, "C", underlying_price=None)]
    assert iv.summarize(no_spot, as_of=fx.AS_OF, min_history_days=60) is None


def test_summarize_ignores_expirations_already_past(contracts):
    """A chain pulled after an expiry should not weight a dead series."""
    expiries = iv.expirations(contracts, date(2027, 1, 20))
    assert fx.MONTHLIES[0] not in expiries
    assert fx.MONTHLIES[3] in expiries
