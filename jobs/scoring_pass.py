"""The scoring pass as a pipeline job: score, log surfaced ideas, keep the cards.

Separate from `scoring/runner.py` because that module is the ALGORITHM and this
is the JOB: it owns the `job_runs` row, the D42 aggregate and the Shadow Book
writes, so the algorithm stays callable from a test or a backtest without
writing anything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from core import clock
from core.jobs import job_run
from decision.idea_builder import Idea
from features.sentiment import get_scorer, scorer_id
from scoring.runner import build_ideas, render_idea, score_universe, top_candidates
from shadow_book.logger import log_surfaced

log = logging.getLogger(__name__)

JOB_NAME = "scoring_pass"

__all__ = ["MAX_REFUSALS_SHOWN", "PassResult", "run"]

# How many refusals the digest NAMES. The rest are counted by stage: the
# engine's own numbers say 305 symbols produced no setup on a typical day, and
# a report that printed each one would bury the five that nearly made it.
MAX_REFUSALS_SHOWN = 5


@dataclass
class PassResult:
    """What the pass produced, for the digest to render.

    `cards` holds ONLY real idea cards; a surfaced symbol that produced no
    plan, or was refused by a gate, goes in `refusals`. The first version put
    both in `cards`, and the digest — which decides between its two shapes on
    whether `cards` is empty — then rendered a zero-plan day as an "IDEAS"
    digest containing five lines saying there were no ideas. Counting strings
    instead of ideas is how that happens.
    """

    cards: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    """The HIGHEST-SCORING few, named. Capped at `MAX_REFUSALS_SHOWN`."""
    refusal_summary: dict[str, int] = field(default_factory=dict)
    """How many candidates each stage declined, across the whole walk."""
    watch_only: list[str] = field(default_factory=list)
    """D38's excess, rendered. OUTSIDE the N and not Shadow Book calls, so they
    are neither `cards` nor `refusals` — a section of their own."""
    candidates: list[dict] = field(default_factory=list)
    ideas_logged: int = 0
    surfaced: int = 0


_LAST: PassResult = PassResult()


def latest() -> PassResult:
    """The most recent pass in THIS process, for the digest step to read.

    In-process rather than a database read because the digest runs seconds
    later in the same pipeline, and re-deriving 433 symbols' cards to render
    them would double the run for nothing.
    """
    return _LAST


def run(*, as_of: date | None = None) -> int:
    """Score the universe, log the surfaced ideas, return rows written."""
    global _LAST
    day = as_of or clock.session_date()

    with job_run(JOB_NAME, session_date=day, is_critical=False, triggered_by="manual") as job:
        universe_run = score_universe(as_of=day)
        candidates = top_candidates(universe_run)

        # D42: only surfaced ideas become Shadow Book calls. The rest are
        # already in `daily_scoring_stats`, written by score_universe.
        built = build_ideas(universe_run, as_of=day)
        ideas = [item for item in built if isinstance(item, Idea)]
        # THE WALK NOW REFUSES EVERY CANDIDATE IT PASSED (D42 amended), which
        # on 2026-10-21 was 309 of them. The digest gets the HIGHEST-SCORING
        # few by name and the rest as a count per stage: a reader needs to know
        # that 305 symbols produced no setup, not to read 305 lines saying so.
        assignment = universe_run.assignment
        refused = assignment.refused if assignment else []
        watching = [idea.card() for idea in (assignment.watch_only if assignment else [])]
        refusals = [
            render_idea(rejection)
            for _, rejection in sorted(refused, key=lambda pair: -(pair[0].composite or 0.0))[
                :MAX_REFUSALS_SHOWN
            ]
        ]
        refusal_summary: dict[str, int] = {}
        for _, rejection in refused:
            refusal_summary[rejection.stage] = refusal_summary.get(rejection.stage, 0) + 1
        logged = log_surfaced(ideas, sentiment_scorer=scorer_id(get_scorer()), as_of=day)

        _LAST = PassResult(
            cards=[idea.card() for idea in ideas],
            refusals=refusals,
            refusal_summary=refusal_summary,
            watch_only=watching,
            candidates=candidates,
            ideas_logged=logged,
            surfaced=len(universe_run.surfaced),
        )
        job.note(
            surfaced=len(universe_run.surfaced),
            ideas_logged=logged,
            tier_a=len(universe_run.tier_a),
            tier_b=len(universe_run.tier_b),
            refused=len(refusals),
        )
        job.add_rows(logged)
    return logged
