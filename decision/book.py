"""Open positions, scoped to one ladder state (D40/D44).

D44: the sector guard (D38), the heat cap (D17) and max-concurrent-swings count
open positions WITHIN THE SAME LADDER STATE ONLY. Each of SHADOW, PAPER and
LIVE keeps its own book.

Without that scoping a shadow idea would block a live one, which is exactly
backwards: the shadow book exists to find out whether a strategy works and must
be free to take every idea the strategy generates, while the live book is
constrained by actual money. It also means a promotion (D40) does not inherit
the demoted state's open positions.

WHERE EACH BOOK LIVES, which is not symmetrical:

  * **shadow** and **paper** — open `shadow_book` rows, filtered on
    `status_at_call`. A shadow "position" is a tracked call, not a holding.
  * **live** — the `positions` table, which is what Schwab actually reports.

The asymmetry is the point rather than an inconsistency: a shadow position has
no custodian and a live one is not ours to invent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core import db

log = logging.getLogger(__name__)

LADDER_STATES = ("shadow", "paper", "live")

__all__ = ["OpenBook", "load_book"]


@dataclass
class OpenBook:
    """What is already open in one ladder state."""

    status: str
    symbols: set[str] = field(default_factory=set)
    by_sector: dict[str, int] = field(default_factory=dict)
    open_risk_pct: float = 0.0
    """Total open heat as a % of the account (D17's cap is 10%)."""
    swing_count: int = 0

    def sector_count(self, sector: str | None) -> int:
        return self.by_sector.get(sector or "", 0)

    def holds(self, symbol: str) -> bool:
        return symbol in self.symbols

    def __str__(self) -> str:
        return (
            f"{self.status}: {self.swing_count} swing(s), "
            f"{self.open_risk_pct:.1f}% open heat, "
            f"{len(self.by_sector)} sector(s)"
        )


SHADOW_SQL = """
    SELECT s.symbol, t.sector, s.strategy, s.proj_entry_high, s.proj_stop
    FROM   shadow_book s
    LEFT   JOIN tickers t USING (symbol)
    WHERE  s.outcome = 'open' AND s.status_at_call = %s
"""

LIVE_SQL = """
    SELECT p.symbol, t.sector, p.bucket AS strategy, p.open_risk_amount
    FROM   positions p
    LEFT   JOIN tickers t USING (symbol)
    WHERE  p.as_of_date = (SELECT max(as_of_date) FROM positions)
      AND  p.quantity <> 0
"""


def load_book(status: str, *, account_value: float) -> OpenBook:
    """The open book for one ladder state.

    `account_value` is needed because open heat is a PERCENTAGE (D17's cap is
    10% of the account) while the stored figures are absolute risk amounts.
    Passing it in rather than reading settings keeps this testable and makes
    the D44 distinction explicit: a shadow book sizes against the virtual
    account even once a live balance exists.
    """
    if status not in LADDER_STATES:
        raise ValueError(f"unknown ladder state {status!r}; expected one of {LADDER_STATES}")

    book = OpenBook(status=status)
    rows = db.fetch_all(LIVE_SQL) if status == "live" else db.fetch_all(SHADOW_SQL, (status,))

    for row in rows:
        book.symbols.add(row["symbol"])
        sector = row.get("sector") or ""
        if row.get("strategy") == "swing":
            book.swing_count += 1
            book.by_sector[sector] = book.by_sector.get(sector, 0) + 1

        risk = _open_risk(row)
        if risk is not None and account_value > 0:
            book.open_risk_pct += risk / account_value * 100.0

    return book


def _open_risk(row: dict) -> float | None:
    """Absolute risk still open on one position.

    For a live position Schwab-derived `open_risk_amount` is authoritative. For
    a shadow call it is derived from the projection, because a tracked call has
    no custodian to ask — and D18 zeroes the figure once the stop reaches
    breakeven, which `outcome_tracker` (step 8) will maintain.
    """
    if row.get("open_risk_amount") is not None:
        return float(row["open_risk_amount"])
    entry, stop = row.get("proj_entry_high"), row.get("proj_stop")
    if entry is None or stop is None:
        return None
    return max(0.0, float(entry) - float(stop))
