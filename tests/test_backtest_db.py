"""The backtest against a real database (D39(4)).

    PGDATABASE=ie_test pytest -m integration

One test here matters more than the rest. The walk reads point-in-time data
from memory for speed, which means the `WHERE` clauses in `features/` now exist
twice — and two implementations of a point-in-time rule drift, quietly, in the
direction that makes results look better.
`test_the_in_memory_slicers_agree_with_the_loaders` runs both against the same
seeded rows so a divergence fails a test instead of flattering an expectancy.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from backtest.engine import (
    events_knowable,
    fundamentals_knowable,
    load_symbol_history,
)
from backtest.engine import (
    run as run_backtest,
)
from core import db, settings
from features import catalyst as catalyst_features
from features import fundamental as fundamental_features
from tests.dbguard import assert_safe_database

pytestmark = pytest.mark.integration

SYMBOL = "ZZBT"
AS_OF = date(2026, 10, 21)
START = date(2024, 1, 2)


@pytest.fixture(scope="module", autouse=True)
def _guard_database():
    return assert_safe_database(settings.database_url())


@pytest.fixture(autouse=True)
def seeded():
    assert_safe_database(db.settings.database_url())
    _clean()
    db.execute(
        "INSERT INTO tickers (symbol, sector, is_active, passes_liquidity) "
        "VALUES (%s, 'Technology', TRUE, TRUE)",
        (SYMBOL,),
    )
    yield
    _clean()


def _clean() -> None:
    for table in ("prices_daily", "corporate_actions", "events", "fundamentals"):
        db.execute(f"DELETE FROM {table} WHERE symbol = %s", (SYMBOL,))
    db.execute("DELETE FROM tickers WHERE symbol = %s", (SYMBOL,))


def add_bar(day: date, close: float, *, high=None, low=None) -> None:
    db.execute(
        """
        INSERT INTO prices_daily (symbol, session_date, ts, open, high, low, close,
                                  volume, source)
        VALUES (%s, %s, %s::timestamptz, %s, %s, %s, %s, 2000000, 'test')
        ON CONFLICT (symbol, ts) DO UPDATE SET close = EXCLUDED.close
        """,
        (SYMBOL, day, f"{day} 20:00+00", close, high or close * 1.01, low or close * 0.99, close),
    )


def add_quarter(end: date, *, filed: date, revenue: float, eps: float) -> None:
    db.execute(
        """
        INSERT INTO fundamentals (symbol, period_type, fiscal_date_ending, filing_date,
                                  revenue, eps_diluted, gross_margin, source)
        VALUES (%s, 'quarter', %s, %s, %s, %s, 40.0, 'test')
        ON CONFLICT (symbol, period_type, fiscal_date_ending) DO UPDATE
        SET filing_date = EXCLUDED.filing_date
        """,
        (SYMBOL, end, filed, revenue, eps),
    )


def add_earnings(day: date) -> None:
    db.execute(
        """
        INSERT INTO events (symbol, event_type, event_date, source)
        VALUES (%s, 'earnings', %s, 'test')
        ON CONFLICT (symbol, event_type, event_date) DO NOTHING
        """,
        (SYMBOL, day),
    )


# ── the anti-drift test ─────────────────────────────────────────────────────


def test_the_in_memory_slicers_agree_with_the_loaders():
    """Two implementations of a point-in-time rule, compared on real rows.

    Checked on the hard dates: the day a quarter was filed, the day before, and
    a date between the period end and the filing — which is exactly where the
    old lookahead lived.
    """
    add_quarter(date(2024, 3, 31), filed=date(2024, 4, 30), revenue=100.0, eps=1.0)
    add_quarter(date(2024, 6, 30), filed=date(2024, 7, 29), revenue=110.0, eps=1.1)
    for day in (date(2024, 2, 1), date(2024, 5, 2), date(2024, 8, 1)):
        add_earnings(day)

    history = load_symbol_history(SYMBOL, as_of=AS_OF, sector="Technology")

    for as_of in (
        date(2024, 4, 5),  # period ended, not yet filed
        date(2024, 4, 29),
        date(2024, 4, 30),  # filed today
        date(2024, 7, 29),
        date(2026, 1, 1),
    ):
        from_db = fundamental_features.load_fundamentals(SYMBOL, as_of=as_of)
        from_memory = fundamentals_knowable(history.fundamentals, as_of=as_of)
        assert [r["fiscal_date_ending"] for r in from_db] == [
            r["fiscal_date_ending"] for r in from_memory
        ], f"fundamentals disagree on {as_of}"

        db_events = catalyst_features.load_events(SYMBOL, as_of=as_of)
        memory_events = events_knowable(history.events, as_of=as_of)
        assert sorted(e["event_date"] for e in db_events) == sorted(
            e["event_date"] for e in memory_events
        ), f"events disagree on {as_of}"


def test_the_slicers_agree_when_there_is_nothing_to_slice():
    """The empty case is where two implementations most often differ by one
    returning None and the other an empty list."""
    history = load_symbol_history(SYMBOL, as_of=AS_OF, sector="Technology")
    assert fundamentals_knowable(history.fundamentals, as_of=AS_OF) == []
    assert fundamental_features.load_fundamentals(SYMBOL, as_of=AS_OF) == []
    assert events_knowable(history.events, as_of=AS_OF) == []


# ── loading ─────────────────────────────────────────────────────────────────


def test_history_loads_the_adjusted_series_the_live_engine_reads():
    for i in range(10):
        add_bar(START + timedelta(days=i), 100.0 + i)
    db.execute(
        """
        INSERT INTO corporate_actions (symbol, ex_date, action_type, split_ratio, source)
        VALUES (%s, %s, 'split', 2.0, 'test')
        """,
        (SYMBOL, START + timedelta(days=5)),
    )

    history = load_symbol_history(SYMBOL, as_of=AS_OF)
    assert "adj_close" in history.bars.columns
    pre, post = history.bars.iloc[0], history.bars.iloc[-1]
    assert float(pre["adj_close"]) == pytest.approx(float(pre["close"]) / 2.0)
    assert float(post["adj_close"]) == pytest.approx(float(post["close"]))


def test_a_symbol_with_a_short_series_is_reported_as_skipped():
    for i in range(30):
        add_bar(START + timedelta(days=i), 100.0)
    result = run_backtest(
        symbols=[SYMBOL], as_of=AS_OF, years=2, with_sectors=False, allow_dirty=True
    )
    assert result.skipped_symbols == [SYMBOL]
    assert result.trades == []
    assert result.symbols == 0


def test_the_whole_walk_runs_on_a_seeded_symbol_and_records_regimes():
    """End to end: history in, trades out, with the regime histogram attached
    even when the seeded symbol produces no setup."""
    for i in range(400):
        add_bar(START + timedelta(days=i), 100.0 + i * 0.1)
    result = run_backtest(
        symbols=[SYMBOL], as_of=AS_OF, years=3, with_sectors=False, allow_dirty=True
    )
    assert result.symbols == 1
    assert result.start is not None and result.end == AS_OF
    assert isinstance(result.regimes, dict)
    for trade in result.trades:
        assert trade.called_on >= result.start
        assert trade.entered_on is None or trade.entered_on > trade.called_on, (
            "next-day entry: a trade cannot be entered on the bar that called it"
        )


def test_sector_indices_are_built_from_the_universe_not_from_the_walked_symbols():
    """A sector index is a fact about the MARKET.

    Built from the symbols being walked, a 60-symbol sweep sample gave sectors
    two members each — under `sector.min_members`, so `rs_vs_sector` abstained
    and `base_breakout`'s relative-strength gate refused everything. The sweep
    would have reported "no setup at any contraction ratio" and the cause would
    have been the sample size, which is why this is pinned against a real
    database rather than left to a comment.
    """
    from backtest.engine import build_sector_indices

    members = [f"ZZSEC{i}" for i in range(4)]
    try:
        for symbol in members:
            db.execute(
                "INSERT INTO tickers (symbol, sector, is_active, passes_liquidity, "
                "avg_volume_20d) VALUES (%s, 'ZZSector', TRUE, TRUE, 5000000)",
                (symbol,),
            )
            for i in range(80):
                day = START + timedelta(days=i)
                db.execute(
                    """
                    INSERT INTO prices_daily (symbol, session_date, ts, open, high, low,
                                              close, volume, source)
                    VALUES (%s, %s, %s::timestamptz, 10, 11, 9, %s, 1000000, 'test')
                    ON CONFLICT (symbol, ts) DO UPDATE SET close = EXCLUDED.close
                    """,
                    (symbol, day, f"{day} 20:00+00", 10.0 + i * 0.1),
                )

        # Asked only for the SECTOR NAME, it finds all four members itself —
        # the walked symbol list never reaches it.
        indices = build_sector_indices(["ZZSector"], as_of=AS_OF)
        assert "ZZSector" in indices
        assert len(indices["ZZSector"]) == 80
    finally:
        db.execute("DELETE FROM prices_daily WHERE symbol = ANY(%s)", (members,))
        db.execute("DELETE FROM tickers WHERE symbol = ANY(%s)", (members,))


def test_a_sector_too_small_to_be_an_index_gets_none_rather_than_a_two_stock_average():
    """Two liquid names are not a sector; their "index" is one of the two
    stocks being measured against it."""
    from backtest.engine import build_sector_indices

    members = ["ZZTINY0", "ZZTINY1"]
    try:
        for symbol in members:
            db.execute(
                "INSERT INTO tickers (symbol, sector, is_active, passes_liquidity) "
                "VALUES (%s, 'ZZTiny', TRUE, TRUE)",
                (symbol,),
            )
        assert build_sector_indices(["ZZTiny"], as_of=AS_OF) == {}
    finally:
        db.execute("DELETE FROM tickers WHERE symbol = ANY(%s)", (members,))
