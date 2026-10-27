"""Turning stored bars into the cross-sections stage 1 measures. READ-ONLY.

Two jobs, both point-in-time by construction:

**FORWARD RETURNS, IN SESSIONS AND ON A TOTAL-RETURN BASIS.** Sessions rather
than calendar days, because "the next 10 trading days" is a fixed amount of
market and "the next 10 calendar days" is not (D42's `candidate_returns` makes
the same choice). Total return rather than price return, because D19 says every
measurement here is total-return and a dividend-paying symbol would otherwise
look worse than it was. The horizon is forward, so the last N sessions of the
window have no answer and are DROPPED rather than filled.

**MEASURABLES, COMPUTED ROLLING AND VECTORISED.** The backtest recomputes
`technical.compute` per symbol per session, which is correct and costs an hour;
stage 1 needs the same numbers over more days, so these are rolling pandas
computations over one bar frame per symbol. A rolling window cannot see the
future, which is what keeps them point-in-time — but it also means they must
MATCH the engine's definitions, or the lab would measure a different signal
from the one the recipes read. Each one names the engine function it mirrors
and `tests/test_lab_providers.py` pins the agreement on real-shaped fixtures.

**THE COMPOSITE IS ABSENT ON PURPOSE.** D39(4): the estimate-revision, IV-rank,
insider and news series start 2026-10-21, so a historical composite is a
two-pillar number wearing a four-pillar name. Its two hypotheses accrue FORWARD
through `daily_candidates` instead, and the registry says so.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date

import numpy as np
import pandas as pd

from backtest.engine import build_sector_indices
from backtest.regime import load_benchmark, regime_series
from core import db
from features import technical as technical_features
from lab.cross_section import Observation

log = logging.getLogger(__name__)

BENCHMARK = "SPY"
# Postgres NUMERIC arrives as `decimal.Decimal`, which will not do arithmetic
# with a float — `_clean` casts in the engine, and the lab has to as well or
# the first forward return raises. Cast ONCE per frame rather than per column
# per measurable.
NUMERIC = ("open", "high", "low", "close", "volume")
UNIVERSE_SQL = "SELECT symbol, sector FROM v_active_universe ORDER BY symbol"


def forward_returns(closes: pd.Series, horizons: Sequence[int]) -> dict[int, pd.Series]:
    """{horizon: percent return from t to t+horizon SESSIONS}.

    `shift(-h)` is the whole point: the value at t describes what happened
    AFTER t, and the last h rows become NaN and are dropped by the caller.
    """
    return {h: (closes.shift(-h) / closes - 1.0) * 100.0 for h in horizons}


# ── the measurables, each mirroring an engine definition ────────────────────


def rsi_14(closes: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI at every session, mirroring `technical.rsi_wilder` EXACTLY.

    **SEEDED WITH THE SIMPLE MEAN OF THE FIRST `period` CHANGES, then smoothed**
    — which is what the engine does and what a charting package shows. A plain
    `ewm(alpha=1/period)` seeds on the first observation instead and disagrees
    by a point or two for the first hundred bars; the recipe's 40-50 band is
    four points wide, so that is the difference between firing and not.
    """
    delta = closes.diff()
    gains = delta.clip(lower=0.0).to_numpy()
    losses = (-delta).clip(lower=0.0).to_numpy()
    out = [float("nan")] * len(closes)
    if len(closes) <= period:
        return pd.Series(out, index=closes.index)

    avg_gain = float(pd.Series(gains[1 : period + 1]).mean())
    avg_loss = float(pd.Series(losses[1 : period + 1]).mean())

    def value(gain: float, loss: float) -> float:
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        rs = gain / loss
        return 100.0 - (100.0 / (1.0 + rs))

    out[period] = value(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i] = value(avg_gain, avg_loss)
    return pd.Series(out, index=closes.index)


def relative_volume_20d(volume: pd.Series, window: int = 20) -> pd.Series:
    """Today's volume over its trailing average, EXCLUDING today.

    Mirrors the engine: a baseline that included today would dampen exactly the
    spike the signal is about.
    """
    baseline = volume.shift(1).rolling(window).mean()
    return volume / baseline


def relative_strength_20d(closes: pd.Series, sector: pd.Series, window: int = 20) -> pd.Series:
    """The symbol's trailing return MINUS its sector's, in points.

    A DIFFERENCE, not a ratio (D33): a ratio is unstable exactly in the quiet
    sector case that matters most.
    """
    own = closes.pct_change(window) * 100.0
    index = sector.reindex(closes.index).pct_change(window) * 100.0
    return own - index


def squeeze_percentile(bars: pd.DataFrame, window: int = 10, history: int = 252) -> pd.Series:
    """Where the recent range sits in the symbol's own trailing year.

    Mirrors `features.technical.squeeze_percentile` (O13's replacement for the
    retired contraction ratio): a LEVEL against the symbol's own history,
    because any ratio of two adjacent windows of one series centres on 1.00.
    """
    tr = pd.concat(
        [
            bars["high"] - bars["low"],
            (bars["high"] - bars["close"].shift()).abs(),
            (bars["low"] - bars["close"].shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    normalised = (tr / bars["close"]).rolling(window).mean()
    # MID-RANK within the trailing `history` readings, matching the engine's
    # `(below + 0.5 * tied) / len(reference)` — counting `<=` would put a tape
    # whose volatility never changes at 1.00, "a one-year high" for a flat
    # series.
    ranked = normalised.rolling(history).rank(pct=True, method="average")
    # **THE SHIFT IS `exclude_today`.** The engine drops the last bar BEFORE
    # computing anything, because the coil is a PRE-breakout condition and the
    # break bar is the release of it — so its reading at session t describes the
    # window ending at t-1. Without this the two disagree wildly rather than
    # subtly: 0.49 against 0.89 on the same fixture, since the break bar's own
    # range expansion lands in the window that is supposed to exclude it.
    # pandas' average rank includes the current value and so sits 0.5/history
    # (~0.002) above the engine, which the test allows for.
    return ranked.shift(1)


def rolling_beta(closes: pd.Series, market: pd.Series, window: int = 252) -> pd.Series:
    """Trailing beta against the market, usable ON the date it is indexed at.

    **SHIFTED BY ONE SESSION, which is the point-in-time part.** A rolling
    window ending TODAY includes today's return, and today's return is inside
    the same session the cross-section is formed on — using it would let the
    adjustment know something about the day it is adjusting. The shift costs one
    session of beta and buys the claim that nothing here saw the future.

    `cov / var` of DAILY simple returns, the market model. None (NaN) while the
    history is short, never 1.0: a default of 1.0 is a guess that happens to
    look neutral, and it would quietly turn a beta adjustment into the SPY
    shift it exists to improve on.
    """
    own = closes.pct_change()
    mkt = market.reindex(closes.index).pct_change()
    beta = own.rolling(window).cov(mkt) / mkt.rolling(window).var()
    return beta.shift(1)


def momentum_12_1(closes: pd.Series, long_window: int = 252, skip: int = 21) -> pd.Series:
    """12-month return EXCLUDING the most recent month, in percent.

    **THE SKIPPED MONTH IS THE POINT, not a detail.** Short-term reversal runs
    the other way over the last few weeks and cancels much of the momentum
    premium, which is why the standard construction drops it. Measuring 12-0
    instead and finding nothing would be a result about two signals fighting
    rather than about momentum.

    Mirrors nothing in `features/` — the engine has no long-horizon momentum
    measure at all, which is why this is registered as a control as much as a
    hypothesis.
    """
    return (closes.shift(skip) / closes.shift(long_window) - 1.0) * 100.0


def volatility_60d(closes: pd.Series, window: int = 60) -> pd.Series:
    """Annualised realised volatility of daily log returns, in percent.

    Annualised with sqrt(252) to match `backtest/regime.py`'s own volatility
    terciles, so a reading here and a regime tag there mean the same thing.
    """
    returns = np.log(closes / closes.shift(1))
    return returns.rolling(window).std() * (252**0.5) * 100.0


MEASURABLES = {
    # One signature for every provider — (bars, sector_index) — so the caller
    # needs no per-measurable knowledge. Most ignore the sector series; naming
    # the parameter `_sector` says that is deliberate rather than forgotten.
    "rsi_14": lambda bars, _sector: rsi_14(bars["close"]),
    "relative_volume_20d": lambda bars, _sector: relative_volume_20d(bars["volume"]),
    "relative_strength_20d": lambda bars, sector: relative_strength_20d(bars["close"], sector),
    "squeeze_percentile": lambda bars, _sector: squeeze_percentile(bars),
    "momentum_12_1": lambda bars, _sector: momentum_12_1(bars["close"]),
    "volatility_60d": lambda bars, _sector: volatility_60d(bars["close"]),
}
"""What can be measured from stored bars alone. The registry holds more; a
measurable with no provider here is reported UNMEASURED rather than skipped,
so the registry and the report cannot disagree about what was tried."""


def universe() -> list[tuple[str, str | None]]:
    return [(r["symbol"], r["sector"]) for r in db.fetch_all(UNIVERSE_SQL)]


def cross_sections(
    measurables: Sequence[str],
    *,
    as_of: date,
    horizons: Sequence[int] = (5, 10, 20),
    symbols: Sequence[str] | None = None,
) -> tuple[dict[str, dict[date, list[Observation]]], dict[date, str]]:
    """({measurable: {day: observations}}, {day: regime}) over all history.

    **EVERY MEASURABLE IN ONE PASS PER SYMBOL.** Bars are the expensive part —
    one query and one adjustment per symbol — so loading them once and
    computing four measurables off the same frame costs a quarter of what four
    separate sweeps would. The benchmark and sector series are loaded once and
    shared, because they are facts about the market rather than about the
    symbol.
    """
    rows = universe()
    if symbols is not None:
        keep = set(symbols)
        rows = [(s, sec) for s, sec in rows if s in keep]
    sectors = {sec for _, sec in rows if sec}
    indices = build_sector_indices(sectors, as_of=as_of)

    spy = technical_features.load_bars(BENCHMARK, as_of=as_of, lookback_days=100_000)
    spy_closes = (
        spy.set_index("session_date")["close"].astype(float)
        if not spy.empty
        else pd.Series(dtype=float)
    )
    spy_forward = (
        forward_returns(spy.set_index("session_date")["close"].astype(float), horizons)
        if not spy.empty
        else {h: pd.Series(dtype=float) for h in horizons}
    )
    regimes = regime_series(load_benchmark(as_of=as_of)).tags

    out: dict[str, dict[date, list[Observation]]] = {name: {} for name in measurables}
    seen_days: set[date] = set()
    for n, (symbol, sector) in enumerate(rows, start=1):
        bars = technical_features.load_bars(symbol, as_of=as_of, lookback_days=100_000)
        if bars.empty or len(bars) < 300:
            continue
        frame = bars.set_index("session_date")
        frame[list(NUMERIC)] = frame[list(NUMERIC)].astype(float)
        index = indices.get(sector or "", pd.Series(dtype=float)).astype(float)
        own_forward = forward_returns(frame["close"], horizons)
        sector_forward = forward_returns(index.reindex(frame.index), horizons)
        betas = rolling_beta(frame["close"], spy_closes)

        for name in measurables:
            try:
                values = MEASURABLES[name](frame, index)
            except KeyError:
                raise KeyError(f"no provider for measurable {name!r}") from None
            for day, value in values.dropna().items():
                forward = {
                    h: float(own_forward[h].get(day))
                    for h in horizons
                    if pd.notna(own_forward[h].get(day))
                }
                if not forward:
                    continue
                out[name].setdefault(day, []).append(
                    Observation(
                        on=day,
                        symbol=symbol,
                        value=float(value),
                        forward=forward,
                        benchmark={
                            h: float(spy_forward[h].get(day))
                            for h in horizons
                            if pd.notna(spy_forward[h].get(day))
                        },
                        sector={
                            h: float(sector_forward[h].get(day))
                            for h in horizons
                            if pd.notna(sector_forward[h].get(day))
                        },
                        beta=(float(betas.get(day)) if pd.notna(betas.get(day)) else None),
                    )
                )
                seen_days.add(day)
        if n % 50 == 0:
            log.info("measured %d/%d symbols", n, len(rows))
    return out, {day: regimes.get(day, "unknown") for day in seen_days}
