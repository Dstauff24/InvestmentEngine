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
    # True when the refusal came from the PROVIDER's own verdict rather than
    # from our configured cap. Different cause, different remedy: raising
    # `fmp.daily_call_budget` fixes the latter and does nothing for the former.
    provider_exhausted: bool = False

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

    @classmethod
    def for_edgar(cls) -> DayBudget:
        """The SEC EDGAR budget from config.

        The SEC publishes no daily cap, so unlike FMP this is not about staying
        inside an allowance. It is a runaway-loop guard: a bug that re-fetched
        in a loop would hammer a public service that has asked politely for
        restraint, and being the reason they introduce a cap is a worse outcome
        than a job that stops early.
        """
        cfg = config.universe().edgar
        return cls("edgar", cfg.daily_call_budget)

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
                "SELECT calls_spent, provider_exhausted_at FROM api_budget "
                "WHERE provider = %s AND session_date = %s FOR UPDATE",
                (self.provider, day),
            )
            row = cur.fetchone()
            prior = row["calls_spent"]
            # The provider has already told us it will serve nothing more today.
            # Refuse locally: granting here would buy another round-trip whose
            # only possible outcome is the same refusal, and FMP reports it as a
            # 200, so it does not even look like one.
            provider_done = row["provider_exhausted_at"] is not None
            granted = 0 if provider_done else max(0, min(n, cap - prior))
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
            provider_exhausted=provider_done,
        )

    def mark_provider_exhausted(self, note: str, *, day: date | None = None) -> None:
        """Record that the PROVIDER says its allowance for today is spent.

        Called from the client the first time a provider answers with its
        allowance-spent signal. From then on `spend()` refuses locally, so no
        later job that day opens a socket to rediscover the same fact — which
        is what cost 225 wasted calls on 2026-10-20.

        Set once and never overwritten: the first observation is the useful one.
        `scripts/fmp_probe.py` talks to `requests` directly and does not draw on
        this budget, so a marked day does not block the diagnostic you would
        reach for to find out whether the plan changed.
        """
        day = day or clock.session_date()
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_budget (provider, session_date) VALUES (%s, %s) "
                "ON CONFLICT (provider, session_date) DO NOTHING",
                (self.provider, day),
            )
            cur.execute(
                "UPDATE api_budget SET provider_exhausted_at = now(), "
                "provider_exhausted_note = %s, updated_at = now() "
                "WHERE provider = %s AND session_date = %s "
                "AND provider_exhausted_at IS NULL",
                (note[:500], self.provider, day),
            )

    def provider_exhausted(self, day: date | None = None) -> str | None:
        """The provider's own allowance-spent note for today, if any."""
        row = db.fetch_one(
            "SELECT provider_exhausted_note FROM api_budget "
            "WHERE provider = %s AND session_date = %s "
            "AND provider_exhausted_at IS NOT NULL",
            (self.provider, day or clock.session_date()),
        )
        return row["provider_exhausted_note"] if row else None

    def spend_or_raise(self, n: int = 1, *, day: date | None = None) -> Grant:
        """Spend, raising `BudgetExhausted` if nothing at all was granted."""
        grant = self.spend(n, day=day)
        if grant.exhausted and grant.provider_exhausted:
            raise BudgetExhausted(
                f"{self.provider} reports its own daily allowance spent for "
                f"{grant.session_date}; refusing further calls without opening a "
                f"socket. This engine spent {grant.spent_after} of its configured "
                f"{self.budget} — the gap is what the provider served to something "
                f"else (a probe, another deployment). Raising "
                f"fmp.daily_call_budget will NOT help; the allowance resets on the "
                f"provider's own schedule. Declined spend, not a failure (D29)."
            )
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
        done = getattr(self, "provider_note", None) is not None
        self.total_requested += n
        return Grant(
            provider=self.provider,
            session_date=day or date.min,
            requested=n,
            granted=0 if done else n,
            spent_after=self.total_requested,
            budget=0,
            provider_exhausted=done,
        )

    # `day` is unused here but must keep its name: these override DayBudget and
    # callers pass it by keyword.
    def spent(self, day: date | None = None) -> int:  # noqa: ARG002
        return self.total_requested

    def declined(self, day: date | None = None) -> int:  # noqa: ARG002
        return 0

    def remaining(self, day: date | None = None) -> int:  # noqa: ARG002
        return 1_000_000

    def mark_provider_exhausted(
        self,
        note: str,
        *,
        day: date | None = None,  # noqa: ARG002
    ) -> None:
        """Remembered in memory only — a NullBudget has no database row.

        Kept truthful rather than a bare `pass`, so a test can assert the client
        reported the provider's verdict without standing up Postgres.
        """
        self.provider_note = note

    def provider_exhausted(self, day: date | None = None) -> str | None:  # noqa: ARG002
        return getattr(self, "provider_note", None)
