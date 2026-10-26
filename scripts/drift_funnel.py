"""Tonight's `post_earnings_drift` funnel, stage by stage. READ-ONLY.

    python -m scripts.drift_funnel                 # today's session
    python -m scripts.drift_funnel --date 2026-10-26

Answers a question the digest cannot: of the whole universe, how many symbols
even had a report to judge tonight, and where did the rest fall out? The digest
reports the SLOT WALK's stages (plan, tier, risk_reward), and those begin after
a recipe has already declined — so a night with no drift calls looks identical
whether no reaction window completed or every beat failed to hold.

**IT RE-RUNS PHASE ONE AND READS THE RECIPE'S OWN `checks`.** Nothing here
re-implements a stage, so the counts cannot drift from what the engine decided.

**WRITE-FREE ON PURPOSE**, so it is safe to point at a live engine host: it
calls `score_symbol`, not `score_universe`, because the latter records a
`daily_scoring_stats` row and a diagnostic must not rewrite the evening's
record — even idempotently. The one consequence is that no sector composites
are built, so PILLAR scores from this path are not comparable to the pass's;
the RECIPE checks are identical, because the drift recipe reads the reaction
window, the gap and the beat, none of which touch relative strength.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import date

from core import clock, config, db
from features import sentiment as sentiment_features
from scoring.runner import score_symbol

log = logging.getLogger(__name__)

RECIPE = "post_earnings_drift"

UNIVERSE_SQL = "SELECT symbol FROM v_active_universe ORDER BY symbol"


def funnel(day: date, named: list | None = None) -> Counter:
    """{stage: count} over the D14 universe, from the recipe's own checks.

    `named` collects one row per symbol whose reaction window COMPLETED — the
    judgeable set, which is small enough to print and is the only population a
    reader can act on. Its composite tier is included because a plan on a
    symbol with no tier is the D53 case: certified, plan-bearing, and invisible
    to the old slot walk.
    """
    scoring = config.scoring()
    gate = scoring.certification_of(RECIPE).min_risk_reward
    tally: Counter = Counter()
    tally["rr_gate"] = gate

    coverage = sentiment_features.news_coverage_start()
    for row in db.fetch_all(UNIVERSE_SQL):
        item = score_symbol(row["symbol"], as_of=day, sector_closes=None, coverage_start=coverage)
        tally["scored"] += 1
        result = next((r for r in item.recipes if r.recipe == RECIPE), None)
        if result is None:
            tally["recipe_not_evaluated"] += 1
            continue
        checks = result.checks or {}

        if not checks.get("has_knowable_report"):
            tally["no_knowable_report"] += 1
            continue
        tally["report_knowable"] += 1

        if not checks.get("reaction_window_complete"):
            tally["window_incomplete"] += 1
            continue
        tally["window_complete"] += 1
        if named is not None:
            rr = result.plan.risk_reward if result.plan else None
            named.append(
                {
                    "symbol": item.symbol,
                    "composite": item.composite,
                    "tier": item.tier,
                    "beat": bool(checks.get("eps_beat_verified")),
                    "held": bool(checks.get("reacted_up") and checks.get("holds_reaction_low")),
                    "risk_reward": rr,
                }
            )

        if checks.get("eps_beat_verified"):
            tally["verified_eps_beat"] += 1
        if checks.get("revenue_beat_verified"):
            tally["verified_revenue_beat"] += 1
        if checks.get("reacted_up") and checks.get("holds_reaction_low"):
            tally["gap_and_hold"] += 1
        elif not checks.get("reacted_up"):
            tally["no_up_reaction"] += 1
        else:
            tally["lost_the_reaction_low"] += 1

        if result.plan is None:
            tally["no_plan"] += 1
            continue
        tally["plan"] += 1
        rr = result.plan.risk_reward
        if rr is not None and rr >= gate:
            tally["cleared_rr"] += 1
        else:
            tally["refused_rr"] += 1
    return tally


def render(day: date, tally: Counter) -> str:
    def n(key: str) -> int:
        return int(tally.get(key, 0))

    return "\n".join(
        [
            f"{RECIPE} funnel — session {day}",
            f"  scored universe                 {n('scored')}",
            f"    recipe not evaluated          {n('recipe_not_evaluated')}",
            f"    no knowable report at all     {n('no_knowable_report')}",
            f"  report knowable                 {n('report_knowable')}",
            f"    reaction window incomplete    {n('window_incomplete')}",
            f"  REACTION WINDOW COMPLETE        {n('window_complete')}"
            "   <- tonight's judgeable set",
            f"    verified EPS beat             {n('verified_eps_beat')}",
            f"    verified revenue beat         {n('verified_revenue_beat')}",
            f"    gap up AND held the low       {n('gap_and_hold')}",
            f"    reaction was not up           {n('no_up_reaction')}",
            f"    gapped up, lost the low       {n('lost_the_reaction_low')}",
            f"  produced a PLAN                 {n('plan')}",
            f"    no plan (a stage declined)    {n('no_plan')}",
            f"    cleared R:R >= {tally.get('rr_gate')}            {n('cleared_rr')}",
            f"    refused by R:R                {n('refused_rr')}",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"{RECIPE} funnel (read-only).")
    parser.add_argument("--date", help="ISO session date; default today's session")
    args = parser.parse_args(argv)
    day = date.fromisoformat(args.date) if args.date else clock.session_date()
    named: list = []
    print(render(day, funnel(day, named)))
    if named:
        gate = config.scoring().certification_of(RECIPE).min_risk_reward
        print("\n  judgeable set, by symbol:")
        for row in sorted(named, key=lambda r: -(r["risk_reward"] or 0)):
            rr = row["risk_reward"]
            verdict = (
                "no plan"
                if rr is None
                else ("clears the gate" if rr >= gate else f"REFUSED by R:R < {gate:g}")
            )
            tier = row["tier"] or "NO TIER (D53: invisible to the old slot walk)"
            print(
                f"    {row['symbol']:6} composite {row['composite']:.1f}  tier {tier}  "
                f"beat {row['beat']}  held {row['held']}  "
                f"R:R {'—' if rr is None else f'{rr:.2f}'}  {verdict}"
            )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    sys.exit(main())
