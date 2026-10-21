"""`features/technical.py`'s loading layer, against a real database.

    PGDATABASE=ie_test pytest -m integration

What a fixture cannot establish: that `load_bars` really returns bars oldest
first (every reading treats the last row as today), that `as_of` really
excludes later bars AND later corporate actions (D24 reproducibility), and that
split adjustment is applied while dividends are not.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from core import db, settings
from features import technical as tech
from tests.dbguard import assert_safe_database

pytestmark = pytest.mark.integration

SYMBOL = "ZZTEST"
START = date(2026, 1, 5)


@pytest.fixture(scope="module", autouse=True)
def _guard_database():
    return assert_safe_database(settings.database_url())


@pytest.fixture(autouse=True)
def seeded():
    assert_safe_database(db.settings.database_url())
    _clean()
    db.execute(
        "INSERT INTO tickers (symbol, sector, is_active, passes_liquidity, avg_volume_20d) "
        "VALUES (%s, 'Technology', TRUE, TRUE, 5000000)",
        (SYMBOL,),
    )
    rows = []
    day = START
    for i in range(60):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        price = 100.0 + i
        rows.append((SYMBOL, day, price, price * 1.01, price * 0.99, price, 1_000_000))
        day += timedelta(days=1)
    db.executemany(
        """
        INSERT INTO prices_daily (symbol, session_date, ts, open, high, low, close, volume, source)
        VALUES (%s, %s, %s::timestamptz, %s, %s, %s, %s, %s, 'test')
        ON CONFLICT (symbol, ts) DO UPDATE SET close = EXCLUDED.close
        """,
        [(s, d, f"{d} 21:00+00", o, h, low, c, v) for s, d, o, h, low, c, v in rows],
    )
    yield rows
    _clean()


def _clean() -> None:
    db.execute("DELETE FROM prices_daily WHERE symbol = %s", (SYMBOL,))
    db.execute("DELETE FROM corporate_actions WHERE symbol = %s", (SYMBOL,))
    db.execute("DELETE FROM tickers WHERE symbol = %s", (SYMBOL,))


def test_load_bars_returns_oldest_first(seeded):
    bars = tech.load_bars(SYMBOL, as_of=date(2026, 12, 31))
    assert len(bars) == 60
    dates = list(bars["session_date"])
    assert dates == sorted(dates), "every reading treats the LAST row as today"
    # ...and compute therefore accepts it without raising.
    features = tech.compute(SYMBOL, bars)
    assert features.close == pytest.approx(159.0)


def test_as_of_excludes_later_bars(seeded):
    cutoff = seeded[29][1]
    bars = tech.load_bars(SYMBOL, as_of=cutoff)
    assert len(bars) == 30
    assert max(bars["session_date"]) == cutoff


def test_a_split_is_applied_to_the_moving_averages(seeded):
    """A split is a change of units, not a move: the MA must follow (D24)."""
    before = tech.compute(SYMBOL, tech.load_bars(SYMBOL, as_of=date(2026, 12, 31)))

    db.execute(
        """
        INSERT INTO corporate_actions (symbol, ex_date, action_type, split_ratio, source)
        VALUES (%s, %s, 'split', 2, 'test')
        """,
        (SYMBOL, seeded[50][1]),
    )
    after = tech.compute(SYMBOL, tech.load_bars(SYMBOL, as_of=date(2026, 12, 31)))

    assert after.ma.fast < before.ma.fast, "pre-split bars should be halved"
    assert after.ma.stacked is before.ma.stacked, "a split must not change the structure"


def test_a_dividend_is_NOT_adjusted_out_of_the_technical_series(seeded):
    """split_only mode: a dividend-adjusted MA drifts off the chart the human
    is looking at, and an MA nobody can see is useless for an entry decision."""
    before = tech.compute(SYMBOL, tech.load_bars(SYMBOL, as_of=date(2026, 12, 31)))

    db.execute(
        """
        INSERT INTO corporate_actions (symbol, ex_date, action_type, cash_amount, source)
        VALUES (%s, %s, 'cash_dividend', 5.00, 'test')
        """,
        (SYMBOL, seeded[50][1]),
    )
    after = tech.compute(SYMBOL, tech.load_bars(SYMBOL, as_of=date(2026, 12, 31)))

    assert after.ma.fast == pytest.approx(before.ma.fast), (
        "a dividend must not move the technical series"
    )


def test_as_of_excludes_a_later_corporate_action(seeded):
    """D24 reproducibility: asking for March must not see April's split."""
    db.execute(
        """
        INSERT INTO corporate_actions (symbol, ex_date, action_type, split_ratio, source)
        VALUES (%s, %s, 'split', 2, 'test')
        """,
        (SYMBOL, seeded[55][1]),
    )
    early = tech.load_bars(SYMBOL, as_of=seeded[29][1])
    assert early["price_factor"].nunique() == 1, "a later split must not reach back"


def test_sector_members_finds_the_seeded_symbol(seeded):
    assert SYMBOL in tech.sector_members("Technology")


def test_a_symbol_with_no_bars_returns_an_empty_frame():
    bars = tech.load_bars("NOSUCHSYMBOL", as_of=date(2026, 12, 31))
    assert bars.empty
    features = tech.compute("NOSUCHSYMBOL", bars)
    assert features.close is None
    assert features.bars_available == 0
