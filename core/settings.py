"""Environment settings — the ONLY place `os.environ` is read.

Secrets and host-specific paths come from `.env` (see `.env.example`). Weights,
thresholds, and risk numbers do NOT live here: those are `config/*.yaml`, loaded
by `core.config` (D25).

Deliberately dependency-light and eager: a missing required variable raises at
point of use with the variable's NAME, not three layers into an API call at
4:31 PM on a cron run nobody is watching.

Scope note: this module currently carries the application and database
accessors, because `db/init.py` needs them and nothing may read `os.environ`
directly. The provider accessors (FMP, Finnhub, Schwab, SMTP, backups) are
appended when those clients land.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent


class SettingsError(RuntimeError):
    """A required environment variable is missing or unusable."""


@lru_cache(maxsize=1)
def _load_dotenv_once() -> None:
    """Load `.env` from the repo root. Real environment variables win."""
    load_dotenv(REPO_ROOT / ".env", override=False)


def get(name: str, default: str | None = None) -> str | None:
    """Return an environment variable, or `default`. Blank counts as unset."""
    _load_dotenv_once()
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def require(name: str, *, hint: str = "") -> str:
    """Return an environment variable or raise. For anything a job cannot run without.

    Note: the `CHANGE_ME` check catches an un-filled template. It is NOT a
    safety mechanism for live calls — possessing a working credential says
    nothing about whether a call was intended, which is what `core.netguard`
    exists for (D27).
    """
    value = get(name)
    if value is None:
        suffix = f" — {hint}" if hint else ""
        raise SettingsError(
            f"Required environment variable {name} is not set{suffix}. "
            f"Copy .env.example to .env and fill it in."
        )
    if value.startswith("CHANGE_ME"):
        raise SettingsError(
            f"Environment variable {name} is still the .env.example placeholder "
            f"({value!r}). Set a real value."
        )
    return value


def get_int(name: str, default: int) -> int:
    raw = get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SettingsError(f"{name}={raw!r} is not an integer.") from exc


def get_path(name: str, default: str) -> Path:
    """Return a filesystem path, resolved against the repo root if relative."""
    raw = get(name, default) or default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (REPO_ROOT / path).resolve()


# ── Application ─────────────────────────────────────────────────────────────


def engine_env() -> str:
    return get("ENGINE_ENV", "dev") or "dev"


def log_level() -> str:
    return (get("LOG_LEVEL", "INFO") or "INFO").upper()


def data_dir() -> Path:
    path = get_path("DATA_DIR", "./data")
    path.mkdir(parents=True, exist_ok=True)
    return path


# ── Database (D20) ──────────────────────────────────────────────────────────


def database_url() -> str:
    """DSN for the engine database. DATABASE_URL wins; otherwise built from PG*."""
    explicit = get("DATABASE_URL")
    if explicit:
        return explicit
    return _build_dsn(
        user=require("PGUSER"),
        password=require("PGPASSWORD"),
        database=require("PGDATABASE"),
    )


def admin_database_url() -> str:
    """DSN for the maintenance database, used ONLY to run CREATE DATABASE.

    Separate credentials on purpose: the engine role does not need, and should
    not have, the privilege to create databases.
    """
    return _build_dsn(
        user=require("PGADMIN_USER"),
        password=require("PGADMIN_PASSWORD"),
        database=get("PGADMIN_DATABASE", "postgres") or "postgres",
    )


def database_name() -> str:
    return require("PGDATABASE")


def _build_dsn(*, user: str, password: str, database: str) -> str:
    host = get("PGHOST", "localhost")
    port = get_int("PGPORT", 5432)
    return f"postgresql://{quote(user)}:{quote(password)}@{host}:{port}/{quote(database)}"
