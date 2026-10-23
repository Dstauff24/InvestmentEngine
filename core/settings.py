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
    if looks_like_placeholder(value):
        raise SettingsError(
            f"Environment variable {name} is still the .env.example placeholder "
            f"({value!r}). Set a real value."
        )
    return value


# THE CANONICAL PLACEHOLDER RULE, so every reader agrees on what "filled in"
# means: `require` here, `scripts/env_contract.py` at deploy time, and
# `alerts/notify.py` deciding whether a channel exists at all. An alert
# addressed to you@example.com reaches nobody, which makes it the same class of
# non-value as CHANGE_ME — and on 2026-10-22 the engine called email
# "configured" on exactly those shipped defaults.
PLACEHOLDERS = ("change_me", "replace_me", "you@example.com", "engine@example.com")


def looks_like_placeholder(value: str | None) -> bool:
    """Whether a value is one of `.env.example`'s own, i.e. nobody filled it in."""
    if not value:
        return False
    lowered = value.lower()
    return any(token in lowered for token in PLACEHOLDERS)


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


# ── Providers ───────────────────────────────────────────────────────────────
# Note: having a usable key here is NOT permission to use it. Every client asks
# core.netguard first (D27).


def fmp_api_key() -> str:
    return require("FMP_API_KEY", hint="https://site.financialmodelingprep.com/developer/docs")


def fmp_base_url() -> str:
    return (get("FMP_BASE_URL", "https://financialmodelingprep.com") or "").rstrip("/")


def fmp_rate_limit() -> int:
    """Requests per minute the shared limiter enforces exactly.

    The separate DAILY budget (D29 free-tier operation) is
    `config/universe.yaml`'s `fmp.daily_call_budget`, not an env var: it is a
    tuning decision, not a secret.

    The default is 600, the Premium value CLAUDE.md pins (the plan allows 750).
    It used to be 300, a free-tier leftover that `.env.example` itself
    documents as WRONG — the free plan's real constraint was ~250 calls/DAY, so
    300/min enforced nothing. That mattered the moment the variable became
    `[OPTIONAL]`: an unset variable now RUNS on this number instead of failing
    the deploy, and a default the file calls wrong is not a state the engine is
    designed to run in.
    """
    return get_int("FMP_RATE_LIMIT_PER_MIN", 600)


def finnhub_api_key() -> str:
    return require("FINNHUB_API_KEY", hint="https://finnhub.io/dashboard")


def finnhub_base_url() -> str:
    return (get("FINNHUB_BASE_URL", "https://finnhub.io/api/v1") or "").rstrip("/")


def finnhub_rate_limit() -> int:
    return get_int("FINNHUB_RATE_LIMIT_PER_MIN", 60)


def schwab_app_key() -> str:
    return require("SCHWAB_APP_KEY", hint="Schwab Developer Portal -> your app")


def schwab_app_secret() -> str:
    return require("SCHWAB_APP_SECRET", hint="Schwab Developer Portal -> your app")


def schwab_callback_url() -> str:
    return require("SCHWAB_CALLBACK_URL", hint="must match the portal registration exactly")


def schwab_token_path() -> Path:
    """Token file location (D21: minted on the laptop, synced to the VM)."""
    return get_path("SCHWAB_TOKEN_PATH", "data/schwab_token.json")


def schwab_rate_limit() -> int:
    # Schwab's individual-use ceiling is 120/min; CLAUDE.md says throttle to ~100.
    return get_int("SCHWAB_RATE_LIMIT_PER_MIN", 100)


# ── Email (D16) ─────────────────────────────────────────────────────────────


def smtp_host() -> str:
    return require("SMTP_HOST")


def smtp_port() -> int:
    return get_int("SMTP_PORT", 587)


def smtp_user() -> str:
    return require("SMTP_USER")


def smtp_password() -> str:
    return require("SMTP_PASSWORD")


def alert_email_from() -> str:
    return require("ALERT_EMAIL_FROM")


def alert_email_to() -> str:
    return require("ALERT_EMAIL_TO")


def alert_email_onfail() -> str:
    """Where job failures page. Falls back to the digest address."""
    return get("ALERT_EMAIL_ONFAIL") or alert_email_to()


# ── Shadow / paper account (D44) ────────────────────────────────────────────


def shadow_account_value() -> float:
    """Account value the shadow and paper books size against (D44).

    Not the live balance, and not optional. A default would silently make every
    R-multiple in the Shadow Book a function of a number nobody chose, and D11
    measures expectancy in R.
    """
    raw = require("SHADOW_ACCOUNT_VALUE")
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(f"SHADOW_ACCOUNT_VALUE must be a number, got {raw!r}") from None
    if value <= 0:
        raise SettingsError(
            f"SHADOW_ACCOUNT_VALUE must be positive, got {value}. Sizing divides "
            f"by it, and a zero account would make every position size infinite."
        )
    return value


# ── SEC EDGAR (D39: insider clusters) ───────────────────────────────────────
# The SEC BLOCKS anonymous traffic. Their guidance asks for a descriptive
# User-Agent carrying a contact address, and for no more than ~10 requests a
# second. Both are conditions of access, not suggestions: ignoring them gets
# the IP blocked, and the engine then loses a free data source for a while.


EDGAR_PLACEHOLDER_MARKERS = ("you@example.com", "CHANGE_ME", "example.com")


def sec_edgar_user_agent() -> str:
    """A descriptive UA with a real contact address, or a hard error.

    The placeholder from `.env.example` is REFUSED rather than sent. Sending
    `you@example.com` to the SEC is worse than sending nothing: it satisfies
    the format while giving them no way to reach us, which is precisely the
    anonymous traffic the rule exists to stop.
    """
    value = require("SEC_EDGAR_USER_AGENT").strip()
    if any(marker in value for marker in EDGAR_PLACEHOLDER_MARKERS):
        raise SettingsError(
            f"SEC_EDGAR_USER_AGENT is still the example value ({value!r}). The SEC "
            f"asks for a real contact address; put yours in .env. Sending the "
            f"placeholder satisfies the format while giving them no way to reach "
            f"us, which is the anonymous traffic the rule exists to stop."
        )
    if "@" not in value:
        raise SettingsError(
            f"SEC_EDGAR_USER_AGENT ({value!r}) has no contact address. Use the "
            f"form 'AppName/0.1 (you@yourdomain.com)'."
        )
    return value


def sec_edgar_rate_limit() -> int:
    """Requests per SECOND. The limiter uses a one-second window for this.

    Defaults to 5, half the SEC's ~10/s guidance. Deliberately expressed per
    second rather than per minute like the other providers: a 300/minute
    sliding window would happily allow all 300 inside one second, which breaks
    the only limit the SEC actually states. A one-second window makes a burst
    structurally impossible instead of merely unlikely.
    """
    return get_int("SEC_EDGAR_RATE_LIMIT_PER_SEC", 5)


# ── Telegram (D16, amended 2026-10-21) ──────────────────────────────────────
# The channel that reaches a phone. Email is the digest; Telegram is the page.


def telegram_bot_token() -> str:
    return require("TELEGRAM_BOT_TOKEN")


def telegram_chat_id() -> str:
    return require("TELEGRAM_CHAT_ID")


def telegram_configured() -> bool:
    """Both values present and non-empty.

    Checked rather than assumed so `alerts/notify.py` can fan out to whatever
    is configured instead of failing where Telegram was never set up — a dev
    box, or a VM before the token is written.
    """
    return bool(get("TELEGRAM_BOT_TOKEN") and get("TELEGRAM_CHAT_ID"))


# ── Backups (D20) ───────────────────────────────────────────────────────────
# scripts/backup_db.sh parses .env itself (it is bash and must not source the
# file); these exist for any Python that needs the same values.


def backup_dir() -> Path:
    path = get_path("BACKUP_DIR", "./data/backups")
    path.mkdir(parents=True, exist_ok=True)
    return path


def backup_retention_days() -> int:
    return get_int("BACKUP_RETENTION_DAYS", 30)


def backup_s3_bucket() -> str | None:
    return get("BACKUP_S3_BUCKET")
