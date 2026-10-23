"""The Friday scorecard (D13/D40/D45): what the book is holding, and what it has shown.

    python -m jobs.weekly_scorecard --dry-run     # render, send nothing
    python -m jobs.weekly_scorecard               # render and send

The daily digest answers "what should I look at tonight". This answers a
different question, once a week: **what is the Shadow Book actually holding,
what did it close, and what has any of it proved yet.** They are separated
because a reader skimming for tonight's ideas does not re-derive a strategy's
expectancy from seven digests, and because the weekly answer is the one D40's
ladder is decided from.

Three sections, and the third is the point:

  * **OPEN CALLS** — days held, where price sits relative to the enforced stop
    and both targets. Distances are from `current_stop`, NOT `proj_stop`: the
    projection is frozen (D13) while the stop the tracker enforces moves to
    breakeven at TP1 and down by a dividend (D19(2)), and a reader needs the
    one that will actually take them out.
  * **CLOSED THIS WEEK** — outcomes in R. `never_triggered` is listed and
    carries no R, because an unfilled plan is not a loss (D13) and showing it
    as 0.0R would drag every average toward nothing.
  * **EXPECTANCY PER RECIPE x REGIME** — straight from
    `shadow_book/analytics.py`, with D39(2)'s 30-closed floor marked on every
    cell. Under D45 that floor applies PER CELL, so almost everything here
    will read `n<30` for months. That is the honest state of a young engine and
    the number is printed anyway, flagged, because hiding a thin cell would
    hide the fact that nothing has been proved.

**ON AN EMPTY BOOK IT SAYS SO.** A scorecard that printed three empty tables
would read like a broken job. It states that the book is empty, then shows what
the WEEK's scoring actually did from `daily_scoring_stats` — tier counts,
pillar depth (D41's mask), and how close the best composite came. **It cannot
name the near-misses**: D42 rolls every qualifying-but-unsurfaced idea into one
aggregate row per day with no symbols in it, deliberately, so "top candidates"
is a number here and not a list of names unless something actually surfaced.
Inventing names would mean persisting near-misses, which is a decision nobody
has taken.

**IT ALSO CHECKS THE WEEK RAN.** The 2026-10-22 lesson (see `alerts/digest.py`):
a report assembled from an empty table cannot tell a quiet market from a job
that died. If the daily pipeline did not complete on a day inside the window,
the scorecard leads with that and says the week is partial.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from alerts import digest, notify
from core import clock, db
from shadow_book import analytics
from shadow_book.outcome_tracker import in_call_basis

log = logging.getLogger(__name__)

JOB_NAME = "weekly_scorecard"
MAX_IDEAS_LISTED = 5

__all__ = [
    "JOB_NAME",
    "Scorecard",
    "build",
    "collect",
    "open_positions",
    "render",
    "run",
    "week_start_for",
]


def week_start_for(day: date) -> date:
    """The Monday of `day`'s week. Monday because a trading week is Mon-Fri and
    a Friday report that reached back seven calendar days would include the
    previous Friday's closures twice across two reports."""
    return day - timedelta(days=day.weekday())


OPEN_SQL = """
    SELECT id, symbol, recipe, tier, strategy, called_on, status_at_call,
           regime_at_call, proj_entry_low, proj_entry_high, proj_stop,
           proj_target_1, proj_target_2, proj_horizon_days,
           current_stop, stop_to_breakeven_on, hit_target_1,
           actual_entry, actual_entry_at, last_evaluated_on
    FROM   shadow_book
    WHERE  outcome = 'open'
    ORDER  BY called_on, symbol
"""

CLOSED_SQL = """
    SELECT symbol, recipe, tier, outcome, r_multiple, days_held, called_on,
           closed_at::date AS closed_on, regime_at_call, status_at_call
    FROM   shadow_book
    WHERE  outcome <> 'open'
      AND  closed_at::date >= %(since)s
      AND  closed_at::date <= %(as_of)s
    ORDER  BY closed_at, symbol
"""

LATEST_BAR_SQL = """
    SELECT DISTINCT ON (symbol) symbol, session_date, open, high, low, close
    FROM   prices_daily
    WHERE  symbol = ANY(%(symbols)s) AND session_date <= %(as_of)s
    ORDER  BY symbol, session_date DESC
"""

SPLITS_SQL = """
    SELECT symbol, ex_date, split_ratio
    FROM   corporate_actions
    WHERE  symbol = ANY(%(symbols)s) AND action_type = 'split'
      AND  ex_date > %(since)s AND ex_date <= %(as_of)s
"""

STATS_SQL = """
    SELECT session_date, symbols_scored, symbols_abstained, tier_a_count,
           tier_b_count, surfaced_count, not_surfaced_count, mask_histogram,
           composite_median, composite_p90, composite_max,
           tier_a_min_composite, tier_b_min_composite
    FROM   daily_scoring_stats
    WHERE  session_date >= %(since)s AND session_date <= %(as_of)s
    ORDER  BY session_date
"""

IDEAS_SQL = """
    SELECT as_of_date, symbol, recipe, tier, composite_score, pillars_agreeing
    FROM   ideas
    WHERE  as_of_date >= %(since)s AND as_of_date <= %(as_of)s
    ORDER  BY composite_score DESC, as_of_date DESC
    LIMIT  %(limit)s
"""


@dataclass(frozen=True)
class OpenPosition:
    """One open call, priced. Every distance is a percentage of the LAST CLOSE,
    which is the number a reader can check against their own screen."""

    symbol: str
    recipe: str | None
    tier: str | None
    called_on: date
    days_held: int
    price: float | None
    priced_on: date | None
    entry: float | None
    stop: float | None
    target_1: float | None
    target_2: float | None
    unrealized_r: float | None
    stop_at_breakeven: bool
    awaiting_entry: bool
    rebased: bool
    """True when a split since the call moved the bars into other units."""

    def pct_to(self, level: float | None) -> float | None:
        if self.price in (None, 0) or level is None:
            return None
        return (level - self.price) / self.price * 100.0


@dataclass
class Scorecard:
    as_of: date
    week_start: date
    open_positions: list[OpenPosition] = field(default_factory=list)
    closed: list[dict] = field(default_factory=list)
    cells: list[analytics.Cell] = field(default_factory=list)
    stats: list[dict] = field(default_factory=list)
    top_ideas: list[dict] = field(default_factory=list)
    incomplete: list[dict] = field(default_factory=list)

    @property
    def book_is_empty(self) -> bool:
        return not self.open_positions and not self.closed and not self.cells

    @property
    def week_partial(self) -> bool:
        """Today's steps, not the whole week's.

        Deliberately NOT "any day in the window with no record": a day before
        the engine was deployed has no `job_runs` row either, so that rule
        would mark every week partial for ever and teach the reader to skip the
        banner. Coverage across the week is reported as a COUNT instead (see
        `sessions_scored` / `weekday_sessions`), which states the gap without
        claiming a failure.
        """
        return bool(self.incomplete)

    @property
    def weekday_sessions(self) -> int:
        """Weekdays from Monday through the report day. Not a market calendar:
        a holiday shows up as a session with no stats row, which is visible
        and harmless — the alternative is a holiday table nobody maintains."""
        days = (self.as_of - self.week_start).days + 1
        return sum(1 for i in range(days) if (self.week_start + timedelta(days=i)).weekday() < 5)

    @property
    def sessions_scored(self) -> int:
        """Sessions with a `daily_scoring_stats` row — which the scoring pass
        writes, so its presence is the evidence that the pass completed."""
        return len(self.stats)

    @property
    def dedupe_key(self) -> str:
        """Its OWN namespace, and keyed on the week plus whether the week ran.

        Separate from `daily_digest:` so a scorecard can never be suppressed as
        a duplicate of the evening's digest, and the partial flag is in the key
        for the reason D16's amendment put the outcome in the digest's: a
        corrected scorecard must be able to supersede one sent while the week
        was still broken.
        """
        state = "partial" if self.week_partial else "ok"
        return f"weekly_scorecard:{self.as_of}:{state}"


def open_positions(
    rows: list[dict], bars: dict[str, dict], splits: dict[str, list[dict]], *, as_of: date
) -> list[OpenPosition]:
    """Price every open call IN THE UNITS IT WAS WRITTEN IN.

    The levels are frozen at the call and `prices_daily` is raw (D24), so a
    split since the call leaves the two in different units — the same defect
    that would have booked a 90% loss in the tracker. `in_call_basis` is reused
    rather than reimplemented so the scorecard and the tracker cannot disagree
    about what a position is worth.
    """
    out: list[OpenPosition] = []
    for row in rows:
        symbol = row["symbol"]
        bar = bars.get(symbol)
        their_splits = splits.get(symbol, [])
        rebased = False
        if bar and their_splits:
            relevant = [s for s in their_splits if s["ex_date"] > row["called_on"]]
            if relevant:
                bars_out, _ = in_call_basis([dict(bar)], [], relevant)
                bar = bars_out[0]
                rebased = True

        price = float(bar["close"]) if bar and bar["close"] is not None else None
        entry = _f(row["actual_entry"])
        stop = _f(row["current_stop"]) or _f(row["proj_stop"])
        unrealized = None
        if price is not None and entry is not None and stop is not None and entry != stop:
            # R is measured against the risk the trade was TAKEN with, which is
            # the original entry-to-stop distance, not today's.
            unrealized = (price - entry) / abs(entry - stop)
        out.append(
            OpenPosition(
                symbol=symbol,
                recipe=row["recipe"],
                tier=row["tier"],
                called_on=row["called_on"],
                days_held=(as_of - row["called_on"]).days,
                price=price,
                priced_on=bar["session_date"] if bar else None,
                entry=entry,
                stop=stop,
                target_1=_f(row["proj_target_1"]),
                target_2=_f(row["proj_target_2"]),
                unrealized_r=unrealized,
                stop_at_breakeven=row["stop_to_breakeven_on"] is not None,
                awaiting_entry=entry is None,
                rebased=rebased,
            )
        )
    return out


def collect(*, as_of: date | None = None) -> Scorecard:
    """Read everything the scorecard needs. The only part that touches the DB."""
    day = as_of or clock.session_date()
    since = week_start_for(day)

    open_rows = db.fetch_all(OPEN_SQL)
    symbols = sorted({r["symbol"] for r in open_rows})
    bars: dict[str, dict] = {}
    splits: dict[str, list[dict]] = {}
    if symbols:
        bars = {
            r["symbol"]: r for r in db.fetch_all(LATEST_BAR_SQL, {"symbols": symbols, "as_of": day})
        }
        oldest = min(r["called_on"] for r in open_rows)
        for row in db.fetch_all(SPLITS_SQL, {"symbols": symbols, "since": oldest, "as_of": day}):
            splits.setdefault(row["symbol"], []).append(row)

    incomplete = digest.incomplete_steps(day, digest.IDEA_STEPS + digest.OUTCOME_STEPS)
    return Scorecard(
        as_of=day,
        week_start=since,
        open_positions=open_positions(open_rows, bars, splits, as_of=day),
        closed=db.fetch_all(CLOSED_SQL, {"since": since, "as_of": day}),
        cells=analytics.cells(as_of=day),
        stats=db.fetch_all(STATS_SQL, {"since": since, "as_of": day}),
        top_ideas=db.fetch_all(
            IDEAS_SQL, {"since": since, "as_of": day, "limit": MAX_IDEAS_LISTED}
        ),
        incomplete=incomplete,
    )


def build(**kwargs) -> Scorecard:
    """A Scorecard from parts, for tests and for a caller that already read."""
    return Scorecard(**kwargs)


def render(card: Scorecard) -> tuple[str, str]:
    """(subject, body). Plain text: Telegram is the page channel (D16)."""
    week = f"{card.week_start:%b %d} - {card.as_of:%b %d}"
    subject = f"Weekly scorecard — {week}"
    lines = [f"WEEKLY SCORECARD   {week}", "=" * 46, ""]

    if card.week_partial:
        names = ", ".join(sorted({b["job_name"] for b in card.incomplete}))
        lines += [
            "WEEK IS PARTIAL: these steps did not complete today —",
            f"  {names}",
            "so what follows describes less than the week did.",
            "",
        ]

    lines += [
        f"scoring ran on {card.sessions_scored} of {card.weekday_sessions} weekday "
        f"session(s) this week",
        "",
    ]

    if card.book_is_empty:
        lines += _empty_book(card)
    else:
        lines += _open_section(card)
        lines += _closed_section(card)
        lines += _expectancy_section(card)

    lines += [
        "",
        "Shadow Book: tracked, NOT actionable unless the recipe's ladder",
        "state says otherwise (D40).",
    ]
    return subject, "\n".join(lines)


def _empty_book(card: Scorecard) -> list[str]:
    lines = [
        "THE SHADOW BOOK IS EMPTY. No open calls, nothing closed this week,",
        "and no closed idea anywhere to measure — so there is no expectancy",
        "to report for any recipe, and D40 keeps every one in `shadow`.",
        "",
        "That is a statement about selectivity, not about the engine being",
        "broken: what the week actually scored is below.",
        "",
    ]
    if not card.stats:
        lines += [
            "NO SCORING STATS THIS WEEK EITHER. `daily_scoring_stats` has no row",
            "for any session in the window, which means the scoring pass did not",
            "complete on a single day — that is a fault, not a quiet market.",
        ]
        return lines

    lines += ["SCORING, PER SESSION (D42's aggregate)", "-" * 46]
    lines.append(f"{'date':<12}{'scored':>7}{'A':>4}{'B':>4}{'surf':>6}{'best':>7}")
    for row in card.stats:
        lines.append(
            f"{row['session_date']:%Y-%m-%d}  {row['symbols_scored']:>6}"
            f"{row['tier_a_count']:>4}{row['tier_b_count']:>4}"
            f"{row['surfaced_count']:>6}{_num(row['composite_max']):>7}"
        )
    lines.append("")

    depth = _mask_totals(card.stats)
    if depth:
        total = sum(depth.values()) or 1
        shape = "  ".join(
            f"{pillars}p: {count / total:.0%}" for pillars, count in sorted(depth.items())
        )
        lines += [
            "PILLAR DEPTH (D41 — how much evidence the scores stood on)",
            f"  {shape}",
            "",
        ]

    gates = card.stats[-1]
    lines += [
        f"GATES IN FORCE: Tier A >= {_num(gates['tier_a_min_composite'])}, "
        f"Tier B >= {_num(gates['tier_b_min_composite'])}",
        "",
    ]

    if card.top_ideas:
        lines += ["IDEAS THAT SURFACED THIS WEEK", "-" * 46]
        for idea in card.top_ideas:
            lines.append(
                f"  {idea['symbol']:<6} {idea['recipe']:<20} tier {idea['tier']} "
                f"{_num(idea['composite_score'])} ({idea['pillars_agreeing']} agreeing)"
            )
    else:
        best = max((r["composite_max"] or 0) for r in card.stats)
        lines += [
            f"CLOSEST THE WEEK CAME: a composite of {_num(best)}, and D42 keeps",
            "NO SYMBOL for an idea that qualified but did not surface — the",
            "aggregate is a count and a distribution by design. Naming the",
            "near-misses would mean deciding to persist them.",
        ]
    return lines


def _open_section(card: Scorecard) -> list[str]:
    lines = [f"OPEN CALLS ({len(card.open_positions)})", "-" * 46]
    if not card.open_positions:
        lines += ["  none open.", ""]
        return lines
    for item in card.open_positions:
        head = f"  {item.symbol:<6} {item.recipe or '-':<20} day {item.days_held}"
        if item.tier:
            head += f"  tier {item.tier}"
        lines.append(head)
        if item.price is None:
            lines.append("    no price for it — cannot measure distance to anything")
            continue
        parts = [f"last {item.price:.2f}"]
        if item.unrealized_r is not None:
            parts.append(f"{item.unrealized_r:+.2f}R")
        if item.awaiting_entry:
            parts.append("UNFILLED, awaiting the entry zone")
        lines.append("    " + "  ".join(parts))
        lines.append(
            f"    stop {_level(item.stop, item.pct_to(item.stop))}"
            f"{'  (at breakeven)' if item.stop_at_breakeven else ''}"
        )
        lines.append(
            f"    TP1  {_level(item.target_1, item.pct_to(item.target_1))}"
            f"    TP2 {_level(item.target_2, item.pct_to(item.target_2))}"
        )
        if item.rebased:
            lines.append("    (a split since the call; priced in the call's units)")
    lines.append("")
    return lines


def _closed_section(card: Scorecard) -> list[str]:
    resolved = [r for r in card.closed if r["outcome"] != "never_triggered"]
    expired = [r for r in card.closed if r["outcome"] == "never_triggered"]
    lines = [f"CLOSED THIS WEEK ({len(resolved)})", "-" * 46]
    if not resolved:
        lines.append("  nothing closed.")
    for row in resolved:
        lines.append(
            f"  {row['symbol']:<6} {row['outcome']:<14} {_r(row['r_multiple']):>7}"
            f"   {row['days_held'] if row['days_held'] is not None else '-'} day(s)"
            f"   {row['recipe'] or '-'}"
        )
    if resolved:
        values = [float(r["r_multiple"]) for r in resolved if r["r_multiple"] is not None]
        if values:
            total = sum(values)
            lines.append("")
            lines.append(
                f"  week: {total:+.2f}R over {len(values)} closed "
                f"(mean {total / len(values):+.2f}R)"
            )
            lines.append("  ONE WEEK IS NOT EVIDENCE: D39(2) wants 30 closed per cell.")
            lines.append("  This is the week, not the verdict.")
    if expired:
        lines.append("")
        lines.append(f"  {len(expired)} plan(s) expired unfilled — no R, because an unfilled")
        lines.append("  plan is not a loss (D13).")
    lines.append("")
    return lines


def _expectancy_section(card: Scorecard) -> list[str]:
    lines = ["EXPECTANCY PER RECIPE x REGIME (D40/D45)", "-" * 46]
    measurable = [c for c in card.cells if c.closed]
    if not measurable:
        lines += [
            "  no closed ideas in any cell yet, so nothing is measured. Every",
            "  recipe stays in `shadow` — which is D40 working, not a gap.",
            "",
        ]
        return lines
    for cell in sorted(measurable, key=lambda c: (-c.closed, c.label)):
        flag = "" if cell.sufficient else f"  n<{analytics.MIN_SAMPLE}"
        lines.append(f"  {cell.recipe} / {cell.regime} / {cell.status}")
        lines.append(
            f"    {_r(cell.expectancy_r):>7}  {cell.closed} closed"
            f"  hit {_pct(cell.hit_rate)}  PF {_num(cell.profit_factor)}{flag}"
        )
    thin = [c for c in measurable if not c.sufficient]
    if thin:
        lines.append("")
        lines.append(
            f"  {len(thin)} of {len(measurable)} cell(s) are under D39(2)'s "
            f"{analysis_floor()}-closed floor."
        )
        lines.append("  A flagged cell is NOT EVIDENCE: a number whose sample")
        lines.append("  is too small to act on, printed so the thinness shows.")
    lines.append("")
    return lines


def analysis_floor() -> int:
    return analytics.MIN_SAMPLE


def run(*, as_of: date | None = None, send: bool = True) -> str:
    """Collect, render and send. Returns the body.

    Non-critical, like the digest: a scorecard that failed to send is a bad
    Friday, not a lost day of history.
    """
    from core.jobs import job_run

    day = as_of or clock.session_date()
    card = collect(as_of=day)
    subject, body = render(card)

    with job_run(JOB_NAME, session_date=day, is_critical=False, triggered_by="cron") as job:
        if send:
            delivery = notify.notify(
                subject,
                body,
                alert_type="weekly_scorecard",
                severity="warning" if card.week_partial else "info",
                dedupe_key=card.dedupe_key,
            )
            job.note(
                channels_sent=list(delivery.sent),
                channels_suppressed=list(delivery.suppressed),
                channels_failed=list(delivery.failed),
                channels_skipped=list(delivery.skipped),
                dedupe_key=card.dedupe_key,
                open_calls=len(card.open_positions),
                closed_this_week=len(card.closed),
                cells=len(card.cells),
                week_partial=card.week_partial,
            )
            if not delivery.reached_someone:
                log.error("weekly scorecard for %s reached nobody: %s", day, delivery)
        else:
            job.note(rendered_only=True, open_calls=len(card.open_positions))
        job.add_rows(len(card.open_positions) + len(card.closed))
    return body


def _mask_totals(stats: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in stats:
        for pillars, count in (row["mask_histogram"] or {}).items():
            out[str(pillars)] = out.get(str(pillars), 0) + int(count)
    return out


def _level(value: float | None, pct: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}" + ("" if pct is None else f" ({pct:+.1f}%)")


def _f(value) -> float | None:
    return None if value is None else float(value)


def _r(value) -> str:
    return "-" if value is None else f"{float(value):+.2f}R"


def _pct(value) -> str:
    return "-" if value is None else f"{float(value):.0%}"


def _num(value) -> str:
    return "-" if value is None else f"{float(value):.1f}"
