"""Probe which FMP endpoints this account can actually reach.

    ALLOW_LIVE_CALLS=1 python scripts/fmp_probe.py

FMP runs two API generations side by side and gates endpoints INDIVIDUALLY by
plan. Both facts matter, and the second one is why this probe covers EVERY
endpoint `ingest/fmp_client.py` calls rather than sampling a few: an earlier
version tested one fundamentals endpoint, saw it pass, and reported
"fundamentals work" while `ratios` and `key-metrics` were both 402.

`tests/test_fmp_probe.py` asserts the coverage stays complete.

It distinguishes **402 from 404**. They have different fixes:
  * 404 — the path is wrong for this generation. A code bug.
  * 402 — the path is right, the plan does not include it. A billing decision.

This is the only thing in the repo that talks to the live API without a
`job_runs` row, because it is a diagnostic rather than a job. It still asks for
consent (D27): running it IS the consent, but the consent should be visible in
shell history.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from core import netguard, settings

TIMEOUT_S = 20
SYMBOL = "AAPL"
WINDOW = {"from": "2026-10-01", "to": "2026-10-08"}

# (label, legacy path + params, stable path + params).
# One row per method on FMPClient. Keep in sync — the test enforces it.
PROBES: list[tuple[str, tuple[str, dict], tuple[str, dict]]] = [
    (
        "sp500 constituents",
        ("/api/v3/sp500_constituent", {}),
        ("/stable/sp500-constituent", {}),
    ),
    (
        "nasdaq constituents",
        ("/api/v3/nasdaq_constituent", {}),
        ("/stable/nasdaq-constituent", {}),
    ),
    (
        "quote",
        (f"/api/v3/quote/{SYMBOL}", {}),
        ("/stable/quote", {"symbol": SYMBOL}),
    ),
    (
        # A SEPARATE endpoint, not the same one with more symbols. Passing a
        # comma-separated list to /stable/quote returns HTTP 200 and `[]`, and
        # the parameter is `symbols` (plural) here — `symbol` is a 400. Probed
        # on its own because the universe builder's enrichment pass depends on
        # it and a silent empty result is indistinguishable from a quiet market.
        "batch quote",
        (f"/api/v3/quote/{SYMBOL},MSFT", {}),
        ("/stable/batch-quote", {"symbols": f"{SYMBOL},MSFT"}),
    ),
    (
        "income statement",
        (f"/api/v3/income-statement/{SYMBOL}", {"period": "quarter", "limit": 1}),
        ("/stable/income-statement", {"symbol": SYMBOL, "period": "quarter", "limit": 1}),
    ),
    (
        "balance sheet",
        (f"/api/v3/balance-sheet-statement/{SYMBOL}", {"period": "quarter", "limit": 1}),
        ("/stable/balance-sheet-statement", {"symbol": SYMBOL, "period": "quarter", "limit": 1}),
    ),
    (
        "cash flow",
        (f"/api/v3/cash-flow-statement/{SYMBOL}", {"period": "quarter", "limit": 1}),
        ("/stable/cash-flow-statement", {"symbol": SYMBOL, "period": "quarter", "limit": 1}),
    ),
    (
        "ratios",
        (f"/api/v3/ratios/{SYMBOL}", {"period": "quarter", "limit": 1}),
        ("/stable/ratios", {"symbol": SYMBOL, "period": "quarter", "limit": 1}),
    ),
    (
        "key metrics",
        (f"/api/v3/key-metrics/{SYMBOL}", {"period": "quarter", "limit": 1}),
        ("/stable/key-metrics", {"symbol": SYMBOL, "period": "quarter", "limit": 1}),
    ),
    (
        "analyst estimates",
        (f"/api/v3/analyst-estimates/{SYMBOL}", {"period": "quarter", "limit": 1}),
        ("/stable/analyst-estimates", {"symbol": SYMBOL, "period": "quarter", "limit": 1}),
    ),
    (
        "earnings calendar",
        ("/api/v3/earning_calendar", WINDOW),
        ("/stable/earnings-calendar", WINDOW),
    ),
    (
        "dividend calendar",
        ("/api/v3/stock_dividend_calendar", WINDOW),
        ("/stable/dividends-calendar", WINDOW),
    ),
    (
        "split calendar",
        ("/api/v3/stock_split_calendar", WINDOW),
        ("/stable/splits-calendar", WINDOW),
    ),
    (
        "historical dividends",
        (f"/api/v3/historical-price-full/stock_dividend/{SYMBOL}", {}),
        ("/stable/dividends", {"symbol": SYMBOL}),
    ),
    (
        "historical splits",
        (f"/api/v3/historical-price-full/stock_split/{SYMBOL}", {}),
        ("/stable/splits", {"symbol": SYMBOL}),
    ),
    (
        "daily bars",
        (f"/api/v3/historical-price-full/{SYMBOL}", {"timeseries": 5}),
        ("/stable/historical-price-eod/full", {"symbol": SYMBOL, **WINDOW}),
    ),
    (
        # The RAW series D24 stores. Separate from the one above because the
        # `full` variant is split-ADJUSTED, and a plan that gates one and not
        # the other would leave the engine storing double-adjustable bars.
        "daily bars (raw)",
        (f"/api/v3/historical-price-full/{SYMBOL}", {"serietype": "line"}),
        ("/stable/historical-price-eod/non-split-adjusted", {"symbol": SYMBOL, **WINDOW}),
    ),
    (
        # Per-symbol earnings, which replaced the market-wide calendar for the
        # event radar: that one is capped at 4,000 rows and truncates silently.
        "earnings (per symbol)",
        (f"/api/v3/historical/earning_calendar/{SYMBOL}", {}),
        ("/stable/earnings", {"symbol": SYMBOL, "limit": 4}),
    ),
]

# What the free tier was observed to gate (D29). Printed alongside the result so
# a change is obvious rather than something you have to remember.
KNOWN_PLAN_GATED = {
    "sp500 constituents",
    "nasdaq constituents",
    "analyst estimates",
    "ratios",
    "key metrics",
}


def probe(base: str, key: str, path: str, params: dict) -> tuple[bool, str]:
    """Return (usable, one-line explanation). Never raises."""
    try:
        response = requests.get(
            f"{base}{path}", params={**params, "apikey": key}, timeout=TIMEOUT_S
        )
    except requests.RequestException as exc:
        return False, f"network error: {type(exc).__name__}"

    if response.status_code == 402:
        return False, "HTTP 402 (path is right; plan does not cover it)"
    if response.status_code == 404:
        return False, "HTTP 404 (endpoint not on this API generation)"
    if response.status_code in (401, 403):
        return False, f"HTTP {response.status_code} (key rejected)"
    if not response.ok:
        return False, f"HTTP {response.status_code}"

    try:
        payload = response.json()
    except ValueError:
        return False, "non-JSON response"

    if isinstance(payload, dict) and "Error Message" in payload:
        return False, f"error: {str(payload['Error Message'])[:60]}"
    if isinstance(payload, list) and not payload:
        return True, "OK but empty (endpoint exists; window may have no data)"
    size = len(payload) if isinstance(payload, list | dict) else 0
    return True, f"OK ({size} item(s))"


def main() -> int:
    # This tool exists to make live calls, so it still asks. Running it IS the
    # consent, but the consent should be visible in shell history.
    if not netguard.live_calls_allowed():
        print(
            f"this probe makes live FMP requests, which need explicit consent:\n"
            f"    {netguard.FLAG}=1 python scripts/fmp_probe.py"
        )
        return 2

    try:
        key = settings.fmp_api_key()
    except settings.SettingsError as exc:
        print(f"cannot probe: {exc}")
        return 2

    base = settings.fmp_base_url()
    print(f"probing {base} with the key in .env\n")
    print(f"  {'endpoint':<22} {'/api/v3/':<46} /stable/")
    print(f"  {'-' * 22} {'-' * 46} {'-' * 46}")

    legacy_ok = stable_ok = 0
    gated: list[str] = []
    for label, (v3_path, v3_params), (st_path, st_params) in PROBES:
        v3_usable, v3_note = probe(base, key, v3_path, v3_params)
        st_usable, st_note = probe(base, key, st_path, st_params)
        legacy_ok += v3_usable
        stable_ok += st_usable
        if not st_usable and "402" in st_note:
            gated.append(label)
        mark = "PASS" if st_usable else "fail"
        v3_mark = "PASS" if v3_usable else "fail"
        print(f"  {label:<22} {v3_mark} {v3_note:<41} {mark} {st_note}")

    total = len(PROBES)
    print(f"\n  /api/v3/ : {legacy_ok}/{total} endpoints usable")
    print(f"  /stable/ : {stable_ok}/{total} endpoints usable")

    if gated:
        print(f"\n  plan-gated (402): {', '.join(sorted(gated))}")
        new = set(gated) - KNOWN_PLAN_GATED
        lifted = KNOWN_PLAN_GATED - set(gated)
        if new:
            print(f"  NEW since D29: {', '.join(sorted(new))}")
        if lifted:
            print(f"  NO LONGER GATED since D29: {', '.join(sorted(lifted))}")
            print("  -> update D29, flip fmp_estimates back to is_critical, and")
            print("     record the estimate-revision clock start date.")

    if stable_ok > legacy_ok:
        print("\n  VERDICT: this account is on /stable/ — ingest/fmp_client.py is correct.")
    elif legacy_ok > stable_ok:
        print("\n  VERDICT: this account answers on /api/v3/ — fmp_client.py needs migrating.")
    else:
        print("\n  VERDICT: neither generation answered. Check FMP_API_KEY and plan status.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
