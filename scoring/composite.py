"""Pillar scores -> composite, rank and tier (D2/D12/D32/D33/D34/D41).

THE COMPOSITE IS COMPUTED OVER SCORING PILLARS ONLY, WEIGHTS RENORMALIZED
(D41). An abstaining pillar is excluded rather than scored 50, because 50 is a
claim that the evidence was neutral and we have none. It can neither agree nor
conflict: silence is not agreement and it is not an objection.

That changes what a composite MEANS, and the change is invisible in the number:
72 from four pillars and 72 from two are different claims. So `mask` travels
with every score, is printed on every card, and is recorded on every Shadow
Book row — D39's calibration groups on it, and whether thinner composites are
measurably less reliable is the only evidence that would revise D41.

TWO PHASES, because D33 is cross-sectional. Phase one scores every symbol in
the universe; phase two ranks composites into percentiles and applies the
tiering gates. A single pass that tiered as it went could not know a symbol's
rank until it had seen every other symbol, and ranking is an input to the
digest cap rather than a presentation detail.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core import config
from scoring.pillars import PillarScores

log = logging.getLogger(__name__)

__all__ = [
    "ScoredSymbol",
    "composite_score",
    "rank_universe",
    "tier_for",
]


# ── the composite (D2 weights, D41 renormalization) ─────────────────────────


def composite_score(pillars: PillarScores, cfg=None) -> tuple[float | None, str]:
    """Weighted composite over SCORING pillars only. Returns (score, why).

    `why` is empty on success and names the reason on abstention, so an idea
    card can say WHY a symbol has no composite rather than leaving a blank.
    """
    scoring_cfg = cfg or config.scoring()
    weights = {
        "A": scoring_cfg.pillar_weights.technical,
        "B": scoring_cfg.pillar_weights.fundamental,
        "C": scoring_cfg.pillar_weights.catalyst,
        "D": scoring_cfg.pillar_weights.sentiment,
    }

    # D41: Technical must have scored for an idea to exist at all. It carries 40
    # points and is never genuinely unavailable for a liquid listed name, so its
    # absence means the DATA is wrong rather than the stock.
    if not pillars.technical.scored:
        return None, "technical pillar abstained — no idea without it (D41)"

    scored = pillars.scored
    if len(scored) < scoring_cfg.mapping.min_scored_pillars:
        return None, (
            f"only {len(scored)} pillar(s) scored, below "
            f"mapping.min_scored_pillars={scoring_cfg.mapping.min_scored_pillars}"
        )

    total_weight = sum(weights[p.letter] for p in scored)
    if total_weight <= 0:
        return None, "scoring pillars carry no weight"

    # RENORMALIZED: each scoring pillar's weight is divided by the weight that
    # actually participated, so the result stays on 0-100 instead of being
    # silently scaled down by the missing pillars' share.
    raw = sum(p.score * weights[p.letter] for p in scored) / total_weight

    # D2/D12: a conflicting pillar caps the composite one point below Tier A.
    # D41 scopes this to pillars that SCORED — an abstainer cannot conflict.
    if pillars.conflicting:
        capped = min(raw, scoring_cfg.composite.capped_at)
        if capped < raw:
            log.debug(
                "%s: composite capped %.1f -> %.1f by conflicting pillar(s) %s",
                pillars.symbol,
                raw,
                capped,
                ",".join(pillars.conflicting),
            )
        raw = capped

    return (
        max(scoring_cfg.composite.scale_min, min(scoring_cfg.composite.scale_max, raw)),
        "",
    )


# ── tiering (D12 + D32/D34/D41) ─────────────────────────────────────────────


def tier_for(pillars: PillarScores, composite: float | None, cfg=None) -> tuple[str | None, str]:
    """Tier A, Tier B, or None. Returns (tier, reason).

    `reason` always explains the outcome — why an idea is Tier B rather than A
    as much as why it is nothing. An idea card that says "Tier B" without
    saying what it missed cannot be argued with.

    Gate order matters: the absolute D12 gates apply BEFORE D33's ranking
    (ranking selects among qualifying ideas, it never promotes one that failed)
    and D34's diversity gate applies only to Tier A.
    """
    scoring_cfg = cfg or config.scoring()
    if composite is None:
        return None, "no composite"

    agreeing = pillars.agreeing
    a_gate, b_gate = scoring_cfg.tiers.a, scoring_cfg.tiers.b

    # ── Tier A ──────────────────────────────────────────────────────────────
    a_reasons: list[str] = []
    if composite < a_gate.min_composite:
        a_reasons.append(f"composite {composite:.1f} < {a_gate.min_composite:g}")
    if len(agreeing) < a_gate.min_pillars_agreeing:
        # D41: counted over SCORED pillars, so a two-pillar idea can reach at
        # most Tier B however high those two are.
        a_reasons.append(f"{len(agreeing)} pillar(s) agreeing < {a_gate.min_pillars_agreeing}")
    diverse, diversity_reason = _passes_diversity(agreeing)
    if not diverse:
        a_reasons.append(diversity_reason)

    if not a_reasons:
        return "A", f"Tier A: {len(agreeing)} pillars agree ({','.join(agreeing)})"

    # ── Tier B ──────────────────────────────────────────────────────────────
    # D12: "compelling asymmetry" — 2 pillars OR a composite in the band.
    in_band = b_gate.min_composite <= composite < b_gate.max_composite
    enough_agreement = len(agreeing) >= b_gate.min_pillars_agreeing
    if (in_band or enough_agreement) and composite >= scoring_cfg.min_surfaceable_composite:
        return "B", "Tier B: " + "; ".join(a_reasons)

    if composite < scoring_cfg.min_surfaceable_composite:
        # D1 / edge source #1: the engine can say "nothing today".
        return None, (
            f"below min_surfaceable_composite "
            f"({composite:.1f} < {scoring_cfg.min_surfaceable_composite:g})"
        )
    return None, "; ".join(a_reasons)


def _passes_diversity(agreeing: tuple[str, ...]) -> tuple[bool, str]:
    """D34: Tier A needs Technical PLUS Fundamental or Catalyst.

    Technical and Sentiment agreeing is one signal measured twice — price
    momentum and news tone are largely the same information arriving by two
    routes, and D12's count treats them as independent when they are not.
    """
    if "A" not in agreeing:
        return False, "D34: Technical must agree for Tier A"
    if not ({"B", "C"} & set(agreeing)):
        return False, (
            "D34: needs Fundamental or Catalyst agreeing too — Technical plus "
            "Sentiment is one signal measured twice"
        )
    return True, ""


# ── phase two: ranking (D33) ────────────────────────────────────────────────


@dataclass
class ScoredSymbol:
    """One symbol's score, rank and tier. Mutable: rank is filled in phase two."""

    symbol: str
    pillars: PillarScores
    composite: float | None
    abstain_reason: str = ""
    percentile: float | None = None
    """D33: composite percentile WITHIN the universe, 0-100."""
    rank: int | None = None
    tier: str | None = None
    tier_reason: str = ""
    surfaced: bool = False
    """Did this idea make the digest's `max_ideas_per_day` cut (D33)?"""

    @property
    def mask(self) -> str:
        return self.pillars.mask

    @property
    def pillars_scored(self) -> int:
        return len(self.pillars.scored)

    def card_line(self) -> str:
        """One line for an idea card. The mask is ALWAYS shown (D41)."""
        composite = "—" if self.composite is None else f"{self.composite:5.1f}"
        pct = "" if self.percentile is None else f" p{self.percentile:.0f}"
        return (
            f"{self.symbol:6} {composite}{pct} "
            f"[{self.mask or 'none'}] "
            f"{self.tier or '-'}  {self.tier_reason}"
        )


def rank_universe(scored: list[ScoredSymbol], cfg=None) -> list[ScoredSymbol]:
    """PHASE TWO. Assign percentiles and tiers, then cap the digest (D33).

    Percentile is over symbols that HAVE a composite. Including abstainers
    would make a mediocre score look strong simply because many symbols lacked
    data — the rank would measure coverage rather than opportunity.
    """
    scoring_cfg = cfg or config.scoring()
    with_scores = [s for s in scored if s.composite is not None]
    population = len(with_scores)

    if population:
        ordered = sorted(with_scores, key=lambda s: s.composite)
        # One ascending pass. `start` is the count STRICTLY below each score, so
        # ties share the lower percentile rather than being broken by whatever
        # order the symbols happened to arrive in. Computed from the run of
        # equal scores rather than by rescanning, which on 433 symbols is the
        # difference between 433 comparisons and 187,000.
        start = 0
        while start < population:
            end = start
            while end < population and ordered[end].composite == ordered[start].composite:
                end += 1
            percentile = 100.0 * start / population
            for item in ordered[start:end]:
                item.percentile = percentile
            start = end

        for rank, item in enumerate(reversed(ordered), start=1):
            item.rank = rank

    for item in scored:
        item.tier, item.tier_reason = tier_for(item.pillars, item.composite, scoring_cfg)

    # D33: the digest surfaces at most `max_ideas_per_day` BY RANK, and only
    # among ideas that already cleared a tier gate. Ranking never promotes an
    # idea that failed its gate.
    eligible = [s for s in scored if s.tier is not None]
    eligible.sort(key=lambda s: (-(s.composite or 0.0), s.symbol))
    for item in eligible[: scoring_cfg.ranking.max_ideas_per_day]:
        item.surfaced = True

    return scored


@dataclass
class UniverseRun:
    """What one full scoring pass produced. For the digest and the job report."""

    scored: list[ScoredSymbol] = field(default_factory=list)

    @property
    def surfaced(self) -> list[ScoredSymbol]:
        return [s for s in self.scored if s.surfaced]

    @property
    def tier_a(self) -> list[ScoredSymbol]:
        return [s for s in self.scored if s.tier == "A"]

    @property
    def tier_b(self) -> list[ScoredSymbol]:
        return [s for s in self.scored if s.tier == "B"]

    @property
    def abstained(self) -> list[ScoredSymbol]:
        return [s for s in self.scored if s.composite is None]

    def mask_histogram(self) -> dict[int, int]:
        """How many symbols scored on how many pillars. D41's visibility."""
        out: dict[int, int] = {}
        for item in self.scored:
            out[item.pillars_scored] = out.get(item.pillars_scored, 0) + 1
        return dict(sorted(out.items()))
