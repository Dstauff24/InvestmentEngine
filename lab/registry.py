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

SHAPES = ("cross_section", "event_study")
"""**TWO DIFFERENT MEASUREMENTS, and conflating them is how a conditional edge
disappears.** A `cross_section` hypothesis sorts EVERY symbol EVERY day, which
is the right instrument for a signal that is always present (momentum,
volatility) and close to the worst one for a signal that fires on one symbol a
week — `post_earnings_drift` would be measured against 400 symbols that had no
earnings. An `event_study` measures forward returns from a QUALIFYING EVENT,
pooled across the window and cut into quintiles by the event's magnitude, which
is the shape the drift literature uses and the shape three of this engine's
four pillars actually imply."""


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
    shape: str = "cross_section"
    notes: str = ""
    confirmation_withheld: str = ""
    """**WHY THIS WINDOW CANNOT CONFIRM THE HYPOTHESIS, when it cannot.**

    Non-empty marks a hypothesis whose measurement here is admissible as a
    REFUSAL and never as a confirmation, and the report carries the reason on
    every one of its rows plus a footnote. The case it was added for is a
    hypothesis registered AFTER a related result was already seen: externally
    motivated (so not a sign flip) and still suggested by what had been seen
    (so not independent of it). Those two facts coexist, and a result printed
    without the second would be read as a clean out-of-sample confirmation.

    The text lives here rather than in the renderer because it is part of the
    pre-registration — it was decided before the measurement, like the
    direction and the rationale.
    """

    def __post_init__(self) -> None:
        if len(self.rationale.strip()) < MIN_RATIONALE_CHARS:
            raise ValueError(
                f"hypothesis {self.name!r} has no economic rationale. A signal without a "
                f"reason is a coincidence with a ticker, and the registry exists so the "
                f"reason is written down BEFORE the measurement rather than after it"
            )
        if self.shape not in SHAPES:
            raise ValueError(
                f"hypothesis {self.name!r} has shape {self.shape!r}; expected one of {SHAPES}. "
                f"The shape decides the instrument, and measuring a conditional signal across "
                f"the whole cross-section is how a real edge comes back null"
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
            # The batches were registered on different days, and the date is
            # part of the pre-registration: it is what says a hypothesis
            # predates the measurement that judges it.
            registered_on=kw.pop("registered_on", date(2026, 10, 26)),
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


# ── Stage 1, batch three: EVENT STUDIES (registered Oct 26 2026) ───────────
# The first run's null is part of the reason these exist. Four technical
# measurables showed no univariate power across the daily cross-section, and
# three of the engine's four pillars are CONDITIONAL by construction — they say
# something about a symbol in the days after an event and nothing about it on a
# random Tuesday. A cross-section is the wrong instrument for them, so these
# are measured from the event forward.

_h(
    "earnings_surprise_magnitude",
    "eps_surprise_pct",
    "If the market under-reacts to news, the SIZE of the surprise should order "
    "the drift that follows it — a bigger gap between actual and consensus leaving "
    "more left to be priced",
    "higher_is_better",
    "fundamental_drift",
    shape="event_study",
    notes="The event is a report whose reaction window has completed (D49). Quintiles cut on "
    "surprise against `abs(estimate)` so a loss-maker beating a negative estimate scores "
    "positive, as the recipe does. This is the premise post_earnings_drift already trades on, "
    "measured independently of the recipe's entry and exit. "
    "**THIS IS THE LAB'S POSITIVE CONTROL (moved here Oct 27 2026, before the re-run).** It "
    "is the only effect the engine has already CERTIFIED in-house — D48's +0.22R out of "
    "sample on verified beats — so the harness has something it must be able to reproduce. "
    "Point-in-time rules are D49's throughout: the effective report date is the LATER of the "
    "dates held, the reaction window gates the event, and forward returns start from the next "
    "session. If this comes back null, the SUSPECT IS THE HARNESS AND NOT THE MARKET, which "
    "is exactly what momentum's null could not establish.",
)
_h(
    "post_report_gap_and_hold",
    "gap_then_hold",
    "A gap the market does not fill is the market refusing to take the news back, "
    "so holding the reaction low should separate real repricing from a one-day "
    "overreaction",
    "higher_is_better",
    "fundamental_drift",
    shape="event_study",
    notes="Measures the gap SIZE and whether the reaction low held, which are the two "
    "conditions post_earnings_drift gates on — so a null here would say the recipe's entry "
    "test carries no information even where its surprise test does.",
)
_h(
    "insider_cluster_after_filing",
    "insider_cluster_score",
    "Several insiders buying with their own money in a short window is the one "
    "trade in the filings that is costly to fake, so the days after the CLUSTER "
    "completes should carry the information the filings revealed",
    "higher_is_better",
    "informed_flow",
    shape="event_study",
    notes="BLOCKED ON O19's EDGAR backfill: `insider_transactions` holds 26 rows from one "
    "validation pull, so there is no history to study yet. Keyed on `filed_at` (D25/O19) — a "
    "filing is knowledge when the SEC accepts it, and keying on the transaction date would let "
    "the study act on a purchase before anyone could know.",
)
_h(
    "estimate_revision_direction",
    "revision_direction",
    "Analysts move in the same direction for weeks, so the SIGN of a consensus "
    "change should predict the next one and the price drift that follows it",
    "higher_is_better",
    "fundamental_drift",
    shape="event_study",
    notes="FORWARD-ONLY, from our OWN `estimate_snapshots`: the series starts 2026-10-22 and "
    "the vendor's historical `epsEstimated` rows are mutable (one carried `lastUpdated` sixteen "
    "months after the report), so a revision measured against them is not point-in-time. The "
    "event is a snapshot-to-snapshot change in consensus.",
)

# ── Stage 1, batch four: the long-horizon factors the registry omitted ─────
# Both are documented cross-sectional premia measured over decades elsewhere,
# and neither was in the engine at all — the composite reaches for 20-day
# relative strength and never asks the question these ask. They are registered
# here partly as a CONTROL: a screen that cannot find 12-1 momentum over five
# years is probably underpowered rather than looking at a dead universe, and
# that is worth knowing before the engine's own signals are judged by it.

_h(
    "momentum_12_1",
    "momentum_12_1",
    "Winners keep winning over months because investors underreact to slow news "
    "and chase after it, while the most recent month is dropped because short-term "
    "reversal runs the other way and cancels it",
    "higher_is_better",
    "momentum",
    horizons=(20, 60, 120),
    notes="The classic 12-month-minus-1-month construction. REGISTERED AS A POWER CONTROL AND "
    "THEN WITHDRAWN AS ONE (Oct 27 2026, before the re-run): measured null in all nine cells "
    "over 2008-2026, |t(NW)| <= 0.86, and that null is CONFOUNDED rather than clean. The 2009 "
    "momentum crash is one of the largest factor drawdowns on record and sits inside this "
    "window; large-cap momentum has been weak since; and survivorship (O22) removes the "
    "delisted losers that the short leg needs. A control has to be a premium this harness "
    "would certainly see if it worked, and this one is not, so it cannot convict the "
    "instrument. The control moved to the drift event study, which has a certified in-house "
    "effect to reproduce.",
)
_h(
    "low_volatility",
    "volatility_60d",
    "Leverage-constrained investors bid up volatile names for their lottery "
    "payoff, so the quiet ones are left cheap and earn more per unit of risk than "
    "they should",
    "lower_is_better",
    "volatility",
    horizons=(20, 60, 120),
    notes="Realised 60-session volatility. Direction is pre-registered as LOWER IS BETTER, "
    "which is the anomaly's claim and the opposite of what a risk model predicts — so a "
    "positive spread here would be the finding, not a sign error.",
)


# ── Stage 1, batch three: an externally motivated reversal test ─────────────
#
# PRE-REGISTERED Oct 27 2026, BEFORE the re-run measured it, and it is NOT a
# sign flip dressed up. The distinction that makes it admissible: `rsi_reset`
# and `relative_strength_vs_sector` came back pointing at short-term losers,
# and the temptation is to re-register them the other way round — which would
# be choosing a sign after seeing a spread, the exact move the registry exists
# to refuse. This hypothesis instead comes from outside the data: Jegadeesh
# (1990) and Lehmann (1990) document one-month reversal in US equities on a
# plain trailing return, and the horizon and the direction are both theirs.
#
# AND ITS CONFIRMATION DOES NOT COME FROM THIS WINDOW. A hypothesis registered
# the day after a related result pointed the same way is still suggested by
# that result, however external its literature. So what this measurement can
# do is REFUSE it; what it cannot do is confirm it. Confirmation has to come
# from the forward `daily_candidates` record (D42), which was written before
# anybody looked.

_h(
    "short_term_reversal",
    "prior_return_20d",
    "Over about a month, price moves made by liquidity demand rather than news "
    "are paid for by whoever supplies the other side, so the move reverses as "
    "that premium is earned back",
    "lower_is_better",
    "mean_reversion",
    registered_on=date(2026, 10, 27),
    confirmation_withheld=(
        "in-window, registered after related results were seen; "
        "confirmation from forward daily_candidates only"
    ),
    notes="Jegadeesh (1990) and Lehmann (1990): the documented one-month reversal effect. "
    "EXTERNALLY MOTIVATED, not a re-registration of rsi_reset or relative_strength_vs_sector "
    "with the sign turned round after they came back negative — the horizon and the direction "
    "are the literature's. THIS WINDOW CAN ONLY REFUSE IT: registered the day after a related "
    "result pointed the same way, it is suggested by that result whatever its provenance, so "
    "confirmation comes from the forward daily_candidates record and never from here. Note it "
    "is the deliberate opposite of momentum_12_1's construction, which skips the most recent "
    "month precisely to keep this effect out.",
)


def by_shape() -> dict[str, list[Hypothesis]]:
    out: dict[str, list[Hypothesis]] = {}
    for h in REGISTRY.values():
        out.setdefault(h.shape, []).append(h)
    return out


def by_family() -> dict[str, list[Hypothesis]]:
    out: dict[str, list[Hypothesis]] = {}
    for h in REGISTRY.values():
        out.setdefault(h.family, []).append(h)
    return out


def measured_count() -> int:
    """How many hypotheses the significance bar has to cover."""
    return len(REGISTRY)
