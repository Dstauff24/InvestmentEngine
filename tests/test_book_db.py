"""The ladder-scoped open book, against a real database (D40/D44).

    PGDATABASE=ie_test pytest -m integration

D44's scoping is the thing a fixture cannot establish: shadow, paper and live
read DIFFERENT sources, and a shadow position must not appear in the live book.
"""

from __future__ import annotations

from datetime import date

import pytest

from core import db, settings
from decision.book import load_book
from tests.dbguard import assert_safe_database

pytestmark = pytest.mark.integration

SYMBOL = "ZZBOOK"
ACCOUNT = 100_000.0


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
    db.execute("DELETE FROM shadow_book WHERE symbol = %s", (SYMBOL,))
    db.execute("DELETE FROM positions WHERE symbol = %s", (SYMBOL,))
    db.execute("DELETE FROM tickers WHERE symbol = %s", (SYMBOL,))


def add_shadow_call(status: str, *, outcome: str = "open") -> None:
    db.execute(
        """
        INSERT INTO shadow_book (symbol, origin, tier, recipe, strategy, called_on,
                                 proj_entry_high, proj_stop, outcome, status_at_call)
        VALUES (%s, 'engine', 'B', 'pullback_to_trend', 'swing', %s,
                100.0, 95.0, %s, %s)
        """,
        (SYMBOL, date(2026, 10, 21), outcome, status),
    )


def test_a_shadow_call_appears_only_in_the_shadow_book():
    """D44: without this scoping a shadow idea blocks a live one, which is
    exactly backwards."""
    add_shadow_call("shadow")

    shadow = load_book("shadow", account_value=ACCOUNT)
    paper = load_book("paper", account_value=ACCOUNT)
    live = load_book("live", account_value=ACCOUNT)

    assert shadow.holds(SYMBOL)
    assert not paper.holds(SYMBOL)
    assert not live.holds(SYMBOL)


def test_a_paper_call_is_separate_from_shadow():
    add_shadow_call("paper")
    assert load_book("paper", account_value=ACCOUNT).holds(SYMBOL)
    assert not load_book("shadow", account_value=ACCOUNT).holds(SYMBOL)


def test_a_closed_call_is_not_in_the_open_book():
    add_shadow_call("shadow", outcome="target_2")
    assert not load_book("shadow", account_value=ACCOUNT).holds(SYMBOL)


def test_open_heat_is_derived_from_the_projection_for_a_shadow_call():
    """A tracked call has no custodian to ask for its open risk."""
    add_shadow_call("shadow")
    book = load_book("shadow", account_value=ACCOUNT)
    # 100.0 entry - 95.0 stop = 5.0 of risk against a 100k account.
    assert book.open_risk_pct == pytest.approx(5.0 / ACCOUNT * 100.0)


def test_swings_are_counted_by_sector():
    add_shadow_call("shadow")
    book = load_book("shadow", account_value=ACCOUNT)
    assert book.swing_count == 1
    assert book.sector_count("Technology") == 1
    assert book.sector_count("Energy") == 0


def test_an_unknown_ladder_state_raises():
    with pytest.raises(ValueError, match="unknown ladder state"):
        load_book("production", account_value=ACCOUNT)
