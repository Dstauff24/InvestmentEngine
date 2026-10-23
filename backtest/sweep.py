"""D43: sweep the thresholds that decide whether a recipe fires at all.

    python -m backtest.sweep --measure-only
    python -m backtest.sweep --sample 60 --years 3 --workers 2

**WHY THESE PARAMETERS CANNOT BE TUNED THE NORMAL WAY.** D39(2) moves a number
only on 30 closed Shadow Book ideas. An EXISTENCE GATE set too tight produces
no ideas at all, so it can never accrue the evidence that would loosen it —
unsatisfiable by construction. `base_breakout` is the live proof: its
`range_contraction_ratio` of 0.6 admitted nothing in 433 symbols, so after a
month of running it would have exactly as much evidence as on day one. (That
parameter is retired; O13 replaced the measurement it read.)

**THE LIVE DISTRIBUTION SETS THE RANGE, NOT TASTE.** The sweep first MEASURES
the gated quantity across the universe — what contraction ratios, base depths,
RSI readings and gap sizes actually occur — and places the candidate values at
percentiles of that distribution. A sweep over invented values would test
whether the engine likes round numbers.

**D50: THE VALUE IS PICKED BY A RULE, AND NOT IN THE WINDOW IT IS JUDGED IN.**
Two guards, both mechanical, because a human reading a sweep table picks the
best cell and a best cell is often noise:

  1. **Centre of the widest positive plateau** (`plateau_choice`). A swept value
     qualifies on positive expectancy AND D39(2)'s 30 closed trades; the chosen
     value must have a QUALIFYING NEIGHBOUR ON EACH SIDE, so a lone spike —
     one good cell between two bad ones — disqualifies itself. No plateau means
     no value is picked, and for a dormant recipe that means it stays dormant.
     A plateau is a claim that the result survives the gate being slightly
     wrong, which is the only kind of result worth shipping.
  2. **Split sample.** Selection runs on an IN-SAMPLE window and the adoption
     criteria are then applied to a LATER, non-overlapping one. `--start` and
     `--end` must be given explicitly: without them this module REFUSES to
     print a selection, because a table whose window nobody pinned is a table
     somebody will pick from.

The axis must also carry at least `MIN_AXIS_VALUES` candidates, or "is this a
plateau or a spike" is not a question the output can answer.

**ONE AXIS AT A TIME, FROM THE BASELINE.** Not a grid. A grid of five axes is
hundreds of configurations whose interactions cannot be read off the output,
and the question here is narrower and answerable: with everything else as
shipped, what does THIS threshold cost and buy. Interactions are a question for
a sweep that has evidence behind its single axes first.

Gate-only axes are nearly free — they share the baseline's features, so one
pass over the dates serves all of them. The two STRUCTURAL parameters
(`min_base_weeks`, `max_base_depth_pct`) are read by the measurement as well as
the gate, so each value needs its own feature pass; that is why they are swept
at fewer values.
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta

from backtest.engine import LOOKBACK_BARS, WARMUP_BARS, ParamSet, load_symbol_history
from backtest.engine import run as run_engine
from backtest.metrics import Expectancy, summarise
from core import clock, config, db
from features import technical as technical_features

log = logging.getLogger(__name__)

__all__ = [
    "Axis",
    "Distributions",
    "Plateau",
    "Point",
    "axes_from",
    "measure",
    "param_sets_for",
    "plateau_choice",
    "run",
]

# D50. Fewer candidates than this and a "plateau" cannot be distinguished from
# a spike, so the rule would be decoration.
MIN_AXIS_VALUES = 5

# Every quantity an existence gate reads, with the parameter that gates it.
GATED = {
    "squeeze_percentile": "recipes.base_breakout.max_squeeze_percentile",
    "base_depth_pct": "recipes.base_breakout.max_base_depth_pct",
    "base_weeks": "recipes.base_breakout.min_base_weeks",
    "relative_volume": "recipes.base_breakout.min_relative_volume_on_break",
    "rsi": "recipes.pullback_to_trend.rsi_max",
    "gap_up_pct": "recipes.post_earnings_drift.min_gap_up_pct",
}


@dataclass
class Distributions:
    """What the universe actually looks like, per gated quantity."""

    samples: dict[str, list[float]] = field(default_factory=dict)
    observations: int = 0
    symbols: int = 0

    def add(self, name: str, value: float | None) -> None:
        if value is None:
            return
        self.samples.setdefault(name, []).append(float(value))

    def quantile(self, name: str, q: float) -> float | None:
        values = sorted(self.samples.get(name, []))
        if not values:
            return None
        index = min(len(values) - 1, max(0, round(q * (len(values) - 1))))
        return values[index]

    def summary(self, name: str) -> str:
        values = self.samples.get(name, [])
        if not values:
            return f"{name}: no observations"
        return (
            f"{name}: n={len(values)} "
            f"p10={self.quantile(name, 0.10):.2f} p25={self.quantile(name, 0.25):.2f} "
            f"median={statistics.median(values):.2f} "
            f"p75={self.quantile(name, 0.75):.2f} p90={self.quantile(name, 0.90):.2f}"
        )

    def __str__(self) -> str:
        head = f"measured over {self.symbols} symbol(s), {self.observations} session(s)"
        return "\n".join([head, *(f"  {self.summary(name)}" for name in sorted(self.samples))])


def measure(
    *,
    symbols: list[str] | None = None,
    as_of: date | None = None,
    years: int = 3,
    stride: int = 5,
    sample: int | None = 60,
) -> Distributions:
    """Measure the gated quantities across a sample of symbols and sessions.

    `stride` reads every Nth session: these distributions are about the shape
    of the universe, and consecutive sessions are nearly the same observation.
    """
    today = as_of or clock.session_date()
    start = today - timedelta(days=365 * years)
    universe = symbols if symbols is not None else db.active_symbols()
    if sample:
        universe = universe[:sample]

    out = Distributions(symbols=0)
    for symbol in universe:
        history = load_symbol_history(symbol, as_of=today)
        if not history.usable:
            continue
        out.symbols += 1
        sessions = list(history.bars["session_date"])
        for index in range(WARMUP_BARS, len(sessions), stride):
            day = sessions[index]
            if day < start:
                continue
            window = history.bars.iloc[max(0, index - LOOKBACK_BARS + 1) : index + 1]
            features = technical_features.compute(symbol, window)
            out.observations += 1
            out.add("rsi", features.rsi)
            out.add("relative_volume", features.relative_volume)
            out.add("squeeze_percentile", features.squeeze_percentile)
            out.add("base_depth_pct", features.structure.base_depth_pct)
            out.add("base_weeks", features.structure.base_weeks)
            out.add("gap_up_pct", features.structure.gap_up_pct)
        log.info("measured %s (%d session(s) so far)", symbol, out.observations)
    return out


@dataclass(frozen=True)
class Point:
    """One swept value's result for the recipe its gate belongs to."""

    value: float
    expectancy_r: float | None
    closed: int

    @property
    def qualifies(self) -> bool:
        """Positive expectancy on a population D39(2) will look at."""
        return (
            self.expectancy_r is not None
            and self.expectancy_r > 0
            and self.closed >= metrics_min_sample()
        )


@dataclass(frozen=True)
class Plateau:
    """What D50's rule returns: a value, or a refusal, and why either way."""

    chosen: float | None
    reason: str
    members: tuple[float, ...] = ()

    def __str__(self) -> str:
        head = "no value" if self.chosen is None else f"{self.chosen:g}"
        return f"{head} — {self.reason}"


def metrics_min_sample() -> int:
    from backtest.metrics import MIN_SAMPLE

    return MIN_SAMPLE


def plateau_choice(points: list[Point]) -> Plateau:
    """D50's selection rule, applied mechanically.

    The centre of the WIDEST run of consecutive qualifying values, where
    qualifying is positive expectancy with at least D39(2)'s 30 closed trades.
    A run must be at least three values long, which is what makes "both
    neighbours qualify" true of its centre and what disqualifies a lone spike.

    The tie-breaks are stated rather than left to list order, because a rule
    whose answer depends on iteration order is not a rule:

      * even-length run -> the LOWER of the two middles, the tighter gate;
      * runs of equal width -> the one whose WEAKEST member is strongest, since
        a plateau is only as good as the cell you might land on by accident;
      * still tied -> the lower value.
    """
    ordered = sorted(points, key=lambda p: p.value)
    if len(ordered) < MIN_AXIS_VALUES:
        return Plateau(
            None,
            f"only {len(ordered)} value(s) on the axis, under the {MIN_AXIS_VALUES} a "
            f"plateau needs to be distinguishable from a spike",
        )

    runs: list[list[Point]] = []
    current: list[Point] = []
    for point in ordered:
        if point.qualifies:
            current.append(point)
        else:
            if current:
                runs.append(current)
            current = []
    if current:
        runs.append(current)

    usable = [r for r in runs if len(r) >= 3]
    if not usable:
        best = max((len(r) for r in runs), default=0)
        spike = " (a lone spike, which D50 disqualifies)" if best in (1, 2) else ""
        return Plateau(
            None,
            f"no plateau: the longest run of qualifying values is {best}{spike}, and "
            f"the centre of a plateau must have a qualifying neighbour on each side",
        )

    def rank(run: list[Point]) -> tuple:
        weakest = min(p.expectancy_r or 0.0 for p in run)
        return (len(run), weakest, -run[0].value)

    chosen_run = max(usable, key=rank)
    middle = chosen_run[(len(chosen_run) - 1) // 2]
    return Plateau(
        middle.value,
        f"centre of a {len(chosen_run)}-wide plateau "
        f"({', '.join(f'{p.value:g}' for p in chosen_run)}), weakest member "
        f"{min(p.expectancy_r or 0.0 for p in chosen_run):+.2f}R",
        tuple(p.value for p in chosen_run),
    )


@dataclass(frozen=True)
class Axis:
    """One parameter, the values to try, and where they came from."""

    path: str
    values: tuple[float, ...]
    rationale: str
    structural: bool = False

    @property
    def name(self) -> str:
        return self.path.rsplit(".", 1)[-1]


def axes_from(distributions: Distributions, *, cfg=None) -> list[Axis]:
    """Candidate values placed on the MEASURED distribution (D43).

    The percentile chosen for each gate encodes what the gate is for, and the
    reasoning is in the rationale string so the output explains itself:

      * A `<=` gate (contraction, depth) is a SELECTIVITY dial. Its values sit
        at p10/p25/p50 — "admit the tightest tenth" through "admit the better
        half". The shipped 0.6 contraction sits below the measured minimum,
        which is why it admits nothing.
      * A `>=` gate (base weeks, break volume, gap) is a QUALITY floor, swept
        at the median and up.
      * The RSI band's upper edge is swept around the shipped 50, since the
        40-50 "reset" band is a definition from the recipe rather than a
        distributional claim.
    """
    scoring = cfg or config.scoring()
    axes: list[Axis] = []

    def at(name: str, *quantiles: float) -> tuple[float, ...]:
        values = [distributions.quantile(name, q) for q in quantiles]
        return tuple(round(v, 2) for v in values if v is not None)

    # O13's replacement. A percentile is self-interpreting — a cap of 0.25
    # admits roughly the quietest quarter of observations — so the sweep's
    # values ARE its selectivity, and the quantity is measured independently of
    # the base detector, which is what lets the live distribution set the range
    # here while `max_base_depth_pct` below has to be swept around its shipped
    # value.
    squeeze = at("squeeze_percentile", 0.05, 0.10, 0.20, 0.30, 0.40, 0.50)
    if squeeze:
        axes.append(
            Axis(
                path=GATED["squeeze_percentile"],
                values=tuple(v for v in squeeze if 0 < v < 1),
                rationale=(
                    f"shipped {scoring.recipes.base_breakout.max_squeeze_percentile:g}; "
                    f"six candidates from p05 {squeeze[0]:g} to p50 {squeeze[-1]:g} of "
                    f"where volatility sits in each symbol's own year — D50 needs "
                    f"{MIN_AXIS_VALUES} to tell a plateau from a spike"
                ),
            )
        )

    # A MEASUREMENT BOUNDED BY THE PARAMETER BEING SWEPT CANNOT SET ITS OWN
    # RANGE. `_detect_base` finds the longest consolidation that FITS the
    # current cap, so every observed depth is below it by construction and the
    # measured percentiles can only ever propose a tighter cap. Swept around
    # the shipped value instead, and the circularity is stated rather than
    # hidden behind a percentile that looks data-driven.
    shipped_depth = scoring.recipes.base_breakout.max_base_depth_pct
    axes.append(
        Axis(
            path=GATED["base_depth_pct"],
            values=(round(shipped_depth * 0.8, 1), shipped_depth, round(shipped_depth * 1.4, 1)),
            rationale=(
                f"around the shipped {shipped_depth:g}% cap — the measured depths "
                f"(median {distributions.quantile('base_depth_pct', 0.5) or float('nan'):.1f}%) "
                f"are bounded BY this cap, so they cannot set its range"
            ),
            structural=True,
        )
    )

    volume = at("relative_volume", 0.50, 0.75, 0.90)
    if volume:
        axes.append(
            Axis(
                path=GATED["relative_volume"],
                values=volume,
                rationale="break-day volume floor, at the median and above",
            )
        )

    gap = at("gap_up_pct", 0.75, 0.90)
    if gap:
        axes.append(
            Axis(
                path=GATED["gap_up_pct"],
                values=tuple(v for v in gap if v > 0),
                rationale=f"gap sizes that occur; shipped minimum "
                f"{scoring.recipes.post_earnings_drift.min_gap_up_pct:g}%",
            )
        )

    axes.append(
        Axis(
            path=GATED["rsi"],
            values=(45.0, 50.0, 55.0, 60.0),
            rationale="the reset band's upper edge, around the shipped 50",
        )
    )
    axes.append(
        Axis(
            path=GATED["base_weeks"],
            values=(4.0, 6.0, 8.0),
            rationale=(
                "base length in weeks, around the shipped minimum — measured lengths "
                "are bounded BELOW by this same parameter, so they cannot set its range "
                "either; structural, so each value costs a feature pass"
            ),
            structural=True,
        )
    )
    return [axis for axis in axes if axis.values]


def with_override(cfg, path: str, value) -> config.ScoringConfig:
    """A copy of the scoring config with one leaf replaced.

    Round-tripped through the loader so every cross-field invariant re-runs: a
    sweep value that contradicts another parameter is REJECTED rather than
    silently tested. `max_squeeze_percentile >= 1` is the live example — the
    loader refuses it, because a percentile cap at 1 admits every observation
    and is not a squeeze.
    """
    data = cfg.model_dump()
    node = data
    parts = path.split(".")
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value
    return config.ScoringConfig.model_validate(data)


def param_sets_for(axes: list[Axis], *, cfg=None) -> list[ParamSet]:
    """The baseline plus one set per candidate value. ONE AXIS AT A TIME."""
    scoring = cfg or config.scoring()
    sets = [ParamSet.baseline(scoring)]
    for axis in axes:
        for value in axis.values:
            try:
                altered = with_override(scoring, axis.path, value)
            except Exception as exc:
                # A rejected combination is a finding, not a crash: the loader
                # just said the two parameters contradict.
                log.warning("sweep value %s=%s rejected by the loader: %s", axis.name, value, exc)
                continue
            sets.append(ParamSet(name=f"{axis.name}={value:g}", cfg=altered))
    return sets


def baseline_with(overrides: list[str] | None, *, cfg=None) -> config.ScoringConfig:
    """The shipped config plus `PATH=VALUE` overrides applied to EVERY arm.

    This is how a DORMANT recipe gets swept. `base_breakout` ships
    `enabled: false` (O13), and a sweep off that baseline would walk twenty
    configurations that all fire nothing — while flipping the shipped file to
    run the sweep would mean the evidence and the tree disagree about what was
    tested. The override is applied to the baseline and to every arm, and each
    arm's `config_hash` then describes the configuration that actually ran.
    """
    scoring = cfg or config.scoring()
    for item in overrides or []:
        path, _, raw = item.partition("=")
        if not raw:
            raise SystemExit(f"--with needs PATH=VALUE, got {item!r}")
        from backtest.engine import _coerce

        scoring = with_override(scoring, path.strip(), _coerce(raw.strip()))
    return scoring


def run(
    *,
    symbols: list[str] | None = None,
    sample: int | None = 60,
    years: int = 3,
    start: date | None = None,
    end: date | None = None,
    workers: int = 1,
    as_of: date | None = None,
    distributions: Distributions | None = None,
    allow_dirty: bool = False,
    overrides: list[str] | None = None,
) -> tuple[Distributions, dict[str, dict[str, Expectancy]], list[Axis]]:
    """Measure, build the axes, walk every parameter set, group the results.

    `start`/`end` pin the window. Under D50 a sweep used for SELECTION must
    pin it, and `render` refuses to print a chosen value when it was not.
    """
    today = as_of or clock.session_date()
    universe = symbols if symbols is not None else db.active_symbols()
    if sample:
        universe = universe[:sample]

    baseline = baseline_with(overrides)
    # The distributions are measured over the SAME window the walk uses, so the
    # percentiles that place the candidates describe the data being judged.
    # Over the WALK'S window, not back from today: the percentiles that place
    # the candidates must describe the data being judged, and a window reaching
    # past `end` would place them using data the walk cannot see.
    measure_years = years if start is None else max(1, round(((end or today) - start).days / 365))
    measured = distributions or measure(
        symbols=universe, as_of=end or today, years=measure_years, sample=None
    )
    axes = axes_from(measured, cfg=baseline)
    sets = param_sets_for(axes, cfg=baseline)
    log.info("sweeping %d configuration(s) over %d symbol(s)", len(sets), len(universe))

    result = run_engine(
        symbols=universe,
        years=years,
        start=start,
        end=end,
        param_sets=sets,
        as_of=today,
        workers=workers,
        allow_dirty=allow_dirty,
    )

    by_set: dict[str, dict[str, Expectancy]] = {}
    for name in result.param_sets:
        trades = [t for t in result.trades if t.param_set == name]
        by_set[name] = summarise(trades, key=lambda t: t.recipe)
    return measured, by_set, axes


def selection(
    axes: list[Axis],
    by_set: dict[str, dict[str, Expectancy]],
) -> dict[str, Plateau]:
    """D50's rule applied to every axis in the table. {axis name: Plateau}.

    The recipe an axis belongs to is read off the parameter path, so the rule
    is applied to the population the gate actually admits — judging a
    `base_breakout` gate by the drift recipe's trades would be meaningless and
    is the kind of mistake a hand-read table invites.
    """
    out: dict[str, Plateau] = {}
    for axis in axes:
        recipe = axis.path.split(".")[1]
        points = []
        for value in axis.values:
            arm = by_set.get(f"{axis.name}={value:g}", {})
            item = arm.get(recipe)
            points.append(
                Point(
                    value=value,
                    expectancy_r=item.expectancy_r if item else None,
                    closed=item.closed if item else 0,
                )
            )
        out[axis.name] = plateau_choice(points)
    return out


def render(
    measured: Distributions,
    by_set: dict[str, dict[str, Expectancy]],
    *,
    overrides: list[str] | None = None,
    axes: list[Axis] | None = None,
    window: tuple[date | None, date | None] | None = None,
) -> str:
    lines = ["=" * 100, "D43 EXISTENCE-GATE SWEEP", "=" * 100, ""]
    if overrides:
        # Printed, because every arm below ran with these on and a reader who
        # cannot see them would take the table for the shipped configuration.
        lines.append(f"APPLIED TO EVERY ARM: {', '.join(overrides)}")
        lines.append("")
    pinned = bool(window and window[0] and window[1])
    if pinned:
        lines.append(f"SELECTION WINDOW: {window[0]} .. {window[1]} (D50: in-sample)")
        lines.append("")
    lines.append("MEASURED DISTRIBUTIONS (these set the sweep range, not taste)")
    lines.append(str(measured))
    lines.append("")
    lines.append(f"{'configuration':<40} {'recipe':<22} {'sig':>5} {'clsd':>5} {'expR':>7}")
    lines.append("-" * 100)
    for name, recipes in by_set.items():
        if not recipes:
            lines.append(f"{name:<40} {'(nothing fired)':<22}")
            continue
        for recipe, item in recipes.items():
            value = item.expectancy_r
            lines.append(
                f"{name:<40} {recipe:<22} {item.signals:>5} {item.closed:>5} "
                f"{'-' if value is None else f'{value:+.2f}':>7}"
                + ("" if item.sufficient else "  (n<30)")
            )
    lines.append("")
    lines.append("D50 SELECTION (centre of the widest positive plateau, >=30 closed each)")
    lines.append("-" * 100)
    if not pinned:
        lines.append(
            "  NOT A SELECTION RUN: no explicit --start/--end, so no value may be picked "
            "from this table."
        )
        lines.append(
            "  A window nobody pinned is a window somebody will pick from after seeing "
            "the answer (D50)."
        )
    elif not axes:
        lines.append("  no axes")
    else:
        for name, plateau in selection(axes, by_set).items():
            lines.append(f"  {name:<28} {plateau}")
        lines.append("")
        lines.append(
            "  A chosen value is a PROPOSAL. D50 certifies it on the LATER window, "
            "with the adoption criteria applied there alone."
        )
    lines.append("")
    lines.append(
        "One axis at a time from the baseline. A row that fires MORE and keeps its "
        "expectancy is a gate that was too tight;"
    )
    lines.append(
        "a row that fires more and loses it is a gate doing its job. Neither is a "
        "decision — D43 sweeps to propose, D39 to accept."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--sample", type=int, default=60, help="symbols to sweep")
    parser.add_argument("--years", type=int, default=3)
    parser.add_argument("--start", help="ISO start date; D50 REQUIRES it for a selection run")
    parser.add_argument("--end", help="ISO end date")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="sweep with uncommitted changes; stamps every output NON-BASELINE",
    )
    parser.add_argument(
        "--measure-only", action="store_true", help="print the distributions and stop"
    )
    parser.add_argument(
        "--with",
        dest="overrides",
        action="append",
        metavar="PATH=VALUE",
        help="apply to EVERY arm, e.g. recipes.base_breakout.enabled=true — the only "
        "way to sweep a dormant recipe without editing the shipped config",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.measure_only:
        measured = measure(symbols=args.symbols, years=args.years, sample=args.sample)
        print(measured)
        print()
        for axis in axes_from(measured, cfg=baseline_with(args.overrides)):
            kind = "structural" if axis.structural else "gate-only"
            print(f"{axis.name:<28} {list(axis.values)}  [{kind}] — {axis.rationale}")
        return 0

    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    measured, by_set, axes = run(
        symbols=args.symbols,
        sample=args.sample,
        years=args.years,
        start=start,
        end=end,
        workers=args.workers,
        allow_dirty=args.allow_dirty,
        overrides=args.overrides,
    )
    print(
        render(
            measured,
            by_set,
            overrides=args.overrides,
            axes=axes,
            window=(start, end),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
