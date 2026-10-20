"""Shared shape for "iterate the universe, tolerate one bad symbol" jobs.

Every ingestion job walks ~500 symbols against a third-party API. One symbol
returning garbage must not cost the other 499 — but it must also be COUNTED, so
that a job which quietly succeeded on twelve names does not look like a job that
succeeded.

The counters land in `job_runs` (symbols_ok / symbols_failed / rows_written).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from core.jobs import JobRun

log = logging.getLogger(__name__)

# Above this share of failures the job is not degraded, it is broken — an
# expired key, a revoked plan, a provider outage. Better to fail loudly than to
# write a tenth of the universe and record success.
DEFAULT_MAX_FAIL_RATIO = 0.25

# Below this many symbols the ratio is not evidence of anything. One failure out
# of three is 33% and would trip the guard, which would make a small debug run
# (`--symbols AAPL MSFT`) unusable exactly when something is broken.
MIN_SAMPLE_FOR_RATIO = 10


@dataclass
class SymbolOutcome:
    ok: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    rows: int = 0

    @property
    def attempted(self) -> int:
        return len(self.ok) + len(self.failed)

    @property
    def fail_ratio(self) -> float:
        return len(self.failed) / self.attempted if self.attempted else 0.0


class TooManyFailures(RuntimeError):
    """The failure rate says the provider or the credentials are broken."""


def for_each_symbol(
    symbols: Sequence[str],
    handler: Callable[[str], int],
    *,
    run: JobRun | None = None,
    label: str = "symbols",
    max_fail_ratio: float = DEFAULT_MAX_FAIL_RATIO,
    min_sample: int = MIN_SAMPLE_FOR_RATIO,
    progress_every: int = 100,
) -> SymbolOutcome:
    """Call `handler(symbol)` for each symbol; it returns the rows it wrote.

    Raises `TooManyFailures` when:

      * EVERY symbol failed, at any sample size. Total failure needs no ratio to
        interpret, and without this a step where nothing worked logs
        "ok, 0 rows" — indistinguishable from a step that legitimately had
        nothing to do, which is how a broken pipeline looks healthy.
      * more than `max_fail_ratio` of at least `min_sample` symbols failed.
        Small runs are exempt from THIS rule only: on a three-symbol debug run a
        single failure is a bad symbol, not a broken provider.
    """
    outcome = SymbolOutcome()

    for index, symbol in enumerate(symbols, start=1):
        try:
            written = handler(symbol)
        except Exception as exc:  # one bad symbol must not stop the other 499
            log.warning("%s: %s failed: %s", label, symbol, exc)
            outcome.failed[symbol] = f"{type(exc).__name__}: {exc}"
            if run:
                run.symbol_failed()
        else:
            outcome.ok.append(symbol)
            outcome.rows += written
            if run:
                run.symbol_ok()
                run.add_rows(written)

        if progress_every and index % progress_every == 0:
            log.info(
                "%s: %d/%d done (%d failed, %d rows)",
                label,
                index,
                len(symbols),
                len(outcome.failed),
                outcome.rows,
            )

    if outcome.attempted and not outcome.ok:
        sample = list(outcome.failed.items())[:3]
        raise TooManyFailures(
            f"{label}: all {outcome.attempted} symbol(s) failed. Examples: {sample}"
        )

    if outcome.attempted >= min_sample and outcome.fail_ratio > max_fail_ratio:
        sample = list(outcome.failed.items())[:3]
        raise TooManyFailures(
            f"{label}: {len(outcome.failed)}/{outcome.attempted} symbols failed "
            f"({outcome.fail_ratio:.0%}, limit {max_fail_ratio:.0%}). This looks like a "
            f"provider or credential problem, not bad symbols. Examples: {sample}"
        )

    log.info(
        "%s: %d ok, %d failed, %d rows",
        label,
        len(outcome.ok),
        len(outcome.failed),
        outcome.rows,
    )
    return outcome


# ── numeric coercion ────────────────────────────────────────────────────────
# Provider numerics arrive as int, float, str, "" or None, sometimes NaN.


def as_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if result != result else result  # drop NaN


def as_int(value: object) -> int | None:
    result = as_float(value)
    return None if result is None else int(result)


def safe_ratio(numerator: object, denominator: object) -> float | None:
    """Margin-style ratio returning None instead of raising on a zero base."""
    top, bottom = as_float(numerator), as_float(denominator)
    if top is None or bottom in (None, 0):
        return None
    return top / bottom  # type: ignore[operator]
