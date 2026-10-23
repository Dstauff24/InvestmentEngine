"""The daily post-close digest (D16), sent on every configured channel.

THREE SHAPES, and only two of them are about ideas. When ideas surfaced, the
digest is their cards in full. When none did, it is an explicit SELECTIVITY
report — the engine's first edge source (D1) is that it can say "nothing
today", and a digest that said so apologetically, or said nothing at all, would
train the reader to treat silence as a failure. So a zero-plan day reports what
the engine looked at, what the distribution looked like, and which candidates
came closest, and it reads like a decision because it is one.

**THE THIRD SHAPE EXISTS BECAUSE THE SECOND ONE LIED.** On 2026-10-22 the VM's
`scoring_pass` FAILED (SHADOW_ACCOUNT_VALUE was missing from its `.env`) and
the digest went out saying "NOTHING TODAY — that is the engine working, not
failing". Nothing had been evaluated at all. The two shapes were chosen on
whether `cards` was empty, and a step that dies produces an empty list exactly
like a quiet market does — so the most reassuring message in the system was
also the one that fired when the system was broken.

So the shape is now chosen on whether the steps that FEED the digest actually
succeeded, read from `job_runs` rather than inferred from the absence of
content. If scoring did not complete, the digest leads with SCORING INCOMPLETE,
names the step and its error, and uses no selectivity language anywhere —
`_selectivity_report` refuses to run at all in that state.

It also carries YESTERDAY'S NEWS: the outcome changes the tracker just wrote.
A digest that only ever shows new ideas would never tell the reader that the
idea it sent last week just stopped out, which is the half of the Shadow Book
that costs money.

Sent after the outcome tracker in the pipeline, for exactly that reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from alerts import notify
from core import clock, config, db

log = logging.getLogger(__name__)

JOB_NAME = "daily_digest"
MAX_CANDIDATES = 5

# Steps whose output IS the idea half of the digest. If any of them did not
# succeed today, no ideas were evaluated and the digest may not describe the
# evening as selective. `scoring_pass` covers idea building too: it scores the
# universe AND builds the cards, so a failure in either lands on this row.
IDEA_STEPS = ("scoring_pass",)
# Steps that supply yesterday's outcomes. A failure here degrades the digest
# without making the idea half dishonest, so it warns rather than leads.
OUTCOME_STEPS = ("outcome_tracker",)

__all__ = [
    "DigestContent",
    "build_digest",
    "incomplete_steps",
    "render",
    "run",
]


# The LATEST run per job today, so a successful retry recovers the day — the
# same rule `v_day_status` uses for `day_complete`.
STEP_STATUS = """
    SELECT DISTINCT ON (job_name) job_name, status, error, finished_at
    FROM   job_runs
    WHERE  session_date = %(day)s AND job_name = ANY(%(jobs)s)
    ORDER  BY job_name, started_at DESC
"""


STATS = """
    SELECT * FROM daily_scoring_stats WHERE session_date = %s
"""

CHANGES = """
    SELECT symbol, recipe, tier, outcome, r_multiple, return_pct, days_held,
           actual_entry, actual_exit, hit_target_1, hit_target_2
    FROM   shadow_book
    WHERE  last_evaluated_on = %s AND outcome <> 'open'
    ORDER  BY outcome, symbol
"""

OPEN_CALLS = """
    SELECT symbol, recipe, tier, called_on, proj_target_1, current_stop,
           hit_target_1, stop_to_breakeven_on
    FROM   shadow_book
    WHERE  outcome = 'open' AND origin = 'engine'
    ORDER  BY called_on, symbol
"""


def incomplete_steps(day: date, jobs: tuple[str, ...]) -> list[dict]:
    """Which of `jobs` did NOT succeed today. A step that never ran counts.

    Read from `job_runs` rather than inferred from empty content, because an
    empty list is exactly what both a quiet market and a dead step produce.
    """
    rows = {r["job_name"]: r for r in db.fetch_all(STEP_STATUS, {"day": day, "jobs": list(jobs)})}
    out = []
    for job in jobs:
        row = rows.get(job)
        if row is None:
            out.append({"job_name": job, "status": "never ran", "error": None})
        elif row["status"] != "success":
            out.append(row)
    return out


@dataclass
class DigestContent:
    """Everything the digest needs. Assembled, then rendered separately."""

    session_date: date
    cards: list[str]
    """ONLY real idea cards. A refusal is not an idea — see `refusals`."""
    refusals: list[str]
    candidates: list[dict]
    stats: dict | None
    closed: list[dict]
    open_calls: list[dict]
    blocked: list[dict] = field(default_factory=list)
    """Idea-feeding steps that did not succeed. Non-empty ⇒ the third shape."""
    degraded: list[dict] = field(default_factory=list)
    """Outcome steps that did not succeed: a warning, not a lead."""

    @property
    def dedupe_key(self) -> str:
        """The key this digest dedupes on — which INCLUDES its outcome.

        `daily_digest:{day}` alone blocked the correction. On 2026-10-22 the VM
        sent a SCORING INCOMPLETE digest, the operator fixed the setting and
        re-ran, and the corrected digest was suppressed as a duplicate of the
        broken one: the reader was left with the failure notice and no idea the
        day had recovered.

        So the outcome is part of the identity of the message. An incomplete
        digest and its corrected successor are different messages and both
        deliver; a clean re-run of an already-clean day is the SAME message and
        stays suppressed. The blocked step names are included too, because a
        different step failing later is a different problem, not a repeat.
        """
        if not self.blocked:
            return f"daily_digest:{self.session_date}:ok"
        failed = "+".join(sorted(b["job_name"] for b in self.blocked))
        return f"daily_digest:{self.session_date}:incomplete({failed})"

    @property
    def scoring_incomplete(self) -> bool:
        """Whether anything was evaluated tonight at all.

        Checked BEFORE `has_ideas` everywhere, because "nothing was evaluated"
        and "nothing qualified" are different facts and only one of them is
        the engine working.
        """
        return bool(self.blocked)

    @property
    def has_ideas(self) -> bool:
        return bool(self.cards) and not self.scoring_incomplete


def build_digest(
    cards: list[str],
    candidates: list[dict],
    *,
    refusals: list[str] | None = None,
    as_of: date | None = None,
) -> DigestContent:
    """Gather the day's material. `candidates` are the top-ranked scored rows.

    `cards` must contain ONLY real idea cards: the two digest shapes are chosen
    on whether it is empty, so a refusal in there renders a zero-plan day as an
    "IDEAS" digest whose ideas all say there were none.
    """
    day = as_of or clock.session_date()
    blocked = incomplete_steps(day, IDEA_STEPS)
    if blocked:
        # Drop whatever was handed in: after a failed pass the caller's lists
        # are the empty defaults of a result that was never produced, and
        # reporting them as "no ideas" is the lie this guards against.
        log.error(
            "digest: %d idea step(s) did not succeed on %s — rendering SCORING "
            "INCOMPLETE rather than a selectivity report: %s",
            len(blocked),
            day,
            ", ".join(f"{b['job_name']}={b['status']}" for b in blocked),
        )
    return DigestContent(
        session_date=day,
        cards=[] if blocked else cards,
        refusals=[] if blocked else (refusals or []),
        candidates=[] if blocked else candidates[:MAX_CANDIDATES],
        stats=db.fetch_one(STATS, (day,)),
        closed=db.fetch_all(CHANGES, (day,)),
        open_calls=db.fetch_all(OPEN_CALLS),
        blocked=blocked,
        degraded=incomplete_steps(day, OUTCOME_STEPS),
    )


def render(content: DigestContent) -> tuple[str, str]:
    """(subject, body). Plain text — it has to read well on a phone."""
    cfg = config.scoring()
    lines: list[str] = []

    if content.scoring_incomplete:
        # THE SHAPE THAT MUST EXIST. No selectivity language appears below, and
        # `_selectivity_report` refuses to run in this state at all.
        subject = f"SCORING INCOMPLETE — {content.session_date}"
        lines.append(
            f"SCORING INCOMPLETE: no ideas were evaluated tonight — {content.session_date}"
        )
        lines.append("")
        lines.append(
            # Deliberately free of the vocabulary a working evening uses. A
            # disclaimer containing the word would defeat the banned-phrase
            # test AND give a skimming reader the wrong word to latch onto.
            "This is NOT a quiet market and NOT a decision the engine reached. The "
            "step that scores the universe and builds ideas did not complete, so no "
            "symbol was judged tonight:"
        )
        for step in content.blocked:
            detail = (step.get("error") or "").strip().splitlines()
            first = detail[0][:180] if detail else "no error recorded"
            lines.append(f"  {step['job_name']}: {step['status']} — {first}")
        lines.append("")
        lines.append(
            "Nothing below says anything about today's opportunities. Fix the step "
            "and re-run `python -m jobs.daily_close --only scoring_pass daily_digest`; "
            "`job_runs` holds the full error."
        )
        lines.append("")
        if content.degraded:
            lines.append(
                "Outcome tracking also did not complete, so the sections below may be "
                "stale: " + ", ".join(f"{d['job_name']}={d['status']}" for d in content.degraded)
            )
            lines.append("")
    elif content.has_ideas:
        subject = f"{len(content.cards)} idea(s) — {content.session_date}"
        lines.append(f"IDEAS FOR {content.session_date}")
        lines.append("")
        for card in content.cards:
            lines.append(card)
            lines.append("")
    else:
        # NOT a fallback. D1's first edge source is selectivity, and this is
        # what exercising it looks like.
        subject = f"nothing today — {content.session_date}"
        lines.append(f"NOTHING TODAY — {content.session_date}")
        lines.append("")
        lines.append(
            "No idea cleared its gates. That is the engine working, not failing: "
            "selectivity is edge source #1 (D1), and a forced idea is worse than "
            "no idea."
        )
        lines.append("")
        lines.extend(_selectivity_report(content, cfg))
        if content.degraded:
            lines.append(
                "Note: outcome tracking did not complete ("
                + ", ".join(f"{d['job_name']}={d['status']}" for d in content.degraded)
                + "), so the sections below may be stale."
            )
            lines.append("")

    if content.closed:
        lines.append("─" * 40)
        lines.append("CLOSED SINCE YESTERDAY")
        for row in content.closed:
            lines.append("  " + _closed_line(row))
        lines.append("")

    if content.open_calls:
        lines.append("─" * 40)
        lines.append(f"STILL OPEN ({len(content.open_calls)})")
        for row in content.open_calls:
            flag = " [trimmed, stop at breakeven]" if row["stop_to_breakeven_on"] else ""
            lines.append(
                f"  {row['symbol']:6} {row['recipe']:20} Tier {row['tier']} "
                f"called {row['called_on']} stop {_num(row['current_stop'])}{flag}"
            )
        lines.append("")

    return subject, "\n".join(lines).rstrip() + "\n"


def _selectivity_report(content: DigestContent, cfg) -> list[str]:
    """What the engine looked at, and what came closest.

    REFUSES to run when scoring did not complete. Selectivity is a claim about
    a judgement that was made; on a night when nothing was judged it is a lie,
    and the 2026-10-22 VM digest made exactly that claim. Belt and braces
    alongside the branch in `render` — the text that reassures must be
    unreachable from the state that would make it false.
    """
    if content.scoring_incomplete:
        raise AssertionError(
            "selectivity report requested for a day whose scoring did not complete: "
            f"{[b['job_name'] for b in content.blocked]}"
        )
    lines: list[str] = []
    stats = content.stats
    if stats:
        lines.append(
            f"Scored {stats['symbols_scored']} symbols ({stats['symbols_abstained']} abstained)."
        )
        lines.append(
            f"  Tier A {stats['tier_a_count']}   Tier B {stats['tier_b_count']}   "
            f"surfaced {stats['surfaced_count']}   "
            f"aggregated {stats['not_surfaced_count']} (D42)"
        )
        lines.append(
            f"  composite: min {_num(stats['composite_min'])} / "
            f"median {_num(stats['composite_median'])} / "
            f"p90 {_num(stats['composite_p90'])} / max {_num(stats['composite_max'])}"
        )
        histogram = stats["mask_histogram"] or {}
        if histogram:
            shape = ", ".join(f"{k} pillar(s): {v}" for k, v in sorted(histogram.items()))
            lines.append(f"  evidence depth (D41) — {shape}")
            lines.append("  a composite built on two pillars is not the claim four would be")
        lines.append("")

    if content.refusals:
        # The most informative lines available on a zero-plan day: these
        # symbols ranked at the top and were then declined, and by what.
        lines.append("RANKED BUT NOT ACTIONABLE:")
        for line in content.refusals:
            lines.append(f"  {line}")
        lines.append("")

    if content.candidates:
        lines.append(f"CLOSEST {len(content.candidates)} (ranked, D33):")
        for item in content.candidates:
            pct = "" if item.get("percentile") is None else f" p{item['percentile']:.0f}"
            lines.append(
                f"  {item['symbol']:6} composite {item['composite']:5.1f}{pct}  "
                f"[{item.get('mask') or 'none'}]  {item.get('reason', '')}"
            )
        lines.append("")
        lines.append(
            f"Gates in force: Tier A composite >= {cfg.tiers.a.min_composite:g} with "
            f">= {cfg.tiers.a.min_pillars_agreeing} pillars agreeing and R:R >= "
            f"{cfg.tiers.a.min_risk_reward:g}; Tier B composite >= "
            f"{cfg.tiers.b.min_composite:g} and R:R >= {cfg.tiers.b.min_risk_reward:g}."
        )
        lines.append("")
    return lines


def _closed_line(row: dict) -> str:
    bits = [f"{row['symbol']:6} {row['recipe']:20} Tier {row['tier']}  {row['outcome'].upper()}"]
    if row["r_multiple"] is not None:
        bits.append(f"{float(row['r_multiple']):+.2f}R")
    if row["return_pct"] is not None:
        bits.append(f"{float(row['return_pct']):+.1f}%")
    if row["days_held"] is not None:
        bits.append(f"{row['days_held']}d")
    return "  ".join(bits)


def _num(value) -> str:
    return "—" if value is None else f"{float(value):.2f}"


def run(
    cards: list[str],
    candidates: list[dict],
    *,
    refusals: list[str] | None = None,
    as_of: date | None = None,
    send: bool = True,
) -> str:
    """Build, render and send the digest. Returns the rendered body.

    Non-critical in the pipeline: a digest that failed to send is a bad
    evening, while a digest that stopped the snapshot jobs would be a lost day
    of unbuyable history.
    """
    from core.jobs import job_run

    day = as_of or clock.session_date()
    content = build_digest(cards, candidates, refusals=refusals, as_of=day)
    subject, body = render(content)

    with job_run(JOB_NAME, session_date=day, is_critical=False, triggered_by="manual") as job:
        if send:
            delivery = notify.notify(
                subject,
                body,
                alert_type="daily_digest",
                # An incomplete digest is a WARNING: the reader has to know the
                # engine did not run, and "info" is what they skim past.
                severity="warning" if content.scoring_incomplete else "info",
                # Keyed on the OUTCOME as well as the day, so a corrected
                # digest supersedes a failure notice instead of being
                # suppressed as a duplicate of it.
                dedupe_key=content.dedupe_key,
            )
            job.note(
                channels_sent=list(delivery.sent),
                channels_suppressed=list(delivery.suppressed),
                channels_failed=list(delivery.failed),
                channels_skipped=list(delivery.skipped),
                dedupe_key=content.dedupe_key,
                ideas=len(content.cards),
                scoring_incomplete=content.scoring_incomplete,
                blocked_by=[b["job_name"] for b in content.blocked],
            )
            if not delivery.reached_someone:
                # The digest is non-critical, so this cannot fail the job — but
                # "nobody got tonight's digest" must not be a line only the log
                # knows about.
                log.error("digest for %s reached nobody: %s", day, delivery)
        else:
            job.note(
                ideas=len(content.cards),
                sent=False,
                scoring_incomplete=content.scoring_incomplete,
                blocked_by=[b["job_name"] for b in content.blocked],
            )
        job.add_rows(len(content.cards) or len(content.candidates))
    return body
