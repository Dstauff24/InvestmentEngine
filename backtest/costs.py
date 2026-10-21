"""What a plan costs to execute (D39(4)).

A backtest that fills at the signal price is a backtest of a different
strategy — one with no spread, no slip and no commission, whose edge can be
entirely the friction it ignored. So every simulated trade is charged, and the
report carries gross and net R side by side so the size of the haircut is never
hidden inside a single number.

**SHARES ARE THE ONLY VEHICLE ANY HISTORICAL TRADE USED.** D36 picks the
vehicle from IV rank, and the IV history starts 2026-10-21 (D7/D22), so no idea
in the backtest window could have selected optionality. The option model below
is complete and unexercised, which is a statement about the data rather than
about the model: it exists so the assumptions are visible and so the day IV
rank exists the cost side already does too.

Everything here is pure arithmetic over numbers the caller supplies. No
database, no config lookups beyond the one the caller passes in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from core import config

Vehicle = Literal["shares", "option"]

__all__ = ["CostModel", "Fill", "Vehicle"]


@dataclass(frozen=True)
class Fill:
    """One side of a trade, after friction."""

    intended: float
    """The price the plan said."""
    filled: float
    """What it actually cost, slippage applied ADVERSELY."""
    commission: float

    @property
    def slippage(self) -> float:
        return abs(self.filled - self.intended)


@dataclass(frozen=True)
class CostModel:
    """Friction, from `risk.yaml`'s `execution` block.

    Constructed from config by default so a caller cannot quietly backtest a
    frictionless world by forgetting an argument.
    """

    slippage_bps: float
    commission_per_share: float
    min_commission: float
    option_premium_pct: float
    option_delta: float
    option_spread_pct: float
    option_commission_per_contract: float

    @classmethod
    def from_config(cls, cfg=None) -> CostModel:
        execution = (cfg or config.risk()).execution
        return cls(
            slippage_bps=execution.slippage_bps,
            commission_per_share=execution.commission_per_share,
            min_commission=execution.min_commission,
            option_premium_pct=execution.options.premium_pct_of_spot,
            option_delta=execution.options.delta,
            option_spread_pct=execution.options.spread_pct_of_premium,
            option_commission_per_contract=execution.options.commission_per_contract,
        )

    @classmethod
    def free(cls) -> CostModel:
        """A frictionless model, for isolating the effect of friction itself."""
        return cls(0.0, 0.0, 0.0, 5.0, 0.55, 0.0, 0.0)

    # ── shares ──────────────────────────────────────────────────────────────

    def buy(self, price: float, shares: float) -> Fill:
        """A long entry. Slippage pays UP: the fill is worse than the plan."""
        return self._fill(price, shares, direction=+1)

    def sell(self, price: float, shares: float) -> Fill:
        """A long exit. Slippage pays DOWN, including on a stop.

        A stop-loss is a market order once it triggers, so the realised price is
        at or below the stop — modelling an exact fill at the stop would make
        every loss exactly 1R, which is the one thing a stop-out reliably is
        not.
        """
        return self._fill(price, shares, direction=-1)

    def _fill(self, price: float, shares: float, *, direction: int) -> Fill:
        slip = price * self.slippage_bps / 10_000.0
        commission = max(abs(shares) * self.commission_per_share, self.min_commission)
        return Fill(intended=price, filled=price + direction * slip, commission=commission)

    def net_r_multiple(
        self,
        *,
        entry: float,
        exit_price: float,
        stop: float,
        shares: float,
        vehicle: Vehicle = "shares",
    ) -> float | None:
        """The R multiple a human would actually have booked.

        R is measured against the PLANNED risk (entry minus stop as the plan
        stated them), not against the slipped entry: the plan is what the
        position was sized from, so dividing by a retrospectively larger risk
        would quietly shrink every loss.
        """
        risk_per_share = entry - stop
        if risk_per_share <= 0 or shares <= 0:
            return None

        bought = self.buy(entry, shares)
        sold = self.sell(exit_price, shares)
        gross = (sold.filled - bought.filled) * shares
        costs = bought.commission + sold.commission
        if vehicle == "option":
            costs += self.option_friction(entry=entry, shares=shares)
        return (gross - costs) / (risk_per_share * shares)

    # ── options (modelled, unexercised) ─────────────────────────────────────

    def contracts_for(self, *, shares: float) -> float:
        """Contracts whose delta matches the share plan's exposure.

        One contract is 100 shares of exposure scaled by delta, so matching a
        share plan takes MORE contracts than `shares / 100` — the leverage is
        in the premium outlay, not in the exposure.
        """
        if self.option_delta <= 0:
            return 0.0
        return shares / (100.0 * self.option_delta)

    def option_friction(self, *, entry: float, shares: float) -> float:
        """Dollars lost to the spread and commissions on an option vehicle.

        `spread_pct_of_premium` is the FULL bid-ask width. Buying at the ask
        pays half of it and selling at the bid pays the other half, so a round
        trip costs exactly one width — not two, which is the easy mistake and
        would double the only cost that matters here.

        It is charged against the PREMIUM rather than the stock, which is why it
        dominates: a 4% spread on a premium worth 5% of spot is 0.2% of the
        notional, against 0.05% for the share spread at 5bp.
        """
        contracts = self.contracts_for(shares=shares)
        premium_per_contract = entry * self.option_premium_pct / 100.0 * 100.0
        round_trip_spread = premium_per_contract * self.option_spread_pct / 100.0
        return contracts * (round_trip_spread + 2.0 * self.option_commission_per_contract)
