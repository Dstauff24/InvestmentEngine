"""Recipe 1: pullback to a rising trend. Reads features; re-measures nothing."""

from __future__ import annotations

from core import config
from features.catalyst import CatalystFeatures
from features.technical import TechnicalFeatures
from scoring.recipes.base import RecipeResult, build_plan, no_setup

NAME = "pullback_to_trend"


def evaluate(
    technical: TechnicalFeatures,
    catalyst: CatalystFeatures,
    *,
    cfg=None,
) -> RecipeResult:
    """An uptrend that has pulled back to a rising MA with RSI reset.

    The trade: buy the pullback, stop below the swing low that the pullback
    made, target the prior swing high it should reclaim, then the measured move
    beyond it.

    EARNINGS INSIDE THE WINDOW IS A HARD REFUSAL, not a score penalty. A
    2-15 day swing horizon that straddles an earnings date is a coin flip
    wearing a trade plan, and `no_earnings_within_days` is the recipe's own
    number rather than Pillar C's. Note the asymmetry with an UNKNOWN earnings
    date: that also refuses, because D19's rules cannot be satisfied by
    ignorance.
    """
    scoring_cfg = cfg or config.scoring()
    recipe = scoring_cfg.recipes.pullback_to_trend
    geometry = scoring_cfg.geometry.pullback_to_trend
    ma = technical.ma
    structure = technical.structure

    checks: dict[str, bool] = {}

    if not recipe.enabled:
        return no_setup(NAME, "recipe disabled in config")

    # ── the setup ───────────────────────────────────────────────────────────
    if ma.stacked is None:
        return no_setup(NAME, "MA stack unknown — too little history", checks)
    checks["ma_stacked"] = bool(ma.stacked)
    if recipe.require_ma_stack and not ma.stacked:
        return no_setup(NAME, "not in an uptrend (MAs not stacked)", checks)

    rising = ma.fast_rising and ma.mid_rising
    checks["ma_rising"] = bool(rising)
    if recipe.require_ma_rising and not rising:
        return no_setup(NAME, "the MAs it would pull back to are not rising", checks)

    if technical.rsi is None:
        return no_setup(NAME, "no RSI — too little history", checks)
    in_band = recipe.rsi_min <= technical.rsi <= recipe.rsi_max
    checks["rsi_reset"] = in_band
    if not in_band:
        return no_setup(
            NAME,
            f"RSI {technical.rsi:.1f} outside the {recipe.rsi_min:g}-{recipe.rsi_max:g} "
            f"reset band — not a pullback yet",
            checks,
        )

    # Price must be AT a pullback MA, not above it: the setup is the touch.
    if ma.pct_from_fast is None:
        return no_setup(NAME, "no fast MA to pull back to", checks)
    at_support = ma.pct_from_fast <= 2.0
    checks["at_pullback_ma"] = at_support
    if not at_support:
        return no_setup(
            NAME, f"price is {ma.pct_from_fast:.1f}% above the fast MA, not at it", checks
        )

    dry = technical.relative_volume is not None and (
        technical.relative_volume <= recipe.volume_dryup_ratio
    )
    checks["volume_dryup"] = dry
    if technical.relative_volume is not None and not dry:
        return no_setup(
            NAME,
            f"pullback volume {technical.relative_volume:.2f}x is not the dry-up "
            f"below {recipe.volume_dryup_ratio:g}x the recipe wants",
            checks,
        )

    # ── the event refusal ───────────────────────────────────────────────────
    days = catalyst.days_to_earnings
    clear = days is not None and days > recipe.no_earnings_within_days
    checks["earnings_clear"] = clear
    if not clear:
        return no_setup(
            NAME,
            (
                f"earnings in {days} day(s), inside the "
                f"{recipe.no_earnings_within_days}-day exclusion"
            )
            if days is not None
            else "no earnings date on record — the exclusion cannot be verified",
            checks,
        )

    # ── the levels ──────────────────────────────────────────────────────────
    if structure.swing_high is None or structure.swing_low is None:
        return no_setup(NAME, "no swing structure to hang a stop or target on", checks)
    if technical.close is None:
        return no_setup(NAME, "no close", checks)

    reclaim = structure.swing_high
    distance = reclaim - technical.close
    if distance <= 0:
        return no_setup(
            NAME,
            f"already at or above the swing high it would reclaim "
            f"({technical.close:.2f} vs {reclaim:.2f})",
            checks,
        )

    plan, why = build_plan(
        entry_reference=technical.close,
        structural_stop=structure.swing_low,
        target_1=reclaim,
        target_2=technical.close + distance * geometry.tp2_measured_move,
        features=technical,
        horizon_days=tuple(recipe.horizon_days),
        cfg=scoring_cfg,
    )
    if plan is None:
        return no_setup(NAME, f"setup fired but the geometry is unusable: {why}", checks)

    return RecipeResult(
        recipe=NAME,
        triggered=True,
        reason=(
            f"uptrend pullback to the {recipe.ma_fast}MA, RSI {technical.rsi:.1f} reset, "
            f"reclaim {reclaim:.2f}"
        ),
        plan=plan,
        checks=checks,
    )
