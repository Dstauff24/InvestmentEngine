"""Pillar B inputs: growth, margins, and estimate-revision momentum (D32 weight 30).

Nothing here scores anything. These are the measurements `scoring/pillars.py`
will read.

STANDING CONVENTION: insufficient data returns `None` — never a number, never
`False`. The scoring layer owns the meaning of abstention. A margin trend
computed from one quarter, or a YoY growth rate computed against a quarter that
does not exist, is a plausible-looking wrong answer that propagates into a
composite with nothing to flag it.

`fundamentals` is point-in-fact: rows are keyed by fiscal period and change
only when a company restates, so re-reading is free and the figures are what
was reported rather than what is currently believed.

Revision momentum is different in kind from everything else in this module. It
is the only reading computed from a series the ENGINE accumulates, which began
on 2026-10-21 and has no history before it. It therefore reports an
`EvidenceDepth` beside its value — see `features/evidence.py` for why, and why
it is NOT gated to None the way IV rank is.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date, timedelta

import pandas as pd

from core import db
from features.evidence import EMPTY, EvidenceDepth, depth_from_dates

log = logging.getLogger(__name__)

# A fiscal year is four quarters; "year over year" means four periods back.
QUARTERS_PER_YEAR = 4

__all__ = [
    "FundamentalFeatures",
    "RevisionMomentum",
    "compute",
    "growth_yoy",
    "load_estimate_snapshots",
    "load_fundamentals",
    "margin_trend",
    "revision_momentum",
]


# ── growth ──────────────────────────────────────────────────────────────────


def _ordered(rows: list[dict]) -> list[dict]:
    """Quarterly rows oldest first, by fiscal period end."""
    return sorted(
        (r for r in rows if r.get("fiscal_date_ending")),
        key=lambda r: r["fiscal_date_ending"],
    )


def _value(row: dict | None, field: str) -> float | None:
    if row is None or row.get(field) is None:
        return None
    return float(row[field])


def _pct_change(now: float | None, then: float | None) -> float | None:
    """Percent change, or None when it cannot be stated honestly.

    A base of zero has no percent change. A NEGATIVE base is worse than
    undefined: earnings going from -1.00 to -0.50 is an improvement, and the
    naive formula reports -50%, which reads as a collapse. Sign-flipping bases
    are common in EPS series, so they are refused rather than mis-signed.
    """
    if now is None or then is None or then == 0:
        return None
    if then < 0:
        return None
    return (now - then) / then * 100.0


def growth_yoy(
    rows: list[dict], field: str, *, periods_back: int = QUARTERS_PER_YEAR
) -> float | None:
    """Latest period against the same period a year earlier, in percent.

    Year-over-year rather than sequential, because almost every business this
    engine looks at is seasonal and a Q4-to-Q1 comparison measures the calendar
    rather than the company.
    """
    ordered = _ordered(rows)
    if len(ordered) < periods_back + 1:
        return None
    return _pct_change(_value(ordered[-1], field), _value(ordered[-1 - periods_back], field))


def growth_acceleration(
    rows: list[dict], field: str, *, periods_back: int = QUARTERS_PER_YEAR
) -> float | None:
    """Change in the YoY growth RATE, in percentage points.

    Pillar B reads acceleration, not level: a company growing 20% that grew 10%
    last quarter is a different proposition from one growing 20% that grew 40%,
    and the level alone cannot tell them apart.

    Needs one more year-ago pair than `growth_yoy`, so two years of quarters.
    """
    ordered = _ordered(rows)
    if len(ordered) < periods_back + 2:
        return None
    current = _pct_change(_value(ordered[-1], field), _value(ordered[-1 - periods_back], field))
    prior = _pct_change(_value(ordered[-2], field), _value(ordered[-2 - periods_back], field))
    if current is None or prior is None:
        return None
    return current - prior


# ── margins ─────────────────────────────────────────────────────────────────


def margin_trend(rows: list[dict], field: str, *, window: int = 4) -> float | None:
    """Latest margin minus the mean of the `window` periods before it, in points.

    Compared against the PRIOR window, excluding the latest period, for the
    same reason relative volume excludes today: including the newest value in
    its own baseline dilutes the change being measured.
    """
    ordered = _ordered(rows)
    if len(ordered) < window + 1:
        return None
    latest = _value(ordered[-1], field)
    history = [_value(r, field) for r in ordered[-window - 1 : -1]]
    if latest is None or any(v is None for v in history):
        return None
    return latest - (sum(history) / len(history))


# ── estimate-revision momentum (D7) ─────────────────────────────────────────


@dataclass(frozen=True)
class RevisionMomentum:
    """How consensus moved for one fiscal period, and on how much evidence.

    CORRECT AT TWO SNAPSHOTS, TRUSTWORTHY AT SIXTY. A consensus that moved from
    2.00 to 2.39 between two snapshots moved exactly that much, as a matter of
    record — so the value is reported rather than suppressed. What two
    snapshots cannot tell you is whether that marks a trend, which is what
    `depth` is for. The scoring layer decides how much weight the depth has
    earned; this module only measures.

    Contrast IV rank, which IS gated to None below a minimum history: a rank
    needs a high and a low to sit between, so on a one-day history the formula
    has no answer at all. Different kind of thin — see features/evidence.py.
    """

    fiscal_period_end: date | None
    eps_pct: float | None
    revenue_pct: float | None
    eps_first: float | None
    eps_last: float | None
    analyst_count_eps: int | None
    """Breadth, as opposed to depth: two analysts revising is not thirty."""
    depth: EvidenceDepth

    @property
    def has_value(self) -> bool:
        return self.eps_pct is not None or self.revenue_pct is not None


def revision_momentum(
    snapshots: list[dict],
    *,
    as_of: date | None = None,
    window_days: int | None = 90,
) -> RevisionMomentum:
    """Consensus drift for the NEAREST UPCOMING fiscal period.

    One period, not an average across periods: the nearest unreported quarter
    is the one a swing horizon can actually trade, and averaging a revision to
    next quarter with one to FY+2 blends a tradeable signal into a forecast.

    Only periods that have not yet ended are considered. A revision to an
    already-reported quarter is not a forecast change, it is bookkeeping.

    `window_days` bounds how far back to look, so a stale snapshot from before
    a guidance reset does not masquerade as recent news. None means no bound,
    which is what a backtest wants.
    """
    if not snapshots:
        return RevisionMomentum(None, None, None, None, None, None, EMPTY)

    today = as_of or max(_as_date(s["snapshot_date"]) for s in snapshots)
    upcoming = [s for s in snapshots if _as_date(s["fiscal_period_end"]) > today]
    if not upcoming:
        return RevisionMomentum(None, None, None, None, None, None, EMPTY)

    target = min(_as_date(s["fiscal_period_end"]) for s in upcoming)
    series = [s for s in upcoming if _as_date(s["fiscal_period_end"]) == target]

    if window_days is not None:
        cutoff = today - _days(window_days)
        bounded = [s for s in series if _as_date(s["snapshot_date"]) >= cutoff]
        # If the window excludes everything, fall back to the full series rather
        # than report nothing: a quarter whose estimates stopped updating is
        # still better described by old data than by silence, and `depth` makes
        # the staleness visible.
        series = bounded or series

    series.sort(key=lambda s: _as_date(s["snapshot_date"]))
    depth = depth_from_dates([_as_date(s["snapshot_date"]) for s in series])

    if depth.observations < 2:
        # One snapshot is a level, not a change. Depth is still reported, so a
        # caller can see that the series has STARTED — which is different from
        # a symbol nobody has ever snapshotted.
        latest = series[-1] if series else None
        return RevisionMomentum(
            fiscal_period_end=target,
            eps_pct=None,
            revenue_pct=None,
            eps_first=_value(latest, "estimated_eps_avg"),
            eps_last=_value(latest, "estimated_eps_avg"),
            analyst_count_eps=_int(latest, "analyst_count_eps"),
            depth=depth,
        )

    first, last = series[0], series[-1]
    return RevisionMomentum(
        fiscal_period_end=target,
        eps_pct=_pct_change(_value(last, "estimated_eps_avg"), _value(first, "estimated_eps_avg")),
        revenue_pct=_pct_change(
            _value(last, "estimated_revenue_avg"), _value(first, "estimated_revenue_avg")
        ),
        eps_first=_value(first, "estimated_eps_avg"),
        eps_last=_value(last, "estimated_eps_avg"),
        analyst_count_eps=_int(last, "analyst_count_eps"),
        depth=depth,
    )


# ── the bundle ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FundamentalFeatures:
    """Everything Pillar B reads for one symbol."""

    symbol: str
    as_of: date | None
    fiscal_date_ending: date | None

    revenue_growth_yoy: float | None
    eps_growth_yoy: float | None
    revenue_acceleration: float | None
    eps_acceleration: float | None

    gross_margin: float | None
    operating_margin: float | None
    net_margin: float | None
    gross_margin_trend: float | None
    operating_margin_trend: float | None
    net_margin_trend: float | None

    roe: float | None
    roic: float | None
    debt_to_equity: float | None

    revision: RevisionMomentum
    quarters_available: int

    def as_dict(self) -> dict:
        out = asdict(self)
        out["revision"] = asdict(self.revision)
        out["revision"]["depth"] = asdict(self.revision.depth)
        return out


def compute(
    symbol: str,
    quarters: list[dict],
    snapshots: list[dict] | None = None,
    *,
    as_of: date | None = None,
) -> FundamentalFeatures:
    """Measure one symbol. Pure: no DB, no network."""
    ordered = _ordered(quarters)
    latest = ordered[-1] if ordered else None

    return FundamentalFeatures(
        symbol=symbol,
        as_of=as_of,
        fiscal_date_ending=(
            _as_date(latest["fiscal_date_ending"])
            if latest and latest.get("fiscal_date_ending")
            else None
        ),
        revenue_growth_yoy=growth_yoy(ordered, "revenue"),
        eps_growth_yoy=growth_yoy(ordered, "eps_diluted"),
        revenue_acceleration=growth_acceleration(ordered, "revenue"),
        eps_acceleration=growth_acceleration(ordered, "eps_diluted"),
        gross_margin=_value(latest, "gross_margin"),
        operating_margin=_value(latest, "operating_margin"),
        net_margin=_value(latest, "net_margin"),
        gross_margin_trend=margin_trend(ordered, "gross_margin"),
        operating_margin_trend=margin_trend(ordered, "operating_margin"),
        net_margin_trend=margin_trend(ordered, "net_margin"),
        roe=_value(latest, "roe"),
        roic=_value(latest, "roic"),
        debt_to_equity=_value(latest, "debt_to_equity"),
        revision=revision_momentum(snapshots or [], as_of=as_of),
        quarters_available=len(ordered),
    )


# ── loading (the only part that touches the database) ───────────────────────


QUARTERS_SQL = """
    SELECT symbol, fiscal_date_ending, fiscal_period, revenue, gross_profit,
           operating_income, net_income, eps_diluted, gross_margin,
           operating_margin, net_margin, roe, roic, debt_to_equity
    FROM   fundamentals
    WHERE  symbol = %s AND period_type = 'quarter' AND fiscal_date_ending <= %s
    ORDER  BY fiscal_date_ending
"""

SNAPSHOTS_SQL = """
    SELECT snapshot_date, fiscal_period_end, estimated_eps_avg,
           estimated_revenue_avg, analyst_count_eps, analyst_count_revenue
    FROM   estimate_snapshots
    WHERE  symbol = %s AND period_type = %s AND snapshot_date <= %s
    ORDER  BY fiscal_period_end, snapshot_date
"""


def load_fundamentals(symbol: str, *, as_of: date, limit: int = 12) -> list[dict]:
    """Quarterly rows up to `as_of`, oldest first.

    `as_of` filters on `fiscal_date_ending` rather than on when the row was
    ingested, which is the honest reading for a point-in-fact table: a backtest
    standing on 2026-03-01 should see the quarters that had ENDED, not the ones
    we happen to have rows for now.
    """
    rows = db.fetch_all(QUARTERS_SQL, (symbol, as_of))
    return rows[-limit:] if limit else rows


def load_estimate_snapshots(
    symbol: str, *, as_of: date, period_type: str = "quarter"
) -> list[dict]:
    """Every snapshot taken on or before `as_of`.

    No lower bound here: `revision_momentum` applies its own window, and a
    backtest wants the choice. The series begins 2026-10-21 regardless — there
    is nothing earlier to load, and no vendor sells it.
    """
    return db.fetch_all(SNAPSHOTS_SQL, (symbol, period_type, as_of))


# ── internals ───────────────────────────────────────────────────────────────


def _int(row: dict | None, field: str) -> int | None:
    if row is None or row.get(field) is None:
        return None
    return int(row[field])


def _as_date(value: object) -> date:
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _days(n: int) -> timedelta:
    return timedelta(days=n)
