"""The Friday scorecard (D13/D40/D45). Fixtures only — no DB, no network.

The load-bearing cases are the two the daily digest had to learn the hard way:
an empty table must not read like a broken job, and a report must not describe
a week it cannot see.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from alerts import scorecard
from shadow_book.analytics import Cell

AS_OF = date(2026, 10, 23)  # a Friday
MONDAY = date(2026, 10, 19)
# 17:00 ET on the report day: after the 16:30 pipeline, so every session in the
# week counts as DUE. Pinned rather than taken from the wall clock, or these
# assertions would read differently depending on the hour the suite runs.
AFTER_THE_RUN = datetime(2026, 10, 23, 21, 0, tzinfo=UTC)
BEFORE_THE_RUN = datetime(2026, 10, 23, 16, 0, tzinfo=UTC)  # 12:00 ET


def open_row(**kw) -> dict:
    base = {
        "id": 1,
        "symbol": "AAPL",
        "recipe": "pullback_to_trend",
        "tier": "A",
        "strategy": "swing",
        "called_on": AS_OF - timedelta(days=9),
        "status_at_call": "shadow",
        "regime_at_call": "bull_calm",
        "proj_entry_low": 99.0,
        "proj_entry_high": 101.0,
        "proj_stop": 95.0,
        "proj_target_1": 110.0,
        "proj_target_2": 120.0,
        "proj_horizon_days": 15,
        "current_stop": 95.0,
        "stop_to_breakeven_on": None,
        "hit_target_1": None,
        "actual_entry": 100.0,
        "actual_entry_at": None,
        "last_evaluated_on": AS_OF,
    }
    base.update(kw)
    return base


def bar(close: float, day: date = AS_OF) -> dict:
    return {
        "symbol": "AAPL",
        "session_date": day,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
    }


def closed_row(**kw) -> dict:
    base = {
        "symbol": "MSFT",
        "recipe": "post_earnings_drift",
        "tier": "A",
        "outcome": "target_1",
        "r_multiple": 1.4,
        "days_held": 6,
        "called_on": MONDAY,
        "closed_on": AS_OF,
        "regime_at_call": "bull_calm",
        "status_at_call": "shadow",
    }
    base.update(kw)
    return base


def stats_row(**kw) -> dict:
    base = {
        "session_date": MONDAY,
        "symbols_scored": 430,
        "symbols_abstained": 3,
        "tier_a_count": 12,
        "tier_b_count": 60,
        "surfaced_count": 0,
        "not_surfaced_count": 72,
        "mask_histogram": {"2": 200, "3": 180, "4": 50},
        "composite_median": 48.0,
        "composite_p90": 70.0,
        "composite_max": 88.5,
        "tier_a_min_composite": 70.0,
        "tier_b_min_composite": 55.0,
    }
    base.update(kw)
    return base


def card(**kw) -> scorecard.Scorecard:
    base = {"as_of": AS_OF, "week_start": MONDAY, "now": AFTER_THE_RUN}
    base.update(kw)
    return scorecard.build(**base)


def positions(rows, bars, splits=None):
    return scorecard.open_positions(rows, bars, splits or {}, as_of=AS_OF)


# ── the week window ─────────────────────────────────────────────────────────


def test_the_week_starts_on_monday():
    """A Friday report reaching back seven CALENDAR days would count the
    previous Friday's closures in two consecutive reports."""
    assert scorecard.week_start_for(AS_OF) == MONDAY
    assert scorecard.week_start_for(MONDAY) == MONDAY


def test_coverage_is_a_count_not_a_verdict():
    """A day with no `job_runs` row might be a failure or might predate the
    deployment, so the week's coverage is REPORTED rather than judged — a
    banner that cried wolf every week would train the reader to skip it."""
    quiet = card(stats=[stats_row()])
    assert quiet.weekday_sessions == 5
    assert quiet.sessions_scored == 1
    assert quiet.week_partial is False
    _, body = scorecard.render(quiet)
    assert "scoring ran on 1 of 5 weekday session(s)" in body
    assert "WEEK IS PARTIAL" not in body


def test_a_step_that_failed_today_leads_the_report():
    """The 2026-10-22 lesson: a report assembled from an empty table cannot
    tell a quiet market from a job that died, so it must not try."""
    broken = card(incomplete=[{"job_name": "scoring_pass", "status": "error", "error": "boom"}])
    assert broken.week_partial is True
    _, body = scorecard.render(broken)
    assert "WEEK IS PARTIAL" in body
    assert "scoring_pass" in body


# ── the empty book ──────────────────────────────────────────────────────────


def test_an_empty_book_says_so_and_shows_what_the_week_scored():
    empty = card(stats=[stats_row()])
    assert empty.book_is_empty
    _, body = scorecard.render(empty)
    assert "THE SHADOW BOOK IS EMPTY" in body
    assert "D40 keeps every one in `shadow`" in body
    assert "SCORING, PER SESSION" in body
    assert "88.5" in body, "the week's best composite"
    assert "2p: 47%" in body, "D41 pillar depth: 200 of 430 scored two pillars"


def test_an_empty_book_with_no_stats_is_reported_as_a_fault():
    """Nothing scored on any day of the week is not selectivity."""
    _, body = scorecard.render(card())
    assert "NO SCORING STATS THIS WEEK EITHER" in body
    assert "that is a fault, not a quiet market" in body


def test_the_near_misses_cannot_be_named_and_the_report_says_why():
    """D42 rolls every qualifying-but-unsurfaced idea into one aggregate row
    with no symbols in it. Naming them would mean deciding to persist them,
    which nobody has decided — so the report states the limit rather than
    quietly showing an empty list."""
    _, body = scorecard.render(card(stats=[stats_row()]))
    assert "D42 keeps" in body
    assert "NO SYMBOL" in body


def test_ideas_that_did_surface_are_listed_by_composite():
    ideas = [
        {
            "as_of_date": MONDAY,
            "symbol": "NVDA",
            "recipe": "post_earnings_drift",
            "tier": "A",
            "composite_score": 81.0,
            "pillars_agreeing": 3,
        }
    ]
    _, body = scorecard.render(card(stats=[stats_row()], top_ideas=ideas))
    assert "IDEAS THAT SURFACED THIS WEEK" in body
    assert "NVDA" in body
    assert "D42 keeps" not in body, "there is something to name, so the caveat is not needed"


# ── open positions ──────────────────────────────────────────────────────────


def test_an_open_call_reports_days_held_and_every_distance():
    [item] = positions([open_row()], {"AAPL": bar(104.0)})
    assert item.days_held == 9
    assert item.unrealized_r == pytest.approx((104.0 - 100.0) / 5.0)
    assert item.pct_to(item.stop) == pytest.approx((95.0 - 104.0) / 104.0 * 100)
    assert item.pct_to(item.target_1) == pytest.approx((110.0 - 104.0) / 104.0 * 100)
    assert item.pct_to(item.target_2) == pytest.approx((120.0 - 104.0) / 104.0 * 100)

    _, body = scorecard.render(card(open_positions=[item]))
    assert "day 9" in body
    assert "+0.80R" in body
    assert "stop 95.00 (-8.7%)" in body
    assert "TP1  110.00 (+5.8%)" in body


def test_the_distance_is_to_the_ENFORCED_stop_not_the_projection():
    """`proj_stop` is frozen at the call (D13) while the stop the tracker
    enforces moves to breakeven at TP1 and down by a dividend (D19(2)). The
    reader needs the one that will actually take them out."""
    [item] = positions(
        [open_row(current_stop=100.0, stop_to_breakeven_on=AS_OF - timedelta(days=2))],
        {"AAPL": bar(104.0)},
    )
    assert item.stop == pytest.approx(100.0)
    _, body = scorecard.render(card(open_positions=[item]))
    assert "(at breakeven)" in body


def test_an_unfilled_plan_says_so_rather_than_showing_a_fake_r():
    """An unfilled plan is not a position and has no R — counting it either way
    would be a claim the data cannot support (D13)."""
    [item] = positions([open_row(actual_entry=None)], {"AAPL": bar(97.0)})
    assert item.awaiting_entry is True
    assert item.unrealized_r is None
    _, body = scorecard.render(card(open_positions=[item]))
    assert "UNFILLED, awaiting the entry zone" in body


def test_a_position_with_no_price_says_that_instead_of_guessing():
    [item] = positions([open_row()], {})
    assert item.price is None
    _, body = scorecard.render(card(open_positions=[item]))
    assert "no price for it" in body


def test_a_split_since_the_call_is_rebased_into_the_calls_units():
    """The levels are frozen at the call and `prices_daily` is raw (D24), so a
    split leaves the two in different units. Reuses the tracker's
    `in_call_basis`, so the scorecard and the tracker cannot disagree about
    what a position is worth."""
    splits = {"AAPL": [{"ex_date": AS_OF - timedelta(days=3), "split_ratio": 10.0}]}
    [item] = positions([open_row()], {"AAPL": bar(10.6)}, splits)
    assert item.rebased is True
    assert item.price == pytest.approx(106.0)
    assert item.unrealized_r == pytest.approx((106.0 - 100.0) / 5.0)

    _, body = scorecard.render(card(open_positions=[item]))
    assert "priced in the call's units" in body


def test_a_split_before_the_call_is_not_applied():
    """Only splits AFTER the call change the units the levels were written in."""
    splits = {"AAPL": [{"ex_date": AS_OF - timedelta(days=30), "split_ratio": 10.0}]}
    [item] = positions([open_row()], {"AAPL": bar(104.0)}, splits)
    assert item.rebased is False
    assert item.price == pytest.approx(104.0)


# ── closures ────────────────────────────────────────────────────────────────


def test_closures_report_r_and_sum_the_week():
    full = card(closed=[closed_row(), closed_row(symbol="KO", outcome="stopped", r_multiple=-1.0)])
    _, body = scorecard.render(full)
    assert "MSFT   target_1        +1.40R" in body
    assert "KO     stopped         -1.00R" in body
    assert "week: +0.40R over 2 closed (mean +0.20R)" in body
    assert "ONE WEEK IS NOT EVIDENCE" in body


def test_an_unfilled_expiry_is_listed_without_an_r():
    """`never_triggered` at 0.0R would drag every average toward nothing."""
    _, body = scorecard.render(
        card(closed=[closed_row(outcome="never_triggered", r_multiple=None, days_held=None)])
    )
    assert "expired unfilled" in body
    assert "not a loss (D13)" in body
    assert "week:" not in body, "nothing closed with an R, so there is no weekly total"


# ── expectancy ──────────────────────────────────────────────────────────────


def _cell(closed: int, *, r: float = 0.3) -> Cell:
    cell = Cell(recipe="post_earnings_drift", regime="bull_calm", status="shadow")
    for _ in range(closed):
        cell.closed += 1
        cell.r_multiples.append(r)
        cell.wins += 1
    return cell


def test_a_thin_cell_is_printed_and_flagged_not_hidden():
    """D45 applies D39(2)'s floor PER CELL, so almost everything reads n<30 for
    months. Printed anyway, flagged: hiding a thin cell would hide the fact
    that nothing has been proved."""
    _, body = scorecard.render(card(cells=[_cell(4)]))
    assert "post_earnings_drift / bull_calm / shadow" in body
    assert "n<30" in body
    assert "NOT EVIDENCE" in body


def test_a_cell_over_the_floor_is_not_flagged():
    _, body = scorecard.render(card(cells=[_cell(31)]))
    assert "n<30" not in body
    assert "+0.30R" in body


def test_cells_with_nothing_closed_say_so_rather_than_printing_zeros():
    empty = Cell(recipe="base_breakout", regime="bull_calm", status="shadow")
    empty.open_now = 2
    _, body = scorecard.render(card(open_positions=[], cells=[empty], closed=[closed_row()]))
    assert "no closed ideas in any cell yet" in body
    assert "D40 working, not a gap" in body


# ── delivery ────────────────────────────────────────────────────────────────


def test_the_dedupe_key_is_its_own_namespace_and_carries_the_week():
    assert card().dedupe_key == "weekly_scorecard:2026-10-23:ok"
    assert not card().dedupe_key.startswith("daily_digest")


def test_a_corrected_scorecard_supersedes_a_partial_one():
    """The same lesson as the digest's key: a corrected report must not be
    suppressed as a duplicate of the broken one it replaces."""
    broken = card(incomplete=[{"job_name": "scoring_pass", "status": "error", "error": "x"}])
    assert broken.dedupe_key != card().dedupe_key
    assert broken.dedupe_key.endswith(":partial")


def test_the_subject_names_the_week():
    subject, _ = scorecard.render(card())
    assert subject == "Weekly scorecard — Oct 19 - Oct 23"


def test_the_report_never_claims_a_shadow_call_is_actionable():
    """D40: a `shadow` recipe is surfaced and tracked, and FLAGGED NOT
    ACTIONABLE. A weekly report of open positions is exactly where that could
    quietly stop being said."""
    _, body = scorecard.render(card(open_positions=positions([open_row()], {"AAPL": bar(104.0)})))
    assert "NOT actionable" in body


# ── the cron entry, as documented ───────────────────────────────────────────


def _scorecard_cron_line() -> str:
    from pathlib import Path

    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    lines = [ln for ln in readme.splitlines() if "jobs.weekly_scorecard" in ln and ln[:1].isdigit()]
    assert len(lines) == 1, f"expected exactly one crontab line, found {lines}"
    return lines[0]


def test_the_documented_cron_entry_runs_on_friday_after_the_daily_run():
    line = _scorecard_cron_line()
    minute, hour, dom, month, dow = line.split()[:5]
    assert (dom, month, dow) == ("*", "*", "5"), "Friday only"
    assert int(hour) > 16 or (int(hour) == 16 and int(minute) > 30), (
        "the 16:30 pipeline must have finished writing the day first"
    )


def test_the_documented_cron_entry_grants_no_network_consent():
    """D27: consent is per-invocation and this job reaches no provider. The
    flag appearing here would mean someone misunderstood what it does — the
    same class of mistake `deploy.sh`'s test guards against."""
    assert "ALLOW_LIVE_CALLS" not in _scorecard_cron_line()


# ── a session that is not due yet is not a session that failed ──────────────


def test_a_run_that_is_not_due_yet_reads_as_not_due_not_as_incomplete():
    """Rendered at lunchtime — the dry run an operator does before trusting the
    cron — nothing was supposed to have happened yet, and a report that called
    that a failure would be crying wolf on its own schedule."""
    early = card(now=BEFORE_THE_RUN, stats=[stats_row()])
    assert early.today_is_due is False
    assert early.week_partial is False
    _, body = scorecard.render(early)
    assert "TODAY'S RUN IS NOT DUE YET (16:30 ET)" in body
    assert "Nothing here is a failure" in body
    assert "WEEK IS PARTIAL" not in body


def test_a_session_not_due_is_left_out_of_the_coverage_count():
    """Otherwise "scoring ran on 4 of 5" is the permanent reading of a healthy
    Friday lunchtime."""
    assert card(now=BEFORE_THE_RUN).weekday_sessions == 4
    assert card(now=AFTER_THE_RUN).weekday_sessions == 5


def test_a_step_that_failed_is_still_reported_before_the_run_is_due():
    """The exemption is for ABSENCE, not for errors: a step that failed at
    15:00 failed, whatever the schedule says."""
    early = card(
        now=BEFORE_THE_RUN,
        incomplete=[{"job_name": "outcome_tracker", "status": "failed", "error": "boom"}],
    )
    assert early.week_partial is True
    _, body = scorecard.render(early)
    assert "WEEK IS PARTIAL" in body
    assert "TODAY'S RUN IS NOT DUE YET" not in body, "a failure outranks the schedule"
