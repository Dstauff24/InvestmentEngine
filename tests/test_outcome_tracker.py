"""The outcome tracker (D11/D13/D18/D19). Fixtures only — no DB, no network.

The ambiguous bar is the load-bearing case: a daily bar that touched both the
stop and a target has no sequence, and the ambiguity is NOT symmetric in cost.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from shadow_book.outcome_tracker import evaluate_call, in_call_basis

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
