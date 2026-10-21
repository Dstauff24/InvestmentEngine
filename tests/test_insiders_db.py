"""Insider transactions against a real database — the point-in-time guarantee.

    PGDATABASE=ie_test pytest -m integration

The guarantee lives in SQL, so a fixture cannot establish it: `load_insider_purchases`
bounds on `filed_at`, the moment the SEC accepted the filing, and NEVER on
`transaction_date`. The two differ by up to two business days, and keying on
the wrong one lets a backtest act on a purchase before anyone could have known
about it — invisibly, since the results simply look better.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from core import db, settings
from features import catalyst as cat
from ingest import edgar_insiders as ins
from tests.dbguard import assert_safe_database
from tests.fixtures import edgar_payloads as fx

pytestmark = pytest.mark.integration

SYMBOL = "ZZINSD"
AS_OF = date(2026, 10, 21)


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
    db.execute("DELETE FROM insider_transactions WHERE symbol = %s", (SYMBOL,))
    db.execute("DELETE FROM tickers WHERE symbol = %s", (SYMBOL,))


def store(
    accession: str,
    insider: str,
    *,
    filed: datetime,
    transaction: date,
    value: float = 200_000.0,
    code: str = "P",
    derivative: bool = False,
) -> None:
    db.execute(
        ins.UPSERT,
        {
            "accession_number": accession,
            "txn_index": 0,
            "symbol": SYMBOL,
            "issuer_cik": "0000000001",
            "filed_at": filed,
            "filed_session": filed.astimezone(UTC).date(),
            "transaction_date": transaction,
            "insider_cik": insider,
            "insider_name": insider,
            "insider_title": None,
            "is_director": True,
            "is_officer": False,
            "is_ten_percent_owner": False,
            "transaction_code": code,
            "acquired_disposed": "A",
            "shares": 1000,
            "price_per_share": value / 1000,
            "value_usd": value,
            "shares_owned_after": 5000,
            "is_10b5_1": False,
            "security_title": "Common Stock",
            "is_derivative": derivative,
        },
    )


def at(day: date, hour: int = 18) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


# ══════════════════════════════════════════════════════════════════════════
# THE point-in-time guarantee
# ══════════════════════════════════════════════════════════════════════════


def test_a_filing_accepted_after_the_as_of_moment_is_invisible():
    """Two business days of lag is legal, and this is the whole reason both
    timestamps are stored."""
    store("a-1", "A", filed=at(AS_OF + timedelta(days=2)), transaction=AS_OF - timedelta(days=1))

    rows = cat.load_insider_purchases(SYMBOL, as_of=AS_OF)
    assert rows == [], (
        "the trade happened before as_of but the filing was accepted after it — "
        "nobody could have known"
    )


def test_the_same_filing_becomes_visible_once_accepted():
    filed = at(AS_OF + timedelta(days=2))
    store("a-1", "A", filed=filed, transaction=AS_OF - timedelta(days=1))

    later = cat.load_insider_purchases(SYMBOL, as_of=filed.date())
    assert len(later) == 1
    assert later[0]["transaction_date"] == AS_OF - timedelta(days=1)


def test_a_trade_long_before_the_window_still_counts_if_filed_inside_it():
    """The other direction. Keying on transaction_date would reject this, and
    it was genuinely news on the day it was filed."""
    store(
        "a-2",
        "A",
        filed=at(AS_OF - timedelta(days=3)),
        transaction=AS_OF - timedelta(days=200),
    )
    rows = cat.load_insider_purchases(SYMBOL, as_of=AS_OF, window_days=90)
    assert len(rows) == 1


def test_the_loader_only_returns_open_market_purchases():
    """Filtered in SQL so the partial index is used — and re-checked in the
    feature, because a hand-assembled list is how an exercise sneaks in."""
    store("p", "A", filed=at(AS_OF - timedelta(days=1)), transaction=AS_OF - timedelta(days=1))
    store(
        "a",
        "B",
        filed=at(AS_OF - timedelta(days=1)),
        transaction=AS_OF - timedelta(days=1),
        code="A",
    )
    store(
        "m",
        "C",
        filed=at(AS_OF - timedelta(days=1)),
        transaction=AS_OF - timedelta(days=1),
        code="M",
    )
    store(
        "s",
        "D",
        filed=at(AS_OF - timedelta(days=1)),
        transaction=AS_OF - timedelta(days=1),
        code="S",
    )
    store(
        "d",
        "E",
        filed=at(AS_OF - timedelta(days=1)),
        transaction=AS_OF - timedelta(days=1),
        derivative=True,
    )

    rows = cat.load_insider_purchases(SYMBOL, as_of=AS_OF)
    assert [r["accession_number"] for r in rows] == ["p"]


def test_a_filing_accepted_after_the_close_is_not_visible_to_that_days_run():
    """The upper bound is the 16:00 ET close, because a 16:05 acceptance is not
    knowable to a run that happened at 16:00."""
    store("late", "A", filed=at(AS_OF, hour=23), transaction=AS_OF)
    assert cat.load_insider_purchases(SYMBOL, as_of=AS_OF) == []


# ══════════════════════════════════════════════════════════════════════════
# Depth, and the cluster end to end
# ══════════════════════════════════════════════════════════════════════════


def test_depth_reflects_our_own_series_not_the_cluster_window():
    for i, insider in enumerate("ABC"):
        store(f"x-{i}", insider, filed=at(AS_OF - timedelta(days=i * 40)), transaction=AS_OF)

    depth = cat.insider_series_depth(SYMBOL, as_of=AS_OF)
    assert depth.observations == 3
    assert depth.span_days == 80, "spans beyond the 90-day cluster window"


def test_an_untouched_symbol_has_empty_depth_so_the_verdict_is_none():
    depth = cat.insider_series_depth("NOSUCHSYMBOL", as_of=AS_OF)
    assert depth.is_empty is True
    result = cat.insider_cluster([], as_of=AS_OF, series_depth=depth)
    assert result.is_cluster is None


def test_a_real_cluster_end_to_end():
    for i, insider in enumerate("ABC"):
        store(f"c-{i}", insider, filed=at(AS_OF - timedelta(days=i + 1)), transaction=AS_OF)

    rows = cat.load_insider_purchases(SYMBOL, as_of=AS_OF)
    depth = cat.insider_series_depth(SYMBOL, as_of=AS_OF)
    result = cat.insider_cluster(rows, as_of=AS_OF, series_depth=depth)

    assert result.is_cluster is True
    assert result.distinct_insiders == 3
    assert result.aggregate_usd == pytest.approx(600_000)


# ══════════════════════════════════════════════════════════════════════════
# Writing
# ══════════════════════════════════════════════════════════════════════════


def test_a_parsed_form4_round_trips_into_the_table():
    filed = at(AS_OF)
    rows = ins.parse_form4(fx.FORM4_PURCHASE, accession="rt-1", filed_at=filed)
    written = db.executemany(ins.UPSERT, [r | {"symbol": SYMBOL} for r in rows])
    assert written == 1

    stored = db.fetch_one("SELECT * FROM insider_transactions WHERE accession_number = 'rt-1'")
    assert stored["transaction_code"] == "P"
    assert float(stored["value_usd"]) == pytest.approx(1_152_500.00)
    assert stored["filed_at"] == filed
    assert stored["transaction_date"] == date(2026, 10, 19)


def test_re_storing_the_same_filing_is_idempotent():
    """A daily re-run must not duplicate a filing."""
    rows = ins.parse_form4(fx.FORM4_PURCHASE, accession="rt-2", filed_at=at(AS_OF))
    payload = [r | {"symbol": SYMBOL} for r in rows]
    db.executemany(ins.UPSERT, payload)
    db.executemany(ins.UPSERT, payload)
    assert (
        db.fetch_one(
            "SELECT count(*) AS n FROM insider_transactions WHERE accession_number = 'rt-2'"
        )["n"]
        == 1
    )


def test_known_accessions_lets_a_re_run_fetch_no_documents():
    """The reason a daily run is cheap: one submissions call per symbol, and a
    document only for genuinely new filings."""
    store("seen-1", "A", filed=at(AS_OF), transaction=AS_OF)
    assert "seen-1" in ins.known_accessions(SYMBOL)
    assert "never-seen" not in ins.known_accessions(SYMBOL)


def test_filed_at_is_not_nullable():
    """A row whose filing time is unknown cannot be read point-in-time, so it
    must not be storable at all."""
    import psycopg

    rows = ins.parse_form4(fx.FORM4_PURCHASE, accession="nf-1", filed_at=at(AS_OF))
    bad = rows[0] | {"symbol": SYMBOL, "filed_at": None}
    with pytest.raises(psycopg.errors.NotNullViolation):
        db.executemany(ins.UPSERT, [bad])
