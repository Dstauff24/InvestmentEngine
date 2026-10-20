"""Warn before the Schwab token expires (D21 / O8).

    python -m scripts.token_expiry_check          # email if inside the window
    python -m scripts.token_expiry_check --check  # report only, send nothing

Schwab refresh tokens last 7 days, so re-auth is a recurring chore, and the way
it goes wrong is silent: the token quietly dies over a weekend and Monday's
chain snapshot fails — which under D7 is a PERMANENTLY missing day, not a
retryable one. This runs on its own cron entry ahead of expiry so the reminder
arrives while there is still time to act.

Exit codes:
    0   plenty of life left, or a warning was successfully sent
    1   the token is missing or already expired
    2   configuration problem
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alerts import mailer
from core import settings
from ingest.schwab_client import (
    REFRESH_TOKEN_LIFETIME,
    TOKEN_WARN_BEFORE,
    token_age,
    token_expires_at,
)


def describe(remaining) -> str:
    if remaining.total_seconds() <= 0:
        return "EXPIRED"
    hours = int(remaining.total_seconds() // 3600)
    return f"{hours // 24}d {hours % 24}h remaining"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="report only, send no email")
    args = parser.parse_args(argv)

    try:
        path = settings.schwab_token_path()
    except settings.SettingsError as exc:
        print(f"configuration problem: {exc}")
        return 2

    expires = token_expires_at(path)
    if expires is None:
        subject = "Schwab token is MISSING"
        body = (
            f"No Schwab token found at {path}.\n\n"
            f"Every Schwab job will fail until this is fixed, including the D7 "
            f"option-chain snapshot — and a missed snapshot day cannot be backfilled "
            f"from any vendor at any price.\n\n"
            f"Fix, on the laptop:\n"
            f"    python scripts/schwab_auth.py\n"
            f"    ./scripts/sync_token.sh\n"
        )
        print(subject)
        if not args.check:
            mailer.send(
                subject,
                body,
                alert_type="token_expiry",
                severity="critical",
                dedupe_key=f"token_missing:{datetime.now(UTC):%Y-%m-%d}",
            )
        return 1

    remaining = expires - datetime.now(UTC)
    age = token_age(path)
    status = describe(remaining)
    print(f"token   {path}")
    print(f"age     {age.days}d {age.seconds // 3600}h")
    print(f"expires {expires:%Y-%m-%d %H:%M %Z}  ({status})")

    if remaining > TOKEN_WARN_BEFORE:
        print("STATUS  ok")
        return 0

    expired = remaining.total_seconds() <= 0
    subject = "Schwab token has EXPIRED" if expired else f"Schwab token expires in {status}"
    body = (
        f"Token file: {path}\n"
        f"Expires:    {expires:%Y-%m-%d %H:%M %Z} ({status})\n"
        f"Lifetime:   {REFRESH_TOKEN_LIFETIME.days} days from last re-auth\n\n"
        f"Re-auth on the laptop and sync:\n"
        f"    python scripts/schwab_auth.py\n"
        f"    ./scripts/sync_token.sh\n\n"
        f"If this lapses, the D7 option-chain snapshot stops running, and a missed "
        f"day is gone for good — the chain as it stood that afternoon is not sold by "
        f"anyone.\n"
    )
    print(f"STATUS  {'EXPIRED' if expired else 'warning'} — re-auth needed")

    if not args.check:
        # One reminder per day, not one per cron fire.
        mailer.send(
            subject,
            body,
            alert_type="token_expiry",
            severity="critical" if expired else "warning",
            dedupe_key=f"token_expiry:{expires:%Y-%m-%d}:{'expired' if expired else 'warn'}",
        )
    return 1 if expired else 0


if __name__ == "__main__":
    sys.exit(main())
