""".env against .env.example (the 2026-10-22 VM failure).

A setting added in code reached production without its value: the VM ran a
whole evening with SHADOW_ACCOUNT_VALUE absent, `scoring_pass` died, and the
digest reported a quiet market. `.env.example` is now a contract the deploy
checks, and these pin what "satisfied" means — especially the case that makes
the check worth having, where a value exists in an interactive shell and not
in the file cron reads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import env_contract


@pytest.fixture
def contract(tmp_path, monkeypatch):
    """Point the checker at throwaway files, with a CONTROLLED ambient env.

    The ambient environment is part of what the checker reads, so each case
    states it: every variable in the example is cleared first, then `ambient`
    puts back exactly what that test means to be in the shell. Without this the
    full suite's own environment leaked in and two cases passed alone and
    failed together — which is the same class of bug as the thing being tested.
    """

    def write(example: str, actual: str, ambient: dict[str, str] | None = None):
        (tmp_path / ".env.example").write_text(example)
        (tmp_path / ".env").write_text(actual)
        monkeypatch.setattr(env_contract, "EXAMPLE", tmp_path / ".env.example")
        monkeypatch.setattr(env_contract, "ACTUAL", tmp_path / ".env")
        for name in env_contract.parse(tmp_path / ".env.example"):
            monkeypatch.delenv(name, raising=False)
        for name, value in (ambient or {}).items():
            monkeypatch.setenv(name, value)
        return env_contract.check()

    return write


def test_a_filled_env_satisfies_the_contract(contract):
    failures, notes = contract("FOO=CHANGE_ME\n", "FOO=real-value\n")
    assert failures == []
    assert "1 variable(s)" in notes[0]


def test_a_missing_required_variable_fails(contract):
    """THE VM BUG. The variable existed in code and in the example, and the
    machine that ran the pipeline did not have it."""
    failures, _ = contract("SHADOW_ACCOUNT_VALUE=100000\n", "PGHOST=localhost\n")
    assert failures == ["SHADOW_ACCOUNT_VALUE: missing in .env"]


def test_a_blank_required_variable_fails(contract):
    failures, _ = contract("FOO=x\n", "FOO=\n")
    assert failures == ["FOO: blank in .env"]


def test_an_optional_variable_may_be_blank_or_absent(contract):
    example = (
        "TELEGRAM_BOT_TOKEN=        # [OPTIONAL] blank runs on email alone\n"
        "TELEGRAM_CHAT_ID=        # [OPTIONAL] same\n"
    )
    failures, notes = contract(example, "TELEGRAM_BOT_TOKEN=\n")
    assert failures == []
    assert "2 marked [OPTIONAL]" in notes[0]


def test_a_placeholder_fails_even_when_the_variable_is_optional(contract):
    """`# [OPTIONAL]` exempts a variable from being SET, never from carrying a
    placeholder: `core/settings.py` refuses a placeholder at read time, which
    turns a designed degrade into a hard failure."""
    example = 'SEC_EDGAR_USER_AGENT="Engine/0.1 (you@example.com)"   # [OPTIONAL] EDGAR skips\n'
    failures, _ = contract(example, 'SEC_EDGAR_USER_AGENT="Engine/0.1 (you@example.com)"\n')
    assert failures == ["SEC_EDGAR_USER_AGENT: still a placeholder in .env"]


def test_a_value_only_in_the_shell_fails_and_says_cron_will_not_see_it(contract):
    """THE TRAP THIS CHECK EXISTS FOR. `core/settings.py` loads .env with
    override=False, so a real environment variable wins — and cron runs with
    almost none of them. A value that lives only in an interactive shell passes
    every check a human runs and is gone at 16:30."""
    failures, _ = contract(
        "FMP_API_KEY=CHANGE_ME\n",
        "FMP_API_KEY=CHANGE_ME\n",
        ambient={"FMP_API_KEY": "a-real-key"},
    )
    assert len(failures) == 1
    assert "CRON WILL NOT SEE IT" in failures[0]


def test_the_same_gap_without_a_shell_value_says_nothing_about_cron(contract):
    """The suffix is a diagnosis, not decoration: it must only appear when the
    shell really is the only thing holding the variable up."""
    failures, _ = contract("FMP_API_KEY=CHANGE_ME\n", "FMP_API_KEY=CHANGE_ME\n")
    assert failures == ["FMP_API_KEY: still a placeholder in .env"]


def test_a_sensible_default_may_be_inherited_unchanged(contract):
    """`PGHOST=localhost` copied from the example is fine; a secret copied from
    the example is not. The difference is whether the example's own value looks
    like a placeholder."""
    failures, _ = contract("PGHOST=localhost\n", "PGHOST=localhost\n")
    assert failures == []


def test_comments_and_blank_lines_are_not_variables(contract):
    failures, notes = contract("# FOO=commented\n\n   \nBAR=1\n", "BAR=2\n")
    assert failures == []
    assert "1 variable(s)" in notes[0]


def test_an_inline_comment_is_not_part_of_the_value(contract):
    failures, _ = contract("FOO=x\n", "FOO=real   # my note\n")
    assert failures == []


def test_a_missing_env_file_reports_every_required_variable(tmp_path, monkeypatch):
    (tmp_path / ".env.example").write_text("FOO=x\nBAR=y\n")
    monkeypatch.setattr(env_contract, "EXAMPLE", tmp_path / ".env.example")
    monkeypatch.setattr(env_contract, "ACTUAL", tmp_path / "nonexistent")
    for name in ("FOO", "BAR"):
        monkeypatch.delenv(name, raising=False)
    failures, _ = env_contract.check()
    assert sorted(failures) == ["BAR: missing in .env", "FOO: missing in .env"]


# ── the contract covers what the code actually reads ───────────────────────


def test_every_variable_settings_reads_is_in_the_example():
    """The other end of the loop. The deploy can only check what the example
    names, so a setting added in code must be added there too — which is the
    step that was skipped on 2026-10-22."""
    import ast

    root = Path(__file__).resolve().parent.parent
    source = (root / "core" / "settings.py").read_text()
    read: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("get", "getenv")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and env_contract.NAME.fullmatch(node.args[0].value)
        ):
            read.add(node.args[0].value)
        # `_require("NAME")` and friends take the name as a bare string.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and env_contract.NAME.fullmatch(node.args[0].value)
        ):
            read.add(node.args[0].value)

    documented = set(env_contract.parse(root / ".env.example"))
    missing = read - documented
    assert missing == set(), (
        f"core/settings.py reads these and .env.example does not document them: "
        f"{sorted(missing)} — the deploy check can only guard what the example names"
    )
