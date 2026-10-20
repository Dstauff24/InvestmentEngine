"""Read-time price adjustment (D24). Fixture frames only — no network, no DB."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from features.adjustment import AdjustmentError, adjust_bars, compute_adjustment_factors


def bars(rows: list[tuple[str, float, int]]) -> pd.DataFrame:
    """rows = [(iso_date, close, volume), ...]. OHLC derived from close."""
    return pd.DataFrame(
        {
            "session_date": [date.fromisoformat(d) for d, _, _ in rows],
            "open": [c for _, c, _ in rows],
            "high": [c for _, c, _ in rows],
            "low": [c for _, c, _ in rows],
            "close": [c for _, c, _ in rows],
            "volume": [v for _, _, v in rows],
        }
    )


def actions(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=["action_type", "ex_date", "split_ratio", "cash_amount"])
    if not frame.empty:
        frame["ex_date"] = [date.fromisoformat(d) for d in frame["ex_date"]]
    return frame


NO_ACTIONS = actions([])

FIVE_DAYS = bars(
    [
        ("2026-10-05", 100.0, 1_000_000),
        ("2026-10-06", 102.0, 1_100_000),
        ("2026-10-07", 104.0, 1_200_000),
        ("2026-10-08", 106.0, 1_300_000),
        ("2026-10-09", 108.0, 1_400_000),
    ]
)


# ── no actions ──────────────────────────────────────────────────────────────


def test_no_actions_leaves_everything_at_one():
    factors = compute_adjustment_factors(FIVE_DAYS, NO_ACTIONS)
    assert (factors["price_factor"] == 1.0).all()
    assert (factors["volume_factor"] == 1.0).all()


def test_no_actions_adjusted_equals_raw():
    out = adjust_bars(FIVE_DAYS, NO_ACTIONS)
    assert out["adj_close"].tolist() == FIVE_DAYS["close"].tolist()
    assert out["adj_volume"].tolist() == [float(v) for v in FIVE_DAYS["volume"]]


def test_the_raw_columns_are_preserved_alongside_the_adjusted_ones():
    """D24: the raw bar is the stored truth; adjusted values are derived."""
    out = adjust_bars(FIVE_DAYS, NO_ACTIONS)
    assert out["close"].tolist() == FIVE_DAYS["close"].tolist()
    assert {"adj_open", "adj_high", "adj_low", "adj_close", "adj_volume"} <= set(out.columns)
    assert {"price_factor", "volume_factor"} <= set(out.columns)


# ── splits ──────────────────────────────────────────────────────────────────


def test_four_for_one_split_scales_prior_bars_and_volume():
    acts = actions([{"action_type": "split", "ex_date": "2026-10-08", "split_ratio": 4.0}])
    out = adjust_bars(FIVE_DAYS, acts, mode="split_only")

    # Bars strictly BEFORE the ex-date are quartered; volume is quadrupled.
    assert out.loc[0, "adj_close"] == pytest.approx(25.0)
    assert out.loc[1, "adj_close"] == pytest.approx(25.5)
    assert out.loc[2, "adj_close"] == pytest.approx(26.0)
    assert out.loc[0, "adj_volume"] == pytest.approx(4_000_000)

    # The ex-date bar and everything after already trade post-split.
    assert out.loc[3, "adj_close"] == pytest.approx(106.0)
    assert out.loc[4, "adj_close"] == pytest.approx(108.0)
    assert out.loc[3, "adj_volume"] == pytest.approx(1_300_000)


def test_reverse_split_scales_prior_bars_up():
    acts = actions([{"action_type": "split", "ex_date": "2026-10-08", "split_ratio": 0.1}])
    out = adjust_bars(FIVE_DAYS, acts, mode="split_only")
    assert out.loc[0, "adj_close"] == pytest.approx(1000.0)
    assert out.loc[0, "adj_volume"] == pytest.approx(100_000)
    assert out.loc[3, "adj_close"] == pytest.approx(106.0)


def test_ohlc_all_move_together():
    acts = actions([{"action_type": "split", "ex_date": "2026-10-08", "split_ratio": 2.0}])
    out = adjust_bars(FIVE_DAYS, acts, mode="split_only")
    for col in ("adj_open", "adj_high", "adj_low", "adj_close"):
        assert out.loc[0, col] == pytest.approx(50.0)


def test_a_zero_or_negative_split_ratio_is_rejected():
    acts = actions([{"action_type": "split", "ex_date": "2026-10-08", "split_ratio": 0.0}])
    with pytest.raises(AdjustmentError, match="non-positive ratio"):
        compute_adjustment_factors(FIVE_DAYS, acts, mode="split_only")


# ── cash distributions (D19) ────────────────────────────────────────────────


def test_a_cash_dividend_uses_the_close_before_the_ex_date():
    # $2.04 on a prior close of 102.00 => factor 0.98 on every earlier bar.
    acts = actions([{"action_type": "cash_dividend", "ex_date": "2026-10-07", "cash_amount": 2.04}])
    factors = compute_adjustment_factors(FIVE_DAYS, acts, mode="total_return")
    assert factors.loc[date(2026, 10, 5), "price_factor"] == pytest.approx(0.98)
    assert factors.loc[date(2026, 10, 6), "price_factor"] == pytest.approx(0.98)
    assert factors.loc[date(2026, 10, 7), "price_factor"] == pytest.approx(1.0)


def test_split_only_mode_ignores_dividends():
    """Technical structure must line up with the chart a human is looking at."""
    acts = actions([{"action_type": "cash_dividend", "ex_date": "2026-10-07", "cash_amount": 2.04}])
    factors = compute_adjustment_factors(FIVE_DAYS, acts, mode="split_only")
    assert (factors["price_factor"] == 1.0).all()


def test_dividends_do_not_touch_volume():
    acts = actions([{"action_type": "cash_dividend", "ex_date": "2026-10-07", "cash_amount": 2.04}])
    factors = compute_adjustment_factors(FIVE_DAYS, acts, mode="total_return")
    assert (factors["volume_factor"] == 1.0).all()


def test_a_special_dividend_is_treated_as_a_cash_distribution():
    acts = actions(
        [{"action_type": "special_dividend", "ex_date": "2026-10-07", "cash_amount": 10.2}]
    )
    factors = compute_adjustment_factors(FIVE_DAYS, acts, mode="total_return")
    assert factors.loc[date(2026, 10, 5), "price_factor"] == pytest.approx(0.9)


def test_a_dividend_with_no_prior_bar_raises_in_strict_mode():
    acts = actions([{"action_type": "cash_dividend", "ex_date": "2026-10-05", "cash_amount": 1.0}])
    with pytest.raises(AdjustmentError, match="no usable prior close"):
        compute_adjustment_factors(FIVE_DAYS, acts, mode="total_return")


def test_a_dividend_with_no_prior_bar_is_skipped_when_not_strict():
    acts = actions([{"action_type": "cash_dividend", "ex_date": "2026-10-05", "cash_amount": 1.0}])
    factors = compute_adjustment_factors(FIVE_DAYS, acts, mode="total_return", strict=False)
    assert (factors["price_factor"] == 1.0).all()


# ── compounding ─────────────────────────────────────────────────────────────


def test_a_split_and_a_dividend_compound():
    acts = actions(
        [
            {"action_type": "cash_dividend", "ex_date": "2026-10-07", "cash_amount": 2.04},
            {"action_type": "split", "ex_date": "2026-10-08", "split_ratio": 2.0},
        ]
    )
    factors = compute_adjustment_factors(FIVE_DAYS, acts, mode="total_return")
    # Before both:  0.98 * 0.5 = 0.49
    assert factors.loc[date(2026, 10, 5), "price_factor"] == pytest.approx(0.49)
    assert factors.loc[date(2026, 10, 6), "price_factor"] == pytest.approx(0.49)
    # Between them: only the split still lies ahead.
    assert factors.loc[date(2026, 10, 7), "price_factor"] == pytest.approx(0.5)
    # On/after the split: nothing left to apply.
    assert factors.loc[date(2026, 10, 8), "price_factor"] == pytest.approx(1.0)


def test_action_order_in_the_input_frame_does_not_matter():
    rows = [
        {"action_type": "split", "ex_date": "2026-10-08", "split_ratio": 2.0},
        {"action_type": "cash_dividend", "ex_date": "2026-10-07", "cash_amount": 2.04},
    ]
    forward = compute_adjustment_factors(FIVE_DAYS, actions(rows))
    reverse = compute_adjustment_factors(FIVE_DAYS, actions(list(reversed(rows))))
    pd.testing.assert_frame_equal(forward, reverse)


# ══════════════════════════════════════════════════════════════════════════
# as_of REPRODUCIBILITY — the whole reason this is read-time (D24)
# ══════════════════════════════════════════════════════════════════════════


def test_as_of_ignores_actions_that_had_not_happened_yet():
    """A backtest re-run next year must see the series it saw the first time.

    Without `as_of`, every dividend paid between the two runs would silently
    restate the history and the same recipe would produce different numbers
    with no diff to point at — which is what breaks O7 walk-forward validation
    and D13's premise that outcomes are measured against what we projected.
    """
    acts = actions([{"action_type": "split", "ex_date": "2026-10-08", "split_ratio": 4.0}])

    known_later = compute_adjustment_factors(FIVE_DAYS, acts, as_of=date(2026, 10, 31))
    known_then = compute_adjustment_factors(FIVE_DAYS, acts, as_of=date(2026, 10, 6))

    assert known_later.loc[date(2026, 10, 5), "price_factor"] == pytest.approx(0.25)
    assert known_then.loc[date(2026, 10, 5), "price_factor"] == pytest.approx(1.0)


def test_as_of_is_inclusive_of_an_action_on_that_very_day():
    acts = actions([{"action_type": "split", "ex_date": "2026-10-08", "split_ratio": 4.0}])
    on_the_day = compute_adjustment_factors(FIVE_DAYS, acts, as_of=date(2026, 10, 8))
    assert on_the_day.loc[date(2026, 10, 5), "price_factor"] == pytest.approx(0.25)


def test_the_same_as_of_always_produces_the_same_series():
    """Determinism is the property a backtest depends on."""
    acts = actions(
        [
            {"action_type": "split", "ex_date": "2026-10-08", "split_ratio": 4.0},
            {"action_type": "cash_dividend", "ex_date": "2026-10-07", "cash_amount": 2.04},
        ]
    )
    first = compute_adjustment_factors(FIVE_DAYS, acts, as_of=date(2026, 10, 31))
    second = compute_adjustment_factors(FIVE_DAYS, acts, as_of=date(2026, 10, 31))
    pd.testing.assert_frame_equal(first, second)


def test_adding_a_later_action_does_not_change_an_earlier_as_of_view():
    """The monotonicity property: history as known on date X never moves."""
    base = [{"action_type": "split", "ex_date": "2026-10-07", "split_ratio": 2.0}]
    later = [*base, {"action_type": "split", "ex_date": "2026-10-09", "split_ratio": 3.0}]

    before = compute_adjustment_factors(FIVE_DAYS, actions(base), as_of=date(2026, 10, 8))
    after = compute_adjustment_factors(FIVE_DAYS, actions(later), as_of=date(2026, 10, 8))
    pd.testing.assert_frame_equal(before, after)


def test_without_as_of_a_new_action_does_restate_history():
    """Shows what `as_of` protects against, so the reason is not theoretical."""
    base = [{"action_type": "split", "ex_date": "2026-10-07", "split_ratio": 2.0}]
    later = [*base, {"action_type": "split", "ex_date": "2026-10-09", "split_ratio": 3.0}]

    before = compute_adjustment_factors(FIVE_DAYS, actions(base))
    after = compute_adjustment_factors(FIVE_DAYS, actions(later))
    assert before.loc[date(2026, 10, 5), "price_factor"] == pytest.approx(0.5)
    assert after.loc[date(2026, 10, 5), "price_factor"] == pytest.approx(0.5 / 3)


def test_the_latest_bar_is_always_unadjusted():
    """Back-adjustment anchors on today; the newest bar is the real traded price."""
    acts = actions(
        [
            {"action_type": "split", "ex_date": "2026-10-06", "split_ratio": 3.0},
            {"action_type": "cash_dividend", "ex_date": "2026-10-08", "cash_amount": 1.04},
        ]
    )
    out = adjust_bars(FIVE_DAYS, acts)
    assert out.iloc[-1]["adj_close"] == pytest.approx(out.iloc[-1]["close"])
    assert out.iloc[-1]["price_factor"] == pytest.approx(1.0)


# ── input handling ──────────────────────────────────────────────────────────


def test_unsorted_bars_are_handled():
    shuffled = FIVE_DAYS.iloc[[3, 0, 4, 1, 2]].reset_index(drop=True)
    acts = actions([{"action_type": "split", "ex_date": "2026-10-08", "split_ratio": 4.0}])
    out = adjust_bars(shuffled, acts, mode="split_only")
    by_date = dict(zip(out["session_date"], out["adj_close"], strict=True))
    assert by_date[date(2026, 10, 5)] == pytest.approx(25.0)
    assert by_date[date(2026, 10, 8)] == pytest.approx(106.0)


def test_timestamp_dates_are_accepted():
    frame = FIVE_DAYS.copy()
    frame["session_date"] = pd.to_datetime(frame["session_date"])
    acts = actions([{"action_type": "split", "ex_date": "2026-10-08", "split_ratio": 4.0}])
    out = adjust_bars(frame, acts, mode="split_only")
    assert out.loc[0, "adj_close"] == pytest.approx(25.0)


def test_a_missing_session_date_column_fails_loudly():
    with pytest.raises(AdjustmentError, match="session_date"):
        compute_adjustment_factors(FIVE_DAYS.drop(columns=["session_date"]), NO_ACTIONS)


def test_an_unknown_mode_fails_loudly():
    acts = actions([{"action_type": "split", "ex_date": "2026-10-08", "split_ratio": 2.0}])
    with pytest.raises(AdjustmentError, match="unknown adjustment mode"):
        compute_adjustment_factors(FIVE_DAYS, acts, mode="whatever")


def test_an_empty_bar_series_returns_empty_factors():
    acts = actions([{"action_type": "split", "ex_date": "2026-10-08", "split_ratio": 2.0}])
    assert compute_adjustment_factors(FIVE_DAYS.iloc[0:0], acts).empty


def test_a_spinoff_is_not_yet_applied_as_a_factor():
    """`spinoff` is a valid action_type in the schema but has no factor rule
    here. It must be IGNORED rather than silently treated as a dividend."""
    acts = actions([{"action_type": "spinoff", "ex_date": "2026-10-07", "cash_amount": 5.0}])
    factors = compute_adjustment_factors(FIVE_DAYS, acts, mode="total_return")
    assert (factors["price_factor"] == 1.0).all()
