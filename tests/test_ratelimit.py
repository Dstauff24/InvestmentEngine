"""Rate limiter tests. A fake clock, so a simulated hour runs in microseconds.

CLAUDE.md pins the limits this must respect: Schwab 120/min (throttled to ~100),
Finnhub 60/min, FMP per-plan. Getting it wrong means either 429s in the middle
of a sacred snapshot job, or a universe pull that takes all evening.
"""

from __future__ import annotations

import pytest

from core.ratelimit import NullRateLimiter, RateLimiter


class FakeClock:
    """Monotonic time that only advances when something sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


def limiter(clock, max_calls=3, period_s=60.0):
    return RateLimiter(max_calls, period_s, clock=clock.time, sleeper=clock.sleep)


# ── construction ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [0, -1])
def test_rejects_a_nonsense_call_budget(bad):
    with pytest.raises(ValueError, match="max_calls"):
        RateLimiter(bad)


@pytest.mark.parametrize("bad", [0, -5.0])
def test_rejects_a_nonsense_period(bad):
    with pytest.raises(ValueError, match="period_s"):
        RateLimiter(10, bad)


# ── the window ──────────────────────────────────────────────────────────────


def test_calls_inside_the_budget_never_wait(clock):
    rl = limiter(clock, max_calls=3)
    assert [rl.acquire() for _ in range(3)] == [0.0, 0.0, 0.0]
    assert clock.sleeps == []


def test_the_call_over_budget_waits_for_the_window_to_roll(clock):
    rl = limiter(clock, max_calls=3, period_s=60.0)
    for _ in range(3):
        rl.acquire()

    clock.advance(10)  # 10s into the window
    waited = rl.acquire()  # the oldest call leaves at t=60

    assert waited == pytest.approx(50.0)
    assert clock.now == pytest.approx(60.0)


def test_it_waits_only_as_long_as_it_has_to(clock):
    rl = limiter(clock, max_calls=2, period_s=60.0)
    rl.acquire()  # t=0
    clock.advance(45)
    rl.acquire()  # t=45
    assert rl.acquire() == pytest.approx(15.0)  # blocked until t=0 expires


def test_a_fully_expired_window_costs_nothing(clock):
    rl = limiter(clock, max_calls=3, period_s=60.0)
    for _ in range(3):
        rl.acquire()
    clock.advance(61)
    assert rl.acquire() == 0.0
    assert clock.sleeps == []


def test_it_slides_rather_than_resetting_in_fixed_blocks(clock):
    """A fixed-window limiter allows 2N calls across a boundary. This must not.

    That burst shape is exactly what earns a 429, which is why this is a sliding
    window and not a token bucket.
    """
    rl = limiter(clock, max_calls=5, period_s=60.0)
    for _ in range(5):
        rl.acquire()  # five calls at t=0
    clock.advance(59.9)
    assert rl.acquire() > 0  # a fixed window would let this through free


def test_sustained_load_converges_on_the_advertised_rate(clock):
    """60/min: 120 calls must take just over two minutes, never less."""
    rl = limiter(clock, max_calls=60, period_s=60.0)
    for _ in range(120):
        rl.acquire()
    assert clock.now >= 60.0
    assert clock.now < 121.0
    assert rl.total_calls == 120


def test_a_realistic_universe_pull_respects_the_schwab_throttle(clock):
    """CLAUDE.md: throttle Schwab's 120/min to ~100. 550 symbols, one call each."""
    rl = RateLimiter(100, 60.0, clock=clock.time, sleeper=clock.sleep, name="schwab")
    for _ in range(550):
        rl.acquire()
    minutes = clock.now / 60.0
    assert 4.0 <= minutes <= 6.0  # D14's "<10 min" claim holds


def test_the_finnhub_free_tier_rate(clock):
    """D5: 60/min. 500 symbols is ~8 minutes, which the orchestrator budgets for."""
    rl = RateLimiter(60, 60.0, clock=clock.time, sleeper=clock.sleep)
    for _ in range(500):
        rl.acquire()
    assert 7.0 <= clock.now / 60.0 <= 9.0


# ── bookkeeping ─────────────────────────────────────────────────────────────


def test_available_reports_remaining_slots(clock):
    rl = limiter(clock, max_calls=3)
    assert rl.available() == 3
    rl.acquire()
    assert rl.available() == 2
    clock.advance(61)
    assert rl.available() == 3


def test_totals_are_tracked_for_the_job_run_record(clock):
    rl = limiter(clock, max_calls=2, period_s=60.0)
    for _ in range(5):
        rl.acquire()
    assert rl.total_calls == 5
    assert rl.total_wait_s > 0
    assert "5 calls" in repr(rl)


def test_slot_context_manager_acquires(clock):
    rl = limiter(clock, max_calls=1, period_s=60.0)
    with rl.slot():
        pass
    assert rl.total_calls == 1


# ── the null limiter ────────────────────────────────────────────────────────


def test_null_limiter_never_blocks():
    rl = NullRateLimiter()
    assert [rl.acquire() for _ in range(1000)] == [0.0] * 1000
    assert rl.total_calls == 1000
