"""SEC EDGAR client for Form 4 insider filings (D39: insider clusters).

SEC ETIQUETTE IS A HARD CONSTRAINT, not a courtesy. Access is conditional on
two things, and the engine honours both structurally rather than by intention:

  * **A descriptive User-Agent with a contact address.** `core/settings` refuses
    the `.env.example` placeholder, because sending `you@example.com` satisfies
    the format while giving the SEC no way to reach us — exactly the anonymous
    traffic the rule exists to stop.
  * **No more than ~10 requests a second.** The limiter here uses a ONE-SECOND
    window with 5 slots. A per-minute window would allow all its slots inside a
    single second, which breaks the only limit the SEC states; a one-second
    window makes a burst impossible rather than unlikely.

And the house rules apply to EDGAR like any other provider: the D27 consent
gate sits ahead of the limiter and the transport, and a day-level budget guards
against a runaway loop. The SEC publishes no daily cap, so that budget is not
about staying inside an allowance — it is about not being the reason they
introduce one.

Two base hosts, which is not a mistake: bulk JSON lives on `data.sec.gov`, and
filing documents live on `www.sec.gov/Archives`.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from core import netguard, settings
from core.budget import DayBudget
from core.ratelimit import RateLimiter

log = logging.getLogger(__name__)

DATA_HOST = "https://data.sec.gov"
ARCHIVE_HOST = "https://www.sec.gov"
TIMEOUT_S = 30

# One-second window: see the module docstring. This is the whole reason the
# limiter takes a configurable period.
RATE_WINDOW_S = 1.0

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class EdgarError(RuntimeError):
    """EDGAR said something we cannot use."""


class EdgarTransientError(EdgarError):
    """A rate limit or a server blip. Retried with backoff."""


class EdgarBlockedError(EdgarError):
    """The SEC refused us. Almost always the User-Agent or the request rate."""


class EdgarClient:
    """Minimal EDGAR reader: ticker->CIK map, submissions, and documents."""

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        limiter: RateLimiter | None = None,
        budget: DayBudget | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.user_agent = user_agent or settings.sec_edgar_user_agent()
        self.limiter = limiter or RateLimiter(
            settings.sec_edgar_rate_limit(), RATE_WINDOW_S, name="edgar"
        )
        self.budget = budget if budget is not None else DayBudget.for_edgar()
        self.session = session or requests.Session()

    # ── transport ───────────────────────────────────────────────────────────

    @retry(
        retry=retry_if_exception_type(EdgarTransientError),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        reraise=True,
    )
    def get(self, url: str, *, as_json: bool = True) -> Any:
        """Fetch one EDGAR URL. Consent, then budget, then limiter, then socket.

        The same order as the FMP client, for the same reason: a blocked call
        must consume no budget and no rate-limit slot, and a call that fails
        still cost the provider a request, so the budget is reserved BEFORE the
        request rather than counted after.
        """
        netguard.require_live_calls("SEC EDGAR", url)
        self.budget.spend_or_raise(1)
        self.limiter.acquire()

        headers = {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate",
        }
        try:
            response = self.session.get(url, headers=headers, timeout=TIMEOUT_S)
        except requests.Timeout as exc:
            raise EdgarTransientError(f"timeout fetching {url}") from exc
        except requests.RequestException as exc:
            raise EdgarTransientError(f"network error fetching {url}: {exc}") from exc

        if response.status_code == 403:
            # The SEC's refusal for missing or unacceptable identification. Not
            # retryable: hammering it is how a soft block becomes a long one.
            raise EdgarBlockedError(
                f"SEC EDGAR returned 403 for {url}. This is almost always the "
                f"User-Agent (it must carry a real contact address) or the request "
                f"rate. Do NOT retry in a loop — check SEC_EDGAR_USER_AGENT and "
                f"SEC_EDGAR_RATE_LIMIT_PER_SEC first."
            )
        if response.status_code == 404:
            raise EdgarError(f"not found: {url}")
        if response.status_code in RETRYABLE_STATUS:
            raise EdgarTransientError(f"HTTP {response.status_code} from {url}")
        if not response.ok:
            raise EdgarError(f"HTTP {response.status_code} from {url}: {response.text[:200]}")

        if not as_json:
            return response.text
        try:
            return response.json()
        except ValueError as exc:
            raise EdgarError(f"{url} returned non-JSON: {response.text[:200]}") from exc

    # ── endpoints ───────────────────────────────────────────────────────────

    def company_tickers(self) -> dict[str, str]:
        """{TICKER: zero-padded CIK} for every SEC filer with a ticker.

        One request for the whole market, so it is fetched once per run rather
        than per symbol — which is the difference between 1 request and 500.
        """
        payload = self.get(f"{ARCHIVE_HOST}/files/company_tickers.json")
        if not isinstance(payload, dict):
            raise EdgarError("company_tickers.json was not a JSON object")
        out: dict[str, str] = {}
        for entry in payload.values():
            ticker = str(entry.get("ticker", "")).strip().upper()
            cik = entry.get("cik_str")
            if ticker and cik is not None:
                out[ticker] = f"{int(cik):010d}"
        if not out:
            raise EdgarError("company_tickers.json produced no usable rows")
        return out

    def submissions(self, cik: str) -> dict:
        """A filer's recent filings, newest first, with acceptance timestamps."""
        return self.get(f"{DATA_HOST}/submissions/CIK{cik}.json")

    def submissions_page(self, name: str) -> dict:
        """One of the OLDER submission files a filer's feed points at.

        **`filings.recent` HOLDS ONLY THE LAST ~1,000 FILINGS.** Everything
        before that lives in separate documents named under `filings.files`,
        and a historical Form 4 walk that reads only `recent` stops wherever
        1,000 filings happens to land — for a large filer that can be two
        years, for a quiet one twenty. The omission is invisible: the feed
        parses, the rows look fine, and the history simply ends.
        """
        return self.get(f"{DATA_HOST}/submissions/{name}")

    def filing_document(self, cik: str, accession: str, document: str) -> str:
        """One document out of one filing, as text (Form 4 XML)."""
        bare_cik = str(int(cik))
        folder = accession.replace("-", "")
        return self.get(
            f"{ARCHIVE_HOST}/Archives/edgar/data/{bare_cik}/{folder}/{document}",
            as_json=False,
        )


def raw_xml_document(primary_document: str) -> str:
    """The machine-readable Form 4 behind EDGAR's rendered view.

    `primaryDocument` in a submissions feed points at the XSL-TRANSFORMED HTML,
    not the XML: `xslF345X06/form4.xml` renders as a styled page a person can
    read, and `form4.xml` in the same folder is the data. The HTML is served
    with an `.xml` extension, which is why this is easy to miss — the name says
    XML, the content is `<!DOCTYPE html>`, and an XML parser fails on it with a
    mismatched-tag error dozens of lines in rather than anything that points at
    the cause.

    Found live 2026-10-21: every Form 4 fetched for AAPL and MSFT failed to
    parse until the prefix was stripped.

    The transform directory is versioned (`xslF345X03`, `X05`, `X06`, ...), so
    this strips any leading `xsl*/` segment rather than matching one spelling.
    """
    if "/" not in primary_document:
        return primary_document
    head, _, tail = primary_document.partition("/")
    return tail if head.lower().startswith("xsl") else primary_document


def to_edgar_ticker(symbol: str) -> str:
    """Canonical dash form -> EDGAR's form. BRK-B stays BRK-B.

    EDGAR uses dashes for share classes, the same convention FMP `/stable/`
    uses, so this is an identity today. It exists as a named boundary anyway:
    the Schwab translator was wrong for months precisely because the conversion
    was assumed obvious and never given a place to live.
    """
    return symbol.strip().upper()
