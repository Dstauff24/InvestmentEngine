"""D24's raw-bar assumption, checked against a real database.

    PGDATABASE=ie_test pytest -m integration

This is the test that would have caught a provider restating its own history.
Schwab's daily candles are split-ADJUSTED, so `prices_daily` held pre-adjusted
bars while `features/adjustment.py` applied split factors on top of them — and
double-adjusting produces a plausible series rather than an error. The only
evidence available is arithmetic: a raw series STEPS by the split ratio at the
ex-date, an adjusted one does not.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from core import db, settings
from features import technical
from features.adjustment import RATIO_MISMATCH, split_basis_violations
from jobs import price_basis_check
from tests.dbguard import assert_safe_database

pytestmark = pytest.mark.integration

SYMBOL = "ZZBASIS"
EX_DATE = date(2026, 4, 6)
AS_OF = date(2026, 10, 21)


@pytest.fixture(scope="module", autouse=True)
def _guard_database():
    return assert_safe_database(settings.database_url())


@pytest.fixture(autouse=True)
def seeded():
    assert_safe_database(db.settings.database_url())
    _clean()
    db.execute("INSERT INTO tickers (symbol, is_active) VALUES (%s, TRUE)", (SYMBOL,))
    yield
    _clean()


def _clean() -> None:
    db.execute("DELETE FROM prices_daily WHERE symbol = %s", (SYMBOL,))
    db.execute("DELETE FROM corporate_actions WHERE symbol = %s", (SYMBOL,))
    db.execute("DELETE FROM price_basis_breaks WHERE symbol = %s", (SYMBOL,))
    db.execute("DELETE FROM tickers WHERE symbol = %s", (SYMBOL,))
    technical.reset_basis_break_cache()


def record_break(day: date = EX_DATE) -> None:
    db.execute(
        """
        INSERT INTO price_basis_breaks (symbol, break_date, logged_ratio, observed_step, reason)
        VALUES (%s, %s, 2.0, 5.2484, 'test')
        ON CONFLICT (symbol, break_date) DO NOTHING
        """,
        (SYMBOL, day),
    )
    technical.reset_basis_break_cache()


def add_split(ratio: float, ex_date: date = EX_DATE) -> None:
    db.execute(
        """
        INSERT INTO corporate_actions (symbol, ex_date, action_type, split_ratio, source)
        VALUES (%s, %s, 'split', %s, 'test')
        ON CONFLICT (symbol, ex_date, action_type) DO UPDATE SET split_ratio = EXCLUDED.split_ratio
        """,
        (SYMBOL, ex_date, ratio),
    )


def add_bar(day: date, close: float) -> None:
    db.execute(
        """
        INSERT INTO prices_daily (symbol, session_date, ts, open, high, low, close,
                                  volume, source)
        VALUES (%s, %s, %s::timestamptz, %s, %s, %s, %s, 1000000, 'test')
        ON CONFLICT (symbol, ts) DO UPDATE SET close = EXCLUDED.close
        """,
        (SYMBOL, day, f"{day} 20:00+00", close, close, close, close),
    )


def seed_series(*, pre: float, post: float) -> None:
    """Five sessions either side of the ex-date."""
    for i in range(5, 0, -1):
        add_bar(EX_DATE - timedelta(days=i), pre)
    for i in range(0, 5):
        add_bar(EX_DATE + timedelta(days=i), post)


def violations() -> list[dict]:
    return [v for v in split_basis_violations(as_of=AS_OF) if v["symbol"] == SYMBOL]


# ── the check ───────────────────────────────────────────────────────────────


def test_a_raw_series_steps_by_the_ratio_and_passes():
    add_split(25.0)
    seed_series(pre=5000.0, post=200.0)
    assert violations() == []


def test_a_pre_adjusted_series_is_caught():
    """BKNG's shape exactly: a 25-for-1 with no step in the stored bars."""
    add_split(25.0)
    seed_series(pre=200.0, post=200.0)

    found = violations()
    assert len(found) == 1
    assert found[0]["ratio"] == 25.0
    assert found[0]["observed_step"] == pytest.approx(1.0)


def test_the_days_own_price_move_does_not_count_as_a_violation():
    """An ex-date is also a trading day; a 4% move is not a basis error."""
    add_split(10.0)
    seed_series(pre=1040.0, post=100.0)
    assert violations() == []


def test_a_spinoff_sized_ratio_is_not_judged():
    """FMP reports a spinoff as a ~0.95 split (O10, still open). The price step
    is not the ratio there, so flagging it would report an unimplemented
    decision as corrupt data."""
    add_split(0.954)
    seed_series(pre=200.0, post=200.0)
    assert violations() == []


def test_a_split_outside_the_stored_window_is_not_judged():
    """Nothing can be concluded from bars that do not exist."""
    add_split(25.0, ex_date=date(2019, 1, 7))
    seed_series(pre=200.0, post=200.0)
    assert violations() == []


def test_a_split_on_the_first_stored_session_is_not_judged():
    """There is no bar before it to compare against."""
    add_split(25.0, ex_date=EX_DATE)
    for i in range(0, 5):
        add_bar(EX_DATE + timedelta(days=i), 200.0)
    assert violations() == []


# ── the job ─────────────────────────────────────────────────────────────────


def test_the_job_fails_loudly_rather_than_warning():
    """A silent warning would be the same mistake one level up."""
    add_split(25.0)
    seed_series(pre=200.0, post=200.0)

    with pytest.raises(price_basis_check.BasisViolation) as exc:
        price_basis_check.run(as_of=AS_OF)
    assert "double-counting" in str(exc.value)
    assert "--repair-split-basis" in str(exc.value), "the message says how to fix it"


def test_the_job_records_a_run_and_passes_on_a_raw_series():
    add_split(25.0)
    seed_series(pre=5000.0, post=200.0)

    assert price_basis_check.run(as_of=AS_OF) == 0
    row = db.fetch_one(
        "SELECT status, is_critical FROM job_runs WHERE job_name = %s "
        "ORDER BY started_at DESC LIMIT 1",
        (price_basis_check.JOB_NAME,),
    )
    assert row["status"] == "success"
    assert row["is_critical"] is False, "a check buys no history; it must not block the day"


# ── the two kinds, and a recorded break (O19) ───────────────────────────────


def test_a_wrong_sized_step_is_a_ratio_mismatch_not_pre_adjustment():
    """WY's shape: the bars DID step, by 5.25 against a logged 2:1.

    Calling this pre-adjustment would send an operator to
    `--repair-split-basis`, which re-fetches the same bad numbers: FMP's raw
    feed is its adjusted feed with the split log reversed out.
    """
    add_split(2.0)
    seed_series(pre=83.66, post=15.94)

    found = violations()
    assert len(found) == 1
    assert found[0]["kind"] == RATIO_MISMATCH
    assert found[0]["observed_step"] == pytest.approx(5.2484, abs=1e-4)


def test_a_recorded_break_is_a_known_state_rather_than_a_violation():
    add_split(2.0)
    seed_series(pre=83.66, post=15.94)
    assert violations(), "unrecorded, it fails"

    record_break()
    assert violations() == [], "recorded, it is a deliberate refusal"


def test_a_recorded_break_is_still_reported_when_asked_for():
    """The check prints what is quarantined, so nobody has to remember it."""
    add_split(2.0)
    seed_series(pre=83.66, post=15.94)
    record_break()

    everything = [
        v for v in split_basis_violations(as_of=AS_OF, include_recorded=True)
        if v["symbol"] == SYMBOL
    ]
    assert len(everything) == 1
    assert everything[0]["recorded"] is True


def test_the_job_passes_with_a_recorded_break_and_names_it_in_the_run():
    add_split(2.0)
    seed_series(pre=83.66, post=15.94)
    record_break()

    assert price_basis_check.run(as_of=AS_OF) == 0
    row = db.fetch_one(
        "SELECT status, details FROM job_runs WHERE job_name = %s ORDER BY started_at DESC LIMIT 1",
        (price_basis_check.JOB_NAME,),
    )
    assert row["status"] == "success"
    recorded = row["details"]["recorded_breaks"]
    assert len(recorded) == 1
    assert SYMBOL in recorded[0] and "ratio_mismatch" in recorded[0]


def test_a_ratio_mismatch_message_does_not_send_the_operator_to_the_repair():
    """The remedy depends on the kind; re-sourcing cannot fix this one."""
    add_split(2.0)
    seed_series(pre=83.66, post=15.94)

    with pytest.raises(price_basis_check.BasisViolation) as exc:
        price_basis_check.run(as_of=AS_OF)
    message = str(exc.value)
    assert "--repair-split-basis" not in message
    assert "price_basis_breaks" in message


# ── and the refusal reaches every reader ────────────────────────────────────


def test_load_bars_drops_the_refused_era():
    """`features.technical.load_bars` is the chokepoint the lab, the backtest
    engine and `backtest.regime` all read through, so one lookup covers them."""
    add_split(2.0)
    seed_series(pre=83.66, post=15.94)
    technical.reset_basis_break_cache()

    before = technical.load_bars(SYMBOL, as_of=AS_OF, lookback_days=100_000)
    assert len(before) == 10

    record_break()
    after = technical.load_bars(SYMBOL, as_of=AS_OF, lookback_days=100_000)
    assert len(after) == 5
    assert after["session_date"].iloc[0] == EX_DATE, "the ex-date bar is the good side"


def test_a_date_entirely_inside_the_refused_era_returns_nothing():
    """Not a short series on a bad basis: nothing. The standing `features/`
    convention then makes every downstream reading abstain."""
    add_split(2.0)
    seed_series(pre=83.66, post=15.94)
    record_break()

    bars = technical.load_bars(SYMBOL, as_of=EX_DATE - timedelta(days=1), lookback_days=100_000)
    assert bars.empty
