"""The daily post-close digest (D16), sent on every configured channel.

TWO SHAPES, and the second is not a fallback. When ideas surfaced, the digest
is their cards in full. When none did, it is an explicit SELECTIVITY report —
the engine's first edge source (D1) is that it can say "nothing today", and a
digest that said so apologetically, or said nothing at all, would train the
reader to treat silence as a failure. So a zero-plan day reports what the
engine looked at, what the distribution looked like, and which candidates came
closest, and it reads like a decision because it is one.

It also carries YESTERDAY'S NEWS: the outcome changes the tracker just wrote.
A digest that only ever shows new ideas would never tell the reader that the
idea it sent last week just stopped out, which is the half of the Shadow Book
that costs money.

Sent after the outcome tracker in the pipeline, for exactly that reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from alerts import notify
from core import clock, config, db

log = logging.getLogger(__name__)

JOB_NAME = "daily_digest"
MAX_CANDIDATES = 5

__all__ = ["DigestContent", "build_digest", "render", "run"]


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

    @property
    def has_ideas(self) -> bool:
        return bool(self.cards)


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
    return DigestContent(
        session_date=day,
        cards=cards,
        refusals=refusals or [],
        candidates=candidates[:MAX_CANDIDATES],
        stats=db.fetch_one(STATS, (day,)),
        closed=db.fetch_all(CHANGES, (day,)),
        open_calls=db.fetch_all(OPEN_CALLS),
    )


def render(content: DigestContent) -> tuple[str, str]:
    """(subject, body). Plain text — it has to read well on a phone."""
    cfg = config.scoring()
    lines: list[str] = []

    if content.has_ideas:
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
    """What the engine looked at, and what came closest."""
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
                severity="info",
                dedupe_key=f"daily_digest:{day}",
            )
            job.note(
                channels_sent=list(delivery.sent),
                channels_failed=list(delivery.failed),
                ideas=len(content.cards),
            )
        else:
            job.note(ideas=len(content.cards), sent=False)
        job.add_rows(len(content.cards) or len(content.candidates))
    return body
