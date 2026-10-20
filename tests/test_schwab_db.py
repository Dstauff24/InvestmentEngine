"""Schwab ingestion SQL against a real database.

    INTEGRATION_DB_OK=1 pytest -m integration

Covers what fixtures cannot: that the chain upsert is idempotent, that both
chain jobs really record is_critical rows so a failure blocks the day, that
derive_iv reads back what parse_chain wrote, and that the has_options writeback
really arms the D14 gate.
"""

from __future__ import annotations

import pytest

from core import clock, config, db, settings
from core.jobs import day_is_complete, failed_critical_jobs, job_run
from ingest.schwab_chains import (
    CHAINS_JOB,
    IV_JOB,
    UPSERT_CHAIN,
    derive_iv,
    parse_chain,
    record_options_availability,
)
from ingest.schwab_prices import UPSERT as UPSERT_PRICE
from ingest.schwab_prices import parse_candles
from tests.dbguard import assert_safe_database
from tests.fixtures import schwab_payloads as fx

pytestmark = pytest.mark.integration

DAY = fx.AS_OF


@pytest.fixture(scope="module", autouse=True)
def _guard_database():
    return assert_safe_database(settings.database_url())


@pytest.fixture(autouse=True)
def clean():
    assert_safe_database(settings.database_url())

    def wipe():
        for table in ("option_chain_snapshots", "iv_daily", "prices_daily", "job_runs", "tickers"):
            db.execute(f"DELETE FROM {table} WHERE true")

    wipe()
    db.execute("INSERT INTO tickers (symbol, in_sp500) VALUES ('TEST', true)")
    yield
    wipe()


def stored_chain() -> int:
    rows = parse_chain(
        fx.CHAIN,
        symbol="TEST",
        snapshot_ts=clock.session_close_utc(DAY),
        session_date=DAY,
        scope=config.universe().option_chains,
    )
    return db.executemany(UPSERT_CHAIN, rows)


# ── the chain snapshot ──────────────────────────────────────────────────────


def test_the_chain_upsert_is_idempotent():
    """D7: a re-run refreshes today and never duplicates."""
    first = stored_chain()
    second = stored_chain()
    assert first == second == 4 * 7 * 2
    assert db.fetch_one("SELECT count(*) AS n FROM option_chain_snapshots")["n"] == first


def test_the_stored_scope_is_the_d22_scope():
    stored_chain()
    rows = db.fetch_all(
        "SELECT DISTINCT expiration FROM option_chain_snapshots ORDER BY expiration"
    )
    assert [r["expiration"] for r in rows] == fx.MONTHLIES
    # Asserted through the filter's own predicate rather than a re-derived
    # threshold: `spot * 1.15` is 114.99999... in binary, which would reject the
    # exactly-+15% strike the boundary is meant to include.
    from ingest.schwab_chains import within_strike_range

    scope = config.universe().option_chains
    stored = db.fetch_all("SELECT strike, underlying_price FROM option_chain_snapshots")
    assert stored
    for row in stored:
        assert within_strike_range(
            float(row["strike"]), float(row["underlying_price"]), scope.strike_range_pct
        ), row
    # And the band really does reach the inclusive edges.
    edges = db.fetch_one("SELECT min(strike) AS lo, max(strike) AS hi FROM option_chain_snapshots")
    assert (float(edges["lo"]), float(edges["hi"])) == (85.0, 115.0)


def test_iv_is_stored_as_a_decimal_fraction_not_a_percent():
    stored_chain()
    band = db.fetch_one(
        "SELECT min(implied_volatility) AS lo, max(implied_volatility) AS hi "
        "FROM option_chain_snapshots"
    )
    assert 0 < float(band["lo"]) < 3
    assert 0 < float(band["hi"]) < 3


def test_no_sentinel_values_reach_the_database():
    """A -999 in an IV average would poison every downstream volatility number."""
    rows = parse_chain(
        fx.chain_with_sentinels(),
        symbol="TEST",
        snapshot_ts=clock.session_close_utc(DAY),
        session_date=DAY,
        scope=config.universe().option_chains,
    )
    db.executemany(UPSERT_CHAIN, rows)
    leaked = db.fetch_one(
        "SELECT count(*) FILTER (WHERE implied_volatility <= -998) AS iv, "
        "       count(*) FILTER (WHERE delta <= -998) AS d "
        "FROM option_chain_snapshots"
    )
    assert (leaked["iv"], leaked["d"]) == (0, 0)


# ── derive_iv reads back what parse_chain wrote ─────────────────────────────


def test_derive_iv_round_trips_the_stored_chain():
    """The two halves of the sacred pair must agree on the row shape: derive_iv
    SELECTs specific columns, so a rename in either place breaks it silently."""
    stored_chain()
    assert derive_iv(session_date=DAY) == 1
    row = db.fetch_one("SELECT * FROM iv_daily")
    assert row["symbol"] == "TEST"
    assert float(row["iv30"]) > 0
    assert float(row["skew_25d"]) > 0  # puts bid up, as the fixture builds
    assert float(row["term_slope"]) > 0  # contango
    assert row["iv_rank_252"] is None  # D7: no history yet
    assert row["history_days"] == 0


def test_derive_iv_is_idempotent():
    stored_chain()
    derive_iv(session_date=DAY)
    derive_iv(session_date=DAY)
    assert db.fetch_one("SELECT count(*) AS n FROM iv_daily")["n"] == 1


def test_derive_iv_skips_cleanly_with_no_stored_chain():
    """A skip is not a failure — but it still blocks the day for a critical job."""
    assert derive_iv(session_date=DAY) == 0
    row = db.fetch_one("SELECT status, error FROM job_runs WHERE job_name = %s", (IV_JOB,))
    assert row["status"] == "skipped"
    assert "no chain snapshots" in row["error"]
    assert day_is_complete(DAY) is False


def test_iv_rank_appears_once_history_accumulates():
    """D7: the rank becomes real after iv_rank_min_history_days of our own data."""
    minimum = config.universe().option_chains.iv_rank_min_history_days
    stored_chain()
    # Seed synthetic history on earlier sessions.
    from datetime import timedelta

    rows = []
    for i in range(minimum):
        day = DAY - timedelta(days=i + 1)
        rows.append(
            {
                "ts": clock.session_close_utc(day),
                "session_date": day,
                "symbol": "TEST",
                "iv30": 0.20 + i * 0.001,
            }
        )
    db.executemany(
        "INSERT INTO iv_daily (ts, session_date, symbol, iv30) "
        "VALUES (%(ts)s, %(session_date)s, %(symbol)s, %(iv30)s) "
        "ON CONFLICT (symbol, ts) DO UPDATE SET iv30 = EXCLUDED.iv30",
        rows,
    )
    derive_iv(session_date=DAY)
    row = db.fetch_one("SELECT * FROM iv_daily WHERE session_date = %s", (DAY,))
    assert row["iv_rank_252"] is not None
    assert row["history_days"] == minimum


# ── both chain jobs are critical ────────────────────────────────────────────


def test_both_chain_jobs_record_critical_runs():
    """D7: chains and the IV derived from them are the sacred pair."""
    stored_chain()
    with job_run(CHAINS_JOB, session_date=DAY, is_critical=True) as run:
        run.add_rows(56)
    derive_iv(session_date=DAY)

    flags = {
        r["job_name"]: r["is_critical"]
        for r in db.fetch_all("SELECT job_name, is_critical FROM job_runs")
    }
    assert flags == {CHAINS_JOB: True, IV_JOB: True}
    assert day_is_complete(DAY) is True


def test_a_failed_chain_pull_blocks_the_day():
    with pytest.raises(RuntimeError), job_run(CHAINS_JOB, session_date=DAY, is_critical=True):
        raise RuntimeError("token expired mid-run")
    assert day_is_complete(DAY) is False
    assert [v["job_name"] for v in failed_critical_jobs(DAY)] == [CHAINS_JOB]


def test_the_two_jobs_fail_independently_so_the_veto_list_distinguishes_them():
    """The reason they are separate runs: a broken chain PULL and broken IV
    MATHS need different fixes, and the orchestrator should say which."""
    stored_chain()
    with job_run(CHAINS_JOB, session_date=DAY, is_critical=True) as run:
        run.add_rows(56)
    with pytest.raises(RuntimeError), job_run(IV_JOB, session_date=DAY, is_critical=True):
        raise RuntimeError("iv maths exploded")

    assert [v["job_name"] for v in failed_critical_jobs(DAY)] == [IV_JOB]
    assert day_is_complete(DAY) is False


# ── prices ──────────────────────────────────────────────────────────────────


def test_the_price_upsert_is_idempotent_and_stores_raw_bars():
    rows = parse_candles(fx.price_history(days=5), "TEST")
    assert db.executemany(UPSERT_PRICE, rows) == 5
    assert db.executemany(UPSERT_PRICE, rows) == 5
    assert db.fetch_one("SELECT count(*) AS n FROM prices_daily")["n"] == 5

    stored = db.fetch_one("SELECT * FROM prices_daily ORDER BY ts LIMIT 1")
    assert [c for c in stored if c.startswith("adj_")] == []  # D24
    assert clock.session_date(stored["ts"]) == stored["session_date"]


# ── has_options arms the D14 gate ───────────────────────────────────────────


def test_has_options_is_written_both_ways():
    db.executemany(
        "INSERT INTO tickers (symbol, in_sp500) VALUES (%s, true) ON CONFLICT DO NOTHING",
        [("NOOPT",), ("TIMEDOUT",)],
    )
    assert record_options_availability({"TEST": True, "NOOPT": False}) == 2

    flags = {
        r["symbol"]: r["has_options"]
        for r in db.fetch_all("SELECT symbol, has_options FROM tickers")
    }
    assert flags["TEST"] is True
    assert flags["NOOPT"] is False
    # A symbol whose chain request FAILED is absent from `observed`, so it stays
    # NULL. A timeout is not evidence that a stock has no options.
    assert flags["TIMEDOUT"] is None


def test_the_writeback_is_idempotent():
    record_options_availability({"TEST": True})
    assert record_options_availability({"TEST": True}) == 1  # statement sent, no change
    assert (
        db.fetch_one("SELECT has_options FROM tickers WHERE symbol='TEST'")["has_options"] is True
    )


def test_a_universe_rebuild_does_not_erase_what_the_chain_job_discovered():
    """universe.py's upsert COALESCEs has_options, because the chain job is the
    only writer that knows."""
    from ingest.universe import Candidate, upsert_tickers

    record_options_availability({"TEST": True})
    upsert_tickers(
        [Candidate(symbol="TEST", avg_volume=9_000_000, has_options=None)],
        session_date=DAY,
        source="static",
    )
    assert (
        db.fetch_one("SELECT has_options FROM tickers WHERE symbol='TEST'")["has_options"] is True
    )
