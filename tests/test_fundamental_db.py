"""`features/fundamental.py`'s loading layer, against a real database.

    PGDATABASE=ie_test pytest -m integration

What a fixture cannot establish: that `as_of` filters on FISCAL PERIOD rather
than on ingest time (the honest reading for a point-in-fact table), that
snapshots come back ordered, and that a real two-snapshot revision — the shape
the engine actually had on 2026-10-21 — measures correctly end to end.
"""

from __future__ import annotations

from datetime import date

import pytest

from core import db, settings
from features import fundamental as fun
from tests.dbguard import assert_safe_database

pytestmark = pytest.mark.integration

SYMBOL = "ZZFUND"


@pytest.fixture(scope="module", autouse=True)
def _guard_database():
    return assert_safe_database(settings.database_url())


@pytest.fixture(autouse=True)
def seeded():
    assert_safe_database(db.settings.database_url())
    _clean()
    db.execute("INSERT INTO tickers (symbol, is_active) VALUES (%s, TRUE)", (SYMBOL,))
    yield
    _clean()


def _clean() -> None:
    db.execute("DELETE FROM estimate_snapshots WHERE symbol = %s", (SYMBOL,))
    db.execute("DELETE FROM fundamentals WHERE symbol = %s", (SYMBOL,))
    db.execute("DELETE FROM tickers WHERE symbol = %s", (SYMBOL,))


def add_quarter(end: date, revenue: float, eps: float, margin: float) -> None:
    db.execute(
        """
        INSERT INTO fundamentals (symbol, period_type, fiscal_date_ending, revenue,
                                  eps_diluted, gross_margin, source)
        VALUES (%s, 'quarter', %s, %s, %s, %s, 'test')
        ON CONFLICT (symbol, period_type, fiscal_date_ending) DO UPDATE
        SET revenue = EXCLUDED.revenue
        """,
        (SYMBOL, end, revenue, eps, margin),
    )


def add_snapshot(snap: date, period_end: date, eps: float, analysts: int = 12) -> None:
    db.execute(
        """
        INSERT INTO estimate_snapshots (symbol, snapshot_date, period_type,
                                        fiscal_period_end, estimated_eps_avg,
                                        analyst_count_eps, source)
        VALUES (%s, %s, 'quarter', %s, %s, %s, 'test')
        ON CONFLICT (symbol, snapshot_date, period_type, fiscal_period_end) DO UPDATE
        SET estimated_eps_avg = EXCLUDED.estimated_eps_avg
        """,
        (SYMBOL, snap, period_end, eps, analysts),
    )


def test_fundamentals_load_oldest_first():
    for i, end in enumerate(
        [date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31)]
    ):
        add_quarter(end, 100.0 + i, 1.0, 40.0)

    rows = fun.load_fundamentals(SYMBOL, as_of=date(2026, 12, 31))
    assert [r["fiscal_date_ending"] for r in rows] == sorted(r["fiscal_date_ending"] for r in rows)


def test_as_of_filters_on_the_fiscal_period_not_the_ingest_time():
    """A backtest standing on 2025-07-01 should see the quarters that had
    ENDED, not the ones we happen to hold rows for now."""
    add_quarter(date(2025, 3, 31), 100.0, 1.0, 40.0)
    add_quarter(date(2025, 12, 31), 200.0, 2.0, 45.0)

    rows = fun.load_fundamentals(SYMBOL, as_of=date(2025, 7, 1))
    assert len(rows) == 1
    assert rows[0]["fiscal_date_ending"] == date(2025, 3, 31)


def test_a_real_two_snapshot_revision_measures_end_to_end():
    """The exact shape the engine had on 2026-10-21: eps 2.00 -> 2.39."""
    period = date(2027, 3, 31)
    add_snapshot(date(2026, 10, 21), period, 2.00)
    add_snapshot(date(2026, 10, 28), period, 2.39)

    snaps = fun.load_estimate_snapshots(SYMBOL, as_of=date(2026, 10, 28))
    r = fun.revision_momentum(snaps, as_of=date(2026, 10, 28))

    assert r.eps_pct == pytest.approx(19.5)
    assert r.depth.observations == 2
    assert r.depth.span_days == 7
    assert r.depth.first == date(2026, 10, 21)
    assert r.depth.last == date(2026, 10, 28)


def test_as_of_excludes_later_snapshots_so_a_backtest_is_reproducible():
    period = date(2027, 3, 31)
    add_snapshot(date(2026, 10, 21), period, 2.00)
    add_snapshot(date(2026, 10, 28), period, 2.39)

    early = fun.load_estimate_snapshots(SYMBOL, as_of=date(2026, 10, 21))
    assert len(early) == 1
    assert fun.revision_momentum(early, as_of=date(2026, 10, 21)).eps_pct is None


def test_compute_combines_both_sources():
    for i, end in enumerate(
        [
            date(2025, 3, 31),
            date(2025, 6, 30),
            date(2025, 9, 30),
            date(2025, 12, 31),
            date(2026, 3, 31),
        ]
    ):
        add_quarter(end, 100.0 if i < 4 else 120.0, 1.0, 40.0)
    period = date(2027, 3, 31)
    add_snapshot(date(2026, 10, 21), period, 2.00)
    add_snapshot(date(2026, 10, 28), period, 2.39)

    as_of = date(2026, 10, 28)
    features = fun.compute(
        SYMBOL,
        fun.load_fundamentals(SYMBOL, as_of=as_of),
        fun.load_estimate_snapshots(SYMBOL, as_of=as_of),
        as_of=as_of,
    )

    assert features.revenue_growth_yoy == pytest.approx(20.0)
    assert features.revision.eps_pct == pytest.approx(19.5)
    assert features.revision.depth.observations == 2
    assert features.quarters_available == 5


def test_a_symbol_with_nothing_stored_abstains_everywhere():
    features = fun.compute(
        "NOSUCHSYMBOL",
        fun.load_fundamentals("NOSUCHSYMBOL", as_of=date(2026, 10, 28)),
        fun.load_estimate_snapshots("NOSUCHSYMBOL", as_of=date(2026, 10, 28)),
        as_of=date(2026, 10, 28),
    )
    assert features.revenue_growth_yoy is None
    assert features.revision.depth.is_empty is True
    assert features.quarters_available == 0
