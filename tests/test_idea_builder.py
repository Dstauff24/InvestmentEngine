"""The decision layer (D12/D17/D18/D19/D36/D38/D40/D41/D44). No DB, no network.

The card must render COMPLETE even when the data is thin — that is the point of
step 7, not a concession. Every section states a value or states why it cannot.
"""

from __future__ import annotations

from datetime import date

import pytest

from core import config
from decision.book import OpenBook
from decision.idea_builder import Idea, Rejection, build_idea, share_count, vehicle_line
from features.catalyst import CatalystFeatures, EarningsSurprise, InsiderCluster
from features.evidence import EvidenceDepth
from scoring.composite import ScoredSymbol
from scoring.pillars import PillarScore, PillarScores
from scoring.recipes.base import RecipeResult, TradePlan

AS_OF = date(2026, 10, 21)
ACCOUNT = 100_000.0
RISK = config.risk()
SCORING = config.scoring()


def plan(**kw) -> TradePlan:
    base = {
        "entry_low": 99.5,
        "entry_high": 100.5,
        "stop": 95.0,
        "target_1": 110.0,
        "target_2": 125.0,
        "horizon_days": (2, 15),
    }
    base.update(kw)
    return TradePlan(**base)


def result(recipe="pullback_to_trend", **kw) -> RecipeResult:
    return RecipeResult(
        recipe=recipe,
        triggered=True,
        reason="setup fired",
        plan=kw.pop("plan", plan()),
        checks={},
    )


def scored(tier="A", composite=82.0, mask_letters="ABCD", **kw) -> ScoredSymbol:
    def pillar(letter, value):
        return PillarScore(letter, value if letter in mask_letters else None)

    pillars = PillarScores(
        symbol=kw.get("symbol", "TEST"),
        technical=pillar("A", 85.0),
        fundamental=pillar("B", 80.0),
        catalyst=pillar("C", 75.0),
        sentiment=pillar("D", 70.0),
    )
    item = ScoredSymbol(symbol=kw.get("symbol", "TEST"), pillars=pillars, composite=composite)
    item.tier = tier
    item.tier_reason = f"Tier {tier}"
    item.percentile = kw.get("percentile", 97.0)
    return item


def cata(**kw) -> CatalystFeatures:
    base = {
        "symbol": "TEST",
        "as_of": AS_OF,
        "days_to_earnings": 40,
        "days_since_earnings": 50,
        "days_to_ex_dividend": 40,
        "next_dividend_amount": None,
        "insiders": InsiderCluster(False, 0, 0, 0.0, None, 90, EvidenceDepth(30, 90)),
        # O12: the last reported quarter\'s beat, as a fact. The default is
        # the honest one — unverified, which the recipe refuses rather than
        # reads as a miss.
        "surprise": EarningsSurprise(None, None, None, None, None),
    }
    base.update(kw)
    return CatalystFeatures(**base)


def book(**kw) -> OpenBook:
    base = {"status": "shadow"}
    base.update(kw)
    return OpenBook(**base)


def build(**kw):
    args = {
        "scored": scored(),
        "result": result(),
        "catalyst": cata(),
        "sector": "Technology",
        "status": "shadow",
        "book": book(),
        "iv_rank": None,
        "iv_history_days": 0,
        "account_value": ACCOUNT,
    }
    args.update(kw)
    return build_idea(args.pop("scored"), args.pop("result"), args.pop("catalyst"), **args)


# ══════════════════════════════════════════════════════════════════════════
# D36: the vehicle line is NEVER blank
# ══════════════════════════════════════════════════════════════════════════


def test_an_unavailable_iv_rank_says_how_far_through_the_window_it_is():
    """THE honest-output requirement. "shares" with no explanation would read
    as a decision that was never made."""
    vehicle, note = vehicle_line(None, 0)
    assert vehicle == "shares"
    assert "day 0 of 60" in note
    assert "D36 cannot choose" in note


def test_the_day_count_advances_with_the_series():
    _, note = vehicle_line(None, 17)
    assert "day 17 of 60" in note


def test_a_low_iv_rank_suggests_buying_optionality():
    vehicle, note = vehicle_line(0.15, 80)
    assert "debit spread" in vehicle
    assert "cheap" in note
    assert "defined risk" in note


def test_a_high_iv_rank_suggests_selling_it():
    vehicle, note = vehicle_line(0.75, 80)
    assert "premium sale" in vehicle
    assert "rich" in note


def test_the_middle_band_is_shares():
    vehicle, note = vehicle_line(0.40, 80)
    assert vehicle == "shares"
    assert "middle band" in note


def test_a_rank_given_as_a_percentage_is_handled():
    """`iv_daily.iv_rank_252` stores 0-1; a caller passing 40 must not read as
    4000%."""
    assert vehicle_line(40.0, 80)[0] == vehicle_line(0.40, 80)[0]


# ══════════════════════════════════════════════════════════════════════════
# D17/D44: sizing
# ══════════════════════════════════════════════════════════════════════════


def test_shares_are_sized_so_a_stop_out_costs_the_risk_budget():
    """2.5% of 100k is 2,500; at 5.50/share risk that is 454 shares."""
    assert share_count(100_000.0, 2.5, 5.50) == 454


def test_share_count_floors_rather_than_rounding():
    """Rounding up would put the position OVER its risk budget, which is the
    one direction D17 does not tolerate."""
    assert share_count(100_000.0, 2.5, 3.0) == 833  # 833.33 -> 833


def test_a_zero_risk_per_share_buys_nothing_rather_than_infinity():
    assert share_count(100_000.0, 2.5, 0.0) == 0


def test_tier_a_and_tier_b_size_differently_per_d17():
    a = build(scored=scored(tier="A"))
    b = build(scored=scored(tier="B", composite=60.0), result=result(plan=plan(target_2=140.0)))
    assert isinstance(a, Idea) and isinstance(b, Idea)
    assert a.risk_pct == RISK.per_trade_risk_pct.tier_a
    assert b.risk_pct == RISK.per_trade_risk_pct.tier_b
    assert a.shares > b.shares


def test_the_risk_amount_is_a_share_of_the_virtual_account():
    idea = build()
    assert idea.account_value == ACCOUNT
    assert idea.risk_amount == pytest.approx(ACCOUNT * idea.risk_pct / 100.0)


def test_a_stop_too_wide_for_the_account_is_refused_not_rounded_to_zero():
    # 2.5% of 100 is 2.50 against 5.50/share of risk -> zero shares. A 1,000
    # account still buys 4, which my first version of this test got wrong.
    wide = build(account_value=100.0)
    assert isinstance(wide, Rejection)
    assert wide.stage == "sizing"
    assert "zero shares" in wide.reason


# ══════════════════════════════════════════════════════════════════════════
# D12: the R:R gate does real work
# ══════════════════════════════════════════════════════════════════════════


def test_tier_a_refuses_an_rr_below_two():
    thin = build(result=result(plan=plan(target_2=106.0)))
    assert isinstance(thin, Rejection)
    assert thin.stage == "risk_reward"
    assert "2:1" in thin.reason


def test_tier_b_demands_more_than_tier_a():
    """D12: Tier B needs 3:1 because it is the lower-conviction tier.

    target_2=115 gives EXACTLY 3.00:1 and passes both gates — my first version
    used it expecting a refusal, which is a reminder that the gate is `>=`.
    114 gives 2.80:1, which clears Tier A's 2:1 and fails Tier B's 3:1.
    """
    geometry = plan(target_2=114.0)  # 2.80:1
    as_a = build(scored=scored(tier="A"), result=result(plan=geometry))
    as_b = build(scored=scored(tier="B", composite=60.0), result=result(plan=geometry))
    assert isinstance(as_a, Idea)
    assert isinstance(as_b, Rejection)
    assert "3:1" in as_b.reason


def test_an_untiered_symbol_builds_nothing():
    item = scored()
    item.tier = None
    item.tier_reason = "below min_surfaceable_composite"
    rejection = build(scored=item)
    assert isinstance(rejection, Rejection)
    assert rejection.stage == "tier"


# ══════════════════════════════════════════════════════════════════════════
# D19: event checks
# ══════════════════════════════════════════════════════════════════════════


def test_an_imminent_ex_date_refuses_a_new_long():
    rejection = build(catalyst=cata(days_to_ex_dividend=1))
    assert isinstance(rejection, Rejection)
    assert rejection.stage == "events"
    assert "D19(3)" in rejection.reason


def test_an_unknown_ex_date_is_noted_rather_than_assumed_clear():
    """D19's rule cannot be satisfied by ignorance, but the idea is not
    refused outright — the card carries the caveat."""
    idea = build(catalyst=cata(days_to_ex_dividend=None))
    assert isinstance(idea, Idea)
    assert any("could not be verified" in n for n in idea.notes)


def test_earnings_inside_the_horizon_is_flagged_on_the_card():
    idea = build(catalyst=cata(days_to_earnings=10))
    assert isinstance(idea, Idea)
    assert any("event risk is live" in n for n in idea.notes)


# ══════════════════════════════════════════════════════════════════════════
# D38/D44: the sector guard, scoped to one ladder state
# ══════════════════════════════════════════════════════════════════════════


def test_a_third_swing_in_a_sector_surfaces_watch_only():
    """D38: the information that the sector is working is worth keeping; the
    concentration is not."""
    full = book(by_sector={"Technology": 2}, swing_count=2)
    idea = build(book=full)
    assert isinstance(idea, Idea)
    assert any("WATCH ONLY" in n for n in idea.notes)
    assert any("D38" in n for n in idea.notes)


def test_a_symbol_already_open_in_this_book_is_refused():
    held = book(symbols={"TEST"})
    rejection = build(book=held)
    assert isinstance(rejection, Rejection)
    assert "already open" in rejection.reason


def test_the_d17_swing_cap_refuses_once_full():
    full = book(swing_count=RISK.max_concurrent_swings)
    rejection = build(book=full)
    assert isinstance(rejection, Rejection)
    assert "D17's limit" in rejection.reason


def test_the_heat_cap_refuses_before_the_cap_is_breached():
    hot = book(open_risk_pct=RISK.heat.max_open_heat_pct - 0.1)
    rejection = build(book=hot)
    assert isinstance(rejection, Rejection)
    assert "open heat" in rejection.reason


def test_the_books_are_separate_per_ladder_state_so_shadow_cannot_block_live():
    """D44. Without this a shadow idea blocks a live one, which is exactly
    backwards: the shadow book must be free to take every idea the strategy
    generates."""
    shadow_full = book(status="shadow", symbols={"TEST"}, swing_count=6)
    live_empty = book(status="live")
    assert isinstance(build(book=shadow_full), Rejection)
    assert isinstance(build(book=live_empty, status="live"), Idea)


# ══════════════════════════════════════════════════════════════════════════
# D18: the Tier B monthly budget
# ══════════════════════════════════════════════════════════════════════════


def test_an_exhausted_tier_b_budget_refuses():
    rejection = build(
        scored=scored(tier="B", composite=60.0),
        result=result(plan=plan(target_2=140.0)),
        tier_b_spent_pct=RISK.tier_b_budget.hard_ceiling_pct,
    )
    assert isinstance(rejection, Rejection)
    # D54 renamed the stage from the catch-all `sizing`: the budget, the heat
    # cap and the concurrency limit were three different causes under one
    # label, and D54 has to name each one to flag it on a certified call.
    assert rejection.stage == "tier_b_budget"
    assert "D18" in rejection.reason


def test_a_nearly_spent_budget_is_noted_on_the_card():
    idea = build(
        scored=scored(tier="B", composite=60.0),
        result=result(plan=plan(target_2=140.0)),
        tier_b_spent_pct=RISK.tier_b_budget.hard_ceiling_pct - 1.0,
    )
    assert isinstance(idea, Idea)
    assert any("nearly spent" in n for n in idea.notes)


def test_the_tier_b_budget_does_not_constrain_tier_a():
    idea = build(scored=scored(tier="A"), tier_b_spent_pct=99.0)
    assert isinstance(idea, Idea)


# ══════════════════════════════════════════════════════════════════════════
# D40/D41: status and mask on every card
# ══════════════════════════════════════════════════════════════════════════


def test_a_shadow_idea_is_flagged_not_actionable():
    idea = build(status="shadow")
    assert isinstance(idea, Idea)
    assert idea.actionable is False
    assert "NOT ACTIONABLE (shadow)" in idea.card()
    assert any("tracked, not traded" in n for n in idea.notes)


def test_a_live_idea_is_actionable():
    idea = build(status="live", book=book(status="live"))
    assert idea.actionable is True
    assert "NOT ACTIONABLE" not in idea.card()


def test_a_thin_composite_says_so_on_the_card():
    """D41: 72 from two pillars is not the claim 72 from four is."""
    idea = build(scored=scored(mask_letters="AB"))
    assert isinstance(idea, Idea)
    assert idea.mask == "A,B"
    assert any("2 of 4 pillars" in n for n in idea.notes)
    assert "[A,B]" in idea.card()


def test_a_full_house_does_not_carry_the_thin_composite_note():
    idea = build(scored=scored(mask_letters="ABCD"))
    assert not any("of 4 pillars" in n for n in idea.notes)


# ══════════════════════════════════════════════════════════════════════════
# THE CARD RENDERS COMPLETE ON THIN DATA
# ══════════════════════════════════════════════════════════════════════════


def test_the_card_renders_every_section_on_the_thinnest_possible_data():
    """A young engine's honest output: two pillars, no IV history, no ex-div
    date, shadow status. Every line must still be present and informative."""
    idea = build(
        scored=scored(tier="B", composite=61.0, mask_letters="AB"),
        result=result(plan=plan(target_2=140.0)),
        catalyst=cata(days_to_ex_dividend=None, days_to_earnings=None),
        iv_rank=None,
        iv_history_days=0,
    )
    assert isinstance(idea, Idea), idea
    card = idea.card()

    for section in (
        "sector",
        "composite",
        "entry",
        "stop",
        "targets",
        "R:R",
        "size",
        "vehicle",
        "horizon",
    ):
        assert section in card, f"the card is missing its {section} line"

    assert "IV rank unavailable, day 0 of 60" in card
    assert "[A,B]" in card
    assert "NOT ACTIONABLE" in card
    assert card.count("\n") >= 11
    # No blank value anywhere: every line has content after its label.
    for line in card.splitlines()[1:]:
        assert line.strip().split(None, 1)[1].strip(), f"blank value in: {line!r}"


def test_the_card_quotes_rr_to_tp2_and_shows_both_targets():
    idea = build()
    card = idea.card()
    assert "to TP2" in card
    assert "TP1" in card and "TP2" in card


def test_the_card_shows_the_account_the_size_resolves_against():
    """D44: a reader must be able to tell it was sized against the virtual
    account rather than a real balance."""
    assert "100,000" in build().card()


def test_sizing_uses_the_WORST_entry_while_rr_is_quoted_at_the_midpoint():
    """A deliberate asymmetry. Size against `entry_high` so a fill at the top
    of the zone still respects the risk budget; quote R:R at the midpoint,
    which is the expected fill. Sizing off the midpoint would let a top-of-zone
    fill exceed the budget D17 sets."""
    idea = build()
    assert isinstance(idea, Idea)
    worst_case_risk = (idea.entry_high - idea.stop) * idea.shares
    assert worst_case_risk <= idea.risk_amount + (idea.entry_high - idea.stop), (
        "a fill at the top of the zone must stay inside the risk budget"
    )
    midpoint_risk = ((idea.entry_low + idea.entry_high) / 2 - idea.stop) * idea.shares
    assert midpoint_risk < worst_case_risk


def test_an_exactly_threshold_rr_passes_because_the_gate_is_inclusive():
    """D12 reads ">= 2:1" and ">= 3:1"."""
    exact = build(scored=scored(tier="B", composite=60.0), result=result(plan=plan(target_2=115.0)))
    assert isinstance(exact, Idea)
    assert exact.risk_reward == pytest.approx(3.0)


# ══════════════════════════════════════════════════════════════════════════
# D42 amended: slots go to ideas that pass EVERY stage
# ══════════════════════════════════════════════════════════════════════════


def universe(*items) -> object:
    from scoring.composite import UniverseRun, rank_universe

    run = UniverseRun(scored=list(items))
    rank_universe(run.scored)
    return run


def contexts(mapping, *, shared_book=None, sectors=None):
    """A `build_context` that serves canned per-symbol inputs.

    `shared_book` is one OpenBook for every symbol, which is what makes the
    stateful guards testable: the walk must see its own acceptances in it.
    """
    the_book = shared_book if shared_book is not None else book()

    def build_context(item):
        entry = mapping.get(item.symbol)
        if entry is None:
            return None
        return {
            "result": entry,
            "catalyst": cata(),
            "sector": (sectors or {}).get(item.symbol, "Technology"),
            "status": "shadow",
            "book": the_book,
            "iv_rank": None,
            "iv_history_days": 0,
            "account_value": ACCOUNT,
        }

    return build_context


def test_a_gate_passing_plan_at_rank_six_is_surfaced_when_the_top_five_cannot_be():
    """THE LIVE BUG, 2026-10-23. Five composite leaders (EXPD, DVN, MPC, FTNT,
    VLO) held every slot with no recipe plan between them, and any plan-bearing
    idea at rank 6 was discarded before anything looked at it. Three days of
    that is three days of Shadow Book calls the engine measured nothing from.
    """
    from decision.idea_builder import assign_slots

    leaders = [
        scored(symbol=s, composite=c)
        for s, c in (("EXPD", 95.0), ("DVN", 94.0), ("MPC", 93.0), ("FTNT", 92.0), ("VLO", 91.0))
    ]
    sixth = scored(symbol="AAPL", composite=90.0)
    run = universe(*leaders, sixth)

    # Only the sixth has a plan at all.
    assignment = assign_slots(run, build_context=contexts({"AAPL": result()}))

    assert [idea.symbol for idea in assignment.accepted] == ["AAPL"]
    assert sixth.surfaced is True
    assert all(item.surfaced is False for item in leaders)
    assert assignment.walked == 6, "it had to look past all five to find one"
    stages = {symbol: r.stage for symbol, r in assignment.refused_by_symbol.items()}
    assert stages == dict.fromkeys(["EXPD", "DVN", "MPC", "FTNT", "VLO"], "plan")


def test_a_CERTIFIED_plan_on_a_symbol_WITH_NO_TIER_is_still_surfaced():
    """D53 END TO END, and the bug that made it only half true.

    `assign_slots` filtered its candidates on `tier is not None`, so a symbol
    the composite gave no tier never reached the branch D53 added inside
    `build_idea` — the composite gate was back in front of plan-building, one
    level earlier, and the branch written to stop exactly that was
    unreachable. Live symptom on 2026-10-26: a digest reporting "286 at plan"
    where 286 was the tier-qualifying population and ~145 symbols had not been
    looked at at all.
    """
    from decision.idea_builder import assign_slots

    no_tier = scored(symbol="HAS", composite=48.0, tier=None)
    no_tier.tier_reason = "composite 48.0 below Tier B's 55"
    run = universe(no_tier)

    assignment = assign_slots(
        run, build_context=contexts({"HAS": result(recipe="post_earnings_drift")})
    )

    assert [idea.symbol for idea in assignment.accepted] == ["HAS"]
    idea = assignment.accepted[0]
    assert idea.tier == "B", "certified but no composite Tier A: Tier B size (D53)"
    assert idea.composite_tier is None
    assert idea.composite_gate_passed is False, "recorded on the row, not silently dropped"


def test_an_UNCERTIFIED_recipe_still_needs_the_composite_gate():
    """The other half of D53, which the same filter also hid: the gate is not
    abolished, it is scoped to recipes that have not certified. Dropping it for
    everything would have made the filter's removal a strategy change."""
    from decision.idea_builder import assign_slots

    no_tier = scored(symbol="HAS", composite=48.0, tier=None)
    no_tier.tier_reason = "composite 48.0 below Tier B's 55"
    run = universe(no_tier)

    assignment = assign_slots(
        run, build_context=contexts({"HAS": result(recipe="pullback_to_trend")})
    )

    assert assignment.accepted == []
    assert assignment.refused_by_symbol["HAS"].stage == "tier"


def test_the_assignment_reports_how_many_symbols_it_CONSIDERED():
    """So the digest can state what was evaluated instead of leaving a reader
    to infer it from a stage count — which is how "286 at plan" was read as
    the universe when it was the tier-qualifying subset."""
    from decision.idea_builder import assign_slots

    items = [scored(symbol=f"S{i}", composite=95.0 - i, tier=None if i else "A") for i in range(8)]
    run = universe(*items)
    assignment = assign_slots(run, build_context=contexts({"S0": result()}), limit=1)

    assert assignment.considered == 8, "every scored symbol is a candidate (D53)"
    assert assignment.walked == 8, "and D54 walks all of them, not just to the first slot"


def test_a_rank_six_idea_is_surfaced_when_the_leaders_are_REFUSED_too():
    """The same hole one stage later: a plan that exists and fails D12's R:R
    gate used to take a slot and print a refusal. Walking past it is the point
    of running every stage before the cut."""
    from decision.idea_builder import assign_slots

    thin = result(plan=plan(target_2=104.0))  # R:R well under Tier A's 2:1
    leaders = [scored(symbol=s, composite=c) for s, c in (("EXPD", 95.0), ("DVN", 94.0))]
    sixth = scored(symbol="AAPL", composite=90.0)
    run = universe(*leaders, sixth)

    assignment = assign_slots(
        run,
        build_context=contexts({"EXPD": thin, "DVN": thin, "AAPL": result()}),
        limit=1,
    )
    assert [idea.symbol for idea in assignment.accepted] == ["AAPL"]
    assert assignment.refused_by_symbol["EXPD"].stage == "risk_reward"


def test_the_walk_RUNS_PAST_the_slot_limit_so_D54_can_log_what_it_finds():
    """It used to stop here, which let `max_ideas_per_day` decide what the
    engine LEARNS from rather than what a human reads at 16:45 (D54).

    The laziness is gone on purpose and the cost is bounded by the same thing
    as before: a symbol with no plan refuses at `plan` before `build_context`
    touches the database, so the walk is long and cheap rather than short.
    """
    from decision.idea_builder import assign_slots

    items = [scored(symbol=f"S{i}", composite=95.0 - i) for i in range(20)]
    run = universe(*items)
    assignment = assign_slots(
        run,
        build_context=contexts(
            {f"S{i}": result() for i in range(20)},
            # One sector each, so D38's two-per-sector guard does not bind and
            # the only thing stopping the walk is the slot count.
            sectors={f"S{i}": f"Sector{i}" for i in range(20)},
        ),
        limit=3,
    )
    assert len(assignment.accepted) == 3, "the digest still shows only the top N"
    assert assignment.walked == 20, "but every candidate is now evaluated (D54)"
    # `pullback_to_trend` is UNCERTIFIED, so the ones past the limit are
    # refused rather than logged — D54 records certified plans only.
    past = [r for _, r in assignment.refused if r.stage == "slot_rank"]
    assert len(past) == 17
    # `logged` is what reaches the Shadow Book: the ACCEPTED ideas as before
    # (D13 logs every surfaced call, certified or not), and under D54 also the
    # certified plans a cap or the slot limit kept out. Nothing certified fired
    # here, so it is exactly the three that took slots.
    assert [idea.symbol for idea in assignment.logged] == ["S0", "S1", "S2"]
    assert all(idea.would_be_taken for idea in assignment.logged)


def test_an_accepted_idea_counts_against_the_sector_guard_for_the_next_one():
    """THE SECOND BUG. One immutable book was handed to every candidate, so
    five ideas accepted the same day in the same sector each saw the sector
    count as it was BEFORE any of them — D38's guard read as enforced and was
    not."""
    from decision.idea_builder import assign_slots

    items = [scored(symbol=f"S{i}", composite=95.0 - i) for i in range(4)]
    run = universe(*items)
    shared = book()
    assignment = assign_slots(
        run,
        build_context=contexts(
            {f"S{i}": result() for i in range(4)},
            shared_book=shared,
            sectors=dict.fromkeys([f"S{i}" for i in range(4)], "Energy"),
        ),
        limit=4,
    )
    guard = config.risk().sector_guard.max_concurrent_swings_per_sector
    assert len(assignment.accepted) == guard, f"D38 allows {guard} positions per sector"
    assert assignment.watch_only, "D38 surfaces the excess rather than hiding it"
    assert shared.by_sector["Energy"] == guard, (
        "a watch-only idea is not a position and must not be counted as one"
    )
    assert all("WATCH ONLY" in " ".join(idea.notes) for idea in assignment.watch_only)
    assert all(
        idea.symbol not in {a.symbol for a in assignment.accepted} for idea in assignment.watch_only
    ), "outside the N, not inside it"
    declined = assignment.declined_by_symbol()
    assert declined[assignment.watch_only[0].symbol].stage == "sector"


def test_an_accepted_idea_counts_against_the_heat_cap_for_the_next_one():
    """D17's 10% total open heat, applied within the day's own decisions."""
    from decision.idea_builder import assign_slots

    items = [scored(symbol=f"S{i}", composite=95.0 - i) for i in range(10)]
    run = universe(*items)
    shared = book()
    assignment = assign_slots(
        run,
        build_context=contexts(
            {f"S{i}": result() for i in range(10)},
            shared_book=shared,
            sectors={f"S{i}": f"Sector{i}" for i in range(10)},
        ),
        limit=10,
    )
    cap = config.risk().heat.max_open_heat_pct
    assert shared.open_risk_pct <= cap + 1e-9
    assert any(r.stage == "heat" for _, r in assignment.refused), (
        "the cap has to refuse someone, or it is not a cap"
    )


def test_a_watch_only_idea_does_not_consume_an_actionable_slot():
    """D38's excess is INFORMATION, not a call. Letting it hold a slot would be
    the 2026-10-23 bug in a new costume: the digest full of ideas the engine
    declined to size, and a tradeable one discarded below them."""
    from decision.idea_builder import assign_slots

    # Three in one sector (the third is watch-only), then one elsewhere.
    items = [
        scored(symbol=s, composite=c)
        for s, c in (("E1", 95.0), ("E2", 94.0), ("E3", 93.0), ("T1", 92.0))
    ]
    run = universe(*items)
    shared = book()
    assignment = assign_slots(
        run,
        build_context=contexts(
            {s: result() for s in ("E1", "E2", "E3", "T1")},
            shared_book=shared,
            sectors={"E1": "Energy", "E2": "Energy", "E3": "Energy", "T1": "Technology"},
        ),
        limit=3,
    )
    assert [idea.symbol for idea in assignment.accepted] == ["E1", "E2", "T1"]
    assert [idea.symbol for idea in assignment.watch_only] == ["E3"]
    assert run.scored[2].surfaced is False, "watch-only is not a Shadow Book call"


# ══════════════════════════════════════════════════════════════════════════
# D53: the composite is a conviction upgrade for a certified recipe
# ══════════════════════════════════════════════════════════════════════════


def _with_certification(recipe: str, **kw) -> object:
    entry = {"status": "certified", "min_risk_reward": 2.0, "evidence": "abc1234 / deadbeef"}
    entry.update(kw)
    return config.ScoringConfig.model_validate(
        {
            **SCORING.model_dump(),
            "certification": {
                **{k: v.model_dump() for k, v in SCORING.certification.items()},
                recipe: entry,
            },
        }
    )


CERTIFIED = _with_certification("pullback_to_trend")


def test_a_certified_recipes_plan_surfaces_with_no_composite_tier_at_all():
    """D53'S WHOLE POINT. The forcing evidence: of 310 tier-qualifying ideas on
    2026-10-21, five produced a plan and the only one that passed every stage
    sat at rank 51. A filter nobody has certified must not veto a recipe
    somebody has."""
    built = build(scored=scored(tier=None, composite=41.0), cfg=CERTIFIED)
    assert isinstance(built, Idea), built
    assert built.tier == "B", "sized at Tier B by default"
    assert built.composite_tier is None
    assert built.composite_gate_passed is False
    assert any("conviction upgrade" in note for note in built.notes)


def test_the_composite_tier_a_gate_upgrades_the_SIZE():
    a = build(scored=scored(tier="A"), cfg=CERTIFIED)
    b = build(scored=scored(tier="B", composite=62.0), cfg=CERTIFIED)
    assert a.tier == "A" and b.tier == "B"
    assert a.risk_pct > b.risk_pct, "Tier A size is the upgrade the composite earns"
    assert a.composite_tier == "A" and b.composite_tier == "B"
    assert a.composite_gate_passed is True and b.composite_gate_passed is True


def test_a_certified_recipe_is_gated_on_ITS_OWN_r_to_r():
    """Not the tier's. A Tier-B-sized plan under D53 would otherwise face Tier
    B's 3:1 gate, which is calibrated to a tier and not to this recipe's
    evidence."""
    thin = result(plan=plan(target_2=104.0))  # R:R ~0.8
    refused = build(scored=scored(tier=None), result=thin, cfg=CERTIFIED)
    assert isinstance(refused, Rejection)
    assert refused.stage == "risk_reward"
    assert "certified minimum for pullback_to_trend" in refused.reason

    ok = build(scored=scored(tier=None), result=result(plan=plan(target_2=125.0)), cfg=CERTIFIED)
    assert isinstance(ok, Idea), "2.4:1 clears the recipe's 2.0 even with no tier"


def test_an_uncertified_recipe_keeps_the_composite_gate():
    """Where there is no evidence, the old conservatism stands."""
    refused = build(scored=scored(tier=None, composite=41.0))
    assert isinstance(refused, Rejection)
    assert refused.stage == "tier"


def test_every_idea_records_what_the_composite_alone_said():
    """The flag ships WITH the change that stopped gating on it, or the change
    erases its own evidence. Expectancy split on it IS O18's answer."""
    for tier, expected in (("A", "A"), ("B", "B"), (None, None)):
        built = build(scored=scored(tier=tier, composite=82.0 if tier else 41.0), cfg=CERTIFIED)
        assert isinstance(built, Idea), built
        assert built.composite_tier == expected
        assert built.composite_gate_passed is (expected is not None)


# ── D54: shadow maximizes evidence ──────────────────────────────────────────

# The recipe NAME that is certified in the shipped config. Distinct from
# `CERTIFIED` above, which is a scoring CONFIG these tests pass as `cfg=`.
CERTIFIED_RECIPE = "post_earnings_drift"


def test_a_certified_plan_PAST_the_slot_limit_is_logged_not_discarded():
    """D54. `max_ideas_per_day` decides what a human reads at 16:45; it has no
    business deciding what the engine learns from. A shadow call costs nothing
    and a day not recorded cannot be recovered."""
    from decision.idea_builder import assign_slots

    items = [scored(symbol=f"S{i}", composite=95.0 - i) for i in range(4)]
    run = universe(*items)
    assignment = assign_slots(
        run,
        build_context=contexts(
            {f"S{i}": result(recipe=CERTIFIED_RECIPE) for i in range(4)},
            sectors={f"S{i}": f"Sector{i}" for i in range(4)},
        ),
        limit=1,
    )

    assert [idea.symbol for idea in assignment.accepted] == ["S0"], "the digest shows one"
    assert [idea.symbol for idea in assignment.logged] == ["S0", "S1", "S2", "S3"]
    past = [idea for idea in assignment.logged if "slot_rank" in idea.would_refuse]
    assert [idea.slot_rank for idea in past] == [2, 3, 4]
    assert all(not idea.would_be_taken for idea in past)
    assert assignment.logged[0].would_be_taken, "the one that took the slot would be taken"


def test_a_cap_FLAGS_a_certified_plan_and_REFUSES_an_uncertified_one():
    """The asymmetry is D53's: where a recipe has certified, the plan is
    evidence whatever the book looks like. Where nothing has shown its plans
    are worth recording, the old conservatism stands."""
    from decision.idea_builder import assign_slots

    full = book(swing_count=RISK.max_concurrent_swings)
    for recipe, expect_logged in ((CERTIFIED_RECIPE, True), ("pullback_to_trend", False)):
        run = universe(scored(symbol="AAPL"))
        assignment = assign_slots(
            run, build_context=contexts({"AAPL": result(recipe=recipe)}, shared_book=full)
        )
        assert assignment.accepted == [], "the cap still keeps it out of the book"
        if expect_logged:
            [idea] = assignment.logged
            assert idea.would_refuse == ("concurrency",)
            assert any("WOULD NOT BE TAKEN" in note for note in idea.notes)
        else:
            assert assignment.logged == []
            assert assignment.refused_by_symbol["AAPL"].stage == "concurrency"


def test_a_flagged_call_RESERVES_NOTHING_in_the_book():
    """It is not a position. Charging the heat cap for risk nobody took would
    refuse the next real idea on a trade that does not exist — the same reason
    D38's watch-only reserves nothing."""
    from decision.idea_builder import assign_slots

    shared = book(open_risk_pct=RISK.heat.max_open_heat_pct)
    run = universe(scored(symbol="AAPL"), scored(symbol="MSFT", composite=80.0))
    assignment = assign_slots(
        run,
        build_context=contexts(
            {"AAPL": result(recipe=CERTIFIED_RECIPE), "MSFT": result(recipe=CERTIFIED_RECIPE)},
            shared_book=shared,
            sectors={"AAPL": "Technology", "MSFT": "Healthcare"},
        ),
    )

    assert len(assignment.logged) == 2
    assert all(idea.would_refuse == ("heat",) for idea in assignment.logged)
    assert shared.open_risk_pct == pytest.approx(RISK.heat.max_open_heat_pct), (
        "a flagged call must not move the book's heat"
    )


def test_a_certified_WATCH_ONLY_call_is_logged_with_its_sector_flag():
    """D54 amends D42 here, deliberately: the engine declined to SIZE it, so it
    takes no slot and reserves nothing, but the plan is still evidence about
    the recipe and the row says why it was not taken."""
    from decision.idea_builder import assign_slots

    full_sector = book(by_sector={"Technology": RISK.sector_guard.max_concurrent_swings_per_sector})
    run = universe(scored(symbol="AAPL"))
    assignment = assign_slots(
        run,
        build_context=contexts({"AAPL": result(recipe=CERTIFIED_RECIPE)}, shared_book=full_sector),
    )

    assert assignment.accepted == []
    assert [idea.symbol for idea in assignment.watch_only] == ["AAPL"]
    [idea] = assignment.logged
    assert idea.would_refuse == ("sector",)
    assert idea.watch_only is True
    assert not idea.would_be_taken


def test_the_constraint_vocabulary_matches_what_the_builder_can_emit():
    """`shadow_book.would_refuse` stores these strings and the digest reads
    them back, so a name invented in one place and not the other would write
    rows nobody can interpret."""
    from decision.idea_builder import CONSTRAINTS

    assert set(CONSTRAINTS) == {"tier_b_budget", "heat", "sector", "concurrency", "slot_rank"}


def test_a_cap_BINDS_at_paper_even_for_a_certified_recipe():
    """D54 says shadow records and paper ENFORCES, and D40's amendment judges
    paper->live on the paper book alone BECAUSE the caps bind there. A paper
    call a cap would have refused must therefore not exist — otherwise the
    portfolio question is answered with calls the portfolio never held."""
    from decision.idea_builder import assign_slots

    full = book(swing_count=RISK.max_concurrent_swings)
    full.status = "paper"
    run = universe(scored(symbol="AAPL"))
    assignment = assign_slots(
        run,
        build_context=lambda item: {
            "result": result(recipe=CERTIFIED_RECIPE),
            "catalyst": cata(),
            "sector": "Technology",
            "status": "paper",
            "book": full,
            "iv_rank": None,
            "iv_history_days": 0,
            "account_value": ACCOUNT,
        },
    )

    assert assignment.logged == [], "paper does not record what it would not take"
    assert assignment.refused_by_symbol["AAPL"].stage == "concurrency"


# ── D45/D56: the regime, and the counter-evidence call ──────────────────────


def test_every_card_SHOWS_the_regime_and_what_the_recipe_measured_there():
    """D56. v3's out-of-sample edge is almost all `bull_calm`, so a human
    deciding on a call needs the cell it landed in on the card, not in their
    memory of a backtest report."""
    idea = build(regime="bull_calm")
    card = idea.card()
    assert "regime      bull_calm" in card
    assert "+0.51R in this regime" in card
    assert "506 closed" in card
    assert "f572337" in card, "the number travels with the run that measured it"
    assert "2024-10-01..2026-10-01" in card, "and with the window"


def test_a_call_in_a_NEGATIVE_cell_is_labelled_COUNTER_EVIDENCE():
    """`bull_normal` measured -0.09R on 432 closed — a real sample. A pullback
    call there is a call the evidence argues against, and the card says so in
    those words rather than leaving it to be inferred from a number."""
    card = build(regime="bull_normal").card()
    assert "COUNTER-EVIDENCE" in card
    assert "-0.09R" in card
    assert "against +0.51R in bull_calm" in card, "the contrast is the point"


def test_a_THIN_cell_is_NO_evidence_rather_than_good_evidence():
    """`bear_normal` measured +1.03R and would be the best cell on the table —
    on 18 closed trades. Under D39(2) that is not evidence at all, and a card
    printing it as a positive would be the most flattering reading of the
    smallest sample."""
    card = build(regime="bear_normal").card()
    assert "NO evidence rather than weak evidence" in card
    assert "COUNTER-EVIDENCE" not in card


def test_an_UNKNOWN_regime_says_the_call_cannot_be_PLACED():
    """Before the benchmark has 200 sessions there is no cell to compare to,
    and `unknown` must not read as "no edge here"."""
    card = build(regime=None).card()
    assert "regime      unknown" in card
    assert "cannot be placed" in card


def test_a_recipe_with_NO_evidence_block_says_so_instead_of_implying_zero():
    """`post_earnings_drift` has no per-regime block on record yet. Silence
    there would read as zero, which is a measurement nobody took."""
    card = build(result=result(recipe=CERTIFIED_RECIPE), regime="bull_calm").card()
    assert "no per-regime backtest evidence on record" in card
