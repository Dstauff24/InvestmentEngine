"""The suite's status line, MEASURED rather than remembered.

Commit messages here carry a line like "1,307 unit + 187 integration tests
pass; ruff clean on 137 files". It was typed from memory twice and was wrong
both times (1158/178 against 1163/179, then 1317 against 1307). A number typed
by hand twice is a process, not a slip: the only numbers in a commit message
that mean anything are ones a command produced, so this produces them.

    python scripts/test_counts.py                 # print the line
    python scripts/test_counts.py --append msg.txt   # then: git commit -F msg.txt
    python scripts/test_counts.py --no-integration    # says so IN the line

**IT REFUSES TO EMIT ON FAILURE.** A red suite has no status line — a message
claiming "1,307 pass" above a commit where they do not is worse than no line
at all, because it is evidence that reads as verification. The failing output
is printed instead and the exit code is non-zero, so a helper wired into a
commit flow stops the commit rather than decorating it.

Integration tests need a live Postgres (`PGDATABASE=ie_test`), which not every
machine has. `--no-integration` is therefore allowed, but it CHANGES THE LINE
to "integration not run" instead of quietly dropping the clause: "we did not
measure it" and "there are none" are different claims (the standing convention
in `features/`, applied to our own reporting).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PASSED = re.compile(r"(\d+) passed")
CHECKED = re.compile(r"Checked (\d+) files")


def _run(argv: list[str], env_extra: dict[str, str] | None = None) -> tuple[int, str]:
    env = dict(os.environ)
    env.update(env_extra or {})
    done = subprocess.run(
        argv,
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return done.returncode, done.stdout + done.stderr


def _count(output: str, pattern: re.Pattern[str], what: str) -> int:
    """The number the command printed, or a refusal.

    A regex that does not match means the output did not have the shape this
    script was written against — a pytest upgrade, a plugin, a crash before the
    summary. Returning 0 there would print "0 unit tests pass" as though it
    were a measurement.
    """
    found = pattern.search(output)
    if not found:
        raise SystemExit(
            f"cannot read {what} from the command's output — the summary line is "
            f"not where this script expects it, so there is no number to report.\n"
            f"{output[-2000:]}"
        )
    return int(found.group(1))


def measure(*, integration: bool) -> str:
    """Run everything, then build the line. Raises SystemExit on any failure."""
    steps: list[tuple[str, list[str], dict[str, str]]] = [
        # No `-q` here: pyproject's addopts already passes one, and a SECOND
        # `-q` suppresses the "N passed" summary this script reads.
        ("unit tests", [sys.executable, "-m", "pytest"], {}),
        ("ruff", [sys.executable, "-m", "ruff", "check", ".", "-v"], {}),
    ]
    if integration:
        steps.insert(
            1,
            (
                "integration tests",
                [sys.executable, "-m", "pytest", "-m", "integration"],
                {"PGDATABASE": "ie_test"},
            ),
        )

    outputs: dict[str, str] = {}
    for name, argv, env_extra in steps:
        print(f"running {name}: {' '.join(argv)}", flush=True)
        code, output = _run(argv, env_extra)
        if code != 0:
            print(output[-4000:], file=sys.stderr)
            raise SystemExit(
                f"\n{name} FAILED (exit {code}) — no status line. Fix the suite, "
                f"then run this again; a measured line is the only kind worth having."
            )
        outputs[name] = output

    unit = _count(outputs["unit tests"], PASSED, "the unit test count")
    files = _count(outputs["ruff"], CHECKED, "ruff's file count")
    middle = (
        f"{_count(outputs['integration tests'], PASSED, 'the integration test count')} integration"
        if integration
        else "integration not run"
    )
    return f"{unit:,} unit + {middle} tests pass; ruff clean on {files} files."


TRAILER = re.compile(r"^[A-Za-z][A-Za-z-]*: .+$")


def with_line(message: str, line: str) -> str:
    """Put the line in the body, ABOVE any trailer block.

    Git reads trailers (`Co-Authored-By:`, `Signed-off-by:`) only from the last
    paragraph, so appending at the very end would silently demote them to prose
    — the message would still look right and the attribution would be gone.
    """
    if not message.strip():
        return f"{line}\n"
    paragraphs = message.rstrip("\n").split("\n\n")
    last = paragraphs[-1].splitlines()
    insert_at = len(paragraphs) - 1 if last and all(TRAILER.match(ln) for ln in last) else None
    if insert_at is None:
        paragraphs.append(line)
    else:
        paragraphs.insert(insert_at, line)
    return "\n\n".join(paragraphs) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-integration",
        action="store_true",
        help="skip the DB-backed suite; the line SAYS it was not run",
    )
    parser.add_argument(
        "--append",
        type=Path,
        help="append the line to a commit-message file once everything passes",
    )
    args = parser.parse_args()

    line = measure(integration=not args.no_integration)
    if args.append:
        existing = args.append.read_text() if args.append.exists() else ""
        args.append.write_text(with_line(existing, line))
        print(f"appended to {args.append}:")
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
