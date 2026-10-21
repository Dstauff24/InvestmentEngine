"""Create the engine database and apply the schema — idempotently.

    python -m db.init                 # create DB if missing, apply schema + migrations
    python -m db.init --check         # verify only; exits 1 if anything is missing
    python -m db.init --skip-create   # schema only (DB exists, e.g. managed Postgres)

Safe to run on every deploy. Nothing here drops or rewrites data: `schema.sql`
is written entirely in CREATE-IF-NOT-EXISTS form, and migrations are applied
once and recorded in `schema_migrations`.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import psycopg
from psycopg import sql

from core import settings

DB_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = DB_DIR / "schema.sql"
MIGRATIONS_DIR = DB_DIR / "migrations"

# Objects `--check` insists on. tests/test_schema.py keeps these in sync with
# schema.sql, so the orchestrator's preflight cannot silently pass on a
# half-built database.
EXPECTED_TABLES = [
    "alerts",
    "api_budget",
    "corporate_actions",
    "daily_scoring_stats",
    "estimate_snapshots",
    "events",
    "fundamentals",
    "ideas",
    "insider_transactions",
    "iv_daily",
    "job_runs",
    "news_articles",
    "option_chain_snapshots",
    "positions",
    "prices_daily",
    "schema_migrations",
    "shadow_book",
    "tickers",
]
EXPECTED_HYPERTABLES = [
    "estimate_snapshots",
    "iv_daily",
    "option_chain_snapshots",
    "prices_daily",
]
EXPECTED_VIEWS = ["v_active_universe", "v_day_status"]


# ── Database creation ───────────────────────────────────────────────────────


def create_database_if_missing() -> bool:
    """Create the engine database using the admin credentials. True if created.

    CREATE DATABASE has no IF NOT EXISTS and cannot run inside a transaction, so
    this checks `pg_database` first and runs autocommit.
    """
    name = settings.database_name()
    dsn = settings.admin_database_url()
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
        if cur.fetchone():
            print(f"  database {name!r} already exists")
            return False
        owner = settings.get("PGUSER")
        stmt = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
        if owner:
            stmt = sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(name), sql.Identifier(owner)
            )
        cur.execute(stmt)
        print(f"  created database {name!r}")
        return True


# ── Schema ──────────────────────────────────────────────────────────────────


def apply_schema() -> None:
    """Apply schema.sql to the engine database."""
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"missing {SCHEMA_PATH}")

    with psycopg.connect(settings.database_url(), autocommit=True) as conn:
        # TimescaleDB refuses to run its own functions in the same transaction
        # that created the extension, so the extension gets its own statement
        # first. schema.sql's own CREATE EXTENSION is then a no-op.
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        with conn.cursor() as cur:
            cur.execute(SCHEMA_PATH.read_text())
        print(f"  applied {SCHEMA_PATH.name}")


# ── Migrations ──────────────────────────────────────────────────────────────


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def apply_migrations() -> int:
    """Apply every unapplied db/migrations/*.sql in filename order.

    Files are expected to be named `NNN_short_description.sql`. An already-
    applied file whose contents changed is a hard error: extend the history,
    never rewrite it.
    """
    if not MIGRATIONS_DIR.exists():
        return 0
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        return 0

    applied = 0
    with psycopg.connect(settings.database_url(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version, checksum FROM schema_migrations")
            known = dict(cur.fetchall())

        for path in files:
            body = path.read_text()
            digest = _checksum(body)
            if path.name in known:
                if known[path.name] and known[path.name] != digest:
                    raise RuntimeError(
                        f"migration {path.name} was modified after it was applied "
                        f"(checksum mismatch). Add a new migration instead."
                    )
                continue
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(body)
                cur.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                    (path.name, digest),
                )
            print(f"  applied migration {path.name}")
            applied += 1
    return applied


# ── Verification ────────────────────────────────────────────────────────────


def check() -> list[str]:
    """Return a list of problems. An empty list means the database is ready."""
    problems: list[str] = []
    try:
        conn = psycopg.connect(settings.database_url())
    except psycopg.Error as exc:
        return [f"cannot connect: {exc}"]

    with conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
        if not cur.fetchone():
            problems.append("timescaledb extension is not installed")

        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        present = {r[0] for r in cur.fetchall()}
        problems += [f"missing table: {t}" for t in EXPECTED_TABLES if t not in present]

        cur.execute("SELECT table_name FROM information_schema.views WHERE table_schema = 'public'")
        views = {r[0] for r in cur.fetchall()}
        problems += [f"missing view: {v}" for v in EXPECTED_VIEWS if v not in views]

        cur.execute("SELECT hypertable_name FROM timescaledb_information.hypertables")
        hyper = {r[0] for r in cur.fetchall()}
        problems += [f"not a hypertable: {t}" for t in EXPECTED_HYPERTABLES if t not in hyper]

        cur.execute(
            "SELECT hypertable_name FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_compression'"
        )
        compressed = {r[0] for r in cur.fetchall()}
        problems += [
            f"no compression policy: {t}" for t in EXPECTED_HYPERTABLES if t not in compressed
        ]

        # D7: a retention policy anywhere would delete the edge.
        cur.execute(
            "SELECT hypertable_name FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_retention'"
        )
        for row in cur.fetchall():
            problems.append(f"RETENTION POLICY on {row[0]} — D7 history must never be dropped")
    return problems


def summarize() -> None:
    with psycopg.connect(settings.database_url()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        tables = cur.fetchone()[0]
        cur.execute(
            "SELECT h.hypertable_name, "
            "  (SELECT config->>'compress_after' FROM timescaledb_information.jobs j "
            "   WHERE j.hypertable_name = h.hypertable_name "
            "     AND j.proc_name = 'policy_compression') "
            "FROM timescaledb_information.hypertables h ORDER BY 1"
        )
        rows = cur.fetchall()

    print(f"\n  {tables} tables")
    for name, compress_after in rows:
        print(f"  hypertable {name:<24} compress after {compress_after or 'NO POLICY'}")


# ── Entry point ─────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--check", action="store_true", help="verify only, change nothing")
    parser.add_argument("--skip-create", action="store_true", help="do not attempt CREATE DATABASE")
    args = parser.parse_args(argv)

    if args.check:
        problems = check()
        if problems:
            print("database is NOT ready:")
            for p in problems:
                print(f"  x {p}")
            return 1
        print("database is ready")
        summarize()
        return 0

    print(f"initializing {settings.database_name()!r} on {settings.get('PGHOST', 'localhost')}")
    if not args.skip_create:
        create_database_if_missing()
    # SCHEMA, MIGRATIONS, THEN SCHEMA AGAIN.
    #
    # The second pass is not belt-and-braces; it is required. On a FRESH
    # database schema.sql creates everything and the migrations are no-ops. On
    # an EXISTING one, `CREATE TABLE IF NOT EXISTS` skips a table that already
    # exists — so a column a migration adds is absent during the first pass,
    # and any schema.sql statement depending on that column fails.
    #
    # That is not hypothetical. Migration 006 added `shadow_book.status_at_call`
    # and schema.sql gained an index on it; on a database holding migrations
    # 001-005 the first pass died on "column status_at_call does not exist",
    # which would have broken the next production deploy — `git pull &&
    # python -m db.init` on a VM that was provisioned before the migration.
    #
    # The first pass is still needed: migrations ALTER tables that must already
    # exist. Reversing the order would break a fresh database instead. So the
    # first pass is allowed to fail on exactly this class of error, the
    # migrations run, and the second pass completes the schema. schema.sql is
    # idempotent throughout (IF NOT EXISTS / CREATE OR REPLACE), so running it
    # twice costs a second of redundant DDL and nothing else.
    first_pass_error = None
    try:
        apply_schema()
    except psycopg.errors.UndefinedColumn as exc:
        # A pending migration almost certainly adds the column. Carry on and
        # report it only if the second pass cannot recover.
        first_pass_error = exc
        print(f"  schema pass 1 incomplete ({str(exc).splitlines()[0]}); migrations first")

    try:
        count = apply_migrations()
    except (RuntimeError, psycopg.Error) as exc:
        print(f"\nmigration failed: {exc}")
        return 1

    try:
        apply_schema()
    except psycopg.Error as exc:
        print(f"\nschema failed after migrations: {exc}")
        if first_pass_error is not None:
            print(f"  (pass 1 also failed: {first_pass_error})")
        return 1

    if count == 0:
        print("  no pending migrations")

    problems = check()
    if problems:
        print("\ninitialization finished but verification failed:")
        for p in problems:
            print(f"  x {p}")
        return 1
    summarize()
    print("\nok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
