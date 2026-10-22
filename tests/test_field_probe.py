"""The field-contract registry, checked statically (no network).

`scripts/field_probe.py` compares live payloads to the fields our parsers
read. That only works if the registry stays honest, so three things are pinned
here:

  * every registered field appears in the recorded fixture, so a fixture cannot
    silently stop representing what the parser needs;
  * every field a registered READER actually reads is registered, so a parser
    cannot start depending on a field the probe does not watch;
  * every endpoint the clients call has a contract at all.

The live half is the script's job and needs D27 consent. This half runs in CI,
which is where the `eps` -> `epsActual` rename should have been caught and was
not.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts.field_probe import CONTRACTS, check_fixtures

ROOT = Path(__file__).resolve().parent.parent


def test_every_registered_field_is_in_its_fixture():
    """A fixture missing a field the parser reads is a test suite that proves
    nothing about that field."""
    drifted = {
        f"{f.contract.provider}{f.contract.endpoint}": list(f.missing)
        for f in check_fixtures()
        if f.missing
    }
    assert drifted == {}, f"fixtures no longer carry fields our parsers read: {drifted}"


def _reader_literals(reader: str) -> set[str]:
    """Every string literal passed to `.get(...)` inside one named function.

    Scoped to the function named in the contract rather than the whole module:
    parsers also read our OWN dict rows, and a module-wide scan would flag
    `event_time` or `known_on` as unregistered provider fields.
    """
    path, _, function = reader.partition(":")
    tree = ast.parse((ROOT / path).read_text())
    target = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == function
        ),
        None,
    )
    assert target is not None, f"{reader} does not exist"

    literals: set[str] = set()
    for node in ast.walk(target):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
        ):
            # ONLY the first argument: `row.get("key", "")` passes a DEFAULT as
            # the second, and collecting it registered "" as a field name.
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                literals.add(first.value)
        # `_first(row, "a", "b")` — a bare call, not an attribute access.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("_first", "_price")
        ):
            for arg in node.args[1:]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    literals.add(arg.value)
    return literals


# Fields a reader looks for that are NOT part of any current contract: previous
# spellings kept as fallbacks, and our own column names where a parser reads a
# dict it built itself. Listed explicitly so the exemption is a decision rather
# than a hole in the check.
TOLERATED = {
    # Previous provider spellings, retained because a rename can be reversed.
    "eps",
    "revenue",
    "time",
    "epsdiluted",
    "fillingDate",
    "calendarYear",
    "estimatedEpsAvg",
    "estimatedEpsLow",
    "estimatedEpsHigh",
    "estimatedRevenueAvg",
    "estimatedRevenueLow",
    "estimatedRevenueHigh",
    "estimatedEbitdaAvg",
    "estimatedNetIncomeAvg",
    "numberAnalystsEstimatedEps",
    "numberAnalystEstimatedRevenue",
    "roic",
    "debtEquityRatio",
    "priceEarningsRatio",
    "priceEarningsToGrowthRatio",
    "enterpriseValueOverEBITDA",
    "companyName",
    "industry",
    "adjDividend",
    "exDate",
    # Our own keys, read from dicts this code built: `merge_statements` is
    # handed {"balance": ..., "cashflow": ..., "ratios": ..., "metrics": ...}.
    "balance",
    "cashflow",
    "ratios",
    "metrics",
    # An ENVELOPE key, not a row field: Schwab wraps bars in {"candles": [...]}.
    "candles",
    "close",
    "open",
    "high",
    "low",
    "symbol",
    "event_time",
    "event_date",
    "event_type",
    "eps_actual",
    "eps_estimate",
    "revenue_actual",
    "revenue_estimate",
}


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda c: f"{c.provider}{c.endpoint}")
def test_every_field_a_reader_reads_is_registered(contract):
    """THE OTHER DIRECTION. The probe can only watch what the registry names,
    so a parser that starts reading a new field must bring a contract with it.
    """
    registered = {
        name
        for c in CONTRACTS
        if c.reader == contract.reader
        for entry in c.required
        for name in ((entry,) if isinstance(entry, str) else entry)
    }
    unregistered = _reader_literals(contract.reader) - registered - TOLERATED
    assert unregistered == set(), (
        f"{contract.reader} reads fields no contract registers: {sorted(unregistered)} — "
        f"add them to scripts/field_probe.py so the live probe watches them"
    )


def test_every_client_endpoint_has_a_contract():
    """An endpoint with no contract is an endpoint whose shape nobody checks."""
    registered = {c.endpoint for c in CONTRACTS}
    source = (ROOT / "ingest" / "fmp_client.py").read_text()
    called = {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("/stable/")
    }
    missing = called - registered
    assert missing == set(), f"endpoints the FMP client calls with no field contract: {missing}"


def test_the_registry_names_a_reader_that_exists_for_every_contract():
    for contract in CONTRACTS:
        path, _, function = contract.reader.partition(":")
        assert (ROOT / path).exists(), f"{contract.endpoint}: no such file {path}"
        assert function, f"{contract.endpoint}: reader must name a function"
