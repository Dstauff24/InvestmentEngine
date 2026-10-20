"""Flow selection and secret redaction in scripts/schwab_auth.py.

The live OAuth flow is not testable here (it needs a browser and a real Schwab
login), but the two things that decide whether it can work at all — reading a
port out of the callback URL, and keeping the authorization code off the
terminal — are pure and are tested.

Context: `--server` fails with `RedirectServerExitedError` against the port-less
callback `https://127.0.0.1`. There is no port to bind, so no redirect server
can exist. That must be detected up front rather than discovered as a traceback.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from scripts.schwab_auth import (
    NOISY_LOGGERS,
    _restore_logging,
    _silence_http_logging,
    callback_port,
    redact,
    resolve_flow,
    should_use_manual_flow,
)

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "schwab_auth.py"


# ── port detection ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "port"),
    [
        ("https://127.0.0.1:8182", 8182),
        ("https://127.0.0.1:8182/", 8182),
        ("https://127.0.0.1:8182/callback", 8182),
        ("http://localhost:8080", 8080),
        ("https://example.com:443/cb", 443),
    ],
)
def test_an_explicit_port_is_found(url, port):
    assert callback_port(url) == port


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1",  # the URL that produced RedirectServerExitedError
        "https://127.0.0.1/",
        "https://localhost",
        "https://example.com/callback",
        "http://127.0.0.1",
    ],
)
def test_an_implicit_port_reads_as_absent(url):
    """https implies 443, but nothing is BOUND there — urlparse says None, and
    that is the distinction that matters for choosing a flow."""
    assert callback_port(url) is None


def test_a_malformed_port_degrades_to_absent_rather_than_raising():
    """A typo in .env should fall back to the flow that always works, not crash."""
    assert callback_port("https://127.0.0.1:notaport") is None
    assert should_use_manual_flow("https://127.0.0.1:notaport") is True


@pytest.mark.parametrize("url", ["", "not a url", "://broken"])
def test_junk_input_does_not_raise(url):
    assert callback_port(url) is None


# ── flow selection ──────────────────────────────────────────────────────────


def test_a_portless_callback_selects_manual():
    """The configured case, and what a Codespace or SSH session will have."""
    assert should_use_manual_flow("https://127.0.0.1") is True
    assert resolve_flow("https://127.0.0.1", manual=False, server=False) == "manual"


def test_a_ported_callback_selects_server():
    assert should_use_manual_flow("https://127.0.0.1:8182") is False
    assert resolve_flow("https://127.0.0.1:8182", manual=False, server=False) == "server"


def test_manual_can_be_forced_on_a_ported_callback():
    """A laptop with a port but no usable browser, or an SSH session."""
    assert resolve_flow("https://127.0.0.1:8182", manual=True, server=False) == "manual"


def test_server_can_be_forced_on_a_portless_callback():
    """Kept available for a future laptop-with-port setup. It will fail, loudly
    and with an explanation, rather than being silently unavailable."""
    assert resolve_flow("https://127.0.0.1", manual=False, server=True) == "server"


def test_the_two_flags_are_mutually_exclusive():
    with pytest.raises(ValueError, match="mutually exclusive"):
        resolve_flow("https://127.0.0.1", manual=True, server=True)


def test_the_cli_rejects_both_flags(capsys):
    from scripts import schwab_auth

    with pytest.raises(SystemExit):
        schwab_auth.main(["--manual", "--server"])
    assert "not allowed with" in capsys.readouterr().err


def test_the_shipped_callback_url_selects_manual():
    """.env.example pins SCHWAB_CALLBACK_URL=https://127.0.0.1 on purpose."""
    env_example = (SCRIPT.parent.parent / ".env.example").read_text()
    line = next(ln for ln in env_example.splitlines() if ln.startswith("SCHWAB_CALLBACK_URL="))
    url = line.split("=", 1)[1].strip()
    assert should_use_manual_flow(url) is True


# ── the redirected URL is a secret ──────────────────────────────────────────


def test_an_authorization_code_is_redacted():
    url = "https://127.0.0.1/?code=C0.b1234SECRETCODE5678&session=abc-def"
    scrubbed = redact(f"Invalid request for {url}")
    assert "SECRETCODE" not in scrubbed
    assert "abc-def" not in scrubbed
    assert "code=<redacted>" in scrubbed


def test_tokens_in_an_error_body_are_redacted():
    body = (
        '{"access_token": "I0.aVeryLongSecret", "refresh_token": "R0.AnotherSecret", '
        '"id_token": "T0.AThirdSecret"}'
    )
    scrubbed = redact(body)
    for secret in ("aVeryLongSecret", "AnotherSecret", "AThirdSecret"):
        assert secret not in scrubbed


def test_redaction_is_case_insensitive():
    assert "SECRET" not in redact("?CODE=SECRETVALUE")


def test_redaction_leaves_harmless_text_alone():
    message = "authentication failed: connection refused to https://api.schwabapi.com"
    assert redact(message) == message


def test_the_callback_url_itself_is_not_treated_as_secret():
    """It is printed for troubleshooting and is not sensitive — only the code is."""
    assert redact("callback https://127.0.0.1") == "callback https://127.0.0.1"


# ── the script never handles the pasted URL itself ──────────────────────────


def test_the_script_does_not_read_the_redirect_url():
    """schwab-py reads it from stdin directly, so this script never holds it.

    Anything here that called input() or logged the value would be a new place
    for the code to leak from.
    """
    source = SCRIPT.read_text()
    assert "input(" not in source
    assert "received_url" not in source


def test_every_error_path_passes_through_redact():
    """A bare f-string of an exception would print the URL it echoed.

    Matched on AST NAME references rather than on the rendered text: a substring
    search for "exc" also hits the word "exchanged" in the warning prose, which
    is how the first version of this test failed on a harmless line.
    """
    tree = ast.parse(SCRIPT.read_text())

    def references_exception(call: ast.Call) -> bool:
        return any(isinstance(n, ast.Name) and n.id == "exc" for n in ast.walk(call))

    def calls_redact(call: ast.Call) -> bool:
        return any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "redact"
            for n in ast.walk(call)
        )

    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "print"):
            continue
        if references_exception(node):
            checked += 1
            assert calls_redact(node), f"unredacted exception printed: {ast.unparse(node)}"

    assert checked >= 2, "expected to find the error-path prints; did the script change?"


def test_http_loggers_are_clamped_during_the_flow():
    """A --verbose run must not be able to dump the token exchange."""
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    previous = _silence_http_logging()
    try:
        for name in NOISY_LOGGERS:
            assert logging.getLogger(name).level == logging.WARNING
    finally:
        _restore_logging(previous)
    assert logging.getLogger("httpx").level == logging.DEBUG
    logging.getLogger("httpx").setLevel(logging.NOTSET)


def test_the_clamped_set_covers_the_libraries_that_log_request_bodies():
    assert {"httpx", "httpcore", "authlib", "schwab"} <= set(NOISY_LOGGERS)


# ── sync_token.sh ───────────────────────────────────────────────────────────


def test_sync_token_uses_temp_then_rename():
    """scp writes incrementally, so copying straight onto the live path would let
    a snapshot job reading the token mid-transfer see a truncated file."""
    script = (SCRIPT.parent / "sync_token.sh").read_text()
    assert "schwab_token.json.tmp" in script
    assert "mv '" in script or 'mv "' in script
    # The temp name must be on the SAME filesystem as the target, or mv is not
    # atomic. Both are under $ENGINE_PATH/data.
    assert script.count("$ENGINE_PATH/data/schwab_token.json") >= 1


def test_sync_token_refuses_an_expired_token():
    """Better to notice here than from a failed snapshot job at 4:30pm, which
    under D7 is a permanently missing day."""
    script = (SCRIPT.parent / "sync_token.sh").read_text()
    assert "--status" in script
    assert "expired or expiring" in script


def test_sync_token_refuses_an_unconfigured_host():
    script = (SCRIPT.parent / "sync_token.sh").read_text()
    assert "REPLACE_ME" in script
    assert "set -euo pipefail" in script
