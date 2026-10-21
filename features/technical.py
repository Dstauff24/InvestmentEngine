"""Pillar A inputs: price and volume structure (D2/D32 weight 40).

Nothing here scores anything. These are the measurements `scoring/pillars.py`
will read — kept separate so the arithmetic can be tested against hand-checkable
fixtures, with no database and no network, which is the only way a number this
load-bearing stays trustworthy.

Three conventions worth stating once:

**Adjusted prices, SPLIT-ONLY mode (D24).** Technical structure is about where
price sits relative to its own history, and a split is a change of units, not a
move. Dividends are NOT adjusted out here: a 200-day moving average computed on
total-return prices drifts above the chart the human is looking at, and an MA
nobody can see on their own screen is useless for an entry decision. The
`total_return` mode exists for performance measurement, which is a different
question.

**Insufficient history yields None, never a number.** Every function returns
`None` when it has fewer bars than it needs. The alternative — a 200-day MA
computed from 40 bars — is a plausible-looking number that is simply wrong, and
it would propagate into a composite with no way to tell. A new listing scores
nothing until it has history, and that is correct.

**Newest bar last.** Every function takes bars in ascending `session_date`
order and reads the final row as "today". `load_bars` guarantees it; a caller
assembling frames by hand must too, and `_require_ascending` fails loudly
rather than silently measuring the past.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date
from itertools import pairwise

import pandas as pd

from core import config, db
from features.adjustment import adjust_bars

log = logging.getLogger(__name__)

# Trading days, not calendar days: these windows index bars.
TRADING_DAYS_PER_YEAR = 252

__all__ = [
    "TechnicalFeatures",
    "atr",
    "compute",
    "fifty_two_week_structure",
    "load_actions",
    "load_bars",
    "ma_stack",
    "relative_strength_vs_sector",
    "relative_volume",
    "rsi_wilder",
]


class InsufficientHistory(ValueError):
    """Raised only where a caller explicitly asks for strictness."""


# ── moving averages ─────────────────────────────────────────────────────────


def sma(closes: pd.Series, window: int) -> float | None:
    """Simple moving average of the last `window` closes, or None."""
    values = _clean(closes)
    if len(values) < window:
        return None
    return float(values.iloc[-window:].mean())


def _slope_is_rising(closes: pd.Series, window: int, *, lookback: int) -> bool | None:
    """Is the `window`-day SMA higher than it was `lookback` bars ago?

    Measured on the MA rather than on price, because "the 50 is rising" is a
    statement about the average and a single strong day can lift price while
    the average still falls.
    """
    values = _clean(closes)
    if len(values) < window + lookback:
        return None
    now = float(values.iloc[-window:].mean())
    then = float(values.iloc[-window - lookback : -lookback].mean())
    return now > then


@dataclass(frozen=True)
class MAStack:
    fast: float | None
    mid: float | None
    slow: float | None
    stacked: bool | None
    """fast > mid > slow — the D-recipe-1 uptrend condition."""
    fast_rising: bool | None
    mid_rising: bool | None
    price_above_fast: bool | None
    price_above_slow: bool | None
    pct_from_fast: float | None
    """How far today's close sits above (+) or below (-) the fast MA, in %."""


def ma_stack(
    closes: pd.Series,
    *,
    fast: int,
    mid: int,
    slow: int,
    rising_lookback: int = 5,
) -> MAStack:
    """The moving-average picture in one object.

    `stacked` is None rather than False when the slow MA is unavailable: "we
    cannot see whether this is an uptrend" and "this is not an uptrend" are
    different answers, and a recipe gating on an uptrend must not treat a young
    listing as a confirmed downtrend.
    """
    values = _clean(closes)
    f, m, s = sma(values, fast), sma(values, mid), sma(values, slow)
    last = float(values.iloc[-1]) if len(values) else None

    stacked = None if None in (f, m, s) else (f > m > s)
    return MAStack(
        fast=f,
        mid=m,
        slow=s,
        stacked=stacked,
        fast_rising=_slope_is_rising(values, fast, lookback=rising_lookback),
        mid_rising=_slope_is_rising(values, mid, lookback=rising_lookback),
        price_above_fast=None if None in (last, f) else last > f,
        price_above_slow=None if None in (last, s) else last > s,
        pct_from_fast=None if None in (last, f) or not f else (last - f) / f * 100.0,
    )


# ── RSI ─────────────────────────────────────────────────────────────────────


def rsi_wilder(closes: pd.Series, period: int = 14) -> float | None:
    """Wilder's RSI, the standard definition the recipe band assumes.

    WILDER smoothing, not a simple mean of gains and losses. The two disagree
    by several points on the same data, and recipe 1's 40-50 "reset" band was
    written against the figure a charting package shows — which is Wilder's.
    Using the simple variant would shift the band without anyone editing it.

    Needs `period + 1` closes to produce `period` changes.
    """
    values = _clean(closes)
    if len(values) < period + 1:
        return None

    delta = values.diff().dropna()
    gains = delta.clip(lower=0.0)
    losses = (-delta).clip(lower=0.0)

    # Seed with the simple mean of the first `period`, then smooth.
    avg_gain = float(gains.iloc[:period].mean())
    avg_loss = float(losses.iloc[:period].mean())
    for i in range(period, len(delta)):
        avg_gain = (avg_gain * (period - 1) + float(gains.iloc[i])) / period
        avg_loss = (avg_loss * (period - 1) + float(losses.iloc[i])) / period

    if avg_loss == 0.0:
        # No down-closes in the window. RSI is 100 by definition; returning it
        # rather than dividing by zero, and rather than None, because "nothing
        # fell" is a real and meaningful reading.
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ── ATR ─────────────────────────────────────────────────────────────────────


def atr(bars: pd.DataFrame, period: int = 14) -> float | None:
    """Average True Range, Wilder-smoothed. The stop-distance unit (D17).

    True range includes the gap: `max(high-low, |high-prev_close|,
    |low-prev_close|)`. Using high-low alone understates risk on exactly the
    names that gap, which are the ones a stop most needs to survive.
    """
    frame = bars.dropna(subset=["high", "low", "close"])
    if len(frame) < period + 1:
        return None

    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    prev_close = frame["close"].astype(float).shift(1)

    spans = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    spans = spans.dropna()
    if len(spans) < period:
        return None

    value = float(spans.iloc[:period].mean())
    for i in range(period, len(spans)):
        value = (value * (period - 1) + float(spans.iloc[i])) / period
    return value


def atr_pct(bars: pd.DataFrame, period: int = 14) -> float | None:
    """ATR as a percentage of the last close, for comparing across symbols."""
    value = atr(bars, period)
    closes = _clean(bars["close"]) if "close" in bars else pd.Series(dtype=float)
    if value is None or not len(closes):
        return None
    last = float(closes.iloc[-1])
    return None if last == 0 else value / last * 100.0


# ── volume ──────────────────────────────────────────────────────────────────


def relative_volume(volumes: pd.Series, *, window: int = 20) -> float | None:
    """Today's volume over the average of the PRIOR `window` days.

    The prior window excludes today on purpose. Including it dilutes the very
    spike being measured — on a 20-day window a true 3x day reads as about
    2.7x — and recipe 2 gates on >= 1.5x, so the error is large enough to
    change which breakouts qualify.
    """
    values = _clean(volumes)
    if len(values) < window + 1:
        return None
    baseline = float(values.iloc[-window - 1 : -1].mean())
    return None if baseline == 0 else float(values.iloc[-1]) / baseline


# ── 52-week structure ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class WeekStructure:
    high: float | None
    low: float | None
    pct_from_high: float | None
    """Negative: how far below the 52-week high today's close sits, in %."""
    pct_above_low: float | None
    range_position: float | None
    """0.0 at the 52-week low, 1.0 at the high. The usable single number."""
    is_new_high: bool | None
    bars_used: int


def fifty_two_week_structure(
    closes: pd.Series, *, window: int = TRADING_DAYS_PER_YEAR, min_bars: int = 126
) -> WeekStructure:
    """Where price sits in its own yearly range.

    `min_bars` defaults to about six months: less than that and "52-week high"
    describes a window the stock has not lived through, which would make every
    recent listing look like it was breaking out. Reported in `bars_used` so a
    caller can see the window was short.
    """
    values = _clean(closes)
    if len(values) < min_bars:
        return WeekStructure(None, None, None, None, None, None, len(values))

    window_values = values.iloc[-window:]
    high = float(window_values.max())
    low = float(window_values.min())
    last = float(values.iloc[-1])
    span = high - low

    return WeekStructure(
        high=high,
        low=low,
        pct_from_high=None if high == 0 else (last - high) / high * 100.0,
        pct_above_low=None if low == 0 else (last - low) / low * 100.0,
        range_position=None if span == 0 else (last - low) / span,
        is_new_high=last >= high,
        bars_used=len(window_values),
    )


# ── relative strength vs sector (D33) ───────────────────────────────────────


def relative_strength_vs_sector(
    closes: pd.Series, sector_closes: pd.Series, *, window: int = 63
) -> float | None:
    """Symbol return minus sector return over `window` bars, in percentage points.

    D33 makes this an explicit Pillar A input: an absolute 10% move means
    something different when the sector did 15% than when it did 2%. A simple
    return DIFFERENCE rather than a ratio, because a ratio is unstable when the
    sector return approaches zero and that is exactly the quiet-sector case
    where relative strength is most interesting.

    `window` defaults to 63 bars — about a quarter, long enough to mean
    something and short enough to still be about current leadership.

    Both series must cover the window; a sector composite that only starts
    halfway through would otherwise read as outperformance.
    """
    mine = _clean(closes)
    theirs = _clean(sector_closes)
    if len(mine) < window + 1 or len(theirs) < window + 1:
        return None

    def change(series: pd.Series) -> float | None:
        start = float(series.iloc[-window - 1])
        end = float(series.iloc[-1])
        return None if start == 0 else (end - start) / start * 100.0

    a, b = change(mine), change(theirs)
    return None if None in (a, b) else a - b


def sector_composite(frames: dict[str, pd.DataFrame], *, column: str = "adj_close") -> pd.Series:
    """An equal-weighted sector index from its members' bars.

    Equal-weighted, not cap-weighted: the question is "is this sector working",
    and a cap-weighted composite of a sector with one mega-cap is mostly a chart
    of that one name, against which every other member looks weak.

    Indexed by `session_date` and restricted to dates where EVERY member has a
    bar, so a member's missing day cannot move the composite on its own. Returns
    normalised levels (first date = 100).
    """
    series = []
    for symbol, frame in frames.items():
        if column not in frame or "session_date" not in frame:
            log.warning("sector composite: skipping %s, missing %s", symbol, column)
            continue
        indexed = frame.set_index(_to_dates(frame["session_date"]))[column].astype(float)
        series.append(indexed[~indexed.index.duplicated(keep="last")])

    if not series:
        return pd.Series(dtype=float)

    joined = pd.concat(series, axis=1, join="inner").dropna()
    if joined.empty:
        return pd.Series(dtype=float)

    normalised = joined.divide(joined.iloc[0], axis=1) * 100.0
    return normalised.mean(axis=1).sort_index()


# ── the bundle ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TechnicalFeatures:
    """Everything Pillar A reads for one symbol on one day."""

    symbol: str
    session_date: date
    close: float | None
    ma: MAStack
    rsi: float | None
    atr: float | None
    atr_pct: float | None
    relative_volume: float | None
    week52: WeekStructure
    rs_vs_sector: float | None
    bars_available: int

    def as_dict(self) -> dict:
        """Flat mapping, for logging and for the eventual `features` writer."""
        out = asdict(self)
        out["ma"] = asdict(self.ma)
        out["week52"] = asdict(self.week52)
        return out


def compute(
    symbol: str,
    bars: pd.DataFrame,
    *,
    sector_closes: pd.Series | None = None,
    cfg: config.PullbackRecipe | None = None,
) -> TechnicalFeatures:
    """Measure one symbol from one frame of bars. Pure: no DB, no network.

    Window lengths come from `config/scoring.yaml`'s pullback recipe, which is
    where D25 says numbers live — the MA triple and the RSI period are shared
    across every technical reading, not private to that recipe.
    """
    recipe = cfg or config.scoring().recipes.pullback_to_trend
    _require_ascending(bars)

    column = "adj_close" if "adj_close" in bars else "close"
    closes = bars[column] if column in bars else pd.Series(dtype=float)
    volumes = (
        bars["adj_volume"] if "adj_volume" in bars else bars.get("volume", pd.Series(dtype=float))
    )

    clean = _clean(closes)
    return TechnicalFeatures(
        symbol=symbol,
        session_date=_last_session_date(bars),
        close=float(clean.iloc[-1]) if len(clean) else None,
        ma=ma_stack(closes, fast=recipe.ma_fast, mid=recipe.ma_mid, slow=recipe.ma_slow),
        rsi=rsi_wilder(closes, recipe.rsi_period),
        atr=atr(bars),
        atr_pct=atr_pct(bars),
        relative_volume=relative_volume(volumes),
        week52=fifty_two_week_structure(closes),
        rs_vs_sector=(
            None if sector_closes is None else relative_strength_vs_sector(closes, sector_closes)
        ),
        bars_available=len(bars),
    )


# ── loading (the only part that touches the database) ───────────────────────


BARS_SQL = """
    SELECT session_date, open, high, low, close, volume
    FROM   prices_daily
    WHERE  symbol = %s AND session_date <= %s
    ORDER  BY session_date
"""

ACTIONS_SQL = """
    SELECT symbol, ex_date, action_type, cash_amount, split_ratio
    FROM   corporate_actions
    WHERE  symbol = %s AND ex_date <= %s
    ORDER  BY ex_date
"""


def load_bars(symbol: str, *, as_of: date, lookback_days: int = 400) -> pd.DataFrame:
    """Split-adjusted daily bars for one symbol, oldest first.

    `as_of` is honoured in BOTH the bar query and the adjustment factors (D24),
    so a backtest asking for 2024-03-01 gets the series as it stood then rather
    than today's view of it. That reproducibility is the whole reason
    adjustment happens at read time.
    """
    rows = db.fetch_all(BARS_SQL, (symbol, as_of))
    if not rows:
        return pd.DataFrame(columns=["session_date", "open", "high", "low", "close", "volume"])
    bars = pd.DataFrame(rows).tail(lookback_days).reset_index(drop=True)
    actions = load_actions(symbol, as_of=as_of)
    # SPLIT-ONLY: see the module docstring. Dividend-adjusted MAs do not match
    # the chart the human is looking at.
    return adjust_bars(bars, actions, mode="split_only", as_of=as_of, strict=False)


def load_actions(symbol: str, *, as_of: date) -> pd.DataFrame:
    rows = db.fetch_all(ACTIONS_SQL, (symbol, as_of))
    return pd.DataFrame(
        rows or [], columns=["symbol", "ex_date", "action_type", "cash_amount", "split_ratio"]
    )


def sector_members(sector: str, *, limit: int | None = None) -> list[str]:
    """Active symbols in one sector, for the composite."""
    sql = """
        SELECT symbol FROM tickers
        WHERE  is_active AND sector = %s AND passes_liquidity
        ORDER  BY avg_volume_20d DESC NULLS LAST
    """
    rows = db.fetch_all(sql, (sector,))
    symbols = [r["symbol"] for r in rows]
    return symbols[:limit] if limit else symbols


# ── internals ───────────────────────────────────────────────────────────────


def _clean(series: pd.Series) -> pd.Series:
    """Numeric, finite, NaN-free — in order, without reindexing."""
    if series is None or not len(series):
        return pd.Series(dtype=float)
    return pd.to_numeric(series, errors="coerce").dropna().astype(float)


def _to_dates(series: pd.Series) -> pd.Index:
    return pd.Index([_as_date(v) for v in series])


def _as_date(value: object) -> date:
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _last_session_date(bars: pd.DataFrame) -> date | None:
    if "session_date" not in bars or not len(bars):
        return None
    return _as_date(bars["session_date"].iloc[-1])


def _require_ascending(bars: pd.DataFrame) -> None:
    """Newest bar LAST. Fail loudly rather than measure the wrong end.

    A frame in descending order produces a complete set of plausible numbers
    describing a year ago, which no assertion downstream would catch.
    """
    if "session_date" not in bars or len(bars) < 2:
        return
    dates = [_as_date(v) for v in bars["session_date"]]
    if any(b < a for a, b in pairwise(dates)):
        raise ValueError(
            "bars must be in ascending session_date order (newest LAST); got a "
            "frame that decreases. Every reading here treats the final row as "
            "today, so a reversed frame measures the past and says nothing."
        )
