"""Read-time price adjustment: raw bars x corporate actions -> adjusted series.

`prices_daily` stores prices exactly as reported and never restates them;
`corporate_actions` stores the action log. Adjusted series are produced HERE, on
demand (D24). See the header comment on `prices_daily` in db/schema.sql for why
the adjusted values are not materialised.

Two modes, because D19 needs both and they are not the same series:

  * ``split_only`` — the chart a human sees. Prior highs, base boundaries and
    support levels must line up with what a trader looking at a broker chart
    sees, and dividend-adjusting those levels moves them off the real prices.
    Pillar A technical structure uses this.

  * ``total_return`` — dividends reinvested. D19 mandates total return for
    performance measurement, the SPY benchmark, hold projections, and
    dividend-adjusted stops (so an ex-date gap cannot fake a stop-out).
    Backtests and the Shadow Book use this.

Everything here is a pure function over frames. No database, no network — the
DB read lives in the caller.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

import pandas as pd

AdjustmentMode = Literal["split_only", "total_return"]

SPLIT_TYPES = frozenset({"split"})
CASH_TYPES = frozenset({"cash_dividend", "special_dividend"})

PRICE_COLUMNS = ("open", "high", "low", "close")


class AdjustmentError(ValueError):
    """The action log and the bar series cannot be reconciled."""


def compute_adjustment_factors(
    bars: pd.DataFrame,
    actions: pd.DataFrame,
    *,
    mode: AdjustmentMode = "total_return",
    as_of: date | None = None,
    strict: bool = True,
) -> pd.DataFrame:
    """Return per-bar price and volume adjustment factors.

    Args:
        bars: columns ``session_date`` and ``close`` at minimum, one row per
            session, any order. ``close`` must be the raw unadjusted close.
        actions: columns ``action_type``, ``ex_date``, and — depending on type —
            ``split_ratio`` or ``cash_amount``. May be empty.
        mode: ``split_only`` applies splits only; ``total_return`` also folds in
            cash distributions.
        as_of: ignore any action with ``ex_date`` after this date. **This is what
            makes a backtest reproducible**: re-running it next year sees the
            same series it saw today, instead of one silently restated by every
            dividend paid in between.
        strict: raise if a cash distribution has no preceding bar to price it
            against. With ``strict=False`` that action is skipped (factor 1.0).

    Returns:
        A frame indexed by ``session_date`` with ``price_factor`` and
        ``volume_factor``. Multiply raw values by these. The most recent bars
        always come out at 1.0; only history is scaled.

    Convention: an action's factor applies to every bar STRICTLY BEFORE its
    ex-date. The ex-date bar itself already trades at the post-action price.
    """
    if "session_date" not in bars.columns:
        raise AdjustmentError("bars must have a 'session_date' column")

    out = pd.DataFrame(index=pd.Index(_dates(bars["session_date"]), name="session_date"))
    out["price_factor"] = 1.0
    out["volume_factor"] = 1.0
    out = out.sort_index()

    if out.empty or actions is None or len(actions) == 0:
        return out

    relevant = _relevant_actions(actions, mode=mode, as_of=as_of)
    if relevant.empty:
        return out

    closes = _close_by_date(bars)

    # Walk actions newest-first, accumulating the factor that applies to every
    # bar older than the one being processed. This is standard back-adjustment:
    # today is the anchor at 1.0 and history is scaled to match it.
    price_cum = 1.0
    volume_cum = 1.0
    dates = out.index

    for row in relevant.sort_values("ex_date", ascending=False).itertuples(index=False):
        ex = row.ex_date
        if row.action_type in SPLIT_TYPES:
            ratio = float(row.split_ratio)
            if ratio <= 0:
                raise AdjustmentError(f"split on {ex} has non-positive ratio {ratio}")
            price_cum *= 1.0 / ratio
            volume_cum *= ratio
        else:
            prev_close = _close_before(closes, ex)
            if prev_close is None or prev_close <= 0:
                if strict:
                    raise AdjustmentError(
                        f"cash distribution on {ex} has no usable prior close in the "
                        f"bar series; cannot compute its adjustment factor"
                    )
                continue
            amount = float(row.cash_amount)
            price_cum *= max(0.0, 1.0 - amount / prev_close)

        before = dates < ex
        out.loc[before, "price_factor"] = price_cum
        out.loc[before, "volume_factor"] = volume_cum

    return out


def adjust_bars(
    bars: pd.DataFrame,
    actions: pd.DataFrame,
    *,
    mode: AdjustmentMode = "total_return",
    as_of: date | None = None,
    strict: bool = True,
) -> pd.DataFrame:
    """Return `bars` with OHLC and volume adjusted. Raw columns are preserved.

    Adds ``adj_open``/``adj_high``/``adj_low``/``adj_close``/``adj_volume`` and
    the two factor columns, so a caller can always see what was applied.
    """
    factors = compute_adjustment_factors(bars, actions, mode=mode, as_of=as_of, strict=strict)
    result = bars.copy()
    keys = _dates(result["session_date"])
    price_factor = pd.Series(factors["price_factor"].reindex(keys).to_numpy(), index=result.index)
    volume_factor = pd.Series(factors["volume_factor"].reindex(keys).to_numpy(), index=result.index)

    for column in PRICE_COLUMNS:
        if column in result.columns:
            result[f"adj_{column}"] = _numeric(result[column]) * price_factor
    if "volume" in result.columns:
        result["adj_volume"] = _numeric(result["volume"]) * volume_factor

    result["price_factor"] = price_factor
    result["volume_factor"] = volume_factor
    return result


# ── internals ───────────────────────────────────────────────────────────────


def _dates(series: pd.Series) -> pd.Index:
    """Normalise a date-ish column to plain `datetime.date` values."""
    return pd.Index([_as_date(v) for v in series])


def _as_date(value: object) -> date:
    if isinstance(value, date) and not isinstance(value, pd.Timestamp):
        return value
    return pd.Timestamp(value).date()


def _numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").astype("float64")


def _relevant_actions(
    actions: pd.DataFrame, *, mode: AdjustmentMode, as_of: date | None
) -> pd.DataFrame:
    if mode not in ("split_only", "total_return"):
        raise AdjustmentError(f"unknown adjustment mode {mode!r}")
    wanted = SPLIT_TYPES if mode == "split_only" else SPLIT_TYPES | CASH_TYPES

    frame = actions.copy()
    frame["ex_date"] = _dates(frame["ex_date"])
    frame = frame[frame["action_type"].isin(wanted)]
    if as_of is not None:
        frame = frame[frame["ex_date"] <= as_of]
    return frame


def _close_by_date(bars: pd.DataFrame) -> pd.Series:
    closes = pd.Series(_numeric(bars["close"]).to_numpy(), index=_dates(bars["session_date"]))
    return closes.sort_index()


def _close_before(closes: pd.Series, ex_date: date) -> float | None:
    """The raw close on the last session strictly before `ex_date`."""
    earlier = closes.loc[closes.index < ex_date]
    if earlier.empty:
        return None
    value = float(earlier.iloc[-1])
    return None if pd.isna(value) else value


# ── the basis guard ─────────────────────────────────────────────────────────
#
# Everything above assumes `prices_daily` holds RAW bars. Nothing in a row says
# whether that is true, and a provider that restates its own history leaves no
# trace: Schwab's daily candles are split-ADJUSTED (verified 2026-10-21 across
# all 14 splits in the stored window, BKNG's 25-for-1 among them), so storing
# them whole made every read-time factor an adjustment of an adjustment.
#
# The bug was invisible for two reasons worth remembering. `corporate_actions`
# held no splits at all until the O7 backfill, so the factor was 1.0 and
# nothing looked wrong; and double-adjusting produces a plausible-looking
# series rather than an error. The only evidence either way is arithmetic the
# data can be asked for directly: a raw series JUMPS by the split ratio at the
# ex-date, and an adjusted one does not.


BASIS_CHECK_SQL = """
    WITH held AS (
        SELECT symbol, min(session_date) AS lo, max(session_date) AS hi
        FROM   prices_daily
        GROUP  BY symbol
    )
    SELECT c.symbol, c.ex_date, c.split_ratio,
           (SELECT close FROM prices_daily p
            WHERE p.symbol = c.symbol AND p.session_date < c.ex_date
            ORDER BY p.session_date DESC LIMIT 1) AS close_before,
           (SELECT close FROM prices_daily p
            WHERE p.symbol = c.symbol AND p.session_date >= c.ex_date
            ORDER BY p.session_date LIMIT 1) AS close_after
    FROM   corporate_actions c
    JOIN   held h ON h.symbol = c.symbol
    WHERE  c.action_type = 'split'
      AND  c.ex_date > h.lo AND c.ex_date <= h.hi
      AND  c.split_ratio IS NOT NULL
      AND  c.split_ratio >= %(min_ratio)s
      AND  c.ex_date <= %(as_of)s
    ORDER  BY c.symbol, c.ex_date
"""

# Only ratios far enough from 1 to be unambiguous. A 0.954 "split" is how FMP
# reports a spinoff (O10), where the price step is not the ratio and this check
# would report a violation that is really an unimplemented decision.
MIN_UNAMBIGUOUS_RATIO = 1.5
# A split ex-date also has a trading day attached to it, so the observed step
# is the ratio times that day's move. 25% of tolerance separates "the series
# jumped" from "it did not" without flagging a volatile session.
BASIS_TOLERANCE = 0.25


# A violation NAMES ITS KIND, because the two have different causes and
# different fixes and the message used to assert the first one.
#
#   PRE_ADJUSTED    the series does not step at all. The provider restated its
#                   own history (Schwab's candles), so the stored bars are
#                   adjusted and read-time adjustment double-counts. Re-source
#                   the affected sessions from a raw feed: repairable.
#
#   RATIO_MISMATCH  the series stepped, by the WRONG amount. The action log and
#                   the bars disagree about what the EVENT was, and FMP's raw
#                   feed is its adjusted feed with that same log reversed out,
#                   so both of its series carry the error. Nothing can be
#                   re-sourced; the pre-event era is refused via
#                   `price_basis_breaks`.
PRE_ADJUSTED = "pre_adjusted"
RATIO_MISMATCH = "ratio_mismatch"

# How close to 1.0 counts as "did not step at all". Wider than BASIS_TOLERANCE
# because the ex-date bar carries a real trading day: a 4% move on the day is a
# flat series, not a 1.04-for-1 split.
FLAT_TOLERANCE = 0.05


def classify_basis_violation(ratio: float, observed: float) -> str | None:
    """`None` when the series stepped as logged, else the kind of mismatch."""
    if abs(observed - ratio) <= ratio * BASIS_TOLERANCE:
        return None
    return PRE_ADJUSTED if abs(observed - 1.0) <= FLAT_TOLERANCE else RATIO_MISMATCH


def split_basis_violations(*, as_of: date, include_recorded: bool = False) -> list[dict]:
    """Splits whose stored series does not step by its logged ratio.

    One row per mismatch, carrying the ratio the log claims, the step the bars
    actually show, and the `kind` that says which failure it is. An empty list
    is the assertion D24 rests on, checked rather than assumed.

    Breaks recorded in `price_basis_breaks` are EXCLUDED by default: a recorded
    break is a deliberate, visible refusal of that era rather than an open
    defect, and `load_bars` already drops the bars it covers. Pass
    `include_recorded=True` to see them anyway — the check prints them, so an
    operator reads what is quarantined instead of having to remember.
    """
    from core import db

    rows = db.fetch_all(BASIS_CHECK_SQL, {"as_of": as_of, "min_ratio": MIN_UNAMBIGUOUS_RATIO})
    recorded = basis_breaks()
    out = []
    for row in rows:
        before, after = row["close_before"], row["close_after"]
        if before is None or not after:
            continue
        ratio = float(row["split_ratio"])
        observed = float(before) / float(after)
        kind = classify_basis_violation(ratio, observed)
        if kind is None:
            continue
        known = recorded.get(row["symbol"]) == row["ex_date"]
        if known and not include_recorded:
            continue
        out.append(
            {
                "symbol": row["symbol"],
                "ex_date": row["ex_date"],
                "ratio": ratio,
                "observed_step": round(observed, 4),
                "kind": kind,
                "recorded": known,
            }
        )
    return out


def basis_breaks() -> dict[str, date]:
    """`{symbol: break_date}` — the first session each symbol can be read from.

    One query, because every caller wants the whole (tiny) table and `load_bars`
    runs once per symbol per session in a walk. The table has no row for the
    overwhelming majority of symbols and the dict is the fast path for them.
    """
    from core import db

    rows = db.fetch_all("SELECT symbol, break_date FROM price_basis_breaks")
    return {r["symbol"]: r["break_date"] for r in rows or []}


def drop_pre_break(bars: pd.DataFrame, break_date: date | None) -> pd.DataFrame:
    """Bars from `break_date` forward. Pure; `None` returns the frame unchanged.

    The dropped era is not recoverable from any series the provider serves (see
    `db/migrations/022_price_basis_breaks.sql`), so this is the standing
    `features/` convention applied to a whole stretch of history: refuse it
    rather than hand back a plausible-looking wrong number.
    """
    if break_date is None or bars is None or bars.empty:
        return bars
    keep = _dates(bars["session_date"]) >= break_date
    return bars.loc[keep].reset_index(drop=True)
