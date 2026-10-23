"""Return ON CAPITAL — time-weighted, with deposits and withdrawals removed.

    python -m decision.performance
    python -m decision.performance --days 90

**AN ACCOUNT THAT DOUBLES BECAUSE MONEY ARRIVED HAS RETURNED NOTHING**, and a
report that cannot tell the difference will be believed in whichever direction
flatters. That is the whole reason this module exists (O17): `account_snapshots`
carries a daily value, and raw balance growth over a period of regular deposits
measures the deposits. So external flows are detected, REMOVED, and the return
is CHAINED across them — the standard time-weighted calculation, which is the
one that describes the strategy rather than the saver.

**A FLOW IS ONLY CLAIMED ON A DAY IT CAN BE KNOWN EXACTLY.** The obvious
approach — call every cash change a flow — is wrong, because a purchase moves
cash into market value and a sale moves it back, and neither is a flow. But on
a day when NO POSITION QUANTITY CHANGED there was no trade, so cash moved for
some other reason, and `Δcash` IS the flow, exactly and without estimating a
fill price. On a day quantities DID change the residual cannot be separated
from the trade without the trade's price: those days are reported as
**UNATTRIBUTED** rather than guessed, and their cash movement is attributed to
trading. The engine holds no transaction history yet, so an unattributed day is
a stated gap in the measurement and not a silent assumption.

**A DIVIDEND IS RETURN; A DEPOSIT IS NOT; AND THEY LOOK IDENTICAL IN A BALANCE
SERIES.** Both raise cash with no position change. Where the engine holds the
distribution (`corporate_actions`, which also drives D19/D24), the credit is
matched by amount and date and classified INTERNAL, so it counts toward the
return it really is. Where it does not — and for a symbol outside the D14
universe there will BE no row, because `corporate_actions.symbol` is a foreign
key into `tickers` — an unmatched credit is called a deposit and the return is
UNDERSTATED by that much. That is the conservative direction and it is reported
rather than buried: every inferred flow is labelled `inferred`, and Schwab's
read-only transactions endpoint is the correct source and the first thing to
add.

**IT READS; IT NEVER DECIDES.** Nothing here sizes, trades or promotes
anything, and `shadow_book` is not consulted: this measures the ACCOUNT, while
D13's expectancy measures the strategy, and the two must not be mixed — D44
keeps shadow and paper on the virtual account precisely so the Shadow Book's
history does not depend on deposits.
"""

from __future__ import annotations

import argparse
import itertools
import logging
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from core import clock, db

log = logging.getLogger(__name__)

__all__ = [
    "Flow",
    "Link",
    "Performance",
    "flows_between",
    "measure",
    "performance",
    "render",
    "time_weighted",
]

# A dividend is DECLARED with an ex-date and PAID weeks later. Where
# `payment_date` is stored the match uses it; where it is not, the credit is
# looked for in this window after the ex-date. A SANITY BOUND on a provider
# field, not a strategy knob: US payment lags run two to six weeks, and beyond
# this a credit of the same size is more likely to be something else.
DIVIDEND_PAY_LAG_DAYS = 60

# A dividend credit is matched against `quantity x cash_amount`. The tolerance
# absorbs withholding, rounding and a fractional share, and is deliberately
# TIGHT: a loose one would swallow a small deposit and overstate the return,
# which is the direction this module must never err in.
DIVIDEND_MATCH_TOLERANCE = Decimal("0.03")

ZERO = Decimal("0")


@dataclass(frozen=True)
class Flow:
    """One cash movement, classified.

    `external` leaves the return alone (it is the saver's doing). `internal` is
    part of the return. `basis` is `inferred` for anything derived from the
    balance series, which is everything until the transactions endpoint is
    wired — the label travels with the number so a reader never has to
    remember which kind they are looking at.
    """

    on: date
    amount: Decimal
    kind: str  # external | internal
    basis: str  # inferred | recorded
    why: str

    @property
    def is_external(self) -> bool:
        return self.kind == "external"


@dataclass(frozen=True)
class Link:
    """One chained sub-period: yesterday's close to today's, flows removed."""

    start: date
    end: date
    start_value: Decimal
    end_value: Decimal
    flow: Decimal
    unattributed: bool

    @property
    def growth(self) -> Decimal | None:
        """`(end - flow) / start`, or None when there was no capital at risk.

        A flow is timed at the END of the day, so a deposit arriving today
        earns nothing today. With daily snapshots that is the most accurate
        timing available and it is the conservative one.
        """
        if self.start_value <= ZERO:
            return None
        return (self.end_value - self.flow) / self.start_value


@dataclass(frozen=True)
class Performance:
    """What the period returned, and what it cost to say so."""

    start: date
    end: date
    start_value: Decimal
    end_value: Decimal
    twr: Decimal | None
    external_in: Decimal
    external_out: Decimal
    internal: Decimal
    links: tuple[Link, ...]
    flows: tuple[Flow, ...]
    unattributed_days: int
    unfunded_days: int

    @property
    def net_external(self) -> Decimal:
        return self.external_in + self.external_out

    @property
    def balance_growth(self) -> Decimal | None:
        """The number this module exists to NOT report, kept for the contrast.

        Printed beside the time-weighted return so the difference between "the
        balance went up" and "the money earned something" is visible rather
        than argued about.
        """
        if self.start_value <= ZERO:
            return None
        return (self.end_value - self.start_value) / self.start_value

    @property
    def inferred(self) -> int:
        return sum(1 for f in self.flows if f.basis == "inferred")


def _as_decimal(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


def _quantities(holdings: list[dict]) -> dict[date, dict[str, Decimal]]:
    out: dict[date, dict[str, Decimal]] = {}
    for row in holdings:
        day = out.setdefault(row["as_of_date"], {})
        key = str(row["symbol"])
        day[key] = day.get(key, ZERO) + _as_decimal(row["quantity"])
    return out


def _traded(before: dict[str, Decimal], after: dict[str, Decimal]) -> bool:
    """Did any quantity change between two days?

    A symbol appearing or disappearing counts, which is what makes a position
    opened or closed in full detectable: Schwab stops listing it, and a
    comparison that only looked at shared keys would read the sale's proceeds
    as a deposit.
    """
    return any(before.get(s, ZERO) != after.get(s, ZERO) for s in before.keys() | after.keys())


def _dividend_match(
    credit: Decimal,
    on: date,
    held: dict[str, Decimal],
    dividends: list[dict],
) -> str | None:
    """The symbol whose distribution explains this credit, if one does.

    Matched on AMOUNT against the quantity actually held, not merely on timing:
    "a dividend was due around now" would classify a deposit as return on any
    day near an ex-date.
    """
    if credit <= ZERO:
        return None
    for row in dividends:
        symbol = str(row["symbol"])
        quantity = held.get(symbol)
        if not quantity or quantity <= ZERO:
            continue
        per_share = _as_decimal(row.get("cash_amount"))
        if per_share <= ZERO:
            continue
        paid = row.get("payment_date")
        if paid:
            if paid != on:
                continue
        else:
            ex = row["ex_date"]
            if not (ex < on <= ex + timedelta(days=DIVIDEND_PAY_LAG_DAYS)):
                continue
        expected = quantity * per_share
        if expected > ZERO and abs(credit - expected) / expected <= DIVIDEND_MATCH_TOLERANCE:
            return symbol
    return None


def flows_between(
    snapshots: list[dict],
    holdings: list[dict],
    dividends: list[dict] | None = None,
) -> tuple[list[Flow], set[date]]:
    """({flows}, {days whose cash movement cannot be attributed}).

    Pure: give it rows, it gives back the classification. The DB lives in
    `performance()`.
    """
    dividends = dividends or []
    quantities = _quantities(holdings)
    flows: list[Flow] = []
    unattributed: set[date] = set()
    for previous, current in itertools.pairwise(snapshots):
        day = current["as_of_date"]
        before = quantities.get(previous["as_of_date"], {})
        after = quantities.get(day, {})
        delta = _as_decimal(current["cash"]) - _as_decimal(previous["cash"])
        if _traded(before, after):
            # A trade moved cash and the fill price is unknown, so nothing here
            # can be called a flow without inventing it.
            unattributed.add(day)
            continue
        if delta == ZERO:
            continue
        symbol = _dividend_match(delta, day, after, dividends)
        if symbol:
            flows.append(
                Flow(
                    on=day,
                    amount=delta,
                    kind="internal",
                    basis="inferred",
                    why=f"{symbol} distribution",
                )
            )
        else:
            flows.append(
                Flow(
                    on=day,
                    amount=delta,
                    kind="external",
                    basis="inferred",
                    why="deposit" if delta > ZERO else "withdrawal",
                )
            )
    return flows, unattributed


def time_weighted(
    snapshots: list[dict],
    flows: list[Flow],
    unattributed: set[date] | None = None,
) -> Performance | None:
    """Chain the sub-period returns, with external flows removed.

    **The chain starts at the first FUNDED snapshot.** A sub-period that began
    with nothing has no return to report — the first deposit into an empty
    account is not a gain — and dividing by it would be a zero-division dressed
    as infinity.
    """
    if len(snapshots) < 2:
        return None
    unattributed = unattributed or set()
    external = {f.on: f.amount for f in flows if f.is_external}
    internal_total = sum((f.amount for f in flows if not f.is_external), ZERO)

    links: list[Link] = []
    chained: Decimal | None = None
    unfunded = 0
    for previous, current in itertools.pairwise(snapshots):
        day = current["as_of_date"]
        link = Link(
            start=previous["as_of_date"],
            end=day,
            start_value=_as_decimal(previous["total_value"]),
            end_value=_as_decimal(current["total_value"]),
            flow=external.get(day, ZERO),
            unattributed=day in unattributed,
        )
        links.append(link)
        growth = link.growth
        if growth is None:
            unfunded += 1
            continue
        chained = growth if chained is None else chained * growth

    inflow = sum((a for a in external.values() if a > ZERO), ZERO)
    outflow = sum((a for a in external.values() if a < ZERO), ZERO)
    return Performance(
        start=snapshots[0]["as_of_date"],
        end=snapshots[-1]["as_of_date"],
        start_value=_as_decimal(snapshots[0]["total_value"]),
        end_value=_as_decimal(snapshots[-1]["total_value"]),
        twr=None if chained is None else chained - 1,
        external_in=inflow,
        external_out=outflow,
        internal=internal_total,
        links=tuple(links),
        flows=tuple(flows),
        unattributed_days=len(unattributed & {link.end for link in links}),
        unfunded_days=unfunded,
    )


def measure(
    snapshots: list[dict],
    holdings: list[dict],
    dividends: list[dict] | None = None,
) -> Performance | None:
    """The whole calculation over rows — the seam every test uses."""
    flows, unattributed = flows_between(snapshots, holdings, dividends)
    return time_weighted(snapshots, flows, unattributed)


SNAPSHOTS_SQL = """
    SELECT as_of_date, account_hash, total_value, cash
      FROM account_snapshots
     WHERE as_of_date >= %(since)s
       AND (%(account)s::text IS NULL OR account_hash = %(account)s)
     ORDER BY as_of_date
"""

HOLDINGS_SQL = """
    SELECT as_of_date, symbol, quantity
      FROM positions
     WHERE as_of_date >= %(since)s
       AND (%(account)s::text IS NULL OR account_hash = %(account)s)
     ORDER BY as_of_date
"""

DIVIDENDS_SQL = """
    SELECT symbol, ex_date, payment_date, cash_amount
      FROM corporate_actions
     WHERE action_type IN ('cash_dividend', 'special_dividend')
       AND ex_date >= %(earliest_ex)s
       AND symbol IN (
           SELECT DISTINCT symbol FROM positions WHERE as_of_date >= %(since)s
       )
     ORDER BY ex_date
"""


def performance(since: date, account: str | None = None) -> Performance | None:
    """Load and measure. One account, or every account pooled if none is named.

    Pooling is the right default for ONE account and wrong for several: two
    accounts' values add up, but their flows and their returns are separate
    scoreboards (D35's rule one level up). The caller names the account; the
    CLI prints which it used.
    """
    params = {"since": since, "account": account}
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(SNAPSHOTS_SQL, params)
        snapshots = [dict(r) for r in cur.fetchall()]
        cur.execute(HOLDINGS_SQL, params)
        holdings = [dict(r) for r in cur.fetchall()]
        cur.execute(
            DIVIDENDS_SQL,
            {"since": since, "earliest_ex": since - timedelta(days=DIVIDEND_PAY_LAG_DAYS)},
        )
        dividends = [dict(r) for r in cur.fetchall()]
    return measure(snapshots, holdings, dividends)


def _pct(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value * 100:+.2f}%"


def _usd(value: Decimal | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def render(result: Performance | None, *, account: str | None = None) -> str:
    """The report. It leads with the time-weighted number and SHOWS the gap."""
    who = f" — account {account[:8]}" if account else ""
    if result is None:
        return (
            f"RETURN ON CAPITAL{who}\n"
            "  not measurable yet: a time-weighted return needs two daily "
            "snapshots, and the series starts the first evening the sync runs."
        )
    lines = [
        f"RETURN ON CAPITAL{who}",
        f"  {result.start} to {result.end}   "
        f"{_usd(result.start_value)} -> {_usd(result.end_value)}",
        f"  time-weighted return   {_pct(result.twr)}   <- the return on the money",
        f"  raw balance growth     {_pct(result.balance_growth)}   "
        "<- NOT a return; it includes deposits",
        f"  external flows         in {_usd(result.external_in)}  "
        f"out {_usd(result.external_out)}  (removed from the return)",
        f"  distributions counted  {_usd(result.internal)}  (part of the return)",
    ]
    if result.flows:
        lines.append("  flows:")
        lines.extend(
            f"    {f.on}  {_usd(f.amount):>14}  {f.kind:<8} {f.basis:<8} {f.why}"
            for f in result.flows
        )
    if result.inferred:
        lines.append(
            f"  {result.inferred} flow(s) INFERRED from the balance series — no transaction "
            "history yet, so an unmatched distribution reads as a deposit and UNDERSTATES "
            "the return."
        )
    if result.unattributed_days:
        lines.append(
            f"  {result.unattributed_days} day(s) UNATTRIBUTED: quantities changed, so that "
            "day's cash movement cannot be separated from the trade without a fill price."
        )
    if result.unfunded_days:
        lines.append(
            f"  {result.unfunded_days} day(s) skipped as unfunded: no capital at risk, so "
            "no return to chain."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Time-weighted return on capital (O17).")
    parser.add_argument("--days", type=int, default=365, help="window, in calendar days")
    parser.add_argument("--account", help="account hash; default pools every account")
    args = parser.parse_args(argv)

    since = clock.today_et() - timedelta(days=args.days)
    print(render(performance(since, args.account), account=args.account))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
