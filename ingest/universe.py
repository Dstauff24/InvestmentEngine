"""Build the Phase 1 ticker universe (D14) and upsert it into `tickers`.

    ALLOW_LIVE_CALLS=1 python -m ingest.universe
    ALLOW_LIVE_CALLS=1 python -m ingest.universe --dry-run
    python -m ingest.universe --dry-run --offline   # static file only, no network

D14: S&P 500 + Nasdaq 100 constituents (~550 unique), filtered to average volume
above 1M and an active options chain, plus `config/watchlist.txt` -> ~500 names.

D29: FMP's free tier 402-gates BOTH constituents endpoints, so the builder falls
back to `config/constituents_static.txt`, parses that file's `AS_OF` header, and
logs a warning naming the date and its age. A stale list must never be mistaken
for a live pull.

Everything above `build_universe` is pure and unit-tested against fixtures with
no network: `parse_static_constituents`, `merge_constituents`,
`apply_liquidity_filter`, `average_volume`, `check_universe_size`.

Symbols are never deleted. A name that leaves the index or stops passing the
filter is marked `is_active = false`, because ideas, Shadow Book rows,
fundamentals, events and news all reference `tickers.symbol` and a delete would
cascade real history away.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date

from core import clock, config, db
from core.budget import BudgetExhausted
from core.jobs import job_run
from ingest.fmp_client import FMPClient, FMPPlanError

log = logging.getLogger(__name__)

JOB_NAME = "universe"

# Beyond this the static constituents file is probably describing a different
# market than the one we are trading. Warn, do not fail: a slightly stale list
# is still better than no universe, and the operator is told.
STATIC_FILE_STALE_AFTER_DAYS = 120

# Quote endpoint accepts a comma-separated batch; this many per call.
QUOTE_BATCH_SIZE = 100


# ══════════════════════════════════════════════════════════════════════════
# Pure logic — no I/O above build_universe()
# ══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Candidate:
    """A symbol under consideration, with everything the filter needs."""

    symbol: str
    company_name: str | None = None
    exchange: str | None = None
    sector: str | None = None
    industry: str | None = None
    market_cap: float | None = None

    in_sp500: bool = False
    in_nasdaq100: bool = False
    in_watchlist: bool = False

    avg_volume: int | None = None
    # When avg_volume was verified. Orders the leftover D29 call budget so a
    # converged universe refreshes its stalest names instead of idling.
    volume_asof: date | None = None
    last_price: float | None = None
    # None = not yet determined. ingest/schwab_chains.py sets this from whether
    # Schwab returned a chain, so the D14 options gate ABSTAINS until then
    # rather than guessing in either direction.
    has_options: bool | None = None


@dataclass
class FilterResult:
    kept: list[Candidate] = field(default_factory=list)
    rejected: list[tuple[Candidate, str]] = field(default_factory=list)

    @property
    def reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _, reason in self.rejected:
            counts[reason] = counts.get(reason, 0) + 1
        return counts


@dataclass(frozen=True)
class StaticConstituents:
    """Parsed `config/constituents_static.txt`."""

    sp500: list[dict]
    nasdaq100: list[dict]
    as_of: date | None
    path: str

    @property
    def total_rows(self) -> int:
        return len(self.sp500) + len(self.nasdaq100)

    def age_days(self, today: date) -> int | None:
        return None if self.as_of is None else (today - self.as_of).days


class UniverseError(RuntimeError):
    """The universe could not be built from what the sources returned."""


def normalize_symbol(raw: str) -> str:
    """FMP uses dots for share classes (BRK.B); Schwab uses slashes (BRK/B).

    The DB stores the FMP/dot form as canonical because that is what every FMP
    endpoint keys on. Translation to the broker's form happens in the Schwab
    client, at that boundary, and nowhere else.
    """
    return raw.strip().upper().replace(" ", "")


def parse_static_constituents(
    text: str, *, path: str = "constituents_static.txt"
) -> StaticConstituents:
    """Parse the D29 offline fallback: `SYMBOL,INDEX` rows plus an AS_OF header.

    The AS_OF date is parsed so the caller can warn about the file's age. A
    missing or unparseable header is NOT fatal — the symbols are still usable
    and a loud warning is more useful than a refusal — but it is reported.
    """
    as_of: date | None = None
    sp500: list[dict] = []
    ndx: list[dict] = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            # `# AS_OF: 2026-10-20`
            marker = stripped.lstrip("#").strip()
            if marker.upper().startswith("AS_OF:"):
                value = marker.split(":", 1)[1].strip()
                try:
                    as_of = date.fromisoformat(value[:10])
                except ValueError:
                    as_of = None
            continue

        parts = [p.strip() for p in stripped.split(",")]
        if len(parts) != 2:
            continue
        symbol, index = normalize_symbol(parts[0]), parts[1].lower()
        if not symbol:
            continue
        row = {"symbol": symbol}
        if index in ("sp500", "sp_500", "spx"):
            sp500.append(row)
        elif index in ("ndx", "ndx100", "nasdaq100", "nasdaq_100"):
            ndx.append(row)
        # An unrecognised index token is skipped rather than guessed at.

    return StaticConstituents(sp500=sp500, nasdaq100=ndx, as_of=as_of, path=path)


def merge_constituents(
    sp500: list[dict],
    nasdaq100: list[dict],
    watchlist: list[str],
) -> list[Candidate]:
    """Union the two index lists with the watchlist, carrying membership flags.

    A symbol in both indices produces ONE Candidate with both flags set, which
    is what makes "~550 unique" out of 500 + 100.
    """
    merged: dict[str, Candidate] = {}

    def absorb(rows: list[dict], **flags: bool) -> None:
        for row in rows:
            raw = row.get("symbol")
            if not raw:
                continue
            symbol = normalize_symbol(str(raw))
            if not symbol:
                continue
            existing = merged.get(symbol)
            if existing is None:
                merged[symbol] = Candidate(
                    symbol=symbol,
                    company_name=row.get("name") or row.get("companyName"),
                    sector=row.get("sector"),
                    industry=row.get("subSector") or row.get("industry"),
                    **flags,
                )
            else:
                merged[symbol] = replace(
                    existing,
                    company_name=existing.company_name or row.get("name") or row.get("companyName"),
                    sector=existing.sector or row.get("sector"),
                    industry=existing.industry or row.get("subSector") or row.get("industry"),
                    **flags,
                )

    absorb(sp500, in_sp500=True)
    absorb(nasdaq100, in_nasdaq100=True)

    for raw in watchlist:
        symbol = normalize_symbol(raw)
        if not symbol:
            continue
        existing = merged.get(symbol)
        merged[symbol] = (
            replace(existing, in_watchlist=True)
            if existing
            else Candidate(symbol=symbol, in_watchlist=True)
        )

    return sorted(merged.values(), key=lambda c: c.symbol)


def average_volume(bars: list[dict], lookback: int) -> int | None:
    """Mean volume over the most recent `lookback` bars, or None if too few.

    Partial history is a rejection rather than a smaller-window average: a name
    with six sessions of data is a recent listing, and a six-day average says
    nothing about whether we can get out of a position in it.
    """
    volumes = [b.get("volume") for b in bars[:lookback]]
    usable = [int(v) for v in volumes if isinstance(v, int | float)]
    if len(usable) < lookback:
        return None
    return int(sum(usable) / len(usable))


def apply_liquidity_filter(
    candidates: list[Candidate],
    cfg: config.Liquidity,
) -> FilterResult:
    """Apply the D14 liquidity gate.

    Rules, in the order checked:

    * Watchlist names are exempt when `exempt_watchlist` (D14: an explicit
      human override — if you put it there, you want it scored).
    * Average volume must be STRICTLY above `min_avg_volume`. D14 says ">1M".
    * Unknown average volume is a rejection, not a pass. An unverified name in
      a universe built for liquidity defeats the point of the filter.
    * Price must be at or above `min_price`.
    * `require_options` rejects only a definite `False`. `None` means nobody has
      checked yet — `ingest/schwab_chains.py` sets the flag from whether Schwab
      returned a chain — so this gate ABSTAINS until the first successful chain
      run and enforces from then on.
    """
    result = FilterResult()

    for candidate in candidates:
        if cfg.exempt_watchlist and candidate.in_watchlist:
            result.kept.append(candidate)
            continue

        if candidate.avg_volume is None:
            result.rejected.append((candidate, "no volume data"))
            continue
        if candidate.avg_volume <= cfg.min_avg_volume:
            result.rejected.append((candidate, "avg volume below minimum"))
            continue
        if candidate.last_price is not None and candidate.last_price < cfg.min_price:
            result.rejected.append((candidate, "price below minimum"))
            continue
        if cfg.require_options and candidate.has_options is False:
            result.rejected.append((candidate, "no options chain"))
            continue

        result.kept.append(candidate)

    return result


def check_universe_size(kept: list[Candidate], maximum: int) -> None:
    """Guardrail: a malformed provider response must fail, not ingest.

    D14 expects ~500 names. Ten thousand means something returned a list that
    is not an index, and writing it would poison every downstream job. Zero
    means writing it would deactivate the entire universe.
    """
    if len(kept) > maximum:
        raise UniverseError(
            f"universe has {len(kept)} symbols, above the configured "
            f"maintenance.max_universe_size of {maximum}. This usually means a "
            f"provider response was not what we expected; refusing to write."
        )
    if not kept:
        raise UniverseError("universe is empty; refusing to deactivate everything")


def _chunks(items: Sequence[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


# ══════════════════════════════════════════════════════════════════════════
# I/O
# ══════════════════════════════════════════════════════════════════════════


def load_static_constituents(path) -> StaticConstituents:
    from pathlib import Path

    file = Path(path)
    if not file.is_absolute():
        file = config.CONFIG_DIR.parent / file
    if not file.exists():
        raise UniverseError(
            f"static constituents file not found: {file}. D29 needs it while FMP's "
            f"constituents endpoints are plan-gated; see the file's own header for "
            f"how to populate it."
        )
    return parse_static_constituents(file.read_text(), path=str(file))


def fetch_constituents(
    client: FMPClient | None, cfg: config.UniverseConfig, *, offline: bool = False
) -> tuple[list[dict], list[dict], str]:
    """Index constituents from FMP, falling back to the static file (D29).

    Returns (sp500, nasdaq100, source_description). The fallback is taken when
    FMP answers 402 on either endpoint — the paths are correct, the plan does
    not cover them — and never silently: the warning names the file's AS_OF date
    and its age in days.
    """
    static_requested = offline or client is None

    if not static_requested:
        try:
            sp500 = client.sp500_constituents() if cfg.sources.sp500 else []
            ndx = client.nasdaq100_constituents() if cfg.sources.nasdaq100 else []
            log.info("constituents from FMP: sp500=%d nasdaq100=%d", len(sp500), len(ndx))
            return sp500, ndx, "fmp"
        except FMPPlanError as exc:
            if not cfg.sources.allow_static_fallback:
                raise
            log.warning("FMP constituents are plan-gated (%s); using the static file", exc)

    if not cfg.sources.allow_static_fallback and static_requested and not offline:
        raise UniverseError("static fallback is disabled in config but FMP is unavailable")

    static = load_static_constituents(cfg.sources.static_fallback_file)
    today = clock.session_date()
    age = static.age_days(today)

    if static.total_rows == 0:
        raise UniverseError(
            f"{static.path} contains no SYMBOL,INDEX rows. It must be populated before "
            f"the universe can be built (D29)."
        )

    if static.as_of is None:
        log.warning(
            "STATIC CONSTITUENTS: %s has no parseable AS_OF header — cannot tell how "
            "stale this list is. %d rows loaded.",
            static.path,
            static.total_rows,
        )
    elif age is not None and age > STATIC_FILE_STALE_AFTER_DAYS:
        log.warning(
            "STATIC CONSTITUENTS ARE %d DAYS OLD (AS_OF %s, %d rows from %s). Past the "
            "%d-day staleness threshold: index membership has almost certainly changed. "
            "Refresh the file, or upgrade the FMP plan to use the live endpoints (D29).",
            age,
            static.as_of,
            static.total_rows,
            static.path,
            STATIC_FILE_STALE_AFTER_DAYS,
        )
    else:
        log.warning(
            "using STATIC constituents: AS_OF %s (%d days old), %d rows from %s. This is "
            "not a live pull — FMP's constituents endpoints are plan-gated (D29).",
            static.as_of,
            age,
            static.total_rows,
            static.path,
        )

    return static.sp500, static.nasdaq100, f"static:{static.as_of or 'undated'}"


def enrich_with_liquidity(
    client: FMPClient,
    candidates: list[Candidate],
    *,
    lookback_days: int,
    # Kept for tests that want a hard local cap; production relies on the
    # SHARED day budget enforced inside FMPClient.get() (D29).
    call_budget: int | None = None,
    known_volumes: Mapping[str, int] | None = None,
    volume_asof: Mapping[str, date] | None = None,
    today: date | None = None,
    on_error: Callable[[str, Exception], None] | None = None,
) -> list[Candidate]:
    """Attach average volume, last price, exchange and market cap.

    TWO PASSES on purpose. The batched quote endpoint gives price, exchange and
    market cap for the whole universe in a handful of calls; the exact
    `lookback_days` average needs one historical call per symbol, because FMP's
    own `avgVolume` uses an undocumented window and D14 specifies 20 sessions.

    The pass stops when the SHARED day budget is exhausted (D29) — the client
    raises on an empty grant, so this does not carry its own cap. Without
    `known_volumes` that is destructive on the free tier: 519 symbols against a
    250-call budget would leave 269 with `avg_volume=None`, the filter would
    reject them, and the next run would do the same to a different 269 — the
    universe would oscillate instead of converging.

    So `known_volumes` seeds each candidate from what `tickers` already holds,
    and the budgeted pass visits UNKNOWN symbols first. Successive runs then
    expand coverage monotonically, and a symbol verified last week stays in the
    universe this week. Symbols still unknown after the budget runs out keep
    `avg_volume=None` and are rejected — unverified is never silently admitted.

    Once every symbol IS known, the budget would otherwise go unspent while the
    stored volumes quietly aged. Leftover calls therefore refresh the STALEST
    known volumes first, ordered by `volume_asof` with never-dated symbols
    ahead of dated ones. A converged universe keeps spending its budget and the
    oldest verification date advances every run.
    """
    known_volumes = known_volumes or {}
    volume_asof = volume_asof or {}
    today = today or clock.session_date()

    by_symbol: dict[str, Candidate] = {}
    for c in candidates:
        if c.avg_volume is not None:
            by_symbol[c.symbol] = c
        else:
            by_symbol[c.symbol] = replace(
                c,
                avg_volume=known_volumes.get(c.symbol),
                volume_asof=volume_asof.get(c.symbol),
            )

    # Pass 1 — batched quotes for the cheap fields.
    for chunk in _chunks(sorted(by_symbol), QUOTE_BATCH_SIZE):
        for quote in client.quotes(chunk):
            symbol = normalize_symbol(str(quote.get("symbol", "")))
            candidate = by_symbol.get(symbol)
            if candidate is None:
                continue
            by_symbol[symbol] = replace(
                candidate,
                company_name=candidate.company_name or quote.get("name"),
                exchange=quote.get("exchange"),
                market_cap=quote.get("marketCap"),
                last_price=quote.get("price"),
            )

    # Pass 2 — the exact N-session average volume the config asks for.
    # Unknown symbols first, so a tight budget buys new coverage rather than
    # re-verifying names we already know about.
    unknown = sorted(s for s, c in by_symbol.items() if c.avg_volume is None)
    # Stalest first; a symbol with no recorded date sorts ahead of every dated
    # one, because "never verified in a way we can date" is maximally stale.
    refresh = sorted(
        (s for s, c in by_symbol.items() if c.avg_volume is not None),
        key=lambda s: (by_symbol[s].volume_asof or date.min, s),
    )
    if known_volumes:
        oldest = by_symbol[refresh[0]].volume_asof if refresh else None
        log.info(
            "volume: %d known from `tickers` (stalest %s), %d to look up",
            len(refresh),
            oldest or "undated",
            len(unknown),
        )

    spent = 0
    declined = 0
    for symbol in [*unknown, *refresh]:
        if call_budget is not None and spent >= call_budget:
            declined = len(unknown) + len(refresh) - spent
            break
        try:
            bars = client.daily_bars(symbol, days=lookback_days)
        except BudgetExhausted as exc:
            # DECLINED SPEND, not a failure (D29). Stop cleanly and report.
            declined = len(unknown) + len(refresh) - spent
            log.info("volume lookups stopped on the shared day budget: %s", exc)
            break
        except Exception as exc:  # one bad symbol must not kill the universe
            log.warning("volume lookup failed for %s: %s", symbol, exc)
            if on_error:
                on_error(symbol, exc)
            spent += 1
            continue
        spent += 1
        fresh = average_volume(bars, lookback_days)
        # Keep a previously-known value if this pull came back short: a thin
        # response is not evidence the name became illiquid. The date only
        # advances when a real figure was obtained, so a run of empty responses
        # cannot make a stale volume look freshly checked.
        if fresh is not None:
            by_symbol[symbol] = replace(by_symbol[symbol], avg_volume=fresh, volume_asof=today)
        elif by_symbol[symbol].avg_volume is None:
            by_symbol[symbol] = replace(by_symbol[symbol], avg_volume=None)

    if declined:
        still_unknown = sum(1 for c in by_symbol.values() if c.avg_volume is None)
        if still_unknown:
            log.warning(
                "stopped volume lookups after %d calls; %d symbol(s) still have no volume "
                "data and will be filtered out. The next run prioritises them, so coverage "
                "expands each time (%d lookups declined today).",
                spent,
                still_unknown,
                declined,
            )
        else:
            log.info(
                "budget reached while refreshing already-known symbols (%d declined); every "
                "symbol has a volume, so nothing is filtered out",
                declined,
            )

    return sorted(by_symbol.values(), key=lambda c: c.symbol)


UPSERT = """
    INSERT INTO tickers (symbol, company_name, exchange, sector, industry,
                         market_cap, in_sp500, in_nasdaq100, in_watchlist,
                         avg_volume_20d, avg_volume_asof, has_options, passes_liquidity,
                         is_active, deactivated_on, last_seen_on, source)
    VALUES (%(symbol)s, %(company_name)s, %(exchange)s, %(sector)s, %(industry)s,
            %(market_cap)s, %(in_sp500)s, %(in_nasdaq100)s, %(in_watchlist)s,
            %(avg_volume)s, %(volume_asof)s, %(has_options)s, TRUE,
            TRUE, NULL, %(session_date)s, %(source)s)
    ON CONFLICT (symbol) DO UPDATE SET
        company_name     = COALESCE(EXCLUDED.company_name, tickers.company_name),
        exchange         = COALESCE(EXCLUDED.exchange, tickers.exchange),
        sector           = COALESCE(EXCLUDED.sector, tickers.sector),
        industry         = COALESCE(EXCLUDED.industry, tickers.industry),
        market_cap       = COALESCE(EXCLUDED.market_cap, tickers.market_cap),
        in_sp500         = EXCLUDED.in_sp500,
        in_nasdaq100     = EXCLUDED.in_nasdaq100,
        in_watchlist     = EXCLUDED.in_watchlist,
        avg_volume_20d   = COALESCE(EXCLUDED.avg_volume_20d, tickers.avg_volume_20d),
        avg_volume_asof  = COALESCE(EXCLUDED.avg_volume_asof, tickers.avg_volume_asof),
        has_options      = COALESCE(EXCLUDED.has_options, tickers.has_options),
        passes_liquidity = TRUE,
        is_active        = TRUE,
        deactivated_on   = NULL,
        last_seen_on     = EXCLUDED.last_seen_on,
        source           = EXCLUDED.source
"""


def upsert_tickers(kept: list[Candidate], *, session_date: date, source: str) -> int:
    """Write the universe. Re-runnable: upserts, never duplicates.

    `has_options` uses COALESCE so a universe rebuild never erases what
    schwab_chains discovered — the chain job is the only writer that knows.
    """
    rows = [
        {
            "symbol": c.symbol,
            "company_name": c.company_name,
            "exchange": c.exchange,
            "sector": c.sector,
            "industry": c.industry,
            "market_cap": c.market_cap,
            "in_sp500": c.in_sp500,
            "in_nasdaq100": c.in_nasdaq100,
            "in_watchlist": c.in_watchlist,
            "avg_volume": c.avg_volume,
            "volume_asof": c.volume_asof,
            "has_options": c.has_options,
            "session_date": session_date,
            "source": source[:64],
        }
        for c in kept
    ]
    return db.executemany(UPSERT, rows)


def deactivate_absent(kept_symbols: set[str], *, session_date: date, grace_days: int) -> int:
    """Deactivate names absent for longer than the grace period.

    Deactivate, NEVER delete: `ideas`, `fundamentals`, `events`,
    `news_articles` and `corporate_actions` all FK to `tickers.symbol` with
    ON DELETE CASCADE, so a delete would silently take real history with it.
    (tests/test_schema_db.py proves that cascade.)
    """
    if not kept_symbols:
        return 0
    return db.execute(
        """
        UPDATE tickers
        SET    is_active        = FALSE,
               deactivated_on   = %(session_date)s,
               passes_liquidity = FALSE
        WHERE  is_active
          AND  NOT (symbol = ANY(%(kept)s))
          AND  last_seen_on < %(session_date)s - make_interval(days => %(grace)s)
        """,
        {"session_date": session_date, "kept": list(kept_symbols), "grace": grace_days},
    )


def stored_volumes() -> tuple[dict[str, int], dict[str, date]]:
    """Average volumes already in `tickers`, and when each was verified.

    Seeds the budgeted enrichment pass so a tight D29 call budget expands
    coverage instead of churning it, and so leftover calls can target the
    stalest names.
    """
    volumes: dict[str, int] = {}
    asof: dict[str, date] = {}
    for row in db.fetch_all(
        "SELECT symbol, avg_volume_20d, avg_volume_asof FROM tickers "
        "WHERE avg_volume_20d IS NOT NULL"
    ):
        volumes[row["symbol"]] = int(row["avg_volume_20d"])
        if row["avg_volume_asof"] is not None:
            asof[row["symbol"]] = row["avg_volume_asof"]
    return volumes, asof


def build_universe(
    client: FMPClient | None = None, *, dry_run: bool = False, offline: bool = False
) -> tuple[FilterResult, str]:
    """Pull, filter, and (unless dry_run) write the universe."""
    cfg = config.universe()
    day = clock.session_date()
    if client is None and not offline:
        client = FMPClient()

    sp500, ndx, source = fetch_constituents(client, cfg, offline=offline)
    watchlist = config.load_watchlist()
    candidates = merge_constituents(sp500, ndx, watchlist)
    log.info("merged to %d unique symbols (watchlist %d)", len(candidates), len(watchlist))

    if offline:
        # No network: nothing can be enriched, so the filter has no volume to
        # judge. Report the merge and stop rather than pretend to have filtered.
        result = FilterResult(kept=candidates)
        return result, source

    candidates = enrich_with_liquidity(
        client,
        candidates,
        lookback_days=cfg.liquidity.avg_volume_lookback_days,
        **dict(zip(("known_volumes", "volume_asof"), stored_volumes(), strict=True)),
        today=day,
    )
    result = apply_liquidity_filter(candidates, cfg.liquidity)
    check_universe_size(result.kept, cfg.maintenance.max_universe_size)

    if dry_run:
        return result, source

    written = upsert_tickers(result.kept, session_date=day, source=source)
    log.info("upserted %d tickers", written)
    if cfg.maintenance.deactivate_missing:
        dropped = deactivate_absent(
            {c.symbol for c in result.kept},
            session_date=day,
            grace_days=cfg.maintenance.deactivate_after_absent_days,
        )
        log.info("deactivated %d absent symbols", dropped)
    return result, source


def _report(result: FilterResult, source: str, *, wrote: bool, offline: bool) -> None:
    total = len(result.kept) + len(result.rejected)
    verb = "wrote" if wrote else "would write"
    print(f"\nuniverse [{source}]: {verb} {len(result.kept)} of {total} considered")
    for reason, count in sorted(result.reasons.items(), key=lambda kv: -kv[1]):
        print(f"  rejected {count:>4}  {reason}")

    if offline:
        print("  note: --offline merged the static file only; no liquidity filtering ran")
        return

    unknown = sum(1 for c in result.kept if c.has_options is None)
    if unknown:
        print(
            f"  note: options availability unknown for {unknown} symbol(s) — the D14 "
            f"options gate ABSTAINS for these. ingest/schwab_chains.py sets the flag "
            f"from whether Schwab returned a chain, so the gate starts enforcing after "
            f"the first successful chain run."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="parse the static constituents file only; make no network calls",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.dry_run or args.offline:
        result, source = build_universe(dry_run=True, offline=args.offline)
        _report(result, source, wrote=False, offline=args.offline)
        return 0

    # Not a D7 snapshot job, so is_critical stays False: a failed rebuild leaves
    # yesterday's universe in place, which is degraded but usable.
    with job_run(JOB_NAME, triggered_by="manual") as run:
        result, source = build_universe()
        run.add_rows(len(result.kept))
        run.note(
            source=source,
            rejected=result.reasons,
            total_considered=len(result.kept) + len(result.rejected),
        )
        _report(result, source, wrote=True, offline=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
