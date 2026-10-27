#!/usr/bin/env python3
"""Compare LIVE provider field names against the ones our parsers read.

    python scripts/field_probe.py --offline        # fixtures only, no network
    ALLOW_LIVE_CALLS=1 python scripts/field_probe.py
    ALLOW_LIVE_CALLS=1 python scripts/field_probe.py --provider fmp

**WHY THIS EXISTS.** FMP renamed the earnings calendar's `eps` to `epsActual`
and dropped `time` altogether. Nothing failed. `parse_earnings` went on reading
`row.get("eps")`, stored NULL actuals from live data, and the test suite stayed
green — because the only thing it ever checked was a FIXTURE captured before
the rename. A fixture proves the parser handles a shape it was shown; it cannot
notice the provider no longer sends it.

So this probe checks the other direction: for every endpoint a parser reads, it
asks the live API for one response and asserts that every field the parser
depends on is still there. `scripts/fmp_probe.py` answers "is this endpoint
reachable on this plan"; this answers "does its payload still have the shape
our code assumes", which is the question that was never being asked.

Two modes, and both matter:

  * **offline** — every registered field must appear in the recorded fixture.
    Catches a parser that starts reading a field the fixture never had, which
    makes the fixture (and therefore every test using it) a lie by omission.
    Runs in CI through tests/test_field_probe.py.
  * **live** — every registered field must appear in today's payload. Catches a
    provider rename. Needs D27 consent and costs one call per endpoint.

A field is "present" when at least one row in the response carries the KEY.
Values are not checked: `epsActual` is legitimately null on a future row, and
demanding a value would make the probe fail on the calendar being forward-
looking rather than on the shape changing.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fixtures import fmp_payloads as fx

SYMBOL = "AAPL"
WINDOW_START = date(2026, 10, 1)
WINDOW_END = date(2026, 10, 21)


@dataclass(frozen=True)
class Contract:
    """What one parser needs from one endpoint.

    `reader` names the function, file-and-symbol, so a failure says WHICH code
    breaks rather than only which endpoint moved — and so the AST check in
    tests/test_field_probe.py can scan exactly that function and no other.
    """

    provider: str
    endpoint: str
    reader: str
    required: tuple[str | tuple[str, ...], ...]
    """Each entry is a field, or a tuple of ALTERNATIVE spellings of one field.

    The alternatives mirror the parsers' own `_first(row, "new", "old")` calls:
    the contract is "at least one of these is present", because that is
    precisely what the code needs to work. Registering only the current
    spelling would fail the probe the moment a provider reverted a rename the
    parser already tolerates.
    """
    fixture: object = None
    """A recorded response (list of rows), or None where none exists yet."""
    call: Callable[[object], list[dict]] | None = None
    """How to fetch one live response. None means live-unprobeable."""
    note: str = ""


def _rows(payload) -> list[dict]:
    if isinstance(payload, dict):
        for key in ("candles", "earningsCalendar", "historical"):
            if isinstance(payload.get(key), list):
                return [r for r in payload[key] if isinstance(r, dict)]
        return [payload]
    return [r for r in (payload or []) if isinstance(r, dict)]


CONTRACTS: tuple[Contract, ...] = (
    # ── FMP ─────────────────────────────────────────────────────────────────
    Contract(
        "fmp",
        "/stable/quote",
        "ingest/universe.py:enrich_with_liquidity",
        ("symbol", "name", "price", "marketCap", "exchange"),
        fx.QUOTES,
        lambda c: c.quotes([SYMBOL]),
        note="sector/industry come from the CONSTITUENTS payload, not from a quote",
    ),
    Contract(
        "fmp",
        "/stable/sp500-constituent",
        "ingest/universe.py:merge_constituents",
        ("symbol", "name", "sector", "subSector"),
        None,
        lambda c: c.sp500_constituents()[:2],
        note="`subSector` is read first and `industry` as a fallback",
    ),
    Contract(
        "fmp",
        "/stable/income-statement",
        "ingest/fmp_fundamentals.py:merge_statements",
        (
            "date",
            "period",
            "revenue",
            "grossProfit",
            "operatingIncome",
            "netIncome",
            "ebitda",
            "reportedCurrency",
            "weightedAverageShsOutDil",
            ("epsDiluted", "epsdiluted"),
            ("fiscalYear", "calendarYear"),
            ("filingDate", "fillingDate"),
            "acceptedDate",
        ),
        fx.INCOME,
        lambda c: c.income_statement(SYMBOL, period="quarter", limit=1),
        note="`epsDiluted` vs `epsdiluted` is already handled in both spellings",
    ),
    Contract(
        "fmp",
        "/stable/balance-sheet-statement",
        "ingest/fmp_fundamentals.py:merge_statements",
        (
            "date",
            "totalAssets",
            "totalStockholdersEquity",
            "cashAndCashEquivalents",
            # ONE ENTRY FOR THE DEBT TRIO because the parser takes `totalDebt`
            # when present and otherwise SUMS short- and long-term. A contract
            # of alternatives cannot express that conjunction, so this asserts
            # the weaker true thing: at least one debt field is still there.
            ("totalDebt", "shortTermDebt", "longTermDebt"),
        ),
        fx.BALANCE,
        lambda c: c.balance_sheet(SYMBOL, period="quarter", limit=1),
    ),
    Contract(
        "fmp",
        "/stable/cash-flow-statement",
        "ingest/fmp_fundamentals.py:merge_statements",
        ("date", "operatingCashFlow", "capitalExpenditure", "freeCashFlow"),
        fx.CASHFLOW,
        lambda c: c.cash_flow(SYMBOL, period="quarter", limit=1),
    ),
    Contract(
        "fmp",
        "/stable/ratios",
        "ingest/fmp_fundamentals.py:merge_statements",
        (
            "date",
            "currentRatio",
            "priceToBookRatio",
            "priceToSalesRatio",
            ("debtToEquityRatio", "debtEquityRatio"),
            ("priceToEarningsRatio", "priceEarningsRatio"),
            ("priceToEarningsGrowthRatio", "priceEarningsToGrowthRatio"),
            ("dividendPayoutRatio", "payoutRatio"),
            "dividendPerShare",
        ),
        fx.RATIOS,
        lambda c: c.ratios(SYMBOL, period="quarter", limit=1),
    ),
    Contract(
        "fmp",
        "/stable/key-metrics",
        "ingest/fmp_fundamentals.py:merge_statements",
        (
            "date",
            ("returnOnInvestedCapital", "roic"),
            "returnOnEquity",
            ("evToEBITDA", "enterpriseValueOverEBITDA"),
        ),
        fx.KEY_METRICS,
        lambda c: c.key_metrics(SYMBOL, period="quarter", limit=1),
        note="ROE comes from /ratios; this endpoint supplies ROIC",
    ),
    Contract(
        "fmp",
        "/stable/analyst-estimates",
        "ingest/fmp_estimates.py:parse_estimates",
        (
            "date",
            ("epsAvg", "estimatedEpsAvg"),
            ("epsLow", "estimatedEpsLow"),
            ("epsHigh", "estimatedEpsHigh"),
            ("revenueAvg", "estimatedRevenueAvg"),
            ("revenueLow", "estimatedRevenueLow"),
            ("revenueHigh", "estimatedRevenueHigh"),
            ("ebitdaAvg", "estimatedEbitdaAvg"),
            ("netIncomeAvg", "estimatedNetIncomeAvg"),
            ("numAnalystsEps", "numberAnalystsEstimatedEps"),
            ("numAnalystsRevenue", "numberAnalystEstimatedRevenue"),
        ),
        fx.ANALYST_ESTIMATES_QUARTER,
        lambda c: c.analyst_estimates(SYMBOL, period="quarter", limit=1),
    ),
    Contract(
        "fmp",
        "/stable/earnings",
        "ingest/fmp_events.py:parse_symbol_earnings",
        (
            "date",
            "epsActual",
            "epsEstimated",
            "revenueActual",
            "revenueEstimated",
            # O23: the vendor's revision stamp. In the registry so a rename is
            # caught — it was published all along and simply never read, which
            # is the failure this probe exists for.
            "lastUpdated",
        ),
        None,
        lambda c: c.symbol_earnings(SYMBOL, limit=4),
        note="D47's radar. THE endpoint whose rename started all this.",
    ),
    Contract(
        "fmp",
        "/stable/earnings-calendar",
        "ingest/fmp_events.py:parse_earnings",
        (
            "symbol",
            "date",
            ("epsActual", "eps"),
            "epsEstimated",
            ("revenueActual", "revenue"),
            "revenueEstimated",
        ),
        fx.EARNINGS_CALENDAR,
        lambda c: c.earnings_calendar(WINDOW_START, WINDOW_START + timedelta(days=6)),
        note=(
            "`eps`/`revenue`/`time` are read as FALLBACKS and are gone from live "
            "payloads — registered as optional, which is why only symbol+date are "
            "required here"
        ),
    ),
    Contract(
        "fmp",
        "/stable/dividends-calendar",
        "ingest/fmp_events.py:parse_dividends",
        (
            "symbol",
            "date",
            ("dividend", "adjDividend"),
            "declarationDate",
            "recordDate",
            "paymentDate",
        ),
        fx.DIVIDEND_CALENDAR,
        lambda c: c.dividend_calendar(WINDOW_START, WINDOW_END),
    ),
    Contract(
        "fmp",
        "/stable/splits-calendar",
        "ingest/fmp_events.py:parse_splits",
        ("symbol", "date", "numerator", "denominator"),
        fx.SPLIT_CALENDAR,
        lambda c: c.split_calendar(WINDOW_START, WINDOW_END),
    ),
    Contract(
        "fmp",
        "/stable/dividends",
        "ingest/fmp_events.py:parse_dividends",
        ("symbol", "date", ("dividend", "adjDividend")),
        fx.HISTORICAL_DIVIDENDS,
        lambda c: c.historical_dividends(SYMBOL),
    ),
    Contract(
        "fmp",
        "/stable/historical-price-eod/non-split-adjusted",
        "ingest/fmp_history.py:parse_bars",
        ("date", "adjOpen", "adjHigh", "adjLow", "adjClose", "volume"),
        None,
        lambda c: c.historical_bars_raw(SYMBOL, start=WINDOW_START, end=WINDOW_END),
        note="D24's raw series. The `adj*` names carry UNADJUSTED prices here.",
    ),
    Contract(
        "fmp",
        "/stable/historical-price-eod/full",
        "ingest/universe.py:average_volume",
        ("date", "volume"),
        None,
        lambda c: c.daily_bars(SYMBOL, days=5),
    ),
    Contract(
        "fmp",
        "/stable/nasdaq-constituent",
        "ingest/universe.py:merge_constituents",
        ("symbol", "name", "sector", "subSector"),
        None,
        lambda c: c.nasdaq100_constituents()[:2],
    ),
    Contract(
        "fmp",
        "/stable/batch-quote",
        "ingest/universe.py:enrich_with_liquidity",
        ("symbol", "name", "price", "marketCap", "exchange"),
        None,
        lambda c: c.quotes([SYMBOL, "MSFT"]),
        note="the PLURAL `symbols` parameter; a comma list to /quote returns []",
    ),
    Contract(
        "fmp",
        "/stable/splits",
        "ingest/fmp_events.py:parse_splits",
        ("symbol", "date", "numerator", "denominator"),
        None,
        lambda c: c.historical_splits(SYMBOL),
    ),
    # ── Finnhub ─────────────────────────────────────────────────────────────
    Contract(
        "finnhub",
        "/company-news",
        "ingest/finnhub_news.py:parse_articles",
        ("id", "datetime", "headline", "url", "source", "summary", "category", "image"),
        fx.FINNHUB_NEWS,
        lambda c: c.company_news(SYMBOL, WINDOW_END - timedelta(days=3), WINDOW_END),
    ),
    Contract(
        "finnhub",
        "/calendar/earnings",
        "ingest/finnhub_earnings.py:parse_times",
        ("symbol", "date", "hour"),
        None,
        lambda c: c.earnings_calendar(WINDOW_END, WINDOW_END + timedelta(days=6)),
        note="The ONLY source of a release time in the stack (D49).",
    ),
    # ── Schwab ──────────────────────────────────────────────────────────────
    Contract(
        "schwab",
        "/pricehistory",
        "ingest/schwab_prices.py:parse_candles",
        ("datetime", "open", "high", "low", "close", "volume"),
        None,
        lambda c: _rows(
            c.price_history_daily(
                SYMBOL,
                start=__import__("datetime").datetime(
                    2026, 10, 1, tzinfo=__import__("datetime").UTC
                ),
                end=__import__("datetime").datetime(
                    2026, 10, 21, tzinfo=__import__("datetime").UTC
                ),
            )
        ),
    ),
)


@dataclass
class Finding:
    contract: Contract
    source: str
    missing: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()
    error: str | None = None
    rows: int = 0
    keys: tuple[str, ...] = field(default_factory=tuple)

    @property
    def drifted(self) -> bool:
        return bool(self.missing) or self.error is not None


def _alternatives(entry: str | tuple[str, ...]) -> tuple[str, ...]:
    return (entry,) if isinstance(entry, str) else entry


def check_rows(contract: Contract, rows: list[dict], *, source: str) -> Finding:
    keys = {k for row in rows for k in row}
    missing = tuple(
        " | ".join(_alternatives(entry))
        for entry in contract.required
        if not (set(_alternatives(entry)) & keys)
    )
    registered = {name for entry in contract.required for name in _alternatives(entry)}
    extra = tuple(sorted(keys - registered))
    return Finding(
        contract, source, missing=missing, extra=extra, rows=len(rows), keys=tuple(sorted(keys))
    )


def check_fixtures(contracts=CONTRACTS) -> list[Finding]:
    """Offline: the recorded fixture must contain every registered field."""
    out = []
    for contract in contracts:
        if contract.fixture is None:
            continue
        out.append(check_rows(contract, _rows(contract.fixture), source="fixture"))
    return out


def _client(provider: str):
    if provider == "fmp":
        from ingest.fmp_client import FMPClient

        return FMPClient()
    if provider == "finnhub":
        from ingest.finnhub_client import FinnhubClient

        return FinnhubClient()
    if provider == "schwab":
        from ingest.schwab_client import SchwabClient

        return SchwabClient()
    raise ValueError(f"unknown provider {provider!r}")


def check_live(contracts=CONTRACTS, *, providers: set[str] | None = None) -> list[Finding]:
    """Live: today's payload must contain every registered field."""
    out: list[Finding] = []
    clients: dict[str, object] = {}
    for contract in contracts:
        if contract.call is None:
            continue
        if providers and contract.provider not in providers:
            continue
        try:
            if contract.provider not in clients:
                clients[contract.provider] = _client(contract.provider)
            rows = _rows(contract.call(clients[contract.provider]))
        except Exception as exc:  # one dead endpoint must not hide the rest
            out.append(Finding(contract, "live", error=f"{type(exc).__name__}: {exc}"[:160]))
            continue
        out.append(check_rows(contract, rows, source="live"))
    return out


def render(findings: list[Finding], *, verbose: bool = False) -> str:
    lines = [
        "=" * 100,
        "FIELD CONTRACTS — what our parsers read vs what the payload has",
        "=" * 100,
        "",
    ]
    drifted = [f for f in findings if f.drifted]
    for finding in findings:
        mark = "DRIFT" if finding.drifted else "ok   "
        lines.append(
            f"{mark} {finding.contract.provider:<9} {finding.contract.endpoint:<48} "
            f"{finding.source:<8} {finding.rows:>4} row(s)"
        )
        if finding.error:
            lines.append(f"      error: {finding.error}")
        if finding.missing:
            lines.append(
                f"      MISSING, and {finding.contract.reader} reads them: {list(finding.missing)}"
            )
        if verbose and finding.extra:
            lines.append(f"      unread fields present: {list(finding.extra)}")
    lines.append("")
    if drifted:
        lines.append(f"{len(drifted)} endpoint(s) no longer send a field a parser reads.")
        lines.append("A fixture cannot catch this: it proves the parser handles a shape it was")
        lines.append("SHOWN, never that the provider still sends it.")
    else:
        lines.append("Every registered field is present. Fixtures and live payloads agree.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--offline", action="store_true", help="fixtures only, no network")
    parser.add_argument("--provider", action="append", help="limit live checks (repeatable)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    findings = check_fixtures()
    if not args.offline:
        findings += check_live(providers=set(args.provider) if args.provider else None)
    print(render(findings, verbose=args.verbose))
    return 1 if any(f.drifted for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
