"""The gate on an adoption, and on citing the right evidence.

The second half exists because of a real mistake: on 2026-10-22 the criteria
script printed a run's config hashes as a SORTED SET with no mapping back to
the arms, and D48's config comment went out naming the BASELINE arm's hash as
the evidence for adopting the other one. Only checking that the shipped config
matched caught it. These tests pin both the mapping and the check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backtest.provenance import config_hash
from core import config
from scripts import adoption_check

SHIPPED = config_hash(config.scoring())
OTHER = "0000deadbeef"

HEADER = (
    "param_set,commit,config_hash,dirty,symbol,sector,recipe,called_on,regime,"
    "risk_reward,outcome,entry,gross_r,net_r,days_held"
)


def row(
    arm: str,
    *,
    hash_: str,
    net_r: float,
    rr: float,
    commit="242301c",
    dirty="0",
    called_on="2024-03-01",
) -> str:
    return (
        f"{arm},{commit},{hash_},{dirty},AAPL,Tech,post_earnings_drift,{called_on},"
        f"bull_calm,{rr},target_2,100.0,{net_r},{net_r},7"
    )


def write_run(
    tmp_path: Path,
    *,
    arm_hash: str = SHIPPED,
    baseline_r: float = 0.10,
    arm_r: float = 0.50,
    arm_count: int = 40,
    commit: str = "242301c",
    dirty: str = "0",
) -> Path:
    """A run where the arm beats the baseline on every cut by construction."""
    lines = [HEADER]
    for _ in range(40):
        lines.append(row("baseline", hash_=OTHER, net_r=baseline_r, rr=4.0))
    for _ in range(arm_count):
        lines.append(
            row("verified", hash_=arm_hash, net_r=arm_r, rr=4.0, commit=commit, dirty=dirty)
        )
    path = tmp_path / "run.csv"
    path.write_text("\n".join(lines) + "\n")
    return path


def run_check(path: Path, *, arm: str = "verified", extra: list[str] | None = None):
    return adoption_check.main([str(path), "--arm", arm, *(extra or [])])


# ── the mapping ─────────────────────────────────────────────────────────────


def test_every_arm_is_printed_next_to_its_own_hash(tmp_path, capsys):
    """NOT A SORTED SET. The set is what let a citation name the wrong arm."""
    run_check(write_run(tmp_path))
    out = capsys.readouterr().out

    lines = out.splitlines()
    baseline_line = next(line for line in lines if line.startswith("baseline"))
    arm_line = next(line for line in lines if line.startswith("verified"))
    assert OTHER in baseline_line and SHIPPED not in baseline_line
    assert SHIPPED in arm_line and OTHER not in arm_line


def test_the_mapping_includes_the_commit_and_whether_the_arm_was_clean(tmp_path, capsys):
    run_check(write_run(tmp_path))
    out = capsys.readouterr().out
    assert "242301c" in out
    assert "config hash" in out and "commit" in out and "clean" in out


# ── the citation ────────────────────────────────────────────────────────────


def test_a_citation_matching_the_shipped_config_passes(tmp_path, capsys):
    assert run_check(write_run(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "CITATION OK" in out
    assert "the config in the repo IS the config that produced these numbers" in out


def test_citing_the_wrong_arm_fails_loudly_and_names_the_others(tmp_path, capsys):
    """THE MISTAKE THIS EXISTS FOR. Adopting one arm while citing another's
    hash: the check must fail and show the hash that WOULD have matched."""
    path = write_run(tmp_path, arm_hash=OTHER)
    assert run_check(path) == 1
    out = capsys.readouterr().out
    assert "CITATION MISMATCH" in out
    assert "the wrong arm is named" in out
    assert "other arms in this run" in out


def test_an_unflipped_config_fails_the_citation_too(tmp_path, capsys):
    """Run before switching the config and the shipped hash is still the
    baseline's — the same failure, and the message covers both readings."""
    path = write_run(tmp_path, arm_hash="aaaa11112222")
    assert run_check(path) == 1
    out = capsys.readouterr().out
    assert "has not been made yet" in out


# ── the criteria ────────────────────────────────────────────────────────────


def test_an_arm_that_loses_a_cut_is_not_adopted(tmp_path, capsys):
    assert run_check(write_run(tmp_path, baseline_r=0.80, arm_r=0.20)) == 1
    out = capsys.readouterr().out
    assert "CRITERIA: DO NOT ADOPT" in out


def test_a_sample_under_the_floor_is_not_adopted(tmp_path, capsys):
    """D39(2)'s floor applies to the Tier B cut, and a tiny sample at a
    flattering expectancy is exactly what it exists to refuse."""
    assert run_check(write_run(tmp_path, arm_count=9)) == 1
    out = capsys.readouterr().out
    assert "CRITERIA: DO NOT ADOPT" in out
    assert "(9)" in out


def test_a_positive_but_tiny_edge_still_needs_the_sample(tmp_path, capsys):
    run_check(write_run(tmp_path, arm_count=29))
    out = capsys.readouterr().out
    assert "closed : False (29)" in out.replace("  ", " ")


# ── the artifact itself ─────────────────────────────────────────────────────


def test_a_run_from_a_dirty_tree_may_not_be_cited(tmp_path, capsys):
    assert run_check(write_run(tmp_path, dirty="1")) == 1
    out = capsys.readouterr().out
    assert "NOT CERTIFYING" in out
    assert "dirty tree" in out
    assert "nothing above may be cited" in out


def test_rows_spanning_two_commits_are_refused_as_mixed_code(tmp_path, capsys):
    path = write_run(tmp_path)
    with path.open("a") as fh:
        fh.write(row("verified", hash_=SHIPPED, net_r=0.5, rr=4.0, commit="deadbee") + "\n")
    assert run_check(path) == 1
    assert "MIXED CODE" in capsys.readouterr().out


def test_an_unstamped_run_may_not_be_cited(tmp_path, capsys):
    """A CSV written before provenance stamping records no commit, which is
    the preview artifact — useful, never evidence."""
    lines = [HEADER]
    for arm, h in (("baseline", OTHER), ("verified", SHIPPED)):
        for _ in range(40):
            lines.append(row(arm, hash_=h, net_r=0.3, rr=4.0, commit="unknown", dirty="1"))
    path = tmp_path / "preview.csv"
    path.write_text("\n".join(lines) + "\n")
    assert run_check(path) == 1
    out = capsys.readouterr().out
    assert "recorded no commit" in out


def test_naming_an_arm_the_file_does_not_have_says_what_it_has(tmp_path, capsys):
    assert run_check(write_run(tmp_path), arm="typo") == 1
    out = capsys.readouterr().out
    assert "no such arm" in out
    assert "'baseline'" in out and "'verified'" in out


# ── D50: the criteria are applied to ONE window ─────────────────────────────


def test_a_window_keeps_only_the_trades_called_inside_it(tmp_path, capsys):
    """D50 selects a value on one window and certifies it on a later one, so a
    certification must be able to EXCLUDE the window the value was picked in."""
    lines = [HEADER]
    # In-sample: the arm looks terrible. Out-of-sample: it wins.
    for _ in range(40):
        lines.append(row("baseline", hash_=OTHER, net_r=0.50, rr=4.0, called_on="2023-03-01"))
        lines.append(row("verified", hash_=SHIPPED, net_r=-0.20, rr=4.0, called_on="2023-03-01"))
        lines.append(row("baseline", hash_=OTHER, net_r=0.10, rr=4.0, called_on="2025-03-01"))
        lines.append(row("verified", hash_=SHIPPED, net_r=0.60, rr=4.0, called_on="2025-03-01"))
    path = tmp_path / "run.csv"
    path.write_text("\n".join(lines) + "\n")

    assert run_check(path, extra=["--since", "2024-10-01", "--until", "2026-10-01"]) == 0
    out = capsys.readouterr().out
    assert "WINDOW (D50): called_on in [2024-10-01, 2026-10-01) — 80 of 160 rows kept" in out
    assert "+0.60R" in out

    # The same file, judged on the earlier window, must FAIL — otherwise the
    # filter is not doing anything.
    assert run_check(path, extra=["--since", "2022-10-01", "--until", "2024-10-01"]) == 1


def test_without_a_window_the_header_says_it_is_not_a_certification(tmp_path, capsys):
    """A run judged over its whole span cannot be a D50 certification, and the
    header says so rather than leaving the reader to notice."""
    run_check(write_run(tmp_path))
    assert "not a D50 certification" in capsys.readouterr().out


def test_a_window_that_keeps_nothing_cannot_adopt(tmp_path, capsys):
    assert run_check(write_run(tmp_path), extra=["--since", "2030-01-01"]) == 1
    assert "0 of 80 rows kept" in capsys.readouterr().out


# ── the absolute floor, for an arm designed against in-sample results ───────


def test_the_floor_is_opt_in_so_it_cannot_rewrite_an_earlier_adoption(tmp_path, capsys):
    """D48's criteria were PRE-REGISTERED. A criterion added afterwards would
    change what an earlier adoption was judged against, so the floor only
    applies when asked for."""
    path = write_run(tmp_path, baseline_r=0.02, arm_r=0.06)
    assert run_check(path) == 0, "beats the baseline on every cut"
    # Not the bare word: pytest's tmp_path contains this test's own name.
    assert "absolute" not in capsys.readouterr().out

    assert run_check(path, extra=["--floor"]) == 1
    out = capsys.readouterr().out
    assert "floor MISSED" in out
    assert "that cut clears the absolute +0.10R floor      : False" in out


def test_the_bare_floor_uses_d40s_promotion_bar():
    """The same number D50's plateau rule uses, read from config rather than
    restated."""
    from core import config

    assert config.scoring().promotion.shadow_to_paper.min_expectancy_r == pytest.approx(0.10)


def test_an_explicit_floor_overrides_the_default(tmp_path, capsys):
    path = write_run(tmp_path, baseline_r=0.02, arm_r=0.06)
    assert run_check(path, extra=["--floor", "0.05"]) == 0
    assert "clears the absolute +0.05R floor      : True" in capsys.readouterr().out


def test_beating_a_weak_baseline_is_not_enough_under_the_floor(tmp_path, capsys):
    """THE CASE THE FLOOR EXISTS FOR. A geometry drawn to fix what v1 did wrong
    will beat v1 — the question is whether what it produces is worth trading at
    all."""
    path = write_run(tmp_path, baseline_r=-0.30, arm_r=0.04)
    assert run_check(path) == 0, "it does beat the baseline"
    capsys.readouterr()
    assert run_check(path, extra=["--floor"]) == 1
    assert "floor MISSED" in capsys.readouterr().out


# ── the standard-error margin ───────────────────────────────────────────────


def _dispersed_run(tmp_path: Path, *, baseline_mean: float, arm_mean: float, spread: float = 1.0):
    """A run whose R multiples DISPERSE, so a standard error exists.

    `write_run` gives every row the same net R, which makes the sd zero and any
    difference infinitely significant — fine for the other criteria, useless
    for this one.
    """
    lines = [HEADER]
    for i in range(120):
        swing = spread if i % 2 else -spread
        lines.append(row("baseline", hash_=OTHER, net_r=baseline_mean + swing, rr=4.0))
        lines.append(row("verified", hash_=SHIPPED, net_r=arm_mean + swing, rr=4.0))
    path = tmp_path / "run.csv"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_a_difference_inside_one_standard_error_is_not_a_win(tmp_path, capsys):
    """THE POINT OF THE FLAG. Two populations of a few hundred R multiples
    differ by a tenth of an R from sampling alone, so "+0.22R beats +0.16R" is
    not an answer until the spread is on the page beside it."""
    path = _dispersed_run(tmp_path, baseline_mean=0.16, arm_mean=0.22, spread=1.0)
    assert run_check(path) == 0, "it does beat the baseline"
    capsys.readouterr()

    assert run_check(path, extra=["--margin-se"]) == 1
    out = capsys.readouterr().out
    assert "by >1 SE on every cut : False" in out
    assert "1SE" in out


def test_a_difference_well_outside_the_spread_still_wins(tmp_path, capsys):
    path = _dispersed_run(tmp_path, baseline_mean=0.0, arm_mean=1.2, spread=0.2)
    assert run_check(path, extra=["--margin-se"]) == 0
    assert "by >1 SE on every cut : True" in capsys.readouterr().out


def test_the_margin_multiple_is_configurable(tmp_path, capsys):
    # delta +0.40R against a combined SE of 0.13R: clears 1 SE and 2 SE, not 4.
    path = _dispersed_run(tmp_path, baseline_mean=0.0, arm_mean=0.4, spread=1.0)
    assert run_check(path, extra=["--margin-se", "2"]) == 0
    capsys.readouterr()
    assert run_check(path, extra=["--margin-se", "4"]) == 1
    assert "by >4 SE" in capsys.readouterr().out


def test_a_population_too_small_to_have_a_spread_fails_the_margin(tmp_path, capsys):
    """An untestable criterion is a FAILED one, not a waived one: with one
    trade there is no standard error, and "cannot tell" must not read as
    "passed"."""
    lines = [HEADER, row("baseline", hash_=OTHER, net_r=0.1, rr=4.0)]
    lines.append(row("verified", hash_=SHIPPED, net_r=0.9, rr=4.0))
    path = tmp_path / "run.csv"
    path.write_text("\n".join(lines) + "\n")

    assert run_check(path, extra=["--margin-se"]) == 1
    assert "no SE: too few trades" in capsys.readouterr().out


def test_the_spread_of_the_difference_is_the_conservative_one(tmp_path):
    """`sqrt(se_a^2 + se_b^2)` assumes independence the arms do not have — a
    tighter gate's trades are a SUBSET of a looser one's — which makes the bar
    HIGHER than a paired test would. A challenger should have to overcome that
    direction, not benefit from it."""
    from backtest import metrics

    path = _dispersed_run(tmp_path, baseline_mean=0.0, arm_mean=0.3, spread=1.0)
    run = metrics.load_csv(path)
    base = adoption_check.cut(run, "baseline", "post_earnings_drift", None)
    arm = adoption_check.cut(run, "verified", "post_earnings_drift", None)
    combined = adoption_check.margin_se(base, arm)
    assert combined == pytest.approx((base.standard_error_r**2 + arm.standard_error_r**2) ** 0.5)
    assert combined > base.standard_error_r


# ── which cut carries which criterion ───────────────────────────────────────


def test_a_cut_the_criteria_did_not_name_cannot_fail_the_adoption(tmp_path, capsys):
    """APPLYING A STRICTER RULE THAN THE ONE WRITTEN DOWN is the same failure
    as a looser one and harder to notice: it refuses an adoption the
    pre-registered criteria would have accepted."""
    lines = [HEADER]
    # The arm wins the ungated population (+0.55R against +0.30R) and LOSES
    # the gated cuts (+0.40R against +0.60R).
    for _ in range(40):
        lines.append(row("baseline", hash_=OTHER, net_r=0.00, rr=1.0))
        lines.append(row("verified", hash_=SHIPPED, net_r=0.70, rr=1.0))
        lines.append(row("baseline", hash_=OTHER, net_r=0.60, rr=4.0))
        lines.append(row("verified", hash_=SHIPPED, net_r=0.40, rr=4.0))
    path = tmp_path / "run.csv"
    path.write_text("\n".join(lines) + "\n")

    assert run_check(path) == 1, "it loses a cut, so D48's every-cut rule refuses"
    capsys.readouterr()

    assert run_check(path, extra=["--beats-cut", "all", "--sample-cut", "all"]) == 0
    out = capsys.readouterr().out
    assert "on the all plans cut" in out
    assert "(not a beat criterion)" in out, "the cuts it does not name are marked"


def test_the_sample_and_floor_move_to_the_named_cut(tmp_path, capsys):
    """Recipe 1's criteria put them on the R:R >= 2 cut, because that is the
    population the engine would actually trade."""
    lines = [HEADER]
    for _ in range(40):
        lines.append(row("baseline", hash_=OTHER, net_r=0.01, rr=2.5))
        lines.append(row("verified", hash_=SHIPPED, net_r=0.30, rr=2.5))
    path = tmp_path / "run.csv"
    path.write_text("\n".join(lines) + "\n")

    # On the Tier B cut there are no trades at all (every R:R is 2.5), so the
    # sample criterion cannot be met there.
    assert run_check(path, extra=["--beats-cut", "all", "--sample-cut", "tier_b"]) == 1
    capsys.readouterr()
    assert run_check(path, extra=["--beats-cut", "all", "--sample-cut", "tier_a", "--floor"]) == 0
    out = capsys.readouterr().out
    assert "its R:R >= 2 cut has >= 30 closed : True (40)" in out
    assert "floor OK" in out


def test_reachability_is_its_own_criterion(tmp_path, capsys):
    """ "Did it earn more" and "can the engine trade it" are separate
    questions. v1's plans clear 2:1 only 3% of the time, so a variant that
    earns more while remaining unreachable has not fixed anything."""
    lines = [HEADER]
    for _ in range(40):
        # Both arms earn the same; only the arm's plans can clear the gate.
        lines.append(row("baseline", hash_=OTHER, net_r=0.20, rr=1.2))
        lines.append(row("verified", hash_=SHIPPED, net_r=0.30, rr=2.4))
    path = tmp_path / "run.csv"
    path.write_text("\n".join(lines) + "\n")

    assert (
        run_check(path, extra=["--beats-cut", "all", "--sample-cut", "all", "--reachability"]) == 0
    )
    out = capsys.readouterr().out
    assert "baseline 0% -> verified 100%" in out
    assert "its Tier A reachability exceeds the baseline's  : True" in out


def test_an_unreachable_variant_fails_even_when_it_earns_more(tmp_path, capsys):
    lines = [HEADER]
    for _ in range(40):
        lines.append(row("baseline", hash_=OTHER, net_r=0.10, rr=1.2))
        lines.append(row("verified", hash_=SHIPPED, net_r=0.60, rr=1.3))
    path = tmp_path / "run.csv"
    path.write_text("\n".join(lines) + "\n")

    assert (
        run_check(path, extra=["--beats-cut", "all", "--sample-cut", "all", "--reachability"]) == 1
    )
    out = capsys.readouterr().out
    assert "baseline 0% -> verified 0%" in out
    assert "reachability exceeds the baseline's  : False" in out
