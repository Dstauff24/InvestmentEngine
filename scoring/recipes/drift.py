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
    """A beat-and-raise whose price reaction held through the window.

    The trade: buy the hold above the reaction's low, stop below it (a fill
    invalidates the whole premise), target multiples of the reaction itself —
    the market's own statement about what the news was worth.

    **THE REACTION IS A WINDOW, NOT A BAR, AND THAT IS A LOOKAHEAD FIX.** For a
    report dated T, a pre-open release gaps on T and an after-close release
    gaps on T+1, and no provider publishes the release time. Reading a single
    bar would either credit an after-close surprise to a bar that closed before
    it existed, or miss every pre-open report. So the reaction is measured from
    the close before the report through the close of the session after it
    (`features.technical.earnings_reaction`), judged once at the close that
    completes that window, and the earliest entry is the session after it —
    T+2, or T+1 when a release time confirms `bmo`.

    WHAT THIS RECIPE CANNOT YET CHECK, and says so rather than pretending:
    `require_raised_guidance` has no data source. Guidance text is in neither
    the FMP endpoints the client calls nor the event rows, so the check is
    reported UNVERIFIED in `checks` rather than silently passed. A recipe that
    quietly skips one of its three stated conditions is a different recipe.
    """
    scoring_cfg = cfg or config.scoring()
    recipe = scoring_cfg.recipes.post_earnings_drift
    geometry = scoring_cfg.geometry.post_earnings_drift

    checks: dict[str, bool] = {}
    if not recipe.enabled:
        return no_setup(NAME, "recipe disabled in config")

    # NO "DAYS SINCE EARNINGS" GATE. D49 judges each report exactly once, at
    # the close that completes its reaction window, so "is the report still
    # recent" is answered by `reaction.complete` below and not by a number.
    # `max_days_since_earnings` was retired rather than renamed (see O14).
    checks["has_knowable_report"] = catalyst.surprise.reported_on is not None

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

    # ── the reaction, measured without needing a release time ───────────────
    reaction = technical.reaction
    if reaction is None or technical.close is None:
        return no_setup(
            NAME,
            "no measurable reaction window — the report's actual is not knowable yet, "
            "or the sessions around it are missing",
            checks,
        )

    # JUDGED EXACTLY ONCE, at the close that completes the window. A reaction
    # re-judged on later sessions would re-enter the same idea for days, and
    # the window's own close is what makes the hold part of the measurement
    # rather than a separate guess.
    checks["reaction_window_complete"] = reaction.complete
    if not reaction.complete:
        return no_setup(
            NAME,
            f"the reaction window ends {reaction.end_session} and is not the latest "
            f"session — judged at the close that completes it, never before",
            checks,
        )

    reacted = reaction.reaction_pct >= recipe.min_gap_up_pct
    checks["reacted_up"] = reacted
    if not reacted:
        return no_setup(
            NAME,
            f"reaction {reaction.reaction_pct:+.2f}% over {reaction.sessions} session(s) "
            f"is under the {recipe.min_gap_up_pct:g}% minimum — a move that filled is "
            f"not a reaction that held",
            checks,
        )

    # The residual hold test. The reaction is measured TO the window's close,
    # so a filled gap already fails above; this only rejects a close sitting on
    # the window's low, which is a rejection candle rather than a hold.
    holding = technical.close > reaction.window_low
    checks["holds_reaction_low"] = holding
    if recipe.require_holds_day_one_low and not holding:
        return no_setup(
            NAME,
            f"closed {technical.close:.2f} at the reaction low "
            f"{reaction.window_low:.2f} — the move is being rejected",
            checks,
        )

    gap_points = reaction.reaction_points
    if gap_points <= 0:
        return no_setup(NAME, "the reaction is not positive in points", checks)

    plan, why = build_plan(
        entry_reference=technical.close,
        # The window's low, not the gap day's: for an after-close report the
        # gap day IS the second session, and for an unknown time it could be
        # either, so the lowest low inside the window is the level that
        # invalidates the move whichever session carried it.
        structural_stop=reaction.window_low,
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
            f"reacted {reaction.summary}, {catalyst.surprise.verdict}, holding above "
            f"{reaction.window_low:.2f} (guidance UNVERIFIED)"
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
