"""Earnings RELEASE TIMES from Finnhub -> `events.event_time`.

    ALLOW_LIVE_CALLS=1 python -m ingest.finnhub_earnings
    ALLOW_LIVE_CALLS=1 python -m ingest.finnhub_earnings --days-back 30

**WHY A SECOND PROVIDER FOR ONE FIELD.** FMP publishes no release time on
either earnings endpoint (verified 2026-10-22: `/stable/earnings` and
`/stable/earnings-calendar` both return `date` with no `time`), so every stored
row was `unknown` — and without it the engine cannot tell a report released
before the open from one released after the close. That single fact is worth a
provider call because it changes what a plan may act on and when:

  * **Unknown** — `post_earnings_drift` must judge the reaction over the whole
    close(T-1)..close(T+1) window and may not enter before T+2.
  * **`bmo`** — the news was out before T's open, so the reaction is complete
    at T's close and entry may be T+1. A day earlier, with no lookahead.
  * **`amc`** — the news landed after T's close, so T's own bar is pre-news and
    close(T) is the exact reference rather than close(T-1).

Finnhub's `/calendar/earnings` carries `hour` as `bmo` / `amc` / `dmh`, and on
a live 7-day window 593 of 1,074 rows had one.

**IT ONLY UPDATES ROWS THAT ALREADY EXIST**, matched on (symbol, event_date).
FMP owns what an earnings row IS (D47) and this owns one column of it; letting
a second provider INSERT earnings dates would mean two sources disagreeing
about when a company reports, with nothing to arbitrate. A date Finnhub has
and FMP does not is reported as unmatched rather than written.

NON-CRITICAL. A failure costs the one-day tightening, never history.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta

from core import clock, config, db
from core.jobs import job_run
from ingest.finnhub_client import FinnhubClient

log = logging.getLogger(__name__)

JOB_NAME = "finnhub_earnings_times"

# Finnhub's own vocabulary. `dmh` ("during market hours") is their third value
# and maps to the schema's `dmt`; anything else, including the empty string
# they return for an unconfirmed time, stays `unknown` rather than guessing.
HOUR_MAP = {"bmo": "bmo", "amc": "amc", "dmh": "dmt"}

# How far back to look. The drift recipe judges a report at the NEXT session, so
# the release time for a report that landed yesterday is still needed today —
# a forward-only window would arrive a day too late to be useful.
DEFAULT_DAYS_BACK = 10
# Windows are chunked so one response stays a manageable size. Finnhub
# documents no row cap, but FMP's silent 4,000-row truncation is recent enough
# to make an unbounded window a bad habit rather than a convenience.
CHUNK_DAYS = 30

UPDATE = """
    UPDATE events
    SET    event_time = %(event_time)s,
           updated_at = now()
    WHERE  symbol = %(symbol)s
      AND  event_type = 'earnings'
      AND  event_date = %(event_date)s
      AND  event_time IS DISTINCT FROM %(event_time)s
"""


@dataclass
class StampResult:
    """What a run learned, in the terms the next decision needs."""

    rows_seen: int = 0
    with_hour: int = 0
    in_universe: int = 0
    matched: int = 0
    changed: int = 0
    unmatched: list[str] = field(default_factory=list)
    windows: int = 0

    @property
    def hour_coverage(self) -> float | None:
        """How often Finnhub knows the time at all. The ceiling on how much of
        the universe can ever get the one-day tightening."""
        return self.with_hour / self.rows_seen if self.rows_seen else None

    @property
    def match_rate(self) -> float | None:
        """How often a Finnhub row lines up with a date FMP gave us.

        The number to watch: the two providers dating the same report a day
        apart would show up here and nowhere else, and stamping a time onto the
        wrong session would be worse than having no time at all.
        """
        return self.matched / self.in_universe if self.in_universe else None

    def __str__(self) -> str:
        coverage = "n/a" if self.hour_coverage is None else f"{self.hour_coverage * 100:.0f}%"
        match = "n/a" if self.match_rate is None else f"{self.match_rate * 100:.0f}%"
        return (
            f"{self.rows_seen} calendar row(s) over {self.windows} window(s); "
            f"{self.with_hour} carry a time ({coverage}); {self.in_universe} in universe, "
            f"{self.matched} matched a stored date ({match}), {self.changed} changed"
        )


def parse_times(rows: list[dict], universe: set[str]) -> list[dict]:
    """Finnhub calendar rows -> `events` update params, universe only.

    A row with no usable `hour` is DROPPED rather than written as `unknown`:
    the column already defaults to unknown, and writing it would overwrite a
    time an earlier run had successfully established.
    """
    out = []
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        when = _as_date(row.get("date"))
        hour = HOUR_MAP.get(str(row.get("hour") or "").strip().lower())
        if not symbol or symbol not in universe or when is None or hour is None:
            continue
        out.append({"symbol": symbol, "event_date": when, "event_time": hour})
    return out


STORED_SQL = """
    SELECT symbol, event_date
    FROM   events
    WHERE  event_type = 'earnings' AND event_date BETWEEN %s AND %s
"""


def stamp_release_times(
    client: FinnhubClient | None = None,
    *,
    days_back: int = DEFAULT_DAYS_BACK,
    as_of: date | None = None,
) -> StampResult:
    """Fetch the calendar and stamp `event_time` on rows we already hold."""
    client = client or FinnhubClient()
    today = as_of or clock.session_date()
    start = today - timedelta(days=days_back)
    end = today + timedelta(days=config.universe().events.forward_days)
    universe = set(db.active_symbols())
    if not universe:
        raise RuntimeError("no active tickers; run `python -m ingest.universe` first")

    result = StampResult()
    params: list[dict] = []
    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=CHUNK_DAYS), end)
        rows = client.earnings_calendar(cursor, window_end)
        result.windows += 1
        result.rows_seen += len(rows)
        result.with_hour += sum(1 for r in rows if str(r.get("hour") or "").strip())
        window_params = parse_times(rows, universe)
        params.extend(window_params)
        log.info(
            "%s..%s: %d row(s), %d in universe with a time",
            cursor,
            window_end,
            len(rows),
            len(window_params),
        )
        cursor = window_end + timedelta(days=1)

    result.in_universe = len(params)
    # ONE query for what we hold, then set arithmetic. Asking the database per
    # row would make "no such row" and "the time was already right" the same
    # rowcount of zero, and those are different facts: the first is a provider
    # disagreement worth reporting and the second is a quiet re-run.
    stored = {(row["symbol"], row["event_date"]) for row in db.fetch_all(STORED_SQL, (start, end))}
    writable = [p for p in params if (p["symbol"], p["event_date"]) in stored]
    result.matched = len(writable)
    result.unmatched = [
        f"{p['symbol']} {p['event_date']}"
        for p in params
        if (p["symbol"], p["event_date"]) not in stored
    ]
    result.changed = db.executemany(UPDATE, writable) if writable else 0
    return result


def run(*, days_back: int = DEFAULT_DAYS_BACK, as_of: date | None = None) -> int:
    """Pipeline entry point. Returns the number of rows stamped."""
    day = as_of or clock.session_date()
    with job_run(JOB_NAME, session_date=day, is_critical=False, triggered_by="manual") as job:
        result = stamp_release_times(days_back=days_back, as_of=day)
        stamped = _count_stamped(day)
        job.add_rows(result.changed)
        job.note(
            rows_seen=result.rows_seen,
            with_hour=result.with_hour,
            hour_coverage_pct=(
                None if result.hour_coverage is None else round(result.hour_coverage * 100, 1)
            ),
            in_universe=result.in_universe,
            matched=result.matched,
            match_rate_pct=(
                None if result.match_rate is None else round(result.match_rate * 100, 1)
            ),
            changed=result.changed,
            windows=result.windows,
            unmatched=len(result.unmatched),
            unmatched_sample=sorted(result.unmatched)[:20],
            stored_with_time=stamped,
        )
        log.info("release times: %s; %d stored rows now carry one", result, stamped)
    return result.changed


def _count_stamped(day: date) -> int:
    row = db.fetch_one(
        """
        SELECT count(*) AS n FROM events
        WHERE  event_type = 'earnings' AND event_time IN ('bmo', 'amc', 'dmt')
          AND  event_date >= %s - 30
        """,
        (day,),
    )
    return int(row["n"]) if row else 0


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--days-back", type=int, default=DEFAULT_DAYS_BACK)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    print(f"release times stamped: {run(days_back=args.days_back)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
