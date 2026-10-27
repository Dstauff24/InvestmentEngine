"""O18 stage 1: run the registered hypotheses and report against one bar.

    python -m lab.run                      # every providable hypothesis
    python -m lab.run --hypothesis rsi_reset
    python -m lab.run --symbols AAPL MSFT  # a smoke run

**THE MULTIPLICITY IS COUNTED OVER EVERY (HYPOTHESIS, HORIZON, BASIS) TRIPLE
ACTUALLY MEASURED.** Three horizons and three bases is nine tests per
hypothesis, so reporting the best of nine against a per-test bar would find
something on noise alone — the exact failure `lab.multiple_testing` exists to
prevent. There is no pre-registered "primary" horizon to fall back on, because
nothing in the decision log fixes one, and inventing one after the fact would
be the same cherry-pick with extra steps. So the screen pays for its own
breadth: Benjamini-Hochberg at q=0.10 over all triples, with the Bonferroni
threshold printed beside it.

**THE WINDOW AND REGIME SPLITS ARE DESCRIPTIVE, NOT ADDITIONAL TESTS.** Two
D50 windows and six regimes would multiply the count by twelve and make the bar
unreachable for anything. They are reported for the same reason D45 prints
thin cells: to show where a result lives. **NOTHING MAY BE CLAIMED SIGNIFICANT
FROM A SPLIT ALONE** — that is what stage 2 and a forward test are for, and the
report says so in its own footer.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import date

from core import clock
from lab import multiple_testing as mt
from lab import providers
from lab.cross_section import Result, measure
from lab.registry import REGISTRY, Hypothesis

log = logging.getLogger(__name__)

BASES = ("raw", "sector", "beta")
"""**EXCESS VERSUS SPY IS NOT A SEPARATE TEST, AND THE FIRST RUN PROVED IT.**
SPY's return is the same number for every symbol on a given day, so subtracting
it shifts the whole cross-section by a constant — and a TOP-MINUS-BOTTOM spread
and a rank correlation are both invariant to a common shift. The run reported
`raw` and `spy` identical to the second decimal on all 12 pairs, because they
are the same statistic. Counting it would have inflated the test count by a
third for no information and made the bar needlessly strict.

It is NOT redundant for absolute bucket returns, which do shift — so
`Result.quintile_means` is still worth reading on a market-excess basis, and
that is a descriptive view rather than a test. `sector` stays: a sector
composite varies BY symbol, so subtracting it changes the ordering.

**`beta` IS THE THIRD BASIS AND IT IS A REAL TEST**, for the same reason:
beta varies by symbol, so `own - beta x market` REORDERS the cross-section
where the flat SPY shift could not. It is what separates a factor from market
exposure — `low_volatility`'s positive raw spread over 2021-2026 is the case
it was added for, since a spread that collapses beta-adjusted was high-beta
leadership in a rising market rather than a volatility premium."""
# D50's two windows. Selection first, then the window no design choice saw.
WINDOWS = {
    "in-sample 2022-10..2024-10": (date(2022, 10, 1), date(2024, 10, 1)),
    "out-of-sample 2024-10..2026-10": (date(2024, 10, 1), date(2026, 10, 1)),
}


def _slice(by_day, start: date, end: date):
    return {day: obs for day, obs in by_day.items() if start <= day < end}


def run(
    hypotheses: Sequence[Hypothesis],
    *,
    as_of: date,
    symbols: Sequence[str] | None = None,
) -> tuple[list[Result], dict[str, list[Result]], dict[str, str]]:
    """(primary results, {window: results}, {day: regime})."""
    providable = [h for h in hypotheses if h.measurable in providers.MEASURABLES]
    absent = [h.name for h in hypotheses if h.measurable not in providers.MEASURABLES]
    if absent:
        log.info("UNMEASURED (no provider; see the registry notes): %s", ", ".join(absent))

    # **THE UNION OF EVERY HYPOTHESIS'S HORIZONS, not the provider default.**
    # The first baseline run reported all six 60- and 120-session cells
    # UNMEASURED with 0 days, and the cause was this call: it used
    # `cross_sections`' default `(5, 10, 20)`, so no forward return existed at
    # the horizons the two long-horizon factors are registered at. The label
    # was honest and the conclusion would not have been — "momentum shows
    # nothing at 120 days" and "nobody computed a 120-day return" are different
    # statements, and the second one looked exactly like the first.
    horizons = sorted({h for hypothesis in providable for h in hypothesis.horizons})
    sections, regimes = providers.cross_sections(
        [h.measurable for h in providable],
        as_of=as_of,
        horizons=horizons,
        symbols=symbols,
    )

    primary: list[Result] = []
    windowed: dict[str, list[Result]] = {name: [] for name in WINDOWS}
    for h in providable:
        by_day = sections[h.measurable]
        for horizon in h.horizons:
            for basis in BASES:
                primary.append(measure(by_day, measurable=h.name, horizon=horizon, basis=basis))
        for label, (start, end) in WINDOWS.items():
            cut = _slice(by_day, start, end)
            windowed[label].append(
                measure(cut, measurable=h.name, horizon=h.horizons[-1], basis="raw")
            )
    return primary, windowed, regimes


def render(
    primary: list[Result],
    windowed: dict[str, list[Result]],
    *,
    unmeasured: Sequence[str] = (),
) -> str:
    """The table, the bar, and what the bar does not cover."""
    p_values = {r.label: mt.two_sided_p(r.t_stat_newey_west or r.t_stat) for r in primary}
    verdicts = {v.name: v for v in mt.adjust(p_values)}
    tests = next(iter(verdicts.values())).tests if verdicts else 0
    bh = next(iter(verdicts.values())).bh_threshold if verdicts else 0.0
    bonf = next(iter(verdicts.values())).bonferroni_threshold if verdicts else mt.ALPHA

    # PROVENANCE, as on any backtest report. A stage-1 screen adopts nothing by
    # itself, but its survivors are what stage 2 and later decisions cite, and
    # "the lab found an IC of 0.03" is not checkable while "on `abc1234`,
    # config `deadbeef`" is. A dirty tree is stamped rather than refused:
    # unlike a backtest this run changes no strategy, so the honest move is to
    # label it, not to block it.
    from backtest.provenance import current as provenance_current

    stamp = provenance_current(allow_dirty=True)

    lines = [
        "=" * 112,
        "O18 STAGE 1 — univariate cross-section, quintile spreads",
        "=" * 112,
        f"code      {stamp.label}" + ("" if stamp.baseline else "   [NON-BASELINE: dirty tree]"),
        "",
        f"{tests} test(s) measured: every (hypothesis, horizon, basis) triple, counted "
        f"because nine per hypothesis",
        "would otherwise let the best of nine pass a per-test bar on noise alone.",
        (
            f"  Benjamini-Hochberg q={mt.FDR_Q:g}  ->  p <= {bh:.5f} survives "
            f"(expected 1 in 10 of the survivors is noise)"
            if bh > 0
            else f"  Benjamini-Hochberg q={mt.FDR_Q:g}  ->  NOTHING QUALIFIED: no p-value "
            f"reached k/m*q at any rank, so the procedure selects none"
        ),
        f"  Bonferroni alpha={mt.ALPHA:g}/{tests}  ->  p <= {bonf:.5f} "
        f"(controls ANY false positive; the bar for spending capital)",
        "",
        f"{'hypothesis / horizon / basis':<52} {'days':>5} {'spread':>8} {'t':>7} "
        f"{'t(NW)':>7} {'IC':>7}  verdict",
        "-" * 112,
    ]
    for r in sorted(primary, key=lambda r: (r.measurable, r.horizon, r.basis)):
        v = verdicts[r.label]
        lines.append(
            f"{r.label[:52]:<52} {r.days:>5} {_num(r.mean_spread):>8} "
            f"{_num(r.t_stat):>7} {_num(r.t_stat_newey_west):>7} "
            f"{_num(r.mean_rank_ic, 3):>7}  {v.verdict}"
        )

    for label, results in windowed.items():
        lines += ["", f"DESCRIPTIVE SPLIT — {label} (longest horizon, raw basis)"]
        lines += [
            f"  {r.measurable[:40]:<40} days {r.days:>4}  spread {_num(r.mean_spread):>8}  "
            f"t(NW) {_num(r.t_stat_newey_west):>7}"
            for r in sorted(results, key=lambda r: r.measurable)
        ]

    if unmeasured:
        from lab.registry import REGISTRY

        lines += [
            "",
            "UNMEASURED, and named rather than dropped (the test count is the bar):",
        ]
        for name in unmeasured:
            h = REGISTRY.get(name)
            shape = f" [{h.shape}]" if h else ""
            why = h.notes.split(".")[0] if h and h.notes else "no provider"
            lines.append(f"  {name}{shape} — {why}")
        lines += [
            "",
            "An `event_study` is a DIFFERENT INSTRUMENT, not a missing provider: it measures",
            "forward returns from a qualifying event rather than across every symbol every day.",
            "A conditional signal — a report, a filing, a revision — is close to invisible in a",
            "cross-section that includes the 400 symbols the event did not happen to.",
        ]

    lines += [
        "",
        "The window splits above are DESCRIPTIVE. Applying the bar to them as well would",
        "multiply the test count by the number of splits and make it unreachable for",
        "anything, so nothing may be called significant from a split alone — that is what",
        "stage 2 and the forward record are for.",
        "Survivorship: the universe is TODAY's constituents, so every delisted loser is",
        "absent and every spread here is an upper bound.",
    ]
    return "\n".join(lines)


def _num(value, places: int = 2) -> str:
    return "—" if value is None else f"{value:+.{places}f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="O18 stage 1 (read-only).")
    parser.add_argument("--hypothesis", action="append", help="name(s); default every one")
    parser.add_argument("--symbols", nargs="*", help="restrict the universe (smoke runs)")
    parser.add_argument("--as-of", help="ISO date; default today's session")
    args = parser.parse_args(argv)

    chosen = (
        [REGISTRY[name] for name in args.hypothesis] if args.hypothesis else list(REGISTRY.values())
    )
    day = date.fromisoformat(args.as_of) if args.as_of else clock.session_date()
    primary, windowed, _ = run(chosen, as_of=day, symbols=args.symbols)
    absent = [h.name for h in chosen if h.measurable not in providers.MEASURABLES]
    print(render(primary, windowed, unmeasured=absent))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
