"""Recipe 1: pullback to a rising trend. Reads features; re-measures nothing."""

from __future__ import annotations

from core import config
from features.catalyst import CatalystFeatures
from features.technical import TechnicalFeatures
from scoring.recipes.base import RecipeResult, build_plan, no_setup

NAME = "pullback_to_trend"


def trail_window() -> int | None:
    """The trail's MA window, from `exits.swing.runner_trail` (risk.yaml).

    Read from the RISK config rather than duplicated into the scoring geometry:
    the framework states the trail once, beside D18's trim rules, and a second
    copy is the thing that drifts.
    """
    return config.risk().exits.swing.trail_ma_days


def evaluate(
    technical: TechnicalFeatures,
    catalyst: CatalystFeatures,
    *,
    cfg=None,
) -> RecipeResult:
    """An uptrend that has pulled back to a rising MA with RSI reset.

    The trade: buy the pullback, stop below the low the pullback made, target
    the prior swing high it should reclaim, then the measured move beyond it.

    **TWO GEOMETRIES, BEHIND `geometry.pullback_to_trend.variant`.** The entry
    conditions are identical in both — the MA stack, the RSI band, the dry-up,
    the earnings exclusion — because the measured problem is not there. v1 hung
    the stop on `swing_low`, the lowest low of the whole 60-day lookback, which
    on a trending stock is a level from a different episode, and anchored TP2
    on the distance from TODAY'S CLOSE, so a deeper pullback got a farther
    target. v2 hangs the stop on `pullback_low` and projects the recovered leg
    beyond the reclaim. See `config.PullbackGeometry` for the numbers behind it.

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
    trail_ma: int | None = None

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

    if geometry.variant == "v3":
        # THE EXPLICIT VERSION OF WHAT v2 DID BY ACCIDENT (O20). Same entry,
        # same structural stop; the difference is the exit. v2 earned +0.24R
        # out of sample while 1.7% of its trades touched a target and 34% were
        # marked out at a 45-day clock — a trend-following profile wearing a
        # measured-move target. So: trim at the reclaim, then TRAIL the runner.
        if structure.pullback_low is None:
            return no_setup(
                NAME,
                f"no pullback low over the last "
                f"{geometry.pullback_low_lookback_days} session(s) — v3 hangs the "
                f"stop on it and will not fall back to the swing low",
                checks,
            )
        trail_ma = trail_window()
        if trail_ma is None:
            # `runner_trail: none` is a legal configuration and means this
            # recipe has no exit for its remainder. Refusing is the only honest
            # answer: a trailing variant without a trail is not v3.
            return no_setup(
                NAME,
                "v3 trails the runner and `exits.swing.runner_trail` is `none`, "
                "so the remainder would have no exit at all",
                checks,
            )
        structural_stop = structure.pullback_low
        target_1 = reclaim
        target_2 = None
    elif geometry.variant == "v2":
        # The pullback's OWN low, not the 60-day low. None REFUSES rather than
        # falling back: silently using `swing_low` would make v2 report v1's
        # geometry under v2's name, which is the one thing a variant must never
        # do — the backtest comparing them would then compare nothing.
        if structure.pullback_low is None:
            return no_setup(
                NAME,
                f"no pullback low over the last "
                f"{geometry.pullback_low_lookback_days} session(s) — v2 hangs the "
                f"stop on it and will not fall back to the swing low",
                checks,
            )
        structural_stop = structure.pullback_low
        # The recovered leg, projected beyond the reclaim. Structure the market
        # made; it does not move with where price sits today.
        leg = reclaim - structure.pullback_low
        target_1 = reclaim
        target_2 = reclaim + leg * geometry.tp2_leg_multiple
    else:
        structural_stop = structure.swing_low
        target_1 = reclaim
        target_2 = technical.close + distance * geometry.tp2_measured_move

    plan, why = build_plan(
        entry_reference=technical.close,
        structural_stop=structural_stop,
        target_1=target_1,
        target_2=target_2,
        features=technical,
        horizon_days=tuple(recipe.horizon_days),
        cfg=scoring_cfg,
        trail_ma=trail_ma,
        trail_confirm=geometry.trail_confirm_closes if trail_ma else None,
    )
    if plan is None:
        return no_setup(NAME, f"setup fired but the geometry is unusable: {why}", checks)

    return RecipeResult(
        recipe=NAME,
        triggered=True,
        reason=(
            f"uptrend pullback to the {recipe.ma_fast}MA, RSI {technical.rsi:.1f} reset, "
            f"reclaim {reclaim:.2f} [{geometry.variant}]"
        ),
        plan=plan,
        checks=checks,
    )
