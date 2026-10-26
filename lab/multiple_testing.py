"""The significance bar, adjusted for how many hypotheses were tested.

**TESTING TWENTY SIGNALS AT p<0.05 PRODUCES ONE "DISCOVERY" FROM NOISE ALONE**,
and a lab that reports each test against its own bar will therefore find
something every time it is run. So the bar moves with the number of tests, and
the count includes every hypothesis in the registry that was measured — not
just the ones worth printing, because choosing what to print after seeing the
answers is the same error wearing a different hat.

TWO METHODS, BOTH REPORTED, because they answer different questions:

* **Bonferroni** (`alpha / m`) controls the chance of ANY false positive. It is
  the conservative bar and the right one for a decision that cannot be undone
  cheaply — promoting a strategy to real capital.
* **Benjamini-Hochberg** controls the FALSE DISCOVERY RATE: of the hypotheses
  called significant, the expected share that are not. At q=0.10 it says "one
  in ten of these will be noise", which is the honest bar for stage 1, whose
  job is to decide what reaches stage 2 rather than what gets traded.

The lab reports BH at q=0.10 as its gate and prints the Bonferroni threshold
beside it, so a reader can see how much of the result depends on the choice.
Nothing here converts a t-stat to a p-value under an assumption of normality
without saying so: the two-sided normal approximation is used, which is
reasonable for the 200+ daily observations stage 1 works with and would not be
for twenty.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

ALPHA = 0.05
"""The per-test bar before any adjustment. Only ever used to derive one."""

FDR_Q = 0.10
"""Stage 1's gate: one in ten survivors is expected to be noise. Stage 1 feeds
stage 2, which measures again — so a false positive costs a measurement, not
capital."""


def two_sided_p(t_stat: float | None) -> float | None:
    """p from a t-stat under the normal approximation. Stated, not hidden."""
    if t_stat is None:
        return None
    return math.erfc(abs(t_stat) / math.sqrt(2))


@dataclass(frozen=True)
class Verdict:
    """One hypothesis against a bar that knows how many others were tested."""

    name: str
    p_value: float | None
    rank: int
    tests: int
    bh_threshold: float
    bonferroni_threshold: float

    @property
    def survives_fdr(self) -> bool:
        return self.p_value is not None and self.p_value <= self.bh_threshold

    @property
    def survives_bonferroni(self) -> bool:
        return self.p_value is not None and self.p_value <= self.bonferroni_threshold

    @property
    def verdict(self) -> str:
        if self.p_value is None:
            return "UNMEASURED"
        if self.survives_bonferroni:
            return "survives both bars"
        if self.survives_fdr:
            return f"survives FDR q={FDR_Q:g}, not Bonferroni"
        return "not significant"


def adjust(p_values: dict[str, float | None], *, q: float = FDR_Q) -> list[Verdict]:
    """Benjamini-Hochberg over every hypothesis MEASURED, plus Bonferroni.

    `m` counts the hypotheses that produced a p-value. An unmeasured one does
    not make the others' bar harder — it was never a chance to find something —
    but it is returned in the list so the registry and the report cannot
    silently disagree about what was tried.
    """
    measured = {name: p for name, p in p_values.items() if p is not None}
    m = len(measured)
    bonferroni = ALPHA / m if m else ALPHA
    ordered = sorted(measured.items(), key=lambda kv: kv[1])

    # BH: the largest k whose p(k) <= k/m * q defines the threshold; every
    # hypothesis at or below that p survives. Taking each p against its own
    # k/m*q instead would reject a small p whose neighbours failed, which is
    # not what the procedure says.
    threshold = 0.0
    for k, (_, p) in enumerate(ordered, start=1):
        if p <= k / m * q:
            threshold = p
    out: list[Verdict] = []
    ranks = {name: i for i, (name, _) in enumerate(ordered, start=1)}
    for name, p in p_values.items():
        out.append(
            Verdict(
                name=name,
                p_value=p,
                rank=ranks.get(name, 0),
                tests=m,
                bh_threshold=threshold,
                bonferroni_threshold=bonferroni,
            )
        )
    return out
