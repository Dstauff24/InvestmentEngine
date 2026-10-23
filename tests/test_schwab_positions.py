"""O17's read-only positions sync. Fixtures only — no DB, no network.

The parser's job is to write what the broker says and derive nothing it
cannot see. Two columns it must leave alone are the point of most of these
tests: `bucket` (D17's intent, the operator's) and `tax_type` (tax treatment,
which Schwab does not report).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from ingest import schwab_positions as sp

DAY = date(2026, 10, 23)
MOMENT = datetime(2026, 10, 23, 20, 0, tzinfo=UTC)


def account_payload(**kw) -> dict:
    account = {
        "hashValue": "ABC123HASH",
        "type": "CASH",
        "currentBalances": {
            "liquidationValue": 184_320.55,
            "cashBalance": 12_004.10,
            "longMarketValue": 172_316.45,
            "buyingPower": 24_008.20,
            "cashAvailableForTrading": 11_500.00,
        },
        "positions": [],
    }
    account.update(kw)
    return {"securitiesAccount": account}


def equity(symbol="AAPL", **kw) -> dict:
    row = {
        "instrument": {"symbol": symbol, "assetType": "EQUITY"},
        "longQuantity": 100.0,
        "shortQuantity": 0.0,
        "averagePrice": 180.0,
        "marketValue": 20_000.0,
        "longOpenProfitLoss": 2_000.0,
        "currentDayProfitLoss": 150.0,
    }
    row.update(kw)
    return row


# ── balances ────────────────────────────────────────────────────────────────


def test_an_account_snapshot_records_what_the_broker_reports():
    row = sp.parse_account(account_payload(), as_of_date=DAY)
    assert row["account_hash"] == "ABC123HASH"
    assert row["account_type"] == "CASH"
    assert row["total_value"] == pytest.approx(184_320.55)
    assert row["cash"] == pytest.approx(12_004.10)
    assert row["cash_available"] == pytest.approx(11_500.00)


def test_the_sync_never_sets_a_tax_type():
    """Schwab reports CASH or MARGIN — a SETTLEMENT mode. A taxable brokerage
    and a Roth IRA both come back as CASH, so the tax type is operator-supplied
    and NULL until stated: the adviser refuses to recommend a sale in an
    account whose tax treatment it does not know rather than assuming the
    cheaper answer (D52 — a guess in this layer is advice)."""
    row = sp.parse_account(account_payload(type="MARGIN"), as_of_date=DAY)
    assert "tax_type" not in row
    # An ASSIGNMENT, not a mention: the SQL explains in a comment why the
    # column is absent, and a test that tripped on the explanation would push
    # somebody to delete the explanation.
    import re

    updates = sp.ACCOUNT_UPSERT.split("DO UPDATE SET")[1]
    assert not re.search(r"^\s*tax_type\s*=", updates, re.MULTILINE), (
        "an evening sync must not overwrite a tax type somebody stated once"
    )


def test_an_envelope_without_an_identifier_is_skipped_not_guessed():
    assert sp.parse_account({"securitiesAccount": {"type": "CASH"}}, as_of_date=DAY) is None


def test_a_bare_payload_without_the_envelope_still_parses():
    """`schwab-py` wraps accounts in `securitiesAccount`; unwrapping happens in
    ONE place so a shape change shows up once."""
    bare = account_payload()["securitiesAccount"]
    assert sp.parse_account(bare, as_of_date=DAY)["account_hash"] == "ABC123HASH"


# ── positions ───────────────────────────────────────────────────────────────


def test_a_long_equity_position_is_parsed_with_its_cost_basis():
    [row] = sp.parse_positions(account_payload(positions=[equity()]), as_of_date=DAY, as_of=MOMENT)
    assert row["symbol"] == "AAPL"
    assert row["asset_type"] == "EQUITY"
    assert row["quantity"] == pytest.approx(100.0)
    assert row["cost_basis"] == pytest.approx(18_000.0), "average price x quantity"
    assert row["unrealized_pl_pct"] == pytest.approx(2_000.0 / 18_000.0 * 100.0)


def test_the_bucket_is_left_for_the_operator():
    """D17's bucket is a statement of INTENT — the same ETF is a core holding
    in one account and a tactical trade in another, and the broker has no idea
    which. Guessing it would put a position into the heat cap, or out of it, on
    the engine's assumption rather than a decision."""
    [row] = sp.parse_positions(account_payload(positions=[equity()]), as_of_date=DAY, as_of=MOMENT)
    assert "bucket" not in row


def test_a_short_quantity_is_signed_rather_than_dropped():
    """The short leg of a defined-risk spread (D17) is part of the position; a
    book that counted only longs would read a credit spread as naked."""
    [row] = sp.parse_positions(
        account_payload(positions=[equity(symbol="SPY", longQuantity=0.0, shortQuantity=2.0)]),
        as_of_date=DAY,
        as_of=MOMENT,
    )
    assert row["quantity"] == pytest.approx(-2.0)


def test_a_closed_position_the_broker_still_lists_is_not_a_holding():
    rows = sp.parse_positions(
        account_payload(positions=[equity(longQuantity=0.0, shortQuantity=0.0)]),
        as_of_date=DAY,
        as_of=MOMENT,
    )
    assert rows == []


def test_an_option_leg_keeps_its_underlying_and_strike():
    [row] = sp.parse_positions(
        account_payload(
            positions=[
                equity(
                    symbol="AAPL  261218C00200000",
                    instrument={
                        "symbol": "AAPL  261218C00200000",
                        "assetType": "OPTION",
                        "underlyingSymbol": "AAPL",
                        "strikePrice": 200.0,
                        "putCall": "CALL",
                    },
                )
            ]
        ),
        as_of_date=DAY,
        as_of=MOMENT,
    )
    assert row["asset_type"] == "OPTION"
    assert row["underlying_symbol"] == "AAPL"
    assert row["strike"] == pytest.approx(200.0)
    assert row["put_call"] == "CALL"


def test_a_collective_investment_is_an_etf():
    """Schwab calls ETFs COLLECTIVE_INVESTMENT, which the positions table's
    CHECK constraint does not allow."""
    [row] = sp.parse_positions(
        account_payload(
            positions=[equity(instrument={"symbol": "VTI", "assetType": "COLLECTIVE_INVESTMENT"})]
        ),
        as_of_date=DAY,
        as_of=MOMENT,
    )
    assert row["asset_type"] == "ETF"


def test_an_unknown_asset_type_becomes_OTHER_rather_than_failing():
    [row] = sp.parse_positions(
        account_payload(positions=[equity(instrument={"symbol": "???", "assetType": "WARRANT"})]),
        as_of_date=DAY,
        as_of=MOMENT,
    )
    assert row["asset_type"] == "OTHER"


# ── read-only, as a mandate ─────────────────────────────────────────────────


def test_nothing_in_this_repo_can_place_an_order():
    """READ-ONLY IS A MANDATE, NOT A PHASE (D52). The check is over the whole
    source tree rather than this module, because the guarantee is only worth
    what the weakest file makes it."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    forbidden = (
        "place_order",
        "cancel_order",
        "replace_order",
        "preview_order",
    )
    offenders = []
    for folder in (
        "alerts",
        "backtest",
        "core",
        "decision",
        "features",
        "ingest",
        "jobs",
        "scoring",
        "scripts",
        "shadow_book",
    ):
        for path in (root / folder).rglob("*.py"):
            text = path.read_text()
            for name in forbidden:
                if name in text:
                    offenders.append(f"{path.relative_to(root)}: {name}")
    assert offenders == [], f"order-placing calls must not exist anywhere: {offenders}"


def test_only_the_hashed_account_identifier_is_ever_fetched():
    """The plain account number lives on an endpoint nothing calls, so the
    engine cannot store one it never fetched."""
    from pathlib import Path

    client = Path(sp.__file__).resolve().parent / "schwab_client.py"
    text = client.read_text()
    assert "get_account_numbers" not in text
    assert "get_accounts" in text
