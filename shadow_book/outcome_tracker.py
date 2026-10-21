"""Mark open Shadow Book calls against daily bars (D11/D13/D18/D19).

Runs in the daily pipeline. For every open call it reads the bars since the
call and decides what happened, in the order the market would have imposed it.

**ORDER WITHIN A BAR IS THE HARD PART, and it is resolved pessimistically.** A
daily bar gives open/high/low/close and no sequence, so a bar that touched both
the stop and TP1 is ambiguous — and the ambiguity is not symmetric in cost.
Assuming the target came first would flatter every result in exactly the
situation where a real trade was most likely stopped out. So:

  * stop before target, on any bar that touched both;
  * EXCEPT once the stop has moved to breakeven, where "both" means the trade
    was already risk-free and TP1 had already been paid.

That is a modelling choice, not a measurement, and D39(4)'s backtest will
re-examine it with the same rule so the two agree.

**DIVIDEND-ADJUSTED STOPS (D19(2)).** A stock that goes ex-dividend drops by
roughly the dividend, and a stop sitting below it has not been "hit" by
anything the thesis cares about. The stop is lowered by the dividend on the
ex-date, so the engine does not record a loss the market did not inflict.

**IDEMPOTENT.** `last_evaluated_on` means re-running a day cannot double-count
an excursion. A call made today is skipped entirely: it was called at today's
close and today's bar is the bar it was called on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from core import clock, config, db

log = logging.getLogger(__name__)

JOB_NAME = "outcome_tracker"

__all__ = ["Change", "TrackResult", "evaluate_call", "run", "track_open_calls"]


OPEN_CALLS = """
    SELECT id, symbol, tier, recipe, strategy, called_on,
           proj_entry_low, proj_entry_high, proj_stop, proj_target_1, proj_target_2,
           proj_horizon_days, current_stop, stop_to_breakeven_on,
           hit_target_1, hit_target_2, last_evaluated_on,
           max_favorable_excursion, max_adverse_excursion
    FROM   shadow_book
    WHERE  outcome = 'open' AND called_on < %s
    ORDER  BY called_on, symbol
"""

BARS = """
    SELECT session_date, open, high, low, close
    FROM   prices_daily
    WHERE  symbol = %s AND session_date > %s AND session_date <= %s
    ORDER  BY session_date
"""

DIVIDENDS = """
    SELECT ex_date, cash_amount
    FROM   corporate_actions
    WHERE  symbol = %s AND action_type = 'cash_dividend'
      AND  ex_date > %s AND ex_date <= %s
    ORDER  BY ex_date
"""

UPDATE = """
    UPDATE shadow_book SET
        outcome                 = %(outcome)s,
        hit_target_1            = %(hit_target_1)s,
        hit_target_2            = %(hit_target_2)s,
        hit_stop                = %(hit_stop)s,
        current_stop            = %(current_stop)s,
        stop_to_breakeven_on    = COALESCE(stop_to_breakeven_on, %(stop_to_breakeven_on)s),
        max_favorable_excursion = %(mfe)s,
        max_adverse_excursion   = %(mae)s,
        actual_entry            = COALESCE(actual_entry, %(actual_entry)s),
        actual_entry_at         = COALESCE(actual_entry_at, %(actual_entry_at)s),
        actual_exit             = %(actual_exit)s,
        actual_exit_at          = %(actual_exit_at)s,
        r_multiple              = %(r_multiple)s,
        return_pct              = %(return_pct)s,
        days_held               = %(days_held)s,
        closed_at               = %(closed_at)s,
        last_evaluated_on       = %(last_evaluated_on)s,
        updated_at              = now()
    WHERE id = %(id)s
"""


@dataclass
class Change:
    """One state transition, for the digest to report."""

    idea_id: int
    symbol: str
    recipe: str
    tier: str
    event: str
    """`entered`, `target_1`, `target_2`, `stopped`, `timeout`, `never_triggered`."""
    detail: str = ""

    def __str__(self) -> str:
        return (
            f"{self.symbol} ({self.recipe}, Tier {self.tier}): {self.event} {self.detail}".strip()
        )


@dataclass
class TrackResult:
    """The new state of one call after reading its bars."""

    outcome: str
    hit_target_1: bool = False
    hit_target_2: bool = False
    hit_stop: bool = False
    current_stop: float | None = None
    stop_to_breakeven_on: date | None = None
    mfe: float | None = None
    mae: float | None = None
    actual_entry: float | None = None
    actual_entry_at: date | None = None
    actual_exit: float | None = None
    actual_exit_at: date | None = None
    r_multiple: float | None = None
    return_pct: float | None = None
    days_held: int | None = None
    changes: list[Change] = field(default_factory=list)


def evaluate_call(
    call: dict, bars: list[dict], dividends: list[dict], *, as_of: date
) -> TrackResult:
    """Walk the bars since the call and decide what happened. Pure.

    Entry first: a plan with an entry ZONE is not filled until price trades
    into it, and a call that never filled is `never_triggered` rather than a
    loss — counting unfilled plans as losses would make selectivity look like
    incompetence.
    """
    exits = config.risk().exits
    entry_low = float(call["proj_entry_low"])
    entry_high = float(call["proj_entry_high"])
    target_1 = float(call["proj_target_1"])
    target_2 = float(call["proj_target_2"])
    stop = float(call["current_stop"] or call["proj_stop"])
    horizon = int(call["proj_horizon_days"] or exits.swing.max_holding_days or 45)

    result = TrackResult(outcome="open", current_stop=stop)
    result.hit_target_1 = bool(call.get("hit_target_1"))
    result.hit_target_2 = bool(call.get("hit_target_2"))
    result.actual_entry = _f(call.get("actual_entry"))
    breakeven_on = call.get("stop_to_breakeven_on")

    dividend_by_date = {_d(d["ex_date"]): float(d["cash_amount"] or 0) for d in dividends}
    entry_price = result.actual_entry
    entered_on = _d(call.get("actual_entry_at")) if call.get("actual_entry_at") else None
    mfe = _f(call.get("max_favorable_excursion"))
    mae = _f(call.get("max_adverse_excursion"))

    for bar in bars:
        day = _d(bar["session_date"])
        high, low = float(bar["high"]), float(bar["low"])

        # D19(2): an ex-dividend drop is not a stop-out. Lower the stop by the
        # dividend so the engine does not record a loss the market did not
        # inflict on the thesis.
        dividend = dividend_by_date.get(day)
        if dividend:
            stop -= dividend
            result.current_stop = stop

        if entry_price is None:
            # THE ENTRY DEADLINE, enforced here rather than left to the caller.
            # A plan that has not filled within its horizon is dead (the
            # `never_triggered` rule below), and that was previously true only
            # because this runs DAILY: the next run would close it before it
            # could fill. Two callers can no longer agree by accident —
            # `backtest/resolve.py` hands over a whole forward window at once,
            # and without this it would fill a plan on day 25 that the live
            # tracker had abandoned on day 16. It also covers a pipeline that
            # missed a week: a resumed tracker must not fill an expired plan.
            if (day - _d(call["called_on"])).days > horizon:
                break
            # A limit inside the zone fills when price trades into it; the fill
            # is taken at the zone edge rather than the bar's extreme, which
            # would assume a better price than a resting order would get.
            if low <= entry_high:
                entry_price = min(entry_high, max(low, entry_low))
                entered_on = day
                result.actual_entry = entry_price
                result.actual_entry_at = day
                result.changes.append(
                    Change(
                        call["id"],
                        call["symbol"],
                        call["recipe"],
                        call["tier"],
                        "entered",
                        f"at {entry_price:.2f}",
                    )
                )
            else:
                continue

        risk = entry_price - float(call["proj_stop"])
        mfe = max(mfe or 0.0, high - entry_price)
        mae = min(mae or 0.0, low - entry_price)

        touched_stop = low <= stop
        touched_t1 = high >= target_1
        touched_t2 = high >= target_2

        # THE AMBIGUOUS BAR. No intrabar sequence, and the cost is asymmetric:
        # assuming the target came first flatters every result in exactly the
        # case where a real trade was most likely stopped out. Pessimistic
        # unless the stop is already at breakeven, where TP1 was already paid.
        if touched_stop and not (result.hit_target_1 and breakeven_on):
            result.hit_stop = True
            result.outcome = "stopped"
            result.actual_exit = stop
            result.actual_exit_at = day
            result.changes.append(
                Change(
                    call["id"],
                    call["symbol"],
                    call["recipe"],
                    call["tier"],
                    "stopped",
                    f"at {stop:.2f}",
                )
            )
            break

        if touched_t2 and not result.hit_target_2:
            result.hit_target_2 = True
            result.hit_target_1 = True
            result.outcome = "target_2"
            result.actual_exit = target_2
            result.actual_exit_at = day
            result.changes.append(
                Change(
                    call["id"],
                    call["symbol"],
                    call["recipe"],
                    call["tier"],
                    "target_2",
                    f"at {target_2:.2f}",
                )
            )
            break

        if touched_t1 and not result.hit_target_1:
            result.hit_target_1 = True
            result.changes.append(
                Change(
                    call["id"],
                    call["symbol"],
                    call["recipe"],
                    call["tier"],
                    "target_1",
                    f"at {target_1:.2f} — trimmed, stop to breakeven",
                )
            )
            # D18: this is what zeroes the open heat. The stop moves to the
            # entry, so the remainder is risk-free and the heat cap frees up.
            if exits.swing.move_stop_to_breakeven_at == "target_1":
                stop = max(stop, entry_price)
                result.current_stop = stop
                breakeven_on = day
                result.stop_to_breakeven_on = day

        if touched_stop and result.hit_target_1 and breakeven_on:
            # Stopped at breakeven after the trim: D11 calls this
            # "breakeven-or-better = acceptable".
            result.outcome = "target_1"
            result.actual_exit = stop
            result.actual_exit_at = day
            break

        held = (day - (entered_on or day)).days
        if held >= horizon and _has_timeout(call["strategy"], exits):
            result.outcome = "timeout"
            result.actual_exit = float(bar["close"])
            result.actual_exit_at = day
            result.changes.append(
                Change(
                    call["id"],
                    call["symbol"],
                    call["recipe"],
                    call["tier"],
                    "timeout",
                    f"after {held} days at {result.actual_exit:.2f}",
                )
            )
            break

    if entry_price is None:
        # Never filled. `never_triggered` once the horizon has passed, because
        # a plan that waited too long is no longer the plan that was called.
        waited = (as_of - _d(call["called_on"])).days
        if waited > horizon:
            result.outcome = "never_triggered"
            result.changes.append(
                Change(
                    call["id"],
                    call["symbol"],
                    call["recipe"],
                    call["tier"],
                    "never_triggered",
                    f"entry never filled in {waited} days",
                )
            )
        result.mfe, result.mae = mfe, mae
        return result

    result.mfe, result.mae = mfe, mae
    risk = entry_price - float(call["proj_stop"])
    if result.actual_exit is not None and risk > 0:
        result.r_multiple = (result.actual_exit - entry_price) / risk
        result.return_pct = (result.actual_exit - entry_price) / entry_price * 100.0
        result.days_held = (_d(result.actual_exit_at) - (entered_on or _d(call["called_on"]))).days
    return result


def track_open_calls(*, as_of: date | None = None) -> list[Change]:
    """Evaluate every open call against the bars since it was made.

    Returns the state changes, which the digest reports as yesterday's news.
    """
    day = as_of or clock.session_date()
    calls = db.fetch_all(OPEN_CALLS, (day,))
    if not calls:
        log.info("no open shadow-book calls to track")
        return []

    changes: list[Change] = []
    for call in calls:
        # Idempotent: a day already evaluated is not re-read.
        if call["last_evaluated_on"] and _d(call["last_evaluated_on"]) >= day:
            continue
        since = _d(call["last_evaluated_on"] or call["called_on"])
        bars = db.fetch_all(BARS, (call["symbol"], since, day))
        if not bars:
            continue
        dividends = db.fetch_all(DIVIDENDS, (call["symbol"], since, day))
        result = evaluate_call(call, bars, dividends, as_of=day)

        db.execute(
            UPDATE,
            {
                "id": call["id"],
                "outcome": result.outcome,
                "hit_target_1": result.hit_target_1,
                "hit_target_2": result.hit_target_2,
                "hit_stop": result.hit_stop,
                "current_stop": result.current_stop,
                "stop_to_breakeven_on": result.stop_to_breakeven_on,
                "mfe": result.mfe,
                "mae": result.mae,
                "actual_entry": result.actual_entry,
                "actual_entry_at": (
                    clock.session_close_utc(result.actual_entry_at)
                    if result.actual_entry_at
                    else None
                ),
                "actual_exit": result.actual_exit,
                "actual_exit_at": (
                    clock.session_close_utc(result.actual_exit_at)
                    if result.actual_exit_at
                    else None
                ),
                "r_multiple": result.r_multiple,
                "return_pct": result.return_pct,
                "days_held": result.days_held,
                "closed_at": (clock.session_close_utc(day) if result.outcome != "open" else None),
                "last_evaluated_on": day,
            },
        )
        changes.extend(result.changes)

    log.info("tracked %d open call(s), %d change(s)", len(calls), len(changes))
    return changes


def run(*, as_of: date | None = None) -> int:
    """Pipeline entry point. Returns the number of state changes."""
    from core.jobs import job_run

    day = as_of or clock.session_date()
    with job_run(JOB_NAME, session_date=day, is_critical=False, triggered_by="manual") as job:
        changes = track_open_calls(as_of=day)
        job.note(changes=[str(c) for c in changes][:50])
        job.add_rows(len(changes))
    return len(changes)


def _has_timeout(strategy: str | None, exits) -> bool:
    """D-exits: swings time out on days, options at expiration, holds never."""
    if strategy == "swing":
        return exits.swing.max_holding_days is not None
    # An option is bounded by its own expiration and a hold exits on a thesis
    # trigger, so neither gets a day-count clock (a second clock would fire
    # before the thesis had a chance).
    return False


def _f(value) -> float | None:
    return None if value is None else float(value)


def _d(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
