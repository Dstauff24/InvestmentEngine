"""The Friday scorecard, as its own cron entry (D13/D16/D40).

    python -m jobs.weekly_scorecard --dry-run    # render to stdout, send nothing
    python -m jobs.weekly_scorecard              # render and send

**A SEPARATE ENTRY, NOT A STEP IN `daily_close`.** The nightly pipeline exists
to collect history that cannot be bought back (D7), and its exit code is what
tells an operator whether the day is intact. A weekly report hanging off the
end of it would make a Friday failure ambiguous — did the snapshot fail, or did
Telegram time out? — and would also mean the scorecard could never be re-run
on its own without re-running the pipeline.

It makes NO provider calls, so the crontab line carries no `ALLOW_LIVE_CALLS`
(D27): everything here is read from the database the nightly run wrote. If the
flag appears on this line, someone has misunderstood what it does.

Exit code is 0 unless the job itself failed. A week with nothing in it is a
report, not an error.
"""

from __future__ import annotations

import logging
import sys
from datetime import date

from alerts import scorecard

log = logging.getLogger(__name__)

JOB_NAME = scorecard.JOB_NAME


def run(*, as_of: date | None = None, send: bool = True) -> str:
    return scorecard.run(as_of=as_of, send=send)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="render and print it, send nothing — what a deploy should run",
    )
    parser.add_argument("--as-of", help="ISO date; defaults to today's session")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    day = date.fromisoformat(args.as_of) if args.as_of else None
    body = run(as_of=day, send=not args.dry_run)
    if args.dry_run:
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
