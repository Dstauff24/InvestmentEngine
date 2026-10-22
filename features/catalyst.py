"""Pillar C inputs: event proximity and insider clusters (D32 weight 20).

Nothing here scores anything. Three readings:

  * **days to earnings** and **ex-dividend proximity**, from `events`;
  * **insider-buying clusters**, from `insider_transactions`.

STANDING CONVENTION: insufficient data returns `None`, never a number and never
`False`. "No earnings date on record" and "earnings are far away" are different
claims, and D19's rule — no new swing long within two days of an ex-date — must
not be satisfied by ignorance.

POINT-IN-TIME HONESTY on the insider side. A Form 4 is knowledge when the SEC
ACCEPTS it, which can be two business days after the trade, so every window
here is measured on `filed_at` and never on `transaction_date`. Keying on the
transaction would let a backtest see a purchase before anyone could act on it.

The cluster RULE is config (D25), not code: N distinct insiders, a D-day
window, a minimum aggregate value. This module applies whatever config says and
reports the evidence depth beside the verdict, because the insider series is
one the engine accumulates itself — it began on the day this job first ran, and
a cluster found in a 3-day-old series is not the same claim as one found in a
year of it.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

from core import clock, config, db
from features.evidence import EMPTY, EvidenceDepth, depth_from_dates

log = logging.getLogger(__name__)

__all__ = [
    "CatalystFeatures",
    "InsiderCluster",
    "compute",
    "days_to_earnings",
    "ex_dividend_proximity",
    "insider_cluster",
    "load_events",
    "load_insider_purchases",
]


# ── event proximity ─────────────────────────────────────────────────────────


def days_to_earnings(events: list[dict], *, as_of: date) -> int | None:
    """Sessions-agnostic calendar days to the next earnings date, or None.

    Calendar days rather than trading days: the recipe thresholds (recipe 1's
    `no_earnings_within_days`, recipe 3's `max_days_since_earnings`) were
    written as plain day counts, and converting here would silently move them.

    None means NO EARNINGS DATE IS KNOWN — which is not the same as "earnings
    are far away", and a recipe gating on proximity must treat it as a reason
    to abstain rather than a green light.
    """
    upcoming = [
        _as_date(e["event_date"])
        for e in events
        if e.get("event_type") == "earnings" and _as_date(e["event_date"]) >= as_of
    ]
    return (min(upcoming) - as_of).days if upcoming else None


def days_since_earnings(events: list[dict], *, as_of: date) -> int | None:
    """Calendar days since the most recent past earnings date. Recipe 3's gate."""
    past = [
        _as_date(e["event_date"])
        for e in events
        if e.get("event_type") == "earnings" and _as_date(e["event_date"]) < as_of
    ]
    return (as_of - max(past)).days if past else None


def ex_dividend_proximity(events: list[dict], *, as_of: date) -> int | None:
    """Calendar days to the next ex-dividend date, or None if none is known.

    D19(3) forbids a new swing long within two days of an ex-date. That rule
    cannot be satisfied by an absent date, so None has to propagate rather than
    read as "clear".
    """
    upcoming = [
        _as_date(e["event_date"])
        for e in events
        if e.get("event_type") == "ex_dividend" and _as_date(e["event_date"]) >= as_of
    ]
    return (min(upcoming) - as_of).days if upcoming else None


# ── insider clusters (D39) ──────────────────────────────────────────────────


@dataclass(frozen=True)
class InsiderCluster:
    """Whether N distinct insiders bought on the open market within D days.

    `is_cluster` is None, not False, when the series is too young to answer.
    The insider series started when the job first ran; before that there is no
    evidence either way, and a recipe reading False would treat "we have not
    looked" as "nobody bought".
    """

    is_cluster: bool | None
    distinct_insiders: int
    purchases: int
    aggregate_usd: float
    largest_usd: float | None
    window_days: int
    depth: EvidenceDepth
    """Depth of OUR OWN series, not of the window — see features/evidence.py."""

    @property
    def shortfall(self) -> str | None:
        """Why this is not a cluster, for the idea card. None when it is one."""
        if self.is_cluster is None:
            return "insider series too young to judge"
        if self.is_cluster:
            return None
        return f"{self.distinct_insiders} distinct insider(s), ${self.aggregate_usd:,.0f}"


def insider_cluster(
    purchases: list[dict],
    *,
    as_of: date,
    cfg: config.InsiderCluster | None = None,
    series_depth: EvidenceDepth | None = None,
) -> InsiderCluster:
    """Apply the configured cluster rule to open-market purchases.

    `purchases` must already be filtered to code 'P' non-derivative rows by the
    loader — the SQL does it so the index can be used — but this re-checks,
    because a caller assembling rows by hand is exactly how an option exercise
    ends up counted as conviction buying.

    DISTINCT INSIDERS, not transactions. One director filing four purchases in
    a week is one person's opinion reported four times; counting transactions
    would make a single buyer look like a consensus, which is the one thing the
    rule exists to detect.
    """
    rule = cfg or config.universe().insider_cluster
    window_start = as_of - timedelta(days=rule.window_days)

    # The window is measured on FILING time. Keying it on transaction_date
    # would admit a purchase the market did not yet know about.
    inside = [
        p
        for p in purchases
        if p.get("transaction_code") == rule.purchase_code
        and not p.get("is_derivative", False)
        and window_start <= clock.session_date(_as_utc(p["filed_at"])) <= as_of
    ]

    insiders = {p.get("insider_cik") or p.get("insider_name") for p in inside}
    insiders.discard(None)
    total = sum(float(p["value_usd"] or 0) for p in inside)
    largest = max((float(p["value_usd"] or 0) for p in inside), default=None)

    depth = series_depth if series_depth is not None else EMPTY
    # Too young to answer: no observations at all means the job has never run
    # for this symbol, so False would be a claim we have not earned.
    verdict: bool | None
    if depth.is_empty:
        verdict = None
    else:
        verdict = len(insiders) >= rule.min_distinct_insiders and total >= rule.min_aggregate_usd

    return InsiderCluster(
        is_cluster=verdict,
        distinct_insiders=len(insiders),
        purchases=len(inside),
        aggregate_usd=total,
        largest_usd=largest,
        window_days=rule.window_days,
        depth=depth,
    )


# ── the bundle ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EarningsSurprise:
    """The last REPORTED quarter's beat, as a fact rather than as a proxy (O12).

    `post_earnings_drift` gates on "did they beat", and until the radar moved
    to per-symbol earnings (D47) the stored rows carried no actuals, so the
    recipe used year-over-year EPS GROWTH instead. Those are different claims:
    a company can grow 20% year on year and still miss the quarter, and the
    drift premise is about the surprise, not the trend.

    **`None` is "we cannot verify", never "no beat"** — the standing `features/`
    convention. 8,858 of 9,396 stored earnings rows carry both numbers; the
    remainder abstain rather than being read as misses.

    **Point-in-time:** the surprise is treated as knowable from the event date.
    A report released after the close is strictly knowable only on the next
    session, but the recipe requires a GAP, which is itself the next session's
    first print, so no plan can act on it earlier than the market did.
    """

    reported_on: date | None
    eps_actual: float | None
    eps_estimate: float | None
    revenue_actual: float | None
    revenue_estimate: float | None

    @property
    def eps_beat(self) -> bool | None:
        if self.eps_actual is None or self.eps_estimate is None:
            return None
        return self.eps_actual > self.eps_estimate

    @property
    def revenue_beat(self) -> bool | None:
        if self.revenue_actual is None or self.revenue_estimate is None:
            return None
        return self.revenue_actual > self.revenue_estimate

    @property
    def eps_surprise_pct(self) -> float | None:
        """Beat size against the estimate's MAGNITUDE.

        Divided by `abs(estimate)` so a company expected to lose 0.10 and
        reporting 0.05 scores a POSITIVE surprise. Dividing by the signed
        estimate would flip the sign on every loss-making name, which is a
        large share of exactly the universe where surprises are biggest.
        """
        if self.eps_actual is None or not self.eps_estimate:
            return None
        return (self.eps_actual - self.eps_estimate) / abs(self.eps_estimate) * 100.0

    @property
    def verdict(self) -> str:
        if self.eps_beat is None:
            return "unverified (no actual or estimate on record)"
        size = self.eps_surprise_pct
        direction = "beat" if self.eps_beat else "missed"
        return f"{direction} by {size:+.1f}%" if size is not None else direction


def last_earnings_surprise(events: list[dict], *, as_of: date) -> EarningsSurprise:
    """The most recent REPORTED quarter on or before `as_of`."""
    reported = [
        e
        for e in events
        if e.get("event_type") == "earnings" and _as_date(e.get("event_date")) <= as_of
    ]
    if not reported:
        return EarningsSurprise(None, None, None, None, None)
    latest = max(reported, key=lambda e: _as_date(e["event_date"]))
    return EarningsSurprise(
        reported_on=_as_date(latest["event_date"]),
        eps_actual=_float(latest.get("eps_actual")),
        eps_estimate=_float(latest.get("eps_estimate")),
        revenue_actual=_float(latest.get("revenue_actual")),
        revenue_estimate=_float(latest.get("revenue_estimate")),
    )


@dataclass(frozen=True)
class CatalystFeatures:
    symbol: str
    as_of: date
    days_to_earnings: int | None
    days_since_earnings: int | None
    days_to_ex_dividend: int | None
    next_dividend_amount: float | None
    insiders: InsiderCluster
    surprise: EarningsSurprise
    # Short interest is NOT available from any provider this engine implements
    # (neither the Schwab Trader API nor the FMP /stable/ endpoints the client
    # calls expose it). Carried as an explicit None rather than omitted, so the
    # gap is visible in every logged feature row instead of being silently
    # absent — and so wiring a source later does not change the shape.
    short_interest_pct: float | None = None

    def as_dict(self) -> dict:
        out = asdict(self)
        out["insiders"] = asdict(self.insiders)
        out["insiders"]["depth"] = asdict(self.insiders.depth)
        return out


def compute(
    symbol: str,
    events: list[dict],
    purchases: list[dict] | None = None,
    *,
    as_of: date,
    series_depth: EvidenceDepth | None = None,
) -> CatalystFeatures:
    """Measure one symbol. Pure: no DB, no network."""
    next_div = _next_dividend_amount(events, as_of=as_of)
    return CatalystFeatures(
        symbol=symbol,
        as_of=as_of,
        days_to_earnings=days_to_earnings(events, as_of=as_of),
        days_since_earnings=days_since_earnings(events, as_of=as_of),
        days_to_ex_dividend=ex_dividend_proximity(events, as_of=as_of),
        next_dividend_amount=next_div,
        insiders=insider_cluster(purchases or [], as_of=as_of, series_depth=series_depth),
        surprise=last_earnings_surprise(events, as_of=as_of),
    )


# ── loading (the only part that touches the database) ───────────────────────


EVENTS_SQL = """
    SELECT symbol, event_type, event_date, is_confirmed, dividend_amount,
           eps_estimate, eps_actual, revenue_estimate, revenue_actual
    FROM   events
    WHERE  symbol = %s AND event_date BETWEEN %s AND %s
    ORDER  BY event_date
"""

# Filtered in SQL so the partial index on (symbol, filed_session) can be used,
# and keyed on FILED time so a backtest cannot see a filing before acceptance.
PURCHASES_SQL = """
    SELECT accession_number, txn_index, insider_cik, insider_name, filed_at,
           filed_session, transaction_date, transaction_code, shares,
           price_per_share, value_usd, is_derivative, is_10b5_1
    FROM   insider_transactions
    WHERE  symbol = %s
      AND  transaction_code = 'P'
      AND  NOT is_derivative
      AND  filed_at <= %s
      AND  filed_session >= %s
    ORDER  BY filed_at
"""

# Depth is the span of OUR series for this symbol, independent of the cluster
# window: a cluster found in a three-day-old series is not the claim a cluster
# found in a year of it is.
DEPTH_SQL = """
    SELECT DISTINCT filed_session
    FROM   insider_transactions
    WHERE  symbol = %s AND filed_at <= %s
"""


def load_events(
    symbol: str, *, as_of: date, forward_days: int = 120, backward_days: int = 120
) -> list[dict]:
    return db.fetch_all(
        EVENTS_SQL,
        (symbol, as_of - timedelta(days=backward_days), as_of + timedelta(days=forward_days)),
    )


def load_insider_purchases(
    symbol: str, *, as_of: date, window_days: int | None = None
) -> list[dict]:
    """Open-market purchases knowable on `as_of`.

    The upper bound is on `filed_at`, the moment of acceptance — not on
    `transaction_date`, and not on `filed_session`, because a filing accepted
    at 16:05 ET is not knowable to a run that happened at 16:00.
    """
    rule_window = window_days or config.universe().insider_cluster.window_days
    return db.fetch_all(
        PURCHASES_SQL,
        (symbol, clock.session_close_utc(as_of), as_of - timedelta(days=rule_window)),
    )


def insider_series_depth(symbol: str, *, as_of: date) -> EvidenceDepth:
    """How much insider history the engine itself holds for this symbol."""
    rows = db.fetch_all(DEPTH_SQL, (symbol, clock.session_close_utc(as_of)))
    return depth_from_dates([_as_date(r["filed_session"]) for r in rows])


# ── internals ───────────────────────────────────────────────────────────────


def _next_dividend_amount(events: list[dict], *, as_of: date) -> float | None:
    upcoming = sorted(
        (
            e
            for e in events
            if e.get("event_type") == "ex_dividend" and _as_date(e["event_date"]) >= as_of
        ),
        key=lambda e: _as_date(e["event_date"]),
    )
    for event in upcoming:
        if event.get("dividend_amount") is not None:
            return float(event["dividend_amount"])
    return None


def _float(value: object) -> float | None:
    """A stored NUMERIC as a float, or None. Never 0.0 for a missing value."""
    return None if value is None else float(value)


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _as_utc(value: object) -> datetime:
    """A stored `filed_at` as a tz-aware datetime."""
    if isinstance(value, datetime):
        return clock.to_utc(value)
    return clock.to_utc(datetime.fromisoformat(str(value)))
