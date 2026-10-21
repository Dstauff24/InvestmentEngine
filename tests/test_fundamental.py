"""Pillar B measurements (D32 weight 30). Fixtures only — no DB, no network.

The revision-momentum section is the important one: it is the engine's first
feature computed from a series we accumulate ourselves, which began
2026-10-21, and it must be CORRECT AT TWO SNAPSHOTS and TRUSTWORTHY AT SIXTY.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from features import fundamental as fun
from features.evidence import EvidenceDepth, depth_from_dates

# ── helpers ─────────────────────────────────────────────────────────────────


def quarter(end: date, **fields) -> dict:
    row = {
        "symbol": "TEST",
        "fiscal_date_ending": end,
        "fiscal_period": "Q1",
        "revenue": None,
        "eps_diluted": None,
        "gross_margin": None,
        "operating_margin": None,
        "net_margin": None,
        "roe": None,
        "roic": None,
        "debt_to_equity": None,
    }
    row.update(fields)
    return row


def quarters(values: list[float], *, field: str = "revenue", start_year: int = 2024) -> list[dict]:
    """One row per quarter, oldest first, stepping a quarter at a time."""
    out = []
    year, month = start_year, 3
    for value in values:
        out.append(quarter(date(year, month, 28), **{field: value}))
        month += 3
        if month > 12:
            month -= 12
            year += 1
    return out


def snapshot(snap: date, period_end: date, eps: float | None, **extra) -> dict:
    row = {
        "snapshot_date": snap,
        "fiscal_period_end": period_end,
        "estimated_eps_avg": eps,
        "estimated_revenue_avg": None,
        "analyst_count_eps": 12,
        "analyst_count_revenue": 10,
    }
    row.update(extra)
    return row


# ══════════════════════════════════════════════════════════════════════════
# Growth
# ══════════════════════════════════════════════════════════════════════════


def test_yoy_growth_compares_the_same_quarter_a_year_earlier():
    """Not sequential: almost every business here is seasonal, so Q4-to-Q1
    measures the calendar rather than the company."""
    rows = quarters([100.0, 200.0, 300.0, 400.0, 110.0])
    assert fun.growth_yoy(rows, "revenue") == pytest.approx(10.0)


def test_yoy_growth_needs_a_year_ago_quarter():
    assert fun.growth_yoy(quarters([100.0, 110.0, 120.0, 130.0]), "revenue") is None
    assert fun.growth_yoy(quarters([100.0, 110.0, 120.0, 130.0, 140.0]), "revenue") is not None


def test_a_zero_base_has_no_percent_change():
    rows = quarters([0.0, 1.0, 2.0, 3.0, 50.0])
    assert fun.growth_yoy(rows, "revenue") is None


def test_a_negative_base_is_refused_rather_than_mis_signed():
    """EPS from -1.00 to -0.50 is an IMPROVEMENT; the naive formula calls it
    -50%, which reads as a collapse. Sign-flipping bases are common in EPS."""
    rows = quarters([-1.00, 0.1, 0.2, 0.3, -0.50], field="eps_diluted")
    assert fun.growth_yoy(rows, "eps_diluted") is None


def test_acceleration_is_the_change_in_the_growth_rate():
    """20% after 10% is a different proposition from 20% after 40%, and the
    level alone cannot tell them apart."""
    # year 1: 100 each quarter. year 2: the last two grow 10% then 20%.
    rows = quarters([100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 110.0, 120.0])
    # latest YoY = 120/100 = +20%; prior YoY = 110/100 = +10%; acceleration +10pp
    assert fun.growth_acceleration(rows, "revenue") == pytest.approx(10.0)


def test_deceleration_is_negative():
    rows = quarters([100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 140.0, 120.0])
    assert fun.growth_acceleration(rows, "revenue") == pytest.approx(-20.0)


def test_acceleration_needs_two_years_of_quarters():
    rows = quarters([100.0] * 5)
    assert fun.growth_acceleration(rows, "revenue") is None
    assert fun.growth_acceleration(quarters([100.0] * 6), "revenue") is not None


def test_rows_out_of_order_are_sorted_not_trusted():
    """Provider payloads arrive newest-first as often as not."""
    rows = quarters([100.0, 200.0, 300.0, 400.0, 110.0])
    assert fun.growth_yoy(list(reversed(rows)), "revenue") == pytest.approx(10.0)


# ══════════════════════════════════════════════════════════════════════════
# Margins
# ══════════════════════════════════════════════════════════════════════════


def test_an_expanding_margin_is_positive():
    rows = quarters([40.0, 40.0, 40.0, 40.0, 45.0], field="gross_margin")
    assert fun.margin_trend(rows, "gross_margin", window=4) == pytest.approx(5.0)


def test_a_contracting_margin_is_negative():
    rows = quarters([40.0, 40.0, 40.0, 40.0, 35.0], field="gross_margin")
    assert fun.margin_trend(rows, "gross_margin", window=4) == pytest.approx(-5.0)


def test_the_margin_baseline_excludes_the_latest_quarter():
    """Including the newest value in its own baseline dilutes the change,
    exactly as it does for relative volume."""
    rows = quarters([40.0, 40.0, 40.0, 40.0, 50.0], field="gross_margin")
    # prior window mean is 40, so the trend is +10 — not +8 (which is what
    # including the latest in a 5-wide baseline would give).
    assert fun.margin_trend(rows, "gross_margin", window=4) == pytest.approx(10.0)


def test_a_missing_margin_in_the_window_yields_none():
    """A gap silently skipped would average a shorter window than claimed."""
    rows = quarters([40.0, None, 40.0, 40.0, 45.0], field="gross_margin")
    assert fun.margin_trend(rows, "gross_margin", window=4) is None


def test_margin_trend_needs_a_full_prior_window():
    rows = quarters([40.0] * 4, field="gross_margin")
    assert fun.margin_trend(rows, "gross_margin", window=4) is None


# ══════════════════════════════════════════════════════════════════════════
# Revision momentum — correct at 2, trustworthy at 60
# ══════════════════════════════════════════════════════════════════════════

PERIOD = date(2027, 3, 31)


def test_revision_is_correct_at_two_snapshots():
    """The real 2026-10-21 shape: eps 2.00 -> 2.39 across two snapshots.

    Two snapshots is the thinnest a real measurement can be, and it IS a real
    measurement: consensus moved exactly that much, as a matter of record.
    Suppressing it would discard a true observation.
    """
    snaps = [
        snapshot(date(2026, 10, 21), PERIOD, 2.00),
        snapshot(date(2026, 10, 28), PERIOD, 2.39),
    ]
    r = fun.revision_momentum(snaps, as_of=date(2026, 10, 28))

    assert r.eps_pct == pytest.approx(19.5)
    assert r.eps_first == pytest.approx(2.00)
    assert r.eps_last == pytest.approx(2.39)
    assert r.depth.observations == 2
    assert r.depth.span_days == 7
    assert r.depth.is_minimal is True, "the flag that says this is as thin as real gets"


def test_revision_reports_depth_that_grows_with_the_series():
    """Trustworthy at sixty: same value, far more evidence behind it."""
    start = date(2026, 10, 21)
    snaps = [snapshot(start + timedelta(days=i), PERIOD, 2.00 + 0.0065 * i) for i in range(60)]
    r = fun.revision_momentum(snaps, as_of=start + timedelta(days=59), window_days=None)

    assert r.eps_pct == pytest.approx(19.17, abs=0.2)
    assert r.depth.observations == 60
    assert r.depth.span_days == 59
    assert r.depth.is_minimal is False
    assert r.depth.meets(observations=30, span_days=30) is True


def test_the_value_is_the_same_at_two_and_sixty_only_the_depth_differs():
    """THE design point: depth is reported BESIDE the value, not folded into it.

    A feature that quietly shrank its own number on thin evidence would be
    making a scoring decision, and the scoring layer owns that.
    """
    thin = fun.revision_momentum(
        [snapshot(date(2026, 10, 21), PERIOD, 2.00), snapshot(date(2026, 12, 20), PERIOD, 2.20)],
        as_of=date(2026, 12, 20),
    )
    thick = fun.revision_momentum(
        [
            snapshot(date(2026, 10, 21) + timedelta(days=i), PERIOD, 2.00 + 0.2 * i / 59)
            for i in range(60)
        ],
        as_of=date(2026, 12, 19),
    )
    assert thin.eps_pct == pytest.approx(10.0)
    assert thick.eps_pct == pytest.approx(10.0)
    assert thin.depth.observations == 2
    assert thick.depth.observations == 60


def test_one_snapshot_is_a_level_not_a_change():
    """But depth still says the series has STARTED, which differs from never."""
    r = fun.revision_momentum(
        [snapshot(date(2026, 10, 21), PERIOD, 2.00)], as_of=date(2026, 10, 21)
    )
    assert r.eps_pct is None
    assert r.eps_last == pytest.approx(2.00), "the level is knowable and reported"
    assert r.depth.observations == 1
    assert r.depth.is_empty is False


def test_no_snapshots_at_all_is_empty_depth():
    r = fun.revision_momentum([], as_of=date(2026, 10, 21))
    assert r.eps_pct is None
    assert r.depth.is_empty is True
    assert r.fiscal_period_end is None


def test_an_already_reported_period_is_not_a_forecast_change():
    """A revision to a quarter that has ended is bookkeeping, not news."""
    past = date(2026, 9, 30)
    snaps = [
        snapshot(date(2026, 10, 21), past, 2.00),
        snapshot(date(2026, 10, 28), past, 2.39),
    ]
    r = fun.revision_momentum(snaps, as_of=date(2026, 10, 28))
    assert r.eps_pct is None
    assert r.depth.is_empty is True


def test_the_nearest_upcoming_period_is_the_one_measured():
    """Averaging next quarter with FY+2 blends a tradeable signal into a
    forecast, so one period is chosen — the nearest unreported."""
    near, far = date(2027, 3, 31), date(2028, 3, 31)
    snaps = [
        snapshot(date(2026, 10, 21), near, 2.00),
        snapshot(date(2026, 10, 28), near, 2.10),
        snapshot(date(2026, 10, 21), far, 5.00),
        snapshot(date(2026, 10, 28), far, 9.00),  # a huge revision, ignored
    ]
    r = fun.revision_momentum(snaps, as_of=date(2026, 10, 28))
    assert r.fiscal_period_end == near
    assert r.eps_pct == pytest.approx(5.0)


def test_a_downward_revision_is_negative():
    snaps = [
        snapshot(date(2026, 10, 21), PERIOD, 2.00),
        snapshot(date(2026, 10, 28), PERIOD, 1.80),
    ]
    assert fun.revision_momentum(snaps, as_of=date(2026, 10, 28)).eps_pct == pytest.approx(-10.0)


def test_a_negative_consensus_base_is_refused():
    """A loss narrowing is an improvement, and the naive formula mis-signs it."""
    snaps = [
        snapshot(date(2026, 10, 21), PERIOD, -1.00),
        snapshot(date(2026, 10, 28), PERIOD, -0.50),
    ]
    assert fun.revision_momentum(snaps, as_of=date(2026, 10, 28)).eps_pct is None


def test_the_window_bounds_how_far_back_a_revision_looks():
    """A snapshot from before a guidance reset must not read as recent news."""
    snaps = [
        snapshot(date(2026, 1, 5), PERIOD, 1.00),  # ancient
        snapshot(date(2026, 10, 21), PERIOD, 2.00),
        snapshot(date(2026, 10, 28), PERIOD, 2.10),
    ]
    bounded = fun.revision_momentum(snaps, as_of=date(2026, 10, 28), window_days=90)
    assert bounded.eps_pct == pytest.approx(5.0)
    assert bounded.depth.observations == 2

    unbounded = fun.revision_momentum(snaps, as_of=date(2026, 10, 28), window_days=None)
    assert unbounded.eps_pct == pytest.approx(110.0)
    assert unbounded.depth.observations == 3


def test_a_window_that_excludes_everything_falls_back_rather_than_going_silent():
    """Stale data with visible depth beats silence."""
    snaps = [
        snapshot(date(2026, 1, 5), PERIOD, 1.00),
        snapshot(date(2026, 1, 12), PERIOD, 1.10),
    ]
    r = fun.revision_momentum(snaps, as_of=date(2026, 10, 28), window_days=30)
    assert r.eps_pct == pytest.approx(10.0)
    assert r.depth.span_days == 7
    assert r.depth.last == date(2026, 1, 12), "the staleness is visible in the depth"


def test_analyst_count_is_breadth_not_depth():
    """Two analysts revising is not thirty; both are separate from time depth."""
    snaps = [
        snapshot(date(2026, 10, 21), PERIOD, 2.00, analyst_count_eps=2),
        snapshot(date(2026, 10, 28), PERIOD, 2.39, analyst_count_eps=3),
    ]
    r = fun.revision_momentum(snaps, as_of=date(2026, 10, 28))
    assert r.analyst_count_eps == 3, "the latest breadth, not the first"
    assert r.depth.observations == 2


def test_duplicate_snapshot_dates_count_once():
    """Re-running a day's ingest must not inflate apparent evidence."""
    snaps = [
        snapshot(date(2026, 10, 21), PERIOD, 2.00),
        snapshot(date(2026, 10, 21), PERIOD, 2.00),
        snapshot(date(2026, 10, 28), PERIOD, 2.39),
    ]
    assert fun.revision_momentum(snaps, as_of=date(2026, 10, 28)).depth.observations == 2


# ══════════════════════════════════════════════════════════════════════════
# EvidenceDepth itself
# ══════════════════════════════════════════════════════════════════════════


def test_depth_distinguishes_count_from_span():
    """Ten snapshots in a week and ten over a year carry different weight."""
    week = depth_from_dates([date(2026, 10, 21) + timedelta(days=i) for i in range(10)])
    year = depth_from_dates([date(2026, 1, 1) + timedelta(days=40 * i) for i in range(10)])
    assert week.observations == year.observations == 10
    assert week.span_days == 9
    assert year.span_days == 360


def test_depth_meets_is_the_callers_bar_not_the_features():
    d = depth_from_dates([date(2026, 10, 21) + timedelta(days=i) for i in range(30)])
    assert d.meets(observations=30) is True
    assert d.meets(observations=31) is False
    assert d.meets(observations=10, span_days=60) is False


def test_empty_depth_reads_clearly():
    assert str(depth_from_dates([])) == "no observations"
    assert EvidenceDepth(observations=0, span_days=0).is_empty is True


# ══════════════════════════════════════════════════════════════════════════
# The bundle
# ══════════════════════════════════════════════════════════════════════════


def test_compute_fills_what_it_can_and_nones_the_rest():
    rows = quarters([100.0, 100.0, 100.0, 100.0, 120.0])
    for row in rows:
        row["gross_margin"] = 40.0
    features = fun.compute("TEST", rows, as_of=date(2026, 10, 28))

    assert features.revenue_growth_yoy == pytest.approx(20.0)
    assert features.gross_margin == pytest.approx(40.0)
    assert features.revenue_acceleration is None, "only five quarters"
    assert features.eps_growth_yoy is None, "no eps in these rows"
    assert features.revision.depth.is_empty is True
    assert features.quarters_available == 5


def test_compute_on_nothing_does_not_raise():
    features = fun.compute("TEST", [], as_of=date(2026, 10, 28))
    assert features.fiscal_date_ending is None
    assert features.quarters_available == 0
    assert features.revision.depth.is_empty is True


def test_as_dict_flattens_the_nested_depth_for_logging():
    features = fun.compute("TEST", quarters([100.0] * 8), as_of=date(2026, 10, 28))
    out = features.as_dict()
    assert isinstance(out["revision"], dict)
    assert isinstance(out["revision"]["depth"], dict)
    assert "observations" in out["revision"]["depth"]
