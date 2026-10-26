"""Every hypothesis, registered with its rationale BEFORE it is measured.

**A SIGNAL WITHOUT A REASON IS A COINCIDENCE WITH A TICKER.** The registry
exists so the count of tests is known (which sets the significance bar in
`lab.multiple_testing`) and so no hypothesis can be invented after seeing the
answer — the two failures that make a research lab produce confident nonsense.
So `register` REFUSES a hypothesis with no economic rationale, and the
registration date is stamped: a rationale written after the measurement is not
a rationale, and a dated one can at least be checked against the run that
measured it.

The rationale is one line and must say WHY the number should predict returns,
not what the number is. "RSI measures momentum" is a definition; "a stock that
has pulled back without breaking its trend is being sold by holders with no
information, and the trend resumes" is a claim that can be wrong.

**DIRECTION IS PART OF THE PRE-REGISTRATION.** A two-sided test on a signal
whose sign was chosen after seeing the spread is a one-sided test wearing a
disguise, and it doubles the effective number of hypotheses. `unsigned` is
allowed and says so explicitly, so the report can hold it to a two-sided bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

MIN_RATIONALE_CHARS = 30

DIRECTIONS = ("higher_is_better", "lower_is_better", "unsigned")


@dataclass(frozen=True)
class Hypothesis:
    """One measurable, one claim about why it should predict returns."""

    name: str
    measurable: str
    """The key a provider resolves to a daily cross-section of values."""
    rationale: str
    direction: str
    family: str
    """Hypotheses that share a mechanism. Reported together so a family that
    only works through one of its members is visible as such."""
    registered_on: date
    horizons: tuple[int, ...] = (5, 10, 20)
    stage: int = 1
    notes: str = ""

    def __post_init__(self) -> None:
        if len(self.rationale.strip()) < MIN_RATIONALE_CHARS:
            raise ValueError(
                f"hypothesis {self.name!r} has no economic rationale. A signal without a "
                f"reason is a coincidence with a ticker, and the registry exists so the "
                f"reason is written down BEFORE the measurement rather than after it"
            )
        if self.direction not in DIRECTIONS:
            raise ValueError(
                f"hypothesis {self.name!r} has direction {self.direction!r}; expected one of "
                f"{DIRECTIONS}. The sign is part of the pre-registration — choosing it after "
                f"seeing the spread is a one-sided test wearing a disguise"
            )


REGISTRY: dict[str, Hypothesis] = {}


def register(hypothesis: Hypothesis) -> Hypothesis:
    if hypothesis.name in REGISTRY:
        raise ValueError(
            f"{hypothesis.name!r} is already registered. Two hypotheses under one name "
            f"would make the test count wrong, and the test count is the significance bar"
        )
    REGISTRY[hypothesis.name] = hypothesis
    return hypothesis


def _h(name: str, measurable: str, rationale: str, direction: str, family: str, **kw) -> None:
    register(
        Hypothesis(
            name=name,
            measurable=measurable,
            rationale=rationale,
            direction=direction,
            family=family,
            registered_on=date(2026, 10, 26),
            **kw,
        )
    )


# ── Stage 1, batch one: what the engine already computes ────────────────────
# These cost nothing new to measure, and every one of them is currently TRUSTED
# by the composite on plausibility alone (D2/D32 set the weights between
# pillars from judgement, and D39(4) says a backtest cannot judge the composite
# because four of its series start 2026-10-21). That is the gap this batch
# closes: the engine has been scoring symbols on these numbers for a week
# without knowing whether any of them predicts anything.

_h(
    "rsi_reset",
    "rsi_14",
    "A pullback that holds its trend is distribution by holders without "
    "information; the trend resumes once they are done, so a mid-range RSI in an "
    "uptrend should precede positive returns",
    "unsigned",
    "mean_reversion",
)
_h(
    "relative_volume",
    "relative_volume_20d",
    "Volume is the cost of changing someone's mind, so a move on heavy volume "
    "reflects more reallocated capital and should persist further than a quiet one",
    "higher_is_better",
    "participation",
)
_h(
    "relative_strength_vs_sector",
    "relative_strength_20d",
    "A stock outperforming its own sector is being bought for reasons specific to "
    "it, and idiosyncratic demand is what a stock-picking edge has to be made of",
    "higher_is_better",
    "momentum",
)
_h(
    "squeeze_percentile",
    "squeeze_percentile",
    "Volatility clusters, so an unusually quiet range against the symbol's own "
    "year precedes an expansion; direction comes from the setup, size from the squeeze",
    "lower_is_better",
    "volatility",
    notes="O13 found it carries no OUTCOME information for base_breakout; stage 1 asks "
    "whether it predicts returns at all, independent of that recipe.",
)
_h(
    "estimate_revision_momentum",
    "revision_momentum",
    "Analysts update slowly and in the same direction, so a consensus already "
    "moving up is more likely to keep moving and the price to follow it",
    "higher_is_better",
    "fundamental_drift",
    notes="Series starts 2026-10-22 (D7/D22), so this is forward-only until O19's backfills.",
)
_h(
    "earnings_surprise",
    "eps_surprise_pct",
    "A beat the market has not finished pricing leaves drift behind it, which is "
    "the oldest documented anomaly in the literature and the premise of post_earnings_drift",
    "higher_is_better",
    "fundamental_drift",
)
_h(
    "insider_cluster_buying",
    "insider_cluster_score",
    "Several insiders buying with their own money in a short window is the one "
    "trade in the filings that is costly to fake and has no compensation explanation",
    "higher_is_better",
    "informed_flow",
)
_h(
    "news_tone_delta",
    "tone_delta",
    "A CHANGE in how a company is written about marks the arrival of information, "
    "where the level only marks what it is already known for (D32)",
    "higher_is_better",
    "attention",
)
_h(
    "news_volume_spike",
    "news_volume_ratio",
    "Attention precedes flow from investors who do not read filings, so a spike "
    "against the symbol's own normal coverage should precede a move",
    "unsigned",
    "attention",
)
_h(
    "iv_rank",
    "iv_rank",
    "Implied volatility is the market's own forecast of movement, and where it is "
    "cheap against the symbol's own history the market is underwriting a move too cheaply",
    "unsigned",
    "volatility",
    notes="Needs the self-built IV history (D7/D22), which starts 2026-10-21.",
)

# ── Stage 1, batch two: the questions the engine itself raised ──────────────

_h(
    "composite_as_ranking",
    "composite",
    "If the four-pillar composite carries information about outcomes, a higher "
    "score should predict a better return across the whole cross-section — which "
    "is the claim that justifies it SORTING candidates",
    "higher_is_better",
    "composite",
    notes="D53/O18. The mirror of the next one: 5 of 310 tier-qualifying symbols produced "
    "a recipe plan on 2026-10-21, so the composite and the recipes are measuring nearly "
    "disjoint things on live data. Whether the composite deserves to RANK is answerable "
    "independently of any recipe; whether it deserves to GATE is not.",
)
_h(
    "composite_as_filter_on_recipe_signals",
    "composite_within_recipe_signals",
    "If the composite adds information to a recipe signal, then among symbols "
    "where a recipe fired the higher-composite ones should do better — which is the "
    "only claim that justifies it GATING a plan",
    "higher_is_better",
    "composite",
    notes="D53's open question. Measured over recipe-signal days only, so its sample is "
    "the signals and not the universe; the bar has to account for that smaller n. If this "
    "fails while composite_as_ranking passes, the two belong in separate products rather "
    "than in one gate (O18 reading (c)).",
)
_h(
    "short_hold_reclaim",
    "reclaim_hold_length",
    "If a reclaim's edge is front-loaded, the first days after the entry carry the "
    "return and holding longer only adds variance — so forward returns should decay "
    "with the horizon rather than accumulate",
    "lower_is_better",
    "mean_reversion",
    notes="From O20's net sweep, where the 60-symbol preview fell monotonically with hold "
    "length and the FULL universe did not (+0.64R at 20 days, +0.41 at 30, +0.55 at 90). "
    "Registered because the question is worth asking properly, with its supporting "
    "evidence explicitly withdrawn: what the full table supports is that hold length "
    "barely matters, which is a different claim. Varies the HOLD alone — same entries, "
    "no concurrency rule in the way — which the sweep could not do.",
)


def by_family() -> dict[str, list[Hypothesis]]:
    out: dict[str, list[Hypothesis]] = {}
    for h in REGISTRY.values():
        out.setdefault(h.family, []).append(h)
    return out


def measured_count() -> int:
    """How many hypotheses the significance bar has to cover."""
    return len(REGISTRY)
