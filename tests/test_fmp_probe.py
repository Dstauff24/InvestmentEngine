"""The probe must cover EVERY endpoint the client calls.

This is the test for a specific past failure: an earlier probe sampled one
fundamentals endpoint, saw it pass, and reported "fundamentals work" while
`ratios` and `key-metrics` were both separately 402. FMP gates endpoints
individually, so partial coverage produces a confidently wrong verdict.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from scripts.fmp_probe import KNOWN_PLAN_GATED, PROBES

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT = REPO_ROOT / "ingest" / "fmp_client.py"

STABLE_PATH = re.compile(r'"(/stable/[a-z0-9/-]+)"')


def client_endpoints() -> set[str]:
    """Every `/stable/...` literal in the client's own method bodies."""
    source = CLIENT.read_text()
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("/stable/")
        ):
            found.add(node.value)
    return found


def probe_endpoints() -> set[str]:
    return {stable[0] for _, _, stable in PROBES}


def test_the_probe_covers_every_endpoint_the_client_calls():
    client, probe = client_endpoints(), probe_endpoints()
    missing = client - probe
    assert missing == set(), (
        f"scripts/fmp_probe.py does not probe these endpoints that "
        f"ingest/fmp_client.py calls: {sorted(missing)}. FMP gates endpoints "
        f"individually, so an unprobed endpoint can be 402 while the probe "
        f"reports everything fine."
    )


def test_the_probe_does_not_probe_endpoints_the_client_never_calls():
    """Dead probe rows would make the pass/fail counts misleading."""
    stale = probe_endpoints() - client_endpoints()
    assert stale == set(), f"probe rows for endpoints the client does not call: {sorted(stale)}"


def test_the_probe_checks_both_api_generations():
    for label, legacy, stable in PROBES:
        assert legacy[0].startswith("/api/v3/"), f"{label} has no legacy path"
        assert stable[0].startswith("/stable/"), f"{label} has no stable path"


def test_every_probe_row_has_a_distinct_label():
    labels = [label for label, _, _ in PROBES]
    assert len(labels) == len(set(labels))


def test_all_five_d29_gated_endpoints_are_probed():
    """D29 names exactly these as 402 on the free tier."""
    gated = set(KNOWN_PLAN_GATED)
    labels = {label for label, _, _ in PROBES}
    assert gated == {
        "sp500 constituents",
        "nasdaq constituents",
        "analyst estimates",
        "ratios",
        "key metrics",
    }
    assert gated.issubset(labels)


def test_the_fundamentals_statements_are_probed_separately():
    """The exact gap that produced the wrong verdict: ratios and key-metrics are
    gated independently of the three core statements."""
    probed = probe_endpoints()
    for path in (
        "/stable/income-statement",
        "/stable/balance-sheet-statement",
        "/stable/cash-flow-statement",
        "/stable/ratios",
        "/stable/key-metrics",
    ):
        assert path in probed


def test_402_and_404_are_classified_differently():
    source = (REPO_ROOT / "scripts" / "fmp_probe.py").read_text()
    assert "402" in source and "404" in source
    assert "plan does not cover it" in source
    assert "not on this API generation" in source
