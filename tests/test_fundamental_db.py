"""`features/fundamental.py`'s loading layer, against a real database.

    PGDATABASE=ie_test pytest -m integration

What a fixture cannot establish: that `as_of` filters on the FILING date (a
quarter is not knowable until it is published, and the SQL is where that rule
lives), that snapshots come back ordered, and that a real two-snapshot
revision — the shape the engine actually had on 2026-10-21 — measures
correctly end to end.
"""

from __future__ import annotations

from datetime import date, timedelta

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


def add_quarter(
    end: date, revenue: float, eps: float, margin: float, *, filed: date | None = None
) -> None:
    """A quarter, with the date it became PUBLIC.

    `filed` defaults to 30 days after the period end because that is the shape
    of the real data (AAPL's June-2026 quarter was filed 2026-07-31), and every
    read keys on it — a fixture that left it NULL would be a quarter nobody
    ever published.
    """
    db.execute(
        """
        INSERT INTO fundamentals (symbol, period_type, fiscal_date_ending, filing_date,
                                  revenue, eps_diluted, gross_margin, source)
        VALUES (%s, 'quarter', %s, %s, %s, %s, %s, 'test')
        ON CONFLICT (symbol, period_type, fiscal_date_ending) DO UPDATE
        SET revenue = EXCLUDED.revenue, filing_date = EXCLUDED.filing_date
        """,
        (SYMBOL, end, filed or end + timedelta(days=30), revenue, eps, margin),
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


def test_as_of_filters_on_the_filing_date_not_the_fiscal_period():
    """THE LOOKAHEAD THE BACKTEST FOUND.

    A quarter ending March 31 is filed weeks later. Keyed on the period end, a
    run standing on April 5 read numbers nobody had published — and the only
    symptom would have been a backtest that looked better than the strategy.
    Same rule as `insider_transactions.filed_at`: a fact is a fact when it is
    published.
    """
    add_quarter(date(2025, 3, 31), 100.0, 1.0, 40.0, filed=date(2025, 4, 30))

    assert fun.load_fundamentals(SYMBOL, as_of=date(2025, 4, 5)) == []
    rows = fun.load_fundamentals(SYMBOL, as_of=date(2025, 4, 30))
    assert len(rows) == 1
    assert rows[0]["fiscal_date_ending"] == date(2025, 3, 31)
    assert rows[0]["known_on"] == date(2025, 4, 30)


def test_a_quarter_with_no_filing_date_is_excluded_rather_than_dated_by_guesswork():
    """It cannot be read point-in-time, and inventing a lag would invent the
    very fact the filter exists to check."""
    db.execute(
        """
        INSERT INTO fundamentals (symbol, period_type, fiscal_date_ending, revenue, source)
        VALUES (%s, 'quarter', %s, 100.0, 'test')
        """,
        (SYMBOL, date(2025, 3, 31)),
    )
    assert fun.load_fundamentals(SYMBOL, as_of=date(2026, 12, 31)) == []


def test_the_accepted_timestamp_stands_in_for_a_missing_filing_date():
    db.execute(
        """
        INSERT INTO fundamentals (symbol, period_type, fiscal_date_ending,
                                  accepted_date, revenue, source)
        VALUES (%s, 'quarter', %s, '2025-04-30 18:05+00', 100.0, 'test')
        """,
        (SYMBOL, date(2025, 3, 31)),
    )
    assert len(fun.load_fundamentals(SYMBOL, as_of=date(2025, 4, 30))) == 1
    assert fun.load_fundamentals(SYMBOL, as_of=date(2025, 4, 29)) == []


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


# ── D48: our own pre-report consensus ───────────────────────────────────────


def test_our_own_pre_report_estimate_is_the_latest_snapshot_before_the_report():
    """Point-in-time by construction: a row this engine wrote on a date.

    The vendor's `epsEstimated` for a past quarter is mutable (one carried
    `lastUpdated` sixteen months after the report), so it cannot be verified as
    the consensus that stood before the print. This can.
    """
    from features.catalyst import own_pre_report_estimate

    period = date(2026, 9, 30)
    add_snapshot(date(2026, 10, 1), period, 1.00)
    add_snapshot(date(2026, 10, 15), period, 1.10)
    # Taken AFTER the report — must never be the one chosen.
    add_snapshot(date(2026, 10, 25), period, 1.25)

    assert own_pre_report_estimate(SYMBOL, reported_on=date(2026, 10, 20)) == pytest.approx(1.10)


def test_a_report_before_the_snapshot_series_started_has_no_own_estimate():
    """D7: the series begins 2026-10-21 and no backfill can buy it, so every
    historical quarter falls back to the vendor figure — labelled as such."""
    from features.catalyst import own_pre_report_estimate

    add_snapshot(date(2026, 10, 21), date(2026, 12, 31), 2.00)
    assert own_pre_report_estimate(SYMBOL, reported_on=date(2025, 1, 30)) is None


def test_a_snapshot_for_a_period_that_had_not_ended_is_not_used():
    """The estimate for NEXT quarter is not the consensus for the one being
    reported."""
    from features.catalyst import own_pre_report_estimate

    add_snapshot(date(2026, 10, 15), date(2027, 3, 31), 3.00)
    assert own_pre_report_estimate(SYMBOL, reported_on=date(2026, 10, 20)) is None
