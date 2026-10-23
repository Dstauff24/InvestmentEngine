"""D42's candidate rows against a real database.

    PGDATABASE=ie_test pytest -m integration

Two properties matter more than the round trip. A re-scored day must REPLACE
the scores and LEAVE the forward returns alone, and a second pass must not
rewrite a return that is already there — otherwise a repaired basis or a late
bar silently moves a number a study has already read.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from core import db, settings
from jobs import candidate_returns
from scoring import runner
from scoring.composite import ScoredSymbol, UniverseRun, rank_universe
from tests.dbguard import assert_safe_database

pytestmark = pytest.mark.integration

SYMBOL = "ZZCD"
AS_OF = date(2026, 6, 1)


@pytest.fixture(autouse=True)
def seeded():
    assert_safe_database(settings.database_url())
    _clean()
    db.execute(
        "INSERT INTO tickers (symbol, sector, is_active, passes_liquidity) "
        "VALUES (%s, 'Technology', TRUE, TRUE)",
        (SYMBOL,),
    )
    yield
    _clean()


def _clean() -> None:
    for table in ("daily_candidates", "prices_daily", "corporate_actions"):
        db.execute(f"DELETE FROM {table} WHERE symbol = %s", (SYMBOL,))
    db.execute("DELETE FROM tickers WHERE symbol = %s", (SYMBOL,))


def add_bars(start: date, closes: list[float]) -> list[date]:
    """Weekday bars from `start`. Returns the session dates written."""
    days: list[date] = []
    day = start
    for close in closes:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        db.execute(
            """
            INSERT INTO prices_daily (symbol, session_date, ts, open, high, low,
                                      close, volume, source)
            VALUES (%s, %s, %s::timestamptz, %s, %s, %s, %s, 1000000, 'test')
            ON CONFLICT (symbol, ts) DO UPDATE SET close = EXCLUDED.close
            """,
            (SYMBOL, day, f"{day} 20:00+00", close, close * 1.01, close * 0.99, close),
        )
        days.append(day)
        day += timedelta(days=1)
    return days


def write_candidate(day: date, composite: float = 81.0) -> None:
    item = ScoredSymbol(
        symbol=SYMBOL, pillars=runner.abstaining_pillars(SYMBOL), composite=composite
    )
    rank_universe([item])
    runner.record_candidates(UniverseRun(scored=[item]), day=day)


def row(day: date = AS_OF) -> dict:
    return db.fetch_one(
        "SELECT * FROM daily_candidates WHERE symbol = %s AND session_date = %s",
        (SYMBOL, day),
    )


# ── the round trip ──────────────────────────────────────────────────────────


def test_a_candidate_is_written_with_its_score_and_the_days_raw_close():
    add_bars(AS_OF, [100.0])
    write_candidate(AS_OF)
    stored = row()
    assert stored["symbol"] == SYMBOL
    assert float(stored["composite"]) == pytest.approx(81.0)
    assert float(stored["close_raw"]) == pytest.approx(100.0)
    assert stored["surfaced"] is False
    assert stored["gates_in_force"]["max_ideas_per_day"] >= 1
    assert stored["fwd_5_pct"] is None, "nothing measured yet"


def test_a_day_with_no_bar_still_records_the_score():
    """The close is a convenience for a human reading the row; its absence must
    not cost the score, which is the thing the study needs."""
    write_candidate(AS_OF)
    assert row()["close_raw"] is None
    assert float(row()["composite"]) == pytest.approx(81.0)


# ── forward returns ─────────────────────────────────────────────────────────


def test_the_job_fills_the_horizons_the_bars_reach():
    days = add_bars(AS_OF, [100.0, 101.0, 102.0, 103.0, 104.0, 110.0, 111.0])
    write_candidate(AS_OF)

    assert candidate_returns.run(as_of=days[-1], record=False) == 1
    stored = row()
    assert float(stored["fwd_5_pct"]) == pytest.approx(10.0)
    assert stored["fwd_5_on"] == days[5]
    assert stored["fwd_10_pct"] is None, "ten sessions have not passed"


def test_a_dividend_inside_the_window_counts_toward_the_return():
    """TOTAL RETURN (D19). The technical features stay split-only because a
    dividend-adjusted moving average does not match the chart a human reads;
    performance is the other question and this is the other series."""
    days = add_bars(AS_OF, [100.0] * 7)
    db.execute(
        """
        INSERT INTO corporate_actions (symbol, ex_date, action_type, cash_amount, source)
        VALUES (%s, %s, 'cash_dividend', 2.0, 'test')
        """,
        (SYMBOL, days[3]),
    )
    write_candidate(AS_OF)
    candidate_returns.run(as_of=days[-1], record=False)

    value = float(row()["fwd_5_pct"])
    assert value == pytest.approx(2.0, abs=0.05), (
        f"a flat price plus a $2 dividend on $100 is about +2%, got {value}"
    )


def test_a_split_inside_the_window_cancels_instead_of_crashing_the_return():
    """Both ends come from ONE adjusted frame, so the ratio is basis-stable —
    the raw columns would read a 10-for-1 as a 90% loss."""
    days = add_bars(AS_OF, [100.0, 100.0, 100.0, 10.0, 10.0, 10.0, 10.0])
    db.execute(
        """
        INSERT INTO corporate_actions (symbol, ex_date, action_type, split_ratio, source)
        VALUES (%s, %s, 'split', 10.0, 'test')
        """,
        (SYMBOL, days[3]),
    )
    write_candidate(AS_OF)
    candidate_returns.run(as_of=days[-1], record=False)
    assert float(row()["fwd_5_pct"]) == pytest.approx(0.0, abs=0.01)


# ── what a re-run must and must not touch ───────────────────────────────────


def test_rescoring_a_day_replaces_the_score_and_keeps_the_return():
    days = add_bars(AS_OF, [100.0] * 6 + [120.0])
    write_candidate(AS_OF, composite=81.0)
    candidate_returns.run(as_of=days[-1], record=False)
    first = float(row()["fwd_5_pct"])

    write_candidate(AS_OF, composite=55.0)
    after = row()
    assert float(after["composite"]) == pytest.approx(55.0), "the score is replaced"
    assert float(after["fwd_5_pct"]) == pytest.approx(first), "the return is untouched"


def test_a_filled_return_is_never_rewritten():
    """FILL ONCE. A repaired basis or a late bar must not silently move a
    number a study has already read."""
    days = add_bars(AS_OF, [100.0, 101.0, 102.0, 103.0, 104.0, 110.0])
    write_candidate(AS_OF)
    candidate_returns.run(as_of=days[-1], record=False)
    assert float(row()["fwd_5_pct"]) == pytest.approx(10.0)

    # The fifth session is restated upward — a late print, or a repair.
    db.execute(
        "UPDATE prices_daily SET close = 200.0 WHERE symbol = %s AND session_date = %s",
        (SYMBOL, days[5]),
    )
    candidate_returns.run(as_of=days[-1], record=False)
    assert float(row()["fwd_5_pct"]) == pytest.approx(10.0), "still the first measurement"


def test_running_twice_changes_nothing_and_reports_nothing_filled():
    days = add_bars(AS_OF, [100.0, 101.0, 102.0, 103.0, 104.0, 110.0])
    write_candidate(AS_OF)
    assert candidate_returns.run(as_of=days[-1], record=False) == 1
    assert candidate_returns.run(as_of=days[-1], record=False) == 0


def test_the_job_touches_no_table_a_promotion_decision_reads():
    """D40 counts closed Shadow Book ideas. This job must not be able to add
    one, and the check is the row count rather than a reading of the code."""
    days = add_bars(AS_OF, [100.0] * 6 + [110.0])
    write_candidate(AS_OF)
    before = db.fetch_one("SELECT count(*) AS n FROM shadow_book")["n"]
    candidate_returns.run(as_of=days[-1], record=False)
    assert db.fetch_one("SELECT count(*) AS n FROM shadow_book")["n"] == before
