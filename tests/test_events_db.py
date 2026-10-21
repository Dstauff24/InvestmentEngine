"""Dividend-cut flagging against a real database (D19(5)).

    PGDATABASE=ie_test pytest -m integration

Why these exist: the first production run on 2026-10-21 reported `cuts=84`
against `actions=84` on a fresh database — reading as "every dividend in the
S&P 500 was cut". The comparison was never wrong; the function returned the
UPDATE's rowcount instead of the cut count, and on an empty database every row
is written for the first time. The true count was zero: 82 of the 84 had no
prior dividend on record at all.

A dividend cut is an automatic thesis-review trigger (D19(5)). A metric that
reads alarming and means nothing trains the operator to ignore it, which is the
one thing a trigger cannot afford — so the no-prior case and the returned
number are both pinned here.
"""

from __future__ import annotations

from datetime import date

import pytest

from core import db
from ingest.fmp_events import flag_dividend_cuts
from tests.dbguard import assert_safe_database

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def _guard_database():
    from core import settings

    return assert_safe_database(settings.database_url())


SYMBOLS = ("AAPL", "MSFT", "JNJ", "KO", "PG", "XOM", "T", "BRK-B", "NVDA")


@pytest.fixture(autouse=True)
def clean():
    assert_safe_database(db.settings.database_url())
    db.execute("DELETE FROM corporate_actions WHERE true")
    # `corporate_actions.symbol` carries an FK to `tickers`, deliberately:
    # an adjustment-log row for a symbol the engine does not track would be
    # unreachable from every read path. So the universe has to exist first.
    db.executemany(
        "INSERT INTO tickers (symbol) VALUES (%s) ON CONFLICT (symbol) DO NOTHING",
        [(s,) for s in SYMBOLS],
    )
    yield
    db.execute("DELETE FROM corporate_actions WHERE true")
    db.execute("DELETE FROM tickers WHERE symbol = ANY(%s)", (list(SYMBOLS),))


def ensure_ticker(symbol: str) -> None:
    db.execute(
        "INSERT INTO tickers (symbol) VALUES (%s) ON CONFLICT (symbol) DO NOTHING", (symbol,)
    )


def dividend(symbol: str, ex: date, amount: str) -> None:
    ensure_ticker(symbol)
    db.execute(
        """
        INSERT INTO corporate_actions (symbol, ex_date, action_type, cash_amount, source)
        VALUES (%s, %s, 'cash_dividend', %s, 'test')
        ON CONFLICT (symbol, ex_date, action_type) DO UPDATE
        SET cash_amount = EXCLUDED.cash_amount
        """,
        (symbol, ex, amount),
    )


def row(symbol: str, ex: date) -> dict:
    return db.fetch_one(
        "SELECT * FROM corporate_actions WHERE symbol = %s AND ex_date = %s",
        (symbol, ex),
    )


# ── the no-prior case: THE bug from the first production run ─────────────────


def test_a_dividend_with_no_prior_is_not_a_cut():
    dividend("AAPL", date(2026, 11, 10), "0.25")

    scan = flag_dividend_cuts()

    assert scan.cuts == 0, "a first-ever dividend cannot be a cut — there is nothing to cut from"
    assert scan.without_prior == 1
    assert row("AAPL", date(2026, 11, 10))["is_dividend_cut"] is False
    assert row("AAPL", date(2026, 11, 10))["prior_cash_amount"] is None


def test_a_fresh_database_of_first_dividends_reports_zero_cuts():
    """The exact shape of the 2026-10-21 report: many rows, no history, 0 cuts."""
    for i, symbol in enumerate(("AAPL", "MSFT", "JNJ", "KO", "PG", "XOM")):
        dividend(symbol, date(2026, 11, 10 + i), "0.50")

    scan = flag_dividend_cuts()

    assert scan.cuts == 0
    assert scan.rows_touched == 6, "every row was written for the first time"
    assert scan.without_prior == 6
    assert (
        db.fetch_one("SELECT count(*) AS n FROM corporate_actions WHERE is_dividend_cut")["n"] == 0
    )


def test_the_returned_number_is_cuts_not_rows_touched():
    """The regression. Six rows examined, one actual cut."""
    dividend("AAPL", date(2026, 5, 10), "0.25")
    dividend("AAPL", date(2026, 8, 10), "0.20")  # the cut
    for i, symbol in enumerate(("MSFT", "JNJ", "KO", "PG")):
        dividend(symbol, date(2026, 11, 10 + i), "0.50")

    scan = flag_dividend_cuts()

    assert scan.rows_touched == 6
    assert scan.cuts == 1, "returning the rowcount here is what produced cuts=84"
    assert row("AAPL", date(2026, 8, 10))["is_dividend_cut"] is True


# ── the comparison itself ────────────────────────────────────────────────────


def test_a_smaller_dividend_than_the_prior_one_is_a_cut():
    dividend("T", date(2026, 5, 1), "0.2775")
    dividend("T", date(2026, 8, 1), "0.1388")

    assert flag_dividend_cuts().cuts == 1
    cut = row("T", date(2026, 8, 1))
    assert cut["is_dividend_cut"] is True
    assert float(cut["prior_cash_amount"]) == pytest.approx(0.2775)


def test_an_unchanged_dividend_is_not_a_cut():
    dividend("KO", date(2026, 5, 1), "0.51")
    dividend("KO", date(2026, 8, 1), "0.51")

    assert flag_dividend_cuts().cuts == 0
    assert row("KO", date(2026, 8, 1))["is_dividend_cut"] is False


def test_a_raised_dividend_is_not_a_cut():
    dividend("MSFT", date(2026, 5, 1), "0.75")
    dividend("MSFT", date(2026, 8, 1), "0.83")

    assert flag_dividend_cuts().cuts == 0
    assert row("MSFT", date(2026, 8, 1))["is_dividend_cut"] is False


def test_the_comparison_is_per_symbol():
    """One company's small dividend is not a cut to another's large one."""
    dividend("BRK-B", date(2026, 5, 1), "0.00")
    dividend("AAPL", date(2026, 8, 1), "0.25")

    assert flag_dividend_cuts().cuts == 0


def test_the_comparison_follows_ex_date_order_not_insertion_order():
    """Rows arrive from a calendar in whatever order FMP returns them."""
    dividend("T", date(2026, 8, 1), "0.1388")  # the later, smaller one FIRST
    dividend("T", date(2026, 5, 1), "0.2775")

    assert flag_dividend_cuts().cuts == 1
    assert row("T", date(2026, 8, 1))["is_dividend_cut"] is True
    assert row("T", date(2026, 5, 1))["is_dividend_cut"] is False


# ── idempotence: the job runs nightly ───────────────────────────────────────


def test_re_running_does_not_re_report_the_same_cut():
    dividend("T", date(2026, 5, 1), "0.2775")
    dividend("T", date(2026, 8, 1), "0.1388")

    assert flag_dividend_cuts().cuts == 1
    second = flag_dividend_cuts()
    assert second.rows_touched == 0, "nothing changed, so nothing is re-examined"
    assert second.cuts == 0
    # ...but the flag itself is still set on the row.
    assert row("T", date(2026, 8, 1))["is_dividend_cut"] is True


def test_a_later_dividend_arriving_flags_the_cut_then():
    """A cut is only visible once the second dividend lands."""
    dividend("T", date(2026, 5, 1), "0.2775")
    assert flag_dividend_cuts().cuts == 0

    dividend("T", date(2026, 8, 1), "0.1388")
    assert flag_dividend_cuts().cuts == 1


def test_a_restated_amount_re_evaluates_the_flag():
    """If the provider corrects an amount, the flag must follow."""
    dividend("T", date(2026, 5, 1), "0.2775")
    dividend("T", date(2026, 8, 1), "0.1388")
    assert flag_dividend_cuts().cuts == 1

    dividend("T", date(2026, 8, 1), "0.30")  # correction: actually a raise
    scan = flag_dividend_cuts()
    assert scan.rows_touched == 1
    assert scan.cuts == 0
    assert row("T", date(2026, 8, 1))["is_dividend_cut"] is False


def test_a_split_row_is_ignored_entirely():
    db.execute(
        """
        INSERT INTO corporate_actions (symbol, ex_date, action_type, split_ratio, source)
        VALUES ('NVDA', %s, 'split', 10, 'test')
        """,
        (date(2026, 6, 1),),
    )
    dividend("NVDA", date(2026, 7, 1), "0.01")

    scan = flag_dividend_cuts()
    assert scan.cuts == 0
    assert scan.rows_touched == 1, "only the dividend row is considered"
