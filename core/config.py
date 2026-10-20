"""Typed, validated loading of config/*.yaml.

D25: config is the only home for numbers. That only works if reading the config
is SAFER than hard-coding, so this module makes a bad config a startup failure
rather than a silently wrong trade plan.

Three layers, in order of how quietly each error would otherwise pass:

1. ``extra="forbid"`` everywhere. A typo (``techncial:``) is not ignored as an
   unknown key and defaulted to zero — it raises, naming the key and its path.
2. Field types and bounds. A weight of ``-35`` or a risk of ``250`` is rejected.
3. Cross-field invariants. Weights totalling 97, a Tier B ceiling below its base
   budget, six swings that cannot reach the heat cap — each is individually
   well-formed and collectively wrong, which is the class of error that survives
   review and shows up months later as mis-sized positions.

Usage:
    from core.config import risk, scoring, universe
    risk().per_trade_risk_pct.tier_a      # -> 2.5

    python -m core.config                 # validate all three, print a summary
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

Percent = Annotated[float, Field(ge=0, le=100)]
Score = Annotated[float, Field(ge=0, le=100)]
Fraction = Annotated[float, Field(ge=0, le=1)]
PositiveInt = Annotated[int, Field(gt=0)]


class ConfigError(RuntimeError):
    """A config file is missing, unparseable, or invalid."""


class Base(BaseModel):
    """Every config model forbids unknown keys. That is layer 1."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _check_horizon(horizon: tuple[int, int], recipe: str) -> None:
    low, high = horizon
    if low > high:
        raise ValueError(f"{recipe}.horizon_days must be [min, max], got [{low}, {high}]")


# ══════════════════════════════════════════════════════════════════════════
# scoring.yaml
# ══════════════════════════════════════════════════════════════════════════


class PillarWeights(Base):
    technical: Percent
    fundamental: Percent
    catalyst: Percent
    sentiment: Percent

    @model_validator(mode="after")
    def _must_total_100(self):
        total = self.technical + self.fundamental + self.catalyst + self.sentiment
        if abs(total - 100.0) > 1e-6:
            raise ValueError(
                f"pillar_weights must total 100, got {total:g} "
                f"(technical={self.technical:g}, fundamental={self.fundamental:g}, "
                f"catalyst={self.catalyst:g}, sentiment={self.sentiment:g})"
            )
        return self

    def as_fractions(self) -> dict[str, float]:
        """Weights as 0-1 fractions, which is what a composite calculation wants."""
        return {
            "technical": self.technical / 100.0,
            "fundamental": self.fundamental / 100.0,
            "catalyst": self.catalyst / 100.0,
            "sentiment": self.sentiment / 100.0,
        }


class CompositeScale(Base):
    scale_min: Score
    scale_max: Score
    conflict_below: Score
    capped_at: Score

    @model_validator(mode="after")
    def _ordered(self):
        if self.scale_min >= self.scale_max:
            raise ValueError("composite.scale_min must be below composite.scale_max")
        if self.conflict_below >= self.capped_at:
            raise ValueError(
                "composite.conflict_below must be below composite.capped_at, otherwise "
                "the conflict cap would raise a score instead of capping it"
            )
        return self


class PillarAgreement(Base):
    min_score: Score
    treat_missing_as_agreement: bool


class TierAGate(Base):
    min_pillars_agreeing: Annotated[int, Field(ge=1, le=4)]
    min_composite: Score
    min_risk_reward: Annotated[float, Field(gt=0)]


class TierBGate(Base):
    min_pillars_agreeing: Annotated[int, Field(ge=1, le=4)]
    min_composite: Score
    max_composite: Score
    min_risk_reward: Annotated[float, Field(gt=0)]

    @model_validator(mode="after")
    def _band_ordered(self):
        if self.min_composite >= self.max_composite:
            raise ValueError(
                f"tiers.b.min_composite ({self.min_composite:g}) must be below "
                f"max_composite ({self.max_composite:g}) or the Tier B band is empty"
            )
        return self


class Tiers(Base):
    a: TierAGate
    b: TierBGate

    @model_validator(mode="after")
    def _tiers_are_coherent(self):
        if self.b.max_composite > self.a.min_composite:
            raise ValueError(
                f"tiers.b.max_composite ({self.b.max_composite:g}) is above "
                f"tiers.a.min_composite ({self.a.min_composite:g}); the bands overlap "
                f"and an idea could qualify as both tiers"
            )
        if self.b.min_pillars_agreeing > self.a.min_pillars_agreeing:
            raise ValueError("tiers.b cannot demand more agreeing pillars than tiers.a")
        if self.b.min_risk_reward < self.a.min_risk_reward:
            raise ValueError(
                "tiers.b.min_risk_reward must be at least tiers.a's (D12/D17: Tier B "
                "takes worse odds, so it must be paid more to be worth surfacing)"
            )
        return self


class PullbackRecipe(Base):
    enabled: bool
    ma_fast: PositiveInt
    ma_mid: PositiveInt
    ma_slow: PositiveInt
    require_ma_stack: bool
    pullback_to: list[PositiveInt]
    require_ma_rising: bool
    rsi_period: PositiveInt
    rsi_min: Score
    rsi_max: Score
    volume_dryup_ratio: Annotated[float, Field(gt=0)]
    require_reversal_candle: bool
    no_earnings_within_days: Annotated[int, Field(ge=0)]
    horizon_days: tuple[PositiveInt, PositiveInt]

    @model_validator(mode="after")
    def _coherent(self):
        if not self.ma_fast < self.ma_mid < self.ma_slow:
            raise ValueError(
                f"pullback_to_trend needs ma_fast < ma_mid < ma_slow, got "
                f"{self.ma_fast} / {self.ma_mid} / {self.ma_slow}"
            )
        if self.rsi_min >= self.rsi_max:
            raise ValueError("pullback_to_trend.rsi_min must be below rsi_max")
        if any(ma not in (self.ma_fast, self.ma_mid, self.ma_slow) for ma in self.pullback_to):
            raise ValueError(
                f"pullback_to {self.pullback_to} references an MA the recipe does not "
                f"compute ({self.ma_fast}/{self.ma_mid}/{self.ma_slow})"
            )
        _check_horizon(self.horizon_days, "pullback_to_trend")
        return self


class BreakoutRecipe(Base):
    enabled: bool
    min_base_weeks: PositiveInt
    max_base_depth_pct: Percent
    range_contraction_ratio: Annotated[float, Field(gt=0)]
    require_relative_strength_vs_sector: bool
    min_relative_volume_on_break: Annotated[float, Field(gt=0)]
    breakout_buffer_pct: Annotated[float, Field(ge=0)]
    horizon_days: tuple[PositiveInt, PositiveInt]

    @model_validator(mode="after")
    def _coherent(self):
        if self.range_contraction_ratio >= 1:
            raise ValueError(
                "base_breakout.range_contraction_ratio must be below 1; at or above 1 "
                "the range is expanding, which is not a base"
            )
        _check_horizon(self.horizon_days, "base_breakout")
        return self


class DriftRecipe(Base):
    enabled: bool
    require_eps_beat: bool
    require_revenue_beat: bool
    require_raised_guidance: bool
    min_gap_up_pct: Annotated[float, Field(gt=0)]
    require_holds_day_one_low: bool
    max_days_since_earnings: PositiveInt
    horizon_days: tuple[PositiveInt, PositiveInt]

    @model_validator(mode="after")
    def _coherent(self):
        _check_horizon(self.horizon_days, "post_earnings_drift")
        if self.max_days_since_earnings > self.horizon_days[1]:
            raise ValueError(
                "post_earnings_drift.max_days_since_earnings exceeds the recipe's own "
                "horizon; the entry window would outlive the setup"
            )
        return self


class Recipes(Base):
    pullback_to_trend: PullbackRecipe
    base_breakout: BreakoutRecipe
    post_earnings_drift: DriftRecipe


class ScoringConfig(Base):
    version: PositiveInt
    pillar_weights: PillarWeights
    composite: CompositeScale
    pillar_agreement: PillarAgreement
    tiers: Tiers
    min_surfaceable_composite: Score
    recipes: Recipes

    @model_validator(mode="after")
    def _gates_are_reachable(self):
        if self.min_surfaceable_composite > self.tiers.b.min_composite:
            raise ValueError(
                f"min_surfaceable_composite ({self.min_surfaceable_composite:g}) is above "
                f"tiers.b.min_composite ({self.tiers.b.min_composite:g}); no Tier B idea "
                f"could ever surface"
            )
        if self.composite.capped_at >= self.tiers.a.min_composite:
            raise ValueError(
                f"composite.capped_at ({self.composite.capped_at:g}) is at or above "
                f"tiers.a.min_composite ({self.tiers.a.min_composite:g}); an idea with a "
                f"conflicting pillar could still reach Tier A"
            )
        if self.pillar_agreement.min_score >= self.tiers.a.min_composite:
            raise ValueError(
                f"pillar_agreement.min_score ({self.pillar_agreement.min_score:g}) is at "
                f"or above tiers.a.min_composite; a pillar would have to clear the whole "
                f"composite gate on its own before it counted as agreeing"
            )
        return self


# ══════════════════════════════════════════════════════════════════════════
# risk.yaml
# ══════════════════════════════════════════════════════════════════════════


class PerTradeRisk(Base):
    tier_a: Percent
    tier_b: Percent

    @model_validator(mode="after")
    def _b_is_smaller(self):
        if self.tier_b >= self.tier_a:
            raise ValueError(
                f"per_trade_risk_pct.tier_b ({self.tier_b:g}) must be below tier_a "
                f"({self.tier_a:g}) — D12: sizing IS the safety valve for Tier B"
            )
        return self


class MinRiskReward(Base):
    tier_a: Annotated[float, Field(gt=0)]
    tier_b: Annotated[float, Field(gt=0)]


class Heat(Base):
    max_open_heat_pct: Percent
    counted_buckets: list[Literal["etf", "hold", "swing", "option", "cash"]]
    release_on_exit: bool
    zero_risk_after_stop_at_breakeven: bool

    @model_validator(mode="after")
    def _active_book_only(self):
        forbidden = {"etf", "hold", "cash"} & set(self.counted_buckets)
        if forbidden:
            raise ValueError(
                f"heat.counted_buckets includes {sorted(forbidden)}; D17 counts the "
                f"ACTIVE book only (swings + options)"
            )
        if not self.counted_buckets:
            raise ValueError("heat.counted_buckets is empty; the heat cap would never bind")
        return self


class TierBBudget(Base):
    base_monthly_pct: Percent
    gain_replenish_rate: Fraction
    hard_ceiling_pct: Percent
    resets_on: Literal["calendar_month", "rolling_30d"]
    measured_as: Literal["risk_at_entry", "notional"]

    @model_validator(mode="after")
    def _ceiling_above_base(self):
        if self.hard_ceiling_pct < self.base_monthly_pct:
            raise ValueError(
                f"tier_b_budget.hard_ceiling_pct ({self.hard_ceiling_pct:g}) is below "
                f"base_monthly_pct ({self.base_monthly_pct:g}); replenishment could never "
                f"be used and the base budget itself would be unreachable"
            )
        return self


class OptionsRisk(Base):
    defined_risk_only: bool
    allowed_structures: list[str]
    monthly_realized_loss_budget_pct: Percent
    max_risk_per_position_pct: Percent
    assignment_risk_check: bool
    assignment_extrinsic_buffer: Annotated[float, Field(gt=0)]

    @model_validator(mode="after")
    def _defined_risk_is_not_optional(self):
        if not self.defined_risk_only:
            raise ValueError(
                "options.defined_risk_only cannot be false — D17 permits defined-risk "
                "structures only, and this is the rule that keeps a max loss knowable"
            )
        if not self.allowed_structures:
            raise ValueError("options.allowed_structures is empty; no option idea could pass")
        if self.max_risk_per_position_pct > self.monthly_realized_loss_budget_pct:
            raise ValueError(
                f"options.max_risk_per_position_pct ({self.max_risk_per_position_pct:g}) "
                f"exceeds the monthly loss budget "
                f"({self.monthly_realized_loss_budget_pct:g}); one loser would blow the month"
            )
        return self


class CircuitBreakerAction(Base):
    pause_tier_b: bool
    tier_a_size_multiplier: Fraction


class CircuitBreaker(Base):
    consecutive_losing_weeks: PositiveInt
    monthly_drawdown_pct: Annotated[float, Field(lt=0)]
    on_trip: CircuitBreakerAction
    resets_after: Literal["winning_week", "calendar_month"]


class SwingExits(Base):
    trim_at_target_1: Fraction
    trim_at_target_2: Fraction
    move_stop_to_breakeven_at: Literal["target_1", "target_2", "never"]
    runner_trail: str
    max_holding_days: PositiveInt

    @model_validator(mode="after")
    def _trims_leave_a_runner(self):
        total = self.trim_at_target_1 + self.trim_at_target_2
        if total >= 1.0:
            raise ValueError(
                f"exits.swing trims total {total:g} of the position, leaving no runner. "
                f"D11: the runner is what makes the true win possible"
            )
        return self


class OptionExits(Base):
    timeout: Literal["expiration"]
    # Typed as optional-int rather than None so a day count reaches the
    # validator below and gets the explanation, instead of a terse type error.
    max_holding_days: PositiveInt | None = None
    early_exit_dte: PositiveInt

    @model_validator(mode="after")
    def _no_day_count(self):
        if self.max_holding_days is not None:
            raise ValueError(
                "exits.option.max_holding_days must be null — a defined-risk option "
                "position (D17) is bounded by its own expiration, and a second clock "
                "would mislabel Shadow Book outcomes. An early exit at N DTE is a "
                "different rule: that is early_exit_dte."
            )
        return self


class HoldExits(Base):
    timeout: Literal["thesis_trigger"]
    max_holding_days: PositiveInt | None = None

    @model_validator(mode="after")
    def _no_day_count(self):
        if self.max_holding_days is not None:
            raise ValueError(
                "exits.hold.max_holding_days must be null — D17 governs long holds by "
                "thesis-invalidation triggers and the review drawdown, not a clock."
            )
        return self


class Exits(Base):
    swing: SwingExits
    option: OptionExits
    hold: HoldExits


class StableBuckets(Base):
    excluded_from_heat: bool
    use_hard_stops: bool
    max_single_name_pct: Percent
    max_sector_pct: Percent
    thesis_review_drawdown_pct: Annotated[float, Field(lt=0)]
    dividend_cut_triggers_review: bool

    @model_validator(mode="after")
    def _concentration_is_coherent(self):
        if self.max_single_name_pct > self.max_sector_pct:
            raise ValueError("stable_buckets.max_single_name_pct cannot exceed max_sector_pct")
        if self.use_hard_stops:
            raise ValueError(
                "stable_buckets.use_hard_stops cannot be true — D17: holds and ETFs run "
                "on thesis-invalidation triggers, not stops"
            )
        return self


class BucketTargets(Base):
    etf: Percent
    hold: Percent
    swing: Percent
    option: Percent
    cash: Percent

    @model_validator(mode="after")
    def _must_total_100(self):
        total = self.etf + self.hold + self.swing + self.option + self.cash
        if abs(total - 100.0) > 1e-6:
            raise ValueError(f"allocation bucket targets must total 100, got {total:g}")
        return self


class AllocationTargets(Base):
    normal: BucketTargets
    risk_off: BucketTargets
    drift_alert_pct: Percent
    risk_off_spy_below_ma: PositiveInt
    risk_off_vix_above: Annotated[float, Field(gt=0)]

    @model_validator(mode="after")
    def _risk_off_is_actually_defensive(self):
        if self.risk_off.swing > self.normal.swing or self.risk_off.option > self.normal.option:
            raise ValueError(
                "allocation_targets.risk_off allocates more to swings or options than "
                "normal; D17 says risk-off reduces the active book and raises cash"
            )
        if self.risk_off.cash < self.normal.cash:
            raise ValueError("allocation_targets.risk_off must hold at least as much cash")
        return self


class Dividends(Base):
    total_return_everywhere: bool
    dividend_adjusted_stops: bool
    no_new_swing_long_within_days_of_ex: Annotated[int, Field(ge=0)]
    core_etf_drip: bool
    hold_dividends_to_cash_bucket: bool

    @model_validator(mode="after")
    def _total_return_is_not_optional(self):
        if not self.total_return_everywhere:
            raise ValueError(
                "dividends.total_return_everywhere cannot be false — D19(1) measures "
                "total return everywhere, including the SPY benchmark"
            )
        return self


class Cash(Base):
    has_risk_profile: bool


class RiskConfig(Base):
    version: PositiveInt
    percent_basis: Literal["current_account_value", "high_water_mark"]
    per_trade_risk_pct: PerTradeRisk
    min_risk_reward: MinRiskReward
    max_concurrent_swings: PositiveInt
    heat: Heat
    tier_b_budget: TierBBudget
    options: OptionsRisk
    circuit_breaker: CircuitBreaker
    exits: Exits
    stable_buckets: StableBuckets
    allocation_targets: AllocationTargets
    dividends: Dividends
    cash: Cash

    @model_validator(mode="after")
    def _caps_are_mutually_reachable(self):
        # D17: six concurrent swings at full Tier A size must be ABLE to exceed
        # the heat cap, or the two numbers contradict and the cap is decorative.
        worst_case = self.max_concurrent_swings * self.per_trade_risk_pct.tier_a
        if worst_case <= self.heat.max_open_heat_pct:
            raise ValueError(
                f"max_concurrent_swings ({self.max_concurrent_swings}) x tier_a risk "
                f"({self.per_trade_risk_pct.tier_a:g}%) = {worst_case:g}%, which does not "
                f"exceed the heat cap of {self.heat.max_open_heat_pct:g}% — the heat cap "
                f"could never bind, so one of the two numbers is wrong"
            )
        if self.per_trade_risk_pct.tier_a > self.heat.max_open_heat_pct:
            raise ValueError("a single Tier A trade cannot be allowed to exceed the heat cap")
        if self.per_trade_risk_pct.tier_b > self.tier_b_budget.base_monthly_pct:
            raise ValueError(
                "per_trade_risk_pct.tier_b exceeds the Tier B monthly base budget; "
                "no Tier B idea could ever be taken"
            )
        if self.min_risk_reward.tier_b < self.min_risk_reward.tier_a:
            raise ValueError("min_risk_reward.tier_b must be at least tier_a's (D17)")
        if not self.stable_buckets.excluded_from_heat:
            raise ValueError(
                "stable_buckets.excluded_from_heat cannot be false — D17 counts the "
                "active book only"
            )
        return self


# ══════════════════════════════════════════════════════════════════════════
# universe.yaml
# ══════════════════════════════════════════════════════════════════════════


class UniverseSources(Base):
    sp500: bool
    nasdaq100: bool
    watchlist_file: str
    static_fallback_file: str
    allow_static_fallback: bool

    @model_validator(mode="after")
    def _something_can_build_the_universe(self):
        if not (self.sp500 or self.nasdaq100):
            raise ValueError(
                "sources has neither sp500 nor nasdaq100 enabled; the universe would be "
                "the watchlist alone (D14 expects ~500 names)"
            )
        return self


class Liquidity(Base):
    min_avg_volume: Annotated[int, Field(ge=0)]
    avg_volume_lookback_days: PositiveInt
    require_options: bool
    min_price: Annotated[float, Field(ge=0)]
    exempt_watchlist: bool


class UniverseMaintenance(Base):
    deactivate_missing: bool
    deactivate_after_absent_days: Annotated[int, Field(ge=0)]
    max_universe_size: PositiveInt


class OptionChainScope(Base):
    strike_range_pct: Annotated[float, Field(gt=0, le=100)]
    monthly_expirations: PositiveInt
    monthly_only: bool
    include_calls: bool
    include_puts: bool
    min_open_interest: Annotated[int, Field(ge=0)]
    aggregate_iv: bool
    iv_rank_lookback_days: PositiveInt
    iv_rank_min_history_days: PositiveInt

    @model_validator(mode="after")
    def _scope_is_usable(self):
        if not (self.include_calls or self.include_puts):
            raise ValueError("option_chains excludes both calls and puts; nothing would snapshot")
        if not self.include_puts:
            raise ValueError(
                "option_chains.include_puts is false; D22 stores skew, which is defined "
                "as a put-vs-call IV difference and cannot be computed from calls alone"
            )
        if self.iv_rank_min_history_days > self.iv_rank_lookback_days:
            raise ValueError(
                "option_chains.iv_rank_min_history_days exceeds iv_rank_lookback_days; "
                "IV rank would never become computable"
            )
        return self


class EstimateScope(Base):
    period_types: list[Literal["quarter", "annual"]]
    quarters_ahead: PositiveInt
    annuals_ahead: PositiveInt

    @model_validator(mode="after")
    def _not_empty(self):
        if not self.period_types:
            raise ValueError("estimates.period_types is empty; the D7 snapshot would be a no-op")
        return self


class FundamentalsScope(Base):
    quarters_history: PositiveInt
    annuals_history: PositiveInt
    refresh_after_days: PositiveInt


class EventsScope(Base):
    forward_days: PositiveInt
    backward_days: Annotated[int, Field(ge=0)]


class NewsScope(Base):
    lookback_days: PositiveInt


class FmpBudget(Base):
    daily_call_budget: PositiveInt


class UniverseConfig(Base):
    version: PositiveInt
    sources: UniverseSources
    liquidity: Liquidity
    maintenance: UniverseMaintenance
    option_chains: OptionChainScope
    estimates: EstimateScope
    fundamentals: FundamentalsScope
    events: EventsScope
    news: NewsScope
    fmp: FmpBudget

    @model_validator(mode="after")
    def _scopes_are_mutually_coherent(self):
        # The event radar must see past the far edge of anything we might hold.
        if self.events.forward_days < 45:
            raise ValueError(
                f"events.forward_days ({self.events.forward_days}) is shorter than the "
                f"longest recipe horizon (45 days); the engine could open a position "
                f"blind to an event just past the edge of the radar"
            )
        # A full fundamentals crawl must fit inside the daily FMP budget (D29).
        if self.fmp.daily_call_budget < 5:
            raise ValueError(
                "fmp.daily_call_budget is below the 5 calls one symbol's fundamentals "
                "cost; no symbol could ever be refreshed"
            )
        return self


# ══════════════════════════════════════════════════════════════════════════
# loading
# ══════════════════════════════════════════════════════════════════════════

_MODELS: dict[str, type[Base]] = {
    "scoring.yaml": ScoringConfig,
    "risk.yaml": RiskConfig,
    "universe.yaml": UniverseConfig,
}


def load_file(filename: str, *, config_dir: Path | None = None) -> Base:
    """Parse and validate one config file. Raises ConfigError naming the path."""
    model = _MODELS.get(filename)
    if model is None:
        raise ConfigError(f"no schema registered for {filename!r}")

    path = (config_dir or CONFIG_DIR) / filename
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

    if raw is None:
        raise ConfigError(f"{path} is empty")
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must be a mapping at the top level, got {type(raw).__name__}")

    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{path} failed validation:\n{_format_errors(exc)}") from exc


def _format_errors(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        location = ".".join(str(part) for part in err["loc"]) or "<root>"
        message = err["msg"]
        if err["type"] == "extra_forbidden":
            message = "unknown key — check the spelling against config/*.yaml"
        lines.append(f"  {location}: {message}")
    return "\n".join(lines)


@lru_cache(maxsize=1)
def scoring() -> ScoringConfig:
    """The validated scoring config (D2/D12). Cached for the process lifetime."""
    return load_file("scoring.yaml")  # type: ignore[return-value]


@lru_cache(maxsize=1)
def risk() -> RiskConfig:
    """The validated risk config (D17/D18/D19)."""
    return load_file("risk.yaml")  # type: ignore[return-value]


@lru_cache(maxsize=1)
def universe() -> UniverseConfig:
    """The validated universe + ingestion-scope config (D14/D22/D29)."""
    return load_file("universe.yaml")  # type: ignore[return-value]


def load_watchlist(path: Path | None = None) -> list[str]:
    """Read the watchlist: one symbol per line, '#' comments, deduped, upper-cased.

    A missing file is not an error — an empty watchlist is legitimate (D14: the
    indices carry the universe).
    """
    path = path or (CONFIG_DIR.parent / universe().sources.watchlist_file)
    if not path.exists():
        return []
    seen: dict[str, None] = {}
    for line in path.read_text().splitlines():
        symbol = line.split("#", 1)[0].strip().upper()
        if symbol:
            seen[symbol] = None
    return list(seen)


def reload() -> None:
    """Drop the caches. For tests and a future SIGHUP-style reload."""
    scoring.cache_clear()
    risk.cache_clear()
    universe.cache_clear()


def validate_all() -> list[str]:
    """Validate every config file. Returns problems; empty means good."""
    problems = []
    for filename in _MODELS:
        try:
            load_file(filename)
        except ConfigError as exc:
            problems.append(str(exc))
    return problems


def main() -> int:
    problems = validate_all()
    if problems:
        print("config is INVALID:\n")
        for problem in problems:
            print(problem)
            print()
        return 1

    s, r, u = scoring(), risk(), universe()
    w = s.pillar_weights
    print("config is valid\n")
    print("  scoring.yaml")
    print(
        f"    pillars          technical {w.technical:g} / fundamental {w.fundamental:g} / "
        f"catalyst {w.catalyst:g} / sentiment {w.sentiment:g}"
    )
    print(
        f"    tier A gate      >={s.tiers.a.min_pillars_agreeing} pillars, composite "
        f">={s.tiers.a.min_composite:g}, R:R >={s.tiers.a.min_risk_reward:g}"
    )
    print(
        f"    tier B gate      >={s.tiers.b.min_pillars_agreeing} pillars, composite "
        f"{s.tiers.b.min_composite:g}-{s.tiers.b.max_composite:g} (excl), R:R "
        f">={s.tiers.b.min_risk_reward:g}"
    )
    print(f"    pillar agrees at >={s.pillar_agreement.min_score:g}")
    enabled = [n for n, rec in vars(s.recipes).items() if rec.enabled]
    print(f"    recipes enabled  {', '.join(enabled)}")

    print("\n  risk.yaml")
    print(
        f"    per trade        tier A {r.per_trade_risk_pct.tier_a:g}% / tier B "
        f"{r.per_trade_risk_pct.tier_b:g}%"
    )
    print(
        f"    heat cap         {r.heat.max_open_heat_pct:g}% across "
        f"{', '.join(r.heat.counted_buckets)}; max {r.max_concurrent_swings} swings "
        f"(worst case {r.max_concurrent_swings * r.per_trade_risk_pct.tier_a:g}%)"
    )
    print(
        f"    tier B budget    {r.tier_b_budget.base_monthly_pct:g}% base, "
        f"{r.tier_b_budget.gain_replenish_rate:.0%} replenish, "
        f"{r.tier_b_budget.hard_ceiling_pct:g}% ceiling / "
        f"{r.tier_b_budget.resets_on}"
    )
    print(
        f"    options          {r.options.monthly_realized_loss_budget_pct:g}%/mo loss "
        f"budget, {r.options.max_risk_per_position_pct:g}% max per position"
    )
    print(
        f"    circuit breaker  {r.circuit_breaker.consecutive_losing_weeks} losing weeks "
        f"or {r.circuit_breaker.monthly_drawdown_pct:g}% month"
    )
    print(
        f"    exits            swing {r.exits.swing.max_holding_days}d / option "
        f"{r.exits.option.timeout} / hold {r.exits.hold.timeout}"
    )

    print("\n  universe.yaml")
    print(
        f"    liquidity        avg vol > {u.liquidity.min_avg_volume:,} over "
        f"{u.liquidity.avg_volume_lookback_days}d, price >= ${u.liquidity.min_price:g}"
    )
    print(
        f"    chain scope      +/-{u.option_chains.strike_range_pct:g}% of spot x "
        f"{u.option_chains.monthly_expirations} monthly expirations; IV rank needs "
        f"{u.option_chains.iv_rank_min_history_days}d"
    )
    print(
        f"    static fallback  {u.sources.static_fallback_file} "
        f"(enabled={u.sources.allow_static_fallback})"
    )
    print(f"    fmp budget       {u.fmp.daily_call_budget} calls/day")
    print(f"    watchlist        {len(load_watchlist())} symbol(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
