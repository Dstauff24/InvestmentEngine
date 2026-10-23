"""The outcome tracker (D11/D13/D18/D19). Fixtures only — no DB, no network.

The ambiguous bar is the load-bearing case: a daily bar that touched both the
stop and a target has no sequence, and the ambiguity is NOT symmetric in cost.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from shadow_book.outcome_tracker import attach_trail_ma, evaluate_call, in_call_basis

CALLED = date(2026, 10, 1)


def call(**kw) -> dict:
    base = {
        "id": 1,
        "symbol": "T",
        "tier": "A",
        "recipe": "pullback_to_trend",
        "strategy": "swing",
        "called_on": CALLED,
        "proj_entry_low": 99.5,
        "proj_entry_high": 100.5,
        "proj_stop": 95.0,
        "proj_target_1": 110.0,
        "proj_target_2": 125.0,
        "proj_horizon_days": 15,
        "current_stop": 95.0,
        "stop_to_breakeven_on": None,
        "hit_target_1": None,
        "hit_target_2": None,
        "last_evaluated_on": None,
        "max_favorable_excursion": None,
        "max_adverse_excursion": None,
    }
    base.update(kw)
    return base


def bar(day_offset: int, high: float, low: float, close: float | None = None) -> dict:
    return {
        "session_date": CALLED + timedelta(days=day_offset),
        "open": low,
        "high": high,
        "low": low,
        "close": close if close is not None else (high + low) / 2,
    }


# ── entry ───────────────────────────────────────────────────────────────────


def test_a_plan_fills_when_price_trades_into_the_zone():
    r = evaluate_call(call(), [bar(1, 102, 100.0)], [], as_of=CALLED + timedelta(days=1))
    assert r.actual_entry == pytest.approx(100.0)
    assert r.outcome == "open"
    assert any(c.event == "entered" for c in r.changes)


def test_the_fill_is_taken_at_the_zone_edge_not_the_bars_extreme():
    """A resting order would not get the bar's low; assuming it would flatters
    every entry."""
    r = evaluate_call(call(), [bar(1, 102, 90.0)], [], as_of=CALLED + timedelta(days=1))
    # Low 90 is below the zone, so the fill is the zone's lower edge.
    assert r.actual_entry == pytest.approx(99.5)


def test_a_plan_that_never_fills_is_not_a_loss():
    """Counting unfilled plans as losses would make selectivity look like
    incompetence."""
    bars = [bar(i, 130, 120) for i in range(1, 20)]
    r = evaluate_call(call(), bars, [], as_of=CALLED + timedelta(days=20))
    assert r.actual_entry is None
    assert r.outcome == "never_triggered"


def test_an_unfilled_plan_inside_its_horizon_is_still_open():
    r = evaluate_call(call(), [bar(1, 130, 120)], [], as_of=CALLED + timedelta(days=1))
    assert r.outcome == "open"


# ── THE AMBIGUOUS BAR ───────────────────────────────────────────────────────


def test_a_bar_touching_both_stop_and_target_resolves_to_the_stop():
    """No intrabar sequence, and the cost is asymmetric: assuming the target
    came first flatters every result in exactly the case where a real trade was
    most likely stopped out."""
    bars = [bar(1, 102, 100.0), bar(2, 112, 94.0)]
    r = evaluate_call(call(), bars, [], as_of=CALLED + timedelta(days=2))
    assert r.outcome == "stopped"
    assert r.hit_stop is True


def test_once_the_stop_is_at_breakeven_a_both_bar_is_a_breakeven_exit():
    """TP1 was already paid, so the trade was risk-free — D11 calls
    breakeven-or-better acceptable."""
    bars = [bar(1, 102, 100.0), bar(2, 112, 105.0), bar(3, 108, 99.0)]
    r = evaluate_call(call(), bars, [], as_of=CALLED + timedelta(days=3))
    assert r.hit_target_1 is True
    assert r.outcome == "target_1"
    assert r.hit_stop is False


# ── targets and the trim ────────────────────────────────────────────────────


def test_tp1_moves_the_stop_to_breakeven():
    """D18: this is what zeroes the open heat."""
    bars = [bar(1, 102, 100.0), bar(2, 112, 105.0)]
    r = evaluate_call(call(), bars, [], as_of=CALLED + timedelta(days=2))
    assert r.hit_target_1 is True
    assert r.current_stop == pytest.approx(100.0), "the entry, not the original stop"
    assert r.stop_to_breakeven_on == CALLED + timedelta(days=2)
    assert r.outcome == "open", "the runner is still running"


def test_tp2_closes_the_trade():
    bars = [bar(1, 102, 100.0), bar(2, 130, 120.0)]
    r = evaluate_call(call(), bars, [], as_of=CALLED + timedelta(days=2))
    assert r.outcome == "target_2"
    assert r.hit_target_1 is True and r.hit_target_2 is True
    assert r.r_multiple == pytest.approx((125.0 - 100.0) / (100.0 - 95.0))


def test_the_r_multiple_is_measured_against_the_PROJECTED_stop():
    """R is defined by the risk taken at entry, not by a stop that has since
    moved — otherwise a breakeven exit would read as infinite R."""
    bars = [bar(1, 102, 100.0), bar(2, 130, 120.0)]
    r = evaluate_call(call(), bars, [], as_of=CALLED + timedelta(days=2))
    assert r.r_multiple == pytest.approx(5.0)


def test_a_stop_out_is_a_negative_r():
    bars = [bar(1, 102, 100.0), bar(2, 101, 90.0)]
    r = evaluate_call(call(), bars, [], as_of=CALLED + timedelta(days=2))
    assert r.outcome == "stopped"
    assert r.r_multiple == pytest.approx(-1.0)


# ── D19(2): dividend-adjusted stops ─────────────────────────────────────────


def test_an_ex_dividend_drop_does_not_trigger_the_stop():
    """A stock that goes ex-dividend drops by roughly the dividend, and a stop
    sitting below it has not been hit by anything the thesis cares about."""
    bars = [bar(1, 102, 100.0), bar(2, 100, 94.6)]
    dividends = [{"ex_date": CALLED + timedelta(days=2), "cash_amount": 0.5}]

    without = evaluate_call(call(), bars, [], as_of=CALLED + timedelta(days=2))
    with_div = evaluate_call(call(), bars, dividends, as_of=CALLED + timedelta(days=2))

    assert without.outcome == "stopped", "94.6 is below the 95.0 stop"
    assert with_div.outcome == "open", "the stop moved to 94.5 on the ex-date"
    assert with_div.current_stop == pytest.approx(94.5)


def test_a_real_break_below_the_adjusted_stop_still_stops():
    bars = [bar(1, 102, 100.0), bar(2, 100, 90.0)]
    dividends = [{"ex_date": CALLED + timedelta(days=2), "cash_amount": 0.5}]
    r = evaluate_call(call(), bars, dividends, as_of=CALLED + timedelta(days=2))
    assert r.outcome == "stopped"


# ── timeouts, per the per-strategy exits config ─────────────────────────────


def test_a_swing_times_out_at_its_horizon():
    bars = [bar(1, 102, 100.0)] + [bar(i, 103, 101) for i in range(2, 25)]
    r = evaluate_call(call(), bars, [], as_of=CALLED + timedelta(days=25))
    assert r.outcome == "timeout"
    assert any(c.event == "timeout" for c in r.changes)


def test_a_hold_never_times_out_on_days():
    """D17: holds exit on thesis triggers only, so a day-count clock would fire
    before the thesis had a chance."""
    bars = [bar(1, 102, 100.0)] + [bar(i, 103, 101) for i in range(2, 60)]
    r = evaluate_call(call(strategy="hold"), bars, [], as_of=CALLED + timedelta(days=60))
    assert r.outcome == "open"


def test_an_option_never_times_out_on_days_either():
    """It is bounded by its own expiration; a second clock would double-gate."""
    bars = [bar(1, 102, 100.0)] + [bar(i, 103, 101) for i in range(2, 60)]
    r = evaluate_call(call(strategy="option"), bars, [], as_of=CALLED + timedelta(days=60))
    assert r.outcome == "open"


# ── excursions ──────────────────────────────────────────────────────────────


def test_excursions_record_the_best_and_worst_the_trade_saw():
    bars = [bar(1, 102, 100.0), bar(2, 109, 97.0)]
    r = evaluate_call(call(), bars, [], as_of=CALLED + timedelta(days=2))
    assert r.mfe == pytest.approx(9.0)
    assert r.mae == pytest.approx(-3.0)


def test_prior_excursions_are_carried_forward_not_reset():
    """The tracker runs daily on a widening window; resetting would lose the
    best and worst the trade ever saw."""
    bars = [bar(3, 103, 101.0)]
    r = evaluate_call(
        call(
            actual_entry=100.0,
            actual_entry_at=CALLED + timedelta(days=1),
            max_favorable_excursion=9.0,
            max_adverse_excursion=-3.0,
            last_evaluated_on=CALLED + timedelta(days=2),
        ),
        bars,
        [],
        as_of=CALLED + timedelta(days=3),
    )
    assert r.mfe == pytest.approx(9.0)
    assert r.mae == pytest.approx(-3.0)


# ── the entry deadline ──────────────────────────────────────────────────────


def test_a_plan_cannot_fill_after_its_horizon_has_passed():
    """THE RULE WAS ONLY TRUE BY ACCIDENT.

    `never_triggered` means a plan that waited too long is no longer the plan
    that was called — but nothing enforced it inside this function, so it held
    only because the tracker runs DAILY and the next run would close the call
    before a late fill could happen. Two things break that: a backtest handing
    over a whole forward window at once, and a pipeline that missed a week.
    Both would fill a plan the live engine had already abandoned.
    """
    bars = [bar(i, 140, 130) for i in range(1, 17)]
    bars += [bar(17, 101, 99.0), bar(18, 125, 100.0)]

    r = evaluate_call(call(), bars, [], as_of=CALLED + timedelta(days=18))

    assert r.actual_entry is None, "a dead plan must not fill"
    assert r.outcome == "never_triggered"
    assert r.r_multiple is None


def test_a_fill_on_the_last_day_of_the_horizon_still_counts():
    """The deadline is a boundary, not a safety margin — closing a day early
    would silently shorten every recipe's entry window."""
    bars = [bar(i, 140, 130) for i in range(1, 15)]
    bars.append(bar(15, 101, 99.0))

    r = evaluate_call(call(), bars, [], as_of=CALLED + timedelta(days=15))

    assert r.actual_entry is not None
    assert r.actual_entry_at == CALLED + timedelta(days=15)


# ══════════════════════════════════════════════════════════════════════════
# A split changes the UNITS the stored levels are written in
# ══════════════════════════════════════════════════════════════════════════


def _split(day_offset: int, ratio: float) -> dict:
    return {"ex_date": CALLED + timedelta(days=day_offset), "split_ratio": ratio}


def test_with_no_splits_the_rows_are_untouched():
    """The overwhelmingly common case, and the reason this defect is invisible
    until the day it books a loss that never happened."""
    bars, dividends = in_call_basis(
        [bar(1, 102, 100)], [{"ex_date": CALLED, "cash_amount": 0.5}], []
    )
    assert bars == [bar(1, 102, 100)]
    assert dividends == [{"ex_date": CALLED, "cash_amount": 0.5}]


def test_a_ten_for_one_split_is_not_a_ninety_percent_loss():
    """THE CASE THAT WOULD HAVE POISONED THE SHADOW BOOK. `shadow_book` stores
    absolute levels frozen at the call (D13) and `prices_daily` is raw (D24),
    so after a 10-for-1 split every bar prints a tenth of what the plan was
    written in. Compared straight, the first post-split bar is a catastrophic
    stop-out the market never inflicted — and that row would then count as
    closed evidence under D40's ladder."""
    raw = [bar(1, 102, 100), bar(5, 10.4, 10.2), bar(6, 11.0, 10.5)]
    naive = evaluate_call(call(), raw, [], as_of=CALLED + timedelta(days=6))
    assert naive.outcome == "stopped", "this is what the bug looked like"

    bars, _ = in_call_basis(raw, [], [_split(5, 10.0)])
    rebased = evaluate_call(call(), bars, [], as_of=CALLED + timedelta(days=6))
    assert rebased.outcome == "open"
    assert rebased.hit_stop is not True


def test_a_reverse_split_does_not_book_a_target():
    """The mirror image, and the one that flatters: a 1-for-5 makes every later
    bar print five times the plan's units, which clears TP2 on the first one."""
    raw = [bar(1, 102, 100), bar(5, 515, 505, close=510)]
    naive = evaluate_call(call(), raw, [], as_of=CALLED + timedelta(days=5))
    assert naive.hit_target_2 is True, "this is what the bug looked like"

    bars, _ = in_call_basis(raw, [], [_split(5, 0.2)])
    rebased = evaluate_call(call(), bars, [], as_of=CALLED + timedelta(days=5))
    assert rebased.hit_target_2 is not True
    assert rebased.outcome == "open"


def test_the_ex_date_bar_is_already_priced_the_new_way():
    """Same convention as `features/adjustment.py`: the ex-date bar itself
    trades post-action, so the ratio applies AT the ex-date, not after it."""
    bars, _ = in_call_basis([bar(5, 10.4, 10.2), bar(6, 10.6, 10.3)], [], [_split(5, 10.0)])
    assert bars[0]["high"] == pytest.approx(104.0)
    assert bars[1]["high"] == pytest.approx(106.0)


def test_a_dividend_after_a_split_is_rebased_with_the_bars():
    """D19(2) lowers the stop BY THE DIVIDEND, so an unrebased amount would
    subtract pre-split dollars from a post-split price."""
    _, dividends = in_call_basis(
        [], [{"ex_date": CALLED + timedelta(days=7), "cash_amount": 0.4}], [_split(5, 10.0)]
    )
    assert dividends[0]["cash_amount"] == pytest.approx(4.0)


def test_several_splits_compound():
    bars, _ = in_call_basis([bar(9, 5.0, 4.0)], [], [_split(3, 2.0), _split(6, 5.0)])
    assert bars[0]["high"] == pytest.approx(50.0)


# ══════════════════════════════════════════════════════════════════════════
# The trail, and R measured in legs (O20)
# ══════════════════════════════════════════════════════════════════════════


def trailing_call(**kw) -> dict:
    """A v3 call: no TP2, a 3-day MA trail, confirm on one close."""
    base = call(proj_target_2=None, proj_trail_ma=3, proj_trail_confirm_closes=1)
    base.update(kw)
    return base


def test_the_trail_needs_the_ma_attached_and_computes_it_once():
    """ONE IMPLEMENTATION, because two callers must not disagree: a backtest
    whose MA differed from the tracker's would report an expectancy the Shadow
    Book could never reproduce."""
    rows = attach_trail_ma([bar(i, 100 + i, 100 + i, 100 + i) for i in range(5)], 3)
    assert rows[0]["trail_ma"] is None, "no window behind it yet"
    assert rows[1]["trail_ma"] is None
    assert rows[2]["trail_ma"] == pytest.approx((100 + 101 + 102) / 3)
    assert rows[4]["trail_ma"] == pytest.approx((102 + 103 + 104) / 3)


def test_the_runner_leaves_on_a_close_below_the_trail():
    """v3's defining exit. The trim happened at TP1; the remainder rides until
    price closes under the MA."""
    bars = attach_trail_ma(
        [
            bar(1, 102, 100.0, 101.0),
            bar(2, 112, 108.0, 111.0),  # clears TP1 at 110
            bar(3, 113, 110.0, 112.0),
            bar(4, 113, 109.0, 109.5),  # closes under the rising MA
        ],
        3,
    )
    result = evaluate_call(trailing_call(), bars, [], as_of=CALLED + timedelta(days=4))
    assert result.outcome == "trailed"
    assert result.hit_target_1 is True
    assert result.actual_exit == pytest.approx(109.5)


def test_the_trail_does_not_fire_before_the_trim():
    """Before TP1 the STRUCTURAL stop governs; the trail is the runner's rule,
    and firing it early would exit a position that has not been trimmed."""
    bars = attach_trail_ma(
        [bar(1, 102, 100.0, 101.0), bar(2, 101, 99.0, 99.0), bar(3, 100, 98.5, 98.5)],
        3,
    )
    result = evaluate_call(trailing_call(), bars, [], as_of=CALLED + timedelta(days=3))
    assert result.outcome == "open", "under the MA, but TP1 was never reached"


def test_a_bar_with_no_computable_ma_does_not_exit_the_runner():
    """None means "cannot compute", never "below": exiting on ignorance would
    close the trade on the first bar of every window."""
    bars = [
        dict(bar(1, 102, 100.0, 101.0), trail_ma=None),
        dict(bar(2, 112, 108.0, 111.0), trail_ma=None),
    ]
    result = evaluate_call(trailing_call(), bars, [], as_of=CALLED + timedelta(days=2))
    assert result.hit_target_1 is True, "filled on bar 1, cleared TP1 on bar 2"
    assert result.outcome == "open"


def test_the_confirmation_count_is_honoured():
    """`trail_confirm_closes: 2` asks for a second close under the MA and gives
    back a day to get it."""
    rows = attach_trail_ma(
        [
            bar(1, 102, 100.0, 101.0),  # fills at 100
            bar(2, 112, 108.0, 111.0),  # clears TP1 at 110
            bar(3, 113, 109.0, 109.0),  # first close under the 2-day MA (110.0)
            bar(4, 112, 108.0, 108.0),  # second (MA 108.5)
        ],
        2,
    )
    once = evaluate_call(trailing_call(proj_trail_ma=2), rows, [], as_of=CALLED + timedelta(days=4))
    assert once.outcome == "trailed"
    assert once.actual_exit == pytest.approx(109.0)

    twice = evaluate_call(
        trailing_call(proj_trail_ma=2, proj_trail_confirm_closes=2),
        rows,
        [],
        as_of=CALLED + timedelta(days=4),
    )
    assert twice.outcome == "trailed"
    assert twice.actual_exit == pytest.approx(108.0), "one day later, as asked"


# ── R in legs ───────────────────────────────────────────────────────────────


def test_a_stop_before_the_trim_is_one_leg_and_both_r_numbers_agree():
    """Nothing was trimmed, so the legs are the whole position and the two
    bases say the same thing. The trimmed number must not quietly differ where
    there is no trim."""
    result = evaluate_call(call(), [bar(1, 102, 100.0), bar(2, 101, 94.0)], [], as_of=CALLED)
    assert result.outcome == "stopped"
    assert result.r_multiple == pytest.approx(result.trimmed_r)
    assert result.legs == [(1.0, 95.0, "stopped")]


def test_the_trim_pays_for_the_remainder_when_the_stop_comes_back_to_breakeven():
    """THE CASE THE WHOLE-POSITION NUMBER CANNOT SEE. TP1 at 110 on an entry of
    100 with a 95 stop is +2R on a third; the remainder then stops at breakeven
    for nothing. Whole-position R reads 0.0 — the trade "went nowhere" — while
    the strategy as defined banked a third of a 2R move."""
    bars = [bar(1, 102, 100.0), bar(2, 112, 108.0), bar(3, 101, 99.0)]
    result = evaluate_call(call(), bars, [], as_of=CALLED + timedelta(days=3))
    assert result.outcome == "target_1"
    assert result.r_multiple == pytest.approx(0.0, abs=1e-9)
    assert result.trimmed_r == pytest.approx(0.3333 * 2.0, abs=1e-3)


def test_the_legs_are_measured_on_the_ORIGINAL_risk():
    """The denominator never moves as the stop does. Measured against the
    breakeven stop, the remainder's R would be infinite."""
    bars = [
        bar(1, 102, 100.0, 101.0),  # fills at 100, original risk 100 - 95 = 5
        bar(2, 112, 108.0, 111.0),  # TP1 at 110: a third off at +2R
        bar(3, 120, 118.0, 119.0),  # still above the MA
        bar(4, 119, 111.0, 112.0),  # closes under it: the runner leaves at 112
    ]
    result = evaluate_call(
        trailing_call(proj_trail_ma=2),
        attach_trail_ma(bars, 2),
        [],
        as_of=CALLED + timedelta(days=4),
    )
    assert result.outcome == "trailed"
    assert result.actual_exit == pytest.approx(112.0)

    risk = 100.0 - 95.0
    expected = 0.3333 * (110.0 - 100.0) / risk + (1 - 0.3333) * (112.0 - 100.0) / risk
    assert result.trimmed_r == pytest.approx(expected, abs=1e-3)
    # The whole-position number sees only the final exit.
    assert result.r_multiple == pytest.approx((112.0 - 100.0) / risk)


def test_a_gap_through_both_targets_still_books_the_trim_at_tp1():
    """A bar that clears TP2 outright passed through TP1 on the way, so the
    trim happened at TP1's price — not at TP2's, which would pay the trim a
    price it never got."""
    bars = [bar(1, 102, 100.0), bar(2, 130, 126.0)]
    result = evaluate_call(call(), bars, [], as_of=CALLED + timedelta(days=2))
    assert result.outcome == "target_2"
    assert [leg[2] for leg in result.legs] == ["target_1", "target_2"]
    assert result.legs[0][1] == pytest.approx(110.0)
    assert result.trimmed_r < result.r_multiple, "the trim took a worse price than TP2"


def test_the_accounting_moves_no_exit():
    """The legs are an ACCOUNTING layer over the same resolution: same fills,
    same pessimistic ambiguous bar, same timeout. If this ever changes, every
    past decision's `r_multiple` moves under it."""
    bars = [bar(1, 102, 100.0), bar(2, 112, 94.0)]  # touches TP1 and the stop
    result = evaluate_call(call(), bars, [], as_of=CALLED + timedelta(days=2))
    assert result.outcome == "stopped", "pessimistic, exactly as before"
    assert result.r_multiple == pytest.approx(-1.0)
