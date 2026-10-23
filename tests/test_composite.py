"""Composite, ranking and tiering (D2/D12/D32/D33/D34/D41). No DB.

D41's consequences are what these mostly pin: renormalization, a two-pillar
idea capping at Tier B, and the conflict cap applying only to scored pillars.
"""

from __future__ import annotations

import pytest

from core import config
from scoring.composite import (
    ScoredSymbol,
    UniverseRun,
    composite_score,
    rank_universe,
    tier_for,
)
from scoring.pillars import PillarScore, PillarScores

CFG = config.scoring()


def pillars(a=None, b=None, c=None, d=None, symbol="TEST") -> PillarScores:
    """Pillar scores straight from numbers. None means abstained."""
    return PillarScores(
        symbol=symbol,
        technical=PillarScore("A", a, {"x": a} if a is not None else {}),
        fundamental=PillarScore("B", b, {"x": b} if b is not None else {}),
        catalyst=PillarScore("C", c, {"x": c} if c is not None else {}),
        sentiment=PillarScore("D", d, {"x": d} if d is not None else {}),
    )


# ══════════════════════════════════════════════════════════════════════════
# D41: renormalization
# ══════════════════════════════════════════════════════════════════════════


def test_all_four_pillars_use_the_d32_weights():
    """40/30/20/10 of 80/80/80/80 is 80."""
    score, why = composite_score(pillars(80, 80, 80, 80))
    assert score == pytest.approx(80.0)
    assert why == ""


def test_the_weights_are_renormalized_over_scoring_pillars():
    """THE D41 rule. A + B only: 40/70 and 30/70, not 40/100 and 30/100.

    Without renormalization an idea scoring 80 on both would composite to 56
    and be penalised for data it never claimed to have.
    """
    score, _ = composite_score(pillars(80, 80, None, None))
    assert score == pytest.approx(80.0), "renormalized, not scaled down to 56"


def test_renormalization_preserves_the_relative_weight_of_the_survivors():
    """A at 100 and B at 0, renormalized: 40/(40+30) = 57.1."""
    score, _ = composite_score(pillars(100, 0, None, None))
    assert score == pytest.approx(100 * 40 / 70, abs=0.1)


def test_an_abstaining_pillar_is_not_scored_fifty():
    """Scoring it 50 would INVENT evidence that the pillar was neutral."""
    renormalized, _ = composite_score(pillars(80, 80, None, None))
    as_if_fifty = (80 * 40 + 80 * 30 + 50 * 20 + 50 * 10) / 100
    assert renormalized == pytest.approx(80.0)
    assert as_if_fifty == pytest.approx(71.0)
    assert renormalized != pytest.approx(as_if_fifty)


def test_technical_abstaining_means_no_idea_at_all():
    """D41: Technical carries 40 points and is never genuinely unavailable for
    a liquid listed name, so its absence means the DATA is wrong."""
    score, why = composite_score(pillars(None, 95, 95, 95))
    assert score is None
    assert "technical" in why.lower()


def test_too_few_scoring_pillars_abstains():
    score, why = composite_score(pillars(90, None, None, None))
    assert score is None
    assert "min_scored_pillars" in why


# ── the conflict cap, scoped to scored pillars ──────────────────────────────


def test_a_conflicting_pillar_caps_the_composite_below_tier_a():
    high = pillars(95, 95, 95, 10)  # D conflicts (<= 30)
    score, _ = composite_score(high)
    assert score == pytest.approx(CFG.composite.capped_at)
    assert score < CFG.tiers.a.min_composite


def test_an_ABSTAINING_pillar_does_not_cap():
    """D41: silence is not an objection. A demoted-but-present Sentiment can
    veto (D32); a missing one cannot."""
    score, _ = composite_score(pillars(95, 95, 95, None))
    assert score > CFG.composite.capped_at


def test_the_cap_only_lowers_never_raises():
    score, _ = composite_score(pillars(40, 40, 40, 10))
    assert score < 50, "a conflicted weak idea is not lifted to the cap"


# ══════════════════════════════════════════════════════════════════════════
# D12 + D34 + D41 tiering
# ══════════════════════════════════════════════════════════════════════════


def test_three_agreeing_pillars_and_a_high_composite_is_tier_a():
    scores = pillars(90, 90, 90, 90)
    composite, _ = composite_score(scores)
    tier, reason = tier_for(scores, composite)
    assert tier == "A"
    assert "Tier A" in reason


def test_a_two_pillar_idea_can_reach_at_most_tier_b():
    """THE D41 consequence the decision calls out explicitly: however high the
    two pillars are, D12's count binds on SCORED pillars."""
    scores = pillars(100, 100, None, None)
    composite, _ = composite_score(scores)
    assert composite == pytest.approx(100.0)
    tier, reason = tier_for(scores, composite)
    assert tier == "B"
    assert "agreeing" in reason


def test_d34_refuses_tier_a_on_technical_plus_sentiment_alone():
    """Technical and Sentiment agreeing is ONE signal measured twice — price
    momentum and news tone are the same information by two routes."""
    scores = pillars(90, 40, 40, 90)  # A and D agree; B and C do not
    composite, _ = composite_score(scores)
    tier, reason = tier_for(scores, composite)
    assert tier != "A"
    assert "one signal measured twice" in reason


def test_d34_accepts_technical_plus_fundamental():
    scores = pillars(90, 90, 40, 90)
    composite, _ = composite_score(scores)
    assert tier_for(scores, composite)[0] == "A"


def test_d34_accepts_technical_plus_catalyst():
    scores = pillars(90, 40, 90, 90)
    composite, _ = composite_score(scores)
    assert tier_for(scores, composite)[0] == "A"


def test_d34_requires_technical_itself_to_agree():
    """Not merely to have scored — to agree."""
    scores = pillars(50, 90, 90, 90)
    composite, _ = composite_score(scores)
    tier, reason = tier_for(scores, composite)
    assert tier != "A"
    assert "Technical must agree" in reason


def test_a_weak_idea_surfaces_nothing():
    """D1 / edge source #1: the engine is allowed to say "nothing today"."""
    scores = pillars(40, 40, 40, 40)
    composite, _ = composite_score(scores)
    tier, reason = tier_for(scores, composite)
    assert tier is None
    assert "min_surfaceable" in reason


def test_the_tier_reason_always_says_what_was_missed():
    """ "Tier B" without saying what it missed cannot be argued with."""
    scores = pillars(100, 100, None, None)
    composite, _ = composite_score(scores)
    _, reason = tier_for(scores, composite)
    assert reason and reason != "Tier B: "


# ══════════════════════════════════════════════════════════════════════════
# D33: phase two ranking
# ══════════════════════════════════════════════════════════════════════════


def build(symbol: str, a, b, c, d) -> ScoredSymbol:
    scores = pillars(a, b, c, d, symbol=symbol)
    composite, reason = composite_score(scores)
    return ScoredSymbol(symbol=symbol, pillars=scores, composite=composite, abstain_reason=reason)


def test_percentiles_are_assigned_across_the_universe():
    universe = [build(f"S{i}", 40 + i * 5, 50, 50, 50) for i in range(10)]
    rank_universe(universe)
    assert universe[0].percentile == pytest.approx(0.0)
    assert universe[-1].percentile == pytest.approx(90.0)
    assert universe[-1].rank == 1


def test_ties_share_the_lower_percentile():
    """Rather than being broken by whatever order the symbols arrived in."""
    universe = [build(f"S{i}", 80, 80, 80, 80) for i in range(4)]
    rank_universe(universe)
    assert {s.percentile for s in universe} == {0.0}


def test_abstainers_are_excluded_from_the_percentile_population():
    """Including them would make a mediocre score look strong simply because
    many symbols lacked data — the rank would measure coverage."""
    universe = [build("GOOD", 90, 90, 90, 90), build("MID", 60, 60, 60, 60)]
    universe += [build(f"NONE{i}", None, 90, 90, 90) for i in range(50)]
    rank_universe(universe)
    good = next(s for s in universe if s.symbol == "GOOD")
    mid = next(s for s in universe if s.symbol == "MID")
    assert good.percentile == pytest.approx(50.0), "2 scored symbols, not 52"
    assert mid.percentile == pytest.approx(0.0)
    assert all(s.percentile is None for s in universe if s.symbol.startswith("NONE"))


def test_ranking_never_promotes_an_idea_that_failed_its_gate():
    """D33: ranking selects AMONG qualifying ideas."""
    universe = [build(f"W{i}", 40, 40, 40, 40) for i in range(5)]
    rank_universe(universe)
    assert all(s.tier is None for s in universe)
    assert all(not s.surfaced for s in universe)


def test_ranking_no_longer_decides_what_surfaces():
    """D42 AMENDED (Oct 23, 2026). `rank_universe` used to mark the top
    `max_ideas_per_day` tier-passers as surfaced right here, BEFORE any plan
    was built — so five composite leaders with no recipe plan took all five
    slots and every plan-bearing idea below them was discarded unseen.

    Phase two now ranks and tiers; `decision.idea_builder.assign_slots` walks
    that ranking and surfaces only what passes every stage."""
    cap = CFG.ranking.max_ideas_per_day
    universe = [build(f"S{i}", 95 - i, 95 - i, 95 - i, 95 - i) for i in range(cap + 4)]
    rank_universe(universe)

    assert [s.rank for s in universe] == list(range(1, len(universe) + 1))
    assert all(s.tier is not None for s in universe[:cap]), "they still qualify"
    assert not any(s.surfaced for s in universe), (
        "nothing surfaces until a plan has been built and every stage has run"
    )


def test_the_run_summary_reports_the_mask_histogram():
    """D41's visibility: how many symbols scored on how many pillars."""
    run = UniverseRun(
        scored=[
            build("FOUR", 80, 80, 80, 80),
            build("THREE", 80, 80, 80, None),
            build("TWO", 80, 80, None, None),
        ]
    )
    rank_universe(run.scored)
    assert run.mask_histogram() == {2: 1, 3: 1, 4: 1}


def test_the_card_line_always_shows_the_mask():
    """D41: the abstention mask is printed on EVERY idea card."""
    item = build("TEST", 80, 80, None, None)
    rank_universe([item])
    line = item.card_line()
    assert "TEST" in line
    assert "[A,B]" in line
