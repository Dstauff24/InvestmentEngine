"""Schwab Trader API client wrapper (D3), built on `schwab-py` 1.5.

Two things this adds over using `schwab-py` directly:

  * the shared rate limiter (CLAUDE.md: 120 req/min individual-use, throttled to
    ~100), which `schwab-py` does not do for us; and
  * a loud, specific error when the token file is missing or stale, because that
    is the ROUTINE failure mode: the refresh token lives 7 days and is minted on
    a laptop and synced (D21/O8), so it WILL go stale on a schedule.

Auth uses `client_from_token_file`, never `easy_client`: easy_client falls back
to an interactive browser login, which on a headless cron box hangs the job
until the callback timeout instead of failing cleanly.

No day-level budget here. Schwab's limit is per-minute, not per-day (D3), so the
rate limiter is the whole constraint — unlike FMP's free tier (D29).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from core import netguard, settings
from core.ratelimit import RateLimiter

log = logging.getLogger(__name__)

# Schwab refresh tokens last 7 days. schwab-py's own easy_client default is
# 561600s = 6.5 days, which leaves half a day of margin.
REFRESH_TOKEN_LIFETIME = timedelta(days=7)
TOKEN_WARN_BEFORE = timedelta(hours=24)

# Statuses worth another attempt. 429 is the one that matters: Schwab's limit is
# ~120/min and the limiter is set to 100, yet a long chain crawl still draws the
# occasional 429 — observed 2026-10-21, two symbols out of 433 on the third
# heavy run of the hour, and a DIFFERENT two than the run before.
#
# Without a retry those two become a permanent hole in an unbuyable series: the
# chain and IV jobs are critical and SACRED (D7/D22), `partial` correctly fails
# the day, and the engine would have been defeated by a rate limiter it was
# already respecting. 401 is deliberately absent — a token problem is not
# transient and must surface immediately for a human to re-auth.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class SchwabError(RuntimeError):
    pass


class SchwabTokenError(SchwabError):
    """The token file is missing, unreadable, or past its refresh lifetime.

    Not retryable and not fixable on the VM: a human has to re-run the login
    flow on a machine with a browser (scripts/schwab_auth.py) and sync the file.
    """


class SchwabTransientError(SchwabError):
    """A rate limit or a server-side blip. Retried with backoff."""


class SchwabResponseError(SchwabError):
    """Schwab answered, but not with something usable."""


def token_path() -> Path:
    return settings.schwab_token_path()


def token_age(path: Path | None = None) -> timedelta | None:
    """How long ago the token file was last written, or None if absent."""
    path = path or token_path()
    if not path.exists():
        return None
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    return datetime.now(UTC) - modified


def token_expires_at(path: Path | None = None) -> datetime | None:
    path = path or token_path()
    if not path.exists():
        return None
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    return modified + REFRESH_TOKEN_LIFETIME


def check_token(path: Path | None = None) -> None:
    """Raise if the token cannot plausibly still work. Called before every job."""
    path = path or token_path()
    if not path.exists():
        raise SchwabTokenError(
            f"no Schwab token at {path}. Run `python scripts/schwab_auth.py` on a "
            f"machine with a browser, then sync it here (scripts/sync_token.sh)."
        )
    age = token_age(path)
    if age is not None and age > REFRESH_TOKEN_LIFETIME:
        raise SchwabTokenError(
            f"Schwab token at {path} is {age.days}d old, past the "
            f"{REFRESH_TOKEN_LIFETIME.days}d refresh lifetime. Re-auth on the laptop "
            f"and re-sync (O8)."
        )


def build_client(*, token_file: Path | None = None):
    """Return an authenticated `schwab-py` Client, or raise SchwabTokenError."""
    from schwab.auth import client_from_token_file  # heavy import, kept local

    path = token_file or token_path()
    check_token(path)
    try:
        return client_from_token_file(
            token_path=str(path),
            api_key=settings.schwab_app_key(),
            app_secret=settings.schwab_app_secret(),
        )
    except Exception as exc:  # schwab-py raises a variety of things here
        raise SchwabTokenError(
            f"could not build a Schwab client from {path}: {type(exc).__name__}: {exc}. "
            f"If the token was revoked or the app credentials changed, re-run "
            f"scripts/schwab_auth.py."
        ) from exc


class SchwabClient:
    """Rate-limited wrapper. Holds a `schwab-py` Client and counts every call."""

    def __init__(self, client=None, *, limiter: RateLimiter | None = None) -> None:
        self._client = client
        self.limiter = limiter or RateLimiter(settings.schwab_rate_limit(), 60.0, name="schwab")

    @property
    def raw(self):
        if self._client is None:
            self._client = build_client()
        return self._client

    @retry(
        retry=retry_if_exception_type(SchwabTransientError),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        reraise=True,
    )
    def _call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke a schwab-py method, rate-limited, and return the parsed JSON.

        The retry wraps the WHOLE method, so each attempt re-acquires a limiter
        slot — every attempt is a real request and must be paced like one.
        """
        # D27: ahead of the limiter and the transport.
        netguard.require_live_calls("Schwab", method_name)
        self.limiter.acquire()

        response = getattr(self.raw, method_name)(*args, **kwargs)
        status = getattr(response, "status_code", None)
        if status == 401:
            raise SchwabTokenError(
                "Schwab returned 401. The access token could not be refreshed — "
                "re-auth on the laptop (scripts/schwab_auth.py) and re-sync."
            )
        if status in RETRYABLE_STATUS:
            body = getattr(response, "text", "")[:200]
            raise SchwabTransientError(f"{method_name} -> HTTP {status} (retryable): {body}")
        if status is not None and status >= 400:
            body = getattr(response, "text", "")[:200]
            raise SchwabResponseError(f"{method_name} -> HTTP {status}: {body}")
        try:
            return response.json()
        except Exception as exc:
            raise SchwabResponseError(f"{method_name} returned non-JSON") from exc

    # ── endpoints ───────────────────────────────────────────────────────────

    def price_history_daily(self, symbol: str, *, start: datetime, end: datetime) -> dict:
        """Daily OHLCV candles (D15: daily bars only in Phase 1)."""
        return self._call(
            "get_price_history_every_day",
            to_schwab_symbol(symbol),
            start_datetime=start,
            end_datetime=end,
            need_extended_hours_data=False,
            need_previous_close=False,
        )

    def option_chain(self, symbol: str, *, from_date, to_date) -> dict:
        """Full option chain across an expiration window.

        The D22 strike filter is applied client-side in schwab_chains.py rather
        than via the API's `strike_count`, because `strike_count` is a COUNT of
        strikes and D22 is a PERCENTAGE band around spot — the two only coincide
        for a stock whose strike increments happen to line up.
        """
        return self._call(
            "get_option_chain",
            to_schwab_symbol(symbol),
            from_date=from_date,
            to_date=to_date,
            include_underlying_quote=True,
        )

    def quotes(self, symbols: list[str]) -> dict:
        return self._call("get_quotes", [to_schwab_symbol(s) for s in symbols])


def to_schwab_symbol(symbol: str) -> str:
    """Canonical form -> Schwab's slash form. BRK-B -> BRK/B.

    THE CANONICAL FORM IS THE DASH FORM. Verified live 2026-10-21: FMP
    `/stable/` constituents return `BRK-B` and `BF-B`, every `/stable/`
    endpoint keys on that spelling, and the DOT form is actively rejected —
    `/stable/ratios?symbol=BRK.B` answers 402 while `?symbol=AAPL` answers 200.

    This function previously translated only `.` -> `/`, on the stated belief
    that the database held the dot form. It does not, and never did under
    `/stable/`: `normalize_symbol` only uppercases and strips spaces, so the
    dash form passes through untouched. The result was that `BRK-B` reached
    Schwab verbatim, Schwab returned no chain, and the only two share-class
    names in the S&P 500 silently lost their option chains — which with the
    D14 options gate now armed would have dropped them from the universe for a
    punctuation mismatch.

    Both separators are translated, so a hand-maintained
    `constituents_static.txt` carrying dots still works.
    """
    return symbol.replace("-", "/").replace(".", "/")


def from_schwab_symbol(symbol: str) -> str:
    """Schwab's slash form -> canonical DASH form. BRK/B -> BRK-B.

    Must agree with `to_schwab_symbol` or a chain comes back keyed to a symbol
    that is not in `tickers`, and the foreign key rejects the write.
    """
    return symbol.replace("/", "-")
