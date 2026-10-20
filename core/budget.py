"""Day-level provider call budget, shared across processes (D29).

The free-tier FMP limit is a DAY limit shared by every call site. Enforcing it
per job — as an earlier version did, with universe enrichment capping its own
pass and fundamentals capping its own — lets two jobs in one evening jointly
exceed it while each believes it is within bounds.

So the counter lives in `api_budget`, keyed on (provider, ET session date):

  * **DB-backed**, because the jobs are separate `python -m` invocations from
    cron and an in-process counter would reset between them.
  * **ET-keyed**, so it rolls over on the trading-day boundary rather than at
    UTC midnight — the same reason every job takes its date from `core.clock`.
  * **Drawn at one chokepoint.** `FMPClient.get()` spends from it, so every call
    site is covered automatically: the probe, universe enrichment, fundamentals,
    estimates, events. Nothing has to remember to opt in.

A grant is reserved BEFORE the request goes out, not counted after, because a
call that fails still consumed quota at the provider.

Reservations: a caller passes `reserve=` to hold calls back for something else.
Every job except the events job reserves `fmp.reserve_for_events`, so the three
market-wide calendar calls cannot be crowded out by a fundamentals crawl that
ran earlier in the evening — the event radar is what stops the engine opening a
position blind to earnings.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from core import clock, config, db


@dataclass(frozen=True)
class Grant:
    """The outcome of asking to spend `requested` calls."""

    provider: str
    session_date: date
    requested: int
    granted: int
    spent_after: int
    budget: int

    @property
    def declined(self) -> int:
        return self.requested - self.granted

    @property
    def exhausted(self) -> bool:
        return self.granted == 0

    @property
    def partial(self) -> bool:
        return 0 < self.granted < self.requested

    def __str__(self) -> str:
        return (
            f"{self.provider} {self.session_date}: granted {self.granted}/{self.requested}, "
            f"{self.spent_after}/{self.budget} spent today"
        )


def _check_amount(n: int) -> None:
    if n < 0:
        raise ValueError(f"cannot spend a negative number of calls: {n}")


class BudgetExhausted(RuntimeError):
    """No calls left in today's budget.

    Not a failure of the job: the orchestrator and the calling job report it as
    DECLINED SPEND and carry on with what they managed. The work resumes
    tomorrow, or sooner on a higher plan.
    """


class DayBudget:
    """DB-backed, cross-process spend counter for one provider."""

    def __init__(self, provider: str, budget: int, *, reserve: int = 0) -> None:
        if budget < 0:
            raise ValueError(f"budget must be non-negative, got {budget}")
        if reserve < 0:
            raise ValueError(f"reserve must be non-negative, got {reserve}")
        self.provider = provider
        self.budget = budget
        self.reserve = reserve

    @classmethod
    def for_fmp(cls, *, honour_events_reserve: bool = True) -> DayBudget:
        """The FMP budget from config.

        `honour_events_reserve=False` is for the events job itself, which is the
        thing the reservation exists to protect.
        """
        cfg = config.universe().fmp
        return cls(
            "fmp",
            cfg.daily_call_budget,
            reserve=cfg.reserve_for_events if honour_events_reserve else 0,
        )

    # ── the one mutating operation ──────────────────────────────────────────

    def spend(self, n: int = 1, *, day: date | None = None) -> Grant:
        """Reserve up to `n` calls. Returns what was actually granted.

        Atomic across processes: the row is locked with SELECT ... FOR UPDATE
        inside one transaction, so two jobs starting at the same moment cannot
        both read the same remaining figure and both spend it.
        """
        _check_amount(n)
        day = day or clock.session_date()
        cap = max(0, self.budget - self.reserve)

        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_budget (provider, session_date) VALUES (%s, %s) "
                "ON CONFLICT (provider, session_date) DO NOTHING",
                (self.provider, day),
            )
            cur.execute(
                "SELECT calls_spent FROM api_budget "
                "WHERE provider = %s AND session_date = %s FOR UPDATE",
                (self.provider, day),
            )
            prior = cur.fetchone()["calls_spent"]
            granted = max(0, min(n, cap - prior))
            declined = n - granted
            if granted or declined:
                cur.execute(
                    "UPDATE api_budget SET calls_spent = calls_spent + %s, "
                    "calls_declined = calls_declined + %s, updated_at = now() "
                    "WHERE provider = %s AND session_date = %s",
                    (granted, declined, self.provider, day),
                )

        return Grant(
            provider=self.provider,
            session_date=day,
            requested=n,
            granted=granted,
            spent_after=prior + granted,
            budget=self.budget,
        )

    def spend_or_raise(self, n: int = 1, *, day: date | None = None) -> Grant:
        """Spend, raising `BudgetExhausted` if nothing at all was granted."""
        grant = self.spend(n, day=day)
        if grant.exhausted:
            raise BudgetExhausted(
                f"{self.provider} daily call budget is exhausted "
                f"({grant.spent_after}/{self.budget} spent for {grant.session_date}"
                f"{f', {self.reserve} reserved' if self.reserve else ''}). "
                f"This is declined spend, not a failure — the remaining work resumes "
                f"on the next run. Raise fmp.daily_call_budget after a plan upgrade (D29)."
            )
        return grant

    # ── read-only ───────────────────────────────────────────────────────────

    def spent(self, day: date | None = None) -> int:
        row = db.fetch_one(
            "SELECT calls_spent FROM api_budget WHERE provider = %s AND session_date = %s",
            (self.provider, day or clock.session_date()),
        )
        return int(row["calls_spent"]) if row else 0

    def declined(self, day: date | None = None) -> int:
        row = db.fetch_one(
            "SELECT calls_declined FROM api_budget WHERE provider = %s AND session_date = %s",
            (self.provider, day or clock.session_date()),
        )
        return int(row["calls_declined"]) if row else 0

    def remaining(self, day: date | None = None) -> int:
        return max(0, self.budget - self.reserve - self.spent(day))

    def __repr__(self) -> str:
        return f"DayBudget({self.provider}, budget={self.budget}, reserve={self.reserve})"


class NullBudget(DayBudget):
    """Unlimited, in-memory. For tests and for providers with no day limit.

    Counts what was asked for so a test can assert the call sites DO draw from a
    budget, without needing a database.
    """

    def __init__(self, provider: str = "null") -> None:
        super().__init__(provider, budget=0, reserve=0)
        self.total_requested = 0

    def spend(self, n: int = 1, *, day: date | None = None) -> Grant:
        _check_amount(n)
        self.total_requested += n
        return Grant(
            provider=self.provider,
            session_date=day or date.min,
            requested=n,
            granted=n,
            spent_after=self.total_requested,
            budget=0,
        )

    # `day` is unused here but must keep its name: these override DayBudget and
    # callers pass it by keyword.
    def spent(self, day: date | None = None) -> int:  # noqa: ARG002
        return self.total_requested

    def declined(self, day: date | None = None) -> int:  # noqa: ARG002
        return 0

    def remaining(self, day: date | None = None) -> int:  # noqa: ARG002
        return 1_000_000
