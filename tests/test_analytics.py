"""Per recipe x regime expectancy (D13/D39/D40/D45). Fixtures only.

The arithmetic is the easy part. What is pinned here is the judgement: that a
cell under D39(2)'s floor is reported as NOT EVIDENCE rather than as a smaller
number, that unfilled and open ideas stay out of the statistics, and that
nothing in this module can promote anything.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from shadow_book import analytics
from shadow_book.analytics import MIN_SAMPLE, Cell, verdict_for

CALLED = date(2026, 10, 1)


def row(**kw) -> dict:
    base = {
        "recipe": "post_earnings_drift",
        "regime": "bull_calm",
        "status": "shadow",
        "tier": "A",
        "outcome": "target_2",
        "r_multiple": 2.0,
        "days_held": 6,
        "called_on": CALLED,
        "proj_confidence": 74.0,
        "pillars_scored": 3,
    }
    base.update(kw)
    return base


def cell_with(rows: list[dict]) -> Cell:
    cell = Cell(recipe="post_earnings_drift", regime="bull_calm", status="shadow")
    for item in rows:
        cell.add(item)
    return cell


# ── what counts ─────────────────────────────────────────────────────────────


def test_an_unfilled_plan_is_a_signal_and_not_a_loss():
    """D13. Counting `never_triggered` as a loss would make selectivity look
    like incompetence."""
    cell = cell_with([row(outcome="never_triggered", r_multiple=None)])
    assert (cell.signals, cell.never_triggered, cell.closed) == (1, 1, 0)
    assert cell.expectancy_r is None
    assert cell.hit_rate is None


def test_an_open_idea_is_counted_and_then_excluded_from_every_statistic():
    """An unresolved trade has no R multiple, and counting it either way would
    be a claim the data cannot support."""
    cell = cell_with([row(outcome="open", r_multiple=None), row(r_multiple=1.0)])
    assert cell.open_now == 1
    assert cell.closed == 1
    assert cell.expectancy_r == pytest.approx(1.0)


def test_a_breakeven_scratch_is_neither_a_win_nor_a_loss():
    cell = cell_with([row(outcome="target_1", r_multiple=0.0)])
    assert (cell.wins, cell.losses, cell.scratches) == (0, 0, 1)
    assert cell.hit_rate == 0.0


def test_expectancy_is_the_mean_r_of_closed_ideas():
    cell = cell_with([row(r_multiple=3.0), row(r_multiple=-1.0, outcome="stopped")])
    assert cell.expectancy_r == pytest.approx(1.0)
    assert cell.avg_win_r == pytest.approx(3.0)
    assert cell.avg_loss_r == pytest.approx(-1.0)
    assert cell.profit_factor == pytest.approx(3.0)


def test_profit_factor_is_none_rather_than_infinite_when_nothing_lost():
    assert cell_with([row(r_multiple=1.0)]).profit_factor is None


def test_an_empty_cell_reports_none_not_zero():
    """0.0R means "measured and flat"; None means "nothing measured", and a
    promotion rule must be able to tell them apart."""
    cell = Cell(recipe="r", regime="bull_calm", status="shadow")
    assert cell.expectancy_r is None
    assert cell.hit_rate is None
    assert cell.sufficient is False


# ── D39(2)'s floor, per cell (D45) ──────────────────────────────────────────


def test_a_cell_under_thirty_closed_ideas_is_not_evidence():
    cell = cell_with([row(r_multiple=2.0) for _ in range(MIN_SAMPLE - 1)])
    assert cell.sufficient is False
    verdict = verdict_for(cell)
    assert verdict.rule == "insufficient"
    assert verdict.actionable is False
    assert "floor" in verdict.detail


def test_the_floor_is_checked_before_the_expectancy_so_a_tiny_sample_cannot_promote():
    """THE ORDER IS THE POINT. Eight closed ideas at +2.0R is not a promotion
    candidate with a small sample; under D39(2) it is not evidence at all."""
    cell = cell_with([row(r_multiple=2.0) for _ in range(8)])
    assert verdict_for(cell).rule == "insufficient"


def test_a_sufficient_positive_shadow_cell_reads_as_a_promotion_candidate():
    cell = cell_with([row(r_multiple=0.5) for _ in range(MIN_SAMPLE)])
    verdict = verdict_for(cell)
    assert verdict.rule == "promote"
    assert "shadow->paper" in verdict.detail


def test_a_sufficient_negative_cell_reads_as_a_demotion():
    cell = cell_with([row(r_multiple=-0.5, outcome="stopped") for _ in range(MIN_SAMPLE)])
    assert verdict_for(cell).rule == "demote"


def test_a_thin_positive_shadow_cell_holds_rather_than_promoting():
    cell = cell_with([row(r_multiple=0.05) for _ in range(MIN_SAMPLE)])
    verdict = verdict_for(cell)
    assert verdict.rule == "hold"
    assert "under shadow->paper" in verdict.detail


def test_a_paper_cell_needs_the_sustained_window_as_well_as_the_expectancy():
    rows = [
        row(status="paper", r_multiple=0.5, called_on=CALLED + timedelta(days=i))
        for i in range(MIN_SAMPLE)
    ]
    cell = Cell(recipe="post_earnings_drift", regime="bull_calm", status="paper")
    for item in rows:
        cell.add(item)

    verdict = verdict_for(cell)
    assert verdict.rule == "hold", "30 days of evidence is not the 90 paper->live wants"
    assert "window" in verdict.detail


def test_a_paper_promotion_says_calibration_is_not_checked_here():
    """D39(1)'s curve is a separate claim, and a verdict that stayed silent
    about it would read as though the gate had been fully satisfied."""
    cell = Cell(recipe="post_earnings_drift", regime="bull_calm", status="paper")
    for i in range(MIN_SAMPLE):
        cell.add(row(status="paper", r_multiple=0.5, called_on=CALLED + timedelta(days=i * 5)))

    verdict = verdict_for(cell)
    assert verdict.rule == "promote"
    assert "CALIBRATION" in verdict.detail


# ── grouping ────────────────────────────────────────────────────────────────


def test_cells_are_keyed_on_recipe_regime_and_ladder_state(monkeypatch):
    """D45's unit of judgement. Mixing regimes averages two strategies, and
    mixing ladder states makes the promotion rule circular (D40)."""
    rows = [
        row(),
        row(regime="bear_volatile"),
        row(recipe="pullback_to_trend"),
        row(status="paper"),
    ]
    monkeypatch.setattr(analytics.db, "fetch_all", lambda sql, params: rows)

    keys = {c.key for c in analytics.cells(as_of=CALLED)}
    assert keys == {
        ("post_earnings_drift", "bull_calm", "shadow"),
        ("post_earnings_drift", "bear_volatile", "shadow"),
        ("pullback_to_trend", "bull_calm", "shadow"),
        ("post_earnings_drift", "bull_calm", "paper"),
    }


def test_a_call_with_no_regime_groups_as_unknown_rather_than_being_dropped(monkeypatch):
    """Before the benchmark has 200 sessions there is no regime, and dropping
    those calls would quietly shrink the evidence."""
    monkeypatch.setattr(analytics.db, "fetch_all", lambda sql, params: [row(regime="unknown")])
    assert [c.regime for c in analytics.cells(as_of=CALLED)] == ["unknown"]


def test_the_roll_up_pools_regimes_and_is_printed_below_them(monkeypatch):
    rows = [row(r_multiple=2.0), row(regime="bear_volatile", r_multiple=-1.0, outcome="stopped")]
    monkeypatch.setattr(analytics.db, "fetch_all", lambda sql, params: rows)

    items = analytics.cells(as_of=CALLED)
    rolled = analytics.totals(items)
    assert rolled["post_earnings_drift"].closed == 2
    assert rolled["post_earnings_drift"].expectancy_r == pytest.approx(0.5)

    text = analytics.render(items)
    assert text.index("per recipe x regime") < text.index("ROLL-UP ACROSS REGIMES")
    assert "context only" in text


# ── the report ──────────────────────────────────────────────────────────────


def test_an_empty_book_says_what_the_absence_means(monkeypatch):
    monkeypatch.setattr(analytics.db, "fetch_all", lambda sql, params: [])
    text = analytics.render(analytics.cells(as_of=CALLED))
    assert "No Shadow Book calls yet" in text
    assert "`shadow`" in text


def test_the_report_marks_every_insufficient_cell(monkeypatch):
    monkeypatch.setattr(analytics.db, "fetch_all", lambda sql, params: [row()])
    text = analytics.render(analytics.cells(as_of=CALLED))
    assert f"n<{MIN_SAMPLE}" in text
    assert "NOT EVIDENCE" in text


def test_nothing_here_writes_a_status():
    """A promotion is a one-line config diff a human makes (D40). A bug in this
    module must not be able to promote a strategy."""
    source = __import__("pathlib").Path(analytics.__file__).read_text()
    for forbidden in ("UPDATE ", "INSERT ", "DELETE ", "db.execute"):
        assert forbidden not in source, f"analytics must not write: {forbidden!r}"
