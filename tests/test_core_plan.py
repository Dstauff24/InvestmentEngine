"""O17's core layer: what to buy, when to diversify, and when to worry.

Fixtures only. Every statement this module makes is about a decision already
taken (the target is 100% SWPPX), so these tests are mostly about the three
ways it must REFUSE rather than guess.
"""

from __future__ import annotations

import pytest

from core import config
from decision import core_plan as cp


def _accounts(**kw):
    """The shipped risk config with the accounts block adjusted."""
    base = config.risk().model_dump()
    base["accounts"] = {**base["accounts"], **kw}
    return config.RiskConfig(**base)


# ── contribution guidance ───────────────────────────────────────────────────


def test_a_mutual_fund_is_bought_in_DOLLARS():
    """SWPPX settles at NAV in dollar amounts. A share count would be wrong by
    whatever the NAV moves between the digest and the order, and for a fund
    there is no share price to quote in the first place."""
    line = cp.contribution_line(
        500.0, cfg=_accounts(core_symbol="SWPPX", core_symbol_kind="mutual_fund")
    )
    assert line == "plan: buy $500.00 of SWPPX"
    assert "share" not in line


def test_an_ETF_target_says_the_amount_is_sized_at_the_price():
    """An ETF trades in shares, so the dollar figure is an instruction to size
    rather than the order itself — and the line says which."""
    line = cp.contribution_line(500.0, cfg=_accounts(core_symbol="VTI", core_symbol_kind="etf"))
    assert "$500.00 of VTI" in line
    assert "shares" in line


def test_NO_CASH_is_silent_rather_than_a_zero():
    """A line that appears nightly saying "buy $0" is a line people stop
    reading, and the absence of new cash is not news."""
    assert cp.contribution_line(0.0, cfg=_accounts(core_symbol="SWPPX")) is None
    assert cp.contribution_line(-5.0, cfg=_accounts(core_symbol="SWPPX")) is None


def test_cash_with_NO_TARGET_SET_says_so_instead_of_guessing():
    """The one case where the line appears with no plan: money is sitting there
    and the engine has not been told what the core is. Guessing would be advice
    about somebody else's portfolio (D52)."""
    line = cp.contribution_line(500.0, cfg=_accounts(core_symbol=None))
    assert "not set" in line
    assert "a decision, not a default" in line


# ── the diversification proposal ────────────────────────────────────────────


def test_below_the_threshold_the_engine_proposes_NOTHING():
    """At $200 the benefit of a second fund is rounding and the cost in
    simplicity is real."""
    cfg = _accounts(core_diversification_threshold_usd=10_000.0)
    assert cp.diversification_note(200.0, cfg=cfg) is None
    assert cp.diversification_note(9_999.0, cfg=cfg) is None


def test_above_the_threshold_it_PROPOSES_and_says_it_is_a_proposal():
    """The difference between a proposal and an instruction is the whole of
    D52's caution about this layer, so it is in the line itself."""
    note = cp.diversification_note(
        12_000.0,
        cfg=_accounts(
            core_diversification_threshold_usd=10_000.0, core_international_target_pct=20.0
        ),
    )
    assert "FOR REVIEW" in note
    assert "20%" in note
    assert "never an automatic change" in note


# ── the tracking check ──────────────────────────────────────────────────────


def test_a_gap_INSIDE_the_expense_ratio_is_reported_and_not_alerted():
    """An index fund should trail its index by about its fee. That is the
    expected state, and reporting it without alarm is what makes the alert
    mean something when it fires."""
    note = cp.tracking_note(10.0, 10.1, cfg=_accounts())
    assert "tracking:" in note
    assert "ALERT" not in note


def test_a_WIDER_gap_alerts_in_EITHER_direction():
    """Beating the index by more than the fee is as much a sign of a wrong
    benchmark as trailing it by more, so the test is on |gap|."""
    behind = cp.tracking_note(8.0, 10.1, cfg=_accounts())
    ahead = cp.tracking_note(12.0, 10.1, cfg=_accounts())
    assert "TRACKING ALERT" in behind
    assert "TRACKING ALERT" in ahead


def test_tracking_is_SILENT_when_either_return_is_unknown():
    """Before there are two snapshots there is no return, and a gap computed
    against a missing number would be a claim about nothing."""
    assert cp.tracking_note(None, 10.0) is None
    assert cp.tracking_note(10.0, None) is None


def test_the_shipped_config_is_the_operators_stated_core():
    """O17's core decisions, pinned: 100% SWPPX, a mutual fund, dividends
    reinvested, with the diversification threshold and tracking tolerance the
    operator set."""
    accounts = config.risk().accounts
    assert accounts.core_symbol == "SWPPX"
    assert accounts.core_symbol_kind == "mutual_fund"
    assert accounts.buys_in_dollars is True
    assert accounts.core_dividends_reinvested is True
    assert accounts.core_diversification_threshold_usd == pytest.approx(10_000.0)
    assert accounts.core_international_target_pct == pytest.approx(20.0)
    assert accounts.single_account_tax_type == "taxable"
