#!/usr/bin/env python3
"""Gate an adoption on its evidence, and on citing the RIGHT evidence.

    python scripts/adoption_check.py data/backtest_d48_evidence.csv \
        --arm beat_source=verified

    python scripts/adoption_check.py data/backtest_o13_certifying.csv \
        --arm max_squeeze_percentile=0.2 --since 2024-10-01 --until 2026-10-01

Three jobs now; the second exists because of a mistake and the third because of
D50:

  1. **Apply the criteria** to a certifying run: the named arm must beat the
     baseline on all three cuts (all plans, Tier A's R:R gate, Tier B's), and
     its Tier B cut must clear D39(2)'s closed-trade floor with a positive
     expectancy.

  2. **Check the citation.** The shipped scoring config must hash to the SAME
     value as the arm being cited. On 2026-10-22 the earlier version of this
     script printed the run's config hashes as a SORTED SET with no mapping
     back to the arms, and D48's config comment went out naming the baseline
     arm's hash as the evidence. Only verifying that the shipped config matched
     caught it. A citation nobody can check is not a citation, and a sorted set
     is exactly how you end up citing the wrong half of your own experiment.

  3. **Apply them to ONE WINDOW.** D50 selects a value on an in-sample window
     and certifies it on a later, non-overlapping one. `--since`/`--until`
     filter by `called_on`, and the window is printed in the header with the
     row count it kept, so a certification cannot quietly include the window
     the value was picked in.

So every hash is printed next to the arm that produced it, the shipped config
is compared to the named arm by value, loudly, and the window is stated.

Exit code 0 only when the criteria hold AND the citation matches. Reads a CSV
and the config; no network, no database.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest import metrics
from backtest.provenance import UNKNOWN, config_hash
from core import config

BASELINE = "baseline"


def arm_map(run) -> dict[str, dict]:
    """{param set: {config_hash, rows, commits, dirty}} — never a bare set."""
    out: dict[str, dict] = {}
    for trade in run.trades:
        entry = out.setdefault(
            trade.param_set,
            {"config_hash": set(), "rows": 0, "commits": set(), "dirty": set()},
        )
        entry["config_hash"].add(trade.config_hash)
        entry["commits"].add(trade.commit)
        entry["dirty"].add(trade.dirty)
        entry["rows"] += 1
    return out


def certifying_problems(arms: dict[str, dict]) -> list[str]:
    """Why this artifact may not be cited as evidence, if it may not be."""
    problems = []
    commits = {c for a in arms.values() for c in a["commits"]}
    if commits == {UNKNOWN} or not commits:
        problems.append("the run recorded no commit (it predates provenance stamping)")
    elif len(commits) > 1:
        problems.append(f"rows span {len(commits)} commits {sorted(commits)} — MIXED CODE")
    if any(True in a["dirty"] for a in arms.values()):
        problems.append("rows were produced from a dirty tree (NON-BASELINE)")
    for name, arm in arms.items():
        if len(arm["config_hash"]) > 1:
            problems.append(f"arm {name!r} carries {len(arm['config_hash'])} config hashes")
    return problems


def within(trades, *, since: date | None, until: date | None) -> list:
    """Trades CALLED inside the window. Filtered on `called_on`, not on the
    exit: a trade belongs to the window that produced the decision, and
    filtering on the exit would push a trade into the window it was still open
    in and count the same decision in two places."""
    out = trades
    if since:
        out = [t for t in out if t.called_on >= since]
    if until:
        out = [t for t in out if t.called_on < until]
    return out


def cut(run, arm: str, recipe: str, min_rr: float | None, *, since=None, until=None):
    rows = [t for t in run.trades if t.param_set == arm and t.recipe == recipe]
    rows = within(rows, since=since, until=until)
    if min_rr is not None:
        rows = metrics.eligible(rows, min_rr=min_rr)
    return metrics.by_recipe(rows).get(recipe)


def main(argv: list[str] | None = None) -> int:
    gates = config.risk().min_risk_reward
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("csv", help="a CSV written by `backtest.engine --out`")
    parser.add_argument("--arm", required=True, help="the param set being adopted")
    parser.add_argument("--against", default=BASELINE, help=f"comparison arm (default {BASELINE})")
    parser.add_argument("--recipe", default="post_earnings_drift")
    parser.add_argument("--since", help="ISO date; keep trades CALLED on or after it (D50)")
    parser.add_argument("--until", help="ISO date; keep trades CALLED strictly before it")
    args = parser.parse_args(argv)

    since = date.fromisoformat(args.since) if args.since else None
    until = date.fromisoformat(args.until) if args.until else None

    run = metrics.load_csv(args.csv)
    arms = arm_map(run)
    print(f"{args.csv}: {len(run.trades)} rows, {run.symbols} symbols")
    if since or until:
        kept = within(run.trades, since=since, until=until)
        print(
            f"WINDOW (D50): called_on in [{since or '-'}, {until or '-'}) — "
            f"{len(kept)} of {len(run.trades)} rows kept"
        )
    else:
        print("WINDOW: the whole run (no --since/--until; not a D50 certification)")
    print()

    # ── the mapping, explicitly ──────────────────────────────────────────────
    print(f"{'arm (param set)':<34} {'config hash':<14} {'commit':<10} {'rows':>6}  clean")
    print("-" * 78)
    for name, arm in sorted(arms.items()):
        print(
            f"{name:<34} {'/'.join(sorted(arm['config_hash'])):<14} "
            f"{'/'.join(sorted(arm['commits'])):<10} {arm['rows']:>6}  "
            f"{not any(arm['dirty'])}"
        )
    print()

    problems = certifying_problems(arms)
    for problem in problems:
        print(f"!! NOT CERTIFYING: {problem}")
    missing = [a for a in (args.arm, args.against) if a not in arms]
    if missing:
        print(f"!! no such arm in this file: {missing}; it has {sorted(arms)}")
        return 1

    # ── the criteria ─────────────────────────────────────────────────────────
    print(
        f"{'cut':<16} {args.against[:14]:>14} {'closed':>8} {args.arm[:18]:>18} {'closed':>8}"
        f"  better?"
    )
    print("-" * 78)
    criteria_ok = True
    tier_b = None
    for label, rr in (
        ("all plans", None),
        (f"R:R >= {gates.tier_a:g}", gates.tier_a),
        (f"R:R >= {gates.tier_b:g}", gates.tier_b),
    ):
        base = cut(run, args.against, args.recipe, rr, since=since, until=until)
        arm = cut(run, args.arm, args.recipe, rr, since=since, until=until)
        base_r = base.expectancy_r if base else None
        arm_r = arm.expectancy_r if arm else None
        better = arm_r is not None and base_r is not None and arm_r > base_r
        criteria_ok &= better
        if rr == gates.tier_b:
            tier_b = arm
        print(
            f"{label:<16} {_r(base_r):>14} {base.closed if base else 0:>8} "
            f"{_r(arm_r):>18} {arm.closed if arm else 0:>8}  {better}"
        )

    sample_ok = bool(tier_b) and tier_b.closed >= metrics.MIN_SAMPLE
    positive_ok = bool(tier_b) and (tier_b.expectancy_r or 0) > 0
    print()
    print(f"(1) {args.arm} beats {args.against} on all three cuts : {criteria_ok}")
    print(
        f"(2) its R:R >= {gates.tier_b:g} cut has >= {metrics.MIN_SAMPLE} closed "
        f": {sample_ok} ({tier_b.closed if tier_b else 0})"
    )
    print(f"    ...and positive expectancy                      : {positive_ok}")

    # ── the citation ─────────────────────────────────────────────────────────
    cited = sorted(arms[args.arm]["config_hash"])[0]
    shipped = config_hash(config.scoring())
    citation_ok = cited == shipped
    print()
    print(f"cited arm {args.arm!r} config hash : {cited}")
    print(f"shipped scoring config hash        : {shipped}")
    if citation_ok:
        print("CITATION OK — the config in the repo IS the config that produced these numbers.")
    else:
        other = {n: sorted(a["config_hash"])[0] for n, a in arms.items() if n != args.arm}
        print(
            "!! CITATION MISMATCH. The shipped config does not hash to the arm being "
            "cited.\n"
            "   Either the switch has not been made yet, or the wrong arm is named.\n"
            f"   other arms in this run: {other}"
        )

    verdict = criteria_ok and sample_ok and positive_ok
    print()
    print(f"CRITERIA: {'ADOPT' if verdict else 'DO NOT ADOPT'}")
    print(f"CITATION: {'OK' if citation_ok else 'MISMATCH'}")
    if problems:
        print("EVIDENCE: NOT CERTIFYING — nothing above may be cited")
    return 0 if (verdict and citation_ok and not problems) else 1


def _r(value: float | None) -> str:
    return "-" if value is None else f"{value:+.2f}R"


if __name__ == "__main__":
    sys.exit(main())
