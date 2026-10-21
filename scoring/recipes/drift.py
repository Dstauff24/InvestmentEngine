"""Recipe 3: post-earnings drift. Reads features; re-measures nothing."""

from __future__ import annotations

from core import config
from features.catalyst import CatalystFeatures
from features.fundamental import FundamentalFeatures
from features.technical import TechnicalFeatures
from scoring.recipes.base import RecipeResult, build_plan, no_setup

NAME = "post_earnings_drift"


def evaluate(
    technical: TechnicalFeatures,
    fundamental: FundamentalFeatures,
    catalyst: CatalystFeatures,
    *,
    cfg=None,
) -> RecipeResult:
    """A beat-and-raise that gapped up and is holding its gap.

    The trade: buy the hold above the gap day's low, stop below that low
    (a fill invalidates the whole premise), target multiples of the gap itself
    — the market's own statement about what the news was worth.

    WHAT THIS RECIPE CANNOT YET CHECK, and says so rather than pretending:
    `require_raised_guidance` has no data source. Guidance text is in neither
    the FMP endpoints the client calls nor the event rows, so the check is
    reported UNVERIFIED in `checks` rather than silently passed. A recipe that
    quietly skips one of its three stated conditions is a different recipe.
    """
    scoring_cfg = cfg or config.scoring()
    recipe = scoring_cfg.recipes.post_earnings_drift
    geometry = scoring_cfg.geometry.post_earnings_drift
    structure = technical.structure

    checks: dict[str, bool] = {}
    if not recipe.enabled:
        return no_setup(NAME, "recipe disabled in config")

    since = catalyst.days_since_earnings
    recent = since is not None and since <= recipe.max_days_since_earnings
    checks["recent_earnings"] = recent
    if not recent:
        return no_setup(
            NAME,
            (
                f"last earnings {since} day(s) ago, past the "
                f"{recipe.max_days_since_earnings}-day drift window"
            )
            if since is not None
            else "no past earnings date on record",
            checks,
        )

    if recipe.require_eps_beat or recipe.require_revenue_beat:
        # Growth is the proxy the stored data supports; the beat/miss flags
        # themselves live on `events` rows that the calendars do not reliably
        # populate with actuals until days after the print.
        beat = fundamental.eps_growth_yoy is not None and fundamental.eps_growth_yoy > 0
        checks["eps_growing"] = beat
        if not beat:
            return no_setup(
                NAME,
                "no positive EPS growth to drift on"
                if fundamental.eps_growth_yoy is not None
                else "no EPS history — the beat cannot be verified",
                checks,
            )

    # Stated but unverifiable. Recorded as a FAILED check, because an
    # unverified condition is not a satisfied one, and the reason names it.
    if recipe.require_raised_guidance:
        checks["raised_guidance_UNVERIFIED"] = False

    gap = structure.gap_up_pct
    gapped = gap is not None and gap >= recipe.min_gap_up_pct
    checks["gapped_up"] = gapped
    if not gapped:
        return no_setup(
            NAME,
            (
                f"gap of {gap:.2f}% is under the {recipe.min_gap_up_pct:g}% minimum"
                if gap is not None
                else "no gap measurable"
            ),
            checks,
        )

    if structure.gap_day_low is None or technical.close is None:
        return no_setup(NAME, "no gap-day low to stop beneath", checks)

    holding = technical.close > structure.gap_day_low
    checks["holds_gap_low"] = holding
    if recipe.require_holds_day_one_low and not holding:
        return no_setup(
            NAME,
            f"closed {technical.close:.2f} below the gap-day low "
            f"{structure.gap_day_low:.2f} — the gap is filling",
            checks,
        )

    if structure.prior_close is None:
        return no_setup(NAME, "no prior close to measure the gap from", checks)
    gap_points = technical.close - structure.prior_close
    if gap_points <= 0:
        return no_setup(NAME, "the gap has already closed in points", checks)

    plan, why = build_plan(
        entry_reference=technical.close,
        structural_stop=structure.gap_day_low,
        target_1=technical.close + gap_points * geometry.tp1_gap_multiple,
        target_2=technical.close + gap_points * geometry.tp2_gap_multiple,
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
            f"gapped {gap:.1f}% {since} day(s) after earnings and holding "
            f"{structure.gap_day_low:.2f} (guidance UNVERIFIED)"
        ),
        plan=plan,
        checks=checks,
    )
