"""Finnhub HTTP client (D5) — free tier only.

D5 is explicit: do NOT plan around paying for Finnhub (the paid jump is
~$3,500/mo). That constrains the design — 60 calls/min and a small endpoint
surface — so this client stays deliberately minimal and the rate limiter is not
optional.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from core import netguard, settings
from core.ratelimit import RateLimiter

log = logging.getLogger(__name__)

TIMEOUT_S = 30
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class FinnhubError(RuntimeError):
    pass


class FinnhubTransientError(FinnhubError):
    pass


class FinnhubAuthError(FinnhubError):
    """Bad key, or the free tier does not cover this endpoint. Retrying will not help."""


class FinnhubClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        limiter: RateLimiter | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = api_key or settings.finnhub_api_key()
        self.base_url = (base_url or settings.finnhub_base_url()).rstrip("/")
        self.limiter = limiter or RateLimiter(settings.finnhub_rate_limit(), 60.0, name="finnhub")
        self.session = session or requests.Session()

    @retry(
        retry=retry_if_exception_type(FinnhubTransientError),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        reraise=True,
    )
    def get(self, path: str, **params: Any) -> Any:
        # D27: ahead of the limiter and the session.
        netguard.require_live_calls("Finnhub", path)
        self.limiter.acquire()

        url = f"{self.base_url}/{path.lstrip('/')}"
        query = {k: v for k, v in params.items() if v is not None}
        query["token"] = self.api_key

        try:
            response = self.session.get(url, params=query, timeout=TIMEOUT_S)
        except requests.Timeout as exc:
            raise FinnhubTransientError(f"timeout calling {path}") from exc
        except requests.RequestException as exc:
            raise FinnhubTransientError(f"network error calling {path}: {exc}") from exc

        if response.status_code in (401, 403):
            raise FinnhubAuthError(
                f"Finnhub rejected the request on {path} (HTTP {response.status_code}). "
                f"Check FINNHUB_API_KEY; note D5 restricts us to free-tier endpoints."
            )
        if response.status_code in RETRYABLE_STATUS:
            raise FinnhubTransientError(f"HTTP {response.status_code} from {path}")
        if not response.ok:
            raise FinnhubError(f"HTTP {response.status_code} from {path}: {response.text[:200]}")

        try:
            return response.json()
        except ValueError as exc:
            raise FinnhubError(f"{path} returned non-JSON: {response.text[:200]}") from exc

    def company_news(self, symbol: str, start: date, end: date) -> list[dict]:
        """Company news over an inclusive date window. Free tier; ~1y of history."""
        payload = self.get("/company-news", symbol=symbol, **{"from": str(start), "to": str(end)})
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]
