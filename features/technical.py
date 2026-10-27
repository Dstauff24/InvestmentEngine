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
from features.adjustment import adjust_bars, basis_breaks, drop_pre_break

log = logging.getLogger(__name__)

# Trading days, not calendar days: these windows index bars.
TRADING_DAYS_PER_YEAR = 252

__all__ = [
    "PriceStructure",
    "TechnicalFeatures",
    "atr",
    "compute",
    "fifty_two_week_structure",
    "load_actions",
    "load_bars",
    "ma_stack",
    "on_adjusted_basis",
    "price_structure",
    "relative_strength_vs_sector",
    "relative_volume",
    "rsi_wilder",
    "squeeze_percentile",
]


class InsufficientHistory(ValueError):
    """Raised only where a caller explicitly asks for strictness."""


def on_adjusted_basis(bars: pd.DataFrame) -> pd.DataFrame:
    """The frame with `adj_*` promoted over the raw OHLCV columns.

    **ONE BASIS PER FRAME, OR THE MEASUREMENTS DISAGREE WITH EACH OTHER.**
    `features.adjustment.adjust_bars` ADDS `adj_open`/`adj_high`/`adj_low`/
    `adj_close`/`adj_volume` and PRESERVES the raw columns (D24 keeps
    `prices_daily` raw and adjusts at read time). `compute` used to take
    `adj_close` for the closes while handing the same frame to
    `price_structure` and `atr`, which read `high`/`low`/`close` — so for any
    symbol with a split inside the window, the levels and the risk unit were on
    the OLD basis while the close was on the new one.

    It is not a rounding difference. Measured live 2026-10-23 on AMCR, whose
    1-for-5 REVERSE split scales its earlier adjusted closes UP by five: the
    adjusted close sat 4.3x above a `base_high` read from the raw column, so
    `base_breakout`'s "close is 0.5% above the base high" trigger was satisfied
    on 806 consecutive sessions. Those were the only firings that recipe ever
    recorded. A forward split fails the other way and more quietly — the raw
    highs are on the pre-split basis, the base's depth reads tens of percent,
    and the symbol is dropped for "no base" with nothing in the log to say the
    reason was arithmetic.

    ATR is the part that reaches furthest: D17 sizes every position off it, so
    a split inside the 14-bar period moved the stop distance on a real plan.
    """
    if bars is None or not len(bars):
        return bars
    promotions = {
        column: f"adj_{column}"
        for column in ("open", "high", "low", "close", "volume")
        if f"adj_{column}" in bars.columns
    }
    if not promotions:
        return bars
    frame = bars.copy()
    for column, adjusted in promotions.items():
        frame[column] = frame[adjusted]
    return frame


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
    # Read the tail out of pandas ONCE. `.iloc[i]` inside the loop costs ~5us a
    # call, which on a 400-bar window is most of this function's runtime and
    # most of a 400-symbol scoring pass. The arithmetic, its order and its
    # results are untouched — tests/test_technical.py pins the values.
    gain_values = gains.to_numpy(dtype=float)
    loss_values = losses.to_numpy(dtype=float)
    for i in range(period, len(delta)):
        avg_gain = (avg_gain * (period - 1) + gain_values[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss_values[i]) / period

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
    # Out of pandas once; see the note in `rsi_wilder`. Same arithmetic, same
    # order, same result.
    span_values = spans.to_numpy(dtype=float)
    for i in range(period, len(spans)):
        value = (value * (period - 1) + span_values[i]) / period
    return value


def atr_pct(bars: pd.DataFrame, period: int = 14) -> float | None:
    """ATR as a percentage of the last close, for comparing across symbols."""
    value = atr(bars, period)
    closes = _clean(bars["close"]) if "close" in bars else pd.Series(dtype=float)
    if value is None or not len(closes):
        return None
    last = float(closes.iloc[-1])
    return None if last == 0 else value / last * 100.0


SQUEEZE_WINDOW = 10
SQUEEZE_HISTORY = TRADING_DAYS_PER_YEAR


def squeeze_percentile(
    bars: pd.DataFrame,
    *,
    window: int = SQUEEZE_WINDOW,
    history: int = SQUEEZE_HISTORY,
    exclude_today: bool = True,
) -> float | None:
    """Where current volatility sits in the symbol's OWN year: 0.0 to 1.0.

    The `window`-session mean true range, normalised by close, ranked against
    the last `history` readings of that same statistic. 0.12 means "quieter
    than 88% of the past year"; 0.90 means the opposite. This is O13's
    replacement for `base_contraction`, and the three choices in it are the
    whole point:

    **A LEVEL, NOT A RATIO OF TWO WINDOWS.** The retired statistic divided the
    last fifth of the base by the whole base, which is ~1.00 BY CONSTRUCTION,
    and measuring the last quarter against the prior quarter instead does not
    fix it: both are ratios of adjacent samples of the same series, and the
    measured median of that alternative is 1.013 (against the retired one's
    1.004). A gate on either is a quantile of noise with no cross-sectional
    meaning — "13% quieter than its own recent past" says nothing about whether
    the stock is actually coiled. A percentile of the symbol's own history
    does, and its scale is self-interpreting: a cap of 0.25 admits roughly the
    quietest quarter of observations, so the gate's selectivity is legible
    rather than accidental. It is also measured INDEPENDENTLY of the base
    detector, which is what lets D43's sweep read its range off the live
    distribution instead of around the shipped value.

    **NORMALISED BY CLOSE.** A dollar range grows with price, so an unnormalised
    series would read a stock that doubled over the reference year as
    permanently expanding. TR/close compares 2024 with 2026.

    **THE BREAK BAR IS EXCLUDED** (`exclude_today`), for the same reason
    `relative_volume` excludes today from its own baseline: the coil is a
    PRE-BREAKOUT condition, and the breakout bar is the release of it. Letting
    that bar into the window would have the very range expansion the recipe is
    buying disqualify the setup that produced it.

    None when there is not enough history, per the standing convention: a
    percentile computed against 30 observations is a plausible-looking number
    for a question that needs a year.
    """
    frame = bars.dropna(subset=["high", "low", "close"]) if len(bars) else bars
    if frame is None or not len(frame):
        return None
    if exclude_today:
        frame = frame.iloc[:-1]
    if len(frame) < history + window:
        return None

    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    closes = frame["close"].astype(float)
    prev_close = closes.shift(1)
    spans = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    normalised = (spans / closes).replace([float("inf"), float("-inf")], pd.NA).dropna()

    rolling = normalised.rolling(window).mean().dropna()
    if len(rolling) < history:
        return None
    reference = rolling.iloc[-history:]
    current = float(rolling.iloc[-1])
    # MID-RANK, so ties do not read as extremes. The current reading is a
    # member of its own reference window, and counting `<=` would put a series
    # whose volatility never changes at 1.00 — "a one-year high" for a flat
    # tape. Averaging the ties puts it at 0.50, which is what mid-range means.
    below = float((reference < current).sum())
    tied = float((reference == current).sum())
    return (below + 0.5 * tied) / float(len(reference))


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


# ── price structure: the levels a trade plan is built from ──────────────────
#
# These live HERE rather than in the recipes because they are MEASUREMENTS. A
# recipe reads features and scores and does not re-measure — otherwise the
# same swing high gets computed three slightly different ways and the stop a
# card prints stops matching the stop the backtest used.


@dataclass(frozen=True)
class PriceStructure:
    """Levels a stop and a target can be hung on.

    Every field defaults to None so an empty frame yields `PriceStructure()`.
    The first version built it with `*([None] * 9)`, which broke the moment a
    tenth field was added — a positional splat over a dataclass is a
    field-count assertion nobody remembers making.
    """

    swing_high: float | None = None
    """Highest high in the lookback — the level a pullback would reclaim."""
    swing_low: float | None = None
    """Lowest low over the SAME long lookback. On a trending stock this is far
    below price, which is why `pullback_low` exists beside it."""
    pullback_low: float | None = None
    """Lowest low over a SHORT recent window — the low the pullback itself
    made. `swing_low` is the 60-day low, which for an uptrend that pulled back
    three days ago is a level from a different episode entirely."""
    base_high: float | None = None
    base_low: float | None = None
    base_depth_pct: float | None = None
    """Depth of the detected CONSOLIDATION, as a % of its high."""
    base_weeks: float | None = None
    gap_up_pct: float | None = None
    """Most recent session's gap vs the prior close, in %."""
    gap_day_low: float | None = None
    """The low of the gap session. A fill below it invalidates the drift."""
    prior_close: float | None = None


def price_structure(
    bars: pd.DataFrame,
    *,
    swing_lookback: int = 60,
    pullback_lookback: int = 10,
    base_lookback: int = 120,
    max_base_depth_pct: float = 25.0,
    min_base_sessions: int = 20,
) -> PriceStructure:
    """Structural levels from the bar history. None where there is too little.

    Two lookbacks on purpose: a swing is recent (the pullback a recipe is
    buying) while a base is the longer consolidation a breakout emerges from,
    and forcing one window on both would make a 4-week base and a 3-day
    pullback the same measurement.

    A BASE IS A CONSOLIDATION, NOT A WINDOW. The first version took the
    high-low range of a fixed 120 sessions and called it the base, which for a
    trending stock is the TREND: live on 2026-10-21 that reported bases 30-48%
    deep and recipe 2's 25% cap rejected almost everything for the right-
    sounding reason. `_detect_base` instead finds the longest RECENT stretch
    whose range fits inside the depth cap, which is what "multi-week base"
    means.
    """
    frame = bars.dropna(subset=["high", "low", "close"]) if len(bars) else bars
    if frame is None or not len(frame):
        return PriceStructure()

    highs = frame["high"].astype(float)
    lows = frame["low"].astype(float)
    closes = frame["close"].astype(float)

    swing = highs.iloc[-swing_lookback:] if len(highs) >= 2 else highs
    swing_lows = lows.iloc[-swing_lookback:] if len(lows) >= 2 else lows
    # THE PULLBACK'S OWN LOW, over a short window. None rather than a shorter
    # window's low when the history does not cover it: a "pullback low" from
    # three bars would be today's low under another name.
    recent_lows = lows.iloc[-pullback_lookback:] if len(lows) >= pullback_lookback else None

    # THE BASE IS WHAT PRICE DID BEFORE TODAY. `_detect_base` walks backward
    # from the last bar it is given, so handing it today's bar makes
    # `base_high >= today's high >= today's close` and the breakout trigger
    # `close >= base_high * (1 + buffer)` ARITHMETICALLY UNREACHABLE. Measured
    # 2026-10-23 over 2,810 base sessions on a clean basis: not one satisfied
    # it, and the largest close/base_high ratio seen was 1.00000. A breakout is
    # a statement about clearing the range that EXISTED, so the range is
    # measured through the previous close and today is the break.
    highs_before_today = highs.iloc[:-1]
    lows_before_today = lows.iloc[:-1]
    base_high, base_low, depth, weeks = _detect_base(
        highs_before_today,
        lows_before_today,
        max_depth_pct=max_base_depth_pct,
        min_sessions=min_base_sessions,
        max_sessions=base_lookback,
    )

    gap_pct = gap_low = prior = None
    if len(frame) >= 2:
        prior = float(closes.iloc[-2])
        today_open = float(frame["open"].astype(float).iloc[-1]) if "open" in frame else None
        if today_open is not None and prior:
            gap_pct = (today_open - prior) / prior * 100.0
        gap_low = float(lows.iloc[-1])

    return PriceStructure(
        swing_high=float(swing.max()) if len(swing) else None,
        swing_low=float(swing_lows.min()) if len(swing_lows) else None,
        pullback_low=None if recent_lows is None else float(recent_lows.min()),
        base_high=base_high,
        base_low=base_low,
        base_depth_pct=depth,
        base_weeks=weeks,
        gap_up_pct=gap_pct,
        gap_day_low=gap_low,
        prior_close=prior,
    )


def _detect_base(
    highs: pd.Series,
    lows: pd.Series,
    *,
    max_depth_pct: float,
    min_sessions: int,
    max_sessions: int,
) -> tuple[float | None, float | None, float | None, float | None]:
    """The longest recent stretch whose range fits inside `max_depth_pct`.

    Walks BACKWARD from the LAST BAR IT IS GIVEN, extending while the running
    range stays within the cap, and stops at the first bar that would breach
    it. That is the consolidation a breakout emerges from — as opposed to a
    fixed window, which on a trending stock measures the trend and then fails
    its own depth test.

    The caller decides where "recent" ends, and `price_structure` ends it at
    the PREVIOUS close: a base that includes today cannot be broken today.

    Returns (high, low, depth_pct, weeks), all None when even `min_sessions`
    cannot fit inside the cap — which is the correct answer for a stock in a
    steep trend: it has no base.

    It no longer returns a contraction ratio. That statistic (the last fifth of
    the base over the whole base) was ~1.00 by construction and is retired with
    O13; tightness is now `squeeze_percentile`, measured against the symbol's
    own year rather than inside this window.
    """
    if len(highs) < min_sessions:
        return (None,) * 4

    best: tuple[int, float, float] | None = None
    running_high = running_low = None
    for count in range(1, min(len(highs), max_sessions) + 1):
        high = float(highs.iloc[-count])
        low = float(lows.iloc[-count])
        running_high = high if running_high is None else max(running_high, high)
        running_low = low if running_low is None else min(running_low, low)
        if running_high <= 0:
            break
        depth = (running_high - running_low) / running_high * 100.0
        if depth > max_depth_pct:
            break
        if count >= min_sessions:
            best = (count, running_high, running_low)

    if best is None:
        return (None,) * 4

    count, high, low = best
    depth = (high - low) / high * 100.0
    # Five sessions a week. Calendar weeks would overstate a base that spans a
    # holiday-shortened stretch.
    weeks = count / 5.0

    return high, low, depth, weeks


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


def sector_composite(
    frames: dict[str, pd.DataFrame],
    *,
    column: str = "adj_close",
    min_member_fraction: float = 0.8,
    min_members: int = 3,
) -> pd.Series:
    """An equal-weighted sector index with PER-DATE membership.

    Equal-weighted, not cap-weighted: the question is "is this sector working",
    and a cap-weighted composite of a sector with one mega-cap is mostly a
    chart of that one name, against which every other member looks weak.

    PER-DATE MEMBERSHIP, which is the whole design. Each date averages the
    members that HAVE data on it, and a date counts only when at least
    `min_member_fraction` of members are present.

    The first version intersected dates across all members, so one short
    history truncated the index for everybody: SPCX, listed June 2026, cut the
    live Industrials index from 278 bars to 91 while every other sector got
    278. Dropping short members was a patch that fixed the symptom — the index
    still ended at the shortest SURVIVING member — and it threw away the
    newcomer's data entirely once it did have history.

    Averaging per date keeps the full span AND uses every member on every date
    it exists. The fraction floor is what stops the early part of the window
    becoming a one-stock index: on a date where only 3 of 40 members had
    listed, the "sector" would be those three.

    Each member is normalised to 100 at its OWN first in-window date before
    averaging. Without that a newcomer entering at a raw price of 5 against
    members at 300 would drag the mean on the day it joins, producing a step
    change in the index that is pure membership rather than price.
    """
    series: dict[str, pd.Series] = {}
    for symbol, frame in frames.items():
        if column not in frame or "session_date" not in frame:
            log.warning("sector composite: skipping %s, missing %s", symbol, column)
            continue
        indexed = frame.set_index(_to_dates(frame["session_date"]))[column].astype(float)
        indexed = indexed[~indexed.index.duplicated(keep="last")].dropna()
        if indexed.empty or indexed.iloc[0] == 0:
            continue
        # Normalised per member, so joining the index is not a price event.
        series[symbol] = indexed / indexed.iloc[0] * 100.0

    if len(series) < min_members:
        return pd.Series(dtype=float)

    wide = pd.DataFrame(series).sort_index()
    present = wide.notna().sum(axis=1)
    required = max(min_members, int(len(series) * min_member_fraction))
    eligible = wide[present >= required]
    if eligible.empty:
        return pd.Series(dtype=float)

    # mean(axis=1) skips NaN, so each date averages exactly the members that
    # had data on it — the per-date membership the docstring promises.
    return eligible.mean(axis=1).sort_index()


# ── the bundle ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EarningsReaction:
    """The market's response to a report, measured WITHOUT a release time.

    **WHY NOT JUST THE GAP.** A single-bar gap is only the reaction if you know
    when the news came out. For a report dated T, a pre-open release gaps on T
    and an after-close release gaps on T+1 — and no FMP endpoint publishes the
    release time (verified 2026-10-22). Reading the gap on T would credit an
    after-close surprise to a bar that closed before it existed; reading it on
    T+1 would miss every pre-open report entirely.

    So the reaction is measured over a WINDOW that contains the gap either way:
    from the close BEFORE the report through the close of the session after it.
    Measuring to the window's CLOSE is also what makes the "hold" real — a gap
    that filled intraday is not a reaction that survived, and it reports as a
    small number rather than needing a separate test.

    When the release time IS known the window tightens to exactly the sessions
    the news could have moved:

      * `bmo` — out before T's open, so the reaction is complete at T's close
        and a plan may enter at T+1. One session, exact reference.
      * `amc` — out after T's close, so T's own bar is pre-news and close(T) is
        the exact reference. The window ends at T+1 as in the agnostic case.
      * `unknown` / `dmt` — the agnostic window: reference close(T-1), through
        close(T+1), entry at T+2. One session of extra noise bought in
        exchange for never acting on a number that was not public.

    The provider date disagreement makes this more than a nicety: measured
    2026-10-22, FMP and Finnhub date the same report ONE DAY apart for a large
    fraction of symbols (ADI 11-23 vs 11-24, CRM 12-01 vs 12-02, CIEN 12-09 vs
    12-10). The agnostic window spans that error; a single-bar gap would fall
    on the wrong side of it.
    """

    reported_on: date
    release_time: str
    reference_close: float
    """The last close BEFORE the news could have been public."""
    end_session: date
    """The session whose close completes the window."""
    sessions: int
    reaction_pct: float
    reaction_points: float
    window_low: float
    """The lowest low inside the window — where the reaction is invalidated."""
    complete: bool
    """True only when `end_session` is the LAST bar available: the recipe may
    judge a reaction exactly once, at the close that completes it."""

    @property
    def summary(self) -> str:
        return (
            f"{self.reaction_pct:+.1f}% over {self.sessions} session(s) from "
            f"{self.reference_close:.2f} (report {self.reported_on}, "
            f"release {self.release_time})"
        )


# `dmt` (during market hours) behaves like an unknown time: the news lands with
# the session half traded, so the reaction spans the rest of T and into T+1.
AGNOSTIC_TIMES = frozenset({"unknown", "dmt", ""})

# How far the window may stretch from the report date before it stops being a
# reaction. T+1 is normally one to four calendar days away; a long weekend plus
# a holiday reaches five or six. Beyond that the symbol did not trade — a halt,
# a suspension, a gap in the series — and the "reaction" would be a week of
# unrelated news measured against a stale reference. A HARD SANITY BOUND, not a
# strategy knob: it replaces the `max_days_since_earnings` gate D49 retired,
# which incidentally caught this case for the wrong reason.
MAX_WINDOW_SPAN_DAYS = 8


def earnings_reaction(
    bars: pd.DataFrame, *, reported_on: date, release_time: str = "unknown"
) -> EarningsReaction | None:
    """Measure the reaction window for one report. None when it cannot be.

    None rather than zeros: before the window is in the data there is no
    reaction to report, and a 0.0% reading would be indistinguishable from a
    report the market ignored.
    """
    if bars is None or len(bars) < 3 or "session_date" not in bars:
        return None
    column = "adj_close" if "adj_close" in bars else "close"
    low_column = "adj_low" if "adj_low" in bars else "low"
    sessions = _to_dates(bars["session_date"])
    closes = pd.to_numeric(bars[column], errors="coerce")
    lows = pd.to_numeric(bars[low_column], errors="coerce")

    # The first session on or after the report date: a provider can date a
    # report on a weekend, and the reaction belongs to the next session.
    matches = [i for i, day in enumerate(sessions) if day >= reported_on]
    if not matches:
        return None
    t_index = matches[0]

    time = (release_time or "unknown").lower()
    if time == "bmo":
        reference_index, end_index = t_index - 1, t_index
    elif time == "amc":
        reference_index, end_index = t_index, t_index + 1
    elif time in AGNOSTIC_TIMES:
        reference_index, end_index = t_index - 1, t_index + 1
    else:
        return None

    if reference_index < 0 or end_index >= len(bars):
        return None
    reference = closes.iloc[reference_index]
    end_close = closes.iloc[end_index]
    if pd.isna(reference) or pd.isna(end_close) or reference == 0:
        return None

    if (sessions[end_index] - reported_on).days > MAX_WINDOW_SPAN_DAYS:
        # The symbol did not trade through the window. Measuring a reaction
        # across a halt would compare a post-resumption price to a reference
        # from before a week of unrelated news.
        return None

    window = slice(reference_index + 1, end_index + 1)
    window_lows = lows.iloc[window].dropna()
    if window_lows.empty:
        return None

    return EarningsReaction(
        reported_on=reported_on,
        release_time=time,
        reference_close=float(reference),
        end_session=sessions[end_index],
        sessions=end_index - reference_index,
        reaction_pct=float((end_close - reference) / reference * 100.0),
        reaction_points=float(end_close - reference),
        window_low=float(window_lows.min()),
        complete=end_index == len(bars) - 1,
    )


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
    squeeze_percentile: float | None
    """Volatility against the symbol's own year (O13). Low = coiled."""
    week52: WeekStructure
    rs_vs_sector: float | None
    structure: PriceStructure
    bars_available: int
    reaction: EarningsReaction | None = None
    """The post-earnings reaction window, when a knowable report anchors one."""

    def as_dict(self) -> dict:
        """Flat mapping, for logging and for the eventual `features` writer."""
        out = asdict(self)
        out["ma"] = asdict(self.ma)
        out["week52"] = asdict(self.week52)
        out["structure"] = asdict(self.structure)
        out["reaction"] = asdict(self.reaction) if self.reaction else None
        return out


def compute(
    symbol: str,
    bars: pd.DataFrame,
    *,
    sector_closes: pd.Series | None = None,
    cfg: config.PullbackRecipe | None = None,
    structure_cfg: config.BreakoutRecipe | None = None,
    geometry_cfg: config.PullbackGeometry | None = None,
    reported_on: date | None = None,
    release_time: str = "unknown",
) -> TechnicalFeatures:
    """Measure one symbol from one frame of bars. Pure: no DB, no network.

    Window lengths come from `config/scoring.yaml`'s pullback recipe, which is
    where D25 says numbers live — the MA triple and the RSI period are shared
    across every technical reading, not private to that recipe.

    `reported_on` / `release_time` anchor the post-earnings reaction window.
    They are passed IN rather than read from the database because this function
    is pure, and because the caller already has the one report whose actual is
    knowable — deciding that twice is how the beat and the price reaction end
    up describing different quarters.

    `geometry_cfg` carries the two windows the STRUCTURE is measured over —
    the swing lookback (#71) and the pullback-low lookback (#82) — for the same
    reason `structure_cfg` exists: they are read by the measurement, so a sweep
    over them has to recompute the features.

    `structure_cfg` exists for D43's sweep. `min_base_weeks` and
    `max_base_depth_pct` are read by the MEASUREMENT as well as by the gate, so
    sweeping them means recomputing the features — passing them in is what lets
    a sweep do that without mutating global config under a running process.
    """
    recipe = cfg or config.scoring().recipes.pullback_to_trend
    breakout = structure_cfg or config.scoring().recipes.base_breakout
    geometry = geometry_cfg or config.scoring().geometry.pullback_to_trend
    _require_ascending(bars)

    # EVERY reading below comes off ONE basis. See `on_adjusted_basis`: taking
    # the closes from `adj_close` while the structure and the ATR read the raw
    # columns is how a reverse split became 806 breakouts.
    frame = on_adjusted_basis(bars)
    closes = frame["close"] if "close" in frame else pd.Series(dtype=float)
    volumes = frame.get("volume", pd.Series(dtype=float))

    clean = _clean(closes)
    return TechnicalFeatures(
        symbol=symbol,
        session_date=_last_session_date(bars),
        close=float(clean.iloc[-1]) if len(clean) else None,
        ma=ma_stack(closes, fast=recipe.ma_fast, mid=recipe.ma_mid, slow=recipe.ma_slow),
        rsi=rsi_wilder(closes, recipe.rsi_period),
        atr=atr(frame),
        atr_pct=atr_pct(frame),
        relative_volume=relative_volume(volumes),
        squeeze_percentile=squeeze_percentile(frame),
        week52=fifty_two_week_structure(closes),
        # The base cap and minimum come from the breakout recipe's own config
        # (D25), so a change there moves the measurement rather than leaving
        # this function's defaults silently disagreeing with the gate.
        structure=price_structure(
            frame,
            # #71 WAS CONFIGURED AND NEVER READ. `price_structure` used its own
            # default of 60 while `geometry.pullback_to_trend
            # .swing_lookback_days` said 60 too, so the duplication was
            # invisible: changing the config moved nothing. A key that loads
            # while nothing reads it is how an operator comes to believe a
            # setting is in force when it is not.
            swing_lookback=geometry.swing_lookback_days,
            pullback_lookback=geometry.pullback_low_lookback_days,
            max_base_depth_pct=breakout.max_base_depth_pct,
            min_base_sessions=int(breakout.min_base_weeks * 5),
        ),
        rs_vs_sector=(
            None if sector_closes is None else relative_strength_vs_sector(closes, sector_closes)
        ),
        bars_available=len(bars),
        # Anchored on the last KNOWABLE report (see `catalyst
        # .last_knowable_earnings`), so the reaction and the beat always
        # describe the same quarter.
        reaction=(
            None
            if reported_on is None
            else earnings_reaction(frame, reported_on=reported_on, release_time=release_time)
        ),
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
    bars = pd.DataFrame(rows)
    # A RECORDED BASIS BREAK IS APPLIED BEFORE THE LOOKBACK IS CUT, so a symbol
    # whose early history is refused gets a SHORT frame rather than a long one
    # padded with bars on an unjoinable basis. Everything that reads history
    # comes through here — the lab, `backtest.engine`, `backtest.regime` — which
    # is the only reason one lookup can cover them all (O19).
    bars = drop_pre_break(bars, _basis_break(symbol))
    if bars.empty:
        return pd.DataFrame(columns=["session_date", "open", "high", "low", "close", "volume"])
    bars = bars.tail(lookback_days).reset_index(drop=True)
    actions = load_actions(symbol, as_of=as_of)
    # SPLIT-ONLY: see the module docstring. Dividend-adjusted MAs do not match
    # the chart the human is looking at.
    return adjust_bars(bars, actions, mode="split_only", as_of=as_of, strict=False)


_BREAKS: dict[str, date] | None = None


def _basis_break(symbol: str) -> date | None:
    """The symbol's basis break, from a table read once per process.

    Cached like the sector composites in `scoring/runner.py` and for the same
    reason: a walk calls `load_bars` once per symbol per session, and the table
    holds a handful of rows that only change when an operator records one.
    """
    global _BREAKS
    if _BREAKS is None:
        _BREAKS = basis_breaks()
    return _BREAKS.get(symbol)


def reset_basis_break_cache() -> None:
    """Forget the cached table. For tests and for a long-lived process."""
    global _BREAKS
    _BREAKS = None


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
