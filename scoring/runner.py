"""The two-phase scoring pass over the universe (D33/D41).

PHASE ONE scores every symbol. PHASE TWO ranks the composites into percentiles
and applies the tiering gates. The split is forced by D33: a symbol's
percentile cannot be known until every other symbol has been scored, so a
single pass that tiered as it went would have to guess or re-walk.

SECTOR COMPOSITES ARE BUILT ONCE PER RUN AND CACHED. D33 makes relative
strength an explicit Pillar A input, which needs a sector index per symbol —
and there are ~11 sectors against ~433 symbols. Rebuilding a sector's
composite for each of its members would load the same forty price series forty
times; the cache turns ~433 sector builds into 11.

Nothing here writes ideas. Phase two produces scored, ranked, tiered symbols;
`decision/idea_builder.py` (step 7) turns the survivors into trade plans and
`shadow_book` records them with D41's abstention mask.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from core import clock, config, db
from features import catalyst as catalyst_features
from features import fundamental as fundamental_features
from features import sentiment as sentiment_features
from features import technical as technical_features
from scoring.composite import ScoredSymbol, UniverseRun, composite_score, rank_universe
from scoring.pillars import PillarScore, PillarScores, score_all

log = logging.getLogger(__name__)

__all__ = ["SectorCache", "abstaining_pillars", "score_symbol", "score_universe"]


def abstaining_pillars(symbol: str) -> PillarScores:
    """Four abstaining pillars, for a symbol that could not be scored at all."""
    return PillarScores(
        symbol=symbol,
        technical=PillarScore("A", None),
        fundamental=PillarScore("B", None),
        catalyst=PillarScore("C", None),
        sentiment=PillarScore("D", None),
    )


@dataclass
class SectorCache:
    """One equal-weighted composite per sector, built on first request.

    Keyed by sector NAME, not by symbol. A miss costs one load per member, so
    the first symbol in a sector pays for all of them and the other forty pay
    nothing.
    """

    as_of: date
    lookback_days: int = 400
    max_members: int = 40
    _composites: dict[str, pd.Series] = field(default_factory=dict)
    _members: dict[str, list[str]] = field(default_factory=dict)
    builds: int = 0
    hits: int = 0

    def composite(self, sector: str | None) -> pd.Series | None:
        """The sector's index, or None when it cannot be built.

        None rather than an empty series: `relative_strength_vs_sector` already
        abstains on a short series, and a sector with two liquid members is a
        sector whose "index" is one of the two stocks being measured.
        """
        if not sector:
            return None
        if sector in self._composites:
            self.hits += 1
            cached = self._composites[sector]
            # `cached or None` is WRONG on a Series: pandas refuses a boolean
            # context ("the truth value of a Series is ambiguous") and raises,
            # which killed every symbol in the first live run. Same class as
            # `Element or root` in the Form 4 parser — a container that looks
            # falsy-testable and is not.
            return None if cached.empty else cached

        members = technical_features.sector_members(sector, limit=self.max_members)
        self._members[sector] = members
        if len(members) < 3:
            # Not an index. Cached as empty so the decision is made once.
            log.info("sector %r has %d usable member(s); no composite", sector, len(members))
            self._composites[sector] = pd.Series(dtype=float)
            return None

        frames = {}
        for symbol in members:
            bars = technical_features.load_bars(
                symbol, as_of=self.as_of, lookback_days=self.lookback_days
            )
            if not bars.empty:
                frames[symbol] = bars
        series = technical_features.sector_composite(frames)
        self.builds += 1
        log.info(
            "built sector composite for %r from %d member(s), %d bars",
            sector,
            len(frames),
            len(series),
        )
        self._composites[sector] = series
        return None if series.empty else series

    def __str__(self) -> str:
        return f"{self.builds} sector composite(s) built, {self.hits} cache hit(s)"


SECTOR_SQL = "SELECT symbol, sector FROM tickers WHERE is_active AND passes_liquidity"


def sectors_by_symbol() -> dict[str, str | None]:
    return {r["symbol"]: r["sector"] for r in db.fetch_all(SECTOR_SQL)}


def score_symbol(
    symbol: str,
    *,
    as_of: date,
    sector_closes: pd.Series | None,
    coverage_start: date | None,
) -> ScoredSymbol:
    """PHASE ONE for one symbol. Loads its features and maps them to pillars."""
    bars = technical_features.load_bars(symbol, as_of=as_of)
    technical = technical_features.compute(symbol, bars, sector_closes=sector_closes)

    fundamental = fundamental_features.compute(
        symbol,
        fundamental_features.load_fundamentals(symbol, as_of=as_of),
        fundamental_features.load_estimate_snapshots(symbol, as_of=as_of),
        as_of=as_of,
    )
    catalyst = catalyst_features.compute(
        symbol,
        catalyst_features.load_events(symbol, as_of=as_of),
        catalyst_features.load_insider_purchases(symbol, as_of=as_of),
        as_of=as_of,
        series_depth=catalyst_features.insider_series_depth(symbol, as_of=as_of),
    )
    sentiment = sentiment_features.compute(
        symbol,
        sentiment_features.load_articles(symbol, as_of=as_of),
        as_of=as_of,
        coverage_start=coverage_start,
    )

    pillars = score_all(symbol, technical, fundamental, catalyst, sentiment)
    score, reason = composite_score(pillars)
    return ScoredSymbol(symbol=symbol, pillars=pillars, composite=score, abstain_reason=reason)


def score_universe(symbols: list[str] | None = None, *, as_of: date | None = None) -> UniverseRun:
    """Both phases. Returns every symbol scored, ranked and tiered."""
    day = as_of or clock.session_date()
    universe = symbols if symbols is not None else db.active_symbols()
    if not universe:
        raise RuntimeError("no active tickers; run `python -m ingest.universe` first")

    sectors = sectors_by_symbol()
    cache = SectorCache(as_of=day)
    # Read ONCE for the whole run: it is a fact about the engine, not a symbol.
    coverage_start = sentiment_features.news_coverage_start()

    log.info("phase one: scoring %d symbols as of %s", len(universe), day)
    scored: list[ScoredSymbol] = []
    for index, symbol in enumerate(universe, start=1):
        try:
            scored.append(
                score_symbol(
                    symbol,
                    as_of=day,
                    sector_closes=cache.composite(sectors.get(symbol)),
                    coverage_start=coverage_start,
                )
            )
        except Exception as exc:
            # One symbol's bad data must not cost the other 432. Recorded as a
            # full abstention — which is what it is, not a zero.
            #
            # An ALL-ABSTAINING PillarScores rather than `pillars=None`: the
            # first version used None and every consumer then needed a null
            # check, which `mask_histogram` promptly did not have. A symbol
            # that failed to score has four abstaining pillars, so saying so
            # keeps the invariant instead of punching a hole in it.
            log.warning("scoring failed for %s: %s", symbol, exc)
            scored.append(
                ScoredSymbol(
                    symbol=symbol,
                    pillars=abstaining_pillars(symbol),
                    composite=None,
                    abstain_reason=f"scoring error: {exc}",
                )
            )
        if index % 50 == 0:
            log.info("  %d/%d scored", index, len(universe))

    log.info("phase one done; %s", cache)
    log.info("phase two: ranking %d symbols (D33)", len(scored))
    rank_universe(scored)

    run = UniverseRun(scored=scored)
    log.info(
        "scored %d: %d tier A, %d tier B, %d abstained; pillars %s",
        len(scored),
        len(run.tier_a),
        len(run.tier_b),
        len(run.abstained),
        run.mask_histogram(),
    )
    return run


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--symbols", nargs="*", help="override the universe")
    parser.add_argument("--limit", type=int, help="score only the first N symbols")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    symbols = args.symbols
    if symbols is None and args.limit:
        symbols = db.active_symbols()[: args.limit]
    run = score_universe(symbols)

    print(f"\nscored {len(run.scored)} symbols as of {clock.session_date()}")
    print(f"  pillars scored -> count: {run.mask_histogram()}")
    print(f"  tier A: {len(run.tier_a)}   tier B: {len(run.tier_b)}")
    print(f"  abstained: {len(run.abstained)}")
    cap = config.scoring().ranking.max_ideas_per_day
    print(f"\nsurfaced (max {cap}/day by rank, D33):")
    for item in run.surfaced:
        print("  " + item.card_line())
    if not run.surfaced:
        print("  nothing today — which the engine is allowed to say (D1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
