"""Synthetic price series for Pillar A tests. Values chosen to be checkable by eye.

Real bars make poor fixtures for arithmetic: nobody can verify a 200-day mean
of market data by inspection, so a test written against it asserts whatever the
code happened to produce. These series have known answers.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

START = date(2025, 1, 2)


def sessions(n: int, *, start: date = START) -> list[date]:
    """`n` consecutive weekdays. Not a real calendar — no holidays — which is
    fine because every function here indexes BARS, never calendar days."""
    out: list[date] = []
    day = start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def frame(closes: list[float], *, volumes: list[float] | None = None) -> pd.DataFrame:
    """Bars from a close series. High/low straddle the close by a fixed 1%.

    A constant band keeps ATR hand-checkable: with no gaps, true range is
    exactly high-low, so ATR is 2% of close on a flat series.
    """
    days = sessions(len(closes))
    vols = volumes if volumes is not None else [1_000_000.0] * len(closes)
    return pd.DataFrame(
        {
            "session_date": days,
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": vols,
        }
    )


def flat(n: int, price: float = 100.0) -> list[float]:
    return [price] * n


def rising(n: int, *, start: float = 100.0, step: float = 1.0) -> list[float]:
    """Strictly rising, so every MA is stacked and rising by construction."""
    return [start + step * i for i in range(n)]


def falling(n: int, *, start: float = 400.0, step: float = 1.0) -> list[float]:
    return [start - step * i for i in range(n)]


def v_shape(down: int, up: int, *, start: float = 200.0, step: float = 1.0) -> list[float]:
    """Down then up — a pullback that resumes, which is recipe 1's shape.

    The down leg INCLUDES the bottom, so the series minimum is exactly
    `start - step * down`. The first version stopped one step short, which made
    the stated low wrong by one step and the midpoint assertion in
    `test_the_midpoint_of_a_v_sits_mid_range` fail against a correct
    implementation — a fixture that lies is worse than no fixture.
    """
    bottom = start - step * down
    descent = [start - step * i for i in range(down + 1)]
    ascent = [bottom + step * i for i in range(1, up + 1)]
    return descent + ascent
