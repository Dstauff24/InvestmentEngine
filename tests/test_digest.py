"""The daily digest (D1/D16/D41/D42). Fixtures only — no DB, no network.

The zero-plan shape is the one that matters: the engine saying "nothing today"
is edge source #1 (D1), and the digest has to read like a decision.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from alerts import digest
from alerts.digest import DigestContent, render
from core import config

DAY = date(2026, 10, 21)


def content(**kw) -> DigestContent:
    base = {
        "session_date": DAY,
        "cards": [],
        "refusals": [],
        "candidates": [],
        "stats": None,
        "closed": [],
        "open_calls": [],
    }
    base.update(kw)
    return DigestContent(**base)


STATS = {
    "symbols_scored": 432,
    "symbols_abstained": 1,
    "tier_a_count": 15,
    "tier_b_count": 175,
    "surfaced_count": 5,
    "not_surfaced_count": 185,
    "mask_histogram": {"2": 263, "3": 137, "4": 32},
    "composite_min": 18.68,
    "composite_median": 54.05,
    "composite_p90": 74.20,
    "composite_max": 92.92,
}

CANDIDATES = [
    {
        "symbol": "EXPD",
        "composite": 92.9,
        "percentile": 100.0,
        "mask": "A,B",
        "reason": "no recipe fired",
    },
    {
        "symbol": "MPC",
        "composite": 92.0,
        "percentile": 100.0,
        "mask": "A,B",
        "reason": "no recipe fired",
    },
]


# ══════════════════════════════════════════════════════════════════════════
# The zero-plan shape is NOT a fallback
# ══════════════════════════════════════════════════════════════════════════


def test_a_zero_plan_day_says_nothing_today_in_the_subject():
    subject, _ = render(content(stats=STATS, candidates=CANDIDATES))
    assert "nothing today" in subject


def test_the_zero_plan_body_frames_selectivity_as_the_engine_working():
    """D1: selectivity is edge source #1. A digest that said so apologetically
    would train the reader to treat silence as a failure."""
    _, body = render(content(stats=STATS, candidates=CANDIDATES))
    assert "NOTHING TODAY" in body
    assert "the engine working, not failing" in body
    assert "edge source #1" in body


def test_the_zero_plan_body_reports_what_was_looked_at():
    _, body = render(content(stats=STATS, candidates=CANDIDATES))
    assert "Scored 432 symbols" in body
    assert "Tier A 15" in body and "Tier B 175" in body
    assert "aggregated 185 (D42)" in body


def test_the_zero_plan_body_reports_the_composite_distribution():
    _, body = render(content(stats=STATS, candidates=CANDIDATES))
    assert "median 54.05" in body
    assert "max 92.92" in body


def test_the_zero_plan_body_reports_the_pillar_mask_histogram():
    """D41: a composite built on two pillars is not the claim four would be,
    and a reader comparing days needs to know which they are looking at."""
    _, body = render(content(stats=STATS, candidates=CANDIDATES))
    assert "evidence depth (D41)" in body
    assert "2 pillar(s): 263" in body
    assert "not the claim four would be" in body


def test_the_zero_plan_body_names_the_closest_candidates():
    _, body = render(content(stats=STATS, candidates=CANDIDATES))
    assert "CLOSEST 2" in body
    assert "EXPD" in body and "p100" in body
    assert "[A,B]" in body


def test_the_zero_plan_body_states_the_gates_in_force():
    """So a reader can judge the near-misses against the bar they missed."""
    _, body = render(content(stats=STATS, candidates=CANDIDATES))
    assert "Gates in force" in body
    assert "Tier A composite >=" in body


def test_the_highest_scoring_no_setup_names_are_a_section_of_their_own():
    """D42 AMENDED. These symbols out-scored the calls and could not be traded,
    which is worth printing BY NAME and in its own section — the live bug on
    2026-10-23 was five such names silently occupying every actionable slot."""
    _, body = render(
        content(
            stats=STATS,
            candidates=CANDIDATES,
            refusals=["EXPD: REFUSED at risk_reward — R:R 1.4 below 3:1"],
        )
    )
    assert "HIGHEST-SCORING, NO SETUP" in body
    assert "R:R 1.4 below 3:1" in body


def test_the_refusal_counts_say_how_far_the_walk_went():
    """The number that would have made the surfacing bug visible on its first
    evening: 305 symbols produced no setup, and the engine had to walk past all
    of them. Printed as counts rather than 305 lines."""
    _, body = render(
        content(
            stats=STATS,
            candidates=CANDIDATES,
            refusals=["EXPD: REFUSED at plan — no recipe produced a plan"],
            refusal_summary={"plan": 305, "risk_reward": 4},
        )
    )
    assert "declined by stage — 305 at plan, 4 at risk_reward" in body
    assert "a high composite is not a setup" in body


def test_an_incomplete_day_prints_no_refusal_counts_either():
    """The 2026-10-22 lesson, applied to the new section: if scoring did not
    complete, the counts describe nothing and must not be shown."""
    _, body = render(
        content(
            refusals=["EXPD: REFUSED at plan"],
            refusal_summary={"plan": 305},
            blocked=[{"job_name": "scoring_pass", "status": "failed", "error": "boom"}],
        )
    )
    assert "HIGHEST-SCORING" not in body
    assert "305" not in body


def test_a_zero_plan_digest_still_renders_without_stats():
    """A first-ever run has no aggregate row yet; the digest must not break."""
    _, body = render(content())
    assert "NOTHING TODAY" in body
    assert body.strip()


# ══════════════════════════════════════════════════════════════════════════
# The idea shape
# ══════════════════════════════════════════════════════════════════════════


def test_a_day_with_ideas_leads_with_them():
    subject, body = render(content(cards=["AAPL  Tier A  pullback_to_trend\n  entry 1-2"]))
    assert "1 idea(s)" in subject
    assert body.startswith("IDEAS FOR")
    assert "AAPL" in body


def test_a_refusal_in_cards_would_have_mis_shaped_the_digest():
    """The bug this separation fixed: `has_ideas` decides the shape, so a
    refusal placed in `cards` rendered a zero-plan day as an IDEAS digest whose
    ideas all said there were none."""
    assert content(cards=[], refusals=["X: no plan"]).has_ideas is False
    assert content(cards=["a real card"]).has_ideas is True


# ══════════════════════════════════════════════════════════════════════════
# Yesterday's news
# ══════════════════════════════════════════════════════════════════════════


def test_closed_ideas_are_reported_with_their_outcome():
    """A digest that only showed new ideas would never tell the reader that
    last week's idea just stopped out — the half that costs money."""
    closed = [
        {
            "symbol": "AAPL",
            "recipe": "pullback_to_trend",
            "tier": "A",
            "outcome": "stopped",
            "r_multiple": -1.0,
            "return_pct": -5.2,
            "days_held": 4,
            "actual_entry": 100.0,
            "actual_exit": 95.0,
            "hit_target_1": False,
            "hit_target_2": False,
        }
    ]
    _, body = render(content(stats=STATS, closed=closed))
    assert "CLOSED SINCE YESTERDAY" in body
    assert "STOPPED" in body
    assert "-1.00R" in body
    assert "-5.2%" in body


def test_a_win_is_reported_with_a_positive_r():
    closed = [
        {
            "symbol": "MSFT",
            "recipe": "base_breakout",
            "tier": "A",
            "outcome": "target_2",
            "r_multiple": 3.4,
            "return_pct": 12.1,
            "days_held": 9,
            "actual_entry": 100.0,
            "actual_exit": 112.0,
            "hit_target_1": True,
            "hit_target_2": True,
        }
    ]
    _, body = render(content(stats=STATS, closed=closed))
    assert "+3.40R" in body


def test_open_calls_are_listed_with_their_current_stop():
    open_calls = [
        {
            "symbol": "NVDA",
            "recipe": "pullback_to_trend",
            "tier": "B",
            "called_on": date(2026, 10, 15),
            "proj_target_1": 110.0,
            "current_stop": 100.0,
            "hit_target_1": True,
            "stop_to_breakeven_on": date(2026, 10, 18),
        }
    ]
    _, body = render(content(stats=STATS, open_calls=open_calls))
    assert "STILL OPEN (1)" in body
    assert "NVDA" in body
    assert "stop at breakeven" in body, "D18 zeroed the heat; the reader needs to know"


def test_a_digest_with_ideas_also_carries_yesterdays_outcomes():
    closed = [
        {
            "symbol": "AAPL",
            "recipe": "pullback_to_trend",
            "tier": "A",
            "outcome": "timeout",
            "r_multiple": 0.2,
            "return_pct": 1.0,
            "days_held": 15,
            "actual_entry": 100.0,
            "actual_exit": 101.0,
            "hit_target_1": False,
            "hit_target_2": False,
        }
    ]
    _, body = render(content(cards=["a card"], closed=closed))
    assert body.startswith("IDEAS FOR")
    assert "CLOSED SINCE YESTERDAY" in body


def test_a_missing_number_renders_as_a_dash_not_an_error():
    open_calls = [
        {
            "symbol": "X",
            "recipe": "r",
            "tier": "A",
            "called_on": DAY,
            "proj_target_1": None,
            "current_stop": None,
            "hit_target_1": None,
            "stop_to_breakeven_on": None,
        }
    ]
    _, body = render(content(stats=STATS, open_calls=open_calls))
    assert "stop —" in body


def test_the_body_always_ends_with_exactly_one_newline():
    for kw in ({}, {"cards": ["a"]}, {"stats": STATS}):
        _, body = render(content(**kw))
        assert body.endswith("\n") and not body.endswith("\n\n")


# ══════════════════════════════════════════════════════════════════════════
# The shape that exists because the selectivity message lied (2026-10-22)
# ══════════════════════════════════════════════════════════════════════════

# Every phrase that claims the engine made a judgement. None of them may
# appear on a night when nothing was judged.
SELECTIVITY_LANGUAGE = (
    "NOTHING TODAY",
    "nothing today",
    "working, not failing",
    "selectivity",
    "edge source",
    "forced idea",
    "No idea cleared its gates",
    "RANKED BUT NOT ACTIONABLE",
    "CLOSEST",
)


def blocked_content(**kw) -> digest.DigestContent:
    base = {
        "session_date": DAY,
        "cards": [],
        "refusals": [],
        "candidates": [],
        "stats": None,
        "closed": [],
        "open_calls": [],
        "blocked": [
            {
                "job_name": "scoring_pass",
                "status": "failed",
                "error": "SettingsError: SHADOW_ACCOUNT_VALUE is not set\nstack...",
            }
        ],
        "degraded": [],
    }
    base.update(kw)
    return digest.DigestContent(**base)


def test_a_failed_scoring_pass_can_never_render_as_selectivity():
    """THE 2026-10-22 VM BUG. `scoring_pass` died (SHADOW_ACCOUNT_VALUE was
    missing) and the digest went out saying "NOTHING TODAY — that is the engine
    working, not failing". Nothing had been evaluated at all: the two shapes
    were chosen on whether `cards` was empty, and a dead step produces an empty
    list exactly like a quiet market does."""
    subject, body = digest.render(blocked_content())

    assert subject.startswith("SCORING INCOMPLETE")
    assert body.startswith("SCORING INCOMPLETE: no ideas were evaluated tonight")
    for phrase in SELECTIVITY_LANGUAGE:
        assert phrase not in body, (
            f"selectivity language leaked into an incomplete digest: {phrase}"
        )


def test_the_incomplete_digest_names_the_step_and_its_error():
    """ "Something went wrong" sends the reader to the logs; naming the step
    and the first line of its error usually ends the investigation."""
    _, body = digest.render(blocked_content())
    assert "scoring_pass: failed" in body
    assert "SHADOW_ACCOUNT_VALUE is not set" in body
    assert "job_runs" in body, "and says where the rest of the error lives"


def test_a_step_that_never_ran_is_also_incomplete():
    """A pipeline that stopped before scoring is not a quiet market either."""
    content = blocked_content(
        blocked=[{"job_name": "scoring_pass", "status": "never ran", "error": None}]
    )
    _, body = digest.render(content)
    assert "scoring_pass: never ran" in body
    assert "no error recorded" in body
    for phrase in SELECTIVITY_LANGUAGE:
        assert phrase not in body


def test_cards_handed_in_after_a_failed_pass_are_discarded():
    """After a failed pass the caller's lists are the empty defaults of a
    result that was never produced — but if anything IS in them it is stale,
    and reporting it would be worse than reporting nothing."""
    content = blocked_content(cards=["AAPL: a card from a previous run"])
    assert content.has_ideas is False
    _, body = digest.render(content)
    assert "AAPL" not in body


def test_the_selectivity_report_refuses_to_be_built_in_that_state():
    """Belt and braces: the text that reassures must be unreachable from the
    state that would make it false, not merely unused by one branch."""
    with pytest.raises(AssertionError, match="scoring did not complete"):
        digest._selectivity_report(blocked_content(), config.scoring())


def test_a_quiet_market_still_reports_selectivity():
    """The fix must not turn every zero-plan day into an alarm: a completed
    pass that surfaced nothing is the engine working, and that message is the
    whole of D1."""
    content = digest.DigestContent(
        session_date=DAY,
        cards=[],
        refusals=[],
        candidates=[],
        stats=None,
        closed=[],
        open_calls=[],
        blocked=[],
    )
    subject, body = digest.render(content)
    assert "nothing today" in subject
    assert "working, not failing" in body
    assert "SCORING INCOMPLETE" not in body


def test_a_failed_outcome_tracker_warns_without_leading():
    """It degrades the digest without making the idea half dishonest, so it is
    a note under the ideas rather than the headline."""
    content = digest.DigestContent(
        session_date=DAY,
        cards=[],
        refusals=[],
        candidates=[],
        stats=None,
        closed=[],
        open_calls=[],
        blocked=[],
        degraded=[{"job_name": "outcome_tracker", "status": "failed", "error": "boom"}],
    )
    _, body = digest.render(content)
    assert "SCORING INCOMPLETE" not in body
    assert "outcome tracking did not complete" in body
    assert "may be stale" in body


# ══════════════════════════════════════════════════════════════════════════
# The dedupe key that blocked a correction (2026-10-22)
# ══════════════════════════════════════════════════════════════════════════


def clean_content(day=DAY, **kw) -> digest.DigestContent:
    base = {
        "session_date": day,
        "cards": [],
        "refusals": [],
        "candidates": [],
        "stats": None,
        "closed": [],
        "open_calls": [],
        "blocked": [],
    }
    base.update(kw)
    return digest.DigestContent(**base)


def test_a_corrected_digest_supersedes_the_failure_notice():
    """THE BUG. The VM sent SCORING INCOMPLETE, the operator fixed the setting
    and re-ran, and the corrected digest was suppressed as a duplicate of the
    broken one — leaving the reader with the failure notice and no idea the day
    had recovered."""
    failed = blocked_content().dedupe_key
    corrected = clean_content().dedupe_key

    assert failed != corrected, "both must deliver"
    assert "incomplete" in failed
    assert corrected.endswith(":ok")


def test_a_clean_rerun_of_an_already_clean_day_stays_suppressed():
    """The dedupe still has to work: re-running a good evening must not page
    the reader twice with the same message."""
    assert clean_content().dedupe_key == clean_content().dedupe_key


def test_two_failures_of_the_same_step_are_one_message():
    """Same problem, same day, already paged."""
    first = blocked_content().dedupe_key
    again = blocked_content().dedupe_key
    assert first == again


def test_a_different_step_failing_is_a_different_message():
    """A new broken step is a new problem, not a repeat of the old one."""
    scoring = blocked_content().dedupe_key
    other = blocked_content(
        blocked=[{"job_name": "schwab_prices", "status": "failed", "error": "x"}]
    ).dedupe_key
    assert scoring != other
    assert "scoring_pass" in scoring and "schwab_prices" in other


def test_two_steps_failing_together_key_stably_whatever_the_order():
    """A set of failures is one state; its key must not depend on row order."""
    a = blocked_content(
        blocked=[
            {"job_name": "scoring_pass", "status": "failed", "error": None},
            {"job_name": "outcome_tracker", "status": "failed", "error": None},
        ]
    ).dedupe_key
    b = blocked_content(
        blocked=[
            {"job_name": "outcome_tracker", "status": "failed", "error": None},
            {"job_name": "scoring_pass", "status": "failed", "error": None},
        ]
    ).dedupe_key
    assert a == b


def test_the_key_is_scoped_to_the_session_date():
    assert clean_content(day=DAY).dedupe_key != clean_content(day=date(2026, 10, 22)).dedupe_key


def test_a_session_whose_run_is_not_due_has_no_incomplete_steps(monkeypatch):
    """The same rule as the scorecard's: before 16:30 ET nothing was supposed
    to have happened, so an absent row is not a failed step. A report rendered
    at lunchtime must not read like a broken engine."""
    monkeypatch.setattr(digest.db, "fetch_all", lambda *a, **k: [])
    noon_et = datetime(2026, 10, 23, 16, 0, tzinfo=UTC)
    evening_et = datetime(2026, 10, 23, 21, 0, tzinfo=UTC)
    day = date(2026, 10, 23)

    assert digest.incomplete_steps(day, ("scoring_pass",), now=noon_et) == []
    after = digest.incomplete_steps(day, ("scoring_pass",), now=evening_et)
    assert [r["status"] for r in after] == ["never ran"]


def test_a_failed_step_is_reported_even_before_the_run_is_due(monkeypatch):
    """The exemption is for ABSENCE, not for errors."""
    monkeypatch.setattr(
        digest.db,
        "fetch_all",
        lambda *a, **k: [
            {"job_name": "scoring_pass", "status": "failed", "error": "boom", "finished_at": None}
        ],
    )
    noon_et = datetime(2026, 10, 23, 16, 0, tzinfo=UTC)
    [row] = digest.incomplete_steps(date(2026, 10, 23), ("scoring_pass",), now=noon_et)
    assert row["status"] == "failed"


def test_watch_only_ideas_get_their_own_section_with_the_d38_reason():
    """D42 amended: outside the actionable N, in a section of their own. A
    watch-only idea holding a slot would be the 2026-10-23 bug in a new
    costume — the digest full of ideas the engine declined to size."""
    _, body = render(
        content(
            cards=["AAPL  Tier A  entry 100.00"],
            stats=STATS,
            watch_only=["XOM  Tier A  WATCH ONLY — 2 swing(s) already open in Energy"],
        )
    )
    assert "WATCH ONLY (D38 — the sector is at its limit)" in body
    assert "already open in Energy" in body
    assert "no slot taken, no risk booked" in body


def test_the_three_sections_are_separate_and_in_order():
    """Calls, watch-only, highest-scoring-no-setup. A reader must be able to
    tell "I could trade this" from "this is information" from "this scored
    higher and produced nothing"."""
    _, body = render(
        content(
            cards=["AAPL  Tier A  entry 100.00"],
            stats=STATS,
            watch_only=["XOM  WATCH ONLY — Energy is full"],
            refusals=["EXPD: REFUSED at plan — no recipe produced a plan"],
            refusal_summary={"plan": 305},
        )
    )
    watch = body.index("WATCH ONLY (D38")
    no_setup = body.index("HIGHEST-SCORING, NO SETUP")
    assert body.index("AAPL") < watch < no_setup


def test_watch_only_lines_are_not_counted_as_ideas():
    """The lesson that produced the third digest shape: counting strings
    instead of ideas is how a zero-plan evening reports itself as an idea
    digest."""
    card = content(stats=STATS, candidates=CANDIDATES, watch_only=["XOM  WATCH ONLY"])
    assert card.has_ideas is False
    _, body = render(card)
    assert "NOTHING TODAY" in body or "no idea cleared" in body.lower()
    assert "WATCH ONLY" in body


def test_the_stage_line_NAMES_THE_POPULATION_it_counted():
    """2026-10-26: the digest said "declined by stage — 286 at plan" and 286
    was the TIER-QUALIFYING subset, not the universe — so a reader could not
    tell whether the engine had looked at everything, and the answer turned out
    to be no (a D53 bug). The counts now travel with the population."""
    content = digest.build_digest(
        [],
        [],
        refusals=["AAPL  no recipe produced a plan"],
        refusal_summary={"plan": 286, "tier": 140, "risk_reward": 3},
        evaluated=429,
        considered=429,
        as_of=DAY,
    )
    _, body = digest.render(content)
    assert "evaluated 429 of 429 scored symbol(s)" in body
    assert "the composite does not decide who is looked at" in body


def test_a_walk_that_stopped_early_SAYS_SO_rather_than_implying_coverage():
    """The walk is lazy by design, so `evaluated` below `considered` is normal
    and must not read as "the engine ignored 400 symbols"."""
    content = digest.build_digest(
        [],
        [],
        refusals=["AAPL  no recipe produced a plan"],
        refusal_summary={"plan": 12},
        evaluated=17,
        considered=429,
        as_of=DAY,
    )
    _, body = digest.render(content)
    assert "evaluated 17 of 429 scored symbol(s)" in body
    assert "the walk stops once the slots are full" in body
