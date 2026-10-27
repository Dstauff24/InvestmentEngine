"""The lab's measurables must BE the engine's measurables.

Stage 1 recomputes rolling what the engine computes per session, because the
per-session path costs an hour over the window stage 1 needs. That is only
sound if the two agree — otherwise the lab measures a signal no recipe reads,
and a result about it says nothing about the engine. These tests compare the
vectorised providers against `features.technical`'s own functions, bar for bar,
on fixture data.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from features import technical
from lab import providers

RNG = np.random.default_rng(20261026)


def bars(n: int = 400) -> pd.DataFrame:
    """A plausible price series: a random walk with volume and a real range."""
    close = 100 * np.cumprod(1 + RNG.normal(0.0004, 0.014, n))
    high = close * (1 + np.abs(RNG.normal(0, 0.006, n)))
    low = close * (1 - np.abs(RNG.normal(0, 0.006, n)))
    return pd.DataFrame(
        {
            "session_date": pd.bdate_range("2024-01-01", periods=n).date,
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": RNG.integers(1_000_000, 5_000_000, n).astype(float),
        }
    )


def test_the_rolling_RSI_equals_the_engines_at_every_session():
    """Wilder's, seeded with the simple mean of the first 14 changes. A plain
    `ewm` seed disagrees by a point or two for the first hundred bars, and the
    recipe's reset band is four points wide."""
    frame = bars()
    rolling = providers.rsi_14(frame["close"])

    for cut in (50, 120, 250, 399):
        engine = technical.rsi_wilder(frame["close"].iloc[: cut + 1])
        assert engine is not None
        assert rolling.iloc[cut] == pytest.approx(engine, abs=1e-9), f"disagreed at bar {cut}"


def test_the_rolling_RELATIVE_VOLUME_equals_the_engines():
    """Today over the average of the PRIOR 20 sessions — today excluded, or a
    true 3x day reads as 2.7x."""
    frame = bars()
    rolling = providers.relative_volume_20d(frame["volume"])

    for cut in (40, 150, 399):
        engine = technical.relative_volume(frame["volume"].iloc[: cut + 1])
        assert engine is not None
        assert rolling.iloc[cut] == pytest.approx(engine, rel=1e-9)


def test_the_rolling_SQUEEZE_tracks_the_engines_within_its_documented_gap():
    """The one approximation in the providers, and it is stated rather than
    hidden: the engine ranks today against the PRIOR year, this ranks it in a
    window including itself, so they differ by at most 1/(history+1)."""
    frame = bars(600)
    rolling = providers.squeeze_percentile(frame)

    for cut in (300, 450, 599):
        engine = technical.squeeze_percentile(frame.iloc[: cut + 1])
        assert engine is not None
        assert rolling.iloc[cut] == pytest.approx(engine, abs=0.01)


def test_forward_returns_look_FORWARD_and_run_out_at_the_end():
    """`shift(-h)`: the value at t describes what happened after t. The last h
    sessions have no answer, and NaN is the honest output — filling them would
    invent a return for days the window does not cover."""
    closes = pd.Series([100.0, 110.0, 121.0, 133.1], index=list(range(4)))
    fwd = providers.forward_returns(closes, (1,))[1]

    assert fwd.iloc[0] == pytest.approx(10.0)
    assert fwd.iloc[1] == pytest.approx(10.0)
    assert pd.isna(fwd.iloc[3]), "the last session has no forward return"


def test_relative_strength_is_a_DIFFERENCE_not_a_ratio():
    """D33: a ratio is unstable exactly in the quiet-sector case that matters
    most, so the engine subtracts and so does this."""
    closes = pd.Series(np.linspace(100, 120, 40))
    sector = pd.Series(np.linspace(100, 110, 40))
    rs = providers.relative_strength_20d(closes, sector)

    own = (closes.iloc[39] / closes.iloc[19] - 1) * 100
    index = (sector.iloc[39] / sector.iloc[19] - 1) * 100
    assert rs.iloc[39] == pytest.approx(own - index)


def test_every_registered_measurable_either_HAS_a_provider_or_is_named_as_absent():
    """The registry holds more hypotheses than stored bars can answer — the
    composite ones need a forward series that starts 2026-10-21 (D39(4)). A
    measurable with no provider must be reported UNMEASURED, never silently
    dropped, or the test count that sets the significance bar is wrong."""
    from lab.registry import REGISTRY

    absent = {h.measurable for h in REGISTRY.values()} - set(providers.MEASURABLES)
    assert absent, "this test is meaningless if everything happens to be providable"
    assert "composite" in absent
    assert "composite_within_recipe_signals" in absent


# ── the long-horizon factors (registered Oct 26 2026) ───────────────────────


def test_momentum_12_1_SKIPS_the_most_recent_month():
    """The skipped month is the construction, not a detail: short-term reversal
    runs the other way over the last few weeks and cancels much of the
    premium. A 12-0 measure finding nothing would be a result about two signals
    fighting rather than about momentum."""
    closes = pd.Series(np.linspace(100, 200, 300))
    mom = providers.momentum_12_1(closes, long_window=252, skip=21)

    expected = (closes.iloc[299 - 21] / closes.iloc[299 - 252] - 1) * 100
    assert mom.iloc[299] == pytest.approx(expected)
    # The last 21 sessions are deliberately absent from the measurement.
    assert mom.iloc[299] != pytest.approx((closes.iloc[299] / closes.iloc[299 - 252] - 1) * 100)


def test_momentum_needs_a_FULL_YEAR_before_it_says_anything():
    """A 12-month measure from six months of bars is the plausible-looking
    wrong answer the standing convention bans."""
    closes = pd.Series(np.linspace(100, 150, 200))
    assert providers.momentum_12_1(closes).isna().all()


def test_volatility_is_annualised_the_SAME_WAY_the_regime_code_does_it():
    """`backtest/regime.py` annualises with sqrt(252) to build its terciles, so
    a reading here and a regime tag there have to mean the same thing."""
    rng = np.random.default_rng(5)
    closes = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, 300)))
    vol = providers.volatility_60d(closes)

    returns = np.log(closes / closes.shift(1))
    expected = returns.iloc[-60:].std() * (252**0.5) * 100
    assert vol.iloc[-1] == pytest.approx(expected)
    assert 5 < vol.iloc[-1] < 40, "a plausible annualised percentage, not a decimal"


# ── the beta basis: the correction that can change the ordering ─────────────


def test_beta_is_one_for_a_symbol_that_IS_the_market():
    frame = bars(400)
    spy = frame.set_index("session_date")["close"]
    beta = providers.rolling_beta(spy, spy)
    assert beta.iloc[-1] == pytest.approx(1.0, abs=1e-9)


def test_beta_is_two_for_a_symbol_that_moves_TWICE_the_market():
    """Constructed rather than fitted: a symbol whose daily return is exactly
    2x the market's must come back at 2.0, or the estimator is wrong in a way
    no real series would reveal."""
    frame = bars(400)
    spy = frame.set_index("session_date")["close"].astype(float)
    market_returns = spy.pct_change().fillna(0.0)
    levered = 100 * (1 + 2 * market_returns).cumprod()

    beta = providers.rolling_beta(levered, spy)
    assert beta.iloc[-1] == pytest.approx(2.0, abs=0.02)


def test_beta_is_POINT_IN_TIME_and_cannot_see_its_own_session():
    """A rolling window ending today includes today's return, and today is the
    session the cross-section is formed on. The shift costs one session of beta
    and buys the claim that nothing here saw the future."""
    frame = bars(400)
    spy = frame.set_index("session_date")["close"].astype(float)
    own = frame.set_index("session_date")["close"].astype(float)

    full = providers.rolling_beta(own, spy)
    # Mutating the LAST close must not change the beta indexed at that date,
    # because that beta was estimated from data strictly before it.
    tampered = own.copy()
    tampered.iloc[-1] = tampered.iloc[-1] * 1.25
    assert providers.rolling_beta(tampered, spy).iloc[-1] == pytest.approx(full.iloc[-1])


def test_a_short_history_gives_NO_beta_rather_than_one():
    """A default of 1.0 is a guess that happens to look neutral, and it would
    quietly turn the beta basis into the SPY shift it exists to improve on."""
    frame = bars(100)
    spy = frame.set_index("session_date")["close"].astype(float)
    assert providers.rolling_beta(spy, spy).isna().all()
