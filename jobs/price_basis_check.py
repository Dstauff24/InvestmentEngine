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
from features.adjustment import PRE_ADJUSTED, split_basis_violations

log = logging.getLogger(__name__)

JOB_NAME = "price_basis_check"


class BasisViolation(RuntimeError):
    """A split's stored series does not step by its logged ratio (D24).

    Either the bars are pre-adjusted or the log and the bars disagree about the
    event; `_remedy` says which, because the two have different fixes.
    """


def describe(violation: dict) -> str:
    return (
        f"{violation['symbol']} {violation['ex_date']}: log says {violation['ratio']:g}, "
        f"bars stepped {violation['observed_step']:g} [{violation['kind']}]"
    )


def _remedy(violations: list[dict]) -> str:
    """The fix depends on the KIND, so the message says which it is.

    A pre-adjusted series is repairable: the provider restated its history and
    a raw feed still exists. A ratio mismatch is not — FMP's raw feed is its
    adjusted feed with the split log reversed out, so a wrong log entry
    corrupts both, and the era has to be refused instead of re-fetched.
    """
    if all(v["kind"] == PRE_ADJUSTED for v in violations):
        return (
            "Those bars are already split-adjusted, so read-time adjustment is "
            "double-counting them. Re-source the affected sessions with "
            "`python -m ingest.fmp_history --repair-split-basis`."
        )
    if any(v["kind"] == PRE_ADJUSTED for v in violations):
        return (
            "MIXED KINDS. Repair the pre-adjusted ones with "
            "`python -m ingest.fmp_history --repair-split-basis`; a ratio mismatch "
            "cannot be re-sourced and needs a `price_basis_breaks` row (see "
            "db/migrations/022_price_basis_breaks.sql)."
        )
    return (
        "The series STEPPED, by the wrong amount: the action log and the bars "
        "disagree about what the event was. Re-sourcing cannot fix that — the "
        "provider's raw feed is its adjusted feed with the same log reversed out — "
        "so decide whether the event is mis-logged (record a `price_basis_breaks` "
        "row and the pre-event era is refused) or genuinely absent from the log."
    )


def run(*, as_of: date | None = None) -> int:
    """Returns the number of splits checked. Raises on a violation."""
    day = as_of or clock.session_date()
    with job_run(JOB_NAME, session_date=day, is_critical=False, triggered_by="manual") as job:
        # Recorded breaks come back too, so the note says what is quarantined
        # rather than leaving it to be remembered — but only the UNRECORDED ones
        # are failures. A recorded break is a known state, like a preflight SKIP.
        everything = split_basis_violations(as_of=day, include_recorded=True)
        violations = [v for v in everything if not v["recorded"]]
        known = [v for v in everything if v["recorded"]]
        job.note(
            violations=[describe(v) for v in violations][:20],
            recorded_breaks=[describe(v) for v in known][:20],
        )
        for v in known:
            log.info("known basis break (bars before it are refused): %s", describe(v))
        if violations:
            raise BasisViolation(
                f"{len(violations)} split(s) do not step by their logged ratio. "
                f"First: {describe(violations[0])}. {_remedy(violations)}"
            )
        log.info(
            "price basis ok: every split in the stored window steps by its ratio "
            "(%d recorded break(s) refused)",
            len(known),
        )
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
    print("price basis ok (D24): every split steps as logged, or its break is recorded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
