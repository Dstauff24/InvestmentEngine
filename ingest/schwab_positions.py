"""O17: read-only positions and balances from Schwab (D52's primary product).

    ALLOW_LIVE_CALLS=1 python -m ingest.schwab_positions
    python -m ingest.schwab_positions --dry-run

**READ-ONLY IS A MANDATE, NOT A PHASE.** This module calls exactly one account
endpoint — `get_accounts(fields=POSITIONS)` — and nothing in this repo places,
previews or cancels an order, in any phase, behind any flag. The identifier
Schwab returns here is already a HASH; the plain account number lives on an
endpoint nothing calls, so the engine cannot store one it never fetched.

**IT WRITES WHAT THE BROKER SAYS AND DERIVES NOTHING IT CANNOT SEE.** Two
columns a reader might expect are deliberately left NULL:

  * **`bucket`** (D17's etf/hold/swing/option/cash). A position's bucket is a
    statement of INTENT — the same ETF is a core holding in one account and a
    tactical trade in another — and the broker has no idea which. Guessing it
    would put a position into the heat cap, or out of it, on the engine's
    assumption rather than the operator's decision.
  * **`tax_type`** on the account. Schwab reports CASH or MARGIN, which is a
    settlement mode: a taxable brokerage and a Roth IRA both come back as CASH.
    The tax type is operator-supplied and NULL until stated, and the adviser
    REFUSES to recommend a sale in an account whose tax treatment it does not
    know (D52: a guess in this layer is advice).

**D44 STILL STANDS.** Shadow and paper sizing keep using
`SHADOW_ACCOUNT_VALUE` even now that real balances exist, or the Shadow Book's
history would depend on deposits having nothing to do with the strategy. These
rows are what the ADVISER reads, not what the sleeve sizes against.
"""

from __future__ import annotations

import logging
import sys
from datetime import date
from typing import Any

from core import clock, db
from core.jobs import job_run
from ingest.schwab_client import SchwabClient, from_schwab_symbol

log = logging.getLogger(__name__)

JOB_NAME = "schwab_positions"

__all__ = ["JOB_NAME", "parse_account", "parse_positions", "run", "store"]

ASSET_TYPES = {
    "EQUITY": "EQUITY",
    "ETF": "ETF",
    "COLLECTIVE_INVESTMENT": "ETF",
    "OPTION": "OPTION",
    "MUTUAL_FUND": "MUTUAL_FUND",
    "FIXED_INCOME": "FIXED_INCOME",
    "CASH_EQUIVALENT": "CASH",
    "CURRENCY": "CASH",
}

POSITION_UPSERT = """
    INSERT INTO positions (
        as_of, as_of_date, account_hash, symbol, asset_type, quantity,
        average_price, market_value, cost_basis, unrealized_pl, unrealized_pl_pct,
        day_pl, underlying_symbol, expiration, strike, put_call, source
    ) VALUES (
        %(as_of)s, %(as_of_date)s, %(account_hash)s, %(symbol)s, %(asset_type)s,
        %(quantity)s, %(average_price)s, %(market_value)s, %(cost_basis)s,
        %(unrealized_pl)s, %(unrealized_pl_pct)s, %(day_pl)s,
        %(underlying_symbol)s, %(expiration)s, %(strike)s, %(put_call)s, 'schwab'
    )
    ON CONFLICT (account_hash, as_of_date, symbol, expiration, strike, put_call)
    DO UPDATE SET
        as_of             = EXCLUDED.as_of,
        quantity          = EXCLUDED.quantity,
        average_price     = EXCLUDED.average_price,
        market_value      = EXCLUDED.market_value,
        cost_basis        = EXCLUDED.cost_basis,
        unrealized_pl     = EXCLUDED.unrealized_pl,
        unrealized_pl_pct = EXCLUDED.unrealized_pl_pct,
        day_pl            = EXCLUDED.day_pl
"""

ACCOUNT_UPSERT = """
    INSERT INTO account_snapshots (
        as_of_date, account_hash, account_type, total_value, cash,
        long_market_value, buying_power, cash_available, source
    ) VALUES (
        %(as_of_date)s, %(account_hash)s, %(account_type)s, %(total_value)s,
        %(cash)s, %(long_market_value)s, %(buying_power)s, %(cash_available)s,
        'schwab'
    )
    ON CONFLICT (as_of_date, account_hash) DO UPDATE SET
        account_type      = EXCLUDED.account_type,
        total_value       = EXCLUDED.total_value,
        cash              = EXCLUDED.cash,
        long_market_value = EXCLUDED.long_market_value,
        buying_power      = EXCLUDED.buying_power,
        cash_available    = EXCLUDED.cash_available,
        updated_at        = now()
        -- `tax_type` is ABSENT from this list on purpose: it is
        -- operator-supplied, and a sync that overwrote it with NULL every
        -- evening would silently un-state what somebody stated once.
"""


def _f(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_account(payload: dict, *, as_of_date: date) -> dict | None:
    """One account's balances. None when the envelope is not what we expect.

    `schwab-py` wraps each account in `securitiesAccount`; unwrapped HERE so a
    shape change shows up in one place rather than in every reader.
    """
    account = payload.get("securitiesAccount") or payload
    account_hash = account.get("hashValue") or account.get("accountNumber")
    if not account_hash:
        return None
    balances = account.get("currentBalances") or {}
    return {
        "as_of_date": as_of_date,
        "account_hash": str(account_hash),
        "account_type": account.get("type"),
        "total_value": _f(balances.get("liquidationValue")),
        "cash": _f(balances.get("cashBalance")),
        "long_market_value": _f(balances.get("longMarketValue")),
        "buying_power": _f(balances.get("buyingPower")),
        "cash_available": _f(
            balances.get("cashAvailableForTrading", balances.get("availableFunds"))
        ),
    }


def parse_positions(payload: dict, *, as_of_date: date, as_of) -> list[dict]:
    """Every position in one account, as the broker reports it.

    SHORT QUANTITIES ARE SIGNED, not dropped: `shortQuantity` becomes a
    negative quantity, because a short leg of a defined-risk spread (D17) is
    part of the position and a book that counted only longs would read a
    credit spread as naked.
    """
    account = payload.get("securitiesAccount") or payload
    account_hash = str(account.get("hashValue") or account.get("accountNumber") or "")
    out: list[dict] = []
    for row in account.get("positions") or []:
        instrument = row.get("instrument") or {}
        raw_symbol = instrument.get("symbol")
        if not raw_symbol:
            continue
        long_qty = _f(row.get("longQuantity")) or 0.0
        short_qty = _f(row.get("shortQuantity")) or 0.0
        quantity = long_qty - short_qty
        if quantity == 0:
            # A closed position Schwab still lists. Not a holding.
            continue
        asset = ASSET_TYPES.get(str(instrument.get("assetType") or "").upper(), "OTHER")
        market_value = _f(row.get("marketValue"))
        cost_basis = _f(row.get("averagePrice"))
        cost_total = None if cost_basis is None else cost_basis * abs(quantity)
        unrealized = _f(row.get("longOpenProfitLoss"))
        if unrealized is None:
            unrealized = _f(row.get("currentDayProfitLoss"))
        out.append(
            {
                "as_of": as_of,
                "as_of_date": as_of_date,
                "account_hash": account_hash,
                "symbol": from_schwab_symbol(str(raw_symbol)),
                "asset_type": asset,
                "quantity": quantity,
                "average_price": cost_basis,
                "market_value": market_value,
                "cost_basis": cost_total,
                "unrealized_pl": unrealized,
                "unrealized_pl_pct": (
                    None
                    if unrealized is None or not cost_total
                    else unrealized / abs(cost_total) * 100.0
                ),
                "day_pl": _f(row.get("currentDayProfitLoss")),
                "underlying_symbol": instrument.get("underlyingSymbol"),
                "expiration": None,
                "strike": _f(instrument.get("strikePrice")),
                "put_call": (instrument.get("putCall") or None),
                # `bucket` stays NULL: see the module docstring. Intent is the
                # operator's, and the heat cap depends on it.
            }
        )
    return out


def store(accounts: list[dict], positions: list[dict]) -> int:
    for account in accounts:
        db.execute(ACCOUNT_UPSERT, account)
    for row in positions:
        db.execute(POSITION_UPSERT, row)
    return len(positions)


def run(*, as_of: date | None = None, record: bool = True) -> int:
    """Sync accounts and positions. Returns the number of position rows."""
    day = as_of or clock.session_date()
    moment = clock.session_close_utc(day)
    client = SchwabClient()

    payloads = client.accounts_with_positions() or []
    accounts = [a for a in (parse_account(p, as_of_date=day) for p in payloads) if a]
    positions: list[dict] = []
    for payload in payloads:
        positions.extend(parse_positions(payload, as_of_date=day, as_of=moment))

    if not accounts:
        # A linked account that returns nothing is a failure, not an empty
        # portfolio: `for_each_symbol`'s rule, one level up — never "ok, 0 rows"
        # when the answer should have been at least one account.
        raise RuntimeError(
            "Schwab returned no accounts. That is an auth or entitlement problem, "
            "not an empty portfolio — check the token and the linked accounts."
        )

    if record:
        with job_run(JOB_NAME, session_date=day, is_critical=False, triggered_by="cron") as job:
            written = store(accounts, positions)
            job.add_rows(written)
            job.note(
                accounts=len(accounts),
                positions=written,
                untyped_accounts=[a["account_hash"][:8] for a in accounts if not a.get("tax_type")],
            )
    else:
        written = store(accounts, positions)

    log.info("synced %d account(s), %d position(s) for %s", len(accounts), written, day)
    return written


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--as-of", help="ISO date; defaults to today's session")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what is stored without a provider call (D27 blocks it anyway "
        "without ALLOW_LIVE_CALLS)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    day = date.fromisoformat(args.as_of) if args.as_of else clock.session_date()
    if args.dry_run:
        rows = db.fetch_all(
            "SELECT account_hash, account_type, tax_type, total_value, cash "
            "FROM account_snapshots WHERE as_of_date = %s ORDER BY account_hash",
            (day,),
        )
        print(f"{len(rows)} account snapshot(s) stored for {day}")
        for row in rows:
            print(f"  {row}")
        print("tax_type NULL means nobody has stated it; the adviser will refuse to")
        print("recommend a sale in such an account rather than assume.")
        return 0
    print(f"stored {run(as_of=day)} position row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
