"""Aggregate volatility metrics from a day's option-chain snapshot (D22).

D22 stores "per-ticker aggregate IV" alongside the chain. This is the maths that
produces it: pure functions over a list of contract rows, no database, no
network, so every one is testable against a fixture chain.

Two conventions everything here depends on:

  * IV is a DECIMAL FRACTION (0.285), not a percent. `ingest/schwab_chains.py`
    divides Schwab's percent by 100 at the boundary so nothing downstream has to
    remember which unit it is holding.
  * **"ATM" means the strike NEAREST the underlying price**, not a delta-0.50
    contract. Nearest-strike is stable across days; delta-0.50 drifts with skew
    and would put a spurious wobble into a series whose whole purpose is
    day-over-day comparison (D7).

IV rank and percentile stay None until `option_chains.iv_rank_min_history_days`
of history exists (D7). That is a normal state for the first quarter, not an
error.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

# The horizon iv30 interpolates to, and the delta wings skew is read at.
TARGET_DAYS = 30
SKEW_DELTA = 0.25


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result


def _dte(expiration: date, as_of: date) -> int:
    return (expiration - as_of).days


def expirations(contracts: Sequence[dict], as_of: date) -> list[date]:
    """Distinct expirations still alive at `as_of`, nearest first."""
    return sorted({c["expiration"] for c in contracts if _dte(c["expiration"], as_of) >= 0})


def atm_strike(contracts: Sequence[dict], spot: float, expiration: date) -> float | None:
    """The listed strike closest to spot within one expiration."""
    strikes = {
        _f(c["strike"]) for c in contracts if c["expiration"] == expiration and _f(c["strike"])
    }
    strikes.discard(None)
    return min(strikes, key=lambda s: abs(s - spot)) if strikes else None


def atm_iv(
    contracts: Sequence[dict], spot: float, expiration: date
) -> tuple[float | None, float | None, float | None]:
    """(blended, call, put) ATM implied volatility for one expiration.

    Blending call and put rather than picking one: at the same strike they
    should agree by put-call parity, and averaging cancels a stale quote on one
    side. Falls back to whichever side has a number when only one does.
    """
    strike = atm_strike(contracts, spot, expiration)
    if strike is None:
        return None, None, None

    call_ivs, put_ivs = [], []
    for contract in contracts:
        if contract["expiration"] != expiration or _f(contract["strike"]) != strike:
            continue
        vol = _f(contract["implied_volatility"])
        if vol is None or vol <= 0:
            continue
        (call_ivs if contract["option_type"] == "C" else put_ivs).append(vol)

    call = sum(call_ivs) / len(call_ivs) if call_ivs else None
    put = sum(put_ivs) / len(put_ivs) if put_ivs else None
    both = [v for v in (call, put) if v is not None]
    blended = sum(both) / len(both) if both else None
    return blended, call, put


def interpolate_iv30(points: list[tuple[int, float]]) -> float | None:
    """Linear-in-days interpolation of ATM IV to a constant 30-day horizon.

    `points` is [(days_to_expiration, atm_iv), ...]. A constant-maturity number
    is what makes IV comparable across days: without it, IV drifts simply
    because the front month got a week closer, and an IV-rank series built on
    that is measuring the calendar rather than the market.

    **Extrapolation is deliberately refused** — the nearest available point is
    returned instead of a straight-line guess past the ends of the curve.
    """
    usable = sorted((d, v) for d, v in points if d is not None and v is not None and v > 0)
    if not usable:
        return None
    if len(usable) == 1:
        return usable[0][1]

    below = [p for p in usable if p[0] <= TARGET_DAYS]
    above = [p for p in usable if p[0] >= TARGET_DAYS]
    if not below:
        return above[0][1]
    if not above:
        return below[-1][1]

    (d0, v0), (d1, v1) = below[-1], above[0]
    if d0 == d1:
        return v0
    weight = (TARGET_DAYS - d0) / (d1 - d0)
    return v0 + weight * (v1 - v0)


def skew_25_delta(contracts: Sequence[dict], expiration: date) -> float | None:
    """25-delta put IV minus 25-delta call IV, for one expiration.

    Positive means downside protection is bid up relative to upside — the normal
    equity state. A collapse toward zero or negative is the contrarian flag.
    Contracts are matched by NEAREST absolute delta to 0.25 rather than
    requiring an exact hit, because listed strikes rarely land on a round delta.
    """
    calls, puts = [], []
    for contract in contracts:
        if contract["expiration"] != expiration:
            continue
        delta, vol = _f(contract["delta"]), _f(contract["implied_volatility"])
        if delta is None or vol is None or vol <= 0:
            continue
        (calls if contract["option_type"] == "C" else puts).append((abs(delta), vol))

    if not calls or not puts:
        return None
    call_iv = min(calls, key=lambda p: abs(p[0] - SKEW_DELTA))[1]
    put_iv = min(puts, key=lambda p: abs(p[0] - SKEW_DELTA))[1]
    return put_iv - call_iv


def expected_move_pct(contracts: Sequence[dict], spot: float, expiration: date) -> float | None:
    """ATM straddle price as a fraction of spot — the market's implied move.

    This answers "is the option premium worth it?" for an event inside the
    expiration, and it is what the event-radar edge leans on.
    """
    strike = atm_strike(contracts, spot, expiration)
    if strike is None or spot <= 0:
        return None

    prices: dict[str, float] = {}
    for contract in contracts:
        if contract["expiration"] != expiration or _f(contract["strike"]) != strike:
            continue
        price = _f(contract["mark"])
        if price is None:
            bid, ask = _f(contract["bid"]), _f(contract["ask"])
            price = (bid + ask) / 2.0 if bid is not None and ask is not None else None
        if price is not None and price >= 0:
            prices[contract["option_type"]] = price

    if "C" not in prices or "P" not in prices:
        return None
    return (prices["C"] + prices["P"]) / spot


def iv_rank(current: float | None, history: Sequence[float]) -> float | None:
    """Where `current` sits in the high-low range of `history`, as 0-1.

    The classic IV rank. Returns None on a flat history (high == low): every
    value would read "100%", which is a division artefact, not a signal.

    GATED HARD on history length by `summarize`, and `iv_daily.history_days`
    records how much there was. That gate is right HERE and wrong for
    estimate-revision momentum, which is computed from two snapshots and
    reported with its depth — a rank needs a high and a low to sit between, so
    on a short history the formula has no answer, whereas a revision between
    two snapshots is a correct measurement of a real change that is merely a
    noisy estimate of a trend. `features/evidence.py` sets out the distinction.
    """
    if current is None or not history:
        return None
    low, high = min(history), max(history)
    if high <= low:
        return None
    return max(0.0, min(1.0, (current - low) / (high - low)))


def iv_percentile(current: float | None, history: Sequence[float]) -> float | None:
    """Fraction of `history` strictly below `current`.

    Robust in a way iv_rank is not: one panic spike permanently flattens a rank
    series, while a percentile shrugs it off.
    """
    if current is None or not history:
        return None
    return sum(1 for value in history if value < current) / len(history)


def summarize(
    contracts: Sequence[dict],
    *,
    as_of: date,
    min_history_days: int,
    history: Sequence[float] = (),
) -> dict | None:
    """Reduce one symbol's stored chain rows to a single `iv_daily` row.

    Returns None when there is nothing usable to store — better an absent row
    than a row of nulls that looks like a real observation of nothing.
    """
    if not contracts:
        return None
    spot = next((_f(c["underlying_price"]) for c in contracts if _f(c["underlying_price"])), None)
    if spot is None or spot <= 0:
        return None

    expiries = expirations(contracts, as_of)
    if not expiries:
        return None

    front = expiries[0]
    blended_front, call_front, put_front = atm_iv(contracts, spot, front)

    points = []
    for expiration in expiries:
        blended, _, _ = atm_iv(contracts, spot, expiration)
        if blended is not None:
            points.append((_dte(expiration, as_of), blended))
    iv30 = interpolate_iv30(points)

    term_slope = points[-1][1] - points[0][1] if len(points) >= 2 else None

    open_interest = sum(int(c["open_interest"] or 0) for c in contracts)
    volume = sum(int(c["volume"] or 0) for c in contracts)
    call_oi = sum(int(c["open_interest"] or 0) for c in contracts if c["option_type"] == "C")
    put_oi = open_interest - call_oi

    # D7: rank needs its own accumulated history before it means anything.
    ranked = len(history) >= min_history_days
    return {
        "underlying_price": spot,
        "iv30": iv30,
        "iv_atm_front": blended_front,
        "iv_call_atm": call_front,
        "iv_put_atm": put_front,
        "skew_25d": skew_25_delta(contracts, front),
        "term_slope": term_slope,
        "expected_move_front_pct": expected_move_pct(contracts, spot, front),
        "iv_rank_252": iv_rank(iv30, history) if ranked else None,
        "iv_percentile_252": iv_percentile(iv30, history) if ranked else None,
        "history_days": len(history),
        "total_open_interest": open_interest,
        "total_option_volume": volume,
        "put_call_oi_ratio": (put_oi / call_oi) if call_oi else None,
    }
