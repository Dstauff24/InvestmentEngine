"""Quarterly and annual financials + key ratios (D4) -> `fundamentals`.

    ALLOW_LIVE_CALLS=1 python -m ingest.fmp_fundamentals        # stale symbols only
    ALLOW_LIVE_CALLS=1 python -m ingest.fmp_fundamentals --all
    ALLOW_LIVE_CALLS=1 python -m ingest.fmp_fundamentals --symbols AAPL MSFT

Pillar B inputs: growth acceleration, margin trend, balance-sheet quality,
valuation, and the dividend-health fields D19 needs for the stable buckets.

Unlike `fmp_estimates` this is NOT a snapshot job. Rows are keyed by fiscal
period, so a restatement legitimately overwrites one and a re-run changes
nothing. That is also why it is not critical: a failure costs freshness, not
history.

**Cost control.** Financials change four times a year, so re-pulling ~500 names
every evening would spend ~2,500 calls rewriting identical rows — far past the
D29 daily budget. By default only symbols whose stored data is older than
`fundamentals.refresh_after_days`, or that have reported since, are pulled, and
the run stops at `fmp.daily_call_budget`.

**D29 degradation.** `/stable/ratios` and `/stable/key-metrics` are 402 on the
free tier, SEPARATELY from the three core statements. One 402 must not cost the
whole symbol, so those two degrade to empty and leave their columns NULL; the
gap is reported in `job_runs.details.unavailable_endpoints`.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from functools import partial

from core import clock, config, db
from core.budget import BudgetExhausted
from core.jobs import job_run
from ingest.fmp_client import CALLS_PER_SYMBOL_FUNDAMENTALS, FMPClient, FMPPlanError
from ingest.runner import as_float, as_int, for_each_symbol, safe_ratio

log = logging.getLogger(__name__)

JOB_NAME = "fmp_fundamentals"

# The three statements are the core of Pillar B: growth, margins, cash and
# balance-sheet strength all come from them. `ratios` and `key-metrics` are
# ENRICHMENT — valuation multiples, ROIC, payout ratio — and are separately
# plan-gated. A 402 on enrichment must not cost us the statements.
OPTIONAL_ENDPOINTS = ("ratios", "key-metrics")

# Discovered at runtime, logged once each rather than per symbol.
_UNAVAILABLE: set[str] = set()

UPSERT = """
    INSERT INTO fundamentals (
        symbol, period_type, fiscal_date_ending, calendar_year, fiscal_period,
        reported_currency,
        revenue, gross_profit, operating_income, net_income, ebitda,
        eps_diluted, shares_diluted,
        operating_cash_flow, capital_expenditure, free_cash_flow,
        total_assets, total_equity, total_debt, cash_and_equivalents,
        gross_margin, operating_margin, net_margin, roe, roic,
        current_ratio, debt_to_equity,
        pe_ratio, ps_ratio, pb_ratio, ev_to_ebitda, peg_ratio,
        dividend_per_share, payout_ratio,
        filing_date, accepted_date, raw, source
    ) VALUES (
        %(symbol)s, %(period_type)s, %(fiscal_date_ending)s, %(calendar_year)s,
        %(fiscal_period)s, %(reported_currency)s,
        %(revenue)s, %(gross_profit)s, %(operating_income)s, %(net_income)s, %(ebitda)s,
        %(eps_diluted)s, %(shares_diluted)s,
        %(operating_cash_flow)s, %(capital_expenditure)s, %(free_cash_flow)s,
        %(total_assets)s, %(total_equity)s, %(total_debt)s, %(cash_and_equivalents)s,
        %(gross_margin)s, %(operating_margin)s, %(net_margin)s, %(roe)s, %(roic)s,
        %(current_ratio)s, %(debt_to_equity)s,
        %(pe_ratio)s, %(ps_ratio)s, %(pb_ratio)s, %(ev_to_ebitda)s, %(peg_ratio)s,
        %(dividend_per_share)s, %(payout_ratio)s,
        %(filing_date)s, %(accepted_date)s, %(raw)s, 'fmp'
    )
    ON CONFLICT (symbol, period_type, fiscal_date_ending) DO UPDATE SET
        calendar_year        = EXCLUDED.calendar_year,
        fiscal_period        = EXCLUDED.fiscal_period,
        reported_currency    = EXCLUDED.reported_currency,
        revenue              = EXCLUDED.revenue,
        gross_profit         = EXCLUDED.gross_profit,
        operating_income     = EXCLUDED.operating_income,
        net_income           = EXCLUDED.net_income,
        ebitda               = EXCLUDED.ebitda,
        eps_diluted          = EXCLUDED.eps_diluted,
        shares_diluted       = EXCLUDED.shares_diluted,
        operating_cash_flow  = EXCLUDED.operating_cash_flow,
        capital_expenditure  = EXCLUDED.capital_expenditure,
        free_cash_flow       = EXCLUDED.free_cash_flow,
        total_assets         = EXCLUDED.total_assets,
        total_equity         = EXCLUDED.total_equity,
        total_debt           = EXCLUDED.total_debt,
        cash_and_equivalents = EXCLUDED.cash_and_equivalents,
        gross_margin         = EXCLUDED.gross_margin,
        operating_margin     = EXCLUDED.operating_margin,
        net_margin           = EXCLUDED.net_margin,
        roe                  = COALESCE(EXCLUDED.roe, fundamentals.roe),
        roic                 = COALESCE(EXCLUDED.roic, fundamentals.roic),
        current_ratio        = COALESCE(EXCLUDED.current_ratio, fundamentals.current_ratio),
        debt_to_equity       = COALESCE(EXCLUDED.debt_to_equity, fundamentals.debt_to_equity),
        pe_ratio             = COALESCE(EXCLUDED.pe_ratio, fundamentals.pe_ratio),
        ps_ratio             = COALESCE(EXCLUDED.ps_ratio, fundamentals.ps_ratio),
        pb_ratio             = COALESCE(EXCLUDED.pb_ratio, fundamentals.pb_ratio),
        ev_to_ebitda         = COALESCE(EXCLUDED.ev_to_ebitda, fundamentals.ev_to_ebitda),
        peg_ratio            = COALESCE(EXCLUDED.peg_ratio, fundamentals.peg_ratio),
        dividend_per_share   = COALESCE(EXCLUDED.dividend_per_share,
                                        fundamentals.dividend_per_share),
        payout_ratio         = COALESCE(EXCLUDED.payout_ratio, fundamentals.payout_ratio),
        filing_date          = EXCLUDED.filing_date,
        accepted_date        = EXCLUDED.accepted_date,
        raw                  = EXCLUDED.raw,
        ingested_at          = now()
"""
# Note the asymmetry above: statement-derived columns overwrite unconditionally
# (a restatement is the truth), while the plan-gated enrichment columns use
# COALESCE so a degraded run cannot ERASE values a pre-gate run had captured.


def _as_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def merge_statements(
    symbol: str,
    period_type: str,
    income: list[dict],
    balance: list[dict],
    cashflow: list[dict],
    ratios: list[dict],
    metrics: list[dict],
) -> list[dict]:
    """Join five FMP endpoints on fiscal period end into one row per period.

    The income statement drives the row set: a period FMP has ratios for but no
    income statement is not a period we can score. Missing companions leave
    their columns NULL rather than dropping the period, because Pillar B reads
    TRENDS and one gap in the cash-flow series should not blank three years of
    margins.

    Margins are computed from the statements rather than taken from the ratios
    endpoint, so numerator and denominator always come from the same filing —
    and so they survive the D29 ratios gate.
    """
    by_period: dict[date, dict] = {}
    for source, key in (
        (balance, "balance"),
        (cashflow, "cashflow"),
        (ratios, "ratios"),
        (metrics, "metrics"),
    ):
        for row in source or []:
            period = _as_date(row.get("date"))
            if period:
                by_period.setdefault(period, {})[key] = row

    out: list[dict] = []
    for row in income or []:
        period = _as_date(row.get("date"))
        if period is None:
            continue
        companions = by_period.get(period, {})
        bal = companions.get("balance", {})
        cash = companions.get("cashflow", {})
        rat = companions.get("ratios", {})
        met = companions.get("metrics", {})

        revenue = as_float(row.get("revenue"))
        total_debt = as_float(bal.get("totalDebt"))
        if total_debt is None:
            short_term = as_float(bal.get("shortTermDebt")) or 0.0
            long_term = as_float(bal.get("longTermDebt")) or 0.0
            total_debt = (short_term + long_term) or None

        out.append(
            {
                "symbol": symbol,
                "period_type": period_type,
                "fiscal_date_ending": period,
                # `/stable/` renamed /api/v3/'s `calendarYear` to `fiscalYear`.
                # Both accepted so a generation or plan change cannot silently
                # blank the column.
                "calendar_year": as_int(row.get("fiscalYear") or row.get("calendarYear")),
                "fiscal_period": row.get("period"),
                "reported_currency": row.get("reportedCurrency"),
                "revenue": revenue,
                "gross_profit": as_float(row.get("grossProfit")),
                "operating_income": as_float(row.get("operatingIncome")),
                "net_income": as_float(row.get("netIncome")),
                "ebitda": as_float(row.get("ebitda")),
                # Likewise `epsdiluted` (v3) vs `epsDiluted` (/stable/).
                "eps_diluted": as_float(row.get("epsdiluted") or row.get("epsDiluted")),
                "shares_diluted": as_float(row.get("weightedAverageShsOutDil")),
                "operating_cash_flow": as_float(cash.get("operatingCashFlow")),
                "capital_expenditure": as_float(cash.get("capitalExpenditure")),
                "free_cash_flow": as_float(cash.get("freeCashFlow")),
                "total_assets": as_float(bal.get("totalAssets")),
                "total_equity": as_float(bal.get("totalStockholdersEquity")),
                "total_debt": total_debt,
                "cash_and_equivalents": as_float(bal.get("cashAndCashEquivalents")),
                "gross_margin": safe_ratio(row.get("grossProfit"), revenue),
                "operating_margin": safe_ratio(row.get("operatingIncome"), revenue),
                "net_margin": safe_ratio(row.get("netIncome"), revenue),
                "roe": as_float(rat.get("returnOnEquity")),
                "roic": as_float(met.get("roic")),
                "current_ratio": as_float(rat.get("currentRatio")),
                "debt_to_equity": as_float(rat.get("debtEquityRatio")),
                "pe_ratio": as_float(rat.get("priceEarningsRatio")),
                "ps_ratio": as_float(rat.get("priceToSalesRatio")),
                "pb_ratio": as_float(rat.get("priceToBookRatio")),
                "ev_to_ebitda": as_float(met.get("enterpriseValueOverEBITDA")),
                "peg_ratio": as_float(rat.get("priceEarningsToGrowthRatio")),
                "dividend_per_share": as_float(met.get("dividendPerShare")),
                "payout_ratio": as_float(rat.get("payoutRatio")),
                "filing_date": _as_date(row.get("filingDate") or row.get("fillingDate")),
                "accepted_date": row.get("acceptedDate") or None,
                "raw": None,
            }
        )
    return out


# A symbol FMP certainly knows, used to tell a plan gate apart from a bad
# symbol. Deliberately not taken from the universe: it must be a constant, or
# the canary could itself be the odd symbol that triggered the 402.
CANARY_SYMBOL = "AAPL"


def _optional(label: str, fetch, *, canary=None) -> list[dict]:
    """Call an optional enrichment endpoint; degrade to empty if the plan lacks it.

    A 402 is NOT sufficient evidence of a plan gate. Verified live on
    2026-10-21: `/stable/ratios?symbol=BRK.B` answers 402 while
    `?symbol=AAPL` answers 200, and an unknown symbol like `BOGUSXYZ` answers
    200 with an empty list. So on this API a 402 can mean "the plan excludes
    this endpoint" OR "this endpoint dislikes that symbol" — and the two want
    opposite handling.

    Inferring a plan gate from one symbol's 402 would disable the endpoint for
    EVERY remaining symbol in the run and leave the valuation columns NULL
    wholesale, announced by a log line confidently naming the wrong cause.
    Today the universe stores dash-form symbols (BRK-B) so nothing triggers it;
    that is a property of `normalize_symbol`, not a guarantee, and this is too
    quiet a failure to leave resting on it.

    So the gate is confirmed with one call for a symbol FMP certainly knows. If
    the canary succeeds, the 402 was about the symbol: a per-symbol miss, no
    endpoint marked.
    """
    try:
        return fetch()
    except FMPPlanError as exc:
        if label in _UNAVAILABLE:
            return []

        if canary is not None:
            try:
                canary()
            except FMPPlanError:
                pass  # the canary 402s too: a real plan gate, fall through
            else:
                log.warning(
                    "/stable/%s returned 402 for this symbol but 200 for %s — so the "
                    "plan DOES cover it and the refusal was symbol-specific. Treating "
                    "it as a per-symbol miss; the endpoint stays enabled. (%s)",
                    label,
                    CANARY_SYMBOL,
                    exc,
                )
                return []

        _UNAVAILABLE.add(label)
        log.warning(
            "FMP plan does not cover /stable/%s — continuing without it. The "
            "valuation and quality columns it feeds stay NULL for every symbol "
            "until the plan is upgraded (D29). (%s)",
            label,
            exc,
        )
        return []


def unavailable_endpoints() -> list[str]:
    """Which optional endpoints this process found to be plan-gated."""
    return sorted(_UNAVAILABLE)


def reset_availability() -> None:
    """Forget what was discovered. For tests."""
    _UNAVAILABLE.clear()


def stale_symbols(all_symbols: list[str], *, refresh_after_days: int) -> list[str]:
    """Symbols whose stored fundamentals are older than the refresh window.

    A symbol with no rows at all is always included. So is one that has reported
    since its last pull — `events` knows the earnings date even when the
    financials have not landed yet.
    """
    rows = db.fetch_all(
        """
        SELECT    t.symbol
        FROM      tickers t
        LEFT JOIN LATERAL (
            SELECT max(ingested_at) AS last_pull
            FROM   fundamentals f
            WHERE  f.symbol = t.symbol
        ) f ON TRUE
        WHERE     t.is_active
          AND     (
                    f.last_pull IS NULL
                 OR f.last_pull < now() - make_interval(days => %(days)s)
                 OR EXISTS (
                        SELECT 1 FROM events e
                        WHERE  e.symbol = t.symbol
                          AND  e.event_type = 'earnings'
                          AND  e.event_date BETWEEN (CURRENT_DATE - 30) AND CURRENT_DATE
                          AND  e.event_date > COALESCE(f.last_pull::date, DATE '1900-01-01')
                    )
                  )
        """,
        {"days": refresh_after_days},
    )
    wanted = {r["symbol"] for r in rows}
    return [s for s in all_symbols if s in wanted]


def load_symbol(client: FMPClient, symbol: str, *, scope: config.FundamentalsScope) -> int:
    written = 0
    for period_type, limit in (
        ("quarter", scope.quarters_history),
        ("annual", scope.annuals_history),
    ):
        rows = merge_statements(
            symbol,
            period_type,
            client.income_statement(symbol, period=period_type, limit=limit),
            client.balance_sheet(symbol, period=period_type, limit=limit),
            client.cash_flow(symbol, period=period_type, limit=limit),
            _optional(
                "ratios",
                partial(client.ratios, symbol, period=period_type, limit=limit),
                canary=partial(client.ratios, CANARY_SYMBOL, period="quarter", limit=1),
            ),
            _optional(
                "key-metrics",
                partial(client.key_metrics, symbol, period=period_type, limit=limit),
                canary=partial(client.key_metrics, CANARY_SYMBOL, period="quarter", limit=1),
            ),
        )
        if rows:
            written += db.executemany(UPSERT, rows)
    return written


def run(
    client: FMPClient | None = None,
    *,
    symbols: list[str] | None = None,
    force_all: bool = False,
) -> int:
    client = client or FMPClient()
    cfg = config.universe()
    scope = cfg.fundamentals
    universe = symbols if symbols is not None else db.active_symbols()
    if not universe:
        raise RuntimeError("no active tickers; run `python -m ingest.universe` first")

    targets = (
        universe
        if force_all or symbols is not None
        else stale_symbols(universe, refresh_after_days=scope.refresh_after_days)
    )

    # No local cap: the SHARED day budget is enforced inside FMPClient.get()
    # (D29), so a symbol simply stops being affordable mid-run and the loop
    # reports declined spend. A per-job cap here was the bug — two jobs could
    # each stay "within budget" and jointly blow the day.
    per_symbol = CALLS_PER_SYMBOL_FUNDAMENTALS * len(("quarter", "annual"))
    log.info(
        "fundamentals: %d of %d symbols are stale (~%d calls each)",
        len(targets),
        len(universe),
        per_symbol,
    )

    with job_run(JOB_NAME, session_date=clock.session_date(), triggered_by="manual") as job:
        if not targets:
            job.note(reason="everything is fresh", universe=len(universe))
            return 0
        outcome = for_each_symbol(
            targets,
            lambda s: load_symbol(client, s, scope=scope),
            run=job,
            label="fundamentals",
            stop_on=BudgetExhausted,
        )
        job.note(
            failed_symbols=sorted(outcome.failed)[:50],
            refreshed=len(outcome.ok),
            stale=len(targets),
            declined_spend=outcome.declined,
            universe=len(universe),
            unavailable_endpoints=unavailable_endpoints(),
        )
        if outcome.declined:
            log.warning(
                "stopped on the shared FMP day budget with %d of %d stale symbol(s) "
                "unrefreshed; they are picked up on the next run (D29)",
                outcome.declined,
                len(targets),
            )
        if _UNAVAILABLE:
            log.warning(
                "wrote fundamentals WITHOUT %s; valuation and quality columns are NULL",
                ", ".join(sorted(_UNAVAILABLE)),
            )
    return outcome.rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--all", action="store_true", help="ignore staleness; refresh everything")
    parser.add_argument("--symbols", nargs="*", help="override the universe (debugging)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    rows = run(symbols=args.symbols, force_all=args.all)
    print(f"fundamentals: {rows} rows")
    if unavailable_endpoints():
        print(f"  plan-gated, columns left NULL: {', '.join(unavailable_endpoints())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
