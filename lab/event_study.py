"""O18's second instrument: forward returns measured FROM AN EVENT.

**A DIFFERENT INSTRUMENT, NOT A DIFFERENT PROVIDER.** `lab/cross_section.py`
sorts every symbol every day; an event study looks only at the symbols
something happened to. The distinction is load-bearing rather than tidy: a
report lands on a few hundred symbols a quarter, so measuring "the earnings
surprise" across a 433-symbol daily cross-section would average one informative
observation against four hundred that carry no event at all, and a real effect
would come back null. That is why the registry carries a `shape` and refuses an
unknown one.

What it shares with the cross-section engine, deliberately: `Observation` and
its four return bases, `quintile_buckets`, `spearman`, and `newey_west_t`. Two
copies of a quintile cut would eventually disagree, and then neither number
could be defended.

**THE ONE HARD PROBLEM HERE IS OVERLAP, and it is worse than in the daily
cross-section.** Events cluster — earnings season puts hundreds of reports into
three weeks — so a pooled set of event returns is not a set of independent
observations in two ways at once: windows that overlap in time share sessions,
and events in the same month share whatever the market did that month. Pooling
them and taking a t-stat over the event count is how an event study manufactures
significance, and the event count is always flatteringly large.

So inference is taken on a CALENDAR-PERIOD SERIES (Jaffe/Mandelker's
calendar-time portfolio, in its simplest form): each event is assigned to its
quintile ONCE, over the pooled set, and then the spread is averaged WITHIN each
calendar month before any t-stat is taken. A month contributes one observation
however many reports it held, and Newey-West at the horizon's length in months
absorbs what overlap remains.

**BOTH SPREADS ARE REPORTED AND A DIVERGENCE IS INFORMATION.** The pooled
spread answers the question as asked — cut the events by magnitude, difference
the ends — but it is silent about WHEN each end happened, so if large surprises
cluster in rising markets it collects the market. The mean of the per-period
spreads cannot, because each of its terms is a difference taken inside one
month. When the two disagree materially, the quintiles are distributed unevenly
across time and the pooled number is the one to distrust.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from lab.cross_section import (
    QUINTILES,
    Observation,
    newey_west_t,
    quintile_buckets,
    spearman,
)

SESSIONS_PER_MONTH = 21
"""Trading sessions in a calendar month, for converting a horizon into a
Newey-West lag on the monthly series. Approximate by nature; the lag only has
to reach as far as the overlap does, and rounding up is the conservative
direction."""

MIN_EVENTS = QUINTILES * 10
"""Below fifty pooled events a quintile holds fewer than ten, and one event
moves the bucket's mean by a tenth of its own return. The same floor the daily
cross-section applies to symbols, for the same reason."""

MIN_PERIODS = 12
"""A Newey-West t-stat on fewer than twelve periods is not worth printing: the
correction estimates autocovariances from the series itself, and with a handful
of terms it is noise correcting noise."""


@dataclass(frozen=True)
class PeriodSpread:
    """One calendar month's quintile spread among the events it held."""

    period: str
    events: int
    spread: float


@dataclass(frozen=True)
class EventResult:
    """One (hypothesis, horizon, basis) measured over a set of events."""

    measurable: str
    horizon: int
    basis: str
    events: int
    """Events with a usable return on this basis. Not the same across bases:
    the beta basis drops a symbol whose trailing history is too short."""
    periods: int
    pooled_spread: float | None
    period_mean_spread: float | None
    t_stat: float | None
    t_stat_newey_west: float | None
    rank_ic: float | None
    symbols: int
    first_event: date | None
    last_event: date | None
    newey_west_lag: int = 0

    @property
    def label(self) -> str:
        return f"{self.measurable} / {self.horizon}d / {self.basis}"

    @property
    def divergent(self) -> bool:
        """Whether the pooled and per-period spreads tell different stories.

        True when they differ in SIGN, or by more than half the larger one in
        magnitude. Either means the quintiles are spread unevenly over time and
        the pooled figure is partly a statement about when events happened.
        """
        a, b = self.pooled_spread, self.period_mean_spread
        if a is None or b is None:
            return False
        if (a > 0) != (b > 0):
            return True
        bigger = max(abs(a), abs(b))
        return bigger > 0 and abs(a - b) / bigger > 0.5


def _period_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def measure_events(
    events: Sequence[Observation],
    *,
    measurable: str,
    horizon: int,
    basis: str = "raw",
) -> EventResult:
    """Quintile the events by magnitude, then infer on a monthly series.

    The quintile cut is POOLED, so a bucket means the same thing in 2009 and
    2025. The t-stats are taken on the per-period series, so neither the
    overlap of forward windows nor the clustering of reports is counted as
    independent evidence.
    """
    usable = [(o, o.excess(horizon, basis)) for o in events]
    usable = [(o, r) for o, r in usable if r is not None]
    empty = EventResult(
        measurable=measurable,
        horizon=horizon,
        basis=basis,
        events=len(usable),
        periods=0,
        pooled_spread=None,
        period_mean_spread=None,
        t_stat=None,
        t_stat_newey_west=None,
        rank_ic=None,
        symbols=len({o.symbol for o, _ in usable}),
        first_event=min((o.on for o, _ in usable), default=None),
        last_event=max((o.on for o, _ in usable), default=None),
    )
    if len(usable) < MIN_EVENTS:
        return empty

    magnitudes = [o.value for o, _ in usable]
    returns = [r for _, r in usable]
    buckets = quintile_buckets(magnitudes)

    top = [r for r, b in zip(returns, buckets, strict=True) if b == QUINTILES - 1]
    bottom = [r for r, b in zip(returns, buckets, strict=True) if b == 0]
    if not top or not bottom:
        return empty
    pooled = statistics.fmean(top) - statistics.fmean(bottom)

    # The per-period series, using the POOLED bucket each event already has.
    # Cutting quintiles inside each month instead would make a bucket mean
    # something different every month, and the series would no longer be
    # measuring one thing.
    by_period: dict[str, tuple[list[float], list[float]]] = {}
    for (observation, value), bucket in zip(usable, buckets, strict=True):
        if bucket not in (0, QUINTILES - 1):
            continue
        key = _period_of(observation.on)
        hi, lo = by_period.setdefault(key, ([], []))
        (hi if bucket == QUINTILES - 1 else lo).append(value)

    spreads = [
        PeriodSpread(period=key, events=len(hi) + len(lo),
                     spread=statistics.fmean(hi) - statistics.fmean(lo))
        for key, (hi, lo) in sorted(by_period.items())
        if hi and lo
    ]
    series = [s.spread for s in spreads]
    lag = max(1, math.ceil(horizon / SESSIONS_PER_MONTH))
    enough = len(series) >= MIN_PERIODS

    return EventResult(
        measurable=measurable,
        horizon=horizon,
        basis=basis,
        events=len(usable),
        periods=len(series),
        pooled_spread=pooled,
        period_mean_spread=statistics.fmean(series) if series else None,
        t_stat=_t(series) if enough else None,
        t_stat_newey_west=newey_west_t(series, lag) if enough else None,
        # POOLED, and that is a compromise worth naming: the daily
        # cross-section averages one IC per day because a day shares a market,
        # and the analogue here would be one per month — but a month can hold
        # four events, where a rank correlation is meaningless. Pooling mixes
        # periods, which the excess bases reduce and do not remove.
        rank_ic=spearman(magnitudes, returns),
        symbols=len({o.symbol for o, _ in usable}),
        first_event=min(o.on for o, _ in usable),
        last_event=max(o.on for o, _ in usable),
        newey_west_lag=lag,
    )


def _t(series: Sequence[float]) -> float | None:
    if len(series) < 2:
        return None
    spread = statistics.stdev(series)
    if spread == 0:
        return None
    return statistics.fmean(series) / (spread / math.sqrt(len(series)))
