"""Static checks on db/schema.sql. No database, no network.

Two jobs:

1. Keep `db/init.py`'s expectation lists in sync with the schema. Add a table to
   schema.sql without adding it to EXPECTED_TABLES and the orchestrator's
   preflight would happily report "database is ready" on a half-built database.
2. Lock the invariants that are decisions rather than preferences — no retention
   policies, no adjusted price columns, snapshot dates in the primary key. Each
   of those is cheap to break by accident and expensive or impossible to undo.
"""

from __future__ import annotations

import re

from db.init import EXPECTED_HYPERTABLES, EXPECTED_TABLES, EXPECTED_VIEWS, SCHEMA_PATH

SCHEMA = SCHEMA_PATH.read_text()
# Comments stripped, for checks that must match executable SQL only: the
# schema explains in prose why there are no retention policies, and a naive
# substring search would flag that explanation as a violation.
SCHEMA_CODE = re.sub(r"--[^\n]*", "", SCHEMA)

CREATE_TABLE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", re.IGNORECASE)
CREATE_HYPERTABLE = re.compile(r"create_hypertable\(\s*'(\w+)'", re.IGNORECASE)
CREATE_VIEW = re.compile(r"CREATE OR REPLACE VIEW\s+(\w+)", re.IGNORECASE)
COMPRESSION_POLICY = re.compile(
    r"add_compression_policy\(\s*'(\w+)',\s*INTERVAL\s*'([^']+)'", re.IGNORECASE
)


# ── the expectation lists match reality ─────────────────────────────────────


def test_expected_tables_match_schema():
    declared = set(CREATE_TABLE.findall(SCHEMA_CODE))
    assert declared == set(EXPECTED_TABLES), (
        "db/init.EXPECTED_TABLES is out of sync with schema.sql: "
        f"only in schema={sorted(declared - set(EXPECTED_TABLES))}, "
        f"only in EXPECTED_TABLES={sorted(set(EXPECTED_TABLES) - declared)}"
    )


def test_expected_hypertables_match_schema():
    assert set(CREATE_HYPERTABLE.findall(SCHEMA)) == set(EXPECTED_HYPERTABLES)


def test_the_api_budget_table_is_keyed_per_provider_and_day():
    """D29: the counter must be shared across processes and roll over with the
    ET trading day, so its key is (provider, session_date) — not a global row
    and not a UTC timestamp."""
    pk = re.search(r"CONSTRAINT pk_api_budget\s*\n?\s*PRIMARY KEY \(([^)]*)\)", SCHEMA)
    assert pk
    assert {p.strip() for p in pk.group(1).split(",")} == {"provider", "session_date"}
    body = _table_body("api_budget")
    assert "calls_declined" in body, "a declined-spend count is needed for reporting"


def test_expected_views_match_schema():
    assert set(CREATE_VIEW.findall(SCHEMA_CODE)) == set(EXPECTED_VIEWS)


def test_d20_hypertables_are_exactly_the_four_named():
    """CLAUDE.md/D20 names these four explicitly."""
    assert set(EXPECTED_HYPERTABLES) == {
        "prices_daily",
        "option_chain_snapshots",
        "estimate_snapshots",
        "iv_daily",
    }


# ── compression and retention ───────────────────────────────────────────────


def test_every_hypertable_has_a_compression_policy():
    policies = {name for name, _ in COMPRESSION_POLICY.findall(SCHEMA)}
    assert policies == set(EXPECTED_HYPERTABLES)


def test_compression_delays_are_staggered_as_specified():
    """CLAUDE.md: chains 14d, estimates 90d, iv 180d, prices 365d.

    Each delay is longer than any window its job might backfill into, because a
    compressed chunk rejects ordinary UPDATEs. Prices are last: corporate
    actions can arrive late and features read long trailing windows.
    """
    assert dict(COMPRESSION_POLICY.findall(SCHEMA)) == {
        "option_chain_snapshots": "14 days",
        "estimate_snapshots": "90 days",
        "iv_daily": "180 days",
        "prices_daily": "365 days",
    }


def test_no_retention_policies():
    """D7's premise is that snapshot history accumulates forever.

    A retention policy would silently delete the only copy of the engine's edge.
    `db/init.py --check` also catches one added at runtime, outside this file.
    """
    assert "add_retention_policy(" not in SCHEMA_CODE


# ── idempotency ─────────────────────────────────────────────────────────────


def test_schema_is_written_idempotently():
    """db/init.py applies this whole file on every deploy."""
    bare = re.findall(
        r"CREATE\s+(?:TABLE|INDEX|UNIQUE INDEX|VIEW)\s+(?!IF NOT EXISTS)(?!OR REPLACE)(\w+)",
        SCHEMA,
        re.IGNORECASE,
    )
    assert bare == [], f"not re-runnable: {bare}"


def test_hypertable_and_policy_calls_are_idempotent():
    for call in CREATE_HYPERTABLE.finditer(SCHEMA):
        tail = SCHEMA[call.start() : SCHEMA.index(";", call.start())]
        assert "if_not_exists" in tail, f"create_hypertable missing if_not_exists: {tail[:60]}"
    assert SCHEMA.count("add_compression_policy(") == SCHEMA.count("if_not_exists => TRUE")


# ── invariants that are decisions, not preferences ──────────────────────────


def test_snapshot_tables_keep_the_snapshot_date_in_the_primary_key():
    """D7: a snapshot that overwrites a prior day destroys the revision signal."""
    for table, time_col in [
        ("option_chain_snapshots", "snapshot_ts"),
        ("estimate_snapshots", "snapshot_date"),
    ]:
        pk = re.search(rf"CONSTRAINT pk_{table}\s*\n?\s*PRIMARY KEY \(([^)]*)\)", SCHEMA)
        assert pk, f"no primary key found for {table}"
        assert time_col in pk.group(1), f"{table} PK must include {time_col}"


def test_prices_daily_has_no_adjusted_columns():
    """D24: adjusted series are computed at read time, never stored.

    A stored adj_close would have to be restated on every corporate action and
    would make a backtest run today disagree with the same run last quarter.
    """
    body = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS prices_daily") :]
    body = body[: body.index(");")]
    assert "adj_" not in body
    for banned in ("dividend_amount", "split_factor"):
        assert banned not in body, f"prices_daily must not carry {banned} (D24)"


def _table_body(table: str) -> str:
    body = SCHEMA[SCHEMA.index(f"CREATE TABLE IF NOT EXISTS {table}") :]
    return body[: body.index(");")]


def test_instant_keyed_tables_carry_both_the_utc_instant_and_the_session_date():
    """Dual time keying: `ts TIMESTAMPTZ` is the hypertable dimension, and
    `session_date DATE` is the ET trading day every join and feature uses.

    Deriving one from the other inline is how a bar gets filed under the wrong
    session, so both are stored.
    """
    for table, dimension in [
        ("prices_daily", "ts"),
        ("option_chain_snapshots", "snapshot_ts"),
        ("iv_daily", "ts"),
    ]:
        body = _table_body(table)
        assert re.search(rf"\b{dimension}\s+TIMESTAMPTZ\s+NOT NULL", body), (
            f"{table} lacks a NOT NULL TIMESTAMPTZ {dimension}"
        )
        assert re.search(r"\bsession_date\s+DATE\s+NOT NULL", body), (
            f"{table} is missing a NOT NULL session_date"
        )


def test_estimate_snapshots_is_keyed_on_the_business_date_alone():
    """The one time-series table with no separate session_date, deliberately.

    An estimate snapshot is "the consensus as of this trading day", not an
    instant: there is no meaningful intraday timestamp to preserve, so
    `snapshot_date DATE` is both the hypertable dimension and the business date.
    Adding a redundant session_date would invite the two to disagree.
    """
    body = _table_body("estimate_snapshots")
    assert re.search(r"\bsnapshot_date\s+DATE NOT NULL", body)
    assert "session_date" not in body


def test_nulls_not_distinct_where_a_null_must_still_dedupe():
    """events has NULL symbol for macro; positions has NULL option legs."""
    for constraint in ("uq_events", "uq_positions"):
        block = SCHEMA[SCHEMA.index(f"CONSTRAINT {constraint}") :][:120]
        assert "NULLS NOT DISTINCT" in block, f"{constraint} needs NULLS NOT DISTINCT"


def test_shadow_book_symbol_has_no_foreign_key():
    """D13 logs discretionary ideas, which may be on off-universe names."""
    body = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS shadow_book") :]
    body = body[: body.index(");")]
    symbol_line = next(ln for ln in body.splitlines() if ln.strip().startswith("symbol "))
    assert "REFERENCES" not in symbol_line


def test_job_runs_supports_the_day_complete_rule():
    body = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS job_runs") :]
    body = body[: body.index(");")]
    assert "is_critical" in body
    # The orchestrator must read the view, not a hard-coded job list.
    assert "day_complete" in SCHEMA
    assert "uq_job_runs_inflight" in SCHEMA
    assert "WHERE status = 'running'" in SCHEMA


def test_state_columns_use_text_plus_check_not_native_enums():
    """A new status value should be a one-line migration, not a type rewrite."""
    assert "CREATE TYPE" not in SCHEMA.upper()
    assert SCHEMA.count("CHECK (") >= 15


def test_tickers_deactivate_rather_than_delete():
    body = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS tickers") :]
    body = body[: body.index(");")]
    assert "is_active" in body
    assert "deactivated_on" in body


def test_fundamentals_keeps_a_raw_jsonb_escape_hatch():
    body = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS fundamentals") :]
    body = body[: body.index(");")]
    assert "raw" in body and "JSONB" in body


def test_news_articles_primary_key_is_per_symbol():
    """One article about three tickers stores three rows; the pillar scores per symbol."""
    pk = re.search(r"CONSTRAINT pk_news_articles\s*\n?\s*PRIMARY KEY \(([^)]*)\)", SCHEMA)
    assert pk
    assert {p.strip() for p in pk.group(1).split(",")} == {
        "source_provider",
        "article_id",
        "symbol",
    }


# ── the DDL scanners must read SQL, not prose ────────────────────────────────


def test_a_comment_cannot_inject_a_table_or_view_name():
    """These scanners read SCHEMA_CODE, never the raw text.

    A comment on 2026-10-21 reading "CREATE OR REPLACE VIEW may only append
    columns" made `test_expected_views_match_schema` report a view named `may`.
    Same class as the retention-policy check, which reads SCHEMA_CODE for
    exactly this reason: a sentence about SQL is not SQL.
    """
    prose = (
        "-- CREATE TABLE IF NOT EXISTS ghost_table (id int);\n"
        "-- CREATE OR REPLACE VIEW ghost_view AS SELECT 1;\n"
    )
    code = re.sub(r"--[^\n]*", "", prose)
    assert CREATE_TABLE.findall(code) == []
    assert CREATE_VIEW.findall(code) == []
    # and the real scanners agree about the shipped schema
    assert "may" not in set(CREATE_VIEW.findall(SCHEMA_CODE))
    assert "ghost_table" not in set(CREATE_TABLE.findall(SCHEMA_CODE))
