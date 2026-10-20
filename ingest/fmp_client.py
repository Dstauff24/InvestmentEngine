"""Financial Modeling Prep HTTP client (D4). Targets the **`/stable/`** API.

Explicit `requests` calls rather than a provider SDK: the surface we need is a
handful of endpoints, and when FMP changes a field name the fix should be one
obvious line rather than a dependency bump.

FMP runs two API generations and this account is on `/stable/`, where the symbol
travels in the QUERY STRING (`/stable/quote?symbol=AAPL`) rather than the path
(`/api/v3/quote/AAPL`), and several endpoints are renamed
(`stock_dividend_calendar` -> `dividends-calendar`, `historical-price-full` ->
`historical-price-eod/full` with a date window instead of `timeseries=N`).

**402 is distinguished from 404**, because they mean different things and have
different fixes: 404 is the wrong path (a code bug), 402 is the right path on a
plan that does not include it (a billing decision). D29 records five endpoints
that 402 on the free tier: `sp500-constituent`, `nasdaq-constituent`,
`analyst-estimates`, `ratios`, `key-metrics`. They are implemented and left in
place; `scripts/fmp_probe.py` re-checks after any plan change.

Response shapes differ between generations too — `/stable/` returns bare arrays
where `/api/v3/` wrapped some payloads in `{"historical": [...]}`. `_rows`
accepts either, so a migration in either direction does not break the parsers.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core import clock, netguard, settings
from core.budget import BudgetExhausted, DayBudget
from core.ratelimit import RateLimiter

log = logging.getLogger(__name__)

TIMEOUT_S = 30
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

# How FMP says "your allowance for today is gone". It arrives TWO ways, neither
# of which looks like a refusal to a naive client: HTTP 200 with an error body,
# or a 429 whose text is about the plan rather than about pacing. A 429 that
# says neither is an ordinary per-minute throttle and stays retryable.
ALLOWANCE_SPENT_PHRASES = ("limit reach", "limit reached", "upgrade your plan")


def is_allowance_spent(text: str) -> bool:
    """Does this response body say the provider's daily allowance is spent?"""
    low = text.lower()
    return any(phrase in low for phrase in ALLOWANCE_SPENT_PHRASES)


# One symbol's fundamentals costs this many calls (income, balance, cash flow,
# ratios, key metrics). The D29 daily budget is measured in these units.
CALLS_PER_SYMBOL_FUNDAMENTALS = 5


class FMPError(RuntimeError):
    """FMP returned something the caller cannot use."""


class FMPTransientError(FMPError):
    """Worth retrying: a timeout, a 429, or a 5xx."""


class FMPAuthError(FMPError):
    """Bad or exhausted API key. Retrying will not help."""


# Re-exported so call sites can catch budget exhaustion without importing
# core.budget directly. It is deliberately NOT an FMPError subclass: it is not a
# provider problem, and the retry decorator must not treat it as transient.
FMPBudgetExhausted = BudgetExhausted


class FMPPlanError(FMPAuthError):
    """HTTP 402: the endpoint exists but the plan does not cover it.

    A subclass of FMPAuthError so the retry decorator ignores it, but distinct
    because the fix is different — nothing is wrong with the key or the code,
    and no amount of retrying changes it (D29).
    """


class FMPClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        limiter: RateLimiter | None = None,
        budget: DayBudget | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = api_key or settings.fmp_api_key()
        self.base_url = (base_url or settings.fmp_base_url()).rstrip("/")
        self.limiter = limiter or RateLimiter(settings.fmp_rate_limit(), 60.0, name="fmp")
        # D29: the day-level budget is drawn HERE so every call site is covered
        # automatically — the probe, universe enrichment, fundamentals,
        # estimates, events. Nothing has to remember to opt in.
        self.budget = budget if budget is not None else DayBudget.for_fmp()
        self.session = session or requests.Session()

    # ── transport ───────────────────────────────────────────────────────────

    @retry(
        retry=retry_if_exception_type(FMPTransientError),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        reraise=True,
    )
    def get(self, path: str, **params: Any) -> Any:
        """GET an FMP endpoint. `path` is everything after the base URL."""
        # Order matters. D27 consent first: a blocked call must consume no
        # budget, no rate-limit slot and no socket. Then the day budget, because
        # a call that fails still spent quota at the provider, so it has to be
        # reserved BEFORE the request rather than counted after.
        netguard.require_live_calls("FMP", path)
        self.budget.spend_or_raise(1)
        self.limiter.acquire()

        url = f"{self.base_url}/{path.lstrip('/')}"
        query = {k: v for k, v in params.items() if v is not None}
        query["apikey"] = self.api_key

        try:
            response = self.session.get(url, params=query, timeout=TIMEOUT_S)
        except requests.Timeout as exc:
            raise FMPTransientError(f"timeout calling {path}") from exc
        except requests.RequestException as exc:
            raise FMPTransientError(f"network error calling {path}: {exc}") from exc

        if response.status_code == 402:
            raise FMPPlanError(
                f"FMP returned 402 Payment Required for {path}. The path is correct but "
                f"the current plan does not include this endpoint — upgrade the plan, or "
                f"re-run scripts/fmp_probe.py to confirm what is covered (D29)."
            )
        if response.status_code in (401, 403):
            raise FMPAuthError(
                f"FMP rejected the API key on {path} (HTTP {response.status_code}). "
                f"Check FMP_API_KEY."
            )
        if response.status_code == 429 and is_allowance_spent(response.text):
            # NOT retryable, and not a pacing problem: backing off and trying
            # again cannot conjure more allowance. Record the provider's verdict
            # so no later job today spends a round-trip rediscovering it.
            self.budget.mark_provider_exhausted(
                f"HTTP 429 on {path}: {response.text.strip()[:200]}"
            )
            raise FMPAuthError(
                f"FMP reports its daily allowance spent (HTTP 429 on {path}). "
                f"Marked for the rest of the session date; later jobs will stop "
                f"at the budget gate without calling out."
            )
        if response.status_code in RETRYABLE_STATUS:
            raise FMPTransientError(f"HTTP {response.status_code} from {path}")
        if not response.ok:
            raise FMPError(f"HTTP {response.status_code} from {path}: {response.text[:200]}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise FMPError(f"{path} returned non-JSON: {response.text[:200]}") from exc

        # FMP signals plan/quota problems with a 200 and an error message body.
        if isinstance(payload, dict) and "Error Message" in payload:
            message = payload["Error Message"]
            if is_allowance_spent(message):
                # THE expensive one: HTTP 200, so it reads as a successful
                # request returning an unhelpful body. On 2026-10-20 this was
                # mistaken for a per-symbol data gap 225 times in one run.
                self.budget.mark_provider_exhausted(f"HTTP 200 on {path}: {message}")
                raise FMPAuthError(
                    f"FMP reports its daily allowance spent on {path}: {message}. "
                    f"Marked for the rest of the session date; later jobs will stop "
                    f"at the budget gate without calling out."
                )
            if "limit" in message.lower() or "upgrade" in message.lower():
                raise FMPAuthError(f"FMP plan/quota error on {path}: {message}")
            raise FMPError(f"FMP error on {path}: {message}")
        return payload

    # ── index constituents (D14) — both 402 on the free tier (D29) ──────────

    def sp500_constituents(self) -> list[dict]:
        """S&P 500 members. Rows carry symbol, name, sector, subSector."""
        return _expect_list(self.get("/stable/sp500-constituent"), "sp500-constituent")

    def nasdaq100_constituents(self) -> list[dict]:
        return _expect_list(self.get("/stable/nasdaq-constituent"), "nasdaq-constituent")

    # ── quotes ──────────────────────────────────────────────────────────────

    def quotes(self, symbols: list[str]) -> list[dict]:
        """Batch quote. Carries price, volume, avgVolume, marketCap, exchange."""
        if not symbols:
            return []
        return _expect_list(self.get("/stable/quote", symbol=",".join(symbols)), "quote")

    # ── fundamentals (D4) — one call per statement per symbol ───────────────

    def income_statement(self, symbol: str, *, period: str, limit: int) -> list[dict]:
        return _rows(
            self.get("/stable/income-statement", symbol=symbol, period=period, limit=limit)
        )

    def balance_sheet(self, symbol: str, *, period: str, limit: int) -> list[dict]:
        return _rows(
            self.get("/stable/balance-sheet-statement", symbol=symbol, period=period, limit=limit)
        )

    def cash_flow(self, symbol: str, *, period: str, limit: int) -> list[dict]:
        return _rows(
            self.get("/stable/cash-flow-statement", symbol=symbol, period=period, limit=limit)
        )

    def ratios(self, symbol: str, *, period: str, limit: int) -> list[dict]:
        """402 on the free tier (D29). Callers treat it as optional enrichment."""
        return _rows(self.get("/stable/ratios", symbol=symbol, period=period, limit=limit))

    def key_metrics(self, symbol: str, *, period: str, limit: int) -> list[dict]:
        """402 on the free tier (D29). Callers treat it as optional enrichment."""
        return _rows(self.get("/stable/key-metrics", symbol=symbol, period=period, limit=limit))

    # ── estimates (D7) — 402 on the free tier (D29) ─────────────────────────

    def analyst_estimates(self, symbol: str, *, period: str, limit: int) -> list[dict]:
        return _rows(
            self.get("/stable/analyst-estimates", symbol=symbol, period=period, limit=limit)
        )

    # ── calendars — market-wide, so 3 calls cover the whole universe ────────

    def earnings_calendar(self, start, end) -> list[dict]:
        return _rows(self.get("/stable/earnings-calendar", **{"from": str(start), "to": str(end)}))

    def dividend_calendar(self, start, end) -> list[dict]:
        return _rows(self.get("/stable/dividends-calendar", **{"from": str(start), "to": str(end)}))

    def split_calendar(self, start, end) -> list[dict]:
        return _rows(self.get("/stable/splits-calendar", **{"from": str(start), "to": str(end)}))

    # ── full history, for the D24 corporate-actions backfill ───────────────

    def historical_dividends(self, symbol: str) -> list[dict]:
        rows = _rows(self.get("/stable/dividends", symbol=symbol))
        return [{**row, "symbol": row.get("symbol") or symbol} for row in rows]

    def historical_splits(self, symbol: str) -> list[dict]:
        rows = _rows(self.get("/stable/splits", symbol=symbol))
        return [{**row, "symbol": row.get("symbol") or symbol} for row in rows]

    # ── daily bars, for the D14 liquidity filter ────────────────────────────

    def daily_bars(self, symbol: str, *, days: int) -> list[dict]:
        """The most recent daily bars for one symbol, newest first.

        `/stable/` takes a date window rather than `/api/v3/`'s `timeseries`
        count, so ask generously in calendar days and let the caller slice:
        weekends and holidays mean roughly 1.5 calendar days per session.
        """
        end = clock.session_date()
        start = end - timedelta(days=int(days * 1.6) + 10)
        return _rows(
            self.get(
                "/stable/historical-price-eod/full",
                symbol=symbol,
                **{"from": str(start), "to": str(end)},
            )
        )


def _rows(payload: Any) -> list[dict]:
    """Tolerant row extraction for per-symbol endpoints.

    Handles both API generations: `/stable/` returns bare arrays, `/api/v3/`
    wrapped some payloads in `{"historical": [...]}`. Also returns `[]` rather
    than raising for a symbol FMP has no data on — one thin ticker is not worth
    failing a 500-symbol run over. Index endpoints use `_expect_list` instead,
    where a wrong shape genuinely does mean a broken pull.
    """
    if isinstance(payload, dict):
        for key in ("historical", "data", "results"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            return [payload]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def _expect_list(payload: Any, endpoint: str) -> list[dict]:
    """Strict coercion: a wrong shape here means an empty universe, so raise."""
    if not isinstance(payload, list):
        raise FMPError(f"{endpoint} returned {type(payload).__name__}, expected a list")
    return payload
