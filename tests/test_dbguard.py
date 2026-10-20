"""The destructive-test guard. Runs in the DEFAULT suite, DB-free.

Deliberately not marked `integration`: the point is that the refusal is proven
on every `pytest` run, including the ones that never touch a database. A guard
whose own test only runs when the thing it guards runs is not a guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.dbguard import (
    OVERRIDE_FLAG,
    UnsafeDatabase,
    assert_safe_database,
    database_name_from_dsn,
    looks_like_scratch_database,
    override_granted,
)

# A name that is neither scratch-looking nor protected: the case the override
# exists for. `investment_engine` used to stand here, but it is now a PROTECTED
# name that no override unlocks, so it cannot also serve as the unlockable
# example — see the protected-class tests at the end of this file.
UNRECOGNISED = "postgresql://engine:pw@localhost:5432/engine_main"
PROTECTED = "postgresql://engine:pw@localhost:5432/investment_engine"
SCRATCH = "postgresql://engine:pw@localhost:5432/ie_test"


# ── the refusal fires ───────────────────────────────────────────────────────


def test_an_unrecognised_database_is_refused():
    """The case this exists for: PGDATABASE pointing somewhere unvetted."""
    with pytest.raises(UnsafeDatabase) as excinfo:
        assert_safe_database(UNRECOGNISED, env={})
    message = str(excinfo.value)
    assert "engine_main" in message  # the refusal names the database it refused
    assert "TRUNCATE" in message
    assert OVERRIDE_FLAG in message  # the message names the way out


def test_the_refusal_explains_both_escape_routes():
    with pytest.raises(UnsafeDatabase) as excinfo:
        assert_safe_database(UNRECOGNISED, env={})
    message = str(excinfo.value)
    assert "scratch database" in message
    assert "never in .env" in message


def test_a_dsn_with_no_database_name_is_refused():
    """Ambiguity is not permission."""
    with pytest.raises(UnsafeDatabase):
        assert_safe_database("postgresql://engine:pw@localhost:5432", env={})


# ── scratch names pass ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "ie_test",
        "investment_engine_test",
        "test_engine",
        "engine_dev",
        "dev",
        "engine_ci",
        "scratch_engine",
        "engine_tmp",
        "sandbox_db",
        "IE_TEST",  # case-insensitive
    ],
)
def test_scratch_names_are_allowed(name):
    assert looks_like_scratch_database(name) is True
    assert assert_safe_database(f"postgresql://u:p@h:5432/{name}", env={}) == name


@pytest.mark.parametrize(
    "name",
    [
        "investment_engine",
        "engine",
        "prod",
        "postgres",
        "",
        # Substring matches must NOT count — this is why the pattern is anchored
        # on delimiters. "contest" contains "test"; it is not a test database.
        "contest",
        "latest_engine",
        "development",  # 'dev' is not a delimited word here
    ],
)
def test_non_scratch_names_are_rejected(name):
    assert looks_like_scratch_database(name) is False


# ── the override ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " 1 "])
def test_truthy_override_grants_access(value):
    assert override_granted({OVERRIDE_FLAG: value}) is True
    assert assert_safe_database(UNRECOGNISED, env={OVERRIDE_FLAG: value}) == "engine_main"


@pytest.mark.parametrize("value", ["0", "", "false", "no", "off", "maybe"])
def test_everything_else_withholds_it(value):
    assert override_granted({OVERRIDE_FLAG: value}) is False
    with pytest.raises(UnsafeDatabase):
        assert_safe_database(UNRECOGNISED, env={OVERRIDE_FLAG: value})


def test_an_absent_override_withholds_access():
    assert override_granted({}) is False


def test_a_working_dsn_is_not_consent():
    """The D27 lesson, applied to the database: capability is not permission."""
    env = {"DATABASE_URL": UNRECOGNISED, "PGPASSWORD": "a-real-password"}
    with pytest.raises(UnsafeDatabase):
        assert_safe_database(UNRECOGNISED, env=env)


def test_the_override_is_not_readable_from_dotenv():
    """It must come from os.environ, bypassing core.settings and `.env`.

    Reading it through settings.get() would make a line in `.env` a permanent
    grant, which is exactly what D27 rejected for network calls. Asserted on the
    IMPORT GRAPH rather than on the source text, because the module's docstring
    legitimately discusses both `.env` and core.settings.
    """
    import ast

    source = (Path(__file__).parent / "dbguard.py").read_text()
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "os" in imported, "the override must be read from os.environ"
    assert "core" not in imported, "importing core.settings would pull in .env"
    assert "dotenv" not in imported


# ── dsn parsing ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        (SCRATCH, "ie_test"),
        ("postgresql://u:p@h:5432/ie_test?sslmode=require", "ie_test"),
        ("postgresql://h/ie_test", "ie_test"),
        ("postgresql://u:p@h:5432/", ""),
        ("postgresql://u:p@h:5432", ""),
    ],
)
def test_database_name_is_parsed_out_of_the_dsn(dsn, expected):
    assert database_name_from_dsn(dsn) == expected


# ── the integration suite is actually wired to it ───────────────────────────


def test_the_destructive_suite_calls_the_guard():
    """If this import or call is ever removed, the suite is unguarded again."""
    source = (__import__("pathlib").Path(__file__).parent / "test_schema_db.py").read_text()
    assert "assert_safe_database" in source
    assert "autouse=True" in source
    # The guard must run before anything connects.
    assert source.index("assert_safe_database") < source.index("psycopg.connect")


# ── the protected class cannot be unlocked ───────────────────────────────────
#
# The lesson of 2026-10-20: the live-data database was wiped mid-session by a
# suite run with INTEGRATION_DB_OK=1 typed from muscle memory. An override that
# gets set reflexively is not a protection.


@pytest.mark.parametrize(
    "name",
    ["investment_engine", "engine_prod", "prod_engine", "ie_live", "engine-production"],
)
def test_protected_names_are_refused_even_with_the_override(name):
    dsn = f"postgresql://u@localhost:5432/{name}"
    with pytest.raises(UnsafeDatabase, match="PROTECTED"):
        assert_safe_database(dsn, env={"INTEGRATION_DB_OK": "1"})


def test_the_refusal_names_the_scratch_database_to_use_instead():
    with pytest.raises(UnsafeDatabase, match="PGDATABASE=ie_test"):
        assert_safe_database(
            "postgresql://u@localhost:5432/investment_engine",
            env={"INTEGRATION_DB_OK": "1"},
        )


def test_the_scratch_database_still_passes_without_any_override():
    assert assert_safe_database("postgresql://u@localhost:5432/ie_test", env={}) == "ie_test"


def test_a_protected_name_beats_a_scratch_looking_suffix():
    """`investment_engine_test` is a scratch database; `investment_engine` is not."""
    assert (
        assert_safe_database("postgresql://u@localhost:5432/investment_engine_test", env={})
        == "investment_engine_test"
    )
    with pytest.raises(UnsafeDatabase, match="PROTECTED"):
        assert_safe_database("postgresql://u@localhost:5432/investment_engine", env={})
