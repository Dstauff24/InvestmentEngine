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

__all__ = [
    "Idea",
    "Rejection",
    "SlotAssignment",
    "assign_slots",
    "build_idea",
    "reserve",
    "vehicle_line",
]


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
    composite_tier: str | None = None
    """What the COMPOSITE alone said: A, B, or None (D53). Recorded on every
    Shadow Book row so the forward record answers O18's question — whether the
    composite adds anything to a recipe signal — instead of the change
    destroying the evidence it depends on."""
    composite_gate_passed: bool = True
    """Would the OLD existence gate have passed? Derivable from
    `composite_tier`, and stored anyway: this is the question D53 poses, and a
    reader splitting expectancy on it should not have to know that NULL meant
    refused."""
    regime: str | None = None
    """D45's market regime at call time, from `backtest/regime.py` — the same
    code the backtest tags trades with, so the card's comparison is between
    like and like. `None`/`unknown` before the benchmark has 200 sessions."""
    regime_note: str = ""
    """What this recipe MEASURED in this regime, and whether that makes the
    call counter-evidence. Rendered on every card."""
    would_refuse: tuple[str, ...] = ()
    """D54: the capital constraints that WOULD have declined this call. Empty
    means the engine would have taken it. The plan is recorded either way,
    because the caps decide what would be TRADED while the Shadow Book measures
    what the RECIPE is worth — two questions the old behaviour answered with
    one number."""
    slot_rank: int | None = None
    """1-based position in the day's ranking, stamped by `assign_slots`. Past
    `max_ideas_per_day` that is itself one of `would_refuse`."""
    watch_only: bool = False
    """D38's excess: the sector is at its limit, so the idea SURFACES as
    information and is not a position. It takes a digest slot and reserves no
    book state — counting it against the heat cap would charge risk nobody
    took."""
    """Every caveat the card must carry — D19 flags, thin-data warnings."""

    @property
    def actionable(self) -> bool:
        """D40: only LIVE strategies are the human's to trade.

        A SHADOW idea is printed with its plan in full — that is how the Shadow
        Book accrues the evidence a promotion needs — but it is flagged not
        actionable, and the card says so rather than leaving it to be inferred.
        """
        return self.status == "live"

    @property
    def would_be_taken(self) -> bool:
        """D54: would the engine have TAKEN this today, caps and slots included?

        Deliberately not called `actionable`, which above means D40's ladder
        question — "is this the human's to trade" — and is about the STRATEGY.
        This one is about the day: the same plan can be live-actionable and
        still not taken because the sector was full. Collapsing the two would
        make `would_refuse` unreadable, since a row would not say which of the
        two reasons kept it out of the book.
        """
        return not self.would_refuse and not self.watch_only

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
            f"  regime      {self.regime or 'unknown'}{self.regime_note}",
        ]
        lines.extend(f"  note        {note}" for note in self.notes)
        return "\n".join(lines)


# ── D45/D56: what this recipe measured in THIS regime ───────────────────────


def regime_line(recipe: str, regime: str | None, *, cfg=None) -> str:
    """The clause a card appends to its regime, from CITED evidence.

    **A CALL IN A CELL THAT MEASURED NEGATIVE IS COUNTER-EVIDENCE, AND THE CARD
    SAYS SO.** v3's out-of-sample edge sits almost entirely in `bull_calm`
    (+0.51R on 506 closed) while `bull_normal` measured -0.09R on 432 — a real
    sample, not a thin one. D45 evaluates the ladder per recipe x regime, so a
    pullback call surfacing in `bull_normal` is a call the evidence argues
    against, and a human should not have to remember that.

    Four shapes, because the honest statement differs:
      * no evidence block at all -> say that, never imply zero;
      * `unknown` regime -> the benchmark cannot place the call in a cell;
      * a cell under D39(2)'s 30-closed floor -> NO evidence, not bad evidence;
      * a well-sampled cell -> the number, with the best cell for contrast and
        the word COUNTER-EVIDENCE when it is at or below zero.
    """
    evidence = (cfg or config.scoring()).evidence_for(recipe)
    if evidence is None:
        return "  (no per-regime backtest evidence on record for this recipe)"
    if not regime or regime == "unknown":
        return (
            "  (regime UNKNOWN — the benchmark has under 200 sessions, so this call "
            f"cannot be placed in one of {recipe}'s measured cells)"
        )
    cell = evidence.of(regime)
    if cell is None:
        return f"  ({recipe} measured nothing in this regime on {evidence.citation})"
    if not cell.sufficient:
        return (
            f"  ({recipe} measured {cell.expectancy_r:+.2f}R here on only {cell.closed} "
            f"closed — under D39(2)'s 30, so this cell is NO evidence rather than weak "
            f"evidence; {evidence.citation})"
        )
    verdict = "COUNTER-EVIDENCE — " if cell.is_counter_evidence else ""
    contrast = ""
    best = evidence.best
    if best and best[0] != regime:
        contrast = f", against {best[1].expectancy_r:+.2f}R in {best[0]}"
    return (
        f"  ({verdict}{recipe} measured {cell.expectancy_r:+.2f}R in this regime on "
        f"{cell.closed} closed{contrast}; {evidence.basis} R over {evidence.window}, "
        f"{evidence.citation})"
    )


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


# ── slot assignment (D42, amended Oct 23 2026) ──────────────────────────────


@dataclass
class SlotAssignment:
    """Which ideas got the day's actionable slots, and why the rest did not."""

    accepted: list = field(default_factory=list)
    """Ideas that passed EVERY stage, in composite order. These take the slots."""
    watch_only: list = field(default_factory=list)
    """D38's excess: the sector is at its limit, so the idea is INFORMATION.
    **Outside the N.** It consumes no actionable slot, reserves no book state,
    and is not a Shadow Book call — the engine declined to size it, and a call
    it declined to take is not a call. It gets its own digest section with the
    D38 reason, because "this sector is working and we are already full" is
    worth reading."""
    logged: list = field(default_factory=list)
    """D54: every certified-recipe plan that cleared its RECIPE gates — the
    accepted ones plus the ones a cap or the slot limit would have refused,
    each carrying `would_refuse`. THIS is what goes to the Shadow Book;
    `accepted` is what the digest shows."""
    refused: list = field(default_factory=list)
    """(ScoredSymbol, Rejection) for every candidate walked and declined."""
    walked: int = 0
    """How far down the ranking the walk went. The number that makes the bug
    this replaced visible: it used to be exactly `max_ideas_per_day`."""
    considered: int = 0
    """How many symbols were CANDIDATES — every scored symbol, since D53 says
    the composite may not decide who is looked at. Reported beside `walked` so
    the digest can state what the engine evaluated rather than leaving a reader
    to infer it from a stage count: on 2026-10-26 "286 at plan" was read as the
    universe and was actually the tier-qualifying subset, which is the question
    this pair exists to answer from the digest itself."""

    @property
    def refused_by_symbol(self) -> dict:
        return {scored.symbol: rejection for scored, rejection in self.refused}

    def declined_by_symbol(self) -> dict:
        """Every candidate that did NOT take a slot, with the stage that said
        so — refusals plus the watch-only excess, which `daily_candidates`
        records under D38's own stage name."""
        out = dict(self.refused_by_symbol)
        for idea in self.watch_only:
            out[idea.symbol] = Rejection(
                idea.symbol,
                next(
                    (note for note in idea.notes if note.startswith("WATCH ONLY")),
                    "watch only (D38)",
                ),
                "sector",
            )
        return out


def reserve(book: OpenBook, *, symbol: str, sector: str | None, risk_pct: float) -> None:
    """Count an ACCEPTED idea against the book, so the next candidate sees it.

    **THE SECOND BUG FOUND WITH THE FIRST.** `build_ideas` passed one immutable
    `OpenBook` to every candidate in turn, so five ideas accepted on the same
    day in the same sector each saw `sector_count` as it was BEFORE any of them
    — D38's two-per-sector guard, D17's max-concurrent-swings and the 10% heat
    cap all counted yesterday's book and none of today's decisions. The guards
    read as enforced and were not.
    """
    book.symbols.add(symbol)
    key = sector or ""
    book.by_sector[key] = book.by_sector.get(key, 0) + 1
    book.swing_count += 1
    book.open_risk_pct += risk_pct


# D54's vocabulary, fixed here because `shadow_book.would_refuse` stores these
# strings and a renderer has to read them back. A CHECK constraint on array
# elements could not be read by the code that renders them, so the pairing is
# pinned by a test instead.
CONSTRAINTS = ("tier_b_budget", "heat", "sector", "concurrency", "slot_rank")


def certified_recipe(recipe: str, cfg) -> bool:
    """D53's certification, by name. D54 logs only certified recipes."""
    return cfg.certification_of(recipe).status == "certified"


def assign_slots(
    run,
    *,
    build_context,
    limit: int | None = None,
    cfg=None,
) -> SlotAssignment:
    """Fill the day's actionable slots by walking the ranking in order.

    **D42 AMENDED: SURFACED MEANS "TOP N BY COMPOSITE AMONG IDEAS THAT PASS
    EVERY STAGE".** It used to mean "top N by composite among ideas that
    cleared a TIER gate", decided before any plan was built — so on 2026-10-23
    five composite leaders with no recipe plan (EXPD, DVN, MPC, FTNT, VLO) took
    all five slots and the digest reported five refusals, while any plan-bearing
    idea at rank 6 or lower was discarded unseen. Three days of that is three
    days of Shadow Book calls the engine measured nothing from.

    **EVERY STAGE, not just the plan check.** D12's R:R gate, D19's event
    checks, D38's sector guard, D17's heat cap and concurrency limits and D18's
    Tier B budget all now run BEFORE a slot is assigned, through this one
    function — because a gate applied after the cut reproduces the same bug one
    step later, and two copies of a gate drift.

    The walk is lazy: candidates are evaluated in composite order and it stops
    at `limit` acceptances, so the cost is "how far it had to go" rather than
    "the whole qualifying population". `walked` records that distance, which is
    the number that would have made the original bug visible.

    `build_context(scored)` supplies the per-symbol inputs a stage needs
    (recipe result, catalyst, sector, status, book, IV, account). Injected so
    this function stays testable without a database, and so the caller decides
    what a "book" means for the ladder state in play (D44).
    """
    scoring_cfg = cfg or config.scoring()
    slots = scoring_cfg.ranking.max_ideas_per_day if limit is None else limit

    # **D53: THE COMPOSITE MAY NOT DECIDE WHICH SYMBOLS ARE CONSIDERED.** This
    # line read `s.tier is not None` until 2026-10-26, which put the composite
    # tier gate BACK in front of plan-building one level earlier than the branch
    # D53 added inside `build_idea` — so a certified recipe's plan on a symbol
    # the composite gave no tier was never built, and the D53 branch that
    # handles exactly that case was unreachable. The live symptom was a digest
    # reporting "286 at plan" where 286 was the tier-qualifying population
    # rather than the universe: the engine had not looked at the other ~145
    # symbols at all. The same shape as the D42 surfacing bug, which is the
    # argument for one function — a gate applied while choosing who to consider
    # is still a gate applied before the plan exists.
    #
    # Every scored symbol is now a candidate, in composite order. Cost is
    # unchanged for the ones with no plan: `build_context` returns None without
    # touching the database, so they refuse at `plan` immediately. The
    # composite gate still binds for UNCERTIFIED recipes — `build_idea` refuses
    # those at stage `tier` — which is D53's actual rule rather than a filter
    # that cannot tell the two apart.
    ranked = [s for s in run.scored if s.composite is not None]
    ranked.sort(key=lambda s: (-(s.composite or 0.0), s.symbol))

    assignment = SlotAssignment(considered=len(ranked))
    tier_b_spent: dict[str, float] = {}
    for scored in ranked:
        # **D54: THE WALK NO LONGER STOPS AT THE SLOT LIMIT.** It used to break
        # here, which made `max_ideas_per_day` decide what the engine LEARNS
        # from rather than what it shows a human at 16:45. A shadow call costs
        # nothing and a day not recorded cannot be recovered, so the walk runs
        # to the end of the ranking and ideas past the limit are logged with
        # `slot_rank` among their refusing constraints. The cost is bounded by
        # the same thing as before: a symbol with no plan refuses at `plan`
        # before `build_context` touches the database.
        assignment.walked += 1
        scored.surfaced = False

        context = build_context(scored)
        if context is None or context.get("result") is None:
            triggered = ", ".join(r.recipe for r in scored.triggered) or "none"
            assignment.refused.append(
                (
                    scored,
                    Rejection(
                        scored.symbol,
                        f"no recipe produced a plan (triggered: {triggered})",
                        "plan",
                    ),
                )
            )
            continue

        status = context["status"]
        built = build_idea(
            scored,
            context["result"],
            context["catalyst"],
            sector=context.get("sector"),
            status=status,
            book=context["book"],
            iv_rank=context.get("iv_rank"),
            iv_history_days=context.get("iv_history_days"),
            account_value=context.get("account_value"),
            tier_b_spent_pct=tier_b_spent.get(status, 0.0),
            regime=context.get("regime"),
            cfg=scoring_cfg,
        )
        if isinstance(built, Rejection):
            assignment.refused.append((scored, built))
            continue

        built.slot_rank = assignment.walked
        if len(assignment.accepted) >= slots and not built.watch_only:
            # Past the limit. For a CERTIFIED recipe that is a slot-rank flag
            # and the call is still recorded; for an uncertified one it is a
            # refusal, since nothing says its plans are worth recording.
            if certified_recipe(built.recipe, scoring_cfg):
                built.would_refuse = (*built.would_refuse, "slot_rank")
                assignment.logged.append(built)
            else:
                assignment.refused.append(
                    (
                        scored,
                        Rejection(
                            scored.symbol,
                            f"rank {built.slot_rank} is past the day's {slots} actionable slot(s)",
                            "slot_rank",
                        ),
                    )
                )
            continue

        if built.watch_only:
            # D38's excess is INFORMATION, not a call: outside the N, no slot,
            # no book state, not logged to the Shadow Book. Charging the heat
            # cap for risk nobody took would refuse the next real idea on a
            # trade that does not exist, and filling the actionable slots with
            # ideas the engine declined to size is the bug this walk replaced.
            assignment.watch_only.append(built)
            if certified_recipe(built.recipe, scoring_cfg):
                # D54 amends D42 here: the engine declined to SIZE it, which is
                # why it takes no slot and reserves no book state, but the plan
                # is still evidence about the recipe.
                assignment.logged.append(built)
            continue

        if built.would_refuse:
            # A certified plan a cap would have refused: recorded, not taken,
            # and it reserves nothing — charging the heat cap for risk nobody
            # took would refuse the next real idea on a trade that does not
            # exist.
            assignment.logged.append(built)
            continue

        assignment.accepted.append(built)
        assignment.logged.append(built)
        scored.surfaced = True
        # The state the NEXT candidate must see (D17/D38/D18).
        reserve(
            context["book"],
            symbol=scored.symbol,
            sector=context.get("sector"),
            risk_pct=built.risk_pct,
        )
        if scored.tier == "B":
            tier_b_spent[status] = tier_b_spent.get(status, 0.0) + built.risk_pct
    return assignment


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
    regime: str | None = None,
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
    watch_only = False
    would_refuse: list[str] = []

    # ── D53: is the COMPOSITE an existence gate for this recipe? ───────────
    certification = scoring_cfg.certification_of(result.recipe)
    certified = certification.status == "certified"
    composite_tier = scored.tier

    if not certified and composite_tier is None:
        # UNCERTIFIED: the old rule stands. Nothing has shown this recipe's
        # plans are worth surfacing on their own, so where there is no evidence
        # the old conservatism is the right default.
        return Rejection(scored.symbol, scored.tier_reason or "no tier", "tier")
    if result.plan is None:
        return Rejection(scored.symbol, f"{result.recipe}: {result.reason}", "tier")

    plan = result.plan
    # THE SIZE TIER. For a certified recipe the composite decides size and not
    # existence: Tier B by default, Tier A only when the composite Tier A gate
    # also passes (D53). For an uncertified one it is the composite's tier, as
    # D12 has always had it.
    tier = ("A" if composite_tier == "A" else "B") if certified else composite_tier

    # ── The R:R gate: the recipe's own when certified, D12's tier gate otherwise
    if certified:
        required = certification.min_risk_reward
        gate_name = f"the {required:g}:1 certified minimum for {result.recipe}"
    else:
        required = (
            scoring_cfg.tiers.a.min_risk_reward
            if tier == "A"
            else scoring_cfg.tiers.b.min_risk_reward
        )
        gate_name = f"the {required:g}:1 Tier {tier} minimum (D12)"
    if plan.risk_reward < required:
        return Rejection(
            scored.symbol,
            f"R:R {plan.risk_reward:.2f} below {gate_name} — the structure does not "
            f"pay enough for the risk",
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
    # ── D54: A CAP DECIDES WHAT WOULD BE TRADED, NOT WHAT IS RECORDED ──────
    # For a CERTIFIED recipe the capital constraints below stop refusing and
    # start FLAGGING: the plan is logged either way and the row says which cap
    # it broke. A shadow call costs nothing and a day not recorded cannot be
    # recovered, while the caps exist to protect capital that shadow is not
    # risking. They bind from PAPER (D40). For an UNCERTIFIED recipe nothing
    # changes — there is no evidence its plans are worth recording at all.
    def capped(name: str, reason: str) -> Rejection | None:
        """Flag for a certified recipe IN SHADOW; refuse everywhere else.

        **THE LADDER STATE IS PART OF THE RULE, not just the certification.**
        D54 says shadow records and paper and live ENFORCE, and D40 (amended
        Oct 26 2026) judges paper->live on the paper book alone precisely
        because the caps bind there — so a paper call a cap would have refused
        must not exist, or the portfolio question is answered with calls the
        portfolio would never have held.
        """
        if not certified or status != "shadow":
            return Rejection(scored.symbol, reason, name)
        would_refuse.append(name)
        notes.append(f"WOULD NOT BE TAKEN — {reason} (D54: recorded anyway, shadow only)")
        return None

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
            # the concentration is not. D54 ALSO logs it when the recipe is
            # certified, which amends D42's "watch-only is not a Shadow Book
            # call" for that case — the plan is evidence about the recipe even
            # though the engine declined to size it.
            notes.append(f"WATCH ONLY — {reason}")
            watch_only = True
            if certified and status == "shadow":
                would_refuse.append("sector")
        else:
            refusal = capped("sector", reason)
            if refusal:
                return refusal
    # NOT a cap: a second call on a symbol already open would count one setup
    # twice, which is the same rule D39(4) gives the backtest. It refuses for
    # every recipe, certified or not.
    if book.holds(scored.symbol):
        return Rejection(scored.symbol, f"already open in the {book.status} book", "duplicate")
    if book.swing_count >= risk_cfg.max_concurrent_swings:
        refusal = capped(
            "concurrency",
            f"{book.swing_count} swings open in the {book.status} book, at D17's "
            f"limit of {risk_cfg.max_concurrent_swings}",
        )
        if refusal:
            return refusal

    # ── D17/D18/D44: sizing ─────────────────────────────────────────────────
    risk_pct = risk_pct_for(tier, risk_cfg)
    if tier == "B":
        budget = risk_cfg.tier_b_budget
        remaining = budget.hard_ceiling_pct - tier_b_spent_pct
        if remaining < risk_pct:
            refusal = capped(
                "tier_b_budget",
                f"Tier B month budget exhausted: {tier_b_spent_pct:.2f}% of the "
                f"{budget.hard_ceiling_pct:g}% ceiling used, {remaining:.2f}% left but this "
                f"idea needs {risk_pct:g}% (D18)",
            )
            if refusal:
                return refusal
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
        refusal = capped(
            "heat",
            f"open heat would reach {heat_after:.1f}%, over D17's "
            f"{risk_cfg.heat.max_open_heat_pct:g}% cap in the {book.status} book",
        )
        if refusal:
            return refusal

    vehicle, vehicle_note = vehicle_line(iv_rank, iv_history_days)

    # D41: the mask is printed on every card, because a composite of 72 from
    # two pillars is not the claim 72 from four is.
    if certified and composite_tier != "A":
        notes.append(
            f"sized at TIER B under D53: {result.recipe} is certified "
            f"({certification.evidence}), so the composite is a conviction upgrade "
            f"and not a gate — "
            + (
                f"the composite reached Tier {composite_tier} only"
                if composite_tier
                else "the composite cleared no tier at all"
            )
        )
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
        watch_only=watch_only,
        composite_tier=composite_tier,
        composite_gate_passed=composite_tier is not None,
        regime=regime,
        regime_note=regime_line(result.recipe, regime, cfg=scoring_cfg),
        would_refuse=tuple(would_refuse),
    )
