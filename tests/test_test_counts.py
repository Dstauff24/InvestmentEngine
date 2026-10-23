"""The status line is measured, and refuses to exist otherwise.

The commit-message line "N unit + M integration tests pass" was typed from
memory twice and wrong both times. `scripts/test_counts.py` produces it from
the runs themselves; these pin the two properties that make it worth trusting —
it reports what the commands printed, and it emits NOTHING when anything fails.
"""

from __future__ import annotations

import pytest

from scripts import test_counts

UNIT_OUTPUT = "....\n1310 passed, 3 warnings in 41.52s\n"
INTEGRATION_OUTPUT = "....\n187 passed, 1 warning in 88.01s\n"
RUFF_OUTPUT = "[DEBUG] Checked 138 files in: 2.0ms\nAll checks passed!\n"


def _fake_run(results: dict[str, tuple[int, str]]):
    """Stand in for the subprocess, keyed on a word from the command."""

    def run(argv, env_extra=None):
        for key, result in results.items():
            if key in argv:
                return result
        raise AssertionError(f"unexpected command: {argv}")

    return run


def test_the_line_reports_what_the_commands_printed(monkeypatch):
    monkeypatch.setattr(
        test_counts,
        "_run",
        _fake_run(
            {
                "integration": (0, INTEGRATION_OUTPUT),
                "check": (0, RUFF_OUTPUT),
                "pytest": (0, UNIT_OUTPUT),
            }
        ),
    )
    assert (
        test_counts.measure(integration=True)
        == "1,310 unit + 187 integration tests pass; ruff clean on 138 files."
    )


def test_a_failing_suite_emits_no_line_at_all(monkeypatch):
    """A message claiming "1,310 pass" above a commit where they do not is
    worse than no line: it reads as verification."""
    monkeypatch.setattr(
        test_counts,
        "_run",
        _fake_run({"pytest": (1, "....F\n1 failed, 1309 passed in 40.0s\n")}),
    )
    with pytest.raises(SystemExit) as raised:
        test_counts.measure(integration=True)
    assert "unit tests FAILED" in str(raised.value)
    assert "1,309" not in str(raised.value)


def test_a_failing_lint_emits_no_line_either(monkeypatch):
    monkeypatch.setattr(
        test_counts,
        "_run",
        _fake_run(
            {
                "integration": (0, INTEGRATION_OUTPUT),
                "check": (1, "core/db.py:1:1: F401 unused import\nFound 1 error.\n"),
                "pytest": (0, UNIT_OUTPUT),
            }
        ),
    )
    with pytest.raises(SystemExit) as raised:
        test_counts.measure(integration=True)
    assert "ruff FAILED" in str(raised.value)


def test_an_unreadable_summary_refuses_rather_than_reporting_zero(monkeypatch):
    """A pytest upgrade, a plugin, or a crash before the summary all leave no
    count. Reporting 0 would print it as though it were a measurement."""
    monkeypatch.setattr(
        test_counts,
        "_run",
        _fake_run({"pytest": (0, "no summary line here\n"), "check": (0, RUFF_OUTPUT)}),
    )
    with pytest.raises(SystemExit) as raised:
        test_counts.measure(integration=False)
    assert "cannot read the unit test count" in str(raised.value)


def test_skipping_integration_says_so_in_the_line(monkeypatch):
    """ "Not measured" and "none" are different claims — the standing
    convention from `features/`, applied to our own reporting."""
    monkeypatch.setattr(
        test_counts,
        "_run",
        _fake_run({"pytest": (0, UNIT_OUTPUT), "check": (0, RUFF_OUTPUT)}),
    )
    line = test_counts.measure(integration=False)
    assert line == "1,310 unit + integration not run tests pass; ruff clean on 138 files."


def test_the_integration_suite_runs_against_the_scratch_database(monkeypatch):
    """`ie_test` truncates freely; the dev database does not. The helper must
    not be the one thing that runs the DB suite against real data."""
    seen: list[dict[str, str] | None] = []

    def run(argv, env_extra=None):
        seen.append(env_extra if "integration" in argv else None)
        if "check" in argv:
            return 0, RUFF_OUTPUT
        return 0, INTEGRATION_OUTPUT if "integration" in argv else UNIT_OUTPUT

    monkeypatch.setattr(test_counts, "_run", run)
    test_counts.measure(integration=True)
    assert {"PGDATABASE": "ie_test"} in seen


def test_the_line_goes_above_a_trailer_block():
    """Git reads trailers only from the LAST paragraph, so appending the line
    at the very end would demote `Co-Authored-By:` to prose — the message would
    still look right and the attribution would be gone."""
    message = "Subject\n\nA paragraph.\n\nCo-Authored-By: Someone <x@y>\n"
    assert test_counts.with_line(message, "1,310 unit ... pass.") == (
        "Subject\n\nA paragraph.\n\n1,310 unit ... pass.\n\nCo-Authored-By: Someone <x@y>\n"
    )


def test_a_message_without_trailers_takes_the_line_at_the_end():
    assert test_counts.with_line("Subject\n\nBody.\n", "LINE") == "Subject\n\nBody.\n\nLINE\n"


def test_an_empty_message_is_just_the_line():
    assert test_counts.with_line("", "LINE") == "LINE\n"
