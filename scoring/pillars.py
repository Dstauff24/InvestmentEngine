"""Feature values -> four pillar scores on 0-100 (D2/D32/D41).

ONE MAPPING SHAPE, used everywhere. A signed signal `x` with a configured
full-credit scale `c` becomes:

    score = 50 + 50 * clamp(x / c, -1, +1)

so 0 is neutral (50), +c earns 100, -c earns 0, and beyond ±c the signal
SATURATES rather than growing without bound. Saturation is deliberate: a
company growing revenue 400% is not sixteen times better than one growing 25%,
and an unbounded map would let one wild sub-signal dominate a pillar.

EQUAL WEIGHTING WITHIN A PILLAR, by decision. A pillar is the MEAN of the
sub-signals that scored. D2/D32 fix the weights BETWEEN pillars because the
decision log states them; nothing states whether revision momentum should
outrank margin trend inside Pillar B, so a number there would be false
precision dressed as configuration. D13's quarterly review introduces
sub-weights when there is evidence for them.

ABSTENTION RENORMALIZES AT BOTH LEVELS (D41). A sub-signal with no data is
excluded from its pillar's mean, exactly as an abstaining pillar is excluded
from the composite. A pillar with no scoring sub-signals abstains in turn, and
`None` propagates — it is never 50, because 50 is a claim that the evidence was
neutral and we have none.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core import config
from features.catalyst import CatalystFeatures
from features.fundamental import FundamentalFeatures
from features.sentiment import SentimentFeatures
from features.technical import TechnicalFeatures

log = logging.getLogger(__name__)

NEUTRAL = 50.0

# The pillar letters, in D2's order. Used for the D41 abstention mask.
PILLARS = ("A", "B", "C", "D")
PILLAR_NAMES = {"A": "technical", "B": "fundamental", "C": "catalyst", "D": "sentiment"}

__all__ = [
    "PILLARS",
    "PillarScore",
    "PillarScores",
    "band_score",
    "proximity_score",
    "score_all",
    "signed_score",
]


# ── the mapping primitives ──────────────────────────────────────────────────


def signed_score(value: float | None, full_credit: float) -> float | None:
    """A signed signal onto 0-100, neutral at 50, saturating at ±full_credit."""
    if value is None:
        return None
    if full_credit <= 0:
        raise ValueError(f"full_credit must be positive, got {full_credit}")
    ratio = max(-1.0, min(1.0, value / full_credit))
    return NEUTRAL + NEUTRAL * ratio


def band_score(
    value: float | None, low: float, high: float, *, floor: float = 0.0, ceiling: float = 100.0
) -> float | None:
    """100 inside [low, high], falling off linearly to 0 at the outer limits.

    For readings where the GOOD answer is a range rather than a direction —
    RSI being the case in point. An RSI of 95 is not better than 60; it is a
    different and worse thing, and a monotonic map would score it highest.
    """
    if value is None:
        return None
    if low <= value <= high:
        return 100.0
    if value < low:
        span = low - floor
        return 0.0 if span <= 0 else max(0.0, 100.0 * (value - floor) / span)
    span = ceiling - high
    return 0.0 if span <= 0 else max(0.0, 100.0 * (ceiling - value) / span)


def proximity_score(days: int | None, near: int, horizon: int) -> float | None:
    """100 when an event is within `near` days, 50 at `horizon`, None beyond.

    None beyond the horizon rather than 0: a catalyst 200 days out is not
    BAD news, it is no news, and scoring it 0 would drag Pillar C down for
    every stock that simply has nothing scheduled.
    """
    if days is None or days < 0 or days > horizon:
        return None
    if days <= near:
        return 100.0
    span = horizon - near
    return 100.0 - 50.0 * (days - near) / span


# ── per-pillar ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PillarScore:
    """One pillar's score and the sub-signals behind it."""

    letter: str
    score: float | None
    """None means ABSTAINED — no sub-signal had data. Never 50 (D41)."""
    inputs: dict[str, float] = field(default_factory=dict)
    """Only the sub-signals that scored, for the idea card."""
    missing: tuple[str, ...] = ()

    @property
    def scored(self) -> bool:
        return self.score is not None

    @property
    def agrees(self) -> bool:
        """D12: does this pillar count as agreeing?

        False when abstaining. D41: an abstaining pillar can neither agree nor
        conflict, and `pillar_agreement.treat_missing_as_agreement` is False in
        config for the same reason.
        """
        if self.score is None:
            return False
        return self.score >= config.scoring().pillar_agreement.min_score

    @property
    def conflicts(self) -> bool:
        """D2: does this pillar actively argue AGAINST the idea?

        False when abstaining (D41) — silence is not an objection.
        """
        if self.score is None:
            return False
        return self.score <= config.scoring().composite.conflict_below


def _reduce(letter: str, parts: dict[str, float | None]) -> PillarScore:
    """Mean of the sub-signals that scored; abstain if none did."""
    scored = {name: value for name, value in parts.items() if value is not None}
    missing = tuple(name for name, value in parts.items() if value is None)
    if not scored:
        return PillarScore(letter, None, {}, missing)
    return PillarScore(letter, sum(scored.values()) / len(scored), scored, missing)


def technical_score(features: TechnicalFeatures, cfg=None) -> PillarScore:
    """Pillar A: price and volume structure (weight 40).

    ATR is deliberately absent. It is a RISK unit — the stop distance D17 sizes
    against — not a directional opinion, and scoring it would reward or punish
    volatility itself. Relative volume is likewise excluded: it confirms a
    breakout within a recipe rather than describing a stock's health.
    """
    m = (cfg or config.scoring().mapping).technical
    ma = features.ma

    structure: float | None = None
    if ma.stacked is not None:
        # The one boolean in the mapping. Full credit for a clean uptrend,
        # nothing for its absence; influence comes from being one of four
        # sub-signals, not from a tuned weight.
        structure = 100.0 if ma.stacked else 0.0

    trend: float | None = None
    if ma.fast_rising is not None and ma.mid_rising is not None:
        trend = 50.0 * (int(ma.fast_rising) + int(ma.mid_rising))

    return _reduce(
        "A",
        {
            "ma_structure": structure,
            "ma_trend": trend,
            # Position in the 52-week range is already 0-1 and already means
            # "high is strong", so it needs no scale parameter.
            "range_position": (
                None
                if features.week52.range_position is None
                else 100.0 * features.week52.range_position
            ),
            "rsi_health": band_score(features.rsi, m.rsi_healthy_low, m.rsi_healthy_high),
            "relative_strength": signed_score(features.rs_vs_sector, m.rs_full_credit_pct),
        },
    )


def fundamental_score(features: FundamentalFeatures, cfg=None) -> PillarScore:
    """Pillar B: growth, margins and revision momentum (weight 30)."""
    m = (cfg or config.scoring().mapping).fundamental

    # Revision momentum is gated on OUR OWN series depth (D41 /
    # features/evidence.py): correct at two snapshots, trustworthy at sixty,
    # and this is where "trustworthy enough to score" is drawn. Below the floor
    # it abstains rather than contributing a noisy estimate of a trend.
    revision: float | None = None
    if features.revision.depth.meets(observations=m.revision_min_observations):
        revision = signed_score(features.revision.eps_pct, m.revision_full_credit_pct)

    growth = [
        signed_score(features.revenue_growth_yoy, m.growth_full_credit_pct),
        signed_score(features.eps_growth_yoy, m.growth_full_credit_pct),
    ]
    acceleration = [
        signed_score(features.revenue_acceleration, m.acceleration_full_credit_pp),
        signed_score(features.eps_acceleration, m.acceleration_full_credit_pp),
    ]
    margins = [
        signed_score(features.gross_margin_trend, m.margin_trend_full_credit_pp),
        signed_score(features.operating_margin_trend, m.margin_trend_full_credit_pp),
        signed_score(features.net_margin_trend, m.margin_trend_full_credit_pp),
    ]

    return _reduce(
        "B",
        {
            # Revenue and EPS are averaged into ONE growth sub-signal rather
            # than counted as two, so a company with both does not get double
            # the growth influence of one reporting only revenue.
            "growth": _mean(growth),
            "acceleration": _mean(acceleration),
            "margin_trend": _mean(margins),
            "revision_momentum": revision,
        },
    )


def catalyst_score(features: CatalystFeatures, cfg=None) -> PillarScore:
    """Pillar C: scheduled events and insider clusters (weight 20)."""
    m = (cfg or config.scoring().mapping).catalyst

    upcoming = proximity_score(
        features.days_to_earnings, m.earnings_near_days, m.earnings_horizon_days
    )
    drift = proximity_score(features.days_since_earnings, m.earnings_near_days, m.drift_window_days)

    insiders: float | None = None
    if features.insiders.is_cluster is not None:
        # None stays None: D41's rule one level down. "The series is too young
        # to judge" is not "there is no cluster".
        insiders = 100.0 if features.insiders.is_cluster else NEUTRAL

    # An imminent ex-date is a RISK (D19(3) forbids a new swing long within two
    # days), so it scores BELOW neutral rather than abstaining — unlike a
    # distant earnings date, this is an active reason for caution.
    ex_div: float | None = None
    if features.days_to_ex_dividend is not None:
        ex_div = 0.0 if features.days_to_ex_dividend <= m.ex_div_penalty_days else NEUTRAL

    return _reduce(
        "C",
        {
            "earnings_upcoming": upcoming,
            "earnings_drift": drift,
            "insider_cluster": insiders,
            "ex_dividend_risk": ex_div,
        },
    )


def sentiment_score(features: SentimentFeatures, cfg=None) -> PillarScore:
    """Pillar D: news tone CHANGE and volume spike (weight 10).

    Both sub-signals are deltas (D32): absolute tone is not scored at all. A
    permanently gloomy stock would otherwise be marked down every single day
    for news that is already in the price.
    """
    m = (cfg or config.scoring().mapping).sentiment

    # A volume spike is UNSIGNED — more coverage than normal is notable in
    # either direction — so it maps from 1.0 (normal) upward only, and never
    # below neutral. Quiet news is not bearish.
    spike: float | None = None
    if features.volume_spike is not None:
        excess = max(0.0, features.volume_spike - 1.0)
        full = max(1e-9, m.volume_spike_full_credit - 1.0)
        spike = NEUTRAL + NEUTRAL * min(1.0, excess / full)

    return _reduce(
        "D",
        {
            "tone_delta": signed_score(features.tone_delta, m.tone_delta_full_credit),
            "volume_spike": spike,
        },
    )


# ── the bundle ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PillarScores:
    """All four pillars for one symbol, plus D41's abstention mask."""

    symbol: str
    technical: PillarScore
    fundamental: PillarScore
    catalyst: PillarScore
    sentiment: PillarScore

    @property
    def all(self) -> tuple[PillarScore, ...]:
        return (self.technical, self.fundamental, self.catalyst, self.sentiment)

    @property
    def scored(self) -> tuple[PillarScore, ...]:
        return tuple(p for p in self.all if p.scored)

    @property
    def mask(self) -> str:
        """D41's abstention mask, e.g. "A,B,C". Recorded on every idea."""
        return ",".join(p.letter for p in self.scored)

    @property
    def agreeing(self) -> tuple[str, ...]:
        """Letters of pillars that AGREE (D12). Abstainers cannot (D41)."""
        return tuple(p.letter for p in self.all if p.agrees)

    @property
    def conflicting(self) -> tuple[str, ...]:
        """Letters of pillars that CONFLICT. Abstainers cannot (D41)."""
        return tuple(p.letter for p in self.all if p.conflicts)


def score_all(
    symbol: str,
    technical: TechnicalFeatures,
    fundamental: FundamentalFeatures,
    catalyst: CatalystFeatures,
    sentiment: SentimentFeatures,
    cfg=None,
) -> PillarScores:
    """Map one symbol's four feature bundles onto four pillar scores."""
    mapping = cfg or config.scoring().mapping
    return PillarScores(
        symbol=symbol,
        technical=technical_score(technical, mapping),
        fundamental=fundamental_score(fundamental, mapping),
        catalyst=catalyst_score(catalyst, mapping),
        sentiment=sentiment_score(sentiment, mapping),
    )


def _mean(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None
