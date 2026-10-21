"""D43: sweep the thresholds that decide whether a recipe fires at all.

    python -m backtest.sweep --measure-only
    python -m backtest.sweep --sample 60 --years 3 --workers 2

**WHY THESE PARAMETERS CANNOT BE TUNED THE NORMAL WAY.** D39(2) moves a number
only on 30 closed Shadow Book ideas. An EXISTENCE GATE set too tight produces
no ideas at all, so it can never accrue the evidence that would loosen it —
unsatisfiable by construction. `base_breakout` is the live proof: its
`range_contraction_ratio` of 0.6 admitted nothing in 433 symbols, so after a
month of running it would have exactly as much evidence as on day one.

**THE LIVE DISTRIBUTION SETS THE RANGE, NOT TASTE.** The sweep first MEASURES
the gated quantity across the universe — what contraction ratios, base depths,
RSI readings and gap sizes actually occur — and places the candidate values at
percentiles of that distribution. A sweep over invented values would test
whether the engine likes round numbers.

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

__all__ = ["Axis", "Distributions", "axes_from", "measure", "param_sets_for", "run"]

# Every quantity an existence gate reads, with the parameter that gates it.
GATED = {
    "base_contraction": "recipes.base_breakout.range_contraction_ratio",
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
            out.add("base_contraction", features.structure.base_contraction)
            out.add("base_depth_pct", features.structure.base_depth_pct)
            out.add("base_weeks", features.structure.base_weeks)
            out.add("gap_up_pct", features.structure.gap_up_pct)
        log.info("measured %s (%d session(s) so far)", symbol, out.observations)
    return out


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

    contraction = at("base_contraction", 0.10, 0.25, 0.50)
    if contraction:
        axes.append(
            Axis(
                path=GATED["base_contraction"],
                values=contraction,
                rationale=(
                    f"shipped {scoring.recipes.base_breakout.range_contraction_ratio:g} vs "
                    f"measured p10 {contraction[0]:g} — the gate sits below the data"
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
    silently tested. `range_contraction_ratio >= 1` is the live example — the
    loader refuses it, because at or above 1 the range is expanding and that is
    not a base.
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


def run(
    *,
    symbols: list[str] | None = None,
    sample: int | None = 60,
    years: int = 3,
    workers: int = 1,
    as_of: date | None = None,
    distributions: Distributions | None = None,
) -> tuple[Distributions, dict[str, dict[str, Expectancy]]]:
    """Measure, build the axes, walk every parameter set, group the results."""
    today = as_of or clock.session_date()
    universe = symbols if symbols is not None else db.active_symbols()
    if sample:
        universe = universe[:sample]

    measured = distributions or measure(symbols=universe, as_of=today, years=years, sample=None)
    axes = axes_from(measured)
    sets = param_sets_for(axes)
    log.info("sweeping %d configuration(s) over %d symbol(s)", len(sets), len(universe))

    result = run_engine(
        symbols=universe,
        years=years,
        param_sets=sets,
        as_of=today,
        workers=workers,
    )

    by_set: dict[str, dict[str, Expectancy]] = {}
    for name in result.param_sets:
        trades = [t for t in result.trades if t.param_set == name]
        by_set[name] = summarise(trades, key=lambda t: t.recipe)
    return measured, by_set


def render(measured: Distributions, by_set: dict[str, dict[str, Expectancy]]) -> str:
    lines = ["=" * 100, "D43 EXISTENCE-GATE SWEEP", "=" * 100, ""]
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
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument(
        "--measure-only", action="store_true", help="print the distributions and stop"
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
        for axis in axes_from(measured):
            kind = "structural" if axis.structural else "gate-only"
            print(f"{axis.name:<28} {list(axis.values)}  [{kind}] — {axis.rationale}")
        return 0

    measured, by_set = run(
        symbols=args.symbols, sample=args.sample, years=args.years, workers=args.workers
    )
    print(render(measured, by_set))
    return 0


if __name__ == "__main__":
    sys.exit(main())
