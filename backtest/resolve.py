"""Resolve a simulated plan against the bars that followed it.

**THERE IS ONE IMPLEMENTATION OF THE FILL RULES AND IT LIVES IN
`shadow_book/outcome_tracker.py`.** This module builds the call dict that
function already takes and hands it the forward bars; it does not re-decide
anything. That matters more than the duplication it saves: the pessimistic
ambiguous-bar rule, the entry-zone fill, the breakeven move at TP1 and the
D19(2) dividend adjustment are modelling CHOICES, and a backtest that scored
them differently from the tracker would produce an expectancy the Shadow Book
can never reproduce — the two numbers would disagree for ever and nobody could
say which was the strategy.

What this module does own is everything the tracker reads from a database and a
simulation has to supply instead: the forward window, next-day entry, and the
cost model (D39(4)).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from backtest.costs import CostModel, Vehicle
from scoring.recipes.base import TradePlan
from shadow_book.outcome_tracker import TrackResult, evaluate_call

__all__ = ["SimFill", "resolve_plan"]

# How far past the horizon to keep reading bars. The tracker closes a trade
# itself on a stop, a target or the timeout, so this only has to be long enough
# for those to happen: the plan may wait unfilled for the whole horizon and then
# be held for the whole horizon again.
FORWARD_SLACK_DAYS = 10


@dataclass(frozen=True)
class SimFill:
    """One resolved trade: what the tracker decided, priced.

    `gross_r` is the tracker's own R multiple, which is what the Shadow Book
    would record for the same plan. `net_r` is that after friction. Both are
    reported because the difference is the only honest way to show how much of
    an edge is really spread.
    """

    outcome: str
    entered_on: date | None
    entry: float | None
    exit_price: float | None
    exit_on: date | None
    gross_r: float | None
    net_r: float | None
    return_pct: float | None
    days_held: int | None
    mfe: float | None
    mae: float | None
    hit_target_1: bool
    hit_target_2: bool
    stop_to_breakeven_on: date | None

    @property
    def filled(self) -> bool:
        return self.entry is not None

    @property
    def closed(self) -> bool:
        return self.outcome not in ("open", "never_triggered")


def _call_dict(
    *, symbol: str, recipe: str, plan: TradePlan, called_on: date, horizon_days: int
) -> dict:
    """The shape `evaluate_call` reads, exactly as the SQL returns it.

    Built here rather than selected from `shadow_book`, because a backtest's
    calls are never written there — logging 400,000 simulated opinions would
    bury the real ones (D42 draws that line for the live engine too).
    """
    return {
        "id": 0,
        "symbol": symbol,
        "tier": "sim",
        "recipe": recipe,
        "strategy": "swing",
        "called_on": called_on,
        "proj_entry_low": plan.entry_low,
        "proj_entry_high": plan.entry_high,
        "proj_stop": plan.stop,
        "proj_target_1": plan.target_1,
        "proj_target_2": plan.target_2,
        "proj_horizon_days": horizon_days,
        "current_stop": plan.stop,
        "stop_to_breakeven_on": None,
        "hit_target_1": False,
        "hit_target_2": False,
        "last_evaluated_on": None,
        "max_favorable_excursion": None,
        "max_adverse_excursion": None,
        "actual_entry": None,
        "actual_entry_at": None,
    }


OHLC = ("open", "high", "low", "close")


def bar_columns(bars: pd.DataFrame) -> dict[str, str]:
    """Which OHLC columns to read: the ADJUSTED ones when they exist.

    The same series the features were measured on, necessarily. Measuring
    structure on a split-adjusted frame and filling against the raw one would
    put the stop on the wrong side of a 10-for-1.
    """
    return {field: (f"adj_{field}" if f"adj_{field}" in bars.columns else field) for field in OHLC}


def forward_bars(bars: pd.DataFrame, *, after: date, horizon_days: int) -> list[dict]:
    """Bars STRICTLY AFTER the call, which is what next-day entry means.

    A plan is formed on the close of the day that produced it, so the first bar
    it can trade against is the next one. Filling on the signal bar is the
    single most common way a backtest invents an edge: the high that triggered
    the entry is already in the past when the decision is made.

    The window reaches 2x the horizon plus slack, which is what the tracker
    needs to be able to close the trade itself — a plan can wait unfilled for a
    whole horizon and then be held for another one.
    """
    if bars.empty:
        return []
    limit = after + timedelta(days=2 * horizon_days + FORWARD_SLACK_DAYS)
    window = bars[(bars["session_date"] > after) & (bars["session_date"] <= limit)]
    if window.empty:
        return []

    columns = bar_columns(bars)
    frame = window[["session_date", *columns.values()]].rename(
        columns={source: field for field, source in columns.items()}
    )
    return [
        {
            "session_date": row["session_date"],
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        }
        for row in frame.to_dict("records")
    ]


def resolve_plan(
    *,
    symbol: str,
    recipe: str,
    plan: TradePlan,
    called_on: date,
    bars: pd.DataFrame,
    dividends: list[dict],
    costs: CostModel,
    shares: float,
    vehicle: Vehicle = "shares",
    horizon_days: int | None = None,
) -> SimFill:
    """Hand one plan to the tracker's rules and price the result."""
    horizon = horizon_days or plan.horizon_days[1]
    rows = forward_bars(bars, after=called_on, horizon_days=horizon)
    if not rows:
        # No forward bars at all: the plan was made on the last session we hold.
        # Not a trade, and NOT a loss — counting it either way would invent an
        # outcome from the edge of the data.
        return _empty("no_forward_data")

    result: TrackResult = evaluate_call(
        _call_dict(
            symbol=symbol, recipe=recipe, plan=plan, called_on=called_on, horizon_days=horizon
        ),
        rows,
        dividends,
        as_of=rows[-1]["session_date"],
    )

    net = None
    if result.actual_entry is not None and result.actual_exit is not None:
        net = costs.net_r_multiple(
            entry=result.actual_entry,
            exit_price=result.actual_exit,
            stop=plan.stop,
            shares=shares,
            vehicle=vehicle,
        )
    return SimFill(
        outcome=result.outcome,
        entered_on=result.actual_entry_at,
        entry=result.actual_entry,
        exit_price=result.actual_exit,
        exit_on=result.actual_exit_at,
        gross_r=result.r_multiple,
        net_r=net,
        return_pct=result.return_pct,
        days_held=result.days_held,
        mfe=result.mfe,
        mae=result.mae,
        hit_target_1=result.hit_target_1,
        hit_target_2=result.hit_target_2,
        stop_to_breakeven_on=result.stop_to_breakeven_on,
    )


def _empty(outcome: str) -> SimFill:
    return SimFill(
        outcome=outcome,
        entered_on=None,
        entry=None,
        exit_price=None,
        exit_on=None,
        gross_r=None,
        net_r=None,
        return_pct=None,
        days_held=None,
        mfe=None,
        mae=None,
        hit_target_1=False,
        hit_target_2=False,
        stop_to_breakeven_on=None,
    )
