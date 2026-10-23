"""O17's return on capital, against a real database.

    PGDATABASE=ie_test pytest -m integration

The arithmetic is covered by fixtures in `test_performance.py`. What only a
database can establish is that the three queries READ WHAT THEY MEAN TO: the
account filter has to work both named and pooled (a NULL text parameter is
where that breaks), the dividend join has to find a distribution through
`positions`, and a NUMERIC column has to arrive as something the Decimal
arithmetic can use rather than a float that rounds.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from core import db, settings
from decision import performance
from tests.dbguard import assert_safe_database

pytestmark = pytest.mark.integration

SYMBOL = "ZZPERF"
ACCOUNT = "ZZPERFHASH"
OTHER = "ZZOTHERHASH"
DAY_ONE = date(2026, 11, 2)
DAYS = [DAY_ONE + timedelta(days=i) for i in range(4)]


@pytest.fixture(scope="module", autouse=True)
def _guard_database():
    return assert_safe_database(settings.database_url())


@pytest.fixture(autouse=True)
def seeded():
    assert_safe_database(db.settings.database_url())
    _clean()
    db.execute(
        "INSERT INTO tickers (symbol, sector, is_active) VALUES (%s, 'Technology', TRUE)",
        (SYMBOL,),
    )
    yield
    _clean()


def _clean() -> None:
    db.execute("DELETE FROM account_snapshots WHERE account_hash IN (%s, %s)", (ACCOUNT, OTHER))
    db.execute("DELETE FROM positions WHERE account_hash IN (%s, %s)", (ACCOUNT, OTHER))
    db.execute("DELETE FROM corporate_actions WHERE symbol = %s", (SYMBOL,))
    db.execute("DELETE FROM tickers WHERE symbol = %s", (SYMBOL,))


def _snapshot(day: date, total: str, cash: str, account: str = ACCOUNT) -> None:
    db.execute(
        """
        INSERT INTO account_snapshots (as_of_date, account_hash, total_value, cash)
        VALUES (%s, %s, %s, %s)
        """,
        (day, account, Decimal(total), Decimal(cash)),
    )


def _position(day: date, quantity: str, account: str = ACCOUNT) -> None:
    db.execute(
        """
        INSERT INTO positions (as_of, as_of_date, account_hash, symbol, asset_type, quantity)
        VALUES (%s, %s, %s, %s, 'ETF', %s)
        """,
        (f"{day} 20:00+00", day, account, SYMBOL, Decimal(quantity)),
    )


def test_a_deposit_is_removed_from_the_return_through_the_real_queries():
    _snapshot(DAYS[0], "1000", "0")
    _snapshot(DAYS[1], "1100", "100")
    _position(DAYS[0], "10")
    _position(DAYS[1], "10")

    result = performance.performance(DAYS[0], ACCOUNT)
    assert result.twr == Decimal("0")
    assert result.external_in == Decimal("100")
    assert result.balance_growth == Decimal("0.1")


def test_naming_an_account_excludes_the_other_one():
    """The pooled default is right for ONE account and wrong for several: two
    accounts' values add up while their returns are separate scoreboards. Both
    paths go through the same `%(account)s::text IS NULL` filter, and an
    untyped NULL parameter is exactly what Postgres refuses."""
    _snapshot(DAYS[0], "1000", "0")
    _snapshot(DAYS[1], "1100", "0")
    _snapshot(DAYS[0], "500", "0", account=OTHER)
    _snapshot(DAYS[1], "500", "0", account=OTHER)

    named = performance.performance(DAYS[0], ACCOUNT)
    assert named.start_value == Decimal("1000")
    assert named.twr == Decimal("0.1")

    pooled = performance.performance(DAYS[0])
    # Pooled reads BOTH accounts' rows, so there are two snapshots per day and
    # the series is no longer one account's history — which is why the caller
    # names the account and the report prints which it used.
    assert len(pooled.links) > len(named.links)


def test_a_stored_distribution_is_found_and_counted_as_return():
    """The dividend query reaches `corporate_actions` through the symbols in
    `positions`, so a distribution on something held is matched while one on
    any other symbol is never considered."""
    _snapshot(DAYS[0], "1000", "0")
    _snapshot(DAYS[1], "1005", "5")
    _position(DAYS[0], "10")
    _position(DAYS[1], "10")
    db.execute(
        """
        INSERT INTO corporate_actions (
            symbol, action_type, ex_date, payment_date, cash_amount, source
        ) VALUES (%s, 'cash_dividend', %s, %s, %s, 'fmp')
        """,
        (SYMBOL, DAYS[0], DAYS[1], Decimal("0.50")),
    )

    result = performance.performance(DAYS[0], ACCOUNT)
    assert [f.kind for f in result.flows] == ["internal"]
    assert result.internal == Decimal("5")
    assert result.twr == Decimal("0.005")


def test_an_account_with_one_snapshot_reports_that_rather_than_a_zero():
    _snapshot(DAYS[0], "200", "0")
    assert performance.performance(DAYS[0], ACCOUNT) is None
    assert "not measurable yet" in performance.render(None, account=ACCOUNT)
