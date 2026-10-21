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


StrategyStatus = Literal["shadow", "paper", "live"]


class PromotionStatus(Base):
    """D40: one status per recipe. A promotion is a one-line diff."""

    pullback_to_trend: StrategyStatus
    base_breakout: StrategyStatus
    post_earnings_drift: StrategyStatus

    def of(self, recipe: str) -> StrategyStatus:
        """Status for a recipe name, raising rather than defaulting.

        Deliberately not `.get(recipe, "shadow")`: a typo'd or newly added
        recipe silently defaulting to `shadow` would look like the safe choice
        and is not — it would hide the fact that the ladder does not know about
        a strategy that is already producing ideas.
        """
        try:
            return getattr(self, recipe)
        except AttributeError as exc:
            raise ConfigError(
                f"no D40 promotion status configured for recipe {recipe!r}. Add it "
                f"to scoring.promotion.status; a new strategy enters 'shadow'."
            ) from exc


class ShadowToPaper(Base):
    min_closed_ideas: PositiveInt
    min_expectancy_r: float  # [OPEN] #38


class PaperToLive(Base):
    min_closed_ideas: PositiveInt
    sustained_window_days: PositiveInt  # [OPEN] #39
    min_expectancy_r: float  # [OPEN] #40
    max_calibration_error_pct: Percent  # [OPEN] #41


class Demotion(Base):
    window_days: PositiveInt  # [OPEN] #42
    min_closed_ideas: PositiveInt  # [OPEN] #43
    expectancy_r_below: float  # [OPEN] #44


class Promotion(Base):
    """D40: the ladder. Rules in config so review is a diff, not a debate."""

    status: PromotionStatus
    shadow_to_paper: ShadowToPaper
    paper_to_live: PaperToLive
    demotion: Demotion

    @model_validator(mode="after")
    def _the_ladder_cannot_contradict_itself(self):
        if self.paper_to_live.min_expectancy_r < self.shadow_to_paper.min_expectancy_r:
            raise ValueError(
                f"paper_to_live.min_expectancy_r ({self.paper_to_live.min_expectancy_r}) "
                f"is below shadow_to_paper's ({self.shadow_to_paper.min_expectancy_r}); "
                f"the second rung must demand at least as much as the first, or a "
                f"strategy could qualify for LIVE on evidence too weak for PAPER"
            )
        if self.demotion.expectancy_r_below >= self.shadow_to_paper.min_expectancy_r:
            raise ValueError(
                f"demotion.expectancy_r_below ({self.demotion.expectancy_r_below}) is at "
                f"or above shadow_to_paper.min_expectancy_r "
                f"({self.shadow_to_paper.min_expectancy_r}); a strategy would be demoted "
                f"the moment it was promoted, oscillating for ever"
            )
        if self.demotion.min_closed_ideas > self.shadow_to_paper.min_closed_ideas:
            raise ValueError(
                f"demotion.min_closed_ideas ({self.demotion.min_closed_ideas}) exceeds "
                f"the promotion floor ({self.shadow_to_paper.min_closed_ideas}); a "
                f"strategy could be promoted on evidence that is not yet enough to "
                f"demote it, so a bad run could never remove it"
            )
        return self


class TechnicalMapping(Base):
    rs_full_credit_pct: float  # [OPEN] #53
    rsi_healthy_low: Score  # [OPEN] #54
    rsi_healthy_high: Score  # [OPEN] #55

    @model_validator(mode="after")
    def _the_band_must_be_a_band(self):
        if self.rsi_healthy_low >= self.rsi_healthy_high:
            raise ValueError(
                f"rsi_healthy_low ({self.rsi_healthy_low}) must be below "
                f"rsi_healthy_high ({self.rsi_healthy_high})"
            )
        return self


class FundamentalMapping(Base):
    growth_full_credit_pct: float  # [OPEN] #56
    acceleration_full_credit_pp: float  # [OPEN] #57
    margin_trend_full_credit_pp: float  # [OPEN] #58
    revision_full_credit_pct: float  # [OPEN] #59
    revision_min_observations: PositiveInt  # [OPEN] #60


class CatalystMapping(Base):
    earnings_near_days: PositiveInt  # [OPEN] #61
    earnings_horizon_days: PositiveInt  # [OPEN] #62
    drift_window_days: PositiveInt  # [OPEN] #63
    ex_div_penalty_days: PositiveInt  # [OPEN] #64

    @model_validator(mode="after")
    def _near_is_inside_the_horizon(self):
        if self.earnings_near_days >= self.earnings_horizon_days:
            raise ValueError(
                f"earnings_near_days ({self.earnings_near_days}) must be inside "
                f"earnings_horizon_days ({self.earnings_horizon_days}), or the "
                f"fall-off has nothing to fall off across"
            )
        return self


class SentimentMapping(Base):
    tone_delta_full_credit: float  # [OPEN] #65
    volume_spike_full_credit: float  # [OPEN] #66


class Mapping(Base):
    """Feature -> pillar-score mapping. Equal sub-weights by design, not default.

    See config/scoring.yaml: D2/D32 fix the weights BETWEEN pillars because the
    log says what they are; nothing says whether revision momentum should
    outrank margin trend INSIDE Pillar B, and inventing that number would be
    false precision dressed as configuration.
    """

    min_scored_pillars: PositiveInt  # [OPEN] #52
    technical: TechnicalMapping
    fundamental: FundamentalMapping
    catalyst: CatalystMapping
    sentiment: SentimentMapping

    @model_validator(mode="after")
    def _full_credit_values_must_be_positive(self):
        scales = {
            "technical.rs_full_credit_pct": self.technical.rs_full_credit_pct,
            "fundamental.growth_full_credit_pct": self.fundamental.growth_full_credit_pct,
            "fundamental.acceleration_full_credit_pp": (
                self.fundamental.acceleration_full_credit_pp
            ),
            "fundamental.margin_trend_full_credit_pp": (
                self.fundamental.margin_trend_full_credit_pp
            ),
            "fundamental.revision_full_credit_pct": self.fundamental.revision_full_credit_pct,
            "sentiment.tone_delta_full_credit": self.sentiment.tone_delta_full_credit,
            "sentiment.volume_spike_full_credit": self.sentiment.volume_spike_full_credit,
        }
        for name, value in scales.items():
            if value <= 0:
                raise ValueError(
                    f"mapping.{name} is {value}; a full-credit scale divides the "
                    f"feature value, so zero or negative would invert or explode "
                    f"every reading it maps"
                )
        if self.min_scored_pillars > 4:
            raise ValueError("mapping.min_scored_pillars cannot exceed the four pillars")
        return self


class SentimentScope(Base):
    """D32: Pillar D measures CHANGE, not level."""

    trailing_days: PositiveInt  # [OPEN] #50
    baseline_days: PositiveInt  # [OPEN] #51
    min_articles: PositiveInt  # [OPEN] #49
    # Phase 1 is a lexicon; Phase 2 adds the Claude API scorer. Every reading
    # records which one produced it so D39 calibration never mixes them.
    scorer: Literal["lexicon"]

    @model_validator(mode="after")
    def _the_baseline_must_be_longer_than_the_trailing_window(self):
        if self.baseline_days <= self.trailing_days:
            raise ValueError(
                f"sentiment.baseline_days ({self.baseline_days}) must exceed "
                f"trailing_days ({self.trailing_days}); otherwise the delta compares a "
                f"window against itself and is always zero"
            )
        return self


class Ranking(Base):
    """D33: ideas are ranked by composite percentile within the universe."""

    rank_within_universe: bool
    max_ideas_per_day: PositiveInt  # [OPEN] #32


class SentimentReinstatement(Base):
    """D32: the condition under which Sentiment earns its weight back.

    Validated so a typo fails at startup; nothing reads it until the Shadow
    Book can produce the evidence.
    """

    min_sentiment_score: Score
    min_sample: PositiveInt  # [OPEN] #37
    restore_toward: Score


class ScoringConfig(Base):
    version: PositiveInt
    pillar_weights: PillarWeights
    composite: CompositeScale
    pillar_agreement: PillarAgreement
    tiers: Tiers
    min_surfaceable_composite: Score
    mapping: Mapping
    sentiment: SentimentScope
    promotion: Promotion
    ranking: Ranking
    sentiment_reinstatement: SentimentReinstatement
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
        # D32: the demotion only stays safe because the conflict veto still
        # bites. If sentiment could score below `conflict_below` and NOT cap the
        # composite, a 10-weight pillar would be decorative — which is the thing
        # D32 explicitly declined to do (it demoted sentiment, it did not
        # disable it). This asserts the veto is reachable rather than assuming.
        if self.composite.conflict_below <= self.composite.scale_min:
            raise ValueError(
                f"composite.conflict_below ({self.composite.conflict_below:g}) is at or "
                f"below scale_min; no pillar could ever register as conflicting, so "
                f"D32's demoted Sentiment pillar would lose its veto as well as its "
                f"weight"
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


class VehicleByIVRank(Base):
    """D36: IV rank selects the vehicle on every Tier A idea."""

    enabled: bool
    iv_vehicle_low: Score  # [OPEN] #33
    iv_vehicle_high: Score  # [OPEN] #34
    defined_risk_only: bool
    check_binary_events_in_expiration: bool

    @model_validator(mode="after")
    def _bands_are_ordered_and_leave_a_middle(self):
        if self.iv_vehicle_low >= self.iv_vehicle_high:
            raise ValueError(
                f"iv_vehicle_low ({self.iv_vehicle_low:g}) must be below iv_vehicle_high "
                f"({self.iv_vehicle_high:g}); D36 defines THREE bands and equal bounds "
                f"erase the middle one, where shares are the default vehicle"
            )
        if not self.defined_risk_only:
            raise ValueError("D17/D12: defined_risk_only cannot be turned off")
        return self


class Pyramiding(Base):
    """D37: adds are funded by locked-in safety, never by raising the ante."""

    enabled: bool
    require_stop_at_breakeven: bool
    cap_at_original_allocation: bool

    @model_validator(mode="after")
    def _the_two_guards_cannot_be_disabled_while_enabled(self):
        # D37 is the two conditions. Pyramiding with either one off is simply
        # "bigger entries", which the decision exists to forbid.
        if self.enabled and not (
            self.require_stop_at_breakeven and self.cap_at_original_allocation
        ):
            raise ValueError(
                "D37: pyramiding requires BOTH require_stop_at_breakeven and "
                "cap_at_original_allocation. Without them an add raises initial risk, "
                "which is the behaviour the decision forbids"
            )
        return self


class SectorGuard(Base):
    """D38: correlation guard the heat cap is blind to."""

    max_concurrent_swings_per_sector: PositiveInt
    surface_excess_as_watch_only: bool


class Sleeve(Base):
    target_pct: Percent
    benchmark: Literal["blend", "spy_total_return"]
    buckets: list[Literal["etf", "hold", "swing", "option", "cash"]]


class Ratchet(Base):
    """D35: winners raise the floor; losses never pull gains back."""

    enabled: bool
    cadence: Literal["quarterly"]
    trigger_excess_points: Percent  # [OPEN] #35
    sweep_fraction: Fraction  # [OPEN] #36
    permanent: bool
    replenish_drawdowns: bool

    @model_validator(mode="after")
    def _the_asymmetry_is_intact(self):
        if not 0 < self.sweep_fraction <= 1:
            raise ValueError(
                f"sweep_fraction ({self.sweep_fraction}) must be in (0, 1]; sweeping "
                f"nothing disables the ratchet and sweeping more than the excess would "
                f"take capital the Growth sleeve never gained"
            )
        if self.enabled and self.replenish_drawdowns:
            raise ValueError(
                "D35: replenish_drawdowns must stay false while the ratchet is enabled. "
                "Sweeping gains out while topping losses back up is a one-way valve "
                "pointed the wrong way — it would ratchet the trading sleeve DOWN in "
                "good years and refill it in bad ones"
            )
        if self.enabled and not self.permanent:
            raise ValueError("D35: a swept gain is moved permanently, not lent")
        return self


class Sleeves(Base):
    """D35: the barbell, with separate targets AND separate benchmarks."""

    core: Sleeve
    growth: Sleeve
    ratchet: Ratchet

    @model_validator(mode="after")
    def _targets_total_100_and_buckets_are_partitioned(self):
        total = self.core.target_pct + self.growth.target_pct
        if abs(total - 100) > 1e-9:
            raise ValueError(
                f"sleeve targets total {total:g}, not 100 — every bucket belongs to "
                f"exactly one sleeve, so the targets must partition the account"
            )
        overlap = set(self.core.buckets) & set(self.growth.buckets)
        if overlap:
            raise ValueError(
                f"buckets {sorted(overlap)} appear in BOTH sleeves; a bucket counted "
                f"twice would let the ratchet sweep capital to and from itself"
            )
        return self


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
    vehicle_by_iv_rank: VehicleByIVRank
    pyramiding: Pyramiding
    sector_guard: SectorGuard
    sleeves: Sleeves
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
    reserve_for_events: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def _reserve_leaves_room(self):
        if self.reserve_for_events >= self.daily_call_budget:
            raise ValueError(
                f"fmp.reserve_for_events ({self.reserve_for_events}) must be below "
                f"daily_call_budget ({self.daily_call_budget}); otherwise no other job "
                f"could ever spend a call"
            )
        return self


class EdgarBudget(Base):
    """D39: SEC EDGAR is a provider like any other, with a runaway-loop guard."""

    daily_call_budget: PositiveInt  # [OPEN] #48
    lookback_days: PositiveInt


class InsiderCluster(Base):
    """D39: N distinct insiders buying on the open market within D days."""

    min_distinct_insiders: PositiveInt  # [OPEN] #45
    window_days: PositiveInt  # [OPEN] #46
    min_aggregate_usd: float  # [OPEN] #47
    # Locked by the decision rather than open: the code filter IS the
    # definition of "open-market purchase". 'A' is an award, 'M' an option
    # exercise, 'F' tax withholding — counting those measures a compensation
    # schedule rather than an opinion.
    purchase_code: Literal["P"]
    exclude_derivatives: bool

    @model_validator(mode="after")
    def _a_cluster_needs_more_than_one_person(self):
        if self.min_distinct_insiders < 2:
            raise ValueError(
                f"insider_cluster.min_distinct_insiders is {self.min_distinct_insiders}; "
                f"a 'cluster' of one is just a purchase, and the whole point of the "
                f"rule is that several people acted independently"
            )
        if self.min_aggregate_usd < 0:
            raise ValueError("insider_cluster.min_aggregate_usd cannot be negative")
        if not self.exclude_derivatives:
            raise ValueError(
                "insider_cluster.exclude_derivatives cannot be false: a derivative "
                "row is an option grant or exercise, which is compensation rather "
                "than a conviction purchase (D39)"
            )
        return self


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
    edgar: EdgarBudget
    insider_cluster: InsiderCluster

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
