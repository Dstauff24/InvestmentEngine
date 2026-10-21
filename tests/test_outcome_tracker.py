"""The outcome tracker (D11/D13/D18/D19). Fixtures only — no DB, no network.

The ambiguous bar is the load-bearing case: a daily bar that touched both the
stop and a target has no sequence, and the ambiguity is NOT symmetric in cost.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from shadow_book.outcome_tracker import evaluate_call

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
