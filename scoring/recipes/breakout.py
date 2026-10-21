"""Recipe 2: base breakout. Reads features; re-measures nothing."""

from __future__ import annotations

from core import config
from features.technical import TechnicalFeatures
from scoring.recipes.base import RecipeResult, build_plan, no_setup

NAME = "base_breakout"


def evaluate(technical: TechnicalFeatures, *, cfg=None) -> RecipeResult:
    """A multi-week base with contracting range, breaking out on volume.

    The trade: buy the break of the base high plus a buffer, stop below the
    base low, target the measured move (base depth projected from the break).

    RELATIVE STRENGTH IS READ FROM THE FEATURE, not from Pillar A's SCORE and
    not re-measured. The feature (`rs_vs_sector`, in percentage points under
    D33) is the raw reading; the pillar score is that reading already passed
    through `mapping.technical.rs_full_credit_pct`. Gating on the score would
    couple this recipe's "does it lead its sector" question to a scale chosen
    for a different purpose, so that a change to the mapping silently moved the
    recipe's trigger. The raw sign is what the recipe actually means.
    """
    scoring_cfg = cfg or config.scoring()
    recipe = scoring_cfg.recipes.base_breakout
    geometry = scoring_cfg.geometry.base_breakout
    structure = technical.structure

    checks: dict[str, bool] = {}
    if not recipe.enabled:
        return no_setup(NAME, "recipe disabled in config")

    if structure.base_high is None or structure.base_low is None:
        return no_setup(NAME, "no base structure — too little history", checks)
    if technical.close is None or technical.atr is None:
        return no_setup(NAME, "no close or ATR", checks)

    long_enough = (structure.base_weeks or 0) >= recipe.min_base_weeks
    checks["base_long_enough"] = long_enough
    if not long_enough:
        return no_setup(
            NAME,
            f"base is {structure.base_weeks:.0f} week(s), under the "
            f"{recipe.min_base_weeks}-week minimum",
            checks,
        )

    shallow = (
        structure.base_depth_pct is not None
        and structure.base_depth_pct <= recipe.max_base_depth_pct
    )
    checks["base_shallow_enough"] = shallow
    if not shallow:
        return no_setup(
            NAME,
            f"base is {structure.base_depth_pct:.1f}% deep, over the "
            f"{recipe.max_base_depth_pct:g}% cap — a deep base is a downtrend in "
            f"a base's clothing",
            checks,
        )

    # RANGE CONTRACTION. The config asks for it (`range_contraction_ratio`) and
    # the first version of this recipe silently ignored the parameter — a base
    # that is TIGHTENING is the one that breaks cleanly, while a base at full
    # width is still a range.
    contracted = (
        structure.base_contraction is not None
        and structure.base_contraction <= recipe.range_contraction_ratio
    )
    checks["range_contracted"] = contracted
    if not contracted:
        measured = (
            f"{structure.base_contraction:.2f}"
            if structure.base_contraction is not None
            else "unknown"
        )
        return no_setup(
            NAME,
            f"range contraction {measured} is above the "
            f"{recipe.range_contraction_ratio:g} the recipe wants — the base is "
            f"still at full width, not tightening",
            checks,
        )

    # The break level: the base high plus a buffer, so a wick through the high
    # is not a breakout.
    trigger = structure.base_high * (1 + recipe.breakout_buffer_pct / 100.0)
    broke = technical.close >= trigger
    checks["broke_out"] = broke
    if not broke:
        return no_setup(
            NAME,
            f"has not broken out: {technical.close:.2f} below the "
            f"{trigger:.2f} trigger ({recipe.breakout_buffer_pct:g}% over the base high)",
            checks,
        )

    confirmed = (
        technical.relative_volume is not None
        and technical.relative_volume >= recipe.min_relative_volume_on_break
    )
    checks["volume_confirms"] = confirmed
    if not confirmed:
        volume = (
            f"{technical.relative_volume:.2f}x"
            if technical.relative_volume is not None
            else "unknown"
        )
        return no_setup(
            NAME,
            f"breakout volume {volume} is under the "
            f"{recipe.min_relative_volume_on_break:g}x the recipe requires — an "
            f"unconfirmed break is the one that fails",
            checks,
        )

    if recipe.require_relative_strength_vs_sector:
        # Pillar A's reading (D33), not a second measurement of the same thing.
        rs = technical.rs_vs_sector
        leading = rs is not None and rs > 0
        checks["leads_its_sector"] = leading
        if not leading:
            return no_setup(
                NAME,
                (
                    f"lags its sector by {abs(rs):.1f}pp"
                    if rs is not None
                    else "no sector composite to measure relative strength against"
                ),
                checks,
            )

    depth = structure.base_high - structure.base_low
    plan, why = build_plan(
        entry_reference=technical.close,
        structural_stop=structure.base_low,
        target_1=technical.close + depth * geometry.tp1_depth_multiple,
        target_2=technical.close + depth * geometry.tp2_depth_multiple,
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
            f"{structure.base_weeks:.0f}-week base, {structure.base_depth_pct:.1f}% deep, "
            f"broke {trigger:.2f} on {technical.relative_volume:.2f}x volume"
        ),
        plan=plan,
        checks=checks,
    )
