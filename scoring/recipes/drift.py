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
        failed = (
            _verified_beat(recipe, catalyst, checks)
            if recipe.beat_source == "verified"
            else _growth_proxy_beat(fundamental, checks)
        )
        if failed is not None:
            return no_setup(NAME, failed, checks)

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
            f"gapped {gap:.1f}% {since} day(s) after earnings, {catalyst.surprise.verdict}, "
            f"holding {structure.gap_day_low:.2f} (guidance UNVERIFIED)"
        ),
        plan=plan,
        checks=checks,
    )


def _growth_proxy_beat(fundamental: FundamentalFeatures, checks: dict) -> str | None:
    """Year-over-year EPS growth as a stand-in for a beat. Returns the refusal.

    What the recipe had to do while the stored earnings rows carried no
    actuals. It answers a DIFFERENT question — growth is about the trend, a
    beat is about the surprise — and a company can grow 20% year on year while
    missing the quarter the drift premise rests on.
    """
    growing = fundamental.eps_growth_yoy is not None and fundamental.eps_growth_yoy > 0
    checks["eps_growing"] = growing
    if growing:
        return None
    return (
        "no positive EPS growth to drift on"
        if fundamental.eps_growth_yoy is not None
        else "no EPS history — the beat cannot be verified"
    )


def _verified_beat(recipe, catalyst: CatalystFeatures, checks: dict) -> str | None:
    """The reported actual against the consensus estimate (O12).

    **UNVERIFIABLE IS A REFUSAL, NOT A PASS.** A missing actual or estimate
    means the beat cannot be established, and the standing `features/`
    convention makes that `None` rather than False — the same asymmetry
    `pullback_to_trend` already applies to an unknown earnings date, where
    D19's rules cannot be satisfied by ignorance.
    """
    surprise = catalyst.surprise
    if recipe.require_eps_beat:
        beat = surprise.eps_beat
        checks["eps_beat_verified"] = bool(beat)
        if beat is None:
            return "no reported EPS actual or estimate on record — the beat cannot be verified"
        if not beat:
            return f"EPS {surprise.verdict} — no surprise to drift on"

    if recipe.require_revenue_beat:
        # Reported separately: revenue actuals are missing more often than EPS,
        # and folding them together would turn a data gap into an EPS miss.
        revenue = surprise.revenue_beat
        checks["revenue_beat_verified"] = bool(revenue)
        if revenue is None:
            return "no reported revenue actual or estimate on record"
        if not revenue:
            return "revenue missed the estimate"
    return None
