"""Guards the D7 snapshot history against destructive SQL. Static analysis only.

D7's edge — IV rank, estimate-revision momentum — is the DIFFERENCE between
consecutive daily snapshots. A DELETE that removes a day, or an INSERT that
quietly does nothing on conflict instead of refreshing, destroys signal that
cannot be reconstructed: the option chain as it stood on a past afternoon is not
available from any vendor at any price. No backup fixes it either, because the
loss looks like a normal row count.

So the rule is enforced by a test that READS THE SOURCE, not by a convention in
CLAUDE.md that a future edit can forget.

Two complementary scans:

  * destructive statements are matched against the RAW file text, so a query
    assembled from pieces still trips the check. That is deliberately
    over-broad — even a comment containing ``DELETE FROM iv_daily`` fails. Say
    it in prose ("rows here are never removed") rather than in SQL syntax.
  * the ON CONFLICT requirement is matched per string literal (via ``ast``),
    because an INSERT and its conflict clause live in the same literal in any
    code we would actually ship.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Append-only for the life of the project.
#   - the three snapshot tables are D7's raison d'etre
#   - prices_daily joined them under D24: it stores raw reported bars, which
#     never legitimately need deleting
PROTECTED_TABLES = (
    "option_chain_snapshots",
    "estimate_snapshots",
    "iv_daily",
    "prices_daily",
)

# Packages scanned. db/ is excluded: db/init.py legitimately manages schema
# objects, and db/schema.sql is not Python.
SCANNED_PACKAGES = (
    "ingest",
    "jobs",
    "features",
    "scoring",
    "decision",
    "shadow_book",
    "alerts",
    "core",
    "backtest",
    "scripts",
)

_TABLES = "|".join(PROTECTED_TABLES)
DESTRUCTIVE = re.compile(
    rf"\b(?:DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?|DROP\s+TABLE(?:\s+IF\s+EXISTS)?)\s+"
    rf"(?:public\.)?(?P<table>{_TABLES})\b",
    re.IGNORECASE,
)
INSERT_INTO = re.compile(rf"\bINSERT\s+INTO\s+(?:public\.)?(?P<table>{_TABLES})\b", re.IGNORECASE)
DO_UPDATE = re.compile(r"\bON\s+CONFLICT\b.*?\bDO\s+UPDATE\b", re.IGNORECASE | re.DOTALL)


# ── the checkers ────────────────────────────────────────────────────────────


def find_destructive(source: str) -> list[str]:
    """Return the protected tables this source destroys rows in."""
    return [m.group("table") for m in DESTRUCTIVE.finditer(source)]


def find_non_upserting_inserts(sql: str) -> list[str]:
    """Return protected tables inserted into without ON CONFLICT ... DO UPDATE."""
    offenders = []
    for match in INSERT_INTO.finditer(sql):
        # Look from this INSERT up to the next one (or the end of the literal).
        following = INSERT_INTO.search(sql, match.end())
        statement = sql[match.start() : following.start() if following else len(sql)]
        if not DO_UPDATE.search(statement):
            offenders.append(match.group("table"))
    return offenders


def string_literals(source: str) -> list[str]:
    """Every string constant in a module, with f-string holes blanked out."""
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            found.append(
                "".join(
                    part.value
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                    else " ? "
                    for part in node.values
                )
            )
    return found


def python_files() -> list[Path]:
    files: list[Path] = []
    for package in SCANNED_PACKAGES:
        directory = REPO_ROOT / package
        if directory.is_dir():
            files.extend(sorted(directory.rglob("*.py")))
    return files


# ══════════════════════════════════════════════════════════════════════════
# CANARY SELF-TESTS — the detector must actually work
# ══════════════════════════════════════════════════════════════════════════
# Without these, the repo scan below passes VACUOUSLY and nobody notices it
# never had teeth. These prove the detector catches what it claims to.


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM option_chain_snapshots WHERE session_date = %s",
        "delete from estimate_snapshots",
        "TRUNCATE iv_daily",
        "TRUNCATE TABLE public.prices_daily",
        "DROP TABLE IF EXISTS option_chain_snapshots",
        "DELETE  FROM\n  iv_daily\nWHERE symbol = %s",
        "DROP TABLE prices_daily CASCADE",
    ],
)
def test_canary_detector_catches_destructive_sql(sql):
    assert find_destructive(sql)


@pytest.mark.parametrize(
    "sql",
    [
        # Unprotected tables may legitimately be deleted from.
        "DELETE FROM news_articles WHERE published_at < %s",
        "DELETE FROM alerts WHERE status = 'sent'",
        "DELETE FROM job_runs WHERE true",
        "SELECT * FROM iv_daily WHERE symbol = %s",
        "UPDATE iv_daily SET iv30 = %s WHERE symbol = %s",
        "INSERT INTO iv_daily (symbol) VALUES (%s) ON CONFLICT (symbol, ts) DO UPDATE SET x = 1",
    ],
)
def test_canary_detector_allows_legitimate_sql(sql):
    assert not find_destructive(sql)


def test_canary_detector_catches_an_insert_without_on_conflict():
    assert find_non_upserting_inserts(
        "INSERT INTO estimate_snapshots (symbol, snapshot_date) VALUES (%s, %s)"
    ) == ["estimate_snapshots"]


def test_canary_detector_catches_do_nothing_which_would_skip_a_refresh():
    """DO NOTHING would silently drop a corrected re-pull on the same day."""
    assert find_non_upserting_inserts(
        "INSERT INTO iv_daily (symbol, ts) VALUES (%s, %s) ON CONFLICT DO NOTHING"
    ) == ["iv_daily"]


def test_canary_detector_accepts_a_proper_upsert():
    assert (
        find_non_upserting_inserts(
            """
            INSERT INTO option_chain_snapshots (symbol, snapshot_ts, strike)
            VALUES (%s, %s, %s)
            ON CONFLICT (symbol, snapshot_ts, expiration, strike, option_type)
            DO UPDATE SET bid = EXCLUDED.bid
            """
        )
        == []
    )


def test_canary_detector_checks_each_insert_separately():
    two = """
        INSERT INTO iv_daily (symbol) VALUES (%s) ON CONFLICT (symbol, ts) DO UPDATE SET a = 1;
        INSERT INTO prices_daily (symbol) VALUES (%s);
    """
    assert find_non_upserting_inserts(two) == ["prices_daily"]


def test_canary_detector_sees_through_f_strings():
    module = 'table = "iv_daily"\nsql = f"DELETE FROM iv_daily WHERE symbol = {sym}"\n'
    assert any(find_destructive(lit) for lit in string_literals(module))


def test_canary_a_planted_violation_would_be_caught_by_the_repo_scan(tmp_path):
    """Proves the scan mechanism, not just the regex."""
    bad = tmp_path / "_canary.py"
    bad.write_text('SQL = "DELETE FROM option_chain_snapshots WHERE session_date = %s"')
    assert find_destructive(bad.read_text()) == ["option_chain_snapshots"]


# ══════════════════════════════════════════════════════════════════════════
# The actual repo scan
# ══════════════════════════════════════════════════════════════════════════


def test_no_module_destroys_snapshot_history():
    offenders = {}
    for path in python_files():
        hits = find_destructive(path.read_text())
        if hits:
            offenders[str(path.relative_to(REPO_ROOT))] = sorted(set(hits))
    assert offenders == {}, (
        "D7 snapshot history must be append-only; these modules issue destructive "
        f"SQL against protected tables: {offenders}"
    )


def test_every_snapshot_insert_is_an_upsert():
    offenders = {}
    for path in python_files():
        hits: list[str] = []
        for literal in string_literals(path.read_text()):
            hits.extend(find_non_upserting_inserts(literal))
        if hits:
            offenders[str(path.relative_to(REPO_ROOT))] = sorted(set(hits))
    assert offenders == {}, (
        "writes to snapshot tables must be ON CONFLICT ... DO UPDATE so a re-run "
        f"refreshes instead of duplicating or silently skipping: {offenders}"
    )


def test_the_scan_is_not_vacuous():
    """At least one protected INSERT must exist, or the scan proves nothing.

    If every writer were removed, test_every_snapshot_insert_is_an_upsert would
    pass trivially and nobody would notice.
    """
    found = 0
    for path in python_files():
        for literal in string_literals(path.read_text()):
            if INSERT_INTO.search(literal):
                found += 1
    assert found > 0, "no protected INSERT found anywhere — the upsert scan is vacuous"


def test_protected_tables_all_exist_in_the_schema():
    """A typo in PROTECTED_TABLES would silently disarm this whole file."""
    schema = (REPO_ROOT / "db" / "schema.sql").read_text()
    for table in PROTECTED_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in schema


def test_protected_tables_match_the_four_hypertables_plus_nothing_else():
    from db.init import EXPECTED_HYPERTABLES

    assert set(PROTECTED_TABLES) == set(EXPECTED_HYPERTABLES)


def test_scanned_packages_all_exist():
    """Likewise a renamed package would quietly stop being scanned."""
    missing = [p for p in SCANNED_PACKAGES if not (REPO_ROOT / p).is_dir()]
    assert missing == [], f"SCANNED_PACKAGES lists directories that do not exist: {missing}"
