"""The three Phase 1 recipes (D-recipes 1-3), built on scored pillars.

Each `evaluate()` reads FEATURES and SCORES and re-measures nothing: every
level a plan hangs on was computed once in `features/`. Each returns a
`RecipeResult` carrying either a `TradePlan` (entry zone, stop, TP1, TP2, and
the R:R that follows from them) or the first condition that failed.
"""

from __future__ import annotations

from scoring.recipes import breakout, drift, pullback
from scoring.recipes.base import RecipeResult, TradePlan

ALL = (pullback.NAME, breakout.NAME, drift.NAME)

__all__ = ["ALL", "RecipeResult", "TradePlan", "breakout", "drift", "evaluate_all", "pullback"]


def evaluate_all(technical, fundamental, catalyst, *, cfg=None) -> list[RecipeResult]:
    """Every recipe against one symbol, in decision-log order.

    Takes FEATURES, not pillar scores. A recipe asks "does this setup exist",
    which is a question about the measurements; whether the pillars AGREE is
    D12's tiering question and is answered separately. Passing scores in here
    would let a mapping-scale change silently move a recipe's trigger.
    """
    return [
        pullback.evaluate(technical, catalyst, cfg=cfg),
        breakout.evaluate(technical, cfg=cfg),
        drift.evaluate(technical, fundamental, catalyst, cfg=cfg),
    ]
