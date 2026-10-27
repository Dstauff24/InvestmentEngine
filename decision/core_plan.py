"""The CORE layer's plan: what to buy with new cash, and what to watch. READ-ONLY.

D52 calls this the primary product. It earns its keep without predicting
anything, because every statement it makes is about a decision already taken:
the target is 100% SWPPX, the contribution rule is "buy the target", and the
only judgement is whether reality has drifted from that.

**IT RECOMMENDS; IT NEVER ACTS.** No order placement exists anywhere in this
repo, in any phase — and the diversification proposal below is explicitly a
recommendation for review, because an automatic allocation change is advice
nobody asked for.

Three statements, each refusing rather than guessing when it cannot be made:

**CONTRIBUTION GUIDANCE, IN DOLLARS FOR A MUTUAL FUND.** SWPPX settles at NAV
in dollar amounts, so the plan says "buy $X of SWPPX" and never a share count —
a share count would be wrong by whatever the NAV moves between the digest and
the order, and for a fund there is no share price to quote in the first place.

**THE DIVERSIFICATION PROPOSAL.** Below `core_diversification_threshold_usd` a
single S&P 500 fund IS the plan; above it the engine proposes an international
index at roughly `core_international_target_pct`. The threshold exists because
diversification has a cost in simplicity and a benefit in variance, and at $200
the benefit is rounding and the cost is real.

**THE TRACKING CHECK.** An index fund should trail its index by about its
expense ratio and no more. A persistent wider gap is either the wrong fund, the
wrong benchmark, or a real tracking problem, and all three are worth an alert.
Measured against SPY total return, which is what the engine prices — SPY
carries its own fee, so the comparison is fund-against-fund and the honest
reading is "the gap moved", not "the index was beaten".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from core import config, db

log = logging.getLogger(__name__)

__all__ = ["CorePlan", "contribution_line", "core_plan", "diversification_note", "tracking_note"]

HOLDINGS_SQL = """
    SELECT symbol, asset_type, bucket, quantity, market_value
      FROM positions
     WHERE as_of_date = (SELECT max(as_of_date) FROM positions)
     ORDER BY market_value DESC NULLS LAST
"""

ACCOUNT_SQL = """
    SELECT as_of_date, total_value, cash, cash_available, tax_type
      FROM account_snapshots
     WHERE as_of_date = (SELECT max(as_of_date) FROM account_snapshots)
     ORDER BY total_value DESC NULLS LAST
     LIMIT 1
"""


@dataclass(frozen=True)
class CorePlan:
    """What the core layer says tonight."""

    as_of: date | None
    core_symbol: str | None
    core_value: float
    cash: float
    total_value: float
    lines: tuple[str, ...]

    @property
    def has_anything_to_say(self) -> bool:
        return bool(self.lines)


def contribution_line(cash: float, *, cfg=None) -> str | None:
    """ "buy $X of SWPPX", or None when there is nothing to deploy.

    None rather than "buy $0": a digest line that appears every evening saying
    nothing is a line people stop reading, and the absence of new cash is not
    news.
    """
    accounts = (cfg or config.risk()).accounts
    if not accounts.core_symbol:
        return (
            f"new cash: ${cash:,.2f} — but `accounts.core_symbol` is not set, so the plan "
            f"cannot say what to buy. Stating it is a decision, not a default."
        )
    if cash <= 0:
        return None
    if accounts.buys_in_dollars:
        return f"plan: buy ${cash:,.2f} of {accounts.core_symbol}"
    return (
        f"plan: buy ${cash:,.2f} of {accounts.core_symbol} "
        f"(an ETF trades in shares — size it at the day's price)"
    )


def diversification_note(core_value: float, *, cfg=None) -> str | None:
    """The international-index proposal, once core value crosses the threshold.

    A RECOMMENDATION FOR REVIEW. It never changes the target by itself, and it
    says so in the line, because the difference between a proposal and an
    instruction is the whole of D52's caution about this layer.
    """
    accounts = (cfg or config.risk()).accounts
    threshold = accounts.core_diversification_threshold_usd
    if core_value < threshold:
        return None
    return (
        f"FOR REVIEW: core is ${core_value:,.0f}, past the ${threshold:,.0f} "
        f"diversification threshold. The plan proposes adding an international index at "
        f"about {accounts.core_international_target_pct:g}% of core — a recommendation "
        f"for you to accept or decline, never an automatic change"
    )


def tracking_note(core_return_pct: float | None, benchmark_return_pct: float | None, *, cfg=None):
    """The tracking-gap alert, or why it cannot be computed.

    An index fund trails its index by about its fee. The alert fires on a gap
    WIDER than the expense ratio plus a tolerance, in either direction: a fund
    that beats its index by more than its fee is as much a sign of the wrong
    benchmark as one that trails it.
    """
    accounts = (cfg or config.risk()).accounts
    if core_return_pct is None or benchmark_return_pct is None:
        return None
    gap = core_return_pct - benchmark_return_pct
    allowed = accounts.core_expense_ratio_pct + accounts.core_tracking_tolerance_pct
    if abs(gap) <= allowed:
        return (
            f"tracking: core {core_return_pct:+.2f}% against the S&P 500 total return's "
            f"{benchmark_return_pct:+.2f}% — a {gap:+.2f}% gap, inside the "
            f"{allowed:.2f}% an expense ratio of {accounts.core_expense_ratio_pct:g}% "
            f"plus tolerance allows"
        )
    return (
        f"TRACKING ALERT: core {core_return_pct:+.2f}% against the S&P 500 total return's "
        f"{benchmark_return_pct:+.2f}% — a {gap:+.2f}% gap, WIDER than the {allowed:.2f}% "
        f"an expense ratio of {accounts.core_expense_ratio_pct:g}% plus tolerance allows. "
        f"An index fund should trail its index by about its fee: a bigger gap is the wrong "
        f"fund, the wrong benchmark, or a tracking problem"
    )


def core_plan(
    *,
    core_return_pct: float | None = None,
    benchmark_return_pct: float | None = None,
    cfg=None,
) -> CorePlan:
    """Read the account and say what the core layer has to say tonight."""
    accounts = (cfg or config.risk()).accounts
    snapshot = db.fetch_one(ACCOUNT_SQL) or {}
    holdings = db.fetch_all(HOLDINGS_SQL)

    cash = float(snapshot.get("cash") or 0.0)
    total = float(snapshot.get("total_value") or 0.0)
    core_value = sum(
        float(h["market_value"] or 0.0)
        for h in holdings
        if accounts.core_symbol and str(h["symbol"]).upper() == accounts.core_symbol.upper()
    )

    lines = [
        line
        for line in (
            contribution_line(cash, cfg=cfg),
            diversification_note(core_value, cfg=cfg),
            tracking_note(core_return_pct, benchmark_return_pct, cfg=cfg),
        )
        if line
    ]
    return CorePlan(
        as_of=snapshot.get("as_of_date"),
        core_symbol=accounts.core_symbol,
        core_value=core_value,
        cash=cash,
        total_value=total,
        lines=tuple(lines),
    )
