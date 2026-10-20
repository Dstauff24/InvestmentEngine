"""Shared rate limiter. One implementation, one place to get provider limits right.

CLAUDE.md: "Respect API limits: Schwab 120 req/min (throttle to ~100), FMP
per-plan limits, Finnhub 60/min. Use a shared rate limiter."

**Sliding window, not a token bucket.** The providers publish their limits as
"N requests per minute", and a sliding window enforces exactly that. A token
bucket with a refill rate permits an initial burst of N followed by more
requests inside the same minute, which is precisely the shape that earns a 429
in the middle of a sacred snapshot job.

The clock and sleep are injectable so tests can run a simulated hour in
microseconds without a real `sleep`.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager


class RateLimiter:
    """Allow at most `max_calls` acquisitions in any `period_s` window.

    Thread-safe. Jobs are single-threaded today, but the lock costs nothing and
    removes a whole class of future surprise.
    """

    def __init__(
        self,
        max_calls: int,
        period_s: float = 60.0,
        *,
        name: str = "",
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_calls < 1:
            raise ValueError(f"max_calls must be at least 1, got {max_calls}")
        if period_s <= 0:
            raise ValueError(f"period_s must be positive, got {period_s}")
        self.max_calls = max_calls
        self.period_s = period_s
        self.name = name or f"{max_calls}/{period_s:g}s"
        self._clock = clock
        self._sleeper = sleeper
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()
        self.total_calls = 0
        self.total_wait_s = 0.0

    def acquire(self) -> float:
        """Block until a slot is free. Returns the seconds spent waiting."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                self._evict(now)
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    self.total_calls += 1
                    self.total_wait_s += waited
                    return waited
                # The oldest call leaves the window at oldest + period.
                delay = self._calls[0] + self.period_s - now
            # Sleep outside the lock so other threads can make progress.
            delay = max(delay, 0.0)
            if delay == 0.0:
                # The window is full but nothing has expired at this clock
                # resolution; yield rather than spin.
                delay = min(0.001, self.period_s)
            self._sleeper(delay)
            waited += delay

    def _evict(self, now: float) -> None:
        cutoff = now - self.period_s
        while self._calls and self._calls[0] <= cutoff:
            self._calls.popleft()

    def available(self) -> int:
        """Slots free right now. Diagnostics only — do not gate work on this."""
        with self._lock:
            self._evict(self._clock())
            return self.max_calls - len(self._calls)

    @contextmanager
    def slot(self):
        """`with limiter.slot(): ...` — acquire, then run."""
        yield self.acquire()

    def __repr__(self) -> str:
        return (
            f"RateLimiter({self.name}: {self.total_calls} calls, {self.total_wait_s:.1f}s waited)"
        )


class NullRateLimiter(RateLimiter):
    """Never waits. For tests and offline replays of stored fixtures."""

    def __init__(self) -> None:
        super().__init__(max_calls=1, period_s=1.0, name="null")

    def acquire(self) -> float:
        self.total_calls += 1
        return 0.0
