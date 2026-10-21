"""Parser tests for the step 5 ingestion jobs. Fixtures only — no network, no DB.

Everything that turns a provider payload into database rows is a pure function,
tested here. The I/O wrappers around them are thin by design.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from core import config
from features.adjustment import compute_adjustment_factors
from ingest.finnhub_news import parse_articles
from ingest.fmp_estimates import parse_estimates
from ingest.fmp_events import parse_dividends, parse_earnings, parse_splits
from ingest.fmp_fundamentals import (
    OPTIONAL_ENDPOINTS,
    merge_statements,
    reset_availability,
    unavailable_endpoints,
)
from ingest.runner import (
    SymbolOutcome,
    TooManyFailures,
    as_float,
    as_int,
    for_each_symbol,
    safe_ratio,
)
from tests.fixtures import fmp_payloads as fx

TODAY = date(2026, 10, 20)
UNIVERSE = {"AAPL", "MSFT"}


# ══════════════════════════════════════════════════════════════════════════
# Estimate snapshots (D7) — the sacred job
# ══════════════════════════════════════════════════════════════════════════


def estimates(limit=4, snapshot_date=TODAY):
    return parse_estimates(
        fx.ANALYST_ESTIMATES_QUARTER,
        symbol="AAPL",
        period_type="quarter",
        snapshot_date=snapshot_date,
        limit=limit,
    )


def test_estimates_are_stamped_with_the_snapshot_date():
    """D7: the signal is the diff between consecutive snapshot_dates."""
    assert all(row["snapshot_date"] == TODAY for row in estimates())


def test_already_reported_periods_are_dropped():
    """FMP backfilling its own history must not manufacture revision signal."""
    periods = [row["fiscal_period_end"] for row in estimates()]
    assert date(2026, 6, 30) not in periods
    assert periods == [date(2026, 12, 31), date(2027, 3, 31), date(2027, 6, 30)]


def test_estimates_come_back_in_fiscal_order():
    periods = [row["fiscal_period_end"] for row in estimates()]
    assert periods == sorted(periods)


def test_the_configured_number_of_periods_is_kept():
    assert [r["fiscal_period_end"] for r in estimates(limit=2)] == [
        date(2026, 12, 31),
        date(2027, 3, 31),
    ]


def test_estimate_fields_are_mapped():
    row = estimates()[0]
    assert row["revenue_avg"] == 128_000_000_000
    assert row["eps_avg"] == 2.35
    assert row["eps_low"] == 2.25
    assert row["ebitda_avg"] == 45e9
    assert row["analysts_eps"] == 31
    assert row["analysts_revenue"] == 29


def test_missing_estimate_fields_become_null_not_zero():
    """NULL means "no consensus"; 0.0 would look like a collapsed estimate."""
    row = next(r for r in estimates() if r["fiscal_period_end"] == date(2027, 3, 31))
    assert row["revenue_low"] is None
    assert row["ebitda_avg"] is None
    assert row["revenue_avg"] == 99_000_000_000


def test_a_later_snapshot_date_narrows_the_forward_window():
    """As periods report they leave the snapshot; nothing prior is rewritten."""
    later = parse_estimates(
        fx.ANALYST_ESTIMATES_QUARTER,
        symbol="AAPL",
        period_type="quarter",
        snapshot_date=date(2027, 1, 15),
        limit=4,
    )
    assert [r["fiscal_period_end"] for r in later] == [date(2027, 3, 31), date(2027, 6, 30)]


def test_rows_without_a_usable_date_are_skipped():
    rows = [{"date": None, "estimatedEpsAvg": 1.0}, {"date": "nonsense", "estimatedEpsAvg": 1.0}]
    assert (
        parse_estimates(rows, symbol="AAPL", period_type="quarter", snapshot_date=TODAY, limit=4)
        == []
    )


def test_consecutive_snapshots_produce_a_clean_revision_diff():
    """The actual D7 edge, computed from two parsed snapshots."""
    day1 = parse_estimates(
        fx.ANALYST_ESTIMATES_QUARTER,
        symbol="AAPL",
        period_type="quarter",
        snapshot_date=TODAY,
        limit=4,
    )
    raised = [
        {**r, "estimatedEpsAvg": (r.get("estimatedEpsAvg") or 0) + 0.05}
        for r in fx.ANALYST_ESTIMATES_QUARTER
    ]
    day2 = parse_estimates(
        raised,
        symbol="AAPL",
        period_type="quarter",
        snapshot_date=date(2026, 10, 21),
        limit=4,
    )
    by_period_1 = {r["fiscal_period_end"]: r["eps_avg"] for r in day1}
    by_period_2 = {r["fiscal_period_end"]: r["eps_avg"] for r in day2}
    deltas = {p: round(by_period_2[p] - by_period_1[p], 4) for p in by_period_1}
    assert set(deltas.values()) == {0.05}


# ══════════════════════════════════════════════════════════════════════════
# Fundamentals (Pillar B)
# ══════════════════════════════════════════════════════════════════════════


def merged(ratios=None, metrics=None):
    return merge_statements(
        "AAPL",
        "quarter",
        fx.INCOME,
        fx.BALANCE,
        fx.CASHFLOW,
        fx.RATIOS if ratios is None else ratios,
        fx.KEY_METRICS if metrics is None else metrics,
    )


def test_statements_join_on_fiscal_period_end():
    rows = {r["fiscal_date_ending"]: r for r in merged()}
    q3 = rows[date(2026, 6, 27)]
    assert q3["revenue"] == 109_417_000_000
    assert q3["total_assets"] == 383_266_000_000  # balance sheet
    assert q3["free_cash_flow"] == 31_914_000_000  # cash flow
    assert q3["current_ratio"] == 0.94  # ratios
    assert q3["roic"] == 0.55  # key metrics


def test_the_income_statement_drives_the_row_set():
    """A period with ratios but no income statement is not a period we can score."""
    assert len(merged()) == len(fx.INCOME)


def test_a_missing_companion_statement_leaves_nulls_not_a_dropped_period():
    """Pillar B reads trends; one gap must not blank three years of margins."""
    q2 = next(r for r in merged() if r["fiscal_date_ending"] == date(2026, 3, 28))
    assert q2["total_assets"] is None  # no balance sheet for this period
    assert q2["revenue"] == 95_000_000_000  # but the period survives
    assert q2["free_cash_flow"] == 27_600_000_000


def test_margins_are_computed_from_the_statements():
    """Numerator and denominator come from the same filing, always — and so
    margins survive the D29 ratios gate."""
    q3 = next(r for r in merged() if r["fiscal_date_ending"] == date(2026, 6, 27))
    assert q3["gross_margin"] == pytest.approx(54.76 / 109.417, rel=1e-6)
    assert q3["operating_margin"] == pytest.approx(32 / 109.417, rel=1e-6)
    assert q3["net_margin"] == pytest.approx(29.789 / 109.417, rel=1e-6)


def test_zero_revenue_gives_a_null_margin_not_a_crash():
    income = [{"date": "2026-06-27", "revenue": 0, "grossProfit": 0, "netIncome": -5e6}]
    row = merge_statements("X", "quarter", income, [], [], [], [])[0]
    assert row["gross_margin"] is None
    assert row["net_margin"] is None


def test_total_debt_falls_back_to_its_components():
    balance = [{"date": "2026-06-27", "shortTermDebt": 1e9, "longTermDebt": 4e9}]
    row = merge_statements("X", "quarter", fx.INCOME[:1], balance, [], [], [])[0]
    assert row["total_debt"] == 5e9


def test_dividend_health_fields_are_carried_for_d19():
    q3 = next(r for r in merged() if r["fiscal_date_ending"] == date(2026, 6, 27))
    assert q3["dividend_per_share"] == 1.00
    assert q3["payout_ratio"] == 0.16


# ── the /stable/ field renames ──────────────────────────────────────────────


def test_both_fiscal_year_spellings_are_accepted():
    """`/stable/` renamed /api/v3/'s `calendarYear` to `fiscalYear`.

    Verified against live FMP data. Accepting both means a generation or plan
    change cannot silently blank the column.
    """
    for rows in (fx.INCOME[:1], fx.INCOME_LEGACY):
        row = merge_statements("AAPL", "quarter", rows, [], [], [], [])[0]
        assert row["calendar_year"] == 2026


def test_both_eps_diluted_spellings_are_accepted():
    """`epsdiluted` (v3) vs `epsDiluted` (/stable/)."""
    for rows in (fx.INCOME[:1], fx.INCOME_LEGACY):
        row = merge_statements("AAPL", "quarter", rows, [], [], [], [])[0]
        assert row["eps_diluted"] == 2.03


def test_both_filing_date_spellings_are_accepted():
    """`fillingDate` (v3, with the typo) vs `filingDate` (/stable/)."""
    for rows in (fx.INCOME[:1], fx.INCOME_LEGACY):
        row = merge_statements("AAPL", "quarter", rows, [], [], [], [])[0]
        assert row["filing_date"] == date(2026, 7, 31)


# ── D29 degradation ─────────────────────────────────────────────────────────


def test_plan_gated_enrichment_leaves_columns_null_without_losing_the_statements():
    """FMP gates /stable/ratios and /stable/key-metrics separately from the
    statements. Losing valuation multiples must not cost us revenue and margins."""
    row = next(
        r for r in merged(ratios=[], metrics=[]) if r["fiscal_date_ending"] == date(2026, 6, 27)
    )
    # Statement-derived: present.
    assert row["revenue"] == 109_417_000_000
    assert row["gross_margin"] is not None
    assert row["free_cash_flow"] == 31_914_000_000
    assert row["total_assets"] == 383_266_000_000
    # Enrichment-derived: NULL.
    for column in (
        "pe_ratio",
        "ps_ratio",
        "pb_ratio",
        "peg_ratio",
        "roe",
        "roic",
        "current_ratio",
        "debt_to_equity",
        "payout_ratio",
        "dividend_per_share",
        "ev_to_ebitda",
    ):
        assert row[column] is None, column


def test_the_optional_endpoints_are_exactly_the_two_d29_names():
    assert set(OPTIONAL_ENDPOINTS) == {"ratios", "key-metrics"}


def test_a_degraded_run_reports_which_endpoints_were_unavailable():
    from ingest.fmp_client import FMPPlanError
    from ingest.fmp_fundamentals import _optional

    reset_availability()
    try:

        def gated():
            raise FMPPlanError("402")

        assert _optional("ratios", gated) == []
        assert _optional("key-metrics", gated) == []
        assert unavailable_endpoints() == ["key-metrics", "ratios"]
    finally:
        reset_availability()


def test_a_non_plan_error_from_an_optional_endpoint_still_propagates():
    """Degradation is only for 402. A timeout is a real failure."""
    from ingest.fmp_fundamentals import _optional

    reset_availability()
    with pytest.raises(RuntimeError, match="timeout"):
        _optional("ratios", lambda: (_ for _ in ()).throw(RuntimeError("timeout")))


# ══════════════════════════════════════════════════════════════════════════
# Events + corporate actions
# ══════════════════════════════════════════════════════════════════════════


def test_earnings_are_filtered_to_the_universe():
    rows = parse_earnings(fx.EARNINGS_CALENDAR, UNIVERSE)
    assert {r["symbol"] for r in rows} == {"AAPL", "MSFT"}


def test_a_reported_quarter_is_confirmed_and_a_future_one_is_not():
    rows = {
        (r["symbol"], r["event_date"]): r for r in parse_earnings(fx.EARNINGS_CALENDAR, UNIVERSE)
    }
    reported = rows[("AAPL", date(2026, 10, 15))]
    upcoming = rows[("AAPL", date(2026, 10, 29))]
    assert reported["is_confirmed"] is True
    assert reported["eps_actual"] == 2.03
    assert upcoming["is_confirmed"] is False
    assert upcoming["eps_actual"] is None
    assert upcoming["eps_estimate"] == 2.35


def test_unknown_report_timing_maps_to_the_schema_value():
    """FMP sends '--'; the events CHECK only accepts bmo/amc/dmt/unknown."""
    rows = {
        (r["symbol"], r["event_date"]): r for r in parse_earnings(fx.EARNINGS_CALENDAR, UNIVERSE)
    }
    assert rows[("AAPL", date(2026, 11, 5))]["event_time"] == "unknown"
    assert rows[("AAPL", date(2026, 10, 29))]["event_time"] == "amc"


def test_every_event_row_carries_the_full_parameter_set():
    """A missing key would be a psycopg error at executemany time, not a parse bug."""
    expected = {
        "symbol",
        "event_type",
        "event_date",
        "event_time",
        "is_confirmed",
        "title",
        "eps_estimate",
        "eps_actual",
        "revenue_estimate",
        "revenue_actual",
        "dividend_amount",
        "declaration_date",
        "record_date",
        "payment_date",
    }
    div_events, _ = parse_dividends(fx.DIVIDEND_CALENDAR, UNIVERSE)
    split_events, _ = parse_splits(fx.SPLIT_CALENDAR, UNIVERSE)
    for row in [*parse_earnings(fx.EARNINGS_CALENDAR, UNIVERSE), *div_events, *split_events]:
        assert set(row) == expected


# ── a dividend writes to BOTH tables ────────────────────────────────────────


def test_a_dividend_produces_both_a_radar_event_and_an_adjustment_action():
    """D19 needs the ex-date on the radar; D24 needs it in the adjustment log."""
    events, actions = parse_dividends(fx.DIVIDEND_CALENDAR, UNIVERSE)
    aapl_event = next(e for e in events if e["symbol"] == "AAPL")
    aapl_action = next(a for a in actions if a["symbol"] == "AAPL")

    assert aapl_event["event_type"] == "ex_dividend"
    assert aapl_event["event_date"] == date(2026, 11, 6)
    assert aapl_event["dividend_amount"] == 0.26
    assert aapl_event["payment_date"] == date(2026, 11, 13)

    assert aapl_action["action_type"] == "cash_dividend"
    assert aapl_action["ex_date"] == date(2026, 11, 6)
    assert aapl_action["cash_amount"] == 0.26
    # Same fact, same date, two tables.
    assert aapl_event["event_date"] == aapl_action["ex_date"]


def test_dividends_without_an_amount_are_skipped():
    """A NULL amount would violate ck_corporate_actions_payload."""
    _, actions = parse_dividends(fx.DIVIDEND_CALENDAR, UNIVERSE)
    assert all(a["cash_amount"] is not None for a in actions)
    assert date(2026, 11, 20) not in {a["ex_date"] for a in actions}


def test_dividends_are_filtered_to_the_universe():
    events, actions = parse_dividends(fx.DIVIDEND_CALENDAR, UNIVERSE)
    assert "NOTINUNIVERSE" not in {e["symbol"] for e in events}
    assert "NOTINUNIVERSE" not in {a["symbol"] for a in actions}


# ── splits ──────────────────────────────────────────────────────────────────


def test_a_forward_split_becomes_a_ratio_above_one():
    """4-for-1 stores 4.0: shares held AFTER per share held BEFORE."""
    _, actions = parse_splits(fx.SPLIT_CALENDAR, UNIVERSE)
    aapl = next(a for a in actions if a["symbol"] == "AAPL")
    assert aapl["split_ratio"] == pytest.approx(4.0)
    assert aapl["ex_date"] == date(2026, 11, 18)


def test_a_reverse_split_becomes_a_ratio_below_one():
    _, actions = parse_splits(fx.SPLIT_CALENDAR, UNIVERSE)
    msft = next(a for a in actions if a["symbol"] == "MSFT")
    assert msft["split_ratio"] == pytest.approx(0.1)


def test_a_zero_ratio_split_is_skipped():
    """It would fail ck_corporate_actions_payload and corrupt every prior price."""
    _, actions = parse_splits(fx.SPLIT_CALENDAR, UNIVERSE)
    assert date(2026, 12, 9) not in {a["ex_date"] for a in actions}


def test_splits_also_reach_the_event_radar():
    """A split moves every level in an open trade plan; the human should see it."""
    events, _ = parse_splits(fx.SPLIT_CALENDAR, UNIVERSE)
    aapl = next(e for e in events if e["symbol"] == "AAPL")
    assert aapl["event_type"] == "split"
    assert "4-for-1" in aapl["title"]


# ══════════════════════════════════════════════════════════════════════════
# THE ROUND TRIP: parser output -> features/adjustment.py
# ══════════════════════════════════════════════════════════════════════════
# The parser and the adjustment module must agree on the shape and meaning of a
# corporate action. Testing each alone would let them drift: a ratio stored
# inverted would pass both suites and silently corrupt every historical price.


def test_a_parsed_split_round_trips_through_the_adjustment_module():
    _, actions = parse_splits(fx.SPLIT_CALENDAR, {"AAPL"})
    bars = pd.DataFrame(
        {"session_date": [date(2026, 11, 17), date(2026, 11, 18)], "close": [400.0, 100.0]}
    )
    factors = compute_adjustment_factors(
        bars, pd.DataFrame(actions), mode="split_only", as_of=date(2026, 11, 30)
    )
    # 4-for-1: the pre-split bar is quartered, the ex-date bar is untouched.
    assert factors.loc[date(2026, 11, 17), "price_factor"] == pytest.approx(0.25)
    assert factors.loc[date(2026, 11, 18), "price_factor"] == pytest.approx(1.0)


def test_a_parsed_reverse_split_round_trips_the_other_way():
    _, actions = parse_splits(fx.SPLIT_CALENDAR, {"MSFT"})
    bars = pd.DataFrame(
        {"session_date": [date(2026, 12, 1), date(2026, 12, 2)], "close": [50.0, 500.0]}
    )
    factors = compute_adjustment_factors(
        bars, pd.DataFrame(actions), mode="split_only", as_of=date(2026, 12, 31)
    )
    assert factors.loc[date(2026, 12, 1), "price_factor"] == pytest.approx(10.0)


def test_a_parsed_dividend_round_trips_through_the_adjustment_module():
    _, actions = parse_dividends(fx.DIVIDEND_CALENDAR, {"AAPL"})
    # $0.26 on a prior close of 26.00 => factor 0.99 on every earlier bar.
    bars = pd.DataFrame(
        {"session_date": [date(2026, 11, 5), date(2026, 11, 6)], "close": [26.0, 25.74]}
    )
    factors = compute_adjustment_factors(
        bars, pd.DataFrame(actions), mode="total_return", as_of=date(2026, 11, 30)
    )
    assert factors.loc[date(2026, 11, 5), "price_factor"] == pytest.approx(0.99)
    assert factors.loc[date(2026, 11, 6), "price_factor"] == pytest.approx(1.0)


def test_the_parsers_action_type_strings_are_the_ones_adjustment_recognises():
    """A typo in either module would make the action silently ignored."""
    from features.adjustment import CASH_TYPES, SPLIT_TYPES

    _, div_actions = parse_dividends(fx.DIVIDEND_CALENDAR, UNIVERSE)
    _, split_actions = parse_splits(fx.SPLIT_CALENDAR, UNIVERSE)
    assert {a["action_type"] for a in div_actions} <= CASH_TYPES
    assert {a["action_type"] for a in split_actions} <= SPLIT_TYPES


def test_the_parsers_action_types_are_all_valid_schema_values():
    """They must satisfy corporate_actions' CHECK constraint."""
    from pathlib import Path

    schema = (Path(__file__).resolve().parent.parent / "db" / "schema.sql").read_text()
    _, div_actions = parse_dividends(fx.DIVIDEND_CALENDAR, UNIVERSE)
    _, split_actions = parse_splits(fx.SPLIT_CALENDAR, UNIVERSE)
    for action in div_actions + split_actions:
        assert f"'{action['action_type']}'" in schema


# ══════════════════════════════════════════════════════════════════════════
# Finnhub news (Pillar D corpus)
# ══════════════════════════════════════════════════════════════════════════


def test_news_epoch_becomes_a_utc_timestamp():
    """CLAUDE.md: all timestamps UTC in the DB."""
    rows = parse_articles(fx.FINNHUB_NEWS, "AAPL")
    assert rows[0]["published_at"] == datetime.fromtimestamp(1_792_000_000, tz=UTC)
    assert rows[0]["published_at"].tzinfo is not None


def test_news_article_ids_are_stored_as_text_for_the_upsert_key():
    rows = parse_articles(fx.FINNHUB_NEWS, "AAPL")
    assert rows[0]["article_id"] == "7411001"
    assert isinstance(rows[0]["article_id"], str)


def test_duplicate_articles_inside_one_window_collapse():
    """Finnhub repeats stories; the upsert would cope, but not silently."""
    rows = parse_articles(fx.FINNHUB_NEWS, "AAPL")
    assert len(rows) == len({r["article_id"] for r in rows}) == 2


def test_articles_without_an_id_or_timestamp_are_dropped():
    ids = {r["article_id"] for r in parse_articles(fx.FINNHUB_NEWS, "AAPL")}
    assert ids == {"7411001", "7411002"}


def test_empty_strings_become_null():
    rows = parse_articles(fx.FINNHUB_NEWS, "AAPL")
    second = next(r for r in rows if r["article_id"] == "7411002")
    assert second["summary"] is None
    assert second["image_url"] is None


def test_each_article_is_tagged_to_the_requested_symbol():
    """One story about two tickers stores two rows; the pillar scores per symbol."""
    for symbol in ("AAPL", "MSFT"):
        assert all(r["symbol"] == symbol for r in parse_articles(fx.FINNHUB_NEWS, symbol))


def test_no_sentiment_is_assigned_at_ingestion():
    """Phase 2 classifies; this job only accumulates the corpus."""
    rows = parse_articles(fx.FINNHUB_NEWS, "AAPL")
    assert all("sentiment_score" not in r for r in rows)


# ══════════════════════════════════════════════════════════════════════════
# for_each_symbol
# ══════════════════════════════════════════════════════════════════════════


def test_one_bad_symbol_does_not_stop_the_run():
    def handler(symbol):
        if symbol == "BAD":
            raise ValueError("provider hiccup")
        return 3

    outcome = for_each_symbol(["A", "BAD", "C"], handler, progress_every=0)
    assert outcome.ok == ["A", "C"]
    assert "BAD" in outcome.failed
    assert outcome.rows == 6


def test_a_broken_provider_fails_the_job_rather_than_writing_a_tenth_of_it():
    """An expired key looks like every symbol failing. That is not 'partial'."""

    def handler(symbol):
        raise RuntimeError("401 unauthorized")

    with pytest.raises(TooManyFailures, match="all 20 symbol"):
        for_each_symbol([f"S{i}" for i in range(20)], handler, progress_every=0)


def test_a_partial_failure_above_the_threshold_also_fails_the_job():
    """The 25% ratio rule, with some symbols succeeding so the total-failure
    rule does not fire first."""

    def handler(symbol):
        if symbol.endswith(("0", "1")):
            return 1
        raise RuntimeError("401 unauthorized")

    with pytest.raises(TooManyFailures, match="provider or credential problem"):
        for_each_symbol([f"S{i}" for i in range(20)], handler, progress_every=0)


def test_a_failure_rate_below_the_threshold_is_tolerated():
    def handler(symbol):
        if symbol == "S0":
            raise RuntimeError("one bad symbol")
        return 1

    outcome = for_each_symbol([f"S{i}" for i in range(20)], handler, progress_every=0)
    assert outcome.fail_ratio == pytest.approx(0.05)
    assert len(outcome.ok) == 19


def test_a_small_debug_run_is_exempt_from_the_ratio_guard():
    """`--symbols AAPL MSFT` must stay usable exactly when something is broken.

    The exemption covers the RATIO rule only: on a tiny run a partial failure is
    tolerated, because one failure out of three is not evidence of a broken
    provider. Total failure is a separate rule and is never tolerated.
    """

    def handler(symbol):
        if symbol == "A":
            return 1
        raise RuntimeError("nope")

    outcome = for_each_symbol(["A", "B", "C"], handler, progress_every=0)
    assert outcome.fail_ratio > 0.5
    assert outcome.ok == ["A"]


def test_the_ten_symbol_minimum_is_where_the_ratio_starts_applying():
    def handler(symbol):
        if symbol in ("S0", "S1"):
            return 1
        raise RuntimeError("nope")

    # 9 symbols, 7 failures (78%) — under the minimum sample, so tolerated.
    outcome = for_each_symbol([f"S{i}" for i in range(9)], handler, progress_every=0)
    assert outcome.fail_ratio > 0.25

    # 10 symbols, 8 failures — at the minimum, so the ratio rule bites.
    with pytest.raises(TooManyFailures):
        for_each_symbol([f"S{i}" for i in range(10)], handler, progress_every=0)


def test_every_symbol_failing_always_raises_regardless_of_sample_size():
    """A step where nothing succeeded must never report success.

    Otherwise it logs "ok, 0 rows", which is indistinguishable from a step that
    legitimately had nothing to do — and that is how a broken pipeline looks
    healthy.
    """

    def handler(symbol):
        raise RuntimeError("blocked")

    with pytest.raises(TooManyFailures, match="all 2 symbol"):
        for_each_symbol(["A", "B"], handler, progress_every=0)


def test_a_single_symbol_failing_raises_too():
    def handler(symbol):
        raise RuntimeError("x")

    with pytest.raises(TooManyFailures, match="all 1 symbol"):
        for_each_symbol(["A"], handler, progress_every=0)


def test_an_empty_symbol_list_is_not_a_failure():
    outcome = for_each_symbol([], lambda s: 0, progress_every=0)
    assert outcome.attempted == 0
    assert outcome.fail_ratio == 0.0


def test_a_run_that_writes_zero_rows_but_succeeds_is_fine():
    """Distinct from total failure: a quiet news day is not a broken job."""
    outcome = for_each_symbol(["A", "B"], lambda s: 0, progress_every=0)
    assert outcome.ok == ["A", "B"]
    assert outcome.rows == 0


def test_outcome_counts_are_what_job_runs_records():
    outcome = SymbolOutcome(ok=["A", "B"], failed={"C": "boom"}, rows=17)
    assert outcome.attempted == 3
    assert outcome.rows == 17
    assert outcome.fail_ratio == pytest.approx(1 / 3)


# ── numeric coercion ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(1, 1.0), ("2.5", 2.5), (None, None), ("", None), ("n/a", None), (float("nan"), None)],
)
def test_as_float_normalises_provider_numerics(raw, expected):
    assert as_float(raw) == expected


def test_as_int_truncates_and_tolerates_junk():
    assert as_int("30") == 30
    assert as_int(30.9) == 30
    assert as_int(None) is None


def test_safe_ratio_returns_none_on_a_zero_base():
    assert safe_ratio(5, 0) is None
    assert safe_ratio(5, None) is None
    assert safe_ratio(None, 5) is None
    assert safe_ratio(1, 4) == 0.25


# ── config wiring ───────────────────────────────────────────────────────────


def test_ingestion_scope_comes_from_config():
    u = config.universe()
    assert u.news.lookback_days == 7
    assert u.fundamentals.refresh_after_days == 7
    assert u.events.forward_days == 90
    assert u.estimates.quarters_ahead == 4
    assert u.fmp.daily_call_budget == 12000  # Premium, raised for O7's backfill


# ── every FMP-budget job must stop cleanly on a spent budget ──────────────────


def test_fmp_budget_jobs_pass_stop_on_budget_exhausted():
    """A spent day budget is declined spend (D29), never a symbol failure.

    Found live on 2026-10-20: `fmp_fundamentals` passed `stop_on=BudgetExhausted`
    and `fmp_estimates` did not, so when the shared counter ran dry every symbol
    "failed", `for_each_symbol` raised TooManyFailures on its all-failed rule,
    and a CRITICAL job failed for a reason the design classifies as normal —
    blocking `day_complete` for the rest of the day.

    Static, not behavioural: this must hold for every current and future module
    that spends FMP budget inside `for_each_symbol`, and a per-module runtime
    test would be forgotten for the next one added.
    """
    import ast
    from pathlib import Path

    # Modules whose `for_each_symbol` handler draws on the shared FMP budget.
    # Schwab and Finnhub are excluded: they have their own rate limiters and
    # never raise BudgetExhausted, so requiring it there would be cargo cult.
    fmp_budget_modules = [
        "ingest/fmp_estimates.py",
        "ingest/fmp_fundamentals.py",
        "ingest/fmp_events.py",
    ]

    missing = []
    for rel in fmp_budget_modules:
        tree = ast.parse(Path(rel).read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "for_each_symbol"
            ):
                kwargs = {kw.arg for kw in node.keywords}
                if "stop_on" not in kwargs:
                    missing.append(f"{rel}:{node.lineno}")

    assert not missing, (
        "for_each_symbol call(s) spending FMP budget without "
        f"stop_on=BudgetExhausted: {missing}. A spent budget would be counted as "
        "symbol failures and fail the job."
    )


# ── the estimates plan probe must not answer a question it could not ask ──────


def test_plan_probe_propagates_budget_exhaustion_instead_of_answering_false():
    """ "Could not afford to ask" is not "asked, and the plan covers it".

    Found live on 2026-10-20: the probe's blanket `except Exception: return False`
    swallowed BudgetExhausted, so with the day's allowance gone it reported
    "not plan-gated" on the strength of a question never put. The job then stayed
    CRITICAL and failed for a reason that had nothing to do with the plan.
    """
    from core.budget import BudgetExhausted
    from ingest.fmp_estimates import plan_blocks_estimates

    class Spent:
        def analyst_estimates(self, *a, **k):
            raise BudgetExhausted("fmp daily call budget is exhausted")

    with pytest.raises(BudgetExhausted):
        plan_blocks_estimates(Spent())


def test_plan_probe_still_reports_false_for_ordinary_errors():
    """Only a 402 is a billing answer; a transient error keeps the job critical."""
    from ingest.fmp_estimates import plan_blocks_estimates

    class Broken:
        def analyst_estimates(self, *a, **k):
            raise RuntimeError("connection reset")

    assert plan_blocks_estimates(Broken()) is False


def test_plan_probe_reports_true_only_on_402():
    from ingest.fmp_client import FMPPlanError
    from ingest.fmp_estimates import plan_blocks_estimates

    class Gated:
        def analyst_estimates(self, *a, **k):
            raise FMPPlanError("402")

    assert plan_blocks_estimates(Gated()) is True


# ── a 402 is not proof of a plan gate ─────────────────────────────────────────


def test_a_symbol_specific_402_does_not_disable_the_endpoint():
    """Verified live 2026-10-21: /stable/ratios 402s for BRK.B, 200s for AAPL.

    Inferring a plan gate from one symbol's 402 would NULL the valuation
    columns for every remaining symbol in the run, announced by a log line
    naming the wrong cause.
    """
    from ingest.fmp_client import FMPPlanError
    from ingest.fmp_fundamentals import _optional, reset_availability, unavailable_endpoints

    reset_availability()

    def fetch():
        raise FMPPlanError("402 for /stable/ratios?symbol=BRK.B")

    assert _optional("ratios", fetch, canary=lambda: [{"symbol": "AAPL"}]) == []
    assert unavailable_endpoints() == [], "a symbol-specific 402 must not gate the endpoint"
    reset_availability()


def test_a_real_plan_gate_still_disables_the_endpoint():
    """When the canary 402s too, the plan genuinely lacks it."""
    from ingest.fmp_client import FMPPlanError
    from ingest.fmp_fundamentals import _optional, reset_availability, unavailable_endpoints

    reset_availability()

    def gated():
        raise FMPPlanError("402")

    assert _optional("ratios", gated, canary=gated) == []
    assert unavailable_endpoints() == ["ratios"]
    reset_availability()


def test_without_a_canary_the_old_conservative_behaviour_holds():
    from ingest.fmp_client import FMPPlanError
    from ingest.fmp_fundamentals import _optional, reset_availability, unavailable_endpoints

    reset_availability()

    def gated():
        raise FMPPlanError("402")

    assert _optional("key-metrics", gated) == []
    assert unavailable_endpoints() == ["key-metrics"]
    reset_availability()


def test_every_valuation_column_populates():
    """A renamed provider field must fail a test, not empty a column.

    Live on 2026-10-21: seven of these were still /api/v3/ spellings that
    `/stable/` had renamed. Because `ratios` and `key-metrics` were 402-gated
    on the free tier the mismatch was never exercised — the crawl reported
    success, `unavailable_endpoints` came back empty, and six of ten valuation
    columns were silently NULL for every symbol. `unavailable_endpoints == []`
    proves the endpoint answered; it says nothing about the answer being read.
    """
    from ingest.fmp_fundamentals import merge_statements

    rows = merge_statements(
        "AAPL",
        "quarter",
        fx.INCOME,
        fx.BALANCE,
        fx.CASHFLOW,
        fx.RATIOS,
        fx.KEY_METRICS,
    )
    assert rows, "the fixture must produce at least one row"
    row = rows[0]

    valuation = (
        "pe_ratio",
        "ps_ratio",
        "pb_ratio",
        "peg_ratio",
        "ev_to_ebitda",
        "roic",
        "current_ratio",
        "debt_to_equity",
        "dividend_per_share",
        "payout_ratio",
    )
    empty = [c for c in valuation if row.get(c) is None]
    assert empty == [], f"valuation columns left NULL by the fixture payloads: {empty}"


def test_a_zero_dividend_is_kept_not_treated_as_missing():
    """`a or b` would read 0.0 as absent and fall through to the other source."""
    from ingest.fmp_fundamentals import merge_statements

    ratios = [{**fx.RATIOS[0], "dividendPerShare": 0.0}]
    metrics = [{**fx.KEY_METRICS[0], "dividendPerShare": 9.99}]
    rows = merge_statements("AAPL", "quarter", fx.INCOME, fx.BALANCE, fx.CASHFLOW, ratios, metrics)
    assert rows[0]["dividend_per_share"] == 0.0


def test_the_legacy_spellings_are_still_tolerated():
    """A v3-shaped payload must not silently empty the columns either."""
    from ingest.fmp_fundamentals import merge_statements

    legacy_ratios = [
        {
            "date": "2026-06-27",
            "currentRatio": 0.94,
            "debtEquityRatio": 0.78,
            "priceEarningsRatio": 33.2,
            "priceToSalesRatio": 8.7,
            "priceToBookRatio": 47.1,
            "priceEarningsToGrowthRatio": 2.4,
            "payoutRatio": 0.16,
        }
    ]
    legacy_metrics = [
        {
            "date": "2026-06-27",
            "roic": 0.55,
            "enterpriseValueOverEBITDA": 24.8,
            "dividendPerShare": 1.0,
        }
    ]
    rows = merge_statements(
        "AAPL",
        "quarter",
        fx.INCOME,
        fx.BALANCE,
        fx.CASHFLOW,
        legacy_ratios,
        legacy_metrics,
    )
    row = rows[0]
    assert row["pe_ratio"] == 33.2
    assert row["debt_to_equity"] == 0.78
    assert row["roic"] == 0.55
    assert row["ev_to_ebitda"] == 24.8
    assert row["payout_ratio"] == 0.16
    assert row["dividend_per_share"] == 1.0
