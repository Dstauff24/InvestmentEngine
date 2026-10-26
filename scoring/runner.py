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

from core import clock, config, db, settings
from decision.book import LADDER_STATES, load_book
from decision.idea_builder import Idea, Rejection, SlotAssignment, assign_slots
from features import catalyst as catalyst_features
from features import fundamental as fundamental_features
from features import sentiment as sentiment_features
from features import technical as technical_features
from scoring.composite import ScoredSymbol, UniverseRun, composite_score, rank_universe
from scoring.pillars import PillarScore, PillarScores, score_all
from scoring.recipes import evaluate_all
from shadow_book.logger import regime_at

log = logging.getLogger(__name__)

__all__ = [
    "SectorCache",
    "abstaining_pillars",
    "build_cards",
    "candidate_rows",
    "record_candidates",
    "record_daily_stats",
    "score_symbol",
    "score_universe",
    "top_candidates",
]


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
        if len(members) < config.universe().sector.min_members:
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
        scope = config.universe().sector
        series = technical_features.sector_composite(
            frames,
            min_member_fraction=scope.min_member_fraction,
            min_members=scope.min_members,
        )
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
    events = catalyst_features.load_events(symbol, as_of=as_of)
    # The reaction window is anchored on the last KNOWABLE report, so the beat
    # and the price move always describe the same quarter — and so no measure
    # can read a bar that closed before the news was public.
    report = catalyst_features.last_knowable_earnings(events, as_of=as_of)
    # The EFFECTIVE date and time (D49): the later of the two providers' dates,
    # with the release time only where it belongs to the winning date.
    anchor = catalyst_features.effective_report(report) if report else (None, "unknown")
    technical = technical_features.compute(
        symbol,
        bars,
        sector_closes=sector_closes,
        reported_on=anchor[0],
        release_time=anchor[1],
    )

    fundamental = fundamental_features.compute(
        symbol,
        fundamental_features.load_fundamentals(symbol, as_of=as_of),
        fundamental_features.load_estimate_snapshots(symbol, as_of=as_of),
        as_of=as_of,
    )
    catalyst = catalyst_features.compute(
        symbol,
        events,
        catalyst_features.load_insider_purchases(symbol, as_of=as_of),
        as_of=as_of,
        series_depth=catalyst_features.insider_series_depth(symbol, as_of=as_of),
        # PREFER OUR OWN PRE-REPORT CONSENSUS. The vendor's `epsEstimated` rows
        # are mutable (one carried `lastUpdated` sixteen months after the
        # report), so a beat priced against them cannot be verified as
        # point-in-time. A snapshot this engine wrote on a date can. None for
        # every report before 2026-10-21, where the fallback is the vendor
        # figure and `surprise.estimate_source` says so.
        own_estimate=_own_estimate(symbol, events, as_of=as_of),
    )
    sentiment = sentiment_features.compute(
        symbol,
        sentiment_features.load_articles(symbol, as_of=as_of),
        as_of=as_of,
        coverage_start=coverage_start,
    )

    pillars = score_all(symbol, technical, fundamental, catalyst, sentiment)
    score, reason = composite_score(pillars)
    return ScoredSymbol(
        symbol=symbol,
        pillars=pillars,
        composite=score,
        abstain_reason=reason,
        # Recipes take FEATURES, not scores: "does this setup exist" is a
        # question about the measurements, while "do the pillars agree" is
        # D12's separate tiering question.
        recipes=evaluate_all(technical, fundamental, catalyst),
    )


def score_universe(
    symbols: list[str] | None = None,
    *,
    as_of: date | None = None,
    record_stats: bool = True,
) -> UniverseRun:
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
    # SLOTS BEFORE STATS. `surfaced` is now the output of slot assignment
    # (D42, amended), and `daily_scoring_stats.surfaced_count` plus
    # `daily_candidates.surfaced` both read it — recording them first would
    # have written zeros for every day.
    run.assignment = assign_daily_slots(run, as_of=day)
    if record_stats:
        record_daily_stats(run, day=day)
        record_candidates(run, day=day)
    log.info(
        "scored %d: %d tier A, %d tier B, %d abstained; pillars %s",
        len(scored),
        len(run.tier_a),
        len(run.tier_b),
        len(run.abstained),
        run.mask_histogram(),
    )
    return run


STATS_UPSERT = """
    INSERT INTO daily_scoring_stats (
        session_date, symbols_scored, symbols_abstained, tier_a_count, tier_b_count,
        surfaced_count, not_surfaced_count, mask_histogram,
        composite_min, composite_p25, composite_median, composite_p75,
        composite_p90, composite_max,
        tier_a_min_composite, tier_b_min_composite, max_ideas_per_day, recipe_counts
    ) VALUES (
        %(session_date)s, %(symbols_scored)s, %(symbols_abstained)s, %(tier_a_count)s,
        %(tier_b_count)s, %(surfaced_count)s, %(not_surfaced_count)s,
        %(mask_histogram)s, %(composite_min)s, %(composite_p25)s, %(composite_median)s,
        %(composite_p75)s, %(composite_p90)s, %(composite_max)s,
        %(tier_a_min_composite)s, %(tier_b_min_composite)s, %(max_ideas_per_day)s,
        %(recipe_counts)s
    )
    -- One row per session date: a day's scoring is a function of the data as
    -- it stood, not an append-only history, so a re-run replaces it.
    ON CONFLICT (session_date) DO UPDATE SET
        symbols_scored     = EXCLUDED.symbols_scored,
        symbols_abstained  = EXCLUDED.symbols_abstained,
        tier_a_count       = EXCLUDED.tier_a_count,
        tier_b_count       = EXCLUDED.tier_b_count,
        surfaced_count     = EXCLUDED.surfaced_count,
        not_surfaced_count = EXCLUDED.not_surfaced_count,
        mask_histogram     = EXCLUDED.mask_histogram,
        composite_min      = EXCLUDED.composite_min,
        composite_p25      = EXCLUDED.composite_p25,
        composite_median   = EXCLUDED.composite_median,
        composite_p75      = EXCLUDED.composite_p75,
        composite_p90      = EXCLUDED.composite_p90,
        composite_max      = EXCLUDED.composite_max,
        tier_a_min_composite = EXCLUDED.tier_a_min_composite,
        tier_b_min_composite = EXCLUDED.tier_b_min_composite,
        max_ideas_per_day  = EXCLUDED.max_ideas_per_day,
        recipe_counts      = EXCLUDED.recipe_counts,
        updated_at         = now()
"""


CANDIDATES_UPSERT = """
    INSERT INTO daily_candidates (
        session_date, symbol, rank, composite, percentile, scored_pillars,
        pillars_scored, tier, tier_reason, surfaced, recipe, risk_reward,
        no_plan_reason, refused_stage, refused_reason, gates_in_force, close_raw
    ) VALUES (
        %(session_date)s, %(symbol)s, %(rank)s, %(composite)s, %(percentile)s,
        %(scored_pillars)s, %(pillars_scored)s, %(tier)s, %(tier_reason)s,
        %(surfaced)s, %(recipe)s, %(risk_reward)s, %(no_plan_reason)s,
        %(refused_stage)s, %(refused_reason)s, %(gates_in_force)s, %(close_raw)s
    )
    ON CONFLICT (session_date, symbol) DO UPDATE SET
        rank           = EXCLUDED.rank,
        composite      = EXCLUDED.composite,
        percentile     = EXCLUDED.percentile,
        scored_pillars = EXCLUDED.scored_pillars,
        pillars_scored = EXCLUDED.pillars_scored,
        tier           = EXCLUDED.tier,
        tier_reason    = EXCLUDED.tier_reason,
        surfaced       = EXCLUDED.surfaced,
        recipe         = EXCLUDED.recipe,
        risk_reward    = EXCLUDED.risk_reward,
        no_plan_reason = EXCLUDED.no_plan_reason,
        refused_stage  = EXCLUDED.refused_stage,
        refused_reason = EXCLUDED.refused_reason,
        gates_in_force = EXCLUDED.gates_in_force,
        close_raw      = EXCLUDED.close_raw
"""

CLOSE_SQL = """
    SELECT close FROM prices_daily WHERE symbol = %s AND session_date = %s
"""

MAX_REASON_CHARS = 600


def candidate_rows(run: UniverseRun, *, day: date, cfg=None) -> list[dict]:
    """The day's top N by rank, as rows. Pure: no database, no clock.

    N is `ranking.max_ideas_per_day` — the same cut D42 uses for surfacing, so
    the rows are "what the engine would have shown if every one of them had a
    plan and cleared a gate", which is the population whose forward returns can
    say whether the composite predicts anything on its own.

    **RANKED, NOT QUALIFYING.** The cut is by rank over the whole universe, so
    a high score that fired no setup is kept — that case is the entire point. A
    cut on `tier is not None` would keep only the ideas the gates already liked
    and could never answer whether the gates are the thing adding value.
    """
    scoring_cfg = cfg or config.scoring()
    ranked = [s for s in run.scored if s.rank is not None]
    ranked.sort(key=lambda s: s.rank)
    top = ranked[: scoring_cfg.ranking.max_ideas_per_day]

    # THE NEAR-MISSES, wherever they rank (D42 amended). A candidate whose plan
    # existed and was declined by a LATER stage is the most informative row in
    # this table — on 2026-10-21 there were four of them against 305 symbols
    # with no plan at all, and the one idea that passed everything sat at rank
    # 51. Keeping only the top N by rank would hide exactly that.
    # Refusals AND D38's watch-only excess: both declined a slot, and the
    # table has to say which gate did it.
    refused = run.assignment.declined_by_symbol() if run.assignment else {}
    kept = {item.symbol for item in top}
    near_misses = [
        item
        for item in ranked
        if item.symbol not in kept
        and refused.get(item.symbol, None) is not None
        and refused[item.symbol].stage != "plan"
    ]
    # AND EVERY ACCEPTED IDEA, wherever it ranks. On 2026-10-21 the one idea
    # that passed every stage sat at rank 51, so a cut at the top N by rank
    # would have left the day's only call out of the table whose whole purpose
    # is relating a composite to what happened next.
    accepted = [item for item in ranked if item.surfaced and item.symbol not in kept]
    top = [*top, *near_misses, *accepted]

    gates = {
        "tier_a_min_composite": scoring_cfg.tiers.a.min_composite,
        "tier_b_min_composite": scoring_cfg.tiers.b.min_composite,
        "max_ideas_per_day": scoring_cfg.ranking.max_ideas_per_day,
        "pillar_agreement_min_score": scoring_cfg.pillar_agreement.min_score,
    }

    rows = []
    for item in top:
        best = item.best_plan
        rows.append(
            {
                "session_date": day,
                "symbol": item.symbol,
                "rank": item.rank,
                "composite": item.composite,
                "percentile": item.percentile,
                "scored_pillars": item.mask or None,
                "pillars_scored": item.pillars_scored,
                "tier": item.tier,
                "tier_reason": item.tier_reason or None,
                "surfaced": item.surfaced,
                "recipe": best.recipe if best else None,
                "risk_reward": round(best.plan.risk_reward, 4) if best else None,
                "no_plan_reason": None if best else _no_plan_reason(item),
                "refused_stage": (
                    None if item.surfaced else getattr(refused.get(item.symbol), "stage", None)
                ),
                "refused_reason": (
                    None if item.surfaced else getattr(refused.get(item.symbol), "reason", None)
                ),
                "gates_in_force": gates,
                "close_raw": None,
            }
        )
    return rows


def _no_plan_reason(item) -> str:
    """Why this score produced no trade plan.

    Every recipe's own refusal, joined — not just the first. A score of 78 with
    "not in an uptrend / no base / no knowable report" is a different fact from
    one whose single recipe refused on a gate, and the shape of these strings
    across a few hundred rows is what will say whether the recipes or the gates
    are what the composite keeps running into.
    """
    reasons = [f"{r.recipe}: {r.reason}" for r in item.recipes if r.reason]
    if not reasons:
        return "no recipe produced a result at all"
    joined = "; ".join(reasons)
    return joined if len(joined) <= MAX_REASON_CHARS else joined[: MAX_REASON_CHARS - 1] + "…"


def record_candidates(run: UniverseRun, *, day: date) -> int:
    """Persist the day's top-ranked candidates (D42 amended). Returns the count.

    Idempotent by (session_date, symbol): a re-run of a day replaces the
    scores, and deliberately does NOT touch the forward-return columns — those
    are filled once by `jobs/candidate_returns.py` and never rewritten, so a
    re-scored day cannot silently move a number a study already read.
    """
    import json

    rows = candidate_rows(run, day=day)
    for row in rows:
        close = db.fetch_one(CLOSE_SQL, (row["symbol"], day))
        row["close_raw"] = close["close"] if close else None
        row["gates_in_force"] = json.dumps(row["gates_in_force"])
        db.execute(CANDIDATES_UPSERT, row)
    log.info(
        "D42 candidates for %s: %d row(s) kept with their mask and refusal reason",
        day,
        len(rows),
    )
    return len(rows)


def record_daily_stats(run: UniverseRun, *, day: date) -> None:
    """D42: one aggregate row for the ideas that qualified but were not surfaced.

    The only record of what the engine ALMOST said. D39(3)'s paper phase tunes
    the Tier A/B gates, and without this a threshold change would be guesswork
    about the distribution just outside the cut.

    The gates in force are stored WITH the row, so a later threshold change
    cannot silently reinterpret today's distribution.
    """
    import json

    cfg = config.scoring()
    composites = sorted(s.composite for s in run.scored if s.composite is not None)
    qualifying = [s for s in run.scored if s.tier is not None]

    recipe_counts: dict[str, int] = {}
    for item in run.scored:
        for result in item.recipes:
            if result.triggered:
                recipe_counts[result.recipe] = recipe_counts.get(result.recipe, 0) + 1

    db.execute(
        STATS_UPSERT,
        {
            "session_date": day,
            "symbols_scored": len(composites),
            "symbols_abstained": len(run.abstained),
            "tier_a_count": len(run.tier_a),
            "tier_b_count": len(run.tier_b),
            "surfaced_count": len(run.surfaced),
            "not_surfaced_count": len(qualifying) - len(run.surfaced),
            "mask_histogram": json.dumps({str(k): v for k, v in run.mask_histogram().items()}),
            "composite_min": _quantile(composites, 0.0),
            "composite_p25": _quantile(composites, 0.25),
            "composite_median": _quantile(composites, 0.5),
            "composite_p75": _quantile(composites, 0.75),
            "composite_p90": _quantile(composites, 0.9),
            "composite_max": _quantile(composites, 1.0),
            "tier_a_min_composite": cfg.tiers.a.min_composite,
            "tier_b_min_composite": cfg.tiers.b.min_composite,
            "max_ideas_per_day": cfg.ranking.max_ideas_per_day,
            "recipe_counts": json.dumps(recipe_counts),
        },
    )
    log.info(
        "D42 stats for %s: %d qualifying, %d surfaced, %d rolled into the aggregate",
        day,
        len(qualifying),
        len(run.surfaced),
        len(qualifying) - len(run.surfaced),
    )


def _quantile(values: list[float], q: float) -> float | None:
    """Nearest-rank quantile. None on an empty population."""
    if not values:
        return None
    index = min(len(values) - 1, max(0, round(q * (len(values) - 1))))
    return round(values[index], 2)


IV_SQL = """
    SELECT iv_rank_252, history_days
    FROM   iv_daily
    WHERE  symbol = %s AND session_date <= %s
    ORDER  BY session_date DESC
    LIMIT  1
"""


def _context_for(
    item,
    *,
    as_of: date,
    sectors: dict,
    statuses,
    books: dict,
    account: float,
    regime: str | None = None,
) -> dict | None:
    """The per-symbol inputs every refusal stage needs. One symbol, one read.

    Called by `assign_slots` as it walks DOWN the ranking, so these reads
    happen only for the candidates actually considered — the cost is "how far
    the walk went", not "the whole qualifying universe".
    """
    best = item.best_plan
    if best is None:
        return None
    status = statuses.of(best.recipe)
    iv = db.fetch_one(IV_SQL, (item.symbol, as_of)) or {}
    card_events = catalyst_features.load_events(item.symbol, as_of=as_of)
    catalyst = catalyst_features.compute(
        item.symbol,
        card_events,
        [],
        as_of=as_of,
        own_estimate=_own_estimate(item.symbol, card_events, as_of=as_of),
    )
    return {
        "result": best,
        "catalyst": catalyst,
        # D45/D56: computed ONCE per run by the caller and passed down. It is a
        # fact about the market, not about the symbol, and reading the whole
        # benchmark series per candidate would be the same answer bought 400
        # times — the same reason `log_surfaced` computes it once.
        "regime": regime,
        "sector": sectors.get(item.symbol),
        "status": status,
        "book": books[status],
        "iv_rank": _float(iv.get("iv_rank_252")),
        "iv_history_days": iv.get("history_days"),
        "account_value": account,
    }


def assign_daily_slots(run: UniverseRun, *, as_of: date) -> SlotAssignment:
    """Fill the day's actionable slots (D42, amended). Reads the live books."""
    account = settings.shadow_account_value()
    sectors = sectors_by_symbol()
    statuses = config.scoring().promotion.status
    books = {state: load_book(state, account_value=account) for state in LADDER_STATES}
    regime = regime_at(as_of)
    assignment = assign_slots(
        run,
        build_context=lambda item: _context_for(
            item,
            as_of=as_of,
            sectors=sectors,
            statuses=statuses,
            books=books,
            account=account,
            regime=regime,
        ),
    )
    log.info(
        "D42 slots: %d accepted, %d refused, walked %d candidate(s) down the ranking",
        len(assignment.accepted),
        len(assignment.refused),
        assignment.walked,
    )
    return assignment


def build_ideas(run: UniverseRun, *, as_of: date) -> list[Idea | Rejection | str]:
    """Idea OBJECTS for the surfaced symbols (D42), or why each refused.

    Objects rather than rendered text, because the Shadow Book must log the
    idea while the digest must render it, and deriving one from the other's
    string would be parsing nobody should have to write. A plain `str` appears
    only for a symbol that surfaced with no plan at all.

    A rejection is RETURNED, not swallowed. "EXPD: R:R 1.4 below the 3:1 Tier B
    minimum" is the most useful line the engine can print about a symbol it
    ranked first and then declined — a silent omission would read as a bug.
    """
    assignment = run.assignment or assign_daily_slots(run, as_of=as_of)
    out: list[Idea | Rejection | str] = list(assignment.accepted)
    # Every candidate the walk declined, named with its stage — the line that
    # tells a reader why a high scorer did not become a call.
    out.extend(rejection for _, rejection in assignment.refused)
    return out


def build_cards(run: UniverseRun, *, as_of: date) -> list[str]:
    """`build_ideas`, rendered. What the digest and the CLI print."""
    return [render_idea(item) for item in build_ideas(run, as_of=as_of)]


def render_idea(item: Idea | Rejection | str) -> str:
    if isinstance(item, Idea):
        return item.card()
    if isinstance(item, Rejection):
        return f"{item.symbol}: REFUSED at {item.stage} — {item.reason}"
    return item


def top_candidates(run: UniverseRun, limit: int = 5) -> list[dict]:
    """The closest-ranked symbols, for a zero-plan digest to report (D1/D33).

    Taken from the ranked population rather than the surfaced one: on a day
    when nothing surfaced, "what came closest" is the only informative thing
    the engine can say, and it has to come from somewhere other than the empty
    surfaced list.
    """
    ranked = [s for s in run.scored if s.composite is not None]
    ranked.sort(key=lambda s: (-(s.composite or 0.0), s.symbol))
    out = []
    for item in ranked[:limit]:
        best = item.best_plan
        out.append(
            {
                "symbol": item.symbol,
                "composite": item.composite,
                "percentile": item.percentile,
                "mask": item.mask,
                "reason": (
                    item.tier_reason
                    if best is not None
                    else ((item.triggered and "no plan") or "no recipe fired")
                ),
            }
        )
    return out


def _own_estimate(symbol: str, events: list[dict], *, as_of: date) -> float | None:
    """Our own pre-report consensus for the quarter that last reported.

    Looked up only once the report is KNOWABLE, so this cannot be the thing
    that reveals an actual the release-time rule is withholding.
    """
    reported = [
        e
        for e in events
        if e.get("event_type") == "earnings"
        and catalyst_features.actual_is_knowable(e, as_of=as_of)
    ]
    if not reported:
        return None
    latest = max(reported, key=lambda e: e["event_date"])
    return catalyst_features.own_pre_report_estimate(symbol, reported_on=latest["event_date"])


def _float(value) -> float | None:
    return None if value is None else float(value)


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

    # D42: only SURFACED ideas become cards. Built here rather than in phase
    # two because a card needs the ladder state, the open book and the IV
    # history — state about the account, not about the symbol.
    cards = build_cards(run, as_of=clock.session_date())
    if cards:
        print("\n" + "=" * 72)
        print("IDEA CARDS")
        print("=" * 72)
        for card in cards:
            print()
            print(card)
    if not run.surfaced:
        print("  nothing today — which the engine is allowed to say (D1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
