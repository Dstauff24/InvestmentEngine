"""How much evidence a feature stood on, reported alongside its value.

Some features are computed from series the engine accumulates itself — the IV
history behind an IV rank (D7/D22), the dated estimate snapshots behind
revision momentum. On the day a series starts there is nothing; months later
there is plenty; in between there is something, and "something" is the hard
case.

A bare value hides which of the three you are looking at. `None` says "no
signal yet" and a number says "signal", but a number computed from two
snapshots and a number computed from sixty are not the same claim, and a
scoring layer given only the number cannot tell them apart. Worse, neither can
the Shadow Book — so when D39's calibration work asks *does thin evidence
predict unreliability*, the data to answer it would never have been recorded.

So every such feature reports an `EvidenceDepth` next to its value. The feature
does not decide what the depth means; the scoring layer owns that, the same way
it owns the meaning of abstention.

TWO KINDS OF THIN, which this deliberately does not collapse:

  * **Mathematically undefined.** An IV rank needs a high and a low to sit
    between; on a flat or one-day history the formula has no answer and the
    honest return is None. `features/iv.py` therefore gates hard on
    `iv_rank_min_history_days`.
  * **Correct but noisy.** A consensus EPS estimate that moved from 2.00 to
    2.39 between two snapshots moved, exactly that much, as a matter of
    record. The measurement is right; what is uncertain is whether it marks a
    trend. Gating it to None would discard a true observation.

Revision momentum is the second kind, so it is computed from two snapshots and
reported with the depth that says how much weight it has earned.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class EvidenceDepth:
    """How much of our own accumulated series a feature actually used."""

    observations: int
    """Distinct observation dates used. For a daily series, days of data."""

    span_days: int
    """Calendar days between the first and last observation used.

    Distinct from `observations` on purpose: ten snapshots taken in one week
    and ten taken over a year carry very different weight, and a count alone
    cannot tell them apart. For a strictly daily series the two move together;
    for estimate snapshots — which arrive when the provider updates, not on a
    schedule — they do not.
    """

    first: date | None = None
    last: date | None = None

    @property
    def is_empty(self) -> bool:
        return self.observations == 0

    @property
    def is_minimal(self) -> bool:
        """Exactly enough to compute a difference, and no more.

        Not a judgement that the value is unusable — just the flag that says
        this is the thinnest a real measurement can be.
        """
        return self.observations == 2

    def meets(self, *, observations: int = 0, span_days: int = 0) -> bool:
        """Does this depth clear a caller's bar? The caller sets the bar."""
        return self.observations >= observations and self.span_days >= span_days

    def __str__(self) -> str:
        if self.is_empty:
            return "no observations"
        window = f"{self.first}..{self.last}" if self.first and self.last else "unknown window"
        return f"{self.observations} observation(s) over {self.span_days}d ({window})"


EMPTY = EvidenceDepth(observations=0, span_days=0)


def depth_from_dates(dates: list[date]) -> EvidenceDepth:
    """Build a depth from the observation dates a feature consumed."""
    unique = sorted(set(dates))
    if not unique:
        return EMPTY
    return EvidenceDepth(
        observations=len(unique),
        span_days=(unique[-1] - unique[0]).days,
        first=unique[0],
        last=unique[-1],
    )
