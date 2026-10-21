"""D24's assumption, checked instead of assumed.

    python -m jobs.price_basis_check

`prices_daily` is defined as RAW OHLCV, with splits applied at read time from
`corporate_actions` (D24). Nothing in a row says whether that is actually true,
and a provider that restates its own history leaves no trace — Schwab's daily
candles turned out to be split-ADJUSTED, so every read-time factor was an
adjustment of an adjustment. Double-adjusting produces a plausible-looking
series rather than an error, which is the worst way for data to be wrong.

The arithmetic settles it: a raw series STEPS by the split ratio at the
ex-date, an adjusted one does not. This job asks the question every day a
split lands, and the answer is a one-line job note until the day it is not.

NON-CRITICAL, like every derived layer: the check reads stored data and buys
no history. But a violation is a real failure of the job — a silent warning
would be the same mistake again, one level up.
"""

from __future__ import annotations

import logging
import sys
from datetime import date

from core import clock
from core.jobs import job_run
from features.adjustment import split_basis_violations

log = logging.getLogger(__name__)

JOB_NAME = "price_basis_check"


class BasisViolation(RuntimeError):
    """Stored bars do not step at a split: they are pre-adjusted (D24)."""


def run(*, as_of: date | None = None) -> int:
    """Returns the number of splits checked. Raises on a violation."""
    day = as_of or clock.session_date()
    with job_run(JOB_NAME, session_date=day, is_critical=False, triggered_by="manual") as job:
        violations = split_basis_violations(as_of=day)
        job.note(
            violations=[
                f"{v['symbol']} {v['ex_date']}: ratio {v['ratio']:g}, "
                f"series stepped {v['observed_step']:g}"
                for v in violations
            ][:20]
        )
        if violations:
            raise BasisViolation(
                f"{len(violations)} split(s) show no price step in `prices_daily`, so "
                f"those bars are already split-adjusted and read-time adjustment is "
                f"double-counting them. First: {violations[0]}. Re-source the affected "
                f"sessions with `python -m ingest.fmp_history --repair-split-basis`."
            )
        log.info("price basis ok: every split in the stored window steps by its ratio")
        return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    run()
    print("price basis ok (D24): stored bars step at every split ex-date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
