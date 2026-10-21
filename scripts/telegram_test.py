"""Send one real Telegram message, to prove the channel works end to end.

    ALLOW_LIVE_CALLS=1 python -m scripts.telegram_test

Deliberately a separate script rather than a flag on the engine: this is the
one thing you want to run by hand on a new VM, before trusting that a 16:30
snapshot failure will actually reach a phone. It bypasses nothing — same
`alerts/telegram.py`, same D27 consent gate, same redaction — so a success here
means the real alert path works, not that a test harness does.

`--no-record` keeps the `alerts` table clean on a VM that is already in
production; by default the send IS recorded, because a message that arrived
without a row would hide a bookkeeping bug.
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alerts import telegram
from core import clock, netguard, settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--no-record", action="store_true", help="do not write an alerts row")
    parser.add_argument(
        "--long",
        action="store_true",
        help="send an over-4096-character message to exercise the chunking",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if not settings.telegram_configured():
        print(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be set in .env.\n"
            "Create a bot with @BotFather, then get the chat id by messaging the\n"
            "bot once and reading https://api.telegram.org/bot<TOKEN>/getUpdates."
        )
        return 2

    if not netguard.live_calls_allowed():
        print(
            "refusing to send without consent (D27). Re-run as:\n"
            "    ALLOW_LIVE_CALLS=1 python -m scripts.telegram_test"
        )
        return 2

    body = (
        f"Telegram alert channel test.\n"
        f"host: {socket.gethostname()}\n"
        f"env: {settings.engine_env()}\n"
        f"session date: {clock.session_date()}\n\n"
        f"If you are reading this on a phone, a failed snapshot job will reach "
        f"you the same way."
    )
    if args.long:
        # Exercise the split path with something that has real line structure,
        # since a mid-line break is the thing that reads badly on a phone.
        body += "\n\n" + "\n".join(f"line {i:04d}: " + "x" * 60 for i in range(120))

    ok = telegram.send(
        "channel test",
        body,
        alert_type="job_failure",
        severity="info",
        dedupe_key=None,  # no dedupe: this is meant to be re-runnable
        record=not args.no_record,
    )

    if ok:
        print("sent — check the chat.")
        return 0
    print("FAILED to send; see the log above. The token is redacted from all output.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
