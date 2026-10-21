"""Scored symbol + recipe plan -> a complete idea card (D12/D17/D18/D36/D38).

THE CARD RENDERS COMPLETE EVEN WHEN THE DATA IS THIN, and that is the design
rather than a concession. "vehicle: shares (IV rank unavailable, day 0 of 60)"
is the honest output of an engine whose own IV history started yesterday; a
blank line, or a confident "shares" with no explanation, would both be worse.
Every section of the card either states a value or states why it cannot.

What this module adds on top of a recipe's `TradePlan`:

  * **R:R gating per D12** — Tier A needs >= 2:1, Tier B >= 3:1. The plan's
    R:R is derived from structure (see `scoring/recipes/base.py`), so this gate
    does real work rather than being satisfied by construction.
  * **Risk-based sizing per D17/D44** — shares = (account x risk%) / per-share
    risk, against `SHADOW_ACCOUNT_VALUE` while the engine is pre-live.
  * **The hybrid Tier B budget per D18** — a calendar-month allowance that
    replenishes from realised Tier B gains, with a ceiling.
  * **The IV-rank vehicle line per D36** — abstaining honestly while the IV
    series is too short.
  * **D19 event checks**, **D38's sector guard** scoped per D44, and **D40/D41**
    status and abstention mask printed on every card.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core import config, settings
from decision.book import OpenBook
from features.catalyst import CatalystFeatures
from scoring.composite import ScoredSymbol
from scoring.recipes.base import RecipeResult

log = logging.getLogger(__name__)

__all__ = ["Idea", "Rejection", "build_idea", "vehicle_line"]


@dataclass(frozen=True)
class Rejection:
    """Why an idea was not built. Always carries a reason."""

    symbol: str
    reason: str
    stage: str
    """Which gate refused: `tier`, `risk_reward`, `events`, `sector`, `sizing`."""


@dataclass
class Idea:
    """A complete, human-readable trade plan."""

    symbol: str
    sector: str | None
    tier: str
    recipe: str
    status: str
    """D40 ladder state. Printed, because it decides what the human may do."""
    composite: float
    percentile: float | None
    mask: str
    """D41 abstention mask."""

    entry_low: float
    entry_high: float
    stop: float
    target_1: float
    target_2: float
    risk_reward: float
    horizon_days: tuple[int, int]

    shares: int
    risk_amount: float
    risk_pct: float
    position_value: float
    account_value: float

    vehicle: str
    vehicle_note: str
    notes: list[str] = field(default_factory=list)
    """Every caveat the card must carry — D19 flags, thin-data warnings."""

    @property
    def actionable(self) -> bool:
        """D40: only LIVE strategies are the human's to trade.

        A SHADOW idea is printed with its plan in full — that is how the Shadow
        Book accrues the evidence a promotion needs — but it is flagged not
        actionable, and the card says so rather than leaving it to be inferred.
        """
        return self.status == "live"

    def card(self) -> str:
        """The full card. Every section states a value or states why not."""
        flag = "" if self.actionable else f"  ** NOT ACTIONABLE ({self.status}) **"
        lines = [
            f"{self.symbol}  Tier {self.tier}  {self.recipe}{flag}",
            f"  sector      {self.sector or 'unknown'}",
            f"  composite   {self.composite:.1f}"
            + (f"  (p{self.percentile:.0f} of universe)" if self.percentile is not None else "")
            + f"  pillars [{self.mask}]",
            f"  entry       {self.entry_low:.2f} - {self.entry_high:.2f}",
            f"  stop        {self.stop:.2f}   risk/share {self.entry_high - self.stop:.2f}",
            f"  targets     TP1 {self.target_1:.2f}   TP2 {self.target_2:.2f}",
            f"  R:R         {self.risk_reward:.2f}:1  (to TP2)",
            f"  size        {self.shares} shares  = {self.position_value:,.0f} "
            f"({self.risk_pct:.2f}% risk = {self.risk_amount:,.0f} of "
            f"{self.account_value:,.0f})",
            f"  vehicle     {self.vehicle}  ({self.vehicle_note})",
            f"  horizon     {self.horizon_days[0]}-{self.horizon_days[1]} days",
        ]
        lines.extend(f"  note        {note}" for note in self.notes)
        return "\n".join(lines)


# ── D36: the vehicle line ───────────────────────────────────────────────────


def vehicle_line(iv_rank: float | None, history_days: int | None) -> tuple[str, str]:
    """D36's vehicle choice, and the note explaining it. Never blank.

    Returns (vehicle, note). While the IV series is too short the vehicle is
    SHARES and the note says exactly how short — "day 3 of 60" is information,
    and a bare "shares" would read as a decision that was never made.

    D36's bands only mean something once there is a rank to band. Defaulting to
    shares is the conservative choice: it is the middle band anyway, and it is
    the only vehicle whose risk does not depend on a volatility read.
    """
    required = config.universe().option_chains.iv_rank_min_history_days
    # The bands live in risk.yaml: D36 is a risk decision about vehicles, not a
    # scoring-mapping one.
    bands = config.risk().vehicle_by_iv_rank

    if iv_rank is None:
        have = 0 if history_days is None else history_days
        return (
            "shares",
            f"IV rank unavailable, day {have} of {required} — D7 history still "
            f"accumulating, so D36 cannot choose",
        )

    percent = iv_rank * 100.0 if iv_rank <= 1.0 else iv_rank
    if percent < bands.iv_vehicle_low:
        return (
            "shares, or long call / debit spread",
            f"IV rank {percent:.0f} below {bands.iv_vehicle_low:g} — optionality is "
            f"cheap (D36), defined risk only",
        )
    if percent > bands.iv_vehicle_high:
        return (
            "shares, or defined-risk premium sale",
            f"IV rank {percent:.0f} above {bands.iv_vehicle_high:g} — optionality is "
            f"rich (D36), so be the seller; defined risk only",
        )
    return (
        "shares",
        f"IV rank {percent:.0f} in the {bands.iv_vehicle_low:g}-"
        f"{bands.iv_vehicle_high:g} middle band — shares are the default (D36)",
    )


# ── D17/D44: sizing ─────────────────────────────────────────────────────────


def risk_pct_for(tier: str, cfg=None) -> float:
    """Per-trade risk as a % of the account. D17: Tier A 2.5%, Tier B 0.75%."""
    risk_cfg = (cfg or config.risk()).per_trade_risk_pct
    return risk_cfg.tier_a if tier == "A" else risk_cfg.tier_b


def share_count(account_value: float, risk_pct: float, risk_per_share: float) -> int:
    """Shares such that a stop-out costs `risk_pct` of the account.

    Floored, never rounded: rounding up would put the position over its risk
    budget, which is the one direction D17 does not tolerate.
    """
    if risk_per_share <= 0:
        return 0
    return int((account_value * risk_pct / 100.0) // risk_per_share)


# ── the builder ─────────────────────────────────────────────────────────────


def build_idea(
    scored: ScoredSymbol,
    result: RecipeResult,
    catalyst: CatalystFeatures,
    *,
    sector: str | None,
    status: str,
    book: OpenBook,
    iv_rank: float | None = None,
    iv_history_days: int | None = None,
    account_value: float | None = None,
    tier_b_spent_pct: float = 0.0,
    cfg=None,
) -> Idea | Rejection:
    """One scored symbol plus one triggered recipe -> an idea, or a rejection.

    Gate order is deliberate: the cheap absolute gates first, then the ones
    that depend on the rest of the book. A rejection always names its stage, so
    a reader can tell "this idea was not good enough" from "this idea was fine
    but the book is full".
    """
    scoring_cfg = cfg or config.scoring()
    risk_cfg = config.risk()
    account = account_value if account_value is not None else settings.shadow_account_value()
    notes: list[str] = []

    if scored.tier is None:
        return Rejection(scored.symbol, scored.tier_reason or "no tier", "tier")
    if result.plan is None:
        return Rejection(scored.symbol, f"{result.recipe}: {result.reason}", "tier")

    plan = result.plan
    tier = scored.tier

    # ── D12: the R:R gate ───────────────────────────────────────────────────
    required = (
        scoring_cfg.tiers.a.min_risk_reward if tier == "A" else scoring_cfg.tiers.b.min_risk_reward
    )
    if plan.risk_reward < required:
        return Rejection(
            scored.symbol,
            f"R:R {plan.risk_reward:.2f} below the {required:g}:1 Tier {tier} minimum "
            f"(D12) — the structure does not pay enough for the risk",
            "risk_reward",
        )

    # ── D19: event checks ───────────────────────────────────────────────────
    ex_div = catalyst.days_to_ex_dividend
    if ex_div is not None and ex_div <= risk_cfg.dividends.no_new_swing_long_within_days_of_ex:
        return Rejection(
            scored.symbol,
            f"ex-dividend in {ex_div} day(s), inside D19(3)'s "
            f"{risk_cfg.dividends.no_new_swing_long_within_days_of_ex}-day exclusion for a new long",
            "events",
        )
    if ex_div is None:
        notes.append("no ex-dividend date on record — D19(3) could not be verified")
    if catalyst.days_to_earnings is None:
        notes.append("no earnings date on record — event risk unverified")
    elif catalyst.days_to_earnings <= plan.horizon_days[1]:
        notes.append(
            f"earnings in {catalyst.days_to_earnings} day(s), inside the "
            f"{plan.horizon_days[1]}-day horizon — event risk is live"
        )

    # ── D38/D44: the sector guard, scoped to this ladder state ──────────────
    guard = risk_cfg.sector_guard
    held = book.sector_count(sector)
    if held >= guard.max_concurrent_swings_per_sector:
        reason = (
            f"{held} swing(s) already open in {sector or 'unknown'} in the "
            f"{book.status} book, at D38's limit of "
            f"{guard.max_concurrent_swings_per_sector}"
        )
        if guard.surface_excess_as_watch_only:
            # D38: the information that the sector is working is worth keeping;
            # the concentration is not.
            notes.append(f"WATCH ONLY — {reason}")
        else:
            return Rejection(scored.symbol, reason, "sector")
    if book.holds(scored.symbol):
        return Rejection(scored.symbol, f"already open in the {book.status} book", "sector")
    if book.swing_count >= risk_cfg.max_concurrent_swings:
        return Rejection(
            scored.symbol,
            f"{book.swing_count} swings open in the {book.status} book, at D17's "
            f"limit of {risk_cfg.max_concurrent_swings}",
            "sector",
        )

    # ── D17/D18/D44: sizing ─────────────────────────────────────────────────
    risk_pct = risk_pct_for(tier, risk_cfg)
    if tier == "B":
        budget = risk_cfg.tier_b_budget
        remaining = budget.hard_ceiling_pct - tier_b_spent_pct
        if remaining < risk_pct:
            return Rejection(
                scored.symbol,
                f"Tier B month budget exhausted: {tier_b_spent_pct:.2f}% of the "
                f"{budget.hard_ceiling_pct:g}% ceiling used, {remaining:.2f}% left but this "
                f"idea needs {risk_pct:g}% (D18)",
                "sizing",
            )
        if remaining < risk_pct * 2:
            notes.append(
                f"Tier B budget nearly spent: {remaining:.2f}% of "
                f"{budget.hard_ceiling_pct:g}% remains this month (D18)"
            )

    risk_per_share = plan.entry_high - plan.stop
    shares = share_count(account, risk_pct, risk_per_share)
    if shares <= 0:
        return Rejection(
            scored.symbol,
            f"risk of {risk_per_share:.2f}/share against a {risk_pct:g}% budget on "
            f"{account:,.0f} buys zero shares — the stop is too wide for the account",
            "sizing",
        )

    heat_after = book.open_risk_pct + risk_pct
    if heat_after > risk_cfg.heat.max_open_heat_pct:
        return Rejection(
            scored.symbol,
            f"open heat would reach {heat_after:.1f}%, over D17's "
            f"{risk_cfg.heat.max_open_heat_pct:g}% cap in the {book.status} book",
            "sizing",
        )

    vehicle, vehicle_note = vehicle_line(iv_rank, iv_history_days)

    # D41: the mask is printed on every card, because a composite of 72 from
    # two pillars is not the claim 72 from four is.
    if scored.pillars_scored < 4:
        notes.append(
            f"composite rests on {scored.pillars_scored} of 4 pillars "
            f"[{scored.mask}] — renormalized per D41"
        )
    if status != "live":
        notes.append(
            f"strategy is {status.upper()} per D40 — tracked, not traded; sized "
            f"against the virtual account (D44)"
        )

    return Idea(
        symbol=scored.symbol,
        sector=sector,
        tier=tier,
        recipe=result.recipe,
        status=status,
        composite=scored.composite or 0.0,
        percentile=scored.percentile,
        mask=scored.mask,
        entry_low=plan.entry_low,
        entry_high=plan.entry_high,
        stop=plan.stop,
        target_1=plan.target_1,
        target_2=plan.target_2,
        risk_reward=plan.risk_reward,
        horizon_days=plan.horizon_days,
        shares=shares,
        risk_amount=account * risk_pct / 100.0,
        risk_pct=risk_pct,
        position_value=shares * plan.entry,
        account_value=account,
        vehicle=vehicle,
        vehicle_note=vehicle_note,
        notes=notes,
    )
