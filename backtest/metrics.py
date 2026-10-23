"""Expectancy per recipe, per regime, with fill counts (D39(1)/(2)).

**EXPECTANCY IS THE NORTH STAR (D13), AND IT IS NOT THE WIN RATE.** A recipe
that wins 35% of the time at +3R and loses 65% at -1R makes 0.4R a trade; one
that wins 70% at +0.5R and loses 30% at -1.5R makes 0.0R. Any report that led
with hit rate would rank those the wrong way round, so expectancy comes first
and the hit rate is one of its components.

**FILL COUNTS ARE PART OF THE ANSWER, NOT BOOKKEEPING.** A plan that never
filled is not a loss (D13 calls it `never_triggered`) — but a recipe whose
plans fill 20% of the time needs five times the signals to put the same money
to work, and a sample of eight closed trades cannot support any conclusion at
all. D39(2) sets 30 closed ideas per recipe as the floor for changing a weight
or a threshold, so every table here prints its own n and says when it is under
the line.

Gross and net are both reported. The difference is the friction (D39(4)); a
strategy whose edge disappears into it never had one.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date

from core import config

__all__ = ["Expectancy", "by_recipe", "by_recipe_and_regime", "render_report", "summarise"]

# D39(2): no weight or threshold moves on fewer than this many closed ideas.
MIN_SAMPLE = 30


@dataclass
class Expectancy:
    """One population of simulated trades, measured.

    Deliberately not a single number: `expectancy_r` is the headline, and the
    components are kept beside it because that is what makes a bad headline
    diagnosable — a 0.0R recipe with a 60% hit rate has a target problem, and
    one with a 20% hit rate has an entry problem.
    """

    label: str
    signals: int = 0
    filled: int = 0
    closed: int = 0
    wins: int = 0
    losses: int = 0
    scratches: int = 0
    gross_rs: list[float] = field(default_factory=list)
    net_rs: list[float] = field(default_factory=list)
    hold_days: list[int] = field(default_factory=list)
    outcomes: dict[str, int] = field(default_factory=dict)

    # ── headline ────────────────────────────────────────────────────────────

    @property
    def expectancy_r(self) -> float | None:
        """Mean NET R per CLOSED trade. The number strategy selection uses."""
        return statistics.fmean(self.net_rs) if self.net_rs else None

    @property
    def standard_error_r(self) -> float | None:
        """Standard error of the mean net R: sd / sqrt(n).

        What makes "+0.22R beats +0.16R" answerable. R multiples disperse
        around ±1, so a few dozen trades carry an SE of 0.15R and a six-point
        difference between two such populations is noise wearing a decimal
        point. None under two trades, where the sd is undefined — not 0.0,
        which would read as a certain measurement.
        """
        if len(self.net_rs) < 2:
            return None
        return statistics.stdev(self.net_rs) / math.sqrt(len(self.net_rs))

    @property
    def gross_expectancy_r(self) -> float | None:
        return statistics.fmean(self.gross_rs) if self.gross_rs else None

    @property
    def friction_r(self) -> float | None:
        """What execution took. Gross minus net, per trade."""
        if not self.net_rs or not self.gross_rs:
            return None
        return self.gross_expectancy_r - self.expectancy_r

    # ── components ──────────────────────────────────────────────────────────

    @property
    def fill_rate(self) -> float | None:
        return self.filled / self.signals if self.signals else None

    @property
    def hit_rate(self) -> float | None:
        decided = self.wins + self.losses + self.scratches
        return self.wins / decided if decided else None

    @property
    def avg_win_r(self) -> float | None:
        wins = [r for r in self.net_rs if r > 0]
        return statistics.fmean(wins) if wins else None

    @property
    def avg_loss_r(self) -> float | None:
        losses = [r for r in self.net_rs if r < 0]
        return statistics.fmean(losses) if losses else None

    @property
    def profit_factor(self) -> float | None:
        """Gross wins over gross losses. None when nothing lost — which is a
        sample too small to have a profit factor, not an infinite one."""
        won = sum(r for r in self.net_rs if r > 0)
        lost = -sum(r for r in self.net_rs if r < 0)
        return won / lost if lost > 0 else None

    @property
    def median_hold_days(self) -> float | None:
        return statistics.median(self.hold_days) if self.hold_days else None

    @property
    def sufficient(self) -> bool:
        """D39(2)'s floor: enough closed trades to justify moving a number."""
        return self.closed >= MIN_SAMPLE

    def add(self, trade) -> None:
        self.signals += 1
        self.outcomes[trade.outcome] = self.outcomes.get(trade.outcome, 0) + 1
        if trade.entry is None:
            return
        self.filled += 1
        if not trade.closed or trade.net_r is None:
            return
        self.closed += 1
        self.net_rs.append(trade.net_r)
        if trade.gross_r is not None:
            self.gross_rs.append(trade.gross_r)
        if trade.days_held is not None:
            self.hold_days.append(trade.days_held)
        # A scratch is its own category: at breakeven after the TP1 trim the
        # trade was neither right nor wrong, and counting it as a loss would
        # understate a recipe whose risk management worked.
        if trade.net_r > 0.05:
            self.wins += 1
        elif trade.net_r < -0.05:
            self.losses += 1
        else:
            self.scratches += 1


def summarise(trades: list, *, key, label_of=str) -> dict[str, Expectancy]:
    """Group trades by `key(trade)` and measure each group."""
    groups: dict[str, Expectancy] = {}
    for trade in trades:
        name = label_of(key(trade))
        groups.setdefault(name, Expectancy(label=name)).add(trade)
    return dict(sorted(groups.items()))


def by_recipe(trades: list) -> dict[str, Expectancy]:
    return summarise(trades, key=lambda t: t.recipe)


def eligible(trades: list, *, min_rr: float) -> list:
    """The trades D12 would actually have let through.

    **THE RECIPES PRODUCE PLANS THE ENGINE REFUSES.** A recipe answers "does
    this setup exist"; D12 then demands R:R >= 2 (Tier A) or >= 3 (Tier B), and
    `decision/idea_builder.py` rejects anything below. Measured over four
    years, `pullback_to_trend`'s MEDIAN R:R is 0.85 and only 4% of its plans
    reach 2:1 — so expectancy over all signals is expectancy over a population
    the live engine would mostly decline. Both cuts are reported: the ungated
    one says what the SETUP is worth, the gated one says what the ENGINE would
    have traded.
    """
    return [t for t in trades if (t.risk_reward or 0) >= min_rr]


def by_recipe_and_regime(trades: list) -> dict[str, Expectancy]:
    return summarise(trades, key=lambda t: f"{t.recipe} / {t.regime}")


# ── rendering ───────────────────────────────────────────────────────────────

HEADER = (
    f"{'population':<46} {'sig':>5} {'fill':>5} {'clsd':>5} {'hit':>6} "
    f"{'expR':>7} {'grossR':>7} {'frict':>6} {'PF':>6} {'hold':>5}"
)


def _row(item: Expectancy) -> str:
    return (
        f"{item.label[:46]:<46} {item.signals:>5} {item.filled:>5} {item.closed:>5} "
        f"{_pct(item.hit_rate):>6} {_num(item.expectancy_r):>7} "
        f"{_num(item.gross_expectancy_r):>7} {_num(item.friction_r):>6} "
        f"{_num(item.profit_factor):>6} {_num(item.median_hold_days, 0):>5}"
        + ("" if item.sufficient else "  (n<30)")
    )


def render_report(result) -> str:
    """The deliverable: what each recipe is worth, and on what evidence."""
    lines: list[str] = []
    promotion = config.scoring().promotion.status
    variants = sorted({getattr(t, "param_set", "baseline") for t in result.trades})
    # EVERY HEADLINE TABLE DESCRIBES ONE CONFIGURATION. With `--variant` the
    # run holds two populations over the same walk, and pooling them into the
    # per-recipe, D12-eligible, regime and outcome tables would describe a
    # strategy nobody runs — each trade counted once under each configuration
    # that produced it. The comparison gets its own section instead.
    reference = [t for t in result.trades if _is_baseline(t, variants)]
    lines.append("=" * 118)
    lines.append(
        f"BACKTEST — {result.start} to {result.end}, {result.symbols} symbol(s), "
        f"{len(result.trades)} simulated trade(s)"
        + ("" if len(variants) < 2 else f" across {len(variants)} configuration(s)")
    )
    lines.append("=" * 118)
    lines.extend(_provenance_block(result, variants))
    lines.append("")
    if result.regimes:
        lines.append("Regime sessions (D17's trend test, SPY realised-vol terciles):")
        lines.append("  " + "   ".join(f"{k} {v}" for k, v in result.regimes.items()))
        lines.append("")

    recipes = by_recipe(reference)
    lines.append("PER RECIPE" + ("" if len(variants) < 2 else f"  [{variants[0]}]"))
    lines.append(HEADER)
    lines.append("-" * 118)
    for item in recipes.values():
        lines.append(_row(item))
    lines.append("")

    if len(variants) > 1:
        # SIDE BY SIDE, from one walk. Two separate runs would leave open
        # whether they saw the same data; one walk with two configurations
        # cannot.
        lines.append("VARIANTS — the same walk, one configuration changed")
        lines.append(HEADER)
        lines.append("-" * 118)
        for name in variants:
            subset = [t for t in result.trades if getattr(t, "param_set", "baseline") == name]
            for recipe, item in by_recipe(subset).items():
                item.label = f"{name} / {recipe}"
                lines.append(_row(item))
        lines.append("")

    gates = config.risk().min_risk_reward
    for tier, minimum in (("A", gates.tier_a), ("B", gates.tier_b)):
        subset = eligible(reference, min_rr=minimum)
        lines.append(f"PER RECIPE — ONLY PLANS D12 WOULD ACCEPT (Tier {tier}: R:R >= {minimum:g})")
        lines.append(HEADER)
        lines.append("-" * 118)
        for item in by_recipe(subset).values():
            lines.append(_row(item))
        if not subset:
            lines.append("  (no plan in the window cleared this gate)")
        lines.append("")

    lines.append("PER RECIPE x REGIME")
    lines.append(HEADER)
    lines.append("-" * 118)
    for item in by_recipe_and_regime(reference).values():
        lines.append(_row(item))
    lines.append("")

    lines.append("OUTCOME MIX")
    for name, item in recipes.items():
        mix = "  ".join(f"{k} {v}" for k, v in sorted(item.outcomes.items()))
        lines.append(f"  {name:<24} {mix}")
    lines.append("")

    lines.append("PROMOTION (D40) — shadow until the evidence says otherwise")
    for name, item in recipes.items():
        status = getattr(promotion, name, "shadow")
        verdict = _verdict(item)
        lines.append(f"  {name:<24} status={status:<7} {verdict}")
    lines.append("")
    lines.append(
        "expR is mean NET R per closed trade (friction charged); hit counts a "
        "breakeven scratch separately."
    )
    lines.append(
        "Survivorship: the universe is TODAY's constituents, so every delisted "
        "loser is absent. These are upper bounds."
    )
    lines.append(
        "A backtest cannot promote anything: D40 counts CLOSED SHADOW BOOK ideas, "
        "and a simulated trade is not one."
    )
    return "\n".join(lines)


def _provenance_block(result, variants: list[str]) -> list[str]:
    """What code and config produced these numbers, and whether they agree.

    **A COMPARISON ACROSS DIFFERENT CODE IS NOT A COMPARISON.** Two runs whose
    commits differ answer two questions, and the difference between them is
    partly the code. The header says so loudly rather than leaving a reader to
    assume one walk produced everything.
    """
    lines: list[str] = []
    stamp = getattr(result, "provenance", None)
    if stamp is not None and hasattr(stamp, "header"):
        lines.extend(stamp.header())

    commits = sorted({getattr(t, "commit", "unknown") for t in result.trades})
    hashes = sorted({getattr(t, "config_hash", "unknown") for t in result.trades})
    dirty_rows = sum(1 for t in result.trades if getattr(t, "dirty", False))

    if stamp is None and result.trades:
        lines.append(f"code      {', '.join(commits)}")
    if len(commits) > 1:
        lines.append("")
        lines.append(
            f"!! MIXED CODE: these rows come from {len(commits)} different commits "
            f"({', '.join(commits)}). The difference between them is partly the code, "
            f"not the configuration — this is not a comparison."
        )
    if dirty_rows and stamp is None:
        lines.append(
            f"!! NON-BASELINE: {dirty_rows} row(s) were produced from a dirty tree and "
            f"cannot be reproduced."
        )
    if len(variants) > 1:
        by_variant = {}
        for trade in result.trades:
            by_variant.setdefault(getattr(trade, "param_set", "baseline"), set()).add(
                getattr(trade, "config_hash", "unknown")
            )
        lines.append("")
        lines.append("configurations compared (effective-config hash):")
        for name in variants:
            lines.append(f"  {name:<40} {', '.join(sorted(by_variant.get(name, {'?'})))}")
        if len(hashes) < len(variants):
            lines.append(
                "  !! two arms share a hash — the override did not change the effective "
                "config, so this comparison is measuring noise."
            )
    lines.append("")
    return lines


def _is_baseline(trade, variants: list[str]) -> bool:
    """Whether a trade belongs to the run's reference configuration.

    With one configuration every trade does; with several, the headline tables
    describe the BASELINE and the comparison gets its own section — averaging
    two configurations into one row would describe a strategy nobody runs.
    """
    return getattr(trade, "param_set", "baseline") == variants[0]


def _verdict(item: Expectancy) -> str:
    """What D39(2)/D40 permit on this evidence, stated as a rule not a hope."""
    if item.closed < MIN_SAMPLE:
        return (
            f"{item.closed} closed — under D39(2)'s 30-idea floor; no promotion, "
            f"no threshold change"
        )
    value = item.expectancy_r
    if value is None:
        return f"{item.closed} closed but no R recorded"
    if value <= 0:
        return f"expectancy {value:+.2f}R on {item.closed} — negative; stays shadow"
    return (
        f"expectancy {value:+.2f}R on {item.closed} closed — positive on backtest "
        f"evidence, which D40 does NOT accept for promotion: the ladder counts "
        f"CLOSED SHADOW BOOK ideas"
    )


# ── reading a finished run back ─────────────────────────────────────────────


@dataclass
class CsvTrade:
    """One row of a written backtest, with the attributes the metrics read.

    Deliberately NOT `engine.SimTrade`: re-rendering a finished run must not
    depend on importing the walk, and a report over a 70-minute run should
    never need the run again. `python -m backtest.metrics data/backtest.csv`
    re-reads and re-renders in a second, which is what made the D12-eligible
    cut above possible without re-walking four years.
    """

    param_set: str
    commit: str
    config_hash: str
    dirty: bool
    recipe: str
    regime: str
    outcome: str
    symbol: str
    called_on: date | None
    """When the plan was formed. D50 windows are cut on THIS, not on the exit:
    a trade belongs to the window that produced the decision."""
    risk_reward: float | None
    entry: float | None
    gross_r: float | None
    net_r: float | None
    days_held: int | None

    @property
    def filled(self) -> bool:
        return self.entry is not None

    @property
    def closed(self) -> bool:
        return self.outcome in ("stopped", "target_1", "target_2", "timeout")


@dataclass
class CsvRun:
    """What `render_report` needs, read from a CSV."""

    trades: list[CsvTrade]
    start: str
    end: str
    symbols: int
    regimes: dict[str, int] = field(default_factory=dict)
    provenance: object = None


def load_csv(path) -> CsvRun:
    import csv
    from pathlib import Path

    rows: list[CsvTrade] = []
    symbols: set[str] = set()
    with Path(path).open(newline="") as handle:
        for row in csv.DictReader(handle):
            symbols.add(row["symbol"])
            rows.append(
                CsvTrade(
                    param_set=row.get("param_set") or "baseline",
                    commit=row.get("commit") or "unknown",
                    config_hash=row.get("config_hash") or "unknown",
                    dirty=str(row.get("dirty") or "1") == "1",
                    recipe=row["recipe"],
                    regime=row["regime"],
                    outcome=row["outcome"],
                    symbol=row["symbol"],
                    called_on=_date(row.get("called_on")),
                    risk_reward=_float(row.get("risk_reward")),
                    entry=_float(row.get("entry")),
                    gross_r=_float(row.get("gross_r")),
                    net_r=_float(row.get("net_r")),
                    days_held=_int(row.get("days_held")),
                )
            )
    return CsvRun(trades=rows, start="(from csv)", end="(from csv)", symbols=len(symbols))


def _date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _float(value) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _int(value) -> int | None:
    number = _float(value)
    return None if number is None else int(number)


def _num(value: float | None, places: int = 2) -> str:
    return "-" if value is None else f"{value:.{places}f}"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def main(argv: list[str] | None = None) -> int:
    """Re-render a written backtest. `python -m backtest.metrics data/backtest.csv`"""
    import argparse

    parser = argparse.ArgumentParser(description="re-render a written backtest")
    parser.add_argument("csv", help="a CSV written by `backtest.engine --out`")
    parser.add_argument(
        "--only",
        metavar="PARAM_SET",
        help="render ONE configuration's own tables (default: the first, which is "
        "the baseline). A multi-variant run has a full report per configuration, "
        "and the D12, regime and outcome tables are only meaningful inside one.",
    )
    args = parser.parse_args(argv)
    run = load_csv(args.csv)
    if args.only:
        kept = [t for t in run.trades if t.param_set == args.only]
        if not kept:
            names = sorted({t.param_set for t in run.trades})
            raise SystemExit(f"no trades for {args.only!r}; this file has {names}")
        run.trades = kept
    print(render_report(run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
