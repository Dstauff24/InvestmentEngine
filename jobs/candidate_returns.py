"""Forward returns for the day's ranked candidates (D42 amended).

    python -m jobs.candidate_returns --dry-run
    python -m jobs.candidate_returns

**THE QUESTION THIS TABLE EXISTS TO ANSWER.** D42 rolls every qualifying idea
that did not surface into one aggregate row, which carries no symbol and
therefore no forward price — so it cannot say whether the COMPOSITE predicts
returns at all, independent of any recipe. `daily_candidates` keeps the top N
by rank with their scores and their refusal reasons, and this job fills in what
price did afterwards.

**THESE ARE SCORES, NOT TRADES.** No entry, stop, target or R multiple: most of
these rows never had a plan, and the ones that did are measured as trades
elsewhere. A forward return here is a property of the score. D13's expectancy
and D40's ladder count CLOSED SHADOW BOOK IDEAS, and a test asserts this job
writes nothing those read.

**SESSIONS, NOT CALENDAR DAYS.** 5, 10 and 20 SESSIONS after the scoring date,
counted in the symbol's own bars, because a holiday week would otherwise
shorten the window it claims to measure. Each horizon records the session it
was measured TO, so the count is checkable rather than implied.

**TOTAL RETURN (D19).** A dividend paid inside a 20-session window is part of
what holding earned. The technical features stay split-only on purpose — a
dividend-adjusted moving average does not match the chart a human reads — so
these two series differ by design, and this is the one that answers "what did
the score earn".

**FILLED ONCE, NEVER REWRITTEN.** The writer only ever fills a NULL. A repaired
basis or a late bar must not silently move a number a study has already read,
and re-running the job on a day is therefore safe and idempotent.
"""

from __future__ import annotations

import logging
import sys
from datetime import date

import pandas as pd

from core import clock, db
from features.adjustment import adjust_bars
from features.technical import load_actions, on_adjusted_basis

log = logging.getLogger(__name__)

JOB_NAME = "candidate_returns"
HORIZONS = (5, 10, 20)

__all__ = ["HORIZONS", "JOB_NAME", "forward_returns", "run"]

PENDING = """
    SELECT session_date, symbol, fwd_5_pct, fwd_10_pct, fwd_20_pct
    FROM   daily_candidates
    WHERE  (fwd_5_pct IS NULL OR fwd_10_pct IS NULL OR fwd_20_pct IS NULL)
      AND  session_date <= %(as_of)s
    ORDER  BY symbol, session_date
"""

BARS = """
    SELECT session_date, open, high, low, close
    FROM   prices_daily
    WHERE  symbol = %(symbol)s AND session_date >= %(since)s AND session_date <= %(as_of)s
    ORDER  BY session_date
"""

# Only ever fills a NULL: `COALESCE(existing, new)` in SQL rather than in
# Python, so a concurrent fill cannot overwrite either.
FILL = """
    UPDATE daily_candidates SET
        fwd_5_pct  = COALESCE(fwd_5_pct,  %(fwd_5_pct)s),
        fwd_5_on   = COALESCE(fwd_5_on,   %(fwd_5_on)s),
        fwd_10_pct = COALESCE(fwd_10_pct, %(fwd_10_pct)s),
        fwd_10_on  = COALESCE(fwd_10_on,  %(fwd_10_on)s),
        fwd_20_pct = COALESCE(fwd_20_pct, %(fwd_20_pct)s),
        fwd_20_on  = COALESCE(fwd_20_on,  %(fwd_20_on)s)
    WHERE session_date = %(session_date)s AND symbol = %(symbol)s
"""


def forward_returns(
    bars: pd.DataFrame, *, from_date: date, horizons=HORIZONS
) -> dict[int, tuple[float, date] | None]:
    """{horizon: (percent return, session measured to)} from a TOTAL-RETURN frame.

    Pure, and indifferent to which symbol it is. `None` for a horizon the bars
    do not reach yet — never a zero, which would read as "it went nowhere".

    Both ends come from ONE adjusted frame, so the ratio is basis-stable: a
    split inside the window cancels, which is exactly what the raw columns
    would not do.
    """
    out: dict[int, tuple[float, date] | None] = dict.fromkeys(horizons)
    if bars is None or bars.empty or "close" not in bars:
        return out

    frame = bars.dropna(subset=["close"]).reset_index(drop=True)
    dates = list(frame["session_date"])
    if from_date not in dates:
        return out
    start = dates.index(from_date)
    base = float(frame["close"].iloc[start])
    if base <= 0:
        return out

    for horizon in horizons:
        index = start + horizon
        if index >= len(frame):
            continue
        later = float(frame["close"].iloc[index])
        out[horizon] = ((later - base) / base * 100.0, dates[index])
    return out


def load_total_return_bars(symbol: str, *, since: date, as_of: date) -> pd.DataFrame:
    """The symbol's bars on a TOTAL-RETURN basis, newest last.

    `features.technical.load_bars` is split-only (D24) because that is what a
    chart shows; performance is measured on total return (D19), so this takes
    the same raw rows through the other mode.
    """
    rows = db.fetch_all(BARS, {"symbol": symbol, "since": since, "as_of": as_of})
    if not rows:
        return pd.DataFrame(columns=["session_date", "close"])
    bars = pd.DataFrame(rows)
    adjusted = adjust_bars(
        bars,
        load_actions(symbol, as_of=as_of),
        mode="total_return",
        as_of=as_of,
        # A cash distribution with no preceding bar in THIS window cannot be
        # priced; skipping it (factor 1.0) is better than refusing the whole
        # symbol, and the window starts before the scoring date for that reason.
        strict=False,
    )
    return on_adjusted_basis(adjusted)


def run(*, as_of: date | None = None, record: bool = True) -> int:
    """Fill every horizon the bars now reach. Returns the number of fills."""
    from core.jobs import job_run

    day = as_of or clock.session_date()
    pending = db.fetch_all(PENDING, {"as_of": day})
    if not pending:
        log.info("no candidate rows awaiting a forward return")
        if record:
            with job_run(JOB_NAME, session_date=day, is_critical=False, triggered_by="cron"):
                pass
        return 0

    by_symbol: dict[str, list[dict]] = {}
    for row in pending:
        by_symbol.setdefault(row["symbol"], []).append(row)

    fills = 0
    with (
        job_run(JOB_NAME, session_date=day, is_critical=False, triggered_by="cron")
        if record
        else _nullcontext()
    ) as job:
        for symbol, rows in by_symbol.items():
            earliest = min(r["session_date"] for r in rows)
            # Reach back a little before the earliest scoring date so a
            # dividend on that session still has a prior close to price against.
            bars = load_total_return_bars(symbol, since=_back(earliest, 40), as_of=day)
            for row in rows:
                measured = forward_returns(bars, from_date=row["session_date"])
                params = {"session_date": row["session_date"], "symbol": symbol}
                wrote = False
                for horizon in HORIZONS:
                    value = measured.get(horizon)
                    params[f"fwd_{horizon}_pct"] = None if value is None else round(value[0], 4)
                    params[f"fwd_{horizon}_on"] = None if value is None else value[1]
                    if value is not None and row[f"fwd_{horizon}_pct"] is None:
                        wrote = True
                if not wrote:
                    continue
                db.execute(FILL, params)
                fills += 1
        if job is not None:
            job.add_rows(fills)
            job.note(symbols=len(by_symbol), pending_rows=len(pending), filled=fills)

    log.info("filled a horizon on %d of %d pending candidate row(s)", fills, len(pending))
    return fills


def _back(day: date, sessions: int) -> date:
    """`sessions` trading days before `day`, approximated in calendar days.

    Deliberately generous rather than exact: this only sets how far back the
    bar window starts, and a few extra rows cost nothing while a short window
    would drop the prior close a dividend needs.
    """
    from datetime import timedelta

    return day - timedelta(days=int(sessions * 1.6) + 7)


class _nullcontext:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--as-of", help="ISO date; defaults to today's session")
    parser.add_argument(
        "--dry-run", action="store_true", help="count what would be filled, write nothing"
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    day = date.fromisoformat(args.as_of) if args.as_of else None
    if args.dry_run:
        pending = db.fetch_all(PENDING, {"as_of": day or clock.session_date()})
        print(f"{len(pending)} candidate row(s) awaiting at least one forward return")
        return 0
    print(f"filled {run(as_of=day)} horizon(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
