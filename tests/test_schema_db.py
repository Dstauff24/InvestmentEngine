"""Schema behaviours that only a real database can prove.

Excluded from the default run (`-m "not integration"`), because the unit suite
has no database (CLAUDE.md). Run explicitly against a Postgres with the schema
applied:

    pytest -m integration

What is here is deliberately the set of guarantees a static read of schema.sql
cannot establish: that NULLS NOT DISTINCT really dedupes a NULL option leg, that
the partial unique index really blocks a second concurrent run, that the
updated_at trigger really fires, and that the upserts the ingestion jobs will
use really are idempotent.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import psycopg
import pytest
from psycopg.rows import dict_row

from core import settings
from tests.dbguard import assert_safe_database

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def _guard_database():
    """Refuse to wipe anything that is not a scratch database.

    Module-scoped and autouse so it fires before any fixture connects, let
    alone deletes. See tests/dbguard.py for why the override is not readable
    from `.env`.
    """
    return assert_safe_database(settings.database_url())


DAY = date(2026, 10, 20)
TS = datetime(2026, 10, 20, 20, 0, tzinfo=UTC)
PREFIX = "ZZTEST"


@pytest.fixture
def conn():
    dsn = settings.database_url()
    assert_safe_database(dsn)  # re-checked here: nothing connects unguarded
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        c.autocommit = True
        yield c


@pytest.fixture(autouse=True)
def clean(conn):
    def wipe():
        for table in (
            "option_chain_snapshots",
            "estimate_snapshots",
            "iv_daily",
            "prices_daily",
            "corporate_actions",
            "positions",
            "events",
            "job_runs",
            "tickers",
        ):
            conn.execute(f"DELETE FROM {table} WHERE true")

    wipe()
    yield
    wipe()


@pytest.fixture
def ticker(conn):
    conn.execute(
        "INSERT INTO tickers (symbol, company_name, in_sp500) VALUES (%s, %s, true)",
        (f"{PREFIX}A", "Test Co"),
    )
    return f"{PREFIX}A"


# ── append-only upserts are idempotent ──────────────────────────────────────


def test_price_upsert_is_idempotent(conn, ticker):
    """Re-running a day must refresh, never duplicate (CLAUDE.md)."""
    sql = """
        INSERT INTO prices_daily (ts, session_date, symbol, open, high, low, close, volume)
        VALUES (%s, %s, %s, 1, 2, 0.5, %s, 100)
        ON CONFLICT (symbol, ts) DO UPDATE SET close = EXCLUDED.close
    """
    conn.execute(sql, (TS, DAY, ticker, 1.5))
    conn.execute(sql, (TS, DAY, ticker, 1.75))
    rows = conn.execute("SELECT close FROM prices_daily").fetchall()
    assert len(rows) == 1
    assert float(rows[0]["close"]) == 1.75


def test_two_snapshot_days_coexist(conn, ticker):
    """D7: the signal IS the diff between consecutive snapshot_dates, so a
    second day must add a row rather than replace the first."""
    sql = """
        INSERT INTO estimate_snapshots
            (snapshot_date, symbol, period_type, fiscal_period_end, estimated_eps_avg)
        VALUES (%s, %s, 'quarter', %s, %s)
        ON CONFLICT (symbol, snapshot_date, period_type, fiscal_period_end)
        DO UPDATE SET estimated_eps_avg = EXCLUDED.estimated_eps_avg
    """
    conn.execute(sql, (DAY, ticker, date(2026, 12, 31), 2.30))
    conn.execute(sql, (date(2026, 10, 21), ticker, date(2026, 12, 31), 2.35))
    rows = conn.execute(
        "SELECT snapshot_date, estimated_eps_avg FROM estimate_snapshots ORDER BY snapshot_date"
    ).fetchall()
    assert len(rows) == 2
    revision = float(rows[1]["estimated_eps_avg"]) - float(rows[0]["estimated_eps_avg"])
    assert revision == pytest.approx(0.05)


def test_chain_primary_key_separates_strikes_and_sides(conn, ticker):
    sql = """
        INSERT INTO option_chain_snapshots
            (snapshot_ts, session_date, symbol, expiration, strike, option_type)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (symbol, snapshot_ts, expiration, strike, option_type)
        DO UPDATE SET ingested_at = now()
    """
    for strike, side in [(100, "C"), (100, "P"), (105, "C")]:
        conn.execute(sql, (TS, DAY, ticker, date(2026, 11, 20), strike, side))
    conn.execute(sql, (TS, DAY, ticker, date(2026, 11, 20), 100, "C"))  # repeat
    n = conn.execute("SELECT count(*) AS n FROM option_chain_snapshots").fetchone()["n"]
    assert n == 3


# ── constraints that protect the adjustment log ─────────────────────────────


def test_a_split_without_a_ratio_is_rejected(conn, ticker):
    """A NULL ratio would be a silently wrong adjustment factor (D24)."""
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO corporate_actions (symbol, action_type, ex_date, source) "
            "VALUES (%s, 'split', %s, 'test')",
            (ticker, DAY),
        )


def test_a_dividend_without_an_amount_is_rejected(conn, ticker):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO corporate_actions (symbol, action_type, ex_date, source) "
            "VALUES (%s, 'cash_dividend', %s, 'test')",
            (ticker, DAY),
        )


def test_a_zero_split_ratio_is_rejected(conn, ticker):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO corporate_actions (symbol, action_type, ex_date, split_ratio, source) "
            "VALUES (%s, 'split', %s, 0, 'test')",
            (ticker, DAY),
        )


def test_a_well_formed_split_and_dividend_are_accepted(conn, ticker):
    conn.execute(
        "INSERT INTO corporate_actions (symbol, action_type, ex_date, split_ratio, source) "
        "VALUES (%s, 'split', %s, 4.0, 'test')",
        (ticker, DAY),
    )
    conn.execute(
        "INSERT INTO corporate_actions (symbol, action_type, ex_date, cash_amount, source) "
        "VALUES (%s, 'cash_dividend', %s, 0.26, 'test')",
        (ticker, DAY),
    )
    assert conn.execute("SELECT count(*) AS n FROM corporate_actions").fetchone()["n"] == 2


# ── NULLS NOT DISTINCT ──────────────────────────────────────────────────────


def test_a_macro_event_with_a_null_symbol_still_dedupes(conn):
    """Default NULL semantics would let the same CPI print insert twice."""
    sql = """
        INSERT INTO events (symbol, event_type, event_date, title, source)
        VALUES (NULL, 'macro', %s, %s, 'test')
        ON CONFLICT (symbol, event_type, event_date) DO UPDATE SET title = EXCLUDED.title
    """
    conn.execute(sql, (DAY, "CPI"))
    conn.execute(sql, (DAY, "CPI revised"))
    rows = conn.execute("SELECT title FROM events").fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "CPI revised"


def test_an_equity_position_with_null_option_legs_still_dedupes(conn):
    sql = """
        INSERT INTO positions (as_of, as_of_date, account_hash, symbol, asset_type, quantity)
        VALUES (%s, %s, 'acct', %s, 'EQUITY', %s)
        ON CONFLICT (account_hash, as_of_date, symbol, expiration, strike, put_call)
        DO UPDATE SET quantity = EXCLUDED.quantity
    """
    conn.execute(sql, (TS, DAY, f"{PREFIX}A", 100))
    conn.execute(sql, (TS, DAY, f"{PREFIX}A", 150))
    rows = conn.execute("SELECT quantity FROM positions").fetchall()
    assert len(rows) == 1
    assert float(rows[0]["quantity"]) == 150


# ── job_runs: the day-complete rule ─────────────────────────────────────────


def start_job(conn, name, *, critical=False):
    return conn.execute(
        "INSERT INTO job_runs (job_name, session_date, status, is_critical) "
        "VALUES (%s, %s, 'running', %s) RETURNING id",
        (name, DAY, critical),
    ).fetchone()["id"]


def finish_job(conn, job_id, status):
    conn.execute(
        "UPDATE job_runs SET status = %s, finished_at = now() WHERE id = %s", (status, job_id)
    )


def day_complete(conn):
    row = conn.execute(
        "SELECT day_complete FROM v_day_status WHERE session_date = %s", (DAY,)
    ).fetchone()
    return bool(row and row["day_complete"])


def test_a_second_concurrent_run_of_a_job_is_blocked(conn):
    """uq_job_runs_inflight: an overlapping cron fire must write nothing."""
    start_job(conn, "snapshot_chains")
    with pytest.raises(psycopg.errors.UniqueViolation):
        start_job(conn, "snapshot_chains")


def test_the_same_job_can_run_again_once_the_first_finished(conn):
    first = start_job(conn, "snapshot_chains")
    finish_job(conn, first, "failed")
    second = start_job(conn, "snapshot_chains")  # a retry must be allowed
    assert second != first


def test_a_failed_critical_job_blocks_the_day(conn):
    """The rule in CLAUDE.md, enforced by the view the orchestrator reads."""
    ok = start_job(conn, "fmp_events", critical=False)
    finish_job(conn, ok, "success")
    chains = start_job(conn, "snapshot_chains", critical=True)
    finish_job(conn, chains, "success")
    assert day_complete(conn) is True

    estimates = start_job(conn, "fmp_estimates", critical=True)
    finish_job(conn, estimates, "failed")
    assert day_complete(conn) is False


def test_a_skipped_critical_job_also_blocks_the_day(conn):
    """A snapshot that was skipped is a snapshot we do not have."""
    job = start_job(conn, "snapshot_chains", critical=True)
    finish_job(conn, job, "skipped")
    assert day_complete(conn) is False


def test_a_partial_critical_job_also_blocks_the_day(conn):
    job = start_job(conn, "snapshot_chains", critical=True)
    finish_job(conn, job, "partial")
    assert day_complete(conn) is False


def test_a_day_with_no_critical_jobs_is_not_complete(conn):
    """Otherwise a night where the snapshot jobs never started would look fine."""
    job = start_job(conn, "finnhub_news", critical=False)
    finish_job(conn, job, "success")
    assert day_complete(conn) is False


# ── triggers and views ──────────────────────────────────────────────────────


def test_updated_at_fires_without_the_writer_setting_it(conn, ticker):
    before = conn.execute("SELECT updated_at FROM tickers WHERE symbol = %s", (ticker,)).fetchone()[
        "updated_at"
    ]
    conn.execute("UPDATE tickers SET sector = 'Technology' WHERE symbol = %s", (ticker,))
    after = conn.execute("SELECT updated_at FROM tickers WHERE symbol = %s", (ticker,)).fetchone()[
        "updated_at"
    ]
    assert after > before


def test_the_active_universe_view_hides_deactivated_symbols(conn, ticker):
    assert conn.execute("SELECT count(*) AS n FROM v_active_universe").fetchone()["n"] == 1
    conn.execute(
        "UPDATE tickers SET is_active = false, deactivated_on = %s WHERE symbol = %s",
        (DAY, ticker),
    )
    assert conn.execute("SELECT count(*) AS n FROM v_active_universe").fetchone()["n"] == 0
    # Deactivated, NOT deleted: historical ideas keep a valid FK.
    assert conn.execute("SELECT count(*) AS n FROM tickers").fetchone()["n"] == 1


def test_deleting_a_ticker_would_cascade_history_away(conn, ticker):
    """Why ingest/universe.py deactivates instead of deleting."""
    conn.execute(
        "INSERT INTO corporate_actions (symbol, action_type, ex_date, cash_amount, source) "
        "VALUES (%s, 'cash_dividend', %s, 0.26, 'test')",
        (ticker, DAY),
    )
    conn.execute("DELETE FROM tickers WHERE symbol = %s", (ticker,))
    assert conn.execute("SELECT count(*) AS n FROM corporate_actions").fetchone()["n"] == 0


def test_the_four_hypertables_and_their_policies_exist(conn):
    hyper = {
        r["hypertable_name"]
        for r in conn.execute(
            "SELECT hypertable_name FROM timescaledb_information.hypertables"
        ).fetchall()
    }
    assert hyper == {"prices_daily", "option_chain_snapshots", "estimate_snapshots", "iv_daily"}

    policies = {
        r["hypertable_name"]: r["config"]["compress_after"]
        for r in conn.execute(
            "SELECT hypertable_name, config FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_compression'"
        ).fetchall()
    }
    assert policies == {
        "option_chain_snapshots": "14 days",
        "estimate_snapshots": "90 days",
        "iv_daily": "180 days",
        "prices_daily": "365 days",
    }


def test_no_retention_policy_is_installed(conn):
    """D7: belt and braces with the static test — this catches one added by hand."""
    rows = conn.execute(
        "SELECT hypertable_name FROM timescaledb_information.jobs "
        "WHERE proc_name = 'policy_retention'"
    ).fetchall()
    assert rows == []
