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

**THE RULES.** Every variable in `.env.example` must be set in `.env` and must
not still carry a placeholder. `# [OPTIONAL]` on the example's line exempts it
from having to be SET, with the reason written beside it — it never exempts it
from the placeholder check, because `core/settings.py` REFUSES a placeholder at
read time and that turns a designed degrade into a hard failure.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / ".env.example"
ACTUAL = REPO_ROOT / ".env"

# Values that mean "nobody filled this in". `.env.example`'s own values are
# compared too, so a variable left exactly as shipped is caught even when its
# placeholder is not in this list.
PLACEHOLDERS = ("change_me", "replace_me", "you@example.com", "engine@example.com", "changeme")
NAME = re.compile(r"[A-Z][A-Z0-9_]*")


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
    optional_count = 0

    for name, (example_value, example_line) in sorted(wanted.items()):
        optional = "[OPTIONAL]" in example_line
        optional_count += optional
        value, present = (have[name][0], True) if name in have else ("", False)
        ambient = os.environ.get(name, "")

        if looks_like_a_placeholder(value, example_value):
            detail = f"{name}: still a placeholder in .env"
            if ambient and not looks_like_a_placeholder(ambient, example_value):
                detail += " (your shell has a real value; CRON WILL NOT SEE IT)"
            failures.append(detail)
            continue

        if not value:
            if optional:
                continue
            detail = f"{name}: {'blank' if present else 'missing'} in .env"
            if ambient:
                detail += " (your shell has a value; CRON WILL NOT SEE IT)"
            failures.append(detail)

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
            "\n`.env` is what cron reads. Fill these in, or mark a variable "
            "`# [OPTIONAL]` in .env.example with the reason it may be blank."
        )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
