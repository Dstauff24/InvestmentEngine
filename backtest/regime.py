"""What the market was doing when a trade was called (D39(4)).

Expectancy without a regime tag is an average over conditions that will never
recur in the same mixture. A pullback recipe that makes 0.4R overall might make
1.1R in an uptrend and lose in a downtrend, and the blended number would
recommend running it in both.

The tags come from D17's own risk-off definition, because the engine already
makes a decision on it: **SPY below its 200-day MA** plus **VIX above 25**. VIX
has no provider in the stack, so the volatility axis is SPY's own realised
volatility against its history — a proxy, named as one. The trend axis is
exactly D17's.

Everything is keyed on the BENCHMARK's bars, which is why `tickers.is_benchmark`
had to exist first: the D14 universe is index constituents and contains no
market series at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import pandas as pd

from core import config
from features import technical as technical_features

log = logging.getLogger(__name__)

__all__ = ["RegimeSeries", "load_benchmark", "regime_series"]

BENCHMARK = "SPY"

# Volatility terciles over the window being tested, not absolute thresholds: a
# "high volatility" number chosen in advance would classify a whole decade as
# calm or stormy depending on when it was picked.
VOL_WINDOW = 20
VOL_QUANTILES = (1 / 3.0, 2 / 3.0)


@dataclass(frozen=True)
class RegimeSeries:
    """One tag per session, plus the counts behind them."""

    tags: dict[date, str]
    benchmark: str
    ma_length: int

    def tag(self, day: date) -> str:
        """The tag for a session, or `unknown` before the MA has history.

        `unknown` rather than a guess: the first 200 sessions of any series
        have no 200-day MA, and labelling them "uptrend" because price is
        rising would be inventing the one fact the tag exists to carry.
        """
        return self.tags.get(day, "unknown")

    def histogram(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for value in self.tags.values():
            out[value] = out.get(value, 0) + 1
        return dict(sorted(out.items()))


def load_benchmark(*, as_of: date, symbol: str = BENCHMARK) -> pd.DataFrame:
    """The benchmark's SPLIT-ADJUSTED closes, through the same door as a symbol.

    It read `prices_daily` directly, which is RAW (D24). SPY has not split
    inside the stored window, so this was latent rather than wrong — and
    latent is exactly how the mixed-basis defect reached production elsewhere.
    A split in the benchmark would put a one-day gap of several hundred percent
    into the realised-volatility terciles and step the 200-day MA, and the
    regime tag that came out is STAMPED on every call (D45) and never
    recomputed, so the error would outlive the data that caused it.
    """
    bars = technical_features.load_bars(symbol, as_of=as_of, lookback_days=100_000)
    if bars.empty:
        return pd.DataFrame(columns=["session_date", "close"])
    frame = technical_features.on_adjusted_basis(bars)[["session_date", "close"]].copy()
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    return frame.dropna(subset=["close"]).reset_index(drop=True)


def regime_series(bars: pd.DataFrame, *, symbol: str = BENCHMARK, cfg=None) -> RegimeSeries:
    """Tag every session as `<trend>_<volatility>`.

    Trend is D17's test exactly: price against the `risk_off_spy_below_ma` MA,
    which is 200. Volatility is the tercile of trailing realised volatility
    WITHIN this window, so the labels mean "calm for this period" rather than
    "calm by some number chosen in 2026".
    """
    targets = (cfg or config.risk()).allocation_targets
    ma_length = int(targets.risk_off_spy_below_ma)
    if bars.empty or len(bars) <= ma_length:
        log.warning(
            "benchmark %s has %d bar(s), under the %d-day MA: every session is `unknown`",
            symbol,
            len(bars),
            ma_length,
        )
        return RegimeSeries(tags={}, benchmark=symbol, ma_length=ma_length)

    closes = bars["close"]
    ma = closes.rolling(ma_length).mean()
    # Realised volatility, annualised only for legibility — the tercile cut is
    # scale-free, so the constant changes nothing but the number's units.
    returns = closes.pct_change()
    vol = returns.rolling(VOL_WINDOW).std() * (252**0.5)
    low, high = vol.quantile(VOL_QUANTILES[0]), vol.quantile(VOL_QUANTILES[1])

    tags: dict[date, str] = {}
    for day, price, average, volatility in zip(bars["session_date"], closes, ma, vol, strict=True):
        if pd.isna(average) or pd.isna(volatility):
            continue
        trend = "bull" if price >= average else "bear"
        if volatility <= low:
            band = "calm"
        elif volatility >= high:
            band = "volatile"
        else:
            band = "normal"
        tags[day] = f"{trend}_{band}"
    return RegimeSeries(tags=tags, benchmark=symbol, ma_length=ma_length)
