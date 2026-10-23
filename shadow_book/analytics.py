"""Expectancy per recipe x regime — the query a promotion decision reads.

    python -m shadow_book.analytics
    python -m shadow_book.analytics --recipe post_earnings_drift --days 90

D13 names expectancy the north star; D40 defines the ladder; **D45 says the
ladder is evaluated per recipe x REGIME**, because `post_earnings_drift`
measured +1.12R in one regime and negative in another and a blended number
describes no market that will recur in that mixture. So the unit of judgement
here is a CELL: one recipe, one regime, one ladder state.

**EVERY CELL CARRIES ITS OWN SAMPLE COUNT AND ITS OWN VERDICT.** D39(2) fixes
a floor of 30 closed ideas before any threshold or weight moves, and under D45
that floor applies PER CELL rather than to the recipe as a whole — which makes
promotion slower and the claim it rests on much narrower. A cell under the
floor is not "slightly less reliable", it is **not evidence**, and this module
prints it as `n<30` rather than letting a seductive number stand next to a
sample of four.

**IT READS; IT NEVER DECIDES.** Nothing here writes a status or promotes
anything: D40 ships the ladder as `scoring.promotion.status` in config, where a
promotion is a one-line diff a human makes. This computes what the rule would
say and shows the evidence next to it, so the diff is informed rather than
automated — and so a bug here cannot promote a strategy.

Three things are deliberately NOT computed. **Calibration** (D39(1)'s
score-to-hit-rate curve) needs the composite banded against realised outcomes
and belongs in its own module. **Backtest expectancy** is not admissible here
at all (D40 counts closed SHADOW BOOK ideas, and a simulated trade is not
one). And **open ideas** are excluded from every statistic: an unresolved trade
has no R multiple, and counting it either way would be a claim the data cannot
support.
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta

from core import clock, config, db

log = logging.getLogger(__name__)

__all__ = ["Cell", "LadderVerdict", "cells", "render", "verdict_for"]

# D39(2). Repeated here as a module constant rather than read from config
# because it is a DECISION, not an [OPEN] value — `backtest/metrics.py` holds
# the same number for the same reason.
MIN_SAMPLE = 30

# `trailed` is a CLOSED outcome: the runner left on its trail, which is a
# resolution rather than an open position (O20).
CLOSED = ("stopped", "target_1", "target_2", "trailed", "timeout")

ROWS_SQL = """
    SELECT recipe,
           COALESCE(regime_at_call, 'unknown') AS regime,
           COALESCE(status_at_call, 'shadow')  AS status,
           tier,
           outcome,
           r_multiple,
           days_held,
           called_on,
           proj_confidence,
           pillars_scored
    FROM   shadow_book
    WHERE  origin = 'engine'
      AND  called_on >= %(since)s
      AND  called_on <= %(as_of)s
    ORDER  BY recipe, regime, called_on
"""


@dataclass
class Cell:
    """One recipe x regime x ladder state, measured.

    The components sit beside the headline because that is what makes a bad
    headline diagnosable: 0.0R at a 60% hit rate is a target problem, and 0.0R
    at a 20% hit rate is an entry problem, and the two want opposite fixes.
    """

    recipe: str
    regime: str
    status: str
    signals: int = 0
    closed: int = 0
    wins: int = 0
    losses: int = 0
    scratches: int = 0
    open_now: int = 0
    never_triggered: int = 0
    r_multiples: list[float] = field(default_factory=list)
    hold_days: list[int] = field(default_factory=list)
    confidences: list[float] = field(default_factory=list)
    first_called: date | None = None
    last_called: date | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.recipe, self.regime, self.status)

    @property
    def label(self) -> str:
        return f"{self.recipe} / {self.regime} / {self.status}"

    # ── the statistics ──────────────────────────────────────────────────────

    @property
    def expectancy_r(self) -> float | None:
        """Mean R per CLOSED idea. None on an empty cell, never 0.0."""
        return statistics.fmean(self.r_multiples) if self.r_multiples else None

    @property
    def hit_rate(self) -> float | None:
        decided = self.wins + self.losses + self.scratches
        return self.wins / decided if decided else None

    @property
    def profit_factor(self) -> float | None:
        """None when nothing lost — a sample with no losses is too small to
        have a profit factor, not one with an infinite edge."""
        won = sum(r for r in self.r_multiples if r > 0)
        lost = -sum(r for r in self.r_multiples if r < 0)
        return won / lost if lost > 0 else None

    @property
    def avg_win_r(self) -> float | None:
        wins = [r for r in self.r_multiples if r > 0]
        return statistics.fmean(wins) if wins else None

    @property
    def avg_loss_r(self) -> float | None:
        losses = [r for r in self.r_multiples if r < 0]
        return statistics.fmean(losses) if losses else None

    @property
    def median_hold_days(self) -> float | None:
        return statistics.median(self.hold_days) if self.hold_days else None

    @property
    def mean_confidence(self) -> float | None:
        """The composite the engine claimed, for D39(1)'s eventual calibration
        curve. Reported, not used: a curve needs bands, not a mean."""
        return statistics.fmean(self.confidences) if self.confidences else None

    @property
    def sufficient(self) -> bool:
        """D39(2)'s floor, applied PER CELL under D45."""
        return self.closed >= MIN_SAMPLE

    @property
    def span_days(self) -> int | None:
        if self.first_called is None or self.last_called is None:
            return None
        return (self.last_called - self.first_called).days

    def add(self, row: dict) -> None:
        self.signals += 1
        called = row["called_on"]
        self.first_called = called if self.first_called is None else min(self.first_called, called)
        self.last_called = called if self.last_called is None else max(self.last_called, called)
        if row["proj_confidence"] is not None:
            self.confidences.append(float(row["proj_confidence"]))

        outcome = row["outcome"]
        if outcome == "open":
            self.open_now += 1
            return
        if outcome == "never_triggered":
            # NOT a loss (D13). Counting unfilled plans as losses would make
            # selectivity look like incompetence.
            self.never_triggered += 1
            return
        if outcome not in CLOSED or row["r_multiple"] is None:
            return

        self.closed += 1
        value = float(row["r_multiple"])
        self.r_multiples.append(value)
        if row["days_held"] is not None:
            self.hold_days.append(int(row["days_held"]))
        # A scratch is its own category: stopped at breakeven after the TP1
        # trim, the trade was neither right nor wrong, and calling it a loss
        # understates a recipe whose risk management worked.
        if value > 0.05:
            self.wins += 1
        elif value < -0.05:
            self.losses += 1
        else:
            self.scratches += 1


@dataclass(frozen=True)
class LadderVerdict:
    """What D40's rule says about one cell, and on what evidence."""

    cell: Cell
    rule: str
    """`insufficient`, `promote`, `hold`, or `demote`."""
    detail: str

    @property
    def actionable(self) -> bool:
        return self.rule in ("promote", "demote")


def verdict_for(cell: Cell, *, cfg=None) -> LadderVerdict:
    """Apply D40's thresholds to one cell. Reads config; decides nothing.

    The floor is checked FIRST and short-circuits. A cell with eight closed
    ideas and +2.0R expectancy is not a promotion candidate with a small
    sample — under D39(2) it is not evidence at all, and the order of these
    checks is what keeps that from being a judgement call.
    """
    promotion = (cfg or config.scoring()).promotion
    if not cell.sufficient:
        return LadderVerdict(
            cell,
            "insufficient",
            f"{cell.closed} closed, under D39(2)'s {MIN_SAMPLE}-idea floor for this cell",
        )

    value = cell.expectancy_r
    if value is None:
        return LadderVerdict(cell, "insufficient", "no R multiples recorded")

    demotion = promotion.demotion
    if cell.closed >= demotion.min_closed_ideas and value <= demotion.expectancy_r_below:
        return LadderVerdict(
            cell,
            "demote",
            f"expectancy {value:+.2f}R on {cell.closed} closed, at or below "
            f"{demotion.expectancy_r_below:+.2f}R",
        )

    if cell.status == "shadow":
        gate = promotion.shadow_to_paper
        if value >= gate.min_expectancy_r:
            return LadderVerdict(
                cell,
                "promote",
                f"expectancy {value:+.2f}R on {cell.closed} closed clears "
                f"shadow->paper ({gate.min_expectancy_r:+.2f}R on "
                f"{gate.min_closed_ideas})",
            )
        return LadderVerdict(
            cell,
            "hold",
            f"expectancy {value:+.2f}R is under shadow->paper's {gate.min_expectancy_r:+.2f}R",
        )

    if cell.status == "paper":
        gate = promotion.paper_to_live
        span = cell.span_days or 0
        if value >= gate.min_expectancy_r and span >= gate.sustained_window_days:
            return LadderVerdict(
                cell,
                "promote",
                f"expectancy {value:+.2f}R on {cell.closed} closed over {span} days "
                f"clears paper->live — CALIBRATION (D39(1)) IS NOT CHECKED HERE "
                f"and the gate also requires it",
            )
        if value < gate.min_expectancy_r:
            return LadderVerdict(
                cell,
                "hold",
                f"expectancy {value:+.2f}R is under paper->live's {gate.min_expectancy_r:+.2f}R",
            )
        return LadderVerdict(
            cell,
            "hold",
            f"expectancy {value:+.2f}R clears the bar but the window is {span} days, "
            f"under the {gate.sustained_window_days} paper->live requires",
        )

    return LadderVerdict(cell, "hold", f"live, expectancy {value:+.2f}R on {cell.closed} closed")


def cells(
    *, as_of: date | None = None, days: int | None = None, recipe: str | None = None
) -> list[Cell]:
    """Every recipe x regime x status cell in the window, measured.

    `days` bounds the window. Unbounded by default: expectancy is a property of
    a strategy over its whole life, and the windowed view is what D40's
    DEMOTION rule reads (60 days), which is a different question.
    """
    day = as_of or clock.session_date()
    since = day - timedelta(days=days) if days else date(1970, 1, 1)
    rows = db.fetch_all(ROWS_SQL, {"since": since, "as_of": day})

    out: dict[tuple[str, str, str], Cell] = {}
    for row in rows:
        if recipe and row["recipe"] != recipe:
            continue
        key = (row["recipe"] or "unknown", row["regime"], row["status"])
        cell = out.get(key)
        if cell is None:
            cell = out[key] = Cell(recipe=key[0], regime=key[1], status=key[2])
        cell.add(row)
    return [out[key] for key in sorted(out)]


def totals(items: list[Cell]) -> dict[str, Cell]:
    """Per-recipe roll-ups ACROSS regimes, for context only.

    Printed under the cells, never above them: D45 exists because the blended
    number is the misleading one, and a reader who sees it first will anchor on
    it. Kept because "is this recipe doing anything at all" is still a question
    worth being able to answer in one line.
    """
    rolled: dict[str, Cell] = {}
    for item in items:
        cell = rolled.setdefault(item.recipe, Cell(recipe=item.recipe, regime="ALL", status="ALL"))
        cell.signals += item.signals
        cell.closed += item.closed
        cell.wins += item.wins
        cell.losses += item.losses
        cell.scratches += item.scratches
        cell.open_now += item.open_now
        cell.never_triggered += item.never_triggered
        cell.r_multiples.extend(item.r_multiples)
        cell.hold_days.extend(item.hold_days)
        cell.confidences.extend(item.confidences)
        for attr, pick in (("first_called", min), ("last_called", max)):
            mine, theirs = getattr(cell, attr), getattr(item, attr)
            if theirs is not None:
                setattr(cell, attr, theirs if mine is None else pick(mine, theirs))
    return dict(sorted(rolled.items()))


HEADER = (
    f"{'recipe / regime / status':<52} {'sig':>5} {'open':>5} {'clsd':>5} {'hit':>6} "
    f"{'expR':>7} {'avgW':>6} {'avgL':>6} {'PF':>6} {'hold':>5}"
)


def _row(cell: Cell) -> str:
    return (
        f"{cell.label[:52]:<52} {cell.signals:>5} {cell.open_now:>5} {cell.closed:>5} "
        f"{_pct(cell.hit_rate):>6} {_num(cell.expectancy_r):>7} {_num(cell.avg_win_r):>6} "
        f"{_num(cell.avg_loss_r):>6} {_num(cell.profit_factor):>6} "
        f"{_num(cell.median_hold_days, 0):>5}" + ("" if cell.sufficient else f"  n<{MIN_SAMPLE}")
    )


def render(items: list[Cell], *, window: str = "all time") -> str:
    """The table a promotion decision reads."""
    lines = ["=" * 118]
    lines.append(f"SHADOW BOOK EXPECTANCY — per recipe x regime x ladder state ({window})")
    lines.append("=" * 118)
    if not items:
        lines.append("")
        lines.append("No Shadow Book calls yet. Nothing to measure, which is not nothing to say:")
        lines.append("the engine has surfaced no idea that cleared a gate, so there is no")
        lines.append("evidence for or against any recipe — and D40 keeps every one in `shadow`.")
        return "\n".join(lines)

    lines.append("")
    lines.append(HEADER)
    lines.append("-" * 118)
    for cell in items:
        lines.append(_row(cell))

    lines.append("")
    lines.append("ROLL-UP ACROSS REGIMES (context only — D45 judges the cells above)")
    lines.append(HEADER)
    lines.append("-" * 118)
    for cell in totals(items).values():
        lines.append(_row(cell))

    lines.append("")
    lines.append("WHAT D40'S RULE SAYS (read-only: a promotion is a one-line config diff)")
    for cell in items:
        verdict = verdict_for(cell)
        mark = "**" if verdict.actionable else "  "
        lines.append(f"{mark} {cell.label:<52} {verdict.rule:<13} {verdict.detail}")

    lines.append("")
    lines.append(
        f"A cell under {MIN_SAMPLE} closed ideas is NOT EVIDENCE (D39(2)), not merely "
        f"a smaller number — the floor applies per cell under D45."
    )
    lines.append(
        "`never_triggered` plans are excluded from hit rate and expectancy (D13): an "
        "unfilled plan is not a loss."
    )
    lines.append(
        "Open ideas are counted and then excluded from every statistic — an unresolved "
        "trade has no R multiple."
    )
    return "\n".join(lines)


def _num(value: float | None, places: int = 2) -> str:
    return "-" if value is None else f"{value:.{places}f}"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=int, help="bound the window (D40's demotion rule uses 60)")
    parser.add_argument("--recipe", help="one recipe only")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    window = f"last {args.days} days" if args.days else "all time"
    print(render(cells(days=args.days, recipe=args.recipe), window=window))
    return 0


if __name__ == "__main__":
    sys.exit(main())
