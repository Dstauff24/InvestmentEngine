"""Walk-forward backtest of the recipes (D39(4) / O7).

    python -m backtest.engine --years 4
    python -m backtest.engine --symbols AAPL MSFT --start 2023-01-01
    python -m backtest.engine --sample 100 --workers 2 --out data/backtest.csv

**WHAT THIS MEASURES AND WHAT IT CANNOT.** It measures the RECIPES: does this
setup, with these levels, pay. It does not measure the composite, the tier
gates or D33's ranking, and not because they were forgotten — the fundamental
statements reach back five years but the estimate-revision, IV-rank, insider
and news series all start in October 2026, so a historical composite would be
a two-pillar number dressed as a four-pillar one. The technical, catalyst and
fundamental-statement skeleton is what five years of data can speak to; the
proprietary signals have to prove themselves forward.

**NO PORTFOLIO QUEUE.** Every signal is taken. D17's heat cap, the six-swing
limit and D38's sector guard are deliberately NOT applied, because expectancy
per recipe is a property of the recipe and applying the caps would measure the
QUEUE instead — which signal happened to arrive when a slot was free. Those
caps are what the paper phase tests.

**ONE OPEN TRADE PER SYMBOL PER RECIPE.** A setup persists for days, so without
this a single pullback would open a new identical trade every session it
remained valid and the same outcome would be counted five times.

**SURVIVORSHIP.** The universe is TODAY's constituents. Every name that was
dropped from the index, acquired or delisted over the window is absent, and
those are disproportionately the losers. The expectancy here is therefore an
upper bound, and the size of the bias is unmeasurable without a historical
constituents feed nobody in this stack sells. It is the single largest caveat
on every number this module prints.

Costs, next-day entry and the ambiguous-bar rule live in `backtest/costs.py`
and `backtest/resolve.py` — which calls the Shadow Book's own tracker so the
backtest and the live book can never disagree about what happened to a plan.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import pandas as pd

from backtest.costs import CostModel
from backtest.regime import RegimeSeries, load_benchmark, regime_series
from backtest.resolve import resolve_plan
from core import clock, config, db
from features import catalyst as catalyst_features
from features import fundamental as fundamental_features
from features import technical as technical_features
from scoring.recipes import evaluate_all
from scoring.recipes.base import TradePlan

log = logging.getLogger(__name__)

__all__ = [
    "BacktestResult",
    "ParamSet",
    "SimTrade",
    "SymbolHistory",
    "load_symbol_history",
    "run",
    "walk_symbol",
]

# Bars of history a symbol needs before it can be scored at all: the 200-day MA
# plus enough room for the 63-bar relative-strength window to mean something.
WARMUP_BARS = 260
# What `technical.compute` is given. Matches `features.technical.load_bars`'s
# own default so a backtest measures the same window the live engine does.
LOOKBACK_BARS = 400
# Position size for the cost model. Expectancy is reported in R, which is
# size-invariant for shares — this only scales commissions, and D44's sizing is
# the paper phase's job rather than the recipe's.
SIM_SHARES = 100.0


@dataclass(frozen=True)
class ParamSet:
    """One configuration under test. The baseline is a sweep of size one."""

    name: str
    cfg: config.ScoringConfig

    @property
    def structure_key(self) -> tuple:
        """What the FEATURES depend on, so a sweep recomputes them only when it
        has to. Gate-only parameters (the RSI band, the contraction ratio) do
        not appear here, which is what makes most of a sweep nearly free."""
        pullback = self.cfg.recipes.pullback_to_trend
        breakout = self.cfg.recipes.base_breakout
        return (
            pullback.ma_fast,
            pullback.ma_mid,
            pullback.ma_slow,
            pullback.rsi_period,
            breakout.min_base_weeks,
            breakout.max_base_depth_pct,
        )

    @classmethod
    def baseline(cls, cfg=None) -> ParamSet:
        return cls(name="baseline", cfg=cfg or config.scoring())


@dataclass(frozen=True)
class SimTrade:
    """One simulated call, resolved. The row every metric is computed from."""

    param_set: str
    symbol: str
    sector: str | None
    recipe: str
    called_on: date
    regime: str
    entry_low: float
    entry_high: float
    stop: float
    target_1: float
    target_2: float
    risk_reward: float
    outcome: str
    entered_on: date | None
    entry: float | None
    exit_price: float | None
    exit_on: date | None
    gross_r: float | None
    net_r: float | None
    days_held: int | None
    mfe: float | None
    mae: float | None
    hit_target_1: bool
    hit_target_2: bool

    @property
    def filled(self) -> bool:
        return self.entry is not None

    @property
    def closed(self) -> bool:
        return self.outcome in ("stopped", "target_1", "target_2", "timeout")


@dataclass
class SymbolHistory:
    """Everything one symbol's walk needs, read once.

    The point of loading it all up front is that the walk is then pure
    in-memory work: 1,000 simulation dates would otherwise be 3,000 database
    round trips per symbol.
    """

    symbol: str
    sector: str | None
    bars: pd.DataFrame
    dividends: list[dict]
    events: list[dict]
    fundamentals: list[dict]

    @property
    def usable(self) -> bool:
        return len(self.bars) > WARMUP_BARS


DIVIDENDS_SQL = """
    SELECT ex_date, cash_amount
    FROM   corporate_actions
    WHERE  symbol = %s AND action_type = 'cash_dividend' AND ex_date <= %s
    ORDER  BY ex_date
"""

EVENTS_SQL = """
    SELECT symbol, event_type, event_date, eps_estimate, eps_actual,
           revenue_estimate, revenue_actual, dividend_amount
    FROM   events
    WHERE  symbol = %s
    ORDER  BY event_date
"""

FUNDAMENTALS_SQL = """
    SELECT symbol, fiscal_date_ending, fiscal_period, revenue, gross_profit,
           operating_income, net_income, eps_diluted, gross_margin,
           operating_margin, net_margin, roe, roic, debt_to_equity,
           COALESCE(filing_date, accepted_date::date) AS known_on
    FROM   fundamentals
    WHERE  symbol = %s AND period_type = 'quarter'
      AND  COALESCE(filing_date, accepted_date::date) IS NOT NULL
    ORDER  BY fiscal_date_ending
"""


def load_symbol_history(symbol: str, *, as_of: date, sector: str | None = None) -> SymbolHistory:
    """Bars, dividends, earnings and filings for one symbol, whole.

    Bars come from `features.technical.load_bars` with a lookback long enough
    for the whole window, so the backtest reads the same split-adjusted series
    the live engine reads — including the read-time adjustment D24 defines.
    """
    bars = technical_features.load_bars(symbol, as_of=as_of, lookback_days=100_000)
    return SymbolHistory(
        symbol=symbol,
        sector=sector,
        bars=bars,
        dividends=db.fetch_all(DIVIDENDS_SQL, (symbol, as_of)),
        events=db.fetch_all(EVENTS_SQL, (symbol,)),
        fundamentals=db.fetch_all(FUNDAMENTALS_SQL, (symbol,)),
    )


# ── point-in-time slicing ───────────────────────────────────────────────────
#
# These mirror the `WHERE` clauses in `features/catalyst.py` and
# `features/fundamental.py`. Two implementations of a point-in-time rule is
# exactly the kind of duplication that drifts, so
# tests/test_backtest_db.py::test_the_in_memory_slicers_agree_with_the_loaders
# runs both against the same seeded database and compares.


def events_knowable(
    events: list[dict], *, as_of: date, forward_days: int = 120, backward_days: int = 120
) -> list[dict]:
    """Earnings and ex-dividend rows inside the radar window.

    Mirrors `catalyst.load_events`'s defaults. The forward half carries a mild
    lookahead the engine cannot avoid: an earnings date 90 days out was not
    necessarily ANNOUNCED 90 days out. It is bounded — dates inside the 5-day
    exclusion that actually gates a recipe were certainly published — but it is
    a leak, and it belongs in the report rather than in a comment nobody reads.
    """
    low, high = as_of - timedelta(days=backward_days), as_of + timedelta(days=forward_days)
    return [e for e in events if low <= _as_date(e["event_date"]) <= high]


def fundamentals_knowable(rows: list[dict], *, as_of: date, limit: int = 12) -> list[dict]:
    """Quarters PUBLISHED on or before `as_of` — `filing_date`, not period end.

    Mirrors `fundamental.load_fundamentals`. Keying on the period end was a
    real lookahead: a run standing on April 5 read a quarter filed April 30.
    """
    known = [r for r in rows if _as_date(r["known_on"]) <= as_of]
    return known[-limit:] if limit else known


def dividends_after(dividends: list[dict], *, after: date) -> list[dict]:
    """The ex-dividends a trade opened on `after` could run into (D19(2))."""
    return [d for d in dividends if _as_date(d["ex_date"]) > after]


# ── the walk ────────────────────────────────────────────────────────────────


def walk_symbol(
    history: SymbolHistory,
    *,
    param_sets: list[ParamSet],
    regimes: RegimeSeries,
    costs: CostModel,
    sector_index: pd.Series | None = None,
    start: date | None = None,
    end: date | None = None,
) -> list[SimTrade]:
    """Every simulated trade for one symbol, across every parameter set.

    SYMBOL-MAJOR, and all parameter sets inside one pass over the dates. The
    features are the expensive part (~14ms a call), so computing them once and
    showing them to every configuration is what makes a sweep affordable:
    sweeping a gate costs almost nothing, and only the structural parameters
    force a second feature pass.
    """
    bars = history.bars
    if not history.usable:
        return []

    sessions = list(bars["session_date"])
    trades: list[SimTrade] = []
    # (param set, recipe) -> the date its open trade stops blocking new ones.
    busy_until: dict[tuple[str, str], date] = {}
    by_structure: dict[tuple, list[ParamSet]] = {}
    for param_set in param_sets:
        by_structure.setdefault(param_set.structure_key, []).append(param_set)

    for index in range(WARMUP_BARS, len(sessions)):
        day = _as_date(sessions[index])
        if (start and day < start) or (end and day > end):
            continue

        window = bars.iloc[max(0, index - LOOKBACK_BARS + 1) : index + 1]
        sector_slice = None if sector_index is None else sector_index.loc[:day]
        visible = events_knowable(history.events, as_of=day)
        # Anchor both the beat and the price-reaction window on the one report
        # whose actual was public before this close. With no release time in
        # the data (no provider publishes one), that is the session after the
        # report — which is exactly the window the recipe judges.
        report = catalyst_features.last_knowable_earnings(visible, as_of=day)
        catalyst = catalyst_features.compute(
            history.symbol,
            visible,
            [],
            as_of=day,
        )
        fundamental = fundamental_features.compute(
            history.symbol,
            fundamentals_knowable(history.fundamentals, as_of=day),
            [],
            as_of=day,
        )

        for structure_key, sets in by_structure.items():
            technical = technical_features.compute(
                history.symbol,
                window,
                sector_closes=sector_slice,
                cfg=sets[0].cfg.recipes.pullback_to_trend,
                structure_cfg=sets[0].cfg.recipes.base_breakout,
                reported_on=report["event_date"] if report else None,
                release_time=(report or {}).get("event_time") or "unknown",
            )
            del structure_key
            for param_set in sets:
                for result in evaluate_all(technical, fundamental, catalyst, cfg=param_set.cfg):
                    if not result.triggered or result.plan is None:
                        continue
                    key = (param_set.name, result.recipe)
                    if key in busy_until and busy_until[key] >= day:
                        continue
                    trade = _simulate(
                        history,
                        param_set=param_set,
                        recipe=result.recipe,
                        plan=result.plan,
                        called_on=day,
                        costs=costs,
                        regime=regimes.tag(day),
                    )
                    if trade is None:
                        continue
                    trades.append(trade)
                    busy_until[key] = trade.exit_on or (
                        day + timedelta(days=result.plan.horizon_days[1])
                    )
    return trades


def _simulate(
    history: SymbolHistory,
    *,
    param_set: ParamSet,
    recipe: str,
    plan: TradePlan,
    called_on: date,
    costs: CostModel,
    regime: str,
) -> SimTrade | None:
    fill = resolve_plan(
        symbol=history.symbol,
        recipe=recipe,
        plan=plan,
        called_on=called_on,
        bars=history.bars,
        dividends=dividends_after(history.dividends, after=called_on),
        costs=costs,
        shares=SIM_SHARES,
    )
    if fill.outcome == "no_forward_data":
        # The plan was made on the last session we hold. Not a trade, and not a
        # loss either — the edge of the data is not an outcome.
        return None
    return SimTrade(
        param_set=param_set.name,
        symbol=history.symbol,
        sector=history.sector,
        recipe=recipe,
        called_on=called_on,
        regime=regime,
        entry_low=plan.entry_low,
        entry_high=plan.entry_high,
        stop=plan.stop,
        target_1=plan.target_1,
        target_2=plan.target_2,
        risk_reward=plan.risk_reward,
        outcome=fill.outcome,
        entered_on=fill.entered_on,
        entry=fill.entry,
        exit_price=fill.exit_price,
        exit_on=fill.exit_on,
        gross_r=fill.gross_r,
        net_r=fill.net_r,
        days_held=fill.days_held,
        mfe=fill.mfe,
        mae=fill.mae,
        hit_target_1=fill.hit_target_1,
        hit_target_2=fill.hit_target_2,
    )


# ── sector indices ──────────────────────────────────────────────────────────


def build_sector_indices(
    sectors: Iterable[str], *, as_of: date, max_members: int = 40
) -> dict[str, pd.Series]:
    """One equal-weighted index per sector, over the WHOLE window, built once.

    **MEMBERS COME FROM THE WHOLE UNIVERSE, NEVER FROM THE SYMBOLS BEING
    WALKED.** A sector index is a fact about the market, and building it from a
    60-symbol sweep sample gave sectors two members each — below
    `sector.min_members`, so `rs_vs_sector` abstained and `base_breakout`'s
    relative-strength gate refused everything. The sweep would have reported
    "no setup at any contraction ratio" and the real cause would have been the
    sample size. `sector_members` is the same call the live `SectorCache`
    makes, in the same volume order, so the index a backtest measures against
    is the one the engine uses.

    Built once and sliced per date rather than rebuilt as of each date. The
    per-member normalisation is anchored to the window start, so a slice equals
    an as-of build — with one difference worth naming: the member SET is fixed
    to the symbols with any data in the window, so the per-date fraction floor
    (D42's `min_member_fraction`) is computed against that set rather than
    against the members that had listed by each date.
    """
    scope = config.universe().sector
    indices: dict[str, pd.Series] = {}
    for sector in sorted(set(sectors)):
        members = technical_features.sector_members(sector, limit=max_members)
        if len(members) < scope.min_members:
            log.info("sector %r has %d member(s); no index", sector, len(members))
            continue
        frames = {}
        for symbol in members:
            bars = technical_features.load_bars(symbol, as_of=as_of, lookback_days=100_000)
            if not bars.empty:
                frames[symbol] = bars
        series = technical_features.sector_composite(
            frames,
            min_member_fraction=scope.min_member_fraction,
            min_members=scope.min_members,
        )
        if not series.empty:
            indices[sector] = series
            log.info("sector index %r: %d bars from %d member(s)", sector, len(series), len(frames))
    return indices


@dataclass
class BacktestResult:
    """Every trade, plus what it took to produce them."""

    trades: list[SimTrade] = field(default_factory=list)
    start: date | None = None
    end: date | None = None
    symbols: int = 0
    skipped_symbols: list[str] = field(default_factory=list)
    regimes: dict[str, int] = field(default_factory=dict)
    param_sets: list[str] = field(default_factory=list)

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame([asdict(t) for t in self.trades])

    def __str__(self) -> str:
        return (
            f"{len(self.trades)} simulated trade(s) across {self.symbols} symbol(s), "
            f"{self.start}..{self.end}"
        )


def run(
    *,
    symbols: list[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    years: int = 4,
    param_sets: list[ParamSet] | None = None,
    costs: CostModel | None = None,
    as_of: date | None = None,
    with_sectors: bool = True,
    workers: int = 1,
) -> BacktestResult:
    """The whole walk-forward.

    `workers` splits the symbol list across processes. The features cost ~14ms
    a call and a four-year walk over the universe is ~2 CPU-hours, which is
    worth splitting; symbols share no state, so the split needs no coordination
    beyond collecting the trades.
    """
    today = as_of or clock.session_date()
    end = end or today
    start = start or (end - timedelta(days=365 * years))
    universe = symbols if symbols is not None else db.active_symbols()
    sets = param_sets or [ParamSet.baseline()]
    model = costs or CostModel.from_config()

    regimes = regime_series(load_benchmark(as_of=today))
    sectors = {
        r["symbol"]: r["sector"]
        for r in db.fetch_all(
            "SELECT symbol, sector FROM tickers WHERE is_active AND passes_liquidity"
        )
    }
    indices: dict[str, pd.Series] = {}
    if with_sectors:
        # The sectors the WALKED symbols belong to, each built from every
        # active member of it — see `build_sector_indices`.
        indices = build_sector_indices(
            (sectors[s] for s in universe if sectors.get(s)), as_of=today
        )

    result = BacktestResult(
        start=start,
        end=end,
        regimes=regimes.histogram(),
        param_sets=[s.name for s in sets],
    )
    jobs = [
        _Job(
            symbol=symbol,
            sector=sectors.get(symbol),
            param_sets=sets,
            regimes=regimes,
            costs=model,
            sector_index=indices.get(sectors.get(symbol) or ""),
            start=start,
            end=end,
            as_of=today,
        )
        for symbol in universe
    ]

    if workers > 1:
        # FORK, not spawn: the sector indices and the parsed config are already
        # built in this process, and a forked child inherits them instead of
        # pickling ~11 price series per symbol. Symbols are independent — no
        # portfolio state crosses between them, which is what makes this safe
        # to split at all.
        import multiprocessing as mp

        with mp.get_context("fork").Pool(processes=workers) as pool:
            for position, (symbol, trades, usable) in enumerate(
                pool.imap_unordered(_walk_job, jobs, chunksize=4), start=1
            ):
                _collect(result, symbol, trades, usable)
                if position % 25 == 0:
                    log.info(
                        "%d/%d symbols walked, %d trade(s) so far",
                        position,
                        len(jobs),
                        len(result.trades),
                    )
        return result

    for position, job in enumerate(jobs, start=1):
        _collect(result, *_walk_job(job))
        if position % 25 == 0:
            log.info(
                "%d/%d symbols walked, %d trade(s) so far",
                position,
                len(jobs),
                len(result.trades),
            )
    return result


@dataclass
class _Job:
    """One symbol's work, self-contained so a worker needs nothing else."""

    symbol: str
    sector: str | None
    param_sets: list[ParamSet]
    regimes: RegimeSeries
    costs: CostModel
    sector_index: pd.Series | None
    start: date
    end: date
    as_of: date


def _walk_job(job: _Job) -> tuple[str, list[SimTrade], bool]:
    history = load_symbol_history(job.symbol, as_of=job.as_of, sector=job.sector)
    if not history.usable:
        return job.symbol, [], False
    trades = walk_symbol(
        history,
        param_sets=job.param_sets,
        regimes=job.regimes,
        costs=job.costs,
        sector_index=job.sector_index,
        start=job.start,
        end=job.end,
    )
    return job.symbol, trades, True


def _collect(result: BacktestResult, symbol: str, trades: list[SimTrade], usable: bool) -> None:
    if not usable:
        result.skipped_symbols.append(symbol)
        return
    result.symbols += 1
    result.trades.extend(trades)


def _as_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _param_sets(variants: list[str] | None) -> list[ParamSet]:
    """The baseline, plus one set per `--variant path=value`.

    Walked TOGETHER rather than in separate runs, because the features are the
    expensive part: a gate-only override shares them, so comparing two
    configurations over four years costs barely more than measuring one. It
    also removes the question of whether the two runs saw the same data.
    """
    from backtest.sweep import with_override

    sets = [ParamSet.baseline()]
    for item in variants or []:
        path, _, raw = item.partition("=")
        if not raw:
            raise SystemExit(f"--variant needs PATH=VALUE, got {item!r}")
        sets.append(
            ParamSet(
                name=f"{path.rsplit('.', 1)[-1]}={raw}",
                cfg=with_override(config.scoring(), path.strip(), _coerce(raw.strip())),
            )
        )
    return sets


def _coerce(raw: str):
    """YAML-ish scalar parsing, so `--variant x=2.5` is a float not a string."""
    lowered = raw.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def main(argv: list[str] | None = None) -> int:
    from backtest.metrics import render_report

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--years", type=int, default=4, help="window length ending today")
    parser.add_argument("--start", help="ISO start date (overrides --years)")
    parser.add_argument("--end", help="ISO end date")
    parser.add_argument("--symbols", nargs="*", help="override the universe")
    parser.add_argument("--sample", type=int, help="first N symbols of the universe")
    parser.add_argument("--no-sectors", action="store_true", help="skip sector indices (faster)")
    parser.add_argument("--workers", type=int, default=1, help="processes to split symbols across")
    parser.add_argument(
        "--variant",
        action="append",
        metavar="PATH=VALUE",
        help="a second configuration to walk ALONGSIDE the baseline, e.g. "
        "recipes.post_earnings_drift.beat_source=verified. Repeatable. Gate-only "
        "overrides share the baseline's features, so the comparison is nearly free.",
    )
    parser.add_argument("--out", help="write every trade to this CSV")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    symbols = args.symbols
    if symbols is None and args.sample:
        symbols = db.active_symbols()[: args.sample]
    result = run(
        param_sets=_param_sets(args.variant),
        symbols=symbols,
        start=date.fromisoformat(args.start) if args.start else None,
        end=date.fromisoformat(args.end) if args.end else None,
        years=args.years,
        with_sectors=not args.no_sectors,
        workers=args.workers,
    )
    print(render_report(result))
    if args.out:
        from pathlib import Path

        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        result.frame().to_csv(path, index=False)
        print(f"\n{len(result.trades)} trade(s) written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
