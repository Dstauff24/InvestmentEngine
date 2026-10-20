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
