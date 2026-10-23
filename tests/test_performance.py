"""O17's return on capital. Fixtures only — no DB, no network.

Every test here is a statement about the one failure mode the module exists to
prevent: a report that counts arriving money as a return. The headline
assertion is `test_a_deposit_is_not_a_return`, and the rest defend it against
the ways a cash change can arise that are NOT deposits.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from decision import performance as perf

DAYS = [date(2026, 11, d) for d in (2, 3, 4, 5, 6)]


def snap(day: date, total: str, cash: str) -> dict:
    return {
        "as_of_date": day,
        "account_hash": "ACC",
        "total_value": Decimal(total),
        "cash": Decimal(cash),
    }


def held(day: date, symbol: str = "VTI", quantity: str = "10") -> dict:
    return {"as_of_date": day, "symbol": symbol, "quantity": Decimal(quantity)}


def test_a_deposit_is_not_a_return():
    """THE WHOLE POINT. $1,000 becomes $1,100 because $100 arrived, nothing
    moved, and the return is zero — while raw balance growth says +10%."""
    snapshots = [snap(DAYS[0], "1000", "0"), snap(DAYS[1], "1100", "100")]
    holdings = [held(DAYS[0]), held(DAYS[1])]
    result = perf.measure(snapshots, holdings)
    assert result.twr == pytest.approx(Decimal("0"))
    assert result.balance_growth == pytest.approx(Decimal("0.10"))
    assert result.external_in == Decimal("100")


def test_a_withdrawal_is_not_a_loss():
    """The mirror, and the one a saver notices: taking money out is not a
    drawdown, and a report that said so would trip D17's circuit breaker on a
    transfer."""
    snapshots = [snap(DAYS[0], "1000", "200"), snap(DAYS[1], "800", "0")]
    holdings = [held(DAYS[0]), held(DAYS[1])]
    result = perf.measure(snapshots, holdings)
    assert result.twr == pytest.approx(Decimal("0"))
    assert result.external_out == Decimal("-200")
    assert result.balance_growth == pytest.approx(Decimal("-0.20"))


def test_a_price_move_is_the_return():
    snapshots = [snap(DAYS[0], "1000", "0"), snap(DAYS[1], "1050", "0")]
    holdings = [held(DAYS[0]), held(DAYS[1])]
    assert perf.measure(snapshots, holdings).twr == pytest.approx(Decimal("0.05"))


def test_sub_period_returns_are_CHAINED_not_added():
    """+10% then -10% is -1%, not 0%. Chaining is what makes the number
    comparable to a benchmark, and the reason the flows have to be timed at all
    rather than netted over the period."""
    snapshots = [
        snap(DAYS[0], "1000", "0"),
        snap(DAYS[1], "1100", "0"),
        snap(DAYS[2], "990", "0"),
    ]
    holdings = [held(d) for d in DAYS[:3]]
    assert perf.measure(snapshots, holdings).twr == pytest.approx(Decimal("-0.01"))


def test_a_deposit_between_two_price_moves_does_not_leak_into_either():
    """The case a period-level subtraction gets wrong: the deposit lands
    mid-window, so the sub-period after it is measured on the larger base."""
    snapshots = [
        snap(DAYS[0], "1000", "0"),
        snap(DAYS[1], "1100", "0"),  # +10%
        snap(DAYS[2], "2100", "1000"),  # deposit, no move
        snap(DAYS[3], "2310", "1000"),  # +10%
    ]
    holdings = [held(d) for d in DAYS[:4]]
    result = perf.measure(snapshots, holdings)
    assert result.twr == pytest.approx(Decimal("1.21") - 1)
    assert result.external_in == Decimal("1000")


def test_a_matched_distribution_counts_as_RETURN():
    """A dividend raises cash exactly as a deposit does, and it IS return. The
    engine already stores the distribution for D19/D24, so where the amount and
    the date line up against the quantity held, it is classified internal."""
    snapshots = [snap(DAYS[0], "1000", "0"), snap(DAYS[1], "1005", "5")]
    holdings = [held(DAYS[0]), held(DAYS[1])]
    dividends = [
        {
            "symbol": "VTI",
            "ex_date": DAYS[0],
            "payment_date": DAYS[1],
            "cash_amount": Decimal("0.50"),
        }
    ]
    result = perf.measure(snapshots, holdings, dividends)
    assert [f.kind for f in result.flows] == ["internal"]
    assert result.internal == Decimal("5")
    assert result.twr == pytest.approx(Decimal("0.005"))


def test_an_unmatched_credit_is_called_a_deposit_and_UNDERSTATES_the_return():
    """The honest gap, pinned so nobody is surprised by it later. A
    distribution the engine does not hold — and for a symbol outside the D14
    universe there will BE no row, since `corporate_actions.symbol` is a
    foreign key into `tickers` — reads as a deposit. That direction is
    deliberate: understating a return is survivable, overstating it is what
    makes somebody believe a strategy works."""
    snapshots = [snap(DAYS[0], "1000", "0"), snap(DAYS[1], "1005", "5")]
    holdings = [held(DAYS[0]), held(DAYS[1])]
    result = perf.measure(snapshots, holdings, dividends=[])
    assert [f.kind for f in result.flows] == ["external"]
    assert result.twr == pytest.approx(Decimal("0"))
    assert result.inferred == 1


def test_a_distribution_of_the_wrong_SIZE_is_not_matched():
    """Timing alone would classify any deposit near an ex-date as return. The
    match is on `quantity x cash_amount`, so a $500 deposit on the payment day
    of a $5 dividend is still a deposit."""
    snapshots = [snap(DAYS[0], "1000", "0"), snap(DAYS[1], "1500", "500")]
    dividends = [
        {
            "symbol": "VTI",
            "ex_date": DAYS[0],
            "payment_date": DAYS[1],
            "cash_amount": Decimal("0.50"),
        }
    ]
    result = perf.measure(snapshots, [held(DAYS[0]), held(DAYS[1])], dividends)
    assert [f.kind for f in result.flows] == ["external"]


def test_a_distribution_on_a_symbol_NOT_HELD_is_not_matched():
    """The quantity is read from the account, not from the universe: a
    dividend on a stock somebody else owns explains nothing here."""
    snapshots = [snap(DAYS[0], "1000", "0"), snap(DAYS[1], "1005", "5")]
    dividends = [
        {
            "symbol": "MSFT",
            "ex_date": DAYS[0],
            "payment_date": DAYS[1],
            "cash_amount": Decimal("0.50"),
        }
    ]
    result = perf.measure(snapshots, [held(DAYS[0]), held(DAYS[1])], dividends)
    assert [f.kind for f in result.flows] == ["external"]


def test_a_purchase_is_not_a_withdrawal():
    """Cash falling because shares were bought is not money leaving. This is
    the case that makes "every cash change is a flow" wrong, and the reason the
    module only claims a flow on a day no quantity moved."""
    snapshots = [snap(DAYS[0], "1000", "1000"), snap(DAYS[1], "1000", "0")]
    holdings = [held(DAYS[0], quantity="0"), held(DAYS[1], quantity="10")]
    result = perf.measure(snapshots, holdings)
    assert result.flows == ()
    assert result.unattributed_days == 1
    assert result.twr == pytest.approx(Decimal("0"))


def test_a_full_exit_is_not_a_deposit():
    """Schwab STOPS LISTING a position that has gone to zero, so a comparison
    of shared symbols only would see no quantity change and read the sale
    proceeds as money arriving — the largest single misreading available, since
    it is the whole position."""
    snapshots = [snap(DAYS[0], "1000", "0"), snap(DAYS[1], "1000", "1000")]
    holdings = [held(DAYS[0])]  # gone on day two
    result = perf.measure(snapshots, holdings)
    assert result.flows == ()
    assert result.unattributed_days == 1


def test_a_day_whose_POSITION_SYNC_FAILED_is_unattributed_not_a_liquidation():
    """`schwab_positions` is a non-critical step, so it can fail on an evening
    the balance snapshot succeeded. The missing rows look exactly like a full
    exit, and both resolve the same conservative way: say the day cannot be
    attributed rather than invent a trade or a transfer."""
    snapshots = [
        snap(DAYS[0], "1000", "0"),
        snap(DAYS[1], "1010", "0"),
        snap(DAYS[2], "1020", "0"),
    ]
    holdings = [held(DAYS[0]), held(DAYS[2])]  # day two never synced
    result = perf.measure(snapshots, holdings)
    assert result.unattributed_days == 2
    # The RETURN is still measured: the values are the broker's either way, and
    # only the FLOW attribution is lost.
    assert result.twr == pytest.approx(Decimal("1.02") - 1)


def test_the_chain_starts_at_the_first_FUNDED_snapshot():
    """The first deposit into an empty account is not a gain, and dividing by
    zero capital would report one — infinity, or a crash, depending on the
    type. The unfunded link is skipped and counted."""
    snapshots = [
        snap(DAYS[0], "0", "0"),
        snap(DAYS[1], "200", "200"),
        snap(DAYS[2], "210", "200"),
    ]
    holdings: list[dict] = []
    result = perf.measure(snapshots, holdings)
    assert result.unfunded_days == 1
    assert result.twr == pytest.approx(Decimal("0.05"))


def test_one_snapshot_is_not_a_period():
    """The engine's own first evening. A return needs two observations, and
    saying so beats printing 0.00%."""
    assert perf.measure([snap(DAYS[0], "200", "0")], []) is None
    assert "not measurable yet" in perf.render(None)


def test_the_report_prints_the_time_weighted_number_BESIDE_the_raw_growth():
    """They differ whenever money moved, and the difference is the thing a
    reader has to see — a report showing only one of them invites the other to
    be assumed."""
    snapshots = [snap(DAYS[0], "1000", "0"), snap(DAYS[1], "1100", "100")]
    out = perf.render(perf.measure(snapshots, [held(DAYS[0]), held(DAYS[1])]), account="ABC123HASH")
    assert "time-weighted return   +0.00%" in out
    assert "raw balance growth     +10.00%" in out
    assert "NOT a return" in out
    assert "INFERRED" in out
    assert "ABC123" in out


def test_every_inferred_flow_SAYS_it_is_inferred():
    """The label travels with the number, per the module's contract: until
    Schwab's transactions endpoint is wired, every flow here is a deduction
    from a balance series and none of them is a record of a transfer."""
    snapshots = [snap(DAYS[0], "1000", "0"), snap(DAYS[1], "1100", "100")]
    result = perf.measure(snapshots, [held(DAYS[0]), held(DAYS[1])])
    assert all(f.basis == "inferred" for f in result.flows)
