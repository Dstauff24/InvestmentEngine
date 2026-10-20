"""Mint a Schwab OAuth token (D21/O8).

    python scripts/schwab_auth.py            # picks a flow from the callback URL
    python scripts/schwab_auth.py --status   # how much life is left in the token
    python scripts/schwab_auth.py --manual   # force copy-paste flow
    python scripts/schwab_auth.py --server   # force local redirect server

Two flows, because the callback URL determines what is even possible:

  * **manual** (default when the callback URL has NO PORT). schwab-py prints an
    authorization link; you open it, log in, and paste back the URL the browser
    was redirected to. Works anywhere — a laptop, an SSH session, a Codespace —
    because nothing has to listen on a socket.

  * **server** (default when the callback URL specifies a port). schwab-py binds
    a local HTTPS server on that port and catches the redirect itself. Fewer
    steps, but it needs a browser on the same machine AND a port to bind. With a
    port-less callback such as `https://127.0.0.1` there is nothing to bind and
    the flow dies with `RedirectServerExitedError`.

**The redirected URL is a secret.** It carries a one-time authorization code
that can be exchanged for a token. schwab-py reads it directly from stdin and
never logs it; this script never sees it, never echoes it, and redacts query
strings out of any error it prints. HTTP-level loggers are clamped for the
duration so a verbose run cannot dump the token exchange. Do not paste that URL
into a chat, a ticket, or a shell that records history.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import settings
from ingest.schwab_client import (
    REFRESH_TOKEN_LIFETIME,
    TOKEN_WARN_BEFORE,
    token_age,
    token_expires_at,
)

# Libraries that can log a request body containing the authorization code or the
# token response if the root logger is set to DEBUG.
NOISY_LOGGERS = ("httpx", "httpcore", "authlib", "urllib3", "schwab")

_SECRET_PATTERNS = (
    re.compile(r"(code=)[^&\s'\"]+", re.IGNORECASE),
    re.compile(r"(session=)[^&\s'\"]+", re.IGNORECASE),
    re.compile(r"(access_token\"?\s*[:=]\s*\"?)[^,&\s'\"]+", re.IGNORECASE),
    re.compile(r"(refresh_token\"?\s*[:=]\s*\"?)[^,&\s'\"]+", re.IGNORECASE),
    re.compile(r"(id_token\"?\s*[:=]\s*\"?)[^,&\s'\"]+", re.IGNORECASE),
)


def redact(text: str) -> str:
    """Strip authorization codes and tokens out of anything about to be printed."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1<redacted>", text)
    return text


# ── flow selection ──────────────────────────────────────────────────────────


def callback_port(url: str) -> int | None:
    """The explicit port in a callback URL, or None if it has none.

    `urlparse` reports None for an unspecified port even on https, which is the
    distinction that matters: `https://127.0.0.1` has nothing to bind, while
    `https://127.0.0.1:8182` does. A malformed port is treated as absent rather
    than raising, so a typo in `.env` degrades to the flow that always works.
    """
    try:
        return urlparse(url).port
    except ValueError:
        return None


def should_use_manual_flow(url: str) -> bool:
    """True when no local redirect server can be bound.

    A port-less callback is the common case for an individual-use Schwab app and
    is what a Codespace or any headless host will have.
    """
    return callback_port(url) is None


def resolve_flow(url: str, *, manual: bool, server: bool) -> str:
    """Return 'manual' or 'server'. Explicit flags win over the URL."""
    if manual and server:
        raise ValueError("--manual and --server are mutually exclusive")
    if manual:
        return "manual"
    if server:
        return "server"
    return "manual" if should_use_manual_flow(url) else "server"


# ── status ──────────────────────────────────────────────────────────────────


def show_status() -> int:
    path = settings.schwab_token_path()
    if not path.exists():
        print(f"no token at {path}")
        print("run this script without --status to create one")
        return 1

    age = token_age(path)
    expires = token_expires_at(path)
    remaining = expires - datetime.now(UTC)

    print(f"token   {path}")
    print(f"age     {age.days}d {age.seconds // 3600}h")
    print(f"expires {expires:%Y-%m-%d %H:%M %Z}")
    if remaining.total_seconds() <= 0:
        print("STATUS  EXPIRED — re-auth now")
        return 1
    if remaining <= TOKEN_WARN_BEFORE:
        print(f"STATUS  expires in {remaining.seconds // 3600}h — re-auth today")
        return 1
    print(f"STATUS  ok, {remaining.days}d {remaining.seconds // 3600}h left")
    return 0


# ── authentication ──────────────────────────────────────────────────────────


def _silence_http_logging() -> dict[str, int]:
    """Clamp libraries that would otherwise log the code or the token at DEBUG."""
    previous = {}
    for name in NOISY_LOGGERS:
        logger = logging.getLogger(name)
        previous[name] = logger.level
        logger.setLevel(logging.WARNING)
    return previous


def _restore_logging(previous: dict[str, int]) -> None:
    for name, level in previous.items():
        logging.getLogger(name).setLevel(level)


def _explain_manual_flow(callback: str) -> None:
    print("Flow: MANUAL (copy-paste)")
    if callback_port(callback) is None:
        print(f"  Chosen because {callback} specifies no port, so there is nothing")
        print("  for a local redirect server to bind. Use --server to override.")
    print()
    print("  You will be shown a link. Open it in any browser, log in, approve the")
    print("  app, and then copy the ENTIRE URL your browser lands on — including")
    print("  everything after the '?', which is where the `code` parameter lives.")
    print("  Paste it at the 'Redirect URL>' prompt below.")
    print()
    print("  The page you land on will usually look like an error. That is expected:")
    print("  nothing is listening at the callback address. The URL in the address")
    print("  bar is still exactly what is needed.")
    print()
    print("  That URL is a SECRET — the code in it can be exchanged for a token.")
    print("  It is read straight from this prompt, never echoed and never logged.")
    print("  Do not paste it anywhere else. It is single-use and expires quickly,")
    print("  so complete this promptly.")
    print()


def authenticate(*, flow: str) -> int:
    from schwab.auth import client_from_login_flow, client_from_manual_flow

    path = settings.schwab_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    app_key = settings.schwab_app_key()
    app_secret = settings.schwab_app_secret()
    callback = settings.schwab_callback_url()

    print(f"app key      {app_key[:4]}...{app_key[-4:]}")
    print(f"callback     {callback}")
    print(f"token file   {path}")
    print()

    if flow == "manual":
        _explain_manual_flow(callback)
    else:
        port = callback_port(callback)
        print("Flow: SERVER (local redirect listener)")
        if port is None:
            print(f"  WARNING: {callback} specifies no port, so there is nothing for a")
            print("  local redirect server to bind to. This will fail with")
            print("  RedirectServerExitedError. Use --manual instead.")
        else:
            print(f"  Binding a local HTTPS listener on port {port} and opening a browser.")
        print()

    previous = _silence_http_logging()
    try:
        flow_fn = client_from_manual_flow if flow == "manual" else client_from_login_flow
        client = flow_fn(
            api_key=app_key,
            app_secret=app_secret,
            callback_url=callback,
            token_path=str(path),
        )
    except KeyboardInterrupt:
        print("\naborted; no token written")
        return 1
    except Exception as exc:
        # redact(): an authlib or requests error can echo the URL it was given,
        # which would put the authorization code on the terminal and into any
        # log the operator is piping this into.
        print(f"\nauthentication failed: {type(exc).__name__}: {redact(str(exc))}")
        print("\nThings worth checking:")
        print("  * SCHWAB_CALLBACK_URL matches the developer-portal registration exactly,")
        print("    including scheme, case, and any trailing slash")
        print("  * the app shows 'Ready for use' on the portal")
        print("  * SCHWAB_APP_KEY / SCHWAB_APP_SECRET are the app's, not the brokerage login")
        if flow == "server":
            print("  * --server needs a port in the callback URL and a browser on this")
            print("    machine; if either is missing, use --manual")
        else:
            print("  * the pasted URL was the COMPLETE address including ?code=...")
            print("  * the code was used promptly — it expires within a couple of minutes")
        return 1
    finally:
        _restore_logging(previous)

    path.chmod(0o600)
    print(f"\ntoken written to {path} (mode 600)")
    print(f"valid for {REFRESH_TOKEN_LIFETIME.days} days")

    try:
        response = client.get_quotes(["SPY"])
        if response.status_code == 200:
            print("verified: fetched a live quote for SPY")
        else:
            print(f"warning: token written but a test quote returned HTTP {response.status_code}")
    except Exception as exc:
        print(f"warning: token written but the test quote failed: {redact(str(exc))}")

    print("\nNext: sync it to the VM  ->  ./scripts/sync_token.sh")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--status", action="store_true", help="report token age and expiry")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--manual",
        action="store_true",
        help="copy-paste flow; works headless. Default when the callback URL has no port",
    )
    group.add_argument(
        "--server",
        action="store_true",
        help="local redirect server; needs a port in the callback URL and a local browser",
    )
    args = parser.parse_args(argv)

    try:
        if args.status:
            return show_status()
        flow = resolve_flow(settings.schwab_callback_url(), manual=args.manual, server=args.server)
        return authenticate(flow=flow)
    except settings.SettingsError as exc:
        # Redacted too. A SettingsError cannot carry an authorization code
        # today, but applying this uniformly is cheaper than reasoning about
        # which exception types can — and a future message might embed a value.
        print(f"configuration problem: {redact(str(exc))}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
