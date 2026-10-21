"""Benchmark symbols: priced, never scored (D17/D35/D39).

    PGDATABASE=ie_test pytest -m integration

The universe (D14) is index CONSTITUENTS, which contains no market series at
all — so "SPY below its 200-day MA" had nothing to read. A benchmark is the
narrow exception: a `tickers` row that carries prices and is excluded from
everything else. The exclusions are what this file pins, because every one of
them is a place where an ETF could leak into a swing recipe.
"""

from __future__ import annotations

from datetime import date

import pytest

from core import db, settings
from ingest.universe import deactivate_absent
from tests.dbguard import assert_safe_database

pytestmark = pytest.mark.integration

BENCH = "ZZSPY"
NORMAL = "ZZCONST"


@pytest.fixture(scope="module", autouse=True)
def _guard_database():
    return assert_safe_database(settings.database_url())


@pytest.fixture(autouse=True)
def seeded():
    assert_safe_database(db.settings.database_url())
    _clean()
    db.execute(
        """
        INSERT INTO tickers (symbol, is_benchmark, is_active, passes_liquidity,
                             sector, last_seen_on)
        VALUES (%s, TRUE, TRUE, FALSE, 'Benchmark', '2020-01-01'),
               (%s, FALSE, TRUE, TRUE, 'Technology', '2020-01-01')
        """,
        (BENCH, NORMAL),
    )
    yield
    _clean()


def _clean() -> None:
    db.execute("DELETE FROM prices_daily WHERE symbol = ANY(%s)", ([BENCH, NORMAL],))
    db.execute("DELETE FROM tickers WHERE symbol = ANY(%s)", ([BENCH, NORMAL],))


# ── the exclusions ──────────────────────────────────────────────────────────


def test_a_benchmark_is_not_in_the_universe_every_job_iterates():
    """`v_active_universe` is the gate: out of it means never scored, never
    chained, never surfaced as an idea."""
    symbols = db.active_symbols()
    assert NORMAL in symbols
    assert BENCH not in symbols


def test_the_price_job_is_the_one_place_they_are_unioned_in():
    assert BENCH in db.benchmark_symbols()
    assert NORMAL not in db.benchmark_symbols()


def test_a_benchmark_is_not_a_sector_member():
    """`passes_liquidity` stays FALSE, so it cannot land in a sector composite
    and be measured against the stocks it is the index for."""
    from features.technical import sector_members

    assert BENCH not in sector_members("Benchmark")


def test_a_benchmark_survives_a_universe_rebuild_that_never_mentions_it():
    """SPY is in no constituents pull, so every rebuild finds it "absent" — and
    deactivating it would take the series D17's risk-off rule reads."""
    deactivated = deactivate_absent({NORMAL}, session_date=date(2026, 10, 21), grace_days=5)

    row = db.fetch_one("SELECT is_active, deactivated_on FROM tickers WHERE symbol = %s", (BENCH,))
    assert row["is_active"] is True
    assert row["deactivated_on"] is None
    assert deactivated == 0, "and the constituent it was told to keep stays too"


def test_a_constituent_absent_past_the_grace_period_still_deactivates():
    """The benchmark exemption must not disable the rule it is an exception to."""
    assert deactivate_absent({BENCH}, session_date=date(2026, 10, 21), grace_days=5) == 1
    row = db.fetch_one("SELECT is_active FROM tickers WHERE symbol = %s", (NORMAL,))
    assert row["is_active"] is False


def test_a_benchmark_can_carry_prices():
    """The whole point: `prices_daily` has a foreign key to `tickers`, which is
    why a benchmark lives there rather than in a table of its own."""
    db.execute(
        """
        INSERT INTO prices_daily (symbol, session_date, ts, open, high, low, close,
                                  volume, source)
        VALUES (%s, '2026-10-21', '2026-10-21 20:00+00', 1, 1, 1, 1, 1, 'test')
        """,
        (BENCH,),
    )
    assert (
        db.fetch_one("SELECT count(*) AS n FROM prices_daily WHERE symbol = %s", (BENCH,))["n"] == 1
    )
