"""Explicit consent for outbound network calls (D27).

Every HTTP client in this repo asks permission here before it sends anything.
Without `ALLOW_LIVE_CALLS=1` in the environment, the call raises instead.

Why a flag and not a credential check. An earlier version of this engine
refused to build a client when an API key still read `CHANGE_ME`. Real keys
were present in the process environment, so nothing looked like a placeholder,
and running the orchestrator merely to exercise its exit codes quietly spent
live Finnhub quota fetching 401 real articles. The presence of a usable
credential says nothing about whether a call was intended. Consent has to be
its own signal.

Two layers:

  * `ALLOW_LIVE_CALLS` must be truthy. Absent or "0" means every provider call
    raises `LiveCallBlocked`, naming the provider and the endpoint.
  * Under pytest the answer is ALWAYS no, flag or not. CLAUDE.md says the test
    suite makes no network calls; that is enforced rather than trusted, and it
    cannot be defeated by a stray export in a shell profile or a CI runner.

Deliberately NOT read from `.env`: consent is a per-invocation decision.
`ALLOW_LIVE_CALLS=1 python -m jobs.daily_close` is a deliberate act and reads
like one in shell history and in the crontab; a line in `.env` is a standing
grant nobody remembers giving.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

FLAG = "ALLOW_LIVE_CALLS"
TRUTHY = frozenset({"1", "true", "yes", "on"})

# pytest sets this for the duration of each test. Its presence means we are
# inside the suite, where no outbound call is ever legitimate.
PYTEST_MARKER = "PYTEST_CURRENT_TEST"


class LiveCallBlocked(RuntimeError):
    """An outbound call was attempted without explicit consent."""


def live_calls_allowed(env: Mapping[str, str] | None = None) -> bool:
    """Is this process permitted to reach the network?

    `env` is injectable so the flag logic itself can be tested from inside
    pytest, where the real environment always answers no.
    """
    source = os.environ if env is None else env
    if PYTEST_MARKER in source:
        return False
    return source.get(FLAG, "").strip().lower() in TRUTHY


def require_live_calls(provider: str, detail: str = "") -> None:
    """Raise unless outbound calls are permitted. Called before every request."""
    if live_calls_allowed():
        return

    where = f" ({detail})" if detail else ""
    if PYTEST_MARKER in os.environ:
        raise LiveCallBlocked(
            f"blocked a live {provider} call{where} from inside the test suite. "
            f"Tests must use fixtures — see tests/fixtures/. This cannot be "
            f"overridden with {FLAG}."
        )
    raise LiveCallBlocked(
        f"blocked a live {provider} call{where}. Outbound calls need explicit "
        f"consent: re-run with {FLAG}=1, e.g.\n"
        f"    {FLAG}=1 python -m jobs.daily_close\n"
        f"Having a working API key in the environment is not consent — that is "
        f"exactly how quota gets spent by accident."
    )
