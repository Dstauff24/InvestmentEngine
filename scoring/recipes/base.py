"""The shared trade-plan shape every recipe emits (D11/D12).

RECIPES READ FEATURES AND SCORES. They do not re-measure. Every level a plan
hangs on — swing high, base depth, gap low, ATR — was computed once in
`features/` and is passed in, because the same swing high measured three
slightly different ways is how the stop a card prints stops matching the stop
the backtest used.

A recipe answers two questions, in this order:

  1. **Does the setup exist?** A boolean with a reason, read off the features.
  2. **Where are the levels?** Entry zone, stop, TP1, TP2, and the R:R that
     falls out of them.

R:R IS NOT CONFIGURED, IT IS DERIVED. Targets hang on structure the market
made, so R:R varies per idea and D12's gates (Tier A >= 2:1, Tier B >= 3:1) do
real work. Setting TP2 at "3R" instead would make R:R a constant and the gates
satisfied by construction on every single idea.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

from core import config
from features.technical import TechnicalFeatures

log = logging.getLogger(__name__)

__all__ = ["RecipeResult", "TradePlan", "build_plan", "no_setup"]


@dataclass(frozen=True)
class TradePlan:
    """Entry zone, stop, two targets and the R:R that follows (D11/D12)."""

    entry_low: float
    entry_high: float
    stop: float
    target_1: float
    target_2: float | None
    """None for a TRAILING plan (O20): the runner has no far target to quote,
    which is the whole design rather than a missing value."""
    horizon_days: tuple[int, int]
    trail_ma: int | None = None
    """The MA window the runner trails, from `exits.swing.runner_trail`. Set
    means the remainder exits on a trail; None means it exits at TP2."""
    trail_confirm: int | None = None
    """Closes below the MA before the runner leaves ([OPEN] #84). Travels WITH
    the plan rather than being read at resolution time, for the reason
    `gates_in_force` travels with a call: a later change to the number must not
    reinterpret an exit that already happened."""

    @property
    def entry(self) -> float:
        """Midpoint of the zone. What R:R is quoted against."""
        return (self.entry_low + self.entry_high) / 2.0

    @property
    def risk(self) -> float:
        return self.entry - self.stop

    @property
    def trails(self) -> bool:
        return self.trail_ma is not None

    @property
    def risk_reward(self) -> float:
        """To TP2 — the target D11 calls a "true win".

        Quoted to TP2 rather than TP1 because TP1 is the trim that protects the
        trade (D18 moves the stop to breakeven there) while TP2 is what the
        idea is actually for. Reporting R:R to TP1 would make every plan look
        like roughly 1:1 and the D12 gates meaningless.

        **A TRAILING PLAN QUOTES TO TP1 (D12, amended Oct 23 2026).** It has no
        TP2 by design, and its Tier A gate is 1.0R rather than 2:1 — at or
        above 1R the trim plus the breakeven stop mean the trade cannot lose
        money once TP1 is hit, so the number falls out of the trim rules rather
        than out of a sweep.
        """
        if self.risk <= 0:
            return 0.0
        if self.trails or self.target_2 is None:
            return self.risk_reward_tp1
        return (self.target_2 - self.entry) / self.risk

    @property
    def risk_reward_tp1(self) -> float:
        return (self.target_1 - self.entry) / self.risk if self.risk > 0 else 0.0

    def as_dict(self) -> dict:
        out = asdict(self)
        out["entry"] = self.entry
        out["risk_reward"] = self.risk_reward
        out["risk_reward_tp1"] = self.risk_reward_tp1
        return out


@dataclass(frozen=True)
class RecipeResult:
    """Did the setup fire, and if so what is the plan."""

    recipe: str
    triggered: bool
    reason: str
    """Why it fired, or the FIRST condition that failed. Always populated."""
    plan: TradePlan | None = None
    checks: dict[str, bool] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.checks is None:
            object.__setattr__(self, "checks", {})

    @property
    def failed_checks(self) -> tuple[str, ...]:
        return tuple(name for name, ok in self.checks.items() if not ok)


def no_setup(recipe: str, reason: str, checks: dict[str, bool] | None = None) -> RecipeResult:
    return RecipeResult(recipe=recipe, triggered=False, reason=reason, checks=checks or {})


def build_plan(
    *,
    entry_reference: float,
    structural_stop: float,
    target_1: float,
    target_2: float | None,
    features: TechnicalFeatures,
    horizon_days: tuple[int, int],
    cfg=None,
    trail_ma: int | None = None,
    trail_confirm: int | None = None,
) -> tuple[TradePlan | None, str]:
    """Assemble a plan from a reference price and structural levels.

    Returns (plan, reason). `plan` is None when the geometry is unusable, and
    the reason says which way — these are the cases where a number could be
    produced and should not be:

      * **No ATR.** Every distance here is in ATR, so without it the stop would
        have to be a fixed percentage, which is a different strategy.
      * **Stop inside the noise.** A stop closer than `min_stop_atr` produces a
        flattering R:R that the market will take out on an ordinary day.
      * **Target below entry.** Structure can put the "target" behind the
        price — a pullback already above its prior swing high, say. That is not
        a trade with a bad R:R, it is not this trade at all.
    """
    geometry = (cfg or config.scoring()).geometry
    atr = features.atr
    if atr is None or atr <= 0:
        return None, "no ATR — every distance in a plan is measured in it"

    half = geometry.entry_zone_atr * atr
    entry_low, entry_high = entry_reference - half, entry_reference + half
    stop = structural_stop - geometry.stop_atr_buffer * atr
    entry = (entry_low + entry_high) / 2.0

    risk = entry - stop
    if risk <= 0:
        return None, f"stop {stop:.2f} is at or above the entry {entry:.2f}"
    if risk < geometry.min_stop_atr * atr:
        return None, (
            f"stop is {risk / atr:.2f} ATR from entry, inside the "
            f"{geometry.min_stop_atr:g} ATR noise floor"
        )
    if target_1 <= entry:
        return None, f"TP1 {target_1:.2f} is not above the entry {entry:.2f}"
    if trail_ma is None:
        if target_2 is None:
            return None, "a fixed-target plan needs a TP2; only a trailing plan may omit it"
        if target_2 <= target_1:
            return None, (
                f"targets are not above the entry in order "
                f"(entry {entry:.2f}, TP1 {target_1:.2f}, TP2 {target_2:.2f})"
            )
    elif target_2 is not None:
        # A trailing plan with a far target would be two exit rules for one
        # remainder, and the backtest could not say which one it measured.
        return None, "a trailing plan must not also carry a TP2"

    return (
        TradePlan(
            entry_low=entry_low,
            entry_high=entry_high,
            stop=stop,
            target_1=target_1,
            target_2=target_2,
            horizon_days=horizon_days,
            trail_ma=trail_ma,
            trail_confirm=trail_confirm,
        ),
        "",
    )
