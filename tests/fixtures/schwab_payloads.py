"""Schwab payloads for tests, shaped from a LIVE-VALIDATED capture.

The SHAPE here was validated against a real SPY option chain (8,338 contracts,
23 expirations) pulled through `ingest/schwab_client.py`. Every field name the
parser reads was confirmed present and correctly spelled — there were no
renames. What the diff DID find is recorded here so the next reader does not
re-learn it:

  * `expirationType` has FOUR values, not the three the docs imply: 'S' standard
    monthly (third Friday), 'W' weekly, 'Q' quarter-end, and **'M' MONTH-END**.
    'M' reads like "monthly" and is not — month-end lands on whatever weekday it
    lands on. Only 'S' is what D22 means. Tag and third-Friday heuristic agreed
    on all 23 live expirations.
  * **`extrinsicValue` is NOT the extrinsic value.** On an OTM call marked at
    0.11 it reads 84.91, while `timeValue` reads 0.07. The two agree on ITM
    contracts and diverge on 2,012 of 4,169 overall, so a spot check on an ATM
    contract would miss it. The parser reads `timeValue`, and D19(4)'s
    assignment-risk test depends on that. The fixture below carries
    `extrinsicValue` with THE SAME WRONGNESS the real payload has, pinned by
    regression test, so a future edit that "helpfully" switches to the
    better-named field fails.
  * `volatility` is a PERCENT and can be garbage on illiquid contracts — the
    live chain ranged 7.5 to 2473, median 21.6. Nothing above 300 survived the
    D22 filter (they live in 0-DTE weeklies and far strikes), so no clamp is
    applied. No -999 sentinels appeared in that capture, but the handling stays.
  * The chain carries `isChainTruncated` and `isDelayed`, both of which the
    parser refuses on.
  * `expirationDate` is an ISO string, not epoch ms.

The contract dicts carry the full 54-field live shape rather than the handful
the parser reads, so a future change that starts reading `settlementType` or
`optionDeliverablesList` finds them already present and correctly typed.

VALUES are synthetic — a tidy $100 underlying with round strikes — because tests
need arithmetic checkable by eye. The SHAPE is real.
"""

from __future__ import annotations

import copy
from datetime import UTC, date, datetime, timedelta

SPOT = 100.0
AS_OF = date(2026, 10, 20)

# Third Fridays: the standard monthlies D22 wants.
MONTHLIES = [date(2026, 11, 20), date(2026, 12, 18), date(2027, 1, 15), date(2027, 2, 19)]
FIFTH_MONTHLY = date(2027, 3, 19)
WEEKLY = date(2026, 11, 27)
QUARTER_END = date(2026, 12, 31)
MONTH_END = date(2026, 11, 30)  # a Monday — the 'M' trap

# -25% .. +25% in $5 steps. The D22 +/-15% band keeps 85..115.
STRIKES = [75.0, 80.0, 85.0, 90.0, 95.0, 100.0, 105.0, 110.0, 115.0, 120.0, 125.0]


def _contract(
    *, option_type: str, strike: float, expiry: date, iv_pct: float, exp_type: str
) -> dict:
    dte = (expiry - AS_OF).days
    moneyness = (strike - SPOT) / SPOT
    # Crude but monotonic: delta walks from ~1 deep ITM to ~0 deep OTM.
    if option_type == "CALL":
        delta = max(0.01, min(0.99, 0.5 - moneyness * 2.0))
        intrinsic = max(0.0, SPOT - strike)
    else:
        delta = -max(0.01, min(0.99, 0.5 + moneyness * 2.0))
        intrinsic = max(0.0, strike - SPOT)
    extrinsic = round(max(0.05, 6.0 - abs(moneyness) * 18.0) * (dte / 30.0) ** 0.5, 2)
    mark = round(intrinsic + extrinsic, 2)
    letter = "C" if option_type == "CALL" else "P"
    return {
        # ── fields the parser reads ──────────────────────────────────────────
        "putCall": option_type,
        "symbol": f"TEST  {expiry:%y%m%d}{letter}{int(strike * 1000):08d}",
        "bid": round(mark - 0.05, 2),
        "ask": round(mark + 0.05, 2),
        "last": mark,
        "mark": mark,
        "totalVolume": 120,
        "openInterest": 3400,
        "volatility": iv_pct,  # Schwab reports PERCENT, e.g. 28.5
        "delta": round(delta, 4),
        "gamma": 0.021,
        "theta": -0.045,
        "vega": 0.112,
        "rho": 0.031,
        "strikePrice": strike,
        "daysToExpiration": dte,
        "expirationType": exp_type,
        "intrinsicValue": intrinsic,
        "timeValue": extrinsic,  # the REAL extrinsic value
        "inTheMoney": intrinsic > 0,
        "nonStandard": False,
        "mini": False,
        # ── present in the live payload, not yet read ────────────────────────
        # `extrinsicValue` is deliberately WRONG, exactly as Schwab has it: for
        # an OTM contract it reports the distance to the strike rather than the
        # option's extrinsic value. Pinned by regression test.
        "extrinsicValue": round(abs(strike - SPOT), 2),
        "description": f"TEST {expiry:%m/%d/%Y} {strike:.2f} {letter}",
        "exchangeName": "OPR",
        "exerciseType": "A",
        "settlementType": "P",
        "expirationDate": f"{expiry:%Y-%m-%d}T20:00:00.000+00:00",  # ISO, not epoch ms
        "lastTradingDay": 1792195200000,
        "multiplier": 100.0,
        "deliverableNote": "100 TEST",
        "optionDeliverablesList": [
            {"symbol": "TEST", "assetType": "STOCK", "deliverableUnits": 100.0}
        ],
        "optionRoot": "TEST",
        "pennyPilot": True,
        "bidSize": 74,
        "askSize": 50,
        "bidAskSize": "74X50",
        "lastSize": 1,
        "openPrice": mark,
        "highPrice": mark + 1.0,
        "lowPrice": max(0.01, mark - 1.0),
        "closePrice": max(0.01, mark - 0.3),
        "high52Week": mark + 16.0,
        "low52Week": max(0.01, mark - 7.0),
        "netChange": 1.5,
        "markChange": 1.3,
        "markPercentChange": 9.83,
        "percentChange": 11.34,
        "breakEven": strike + extrinsic,
        "theoreticalOptionValue": max(0.01, mark - 3.4),
        "theoreticalVolatility": 29.0,
        "quoteTimeInLong": 1792380099980,
        "tradeTimeInLong": 1792379350958,
        "ssid": 137834897,
    }


def _expiry_map(option_type: str, expiries: list[tuple[date, float, str]]) -> dict:
    out = {}
    for expiry, iv_pct, exp_type in expiries:
        key = f"{expiry:%Y-%m-%d}:{(expiry - AS_OF).days}"
        out[key] = {
            f"{strike:.1f}": [
                _contract(
                    option_type=option_type,
                    strike=strike,
                    expiry=expiry,
                    # Skew: puts richer than calls, more so further downside.
                    iv_pct=iv_pct
                    + (4.0 if option_type == "PUT" else 0.0)
                    + max(0.0, (SPOT - strike) / SPOT * 20.0),
                    exp_type=exp_type,
                )
            ]
            for strike in STRIKES
        }
    return out


# Term structure in contango: front 25%, rising to 30% at the back. Includes a
# weekly, a quarter-end and a MONTH-END so the D22 filter has traps to reject.
_EXPIRIES = [
    (MONTHLIES[0], 25.0, "S"),
    (WEEKLY, 24.0, "W"),
    (MONTH_END, 26.0, "M"),  # the 'M' trap: NOT a standard monthly
    (MONTHLIES[1], 27.0, "S"),
    (QUARTER_END, 28.0, "Q"),
    (MONTHLIES[2], 29.0, "S"),
    (MONTHLIES[3], 30.0, "S"),
    (FIFTH_MONTHLY, 31.0, "S"),  # one monthly too far
]

CHAIN = {
    "symbol": "TEST",
    "status": "SUCCESS",
    "underlyingPrice": SPOT,
    # Chain-level fields the live payload carries. isChainTruncated and
    # isDelayed are refused by the parser; the rest are shape fidelity.
    "assetMainType": "EQUITY",
    "assetSubType": "ETF",
    "isChainTruncated": False,
    "isDelayed": False,
    "isIndex": False,
    "hasBinaryOptions": False,
    "ethOptionEligible": False,
    "numberOfContracts": len(STRIKES) * len(_EXPIRIES) * 2,
    "strategy": "SINGLE",
    "interval": 0.0,
    "daysToExpiration": 0.0,
    "interestRate": 3.772,
    "dividendYield": 0.995,
    "volatility": 29.0,
    "underlying": {"symbol": "TEST", "last": SPOT, "mark": SPOT},
    "callExpDateMap": _expiry_map("CALL", _EXPIRIES),
    "putExpDateMap": _expiry_map("PUT", _EXPIRIES),
}


def truncated_chain() -> dict:
    """Schwab flagging a partial response. Must be refused, not stored (D7)."""
    return {**CHAIN, "isChainTruncated": True}


def delayed_chain() -> dict:
    """Delayed quotes. A snapshot built from them is not the same observation."""
    return {**CHAIN, "isDelayed": True}


def chain_without_expiration_types() -> dict:
    """`expirationType` stripped, to exercise the third-Friday fallback."""
    payload = copy.deepcopy(CHAIN)
    for side in ("callExpDateMap", "putExpDateMap"):
        for strike_map in payload[side].values():
            for contracts in strike_map.values():
                for contract in contracts:
                    contract.pop("expirationType", None)
    return payload


def chain_with_sentinels() -> dict:
    """Schwab sends -999.0 where a greek or IV is unavailable."""
    payload = copy.deepcopy(CHAIN)
    key = f"{MONTHLIES[0]:%Y-%m-%d}:{(MONTHLIES[0] - AS_OF).days}"
    for contracts in payload["callExpDateMap"][key].values():
        for contract in contracts:
            contract["volatility"] = -999.0
            contract["delta"] = -999.0
    return payload


def chain_with_nonstandard_contracts() -> dict:
    """Contracts adjusted after a corporate action — odd deliverables, and IV
    that is not comparable with the rest of the surface."""
    payload = copy.deepcopy(CHAIN)
    key = f"{MONTHLIES[0]:%Y-%m-%d}:{(MONTHLIES[0] - AS_OF).days}"
    for contracts in payload["callExpDateMap"][key].values():
        for contract in contracts:
            contract["nonStandard"] = True
    return payload


def chain_with_mini_contracts() -> dict:
    payload = copy.deepcopy(CHAIN)
    key = f"{MONTHLIES[1]:%Y-%m-%d}:{(MONTHLIES[1] - AS_OF).days}"
    for contracts in payload["putExpDateMap"][key].values():
        for contract in contracts:
            contract["mini"] = True
    return payload


# GET /marketdata/v1/pricehistory
def price_history(days: int = 5, *, start: date = date(2026, 10, 13)) -> dict:
    candles = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        # Schwab stamps a daily candle at midnight ET; that is 04:00 UTC in EDT.
        epoch_ms = int(datetime(day.year, day.month, day.day, 4, 0, tzinfo=UTC).timestamp() * 1000)
        base = 100.0 + offset
        candles.append(
            {
                "open": base,
                "high": base + 1.5,
                "low": base - 1.0,
                "close": base + 0.5,
                "volume": 1_000_000 + offset * 10_000,
                "datetime": epoch_ms,
            }
        )
    return {"symbol": "TEST", "empty": False, "candles": candles}


EMPTY_PRICE_HISTORY = {"symbol": "TEST", "empty": True, "candles": []}
