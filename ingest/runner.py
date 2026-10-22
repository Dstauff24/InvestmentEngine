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
    # Symbols never attempted because the run stopped cleanly (day budget
    # exhausted, D29). DECLINED is not FAILED: nothing went wrong, there was
    # simply no quota left, and the work resumes on the next run.
    declined: int = 0
    stopped_early: str | None = None

    @property
    def attempted(self) -> int:
        return len(self.ok) + len(self.failed)

    @property
    def fail_ratio(self) -> float:
        return len(self.failed) / self.attempted if self.attempted else 0.0


class TooManyFailures(RuntimeError):
    """The failure rate says the provider or the credentials are broken."""


class EmptyColumns(RuntimeError):
    """Rows parsed, but the fields that make them worth storing are all NULL."""


def require_value_coverage(
    rows: list[dict], *, fields: tuple[str, ...], label: str, minimum: float = 0.0
) -> float:
    """Fraction of rows carrying at least one of `fields`. Raises when it is 0.

    **THE ROW-COUNT GUARD WAS NOT ENOUGH.** `for_each_symbol` refuses to report
    "ok, 0 rows" when everything failed, because a job that writes nothing and
    says it succeeded is how a broken pipeline looks healthy. This is the same
    failure one level in: on 2026-10-21 the estimates job wrote 2,598 snapshot
    rows and reported success, and every estimate column in them was NULL —
    FMP had renamed `estimatedEpsAvg` to `epsAvg`, and a row that parses to
    None still INSERTs. The D7 revision clock, which no backfill can ever buy
    back, recorded nothing while the job logs said it was working.

    So a writer whose rows are all hollow fails. `minimum` allows a job to
    demand more than "at least one row has a value" where that is meaningful;
    zero coverage is always an error, because it means the parser and the
    payload no longer agree about anything.
    """
    if not rows:
        return 0.0
    covered = sum(1 for row in rows if any(row.get(f) is not None for f in fields))
    coverage = covered / len(rows)
    if coverage <= minimum:
        raise EmptyColumns(
            f"{label}: {len(rows)} row(s) parsed and {covered} carry any of "
            f"{list(fields)} ({coverage * 100:.0f}%). The parser and the payload no "
            f"longer agree — run `python scripts/field_probe.py` to see which field "
            f"names moved."
        )
    return coverage


def for_each_symbol(
    symbols: Sequence[str],
    handler: Callable[[str], int],
    *,
    run: JobRun | None = None,
    label: str = "symbols",
    max_fail_ratio: float = DEFAULT_MAX_FAIL_RATIO,
    min_sample: int = MIN_SAMPLE_FOR_RATIO,
    stop_on: type[BaseException] | tuple[type[BaseException], ...] | None = None,
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

    `stop_on` names exceptions that end the loop CLEANLY rather than counting as
    a failure — currently `BudgetExhausted` (D29). The unvisited symbols land in
    `outcome.declined`, and neither guard above fires, because running out of
    quota is not evidence that anything is broken.
    """
    outcome = SymbolOutcome()
    remaining = list(symbols)
    # An empty tuple in an `except` clause never matches, which is exactly the
    # behaviour we want when no clean-stop exception was named.
    stop_types: tuple[type[BaseException], ...] = (
        () if stop_on is None else stop_on if isinstance(stop_on, tuple) else (stop_on,)
    )

    for index, symbol in enumerate(symbols, start=1):
        remaining.remove(symbol)
        try:
            written = handler(symbol)
        except stop_types as exc:
            outcome.declined = len(remaining) + 1  # this symbol was not written either
            outcome.stopped_early = f"{type(exc).__name__}: {exc}"
            log.info("%s: stopped cleanly after %d symbol(s): %s", label, index - 1, exc)
            break
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
        "%s: %d ok, %d failed, %d declined, %d rows",
        label,
        len(outcome.ok),
        len(outcome.failed),
        outcome.declined,
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
