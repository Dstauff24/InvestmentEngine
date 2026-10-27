"""The daily cross-section: quintiles, spread, rank IC, and honest t-stats.

One measurable, one day, every symbol that has a value: sort, cut into
quintiles, and ask what the top fifth earned against the bottom fifth over the
next 5, 10 and 20 SESSIONS. Then average those daily answers across the window
and ask whether the average is distinguishable from zero.

**THE T-STAT IS COMPUTED ON THE DAILY SERIES, NOT ON THE POOLED TRADES**, and
that is the only statistically load-bearing choice in this module. Pooling
every symbol-day treats 400 symbols on one day as 400 independent
observations, when a single market move drives all of them — the standard way
a cross-sectional study invents significance. The daily spread series is one
number per day, so its t-stat answers "did this work on enough different days",
which is the question. Overlapping windows still correlate (a 20-session
forward return on consecutive days shares 19 sessions), so the t-stat carries a
NEWEY-WEST correction with a lag equal to the horizon and the module reports
both the naive and the corrected value — the gap between them is itself worth
seeing.

**EXCESS RETURNS, THREE WAYS.** Raw, excess versus SPY (the market), and excess
versus the symbol's own sector composite (D33's series, already built for
relative strength). A signal that looks strong raw and vanishes against its
sector was a sector bet; one that survives both is a claim about the symbol.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

QUINTILES = 5
# A day needs enough symbols for quintiles to mean anything. Ten per bucket is
# the floor: below that one symbol moves the bucket's mean by a tenth of its
# own return, and the spread stops describing the signal.
MIN_SYMBOLS_PER_DAY = QUINTILES * 10


@dataclass(frozen=True, slots=True)
class Observation:
    """One symbol on one day: the signal's value and what happened next.

    **`slots=True` IS A CAPACITY DECISION, NOT A MICRO-OPTIMISATION.** The deep
    window (O19) holds ~1.9M symbol-days and one of these per measurable, so a
    per-instance `__dict__` costs over a gigabyte on its own — the first deep
    run was killed by the kernel partway through loading. The three return
    dicts below are also SHARED rather than copied: see `providers.cross_sections`.
    """

    on: date
    symbol: str
    value: float
    forward: dict[int, float]
    """{horizon in sessions: return in percent}. Missing horizons are absent,
    never zero — a symbol delisted inside the window has no 20-session return
    and inventing one is how survivorship gets built in twice."""
    benchmark: dict[int, float] = field(default_factory=dict)
    """SPY's return over the same horizons."""
    sector: dict[int, float] = field(default_factory=dict)
    """The symbol's sector composite over the same horizons."""
    beta: float | None = None
    """Trailing 252-session beta against SPY, estimated from data STRICTLY
    BEFORE this date. None while the history is too short — never 1.0, which
    would be a guess that happens to look neutral."""

    def excess(self, horizon: int, basis: str) -> float | None:
        """The return on one of four bases, or None when it cannot be formed.

        **`spy` AND `beta` ARE NOT THE SAME CORRECTION, and the difference is
        the whole reason the second exists.** Subtracting SPY's return shifts
        every symbol by the SAME number, so it cannot change the ordering and a
        top-minus-bottom spread is identical to raw (measured: identical on all
        twelve pairs). Beta VARIES BY SYMBOL, so subtracting `beta x market`
        reorders the cross-section — which is what separates a factor from
        plain market exposure. A spread that survives raw and collapses
        beta-adjusted was high-beta leadership in a rising market wearing a
        factor's name.
        """
        own = self.forward.get(horizon)
        if own is None:
            return None
        if basis == "raw":
            return own
        if basis == "beta":
            market = self.benchmark.get(horizon)
            if market is None or self.beta is None:
                return None
            return own - self.beta * market
        against = (self.benchmark if basis == "spy" else self.sector).get(horizon)
        return None if against is None else own - against


@dataclass(frozen=True)
class DailySpread:
    """One day's top-minus-bottom spread and rank correlation."""

    on: date
    symbols: int
    spread: float
    top: float
    bottom: float
    rank_ic: float


@dataclass(frozen=True)
class Result:
    """What the window says about one measurable at one horizon and basis."""

    measurable: str
    horizon: int
    basis: str
    days: int
    symbol_days: int
    mean_spread: float | None
    t_stat: float | None
    t_stat_newey_west: float | None
    mean_rank_ic: float | None
    ic_t_stat: float | None
    quintile_means: tuple[float, ...] = ()
    symbols_min: int = 0
    symbols_median: int = 0
    symbols_max: int = 0
    """**HOW MANY SYMBOLS STOOD BEHIND THIS NUMBER, per day.** Required on every
    deep-window result (O22): the universe is TODAY's constituent list, so the
    further back a date is, the more of the market that existed then is missing
    — and what is missing is not random, it is the companies that failed. A
    spread measured on 180 survivors of 2009 and one measured on 430 symbols of
    2025 are different claims, and the count is the only thing on the report
    that says so."""
    first_day: date | None = None
    last_day: date | None = None

    @property
    def label(self) -> str:
        return f"{self.measurable} / {self.horizon}d / {self.basis}"

    @property
    def monotone(self) -> bool:
        """Do the quintile means increase (or decrease) all the way through?

        A monotone ladder is weak evidence that the VALUE matters rather than
        one extreme bucket; it is reported and never used as a criterion,
        because with five buckets monotonicity happens by chance often enough.
        """
        if len(self.quintile_means) < QUINTILES:
            return False
        rising = all(
            b >= a for a, b in zip(self.quintile_means, self.quintile_means[1:], strict=False)
        )
        falling = all(
            b <= a for a, b in zip(self.quintile_means, self.quintile_means[1:], strict=False)
        )
        return rising or falling


def _rank(values: Sequence[float]) -> list[float]:
    """Average ranks, so ties do not fabricate an ordering."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Rank correlation. None when either side has no spread to correlate."""
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    rx, ry = _rank(xs), _rank(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return None if dx == 0 or dy == 0 else num / (dx * dy)


def quintile_buckets(values: Sequence[float]) -> list[int]:
    """Which quintile each value falls in, 0 (lowest) to 4 (highest).

    Cut by POSITION rather than by value, so an ordinary skewed measurable
    still fills its buckets — a value-based cut on the squeeze percentile would
    have put most symbols in one bucket.

    **TIES SHARE A BUCKET, AND A HEAVILY TIED DAY IS THEREFORE UNCUTTABLE.**
    Ranks are averaged, so forty symbols holding the same value all land in one
    quintile and the others come back empty; `daily_spread` then finds no top
    or no bottom and SKIPS the day. That is the honest outcome: inside a block
    of identical values there is no ordering, and breaking the tie by position
    would assign identical symbols to different quintiles and call the
    difference signal. A measurable that is mostly one value is not a
    cross-sectional signal, and the skipped-day count is what says so.
    """
    ranks = _rank(values)
    n = len(values)
    return [min(QUINTILES - 1, int((r - 1) * QUINTILES / n)) for r in ranks]


def daily_spread(
    observations: Sequence[Observation], *, horizon: int, basis: str
) -> DailySpread | None:
    """One day's quintile spread, or None when the day is too thin to cut."""
    usable = [(o, o.excess(horizon, basis)) for o in observations]
    usable = [(o, r) for o, r in usable if r is not None]
    if len(usable) < MIN_SYMBOLS_PER_DAY:
        return None
    values = [o.value for o, _ in usable]
    returns = [r for _, r in usable]
    buckets = quintile_buckets(values)
    top = [r for r, b in zip(returns, buckets, strict=True) if b == QUINTILES - 1]
    bottom = [r for r, b in zip(returns, buckets, strict=True) if b == 0]
    if not top or not bottom:
        return None
    ic = spearman(values, returns)
    return DailySpread(
        on=usable[0][0].on,
        symbols=len(usable),
        spread=statistics.fmean(top) - statistics.fmean(bottom),
        top=statistics.fmean(top),
        bottom=statistics.fmean(bottom),
        rank_ic=0.0 if ic is None else ic,
    )


def _t_stat(series: Sequence[float]) -> float | None:
    """None rather than a number when the t-stat is not defined.

    Fewer than three days is not a series. ZERO VARIANCE is the stranger case:
    a spread identical on every day has an infinite t-stat in the limit, and
    `None` reads as "unmeasured" rather than "perfect" — accepted deliberately,
    because real daily spreads never have zero variance and a module that
    returned `inf` would put it into a report as though it were measured.
    """
    if len(series) < 3:
        return None
    mean = statistics.fmean(series)
    sd = statistics.stdev(series)
    return None if sd == 0 else mean / (sd / math.sqrt(len(series)))


def newey_west_t(series: Sequence[float], lag: int) -> float | None:
    """A t-stat that does not pretend overlapping windows are independent.

    A 20-session forward return measured on consecutive days shares 19 of its
    20 sessions with yesterday's, so the naive standard error is too small and
    the t-stat too large — the most common way a signal study overstates its
    own confidence. Bartlett weights, lag = the horizon.
    """
    n = len(series)
    if n < 3:
        return None
    mean = statistics.fmean(series)
    deviations = [x - mean for x in series]
    gamma0 = sum(d * d for d in deviations) / n
    variance = gamma0
    for k in range(1, min(lag, n - 1) + 1):
        gamma = sum(deviations[t] * deviations[t - k] for t in range(k, n)) / n
        variance += 2 * (1 - k / (lag + 1)) * gamma
    if variance <= 0:
        return None
    return mean / math.sqrt(variance / n)


def measure(
    by_day: dict[date, Sequence[Observation]],
    *,
    measurable: str,
    horizon: int,
    basis: str = "raw",
) -> Result:
    """Average the daily answers across the window and test them against zero."""
    spreads = [daily_spread(obs, horizon=horizon, basis=basis) for obs in by_day.values()]
    kept = [s for s in spreads if s is not None]
    series = [s.spread for s in kept]
    ics = [s.rank_ic for s in kept]

    buckets: list[list[float]] = [[] for _ in range(QUINTILES)]
    for obs in by_day.values():
        usable = [(o, o.excess(horizon, basis)) for o in obs]
        usable = [(o, r) for o, r in usable if r is not None]
        if len(usable) < MIN_SYMBOLS_PER_DAY:
            continue
        assignment = quintile_buckets([o.value for o, _ in usable])
        for (_, r), b in zip(usable, assignment, strict=True):
            buckets[b].append(r)

    counts = sorted(s.symbols for s in kept)
    return Result(
        measurable=measurable,
        horizon=horizon,
        basis=basis,
        days=len(kept),
        symbol_days=sum(counts),
        symbols_min=counts[0] if counts else 0,
        symbols_median=counts[len(counts) // 2] if counts else 0,
        symbols_max=counts[-1] if counts else 0,
        first_day=min((s.on for s in kept), default=None),
        last_day=max((s.on for s in kept), default=None),
        mean_spread=statistics.fmean(series) if series else None,
        t_stat=_t_stat(series),
        t_stat_newey_west=newey_west_t(series, horizon),
        mean_rank_ic=statistics.fmean(ics) if ics else None,
        ic_t_stat=_t_stat(ics),
        quintile_means=tuple(statistics.fmean(b) if b else 0.0 for b in buckets),
    )
