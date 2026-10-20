"""Daily option-chain snapshot (D7/D22). **SACRED JOB — must never silently fail.**

    ALLOW_LIVE_CALLS=1 python -m ingest.schwab_chains              # chains, then IV
    ALLOW_LIVE_CALLS=1 python -m ingest.schwab_chains --chains-only
    python -m ingest.schwab_chains --iv-only --session-date 2026-10-20

D22 scope: strikes within +/-15% of spot x the nearest 4 MONTHLY expirations,
plus a per-ticker aggregate IV row. ~10x less data than full chains, still
enough for IV rank, skew, and expected move.

D7 is the reason this exists: "the clock only starts when we do." Every trading
day this does not run is a permanent hole — the chain as it stood on a past
afternoon is not purchasable from any vendor at any price. Both jobs here are
`is_critical=True`.

TWO SEPARATE job_runs on purpose. `schwab_chains` fetches and writes the chain;
`schwab_iv` derives the aggregate from what is already stored. That means the
orchestrator's veto list can tell a broken chain PULL from broken IV MATHS, and
IV can be recomputed after a config change without spending another day's API
budget.

The filtering is pure and lives above the I/O so it can be unit-tested against
recorded payloads with no network.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta

from core import clock, config, db
from core.jobs import job_run
from features import iv as ivmath
from ingest.runner import as_float, as_int, for_each_symbol
from ingest.schwab_client import SchwabClient, from_schwab_symbol

log = logging.getLogger(__name__)

CHAINS_JOB = "schwab_chains"
IV_JOB = "schwab_iv"

# Schwab sends -999.0 where a greek or IV is unavailable. Storing it would put a
# -999 into an IV average and quietly poison every downstream volatility number.
SENTINEL_FLOOR = -998.0

UPSERT_CHAIN = """
    INSERT INTO option_chain_snapshots (
        snapshot_ts, session_date, symbol, expiration, strike, option_type,
        contract_symbol, underlying_price, days_to_expiration, is_monthly,
        bid, ask, last, mark, volume, open_interest,
        implied_volatility, delta, gamma, theta, vega, rho,
        intrinsic_value, extrinsic_value, in_the_money, source
    ) VALUES (
        %(snapshot_ts)s, %(session_date)s, %(symbol)s, %(expiration)s, %(strike)s,
        %(option_type)s, %(contract_symbol)s, %(underlying_price)s,
        %(days_to_expiration)s, %(is_monthly)s,
        %(bid)s, %(ask)s, %(last)s, %(mark)s, %(volume)s, %(open_interest)s,
        %(implied_volatility)s, %(delta)s, %(gamma)s, %(theta)s, %(vega)s, %(rho)s,
        %(intrinsic_value)s, %(extrinsic_value)s, %(in_the_money)s, 'schwab'
    )
    ON CONFLICT (symbol, snapshot_ts, expiration, strike, option_type) DO UPDATE SET
        contract_symbol    = EXCLUDED.contract_symbol,
        underlying_price   = EXCLUDED.underlying_price,
        days_to_expiration = EXCLUDED.days_to_expiration,
        is_monthly         = EXCLUDED.is_monthly,
        bid                = EXCLUDED.bid,
        ask                = EXCLUDED.ask,
        last               = EXCLUDED.last,
        mark               = EXCLUDED.mark,
        volume             = EXCLUDED.volume,
        open_interest      = EXCLUDED.open_interest,
        implied_volatility = EXCLUDED.implied_volatility,
        delta              = EXCLUDED.delta,
        gamma              = EXCLUDED.gamma,
        theta              = EXCLUDED.theta,
        vega               = EXCLUDED.vega,
        rho                = EXCLUDED.rho,
        intrinsic_value    = EXCLUDED.intrinsic_value,
        extrinsic_value    = EXCLUDED.extrinsic_value,
        in_the_money       = EXCLUDED.in_the_money,
        ingested_at        = now()
"""

UPSERT_IV = """
    INSERT INTO iv_daily (
        ts, session_date, symbol, underlying_price,
        iv30, iv_atm_front, iv_call_atm, iv_put_atm,
        skew_25d, term_slope, expected_move_front_pct,
        iv_rank_252, iv_percentile_252, history_days,
        total_open_interest, total_option_volume, put_call_oi_ratio, source
    ) VALUES (
        %(ts)s, %(session_date)s, %(symbol)s, %(underlying_price)s,
        %(iv30)s, %(iv_atm_front)s, %(iv_call_atm)s, %(iv_put_atm)s,
        %(skew_25d)s, %(term_slope)s, %(expected_move_front_pct)s,
        %(iv_rank_252)s, %(iv_percentile_252)s, %(history_days)s,
        %(total_open_interest)s, %(total_option_volume)s, %(put_call_oi_ratio)s, 'derived'
    )
    ON CONFLICT (symbol, ts) DO UPDATE SET
        underlying_price        = EXCLUDED.underlying_price,
        iv30                    = EXCLUDED.iv30,
        iv_atm_front            = EXCLUDED.iv_atm_front,
        iv_call_atm             = EXCLUDED.iv_call_atm,
        iv_put_atm              = EXCLUDED.iv_put_atm,
        skew_25d                = EXCLUDED.skew_25d,
        term_slope              = EXCLUDED.term_slope,
        expected_move_front_pct = EXCLUDED.expected_move_front_pct,
        iv_rank_252             = EXCLUDED.iv_rank_252,
        iv_percentile_252       = EXCLUDED.iv_percentile_252,
        history_days            = EXCLUDED.history_days,
        total_open_interest     = EXCLUDED.total_open_interest,
        total_option_volume     = EXCLUDED.total_option_volume,
        put_call_oi_ratio       = EXCLUDED.put_call_oi_ratio,
        ingested_at             = now()
"""

UPDATE_HAS_OPTIONS = """
    UPDATE tickers
    SET    has_options = %(has_options)s,
           updated_at  = now()
    WHERE  symbol = %(symbol)s
      AND  has_options IS DISTINCT FROM %(has_options)s
"""


# ══════════════════════════════════════════════════════════════════════════
# Pure filtering — unit-tested; no I/O until snapshot_symbol()
# ══════════════════════════════════════════════════════════════════════════


def is_monthly_expiration(day: date, expiration_type: str | None = None) -> bool:
    """Is this a standard monthly expiration?

    Schwab tags contracts with `expirationType`. A live SPY capture showed FOUR
    values, not the three the docs suggest:

        'S'  standard monthly — the third Friday. This is what D22 wants.
        'W'  weekly
        'Q'  quarterly, expiring at quarter end
        'M'  MONTH-END, e.g. 2026-11-30 — which is a Monday

    **'M' is the trap**: it reads like "monthly" and is not. Month-end
    expirations are a separate series from the standard third-Friday monthlies,
    and mixing them would break exactly the comparability D7 accumulates history
    for. Only 'S' counts.

    In that capture the tag and the third-Friday rule agreed on all 23
    expirations, so the fallback below is sound when the tag is missing.
    """
    if expiration_type:
        return expiration_type.upper().startswith("S")
    return day.weekday() == 4 and 15 <= day.day <= 21


def select_expirations(
    expirations: list[date],
    *,
    count: int,
    monthly_only: bool,
    as_of: date,
    types: dict[date, str] | None = None,
) -> list[date]:
    """The nearest `count` expirations at or after `as_of`, monthlies by default.

    Already-expired dates are dropped: a chain payload can carry the current
    day's expiring series, and snapshotting a contract with zero life left adds
    noise to every IV aggregate.
    """
    types = types or {}
    future = sorted({d for d in expirations if d >= as_of})
    if monthly_only:
        future = [d for d in future if is_monthly_expiration(d, types.get(d))]
    return future[:count]


def within_strike_range(strike: float, spot: float, pct: float) -> bool:
    """Is `strike` inside +/-`pct`% of `spot`? (D22: 15%, boundary inclusive.)"""
    if spot <= 0:
        return False
    return abs(strike - spot) / spot <= pct / 100.0


def payload_has_contracts(payload: dict) -> bool:
    """Did Schwab return ANY listed option for this symbol?

    Checked on the RAW payload, BEFORE the D22 filter. A name whose every strike
    falls outside +/-15% of spot still has an options chain; it just has no
    strike we care to snapshot. Conflating the two would set has_options=False
    and drop a liquid ticker on the next universe rebuild.
    """
    for side in ("callExpDateMap", "putExpDateMap"):
        for strike_map in (payload.get(side) or {}).values():
            for contracts in (strike_map or {}).values():
                if contracts:
                    return True
    return False


def _clean(value: object) -> float | None:
    """Drop Schwab's -999.0 "not available" sentinel and NaN."""
    result = as_float(value)
    if result is None or result <= SENTINEL_FLOOR:
        return None
    return result


def _expiry_key_to_date(key: str) -> date | None:
    """Schwab keys expirations as '2026-09-18:17' (date : days to expiration)."""
    try:
        return date.fromisoformat(str(key).split(":", 1)[0])
    except ValueError:
        return None


def _first_expiration_type(strike_map: dict) -> str | None:
    for contracts in (strike_map or {}).values():
        for contract in contracts or []:
            tag = contract.get("expirationType")
            if tag:
                return str(tag)
    return None


def parse_chain(
    payload: dict,
    *,
    symbol: str,
    snapshot_ts: datetime,
    session_date: date,
    scope: config.OptionChainScope,
    as_of: date | None = None,
) -> list[dict]:
    """Apply the D22 filter to a Schwab chain payload and return DB rows.

    Order of operations matters: expirations are selected first (cheap, and it
    determines what "front month" means for the IV aggregates), then strikes are
    filtered inside each surviving expiration.
    """
    # D7: a truncated or delayed chain stored as if it were the whole, live
    # surface is a permanent undetectable lie. Schwab flags both; refuse rather
    # than record a partial day that looks complete.
    if payload.get("isChainTruncated"):
        raise ValueError(
            f"{symbol}: Schwab reported isChainTruncated — refusing to store a "
            f"partial chain as a D7 snapshot"
        )
    if payload.get("isDelayed"):
        raise ValueError(
            f"{symbol}: Schwab reported isDelayed — a snapshot built from delayed "
            f"quotes is not the same observation as a live one, and IV rank must "
            f"not be compared across the two"
        )

    spot = _clean(payload.get("underlyingPrice"))
    if spot is None or spot <= 0:
        underlying = payload.get("underlying") or {}
        spot = _clean(underlying.get("last") or underlying.get("mark"))
    if spot is None or spot <= 0:
        raise ValueError(f"{symbol}: chain payload has no usable underlying price")

    as_of = as_of or session_date

    maps = []
    if scope.include_calls:
        maps.append(("C", payload.get("callExpDateMap") or {}))
    if scope.include_puts:
        maps.append(("P", payload.get("putExpDateMap") or {}))

    # First pass: what expirations exist, and are they monthly?
    available: set[date] = set()
    types: dict[date, str] = {}
    for _, expiry_map in maps:
        for key, strikes in expiry_map.items():
            day = _expiry_key_to_date(key)
            if day is None:
                continue
            available.add(day)
            tag = _first_expiration_type(strikes)
            if tag:
                types[day] = tag

    wanted = set(
        select_expirations(
            sorted(available),
            count=scope.monthly_expirations,
            monthly_only=scope.monthly_only,
            as_of=as_of,
            types=types,
        )
    )
    if not wanted:
        return []

    rows: list[dict] = []
    for option_type, expiry_map in maps:
        for key, strike_map in expiry_map.items():
            expiration = _expiry_key_to_date(key)
            if expiration is None or expiration not in wanted:
                continue
            for strike_key, contracts in strike_map.items():
                strike = as_float(strike_key)
                if strike is None or not within_strike_range(strike, spot, scope.strike_range_pct):
                    continue
                for contract in contracts or []:
                    # Contracts adjusted after a corporate action deliver
                    # something other than 100 shares, and their IV is not
                    # comparable with the rest of the surface.
                    if contract.get("nonStandard") or contract.get("mini"):
                        continue
                    row = _contract_row(
                        contract,
                        symbol=symbol,
                        option_type=option_type,
                        expiration=expiration,
                        strike=strike,
                        spot=spot,
                        snapshot_ts=snapshot_ts,
                        session_date=session_date,
                        types=types,
                    )
                    if (row["open_interest"] or 0) >= scope.min_open_interest:
                        rows.append(row)
    return rows


def _contract_row(
    contract: dict,
    *,
    symbol: str,
    option_type: str,
    expiration: date,
    strike: float,
    spot: float,
    snapshot_ts: datetime,
    session_date: date,
    types: dict[date, str],
) -> dict:
    bid, ask = _clean(contract.get("bid")), _clean(contract.get("ask"))
    mark = _clean(contract.get("mark"))
    if mark is None and bid is not None and ask is not None:
        mark = (bid + ask) / 2.0

    volatility = _clean(contract.get("volatility"))
    return {
        "snapshot_ts": snapshot_ts,
        "session_date": session_date,
        "symbol": symbol,
        "expiration": expiration,
        "strike": strike,
        "option_type": option_type,
        "contract_symbol": contract.get("symbol"),
        "underlying_price": spot,
        "days_to_expiration": as_int(contract.get("daysToExpiration")),
        "is_monthly": is_monthly_expiration(expiration, types.get(expiration)),
        "bid": bid,
        "ask": ask,
        "last": _clean(contract.get("last")),
        "mark": mark,
        "volume": as_int(contract.get("totalVolume")) or 0,
        "open_interest": as_int(contract.get("openInterest")) or 0,
        # Schwab reports IV in PERCENT (28.5 = 28.5%). Stored as a decimal
        # fraction so every downstream calculation speaks one unit.
        "implied_volatility": volatility / 100.0 if volatility is not None else None,
        "delta": _clean(contract.get("delta")),
        "gamma": _clean(contract.get("gamma")),
        "theta": _clean(contract.get("theta")),
        "vega": _clean(contract.get("vega")),
        "rho": _clean(contract.get("rho")),
        "intrinsic_value": _clean(contract.get("intrinsicValue")),
        # D19(4): extrinsic vs. an upcoming dividend is the assignment-risk test.
        #
        # `timeValue`, NOT `extrinsicValue`, despite the names. Verified against
        # a live SPY chain: on an OTM call marked at 0.11, `timeValue` reads 0.07
        # (right — an OTM option is all extrinsic) while `extrinsicValue` reads
        # 84.91 (nonsense). They AGREE on ITM contracts and diverge on 2,012 of
        # 4,169 otherwise, so a spot check would not have caught it.
        "extrinsic_value": _clean(contract.get("timeValue")),
        "in_the_money": bool(contract.get("inTheMoney")),
    }


# ══════════════════════════════════════════════════════════════════════════
# I/O
# ══════════════════════════════════════════════════════════════════════════


def record_options_availability(observed: dict[str, bool]) -> int:
    """Write what we learned into `tickers.has_options` (D14).

    This is what finally ARMS the D14 options gate. `ingest/universe.py` rejects
    a symbol only on a definite `False`, and until Schwab is live nothing can set
    one, so the gate abstains. From the first successful chain run it enforces.

    Only symbols whose chain request SUCCEEDED appear in `observed`. A timeout or
    a 500 is not evidence that a stock has no options, and writing `False` on one
    would drop a liquid name out of the universe on the next rebuild.
    """
    if not observed:
        return 0
    return db.executemany(
        UPDATE_HAS_OPTIONS,
        [{"symbol": symbol, "has_options": value} for symbol, value in observed.items()],
    )


def snapshot_symbol(
    client: SchwabClient,
    symbol: str,
    *,
    snapshot_ts: datetime,
    session_date: date,
    scope: config.OptionChainScope,
    observed: dict[str, bool] | None = None,
) -> int:
    # Ask for slightly more calendar than the monthlies we want, so the fourth
    # monthly is definitely inside the window regardless of where in the month
    # today falls.
    payload = client.option_chain(
        symbol,
        from_date=session_date,
        to_date=session_date + timedelta(days=35 * (scope.monthly_expirations + 1)),
    )
    status = str(payload.get("status", "")).upper()
    if status and status != "SUCCESS":
        raise ValueError(f"{symbol}: chain status {status}")

    # Recorded only on a successful response — see record_options_availability.
    if observed is not None:
        observed[symbol] = payload_has_contracts(payload)

    rows = parse_chain(
        payload,
        symbol=from_schwab_symbol(payload.get("symbol") or symbol),
        snapshot_ts=snapshot_ts,
        session_date=session_date,
        scope=scope,
    )
    return db.executemany(UPSERT_CHAIN, rows) if rows else 0


def snapshot_chains(client: SchwabClient | None = None, *, symbols: list[str] | None = None) -> int:
    client = client or SchwabClient()
    scope = config.universe().option_chains
    day = clock.session_date()
    snapshot_ts = clock.session_close_utc(day)
    universe = symbols if symbols is not None else db.active_symbols()
    if not universe:
        raise RuntimeError("no active tickers; run `python -m ingest.universe` first")

    observed: dict[str, bool] = {}
    with job_run(CHAINS_JOB, session_date=day, is_critical=True, triggered_by="manual") as job:
        outcome = for_each_symbol(
            universe,
            lambda s: snapshot_symbol(
                client,
                s,
                snapshot_ts=snapshot_ts,
                session_date=day,
                scope=scope,
                observed=observed,
            ),
            run=job,
            label="chains",
        )
        without_options = sorted(s for s, has in observed.items() if not has)
        updated = record_options_availability(observed)
        job.note(
            failed_symbols=sorted(outcome.failed)[:50],
            snapshot_ts=str(snapshot_ts),
            has_options_updated=updated,
            symbols_without_options=without_options[:50],
        )
        if without_options:
            log.info(
                "%d symbol(s) returned no option chain; the D14 options gate will drop "
                "them on the next universe rebuild: %s",
                len(without_options),
                ", ".join(without_options[:10]),
            )
    return outcome.rows


def derive_iv(*, session_date: date | None = None) -> int:
    """Compute `iv_daily` from the chains already stored for a session.

    A separate job, and separately critical, for two reasons: it reads the
    database rather than the network, so it can be re-run to recompute the whole
    surface after a config change without burning another day's API budget; and
    a failure here is a DIFFERENT failure from "the chain pull broke", which the
    orchestrator's veto list should be able to tell apart.
    """
    day = session_date or clock.session_date()
    scope = config.universe().option_chains
    ts = clock.session_close_utc(day)

    with job_run(IV_JOB, session_date=day, is_critical=True, triggered_by="manual") as job:
        symbols = [
            r["symbol"]
            for r in db.fetch_all(
                "SELECT DISTINCT symbol FROM option_chain_snapshots "
                "WHERE session_date = %s ORDER BY symbol",
                (day,),
            )
        ]
        if not symbols:
            job.skip(f"no chain snapshots stored for {day}")
            return 0

        rows = []
        for symbol in symbols:
            contracts = db.fetch_all(
                "SELECT expiration, strike, option_type, mark, bid, ask, "
                "       implied_volatility, delta, open_interest, volume, underlying_price "
                "FROM   option_chain_snapshots "
                "WHERE  symbol = %s AND session_date = %s",
                (symbol, day),
            )
            history = [
                as_float(r["iv30"])
                for r in db.fetch_all(
                    "SELECT iv30 FROM iv_daily WHERE symbol = %s AND session_date < %s "
                    "ORDER BY session_date DESC LIMIT %s",
                    (symbol, day, scope.iv_rank_lookback_days),
                )
            ]
            summary = ivmath.summarize(
                contracts,
                as_of=day,
                min_history_days=scope.iv_rank_min_history_days,
                history=[h for h in history if h is not None],
            )
            if summary is None:
                continue
            rows.append({"ts": ts, "session_date": day, "symbol": symbol, **summary})

        written = db.executemany(UPSERT_IV, rows) if rows else 0
        job.add_rows(written)
        job.note(
            symbols=len(symbols),
            ranked=sum(1 for r in rows if r["iv_rank_252"] is not None),
        )
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--chains-only", action="store_true", help="skip the IV derivation")
    parser.add_argument("--iv-only", action="store_true", help="recompute IV from stored chains")
    parser.add_argument("--session-date", help="ISO date, for --iv-only backfills")
    parser.add_argument("--symbols", nargs="*", help="override the universe (debugging)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    day = date.fromisoformat(args.session_date) if args.session_date else None

    if args.iv_only:
        print(f"iv_daily: {derive_iv(session_date=day)} rows")
        return 0

    rows = snapshot_chains(symbols=args.symbols)
    print(f"option_chain_snapshots: {rows} rows")
    if not args.chains_only:
        print(f"iv_daily: {derive_iv(session_date=day)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
