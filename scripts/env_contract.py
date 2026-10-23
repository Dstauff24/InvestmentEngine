#!/usr/bin/env python3
"""Check `.env` against the contract in `.env.example`.

    python scripts/env_contract.py          # exit 1 if the contract is broken

**WHY THE FILE AND NOT THE ENVIRONMENT.** `core/settings.py` loads `.env` with
`override=False`, so a real environment variable wins over it — and cron runs
with almost none of them. A value that lives only in an interactive shell
passes every check a human runs and is missing at 16:30. So this reads the
FILE, and when the ambient environment is the only thing holding a variable up
it says so instead of passing.

It exists because the gap was not hypothetical: on 2026-10-22 the VM ran a
whole evening with `SHADOW_ACCOUNT_VALUE` absent, `scoring_pass` died, and the
digest reported a quiet market.

**THE REQUIREDNESS RULE** is stated in full in `.env.example`'s header, which
is the canonical copy — three restatements would drift. In one line: a variable
is REQUIRED only if the engine cannot run its required pipeline without it, so
in practice only if `core/settings.py` reads it through `require()`, and
`tests/test_env_contract.py` enforces that direction. The rest carry
`# [OPTIONAL: <feature>] <reason>`, which exempts them from being set AND from
the placeholder check, because for something optional those are the same state
and every reader already treats a placeholder as absent (`alerts/notify.py`
asks the real accessors, each of which refuses one). A placeholder in a
REQUIRED variable stays a deploy failure — that is a value somebody meant to
fill in and did not.

**THE FEATURE TAG IS NOT DECORATION.** This report groups by it, because the
question an operator has is never "which variables are blank" but "what is
switched off on this box". `ENGINE_PATH` failed a VM deploy while its two
siblings were already marked: nothing tied the three together, so marking two
of them read as finished work. Optional variables are PRINTED, never silently
passed.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import settings

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / ".env.example"
ACTUAL = REPO_ROOT / ".env"

# ONE DEFINITION of "nobody filled this in", imported from the only module
# that reads os.environ — so the deploy check, `settings.require` and the
# alerting layer cannot disagree about whether a value counts.
PLACEHOLDERS = settings.PLACEHOLDERS
NAME = re.compile(r"[A-Z][A-Z0-9_]*")
# `# [OPTIONAL: email alerts] blank turns the channel off`. The feature name is
# optional in the SYNTAX so an older line still parses, and `tests/
# test_env_contract.py` is what requires every shipped marker to carry one.
OPTIONAL = re.compile(r"\[OPTIONAL(?::\s*([^\]]+?)\s*)?\]")
UNATTRIBUTED = "no feature named — see .env.example"


def parse(path: Path) -> dict[str, tuple[str, str]]:
    """{name: (value, raw line)}. A comment is never a value."""
    out: dict[str, tuple[str, str]] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, rest = stripped.partition("=")
        if not NAME.fullmatch(name):
            continue
        out[name] = (rest.split("#", 1)[0].strip().strip("\"'"), line)
    return out


def looks_like_a_placeholder(value: str, example_value: str) -> bool:
    if not value:
        return False
    lowered = value.lower()
    if any(token in lowered for token in PLACEHOLDERS):
        return True
    # Unchanged from the shipped example AND the example looks like a secret
    # rather than a sensible default: `PGHOST=localhost` is fine to inherit,
    # `SMTP_PASSWORD=<whatever we shipped>` is not.
    return bool(example_value) and value == example_value and _is_secretish(example_value)


def _is_secretish(value: str) -> bool:
    return any(token in value.lower() for token in PLACEHOLDERS)


def check() -> tuple[list[str], list[str]]:
    """(failures, notes). Failures break the deploy; notes are informational."""
    wanted, have = parse(EXAMPLE), parse(ACTUAL)
    failures: list[str] = []
    notes: list[str] = []
    off: dict[str, list[str]] = {}
    optional_count = 0

    for name, (example_value, example_line) in sorted(wanted.items()):
        marker = OPTIONAL.search(example_line)
        optional_count += marker is not None
        value, present = (have[name][0], True) if name in have else ("", False)
        ambient = os.environ.get(name, "")
        placeholder = looks_like_a_placeholder(value, example_value)

        if marker:
            # Blank or placeholder are the SAME state for something optional:
            # the feature is off, or its code-side default is in force.
            # Recorded under the feature so the operator reads what is off.
            if not value or placeholder:
                off.setdefault(marker.group(1) or UNATTRIBUTED, []).append(name)
            continue

        if placeholder:
            detail = f"{name}: still a placeholder in .env"
            if ambient and not looks_like_a_placeholder(ambient, example_value):
                detail += " (your shell has a real value; CRON WILL NOT SEE IT)"
            failures.append(detail)
            continue

        if not value:
            detail = f"{name}: {'blank' if present else 'missing'} in .env"
            if ambient:
                detail += " (your shell has a value; CRON WILL NOT SEE IT)"
            failures.append(detail)

    if off:
        blank = sum(len(names) for names in off.values())
        notes.append(
            f"NOT SET, by design ({blank} optional — the feature is off, or its "
            f"code-side default is in force):"
        )
        notes.extend(
            f"  {feature}: {', '.join(names)}"
            # Case-insensitive, so "EDGAR insiders" sorts next to "email alerts"
            # rather than above every lowercase feature in the list.
            for feature, names in sorted(off.items(), key=lambda item: item[0].lower())
        )
    notes.append(
        f"{len(wanted)} variable(s) in .env.example, {optional_count} marked [OPTIONAL], "
        f"{len(wanted) - len(failures)} satisfied by .env"
    )
    return failures, notes


def main() -> int:
    failures, notes = check()
    for failure in failures:
        print(failure)
    for note in notes:
        print(note)
    if failures:
        print(
            "\n`.env` is what cron reads. Fill these in, or — if the engine can run "
            "its required pipeline without one — mark it `# [OPTIONAL: <feature>]` in "
            ".env.example with the reason it may be blank."
        )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
