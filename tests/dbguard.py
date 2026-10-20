"""Refuse to run destructive integration tests against a non-test database.

`tests/test_schema_db.py` truncates the tables it exercises. That is correct
against a scratch database and catastrophic against a populated one — and under
D7 the snapshot history it would delete cannot be re-fetched from any vendor at
any price.

Same philosophy as the D27 network gate: the ability to do the thing is not
permission to do it. A working `DATABASE_URL` says nothing about whether the
operator meant to point a wiping test suite at it.

Two ways to pass, and no third:

  * the connected database NAME looks like a scratch database
    (`ie_test`, `engine_dev`, `…_ci`, `scratch_…`), or
  * `INTEGRATION_DB_OK=1` is set on the invocation.

And one way that CANNOT be unlocked: a name that looks like the live collection
database (`investment_engine`, or anything containing `prod`/`production`/`live`
as a word) is refused even with the override set. That is not belt-and-braces;
it is the lesson of 2026-10-20, when the live-data database was wiped mid-session
by a suite run with `INTEGRATION_DB_OK=1` typed from muscle memory. An override
that can be set reflexively is not a protection, so the class of database that
must never be wiped by this suite is named rather than merely unmatched. There
is no legitimate reason for the integration suite to truncate the collection
database; if you genuinely need to reset it, do it by hand, where the decision
is visible.

The override is read from `os.environ` DIRECTLY, deliberately bypassing
`core.settings` and therefore `.env`. A line in `.env` is a standing grant
nobody remembers giving; this has to be a decision made at the moment of the
run:

    INTEGRATION_DB_OK=1 pytest -m integration
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

OVERRIDE_FLAG = "INTEGRATION_DB_OK"
TRUTHY = frozenset({"1", "true", "yes", "on"})

# A scratch-looking name: `test`/`dev`/`ci`/`scratch`/`tmp`/`sandbox` appearing
# as a whole word, delimited by a boundary or an underscore/hyphen. Anchored on
# delimiters so `investment_engine` does not match on a substring.
SCRATCH_NAME = re.compile(r"(?:^|[_-])(test|dev|ci|scratch|tmp|sandbox)(?:[_-]|$)", re.IGNORECASE)

# Names the suite must never truncate, override or not. `investment_engine` is
# the live collection database (D7 history, unbuyable); `prod`/`production`/
# `live` cover a deployment named by convention rather than by this project's.
PROTECTED_NAME = re.compile(
    r"^investment_engine$|(?:^|[_-])(prod|production|live)(?:[_-]|$)", re.IGNORECASE
)


class UnsafeDatabase(RuntimeError):
    """The integration suite would have wiped a database it was not told to."""


def database_name_from_dsn(dsn: str) -> str:
    """The database name out of a libpq URI. Empty string if it has none."""
    path = urlsplit(dsn).path
    return path.lstrip("/") if path else ""


def looks_like_scratch_database(name: str) -> bool:
    return bool(name) and bool(SCRATCH_NAME.search(name))


def is_protected_database(name: str) -> bool:
    """A database the integration suite may not wipe under any circumstances."""
    return bool(name) and bool(PROTECTED_NAME.search(name))


def override_granted(env: Mapping[str, str] | None = None) -> bool:
    """Is the explicit override set? Reads os.environ, never `.env`."""
    source = os.environ if env is None else env
    return source.get(OVERRIDE_FLAG, "").strip().lower() in TRUTHY


def assert_safe_database(dsn: str, env: Mapping[str, str] | None = None) -> str:
    """Raise unless this DSN is safe to wipe. Returns the database name."""
    name = database_name_from_dsn(dsn)

    # Checked BEFORE the override, which is the whole point: this one cannot be
    # unlocked by a flag typed out of habit.
    if is_protected_database(name):
        raise UnsafeDatabase(
            f"refusing to run destructive integration tests against database "
            f"{name!r}: this is a PROTECTED name — the live collection database.\n"
            f"{OVERRIDE_FLAG} does not apply here and will not unlock it.\n"
            f"Run the suite against the scratch database instead:\n"
            f"    PGDATABASE=ie_test pytest -m integration\n"
            f"Under D7 the snapshot history here cannot be re-fetched at any price."
        )

    if looks_like_scratch_database(name) or override_granted(env):
        return name

    shown = name or "<none>"
    raise UnsafeDatabase(
        f"refusing to run destructive integration tests against database "
        f"{shown!r}: the name does not look like a scratch database and "
        f"{OVERRIDE_FLAG} is not set.\n"
        f"These tests TRUNCATE the tables they touch. Under D7 the snapshot "
        f"history cannot be re-fetched at any price.\n"
        f"Either point PGDATABASE at a scratch database (a name containing "
        f"test/dev/ci/scratch), or, if you really mean this one:\n"
        f"    {OVERRIDE_FLAG}=1 pytest -m integration\n"
        f"Set it on the invocation, never in .env."
    )
