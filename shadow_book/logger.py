"""Log surfaced ideas as Shadow Book calls (D13/D40/D42).

ONLY SURFACED IDEAS ARE LOGGED (D42). An idea that cleared a tier gate but did
not rank into the digest rolls into `daily_scoring_stats` instead — on the live
universe that is 185 of 190, and tracking all of them would make the Shadow
Book a record of the engine's COVERAGE rather than its JUDGEMENT.

THE PROJECTIONS ARE FROZEN. Every `proj_*` column is written once and never
updated: the whole point is measuring reality against what we said we expected,
and a projection that drifts toward the outcome measures nothing. The tracker
therefore writes to `current_stop`, never to `proj_stop`.

WHAT TRAVELS WITH THE CALL, and why each one would otherwise be unrecoverable:

  * `status_at_call` (D40) — expectancy mixed across a promotion boundary
    averages two different strategies.
  * `scored_pillars` / `pillars_scored` (D41) — a composite of 72 from two
    pillars is not the claim 72 from four is.
  * `sentiment_scorer` — a lexicon and a Claude scorer are differently
    calibrated instruments.
  * `gates_in_force` — a threshold change must not silently reinterpret
    yesterday's calls.
  * `regime_at_call` (D45) — the ladder is evaluated per recipe x REGIME, and
    the benchmark series gets revised, so a regime derived later would move an
    old call between cells underneath a promotion decision already made.
"""

from __future__ import annotations

import json
import logging
from datetime import date

from core import clock, config, db
from decision.idea_builder import Idea

log = logging.getLogger(__name__)

__all__ = ["gates_in_force", "log_idea", "log_surfaced"]


INSERT = """
    INSERT INTO shadow_book (
        symbol, origin, tier, recipe, strategy, called_on,
        proj_entry_low, proj_entry_high, proj_stop, proj_target_1, proj_target_2,
        proj_risk_reward, proj_confidence, proj_horizon_days,
        proj_trail_ma, proj_trail_confirm_closes,
        was_taken, outcome,
        status_at_call, regime_at_call, scored_pillars, pillars_scored,
        sentiment_scorer, gates_in_force,
        current_stop, notes
    ) VALUES (
        %(symbol)s, 'engine', %(tier)s, %(recipe)s, %(strategy)s, %(called_on)s,
        %(proj_entry_low)s, %(proj_entry_high)s, %(proj_stop)s,
        %(proj_target_1)s, %(proj_target_2)s, %(proj_risk_reward)s,
        %(proj_confidence)s, %(proj_horizon_days)s,
        %(proj_trail_ma)s, %(proj_trail_confirm_closes)s,
        FALSE, 'open',
        %(status_at_call)s, %(regime_at_call)s, %(scored_pillars)s, %(pillars_scored)s,
        %(sentiment_scorer)s, %(gates_in_force)s,
        %(current_stop)s, %(notes)s
    )
    RETURNING id
"""

# One call per symbol per recipe per day. A re-run of the pipeline must not
# duplicate a call, and two recipes firing on one symbol are two genuinely
# different plans — so the key is (symbol, recipe, called_on), not symbol alone.
EXISTS = """
    SELECT id FROM shadow_book
    WHERE  symbol = %s AND recipe = %s AND called_on = %s AND origin = 'engine'
"""


def gates_in_force() -> dict:
    """The thresholds that produced today's calls, for the row to carry.

    Recorded so a later change cannot silently reinterpret an old call. Kept
    deliberately small: the gates that DECIDE whether an idea exists, not the
    whole config, because a dump of everything is a thing nobody reads.
    """
    s = config.scoring()
    return {
        "tier_a_min_composite": s.tiers.a.min_composite,
        "tier_a_min_pillars": s.tiers.a.min_pillars_agreeing,
        "tier_a_min_rr": s.tiers.a.min_risk_reward,
        "tier_b_min_composite": s.tiers.b.min_composite,
        "tier_b_max_composite": s.tiers.b.max_composite,
        "tier_b_min_rr": s.tiers.b.min_risk_reward,
        "pillar_agreement_min_score": s.pillar_agreement.min_score,
        "conflict_below": s.composite.conflict_below,
        "max_ideas_per_day": s.ranking.max_ideas_per_day,
        "pillar_weights": s.pillar_weights.model_dump(),
    }


def regime_at(day: date) -> str:
    """The market regime for a session (D45), from the benchmark series.

    Reads `backtest/regime.py` — the same code the backtest tags trades with,
    so a live call and a simulated one land in the same cell. The alternative
    was a second implementation of "what was the market doing", which would
    make the two sets of expectancy numbers incomparable exactly where D45
    needs them compared.

    Returns `unknown` rather than guessing when the benchmark has fewer than
    200 sessions, or when SPY is missing entirely — a regime nobody can
    compute is not a regime, and `unknown` groups honestly.
    """
    from backtest.regime import load_benchmark, regime_series

    try:
        return regime_series(load_benchmark(as_of=day)).tag(day)
    except Exception as exc:
        # A missing benchmark must not stop a call being logged: the idea is
        # the valuable thing and the regime is an attribute of it.
        log.warning("regime unavailable for %s: %s", day, exc)
        return "unknown"


def log_idea(
    idea: Idea,
    *,
    sentiment_scorer: str | None = None,
    as_of: date | None = None,
    strategy: str = "swing",
    regime: str | None = None,
) -> int | None:
    """Write one call. Returns its id, or None if it was already logged today.

    Idempotent on (symbol, recipe, called_on): the pipeline can be re-run, and
    a duplicated call would corrupt expectancy by counting one opinion twice.
    """
    day = as_of or clock.session_date()
    existing = db.fetch_one(EXISTS, (idea.symbol, idea.recipe, day))
    if existing:
        log.info("%s/%s already logged for %s", idea.symbol, idea.recipe, day)
        return None

    row = db.fetch_one(
        INSERT,
        {
            "symbol": idea.symbol,
            "tier": idea.tier,
            "recipe": idea.recipe,
            "strategy": strategy,
            "called_on": day,
            "proj_entry_low": idea.entry_low,
            "proj_entry_high": idea.entry_high,
            "proj_stop": idea.stop,
            "proj_target_1": idea.target_1,
            "proj_target_2": idea.target_2,
            "proj_risk_reward": idea.risk_reward,
            # The composite IS the confidence claim (D39 will calibrate it
            # against realised hit rate), so it is stored as such rather than
            # invented separately.
            "proj_confidence": idea.composite,
            "proj_horizon_days": idea.horizon_days[1],
            # The remainder's exit rule, frozen with the call (O20).
            "proj_trail_ma": getattr(idea, "trail_ma", None),
            "proj_trail_confirm_closes": getattr(idea, "trail_confirm", None),
            "status_at_call": idea.status,
            "regime_at_call": regime if regime is not None else regime_at(day),
            "scored_pillars": idea.mask,
            "pillars_scored": len(idea.mask.split(",")) if idea.mask else 0,
            "sentiment_scorer": sentiment_scorer,
            "gates_in_force": json.dumps(gates_in_force()),
            # Starts at the projected stop and moves from there.
            "current_stop": idea.stop,
            "notes": "; ".join(idea.notes) or None,
        },
    )
    log.info(
        "logged %s %s Tier %s [%s] status=%s regime=%s R:R %.2f",
        idea.symbol,
        idea.recipe,
        idea.tier,
        idea.mask,
        idea.status,
        regime if regime is not None else "(computed)",
        idea.risk_reward,
    )
    return row["id"] if row else None


def log_surfaced(
    ideas: list[Idea], *, sentiment_scorer: str | None = None, as_of: date | None = None
) -> int:
    """Log every surfaced idea. Returns how many were newly written.

    The regime is computed ONCE for the day and passed to each call: it is a
    fact about the market, not about the idea, and reading the whole benchmark
    series per idea would be the same answer bought five times.
    """
    day = as_of or clock.session_date()
    regime = regime_at(day)
    written = 0
    for idea in ideas:
        if log_idea(idea, sentiment_scorer=sentiment_scorer, as_of=day, regime=regime) is not None:
            written += 1
    return written
