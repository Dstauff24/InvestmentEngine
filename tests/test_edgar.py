"""SEC EDGAR client and Form 4 parsing. Fixtures only — no network.

Three things are load-bearing and tested as such:

  * **point-in-time honesty** — `filed_at` comes from `acceptanceDateTime`, and
    nothing keys on `transaction_date`;
  * **the transaction-code filter** — only `P` is an open-market purchase;
  * **SEC etiquette** — a real User-Agent, a one-SECOND rate window, the D27
    consent gate, and a day budget.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from core import netguard, settings
from core.budget import NullBudget
from core.ratelimit import NullRateLimiter
from ingest import edgar_insiders as ins
from ingest.edgar_client import (
    EdgarBlockedError,
    EdgarClient,
    EdgarError,
)
from tests.fixtures import edgar_payloads as fx

UA = "InvestmentEngine/0.1 (real.person@realdomain.com)"
# EDGAR stamps acceptanceDateTime with an explicit `Z`, so it is UTC and is
# taken at face value. `clock.to_utc` only assumes Eastern for a NAIVE
# timestamp — second-guessing an explicit offset would be worse than trusting
# it, and the direction of any such error matters: reading a 16:35 ET filing as
# 16:35 UTC would place it four hours early, which is lookahead.
ACCEPTED = "2026-10-21T16:35:00.000Z"
FILED_AT = datetime(2026, 10, 21, 16, 35, tzinfo=UTC)


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or (payload if isinstance(payload, str) else str(payload))
        self.ok = 200 <= status_code < 400

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses) or [FakeResponse({})]
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers or {}))
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


def client(*responses, **kwargs) -> tuple[EdgarClient, FakeSession]:
    session = FakeSession(*responses)
    return (
        EdgarClient(
            user_agent=UA,
            limiter=NullRateLimiter(),
            budget=NullBudget(),
            session=session,
            **kwargs,
        ),
        session,
    )


# ══════════════════════════════════════════════════════════════════════════
# SEC etiquette — a condition of access, not a courtesy
# ══════════════════════════════════════════════════════════════════════════


def test_the_user_agent_is_sent_on_every_request(stubbed_transport):
    api, session = client(FakeResponse(fx.COMPANY_TICKERS))
    api.company_tickers()
    _, headers = session.calls[0]
    assert headers["User-Agent"] == UA
    assert "@" in headers["User-Agent"], "the SEC asks for a contact address"


def test_the_example_user_agent_is_refused_rather_than_sent(monkeypatch):
    """`you@example.com` satisfies the format while giving them no way to reach
    us — which is the anonymous traffic the rule exists to stop."""
    monkeypatch.setattr(settings, "require", lambda name: "InvestmentEngine/0.1 (you@example.com)")
    with pytest.raises(settings.SettingsError, match="still the example value"):
        settings.sec_edgar_user_agent()


def test_a_user_agent_without_a_contact_address_is_refused(monkeypatch):
    monkeypatch.setattr(settings, "require", lambda name: "InvestmentEngine/0.1")
    with pytest.raises(settings.SettingsError, match="no contact address"):
        settings.sec_edgar_user_agent()


def test_the_rate_limit_window_is_one_second_not_one_minute():
    """A 300/minute sliding window would allow all 300 inside one second, which
    breaks the only limit the SEC actually states."""
    from ingest.edgar_client import RATE_WINDOW_S

    assert RATE_WINDOW_S == 1.0
    assert settings.sec_edgar_rate_limit() <= 10, "must stay under the SEC's ~10/s"
    assert settings.sec_edgar_rate_limit() <= 5, "and well under it, not at the line"


def test_consent_is_required_before_any_request():
    """D27 applies to EDGAR like every other provider, and inside pytest the
    gate refuses regardless of the flag."""
    api, session = client(FakeResponse(fx.COMPANY_TICKERS))
    with pytest.raises(netguard.LiveCallBlocked):
        api.company_tickers()
    assert session.calls == [], "the gate must fire BEFORE the socket"


def test_a_403_is_not_retried(stubbed_transport):
    """Hammering a refusal is how a soft block becomes a long one."""
    api, session = client(FakeResponse("forbidden", status_code=403))
    with pytest.raises(EdgarBlockedError, match="User-Agent"):
        api.company_tickers()
    assert len(session.calls) == 1


def test_a_429_is_retried(stubbed_transport):
    api, session = client(
        FakeResponse("slow down", status_code=429),
        FakeResponse(fx.COMPANY_TICKERS),
    )
    from tenacity import wait_none

    EdgarClient.get.retry.wait = wait_none()
    try:
        assert api.company_tickers()["AAPL"] == "0000320193"
    finally:
        pass
    assert len(session.calls) == 2


def test_the_budget_is_drawn_before_the_socket(stubbed_transport):
    api, _ = client(FakeResponse(fx.COMPANY_TICKERS))
    api.company_tickers()
    assert api.budget.total_requested == 1


# ══════════════════════════════════════════════════════════════════════════
# Ticker -> CIK
# ══════════════════════════════════════════════════════════════════════════


def test_ciks_are_zero_padded_to_ten_digits(stubbed_transport):
    """EDGAR's submissions URL needs the padded form; the JSON gives an int."""
    api, _ = client(FakeResponse(fx.COMPANY_TICKERS))
    mapping = api.company_tickers()
    assert mapping["AAPL"] == "0000320193"
    assert mapping["MSFT"] == "0000789019"


def test_share_class_tickers_use_the_dash_form(stubbed_transport):
    """EDGAR uses dashes, like FMP /stable/ — so the translation is identity.
    Named anyway: the Schwab translator was wrong for months because the
    conversion was assumed obvious and had nowhere to live."""
    api, _ = client(FakeResponse(fx.COMPANY_TICKERS))
    assert api.company_tickers()["BRK-B"] == "0001067983"
    from ingest.edgar_client import to_edgar_ticker

    assert to_edgar_ticker("BRK-B") == "BRK-B"


def test_an_empty_ticker_map_is_an_error_not_an_empty_universe(stubbed_transport):
    api, _ = client(FakeResponse({}))
    with pytest.raises(EdgarError, match="no usable rows"):
        api.company_tickers()


# ══════════════════════════════════════════════════════════════════════════
# POINT-IN-TIME HONESTY
# ══════════════════════════════════════════════════════════════════════════


def test_filed_at_comes_from_acceptance_not_from_the_filing_date():
    """`filingDate` is a date and would collapse a 16:05 acceptance and an
    09:00 one onto the same instant — and a filing accepted after the close is
    not tradeable until the next session."""
    payload = fx.submissions(
        [{"accession": "0000320193-26-000100", "accepted": ACCEPTED, "filing_date": "2026-10-21"}]
    )
    filings = ins.parse_submissions(payload, since=date(2026, 10, 1))
    assert len(filings) == 1
    assert filings[0].filed_at == FILED_AT


def test_the_transaction_date_is_stored_but_never_drives_the_timestamp():
    """THE requirement: a Form 4 is known at FILING, not at transaction.

    Here the trade was 2026-10-19 and acceptance 2026-10-21 — two days later,
    which is the legal maximum and the exact gap that leaks future information
    if a read keys on the wrong column.
    """
    rows = ins.parse_form4(fx.FORM4_PURCHASE, accession="0000320193-26-000100", filed_at=FILED_AT)
    assert len(rows) == 1
    row = rows[0]
    assert row["transaction_date"] == date(2026, 10, 19)
    assert row["filed_at"] == FILED_AT
    assert row["filed_session"] == date(2026, 10, 21), "the ET day of ACCEPTANCE"
    assert row["filed_session"] != row["transaction_date"]


def test_an_explicit_offset_is_respected_and_a_naive_one_is_read_as_eastern():
    """Both halves matter. Trusting an explicit `Z` is right; assuming UTC for a
    naive US-market timestamp would place it four hours early — lookahead."""
    from core import clock

    assert clock.to_utc(datetime.fromisoformat("2026-10-21T16:35:00+00:00")).hour == 16
    # DTZ001 is waived HERE on purpose: a naive datetime is exactly the input
    # under test, and the assertion is that it is read as Eastern rather than UTC.
    naive = datetime(2026, 10, 21, 16, 35)  # noqa: DTZ001
    assert clock.to_utc(naive).hour == 20  # ET -> UTC


def test_a_missing_acceptance_time_falls_back_to_the_CLOSE_not_midnight():
    """Midnight would make the filing look knowable at the open of its filing
    date, manufacturing up to a day of lookahead on the rows we are least sure
    about. The close is the latest plausible moment inside that date."""
    payload = fx.submissions([{"accession": "x-1", "accepted": None, "filing_date": "2026-10-21"}])
    filings = ins.parse_submissions(payload, since=date(2026, 10, 1))
    assert filings[0].filed_at.astimezone(UTC).hour == 20, "16:00 ET"


def test_filings_before_the_since_date_are_skipped():
    payload = fx.submissions(
        [
            {"accession": "new", "accepted": ACCEPTED},
            {"accession": "old", "accepted": "2025-01-05T16:00:00.000Z"},
        ]
    )
    accessions = [f.accession for f in ins.parse_submissions(payload, since=date(2026, 10, 1))]
    assert accessions == ["new"]


def test_only_form_4_filings_are_taken():
    payload = fx.submissions(
        [
            {"accession": "a", "accepted": ACCEPTED, "form": "4"},
            {"accession": "b", "accepted": ACCEPTED, "form": "10-K"},
            {"accession": "c", "accepted": ACCEPTED, "form": "4/A"},
        ]
    )
    accessions = [f.accession for f in ins.parse_submissions(payload, since=date(2026, 10, 1))]
    assert accessions == ["a", "c"]


def test_the_parallel_array_shape_is_read_without_an_off_by_one():
    """EDGAR returns parallel arrays, where a slip attaches the wrong timestamp
    to the wrong filing."""
    payload = fx.submissions(
        [
            {"accession": "first", "accepted": "2026-10-20T16:00:00.000Z", "document": "a.xml"},
            {"accession": "second", "accepted": "2026-10-21T16:00:00.000Z", "document": "b.xml"},
        ]
    )
    filings = {f.accession: f for f in ins.parse_submissions(payload, since=date(2026, 10, 1))}
    assert filings["first"].document == "a.xml"
    assert filings["second"].document == "b.xml"
    assert filings["first"].filed_at < filings["second"].filed_at


# ══════════════════════════════════════════════════════════════════════════
# Form 4 parsing
# ══════════════════════════════════════════════════════════════════════════


def test_a_purchase_parses_with_its_value_computed():
    rows = ins.parse_form4(fx.FORM4_PURCHASE, accession="a-1", filed_at=FILED_AT)
    row = rows[0]
    assert row["transaction_code"] == "P"
    assert row["shares"] == pytest.approx(5000)
    assert row["price_per_share"] == pytest.approx(230.50)
    assert row["value_usd"] == pytest.approx(1_152_500.00)
    assert row["acquired_disposed"] == "A"
    assert row["is_derivative"] is False


def test_the_insider_identity_and_role_are_captured():
    row = ins.parse_form4(fx.FORM4_PURCHASE, accession="a-1", filed_at=FILED_AT)[0]
    assert row["insider_cik"] == "0001214128"
    assert row["insider_name"] == "COOK TIMOTHY D"
    assert row["insider_title"] == "Chief Executive Officer"
    assert row["is_director"] is True
    assert row["is_officer"] is True
    assert row["is_ten_percent_owner"] is False


def test_awards_and_exercises_are_stored_unfiltered_with_their_real_codes():
    """Stored, not dropped: the cluster rule lives in config (D25), so
    narrowing here would mean re-fetching EDGAR whenever the rule changed."""
    award = ins.parse_form4(fx.FORM4_AWARD, accession="a-2", filed_at=FILED_AT)[0]
    exercise = ins.parse_form4(fx.FORM4_EXERCISE, accession="a-3", filed_at=FILED_AT)[0]
    assert award["transaction_code"] == "A"
    assert exercise["transaction_code"] == "M"


def test_a_10b5_1_sale_is_flagged():
    """Scheduled months earlier, so it carries no opinion about today."""
    row = ins.parse_form4(fx.FORM4_PLANNED_SALE, accession="a-4", filed_at=FILED_AT)[0]
    assert row["transaction_code"] == "S"
    assert row["acquired_disposed"] == "D"
    assert row["is_10b5_1"] is True


def test_a_derivative_row_is_marked_as_such():
    row = ins.parse_form4(fx.FORM4_DERIVATIVE, accession="a-5", filed_at=FILED_AT)[0]
    assert row["is_derivative"] is True
    assert row["transaction_code"] == "A"


def test_several_transactions_in_one_filing_get_distinct_indexes():
    """The primary key is (accession, txn_index), so a collision would silently
    drop one of a filing's transactions."""
    rows = ins.parse_form4(fx.FORM4_TWO_TRANSACTIONS, accession="a-6", filed_at=FILED_AT)
    assert len(rows) == 2
    assert [r["txn_index"] for r in rows] == [0, 1]


def test_malformed_xml_raises_rather_than_returning_nothing():
    """An empty list would look like a filing with no transactions."""
    with pytest.raises(EdgarError, match="did not parse"):
        ins.parse_form4(fx.MALFORMED, accession="a-7", filed_at=FILED_AT)


def test_every_row_carries_the_same_filing_timestamp():
    rows = ins.parse_form4(fx.FORM4_TWO_TRANSACTIONS, accession="a-8", filed_at=FILED_AT)
    assert {r["filed_at"] for r in rows} == {FILED_AT}
    assert {r["filed_session"] for r in rows} == {date(2026, 10, 21)}


# ══════════════════════════════════════════════════════════════════════════
# The rendered view vs the data
# ══════════════════════════════════════════════════════════════════════════
#
# Found live 2026-10-21: every Form 4 fetched for AAPL and MSFT failed to parse
# with "mismatched tag: line 29" until the XSL prefix was stripped. The trap is
# that the rendered HTML is served with an `.xml` extension, so the name says
# XML and the content is `<!DOCTYPE html>`.


@pytest.mark.parametrize(
    ("primary", "expected"),
    [
        ("xslF345X06/form4.xml", "form4.xml"),
        ("xslF345X05/primary_doc.xml", "primary_doc.xml"),
        ("xslF345X03/wk-form4_1234.xml", "wk-form4_1234.xml"),
        # Already the data: left alone.
        ("form4.xml", "form4.xml"),
        ("primary_doc.xml", "primary_doc.xml"),
    ],
)
def test_the_xsl_prefix_is_stripped_to_reach_the_raw_xml(primary, expected):
    from ingest.edgar_client import raw_xml_document

    assert raw_xml_document(primary) == expected


def test_a_non_xsl_subdirectory_is_not_stripped():
    """Only the transform directory is removed — not any path segment."""
    from ingest.edgar_client import raw_xml_document

    assert raw_xml_document("subdir/form4.xml") == "subdir/form4.xml"


def test_html_served_as_xml_fails_loudly_rather_than_silently():
    """The symptom that led here: an XML parser on an HTML page reports a
    mismatched tag dozens of lines in, pointing nowhere near the cause."""
    html = '<!DOCTYPE html PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">\n<html><head><br></head></html>'
    with pytest.raises(EdgarError, match="did not parse"):
        ins.parse_form4(html, accession="a-9", filed_at=FILED_AT)
