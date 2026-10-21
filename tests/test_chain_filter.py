"""D22 option-chain filter tests. Fixtures only — no network, no DB."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from core import config
from ingest.schwab_chains import (
    is_monthly_expiration,
    parse_chain,
    payload_has_contracts,
    select_expirations,
    within_strike_range,
)
from ingest.schwab_client import from_schwab_symbol, to_schwab_symbol
from tests.fixtures import schwab_payloads as fx

SNAPSHOT_TS = datetime(2026, 10, 20, 20, 0, tzinfo=UTC)


@pytest.fixture
def scope():
    """The shipped D22 config: +/-15% of spot x 4 monthly expirations."""
    return config.universe().option_chains


def rows(scope, payload=None, **overrides):
    return parse_chain(
        payload or fx.CHAIN,
        symbol="TEST",
        snapshot_ts=SNAPSHOT_TS,
        session_date=fx.AS_OF,
        scope=scope,
        **overrides,
    )


# ══════════════════════════════════════════════════════════════════════════
# Monthly detection — expirationType 'S' with a third-Friday fallback
# ══════════════════════════════════════════════════════════════════════════


def test_the_schwab_expiration_type_is_authoritative_when_present():
    assert is_monthly_expiration(fx.WEEKLY, "S") is True  # tag wins
    assert is_monthly_expiration(fx.MONTHLIES[0], "W") is False  # even on a 3rd Friday


def test_month_end_is_not_monthly():
    """THE TRAP. 'M' reads like "monthly" and means MONTH-END.

    Month-end expirations are a separate series from the third-Friday monthlies.
    Mixing them would break exactly the cross-day comparability D7 accumulates
    history for — silently, with the IV-rank series still looking fine.
    """
    assert is_monthly_expiration(fx.MONTH_END, "M") is False
    assert fx.MONTH_END.weekday() != 4  # 2026-11-30 is a Monday


def test_quarter_end_is_not_monthly():
    assert is_monthly_expiration(fx.QUARTER_END, "Q") is False


@pytest.mark.parametrize("day", fx.MONTHLIES)
def test_third_fridays_are_monthly_without_a_tag(day):
    assert is_monthly_expiration(day) is True
    assert day.weekday() == 4
    assert 15 <= day.day <= 21


@pytest.mark.parametrize(
    "day",
    [
        date(2026, 11, 27),  # 4th Friday
        date(2026, 11, 13),  # 2nd Friday
        date(2026, 11, 19),  # a Thursday
        date(2026, 11, 21),  # a Saturday
        date(2026, 11, 30),  # month end, a Monday
    ],
)
def test_non_third_fridays_are_not_monthly_without_a_tag(day):
    assert is_monthly_expiration(day) is False


def test_the_tag_and_the_fallback_agree_on_the_fixture_chain():
    """They agreed on all 23 expirations in the live capture; keep it that way."""
    for key, strikes in fx.CHAIN["callExpDateMap"].items():
        day = date.fromisoformat(key.split(":")[0])
        tag = next(c["expirationType"] for cs in strikes.values() for c in cs)
        assert is_monthly_expiration(day, tag) == is_monthly_expiration(day), day


# ── expiration selection ────────────────────────────────────────────────────


def test_the_nearest_four_monthlies_are_selected():
    """D22: the nearest 4 MONTHLY expirations."""
    everything = [*fx.MONTHLIES, fx.WEEKLY, fx.MONTH_END, fx.QUARTER_END, fx.FIFTH_MONTHLY]
    chosen = select_expirations(everything, count=4, monthly_only=True, as_of=fx.AS_OF)
    assert chosen == fx.MONTHLIES
    for trap in (fx.WEEKLY, fx.MONTH_END, fx.QUARTER_END, fx.FIFTH_MONTHLY):
        assert trap not in chosen


def test_monthly_only_can_be_turned_off():
    chosen = select_expirations(
        [fx.MONTHLIES[0], fx.WEEKLY], count=4, monthly_only=False, as_of=fx.AS_OF
    )
    assert fx.WEEKLY in chosen


def test_already_expired_dates_are_dropped():
    """A same-day expiring series has no life left and only adds noise to IV."""
    past = date(2026, 9, 18)
    chosen = select_expirations([past, *fx.MONTHLIES], count=4, monthly_only=True, as_of=fx.AS_OF)
    assert past not in chosen


def test_todays_expiration_is_kept():
    """`>= as_of`, not `>`: a chain pulled ON expiration day is still a data point."""
    today_expiry = date(2026, 11, 20)
    assert select_expirations([today_expiry], count=4, monthly_only=True, as_of=today_expiry) == [
        today_expiry
    ]


def test_selection_is_ordered_nearest_first():
    chosen = select_expirations(
        list(reversed(fx.MONTHLIES)), count=4, monthly_only=True, as_of=fx.AS_OF
    )
    assert chosen == sorted(chosen)


def test_fewer_expirations_than_requested_is_fine():
    assert (
        len(select_expirations(fx.MONTHLIES[:2], count=4, monthly_only=True, as_of=fx.AS_OF)) == 2
    )


def test_the_expiration_type_map_overrides_the_date_heuristic():
    types = {fx.WEEKLY: "S", fx.MONTHLIES[0]: "W"}
    chosen = select_expirations(
        [fx.WEEKLY, *fx.MONTHLIES], count=4, monthly_only=True, as_of=fx.AS_OF, types=types
    )
    assert fx.WEEKLY in chosen
    assert fx.MONTHLIES[0] not in chosen


# ══════════════════════════════════════════════════════════════════════════
# The +/-15% strike band
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("strike", "inside"),
    [
        (100.0, True),  # at the money
        (85.0, True),  # exactly -15%: the boundary is inclusive
        (115.0, True),  # exactly +15%
        (84.9, False),
        (115.1, False),
        (75.0, False),
        (125.0, False),
    ],
)
def test_the_fifteen_percent_band(strike, inside):
    assert within_strike_range(strike, 100.0, 15.0) is inside


def test_a_nonsense_spot_rejects_everything_rather_than_dividing_by_zero():
    assert within_strike_range(100.0, 0.0, 15.0) is False
    assert within_strike_range(100.0, -5.0, 15.0) is False


def test_the_band_scales_with_the_underlying():
    assert within_strike_range(4250.0, 5000.0, 15.0) is True  # -15%
    assert within_strike_range(4249.0, 5000.0, 15.0) is False


# ══════════════════════════════════════════════════════════════════════════
# Whole-payload parsing
# ══════════════════════════════════════════════════════════════════════════


def test_only_the_d22_scope_survives(scope):
    parsed = rows(scope)
    assert {r["expiration"] for r in parsed} == set(fx.MONTHLIES)
    assert {r["strike"] for r in parsed} == {85.0, 90.0, 95.0, 100.0, 105.0, 110.0, 115.0}


def test_row_count_is_expirations_x_strikes_x_sides(scope):
    assert len(rows(scope)) == 4 * 7 * 2


def test_both_sides_are_captured_because_skew_needs_puts(scope):
    assert {r["option_type"] for r in rows(scope)} == {"C", "P"}


def test_every_kept_row_is_flagged_monthly(scope):
    assert all(r["is_monthly"] for r in rows(scope))


# ── unit conversion and sentinels ───────────────────────────────────────────


def test_schwab_percent_iv_is_stored_as_a_decimal_fraction(scope):
    """One unit everywhere downstream, converted once at the boundary."""
    atm = next(
        r
        for r in rows(scope)
        if r["strike"] == 100.0 and r["expiration"] == fx.MONTHLIES[0] and r["option_type"] == "C"
    )
    assert atm["implied_volatility"] == pytest.approx(0.25)
    assert all(0 < r["implied_volatility"] < 3 for r in rows(scope))


def test_the_minus_999_sentinel_becomes_null_not_a_number(scope):
    """A -999 in an IV average would silently poison every volatility number."""
    parsed = rows(scope, payload=fx.chain_with_sentinels())
    front_calls = [
        r for r in parsed if r["expiration"] == fx.MONTHLIES[0] and r["option_type"] == "C"
    ]
    assert front_calls
    assert all(r["implied_volatility"] is None for r in front_calls)
    assert all(r["delta"] is None for r in front_calls)
    # Other expirations are untouched.
    assert any(
        r["implied_volatility"] is not None for r in parsed if r["expiration"] == fx.MONTHLIES[1]
    )


# ── the extrinsicValue trap ─────────────────────────────────────────────────


def test_extrinsic_value_comes_from_time_value_not_extrinsic_value(scope):
    """REGRESSION GUARD for the live finding.

    Schwab's `extrinsicValue` is wrong — it exceeds the mark on OTM contracts.
    The fixture carries it with the same wrongness the real payload has, so a
    future edit that switches to the better-named field fails here.
    """
    key = f"{fx.MONTHLIES[0]:%Y-%m-%d}:{(fx.MONTHLIES[0] - fx.AS_OF).days}"
    for strike in ("100.0", "110.0", "90.0"):
        contract = fx.CHAIN["callExpDateMap"][key][strike][0]
        parsed = next(
            r
            for r in rows(scope)
            if r["strike"] == float(strike)
            and r["expiration"] == fx.MONTHLIES[0]
            and r["option_type"] == "C"
        )
        assert parsed["extrinsic_value"] == contract["timeValue"]
        if contract["timeValue"] != contract["extrinsicValue"]:
            assert parsed["extrinsic_value"] != contract["extrinsicValue"]


def test_the_fixture_still_carries_the_wrong_extrinsic_value():
    """If the fixture were "fixed", the regression guard above would stop biting."""
    key = f"{fx.MONTHLIES[0]:%Y-%m-%d}:{(fx.MONTHLIES[0] - fx.AS_OF).days}"
    otm = fx.CHAIN["callExpDateMap"][key]["115.0"][0]
    assert otm["extrinsicValue"] != otm["timeValue"]
    # An OTM option is ENTIRELY extrinsic, so a value above the mark is nonsense.
    assert otm["extrinsicValue"] > otm["mark"]
    assert otm["timeValue"] <= otm["mark"]


# ── chain-level refusals ────────────────────────────────────────────────────


def test_a_truncated_chain_is_refused(scope):
    """D7: a partial surface stored as if whole is undetectable afterwards."""
    with pytest.raises(ValueError, match="isChainTruncated"):
        rows(scope, payload=fx.truncated_chain())


def test_a_delayed_chain_is_refused(scope):
    """IV rank must not be compared across live and delayed observations."""
    with pytest.raises(ValueError, match="isDelayed"):
        rows(scope, payload=fx.delayed_chain())


def test_a_payload_with_no_underlying_price_fails_loudly(scope):
    """Silently snapshotting with a zero spot would centre the band on nothing."""
    broken = {**fx.CHAIN, "underlyingPrice": 0, "underlying": {}}
    with pytest.raises(ValueError, match="no usable underlying price"):
        rows(scope, payload=broken)


def test_the_underlying_price_falls_back_to_the_quote_block(scope):
    payload = {**fx.CHAIN, "underlyingPrice": None, "underlying": {"last": 100.0}}
    assert len(rows(scope, payload=payload)) == 4 * 7 * 2


# ── contract-level exclusions ───────────────────────────────────────────────


def test_non_standard_contracts_are_excluded(scope):
    """Adjusted after a corporate action: odd deliverables, incomparable IV."""
    parsed = rows(scope, payload=fx.chain_with_nonstandard_contracts())
    front_calls = [
        r for r in parsed if r["expiration"] == fx.MONTHLIES[0] and r["option_type"] == "C"
    ]
    assert front_calls == []
    assert any(r["expiration"] == fx.MONTHLIES[1] for r in parsed)  # the rest survive


def test_mini_contracts_are_excluded(scope):
    parsed = rows(scope, payload=fx.chain_with_mini_contracts())
    second_puts = [
        r for r in parsed if r["expiration"] == fx.MONTHLIES[1] and r["option_type"] == "P"
    ]
    assert second_puts == []


def test_the_open_interest_floor_is_applied(scope):
    strict = scope.model_copy(update={"min_open_interest": 5000})
    assert rows(strict) == []


def test_calls_or_puts_can_be_disabled(scope):
    puts_only = scope.model_copy(update={"include_calls": False})
    assert {r["option_type"] for r in rows(puts_only)} == {"P"}


# ── mapping and stamping ────────────────────────────────────────────────────


def test_contract_fields_are_mapped(scope):
    atm = next(
        r
        for r in rows(scope)
        if r["strike"] == 100.0 and r["expiration"] == fx.MONTHLIES[0] and r["option_type"] == "C"
    )
    assert atm["underlying_price"] == fx.SPOT
    assert atm["open_interest"] == 3400
    assert atm["volume"] == 120
    assert atm["days_to_expiration"] == (fx.MONTHLIES[0] - fx.AS_OF).days
    assert atm["is_monthly"] is True
    assert atm["contract_symbol"].startswith("TEST")
    assert atm["mark"] == pytest.approx((atm["bid"] + atm["ask"]) / 2, abs=0.01)


def test_the_snapshot_stamp_is_identical_across_every_row(scope):
    """One symbol, one instant — the PK and every day-over-day diff depend on it."""
    parsed = rows(scope)
    assert {r["snapshot_ts"] for r in parsed} == {SNAPSHOT_TS}
    assert {r["session_date"] for r in parsed} == {fx.AS_OF}


def test_the_date_fallback_works_when_schwab_omits_the_type_tag(scope):
    parsed = rows(scope, payload=fx.chain_without_expiration_types())
    assert {r["expiration"] for r in parsed} == set(fx.MONTHLIES)


def test_a_chain_with_no_monthlies_returns_nothing_rather_than_guessing(scope):
    key = f"{fx.WEEKLY:%Y-%m-%d}:{(fx.WEEKLY - fx.AS_OF).days}"
    payload = {
        "symbol": "TEST",
        "underlyingPrice": 100.0,
        "callExpDateMap": {key: fx.CHAIN["callExpDateMap"][key]},
        "putExpDateMap": {},
    }
    assert rows(scope, payload=payload) == []


def test_a_malformed_expiration_key_is_skipped(scope):
    payload = {**fx.CHAIN, "callExpDateMap": {"not-a-date": {}, **fx.CHAIN["callExpDateMap"]}}
    assert len(rows(scope, payload=payload)) == 4 * 7 * 2


# ══════════════════════════════════════════════════════════════════════════
# has_options detection (arms the D14 gate)
# ══════════════════════════════════════════════════════════════════════════


def test_a_chain_with_contracts_reports_options_available():
    assert payload_has_contracts(fx.CHAIN) is True


def test_an_empty_chain_reports_no_options():
    assert payload_has_contracts({"callExpDateMap": {}, "putExpDateMap": {}}) is False
    assert payload_has_contracts({}) is False


def test_expirations_present_but_no_contracts_still_means_no_options():
    assert payload_has_contracts({"callExpDateMap": {"2026-11-20:31": {"100.0": []}}}) is False


def test_detection_reads_the_raw_payload_not_the_filtered_rows(scope):
    """A name whose every strike sits outside +/-15% still HAS options.

    Conflating "nothing inside the D22 band" with "no options listed" would set
    has_options=False and drop a liquid ticker on the next universe rebuild.
    """
    key = f"{fx.MONTHLIES[0]:%Y-%m-%d}:{(fx.MONTHLIES[0] - fx.AS_OF).days}"
    far_only = {
        "symbol": "TEST",
        "underlyingPrice": 100.0,
        "callExpDateMap": {key: {"75.0": fx.CHAIN["callExpDateMap"][key]["75.0"]}},
        "putExpDateMap": {},
    }
    assert rows(scope, payload=far_only) == []  # nothing to snapshot
    assert payload_has_contracts(far_only) is True  # but it does have options


# ══════════════════════════════════════════════════════════════════════════
# Symbol translation at the broker boundary
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("canonical", "schwab"), [("BRK-B", "BRK/B"), ("BF-B", "BF/B"), ("AAPL", "AAPL")]
)
def test_share_class_symbols_translate_both_ways(canonical, schwab):
    """The canonical form is the DASH form — what FMP /stable/ returns.

    Live 2026-10-21: FMP constituents gave `BRK-B` and `BF-B`, and the dot form
    is rejected with a 402. The translator used to convert only `.` -> `/`, so
    `BRK-B` reached Schwab verbatim, no chain came back, and the only two
    share-class names in the index silently lost their options — which with the
    D14 gate armed would have dropped them from the universe over punctuation.
    """
    assert to_schwab_symbol(canonical) == schwab
    assert from_schwab_symbol(schwab) == canonical


def test_the_translation_round_trips_for_the_shipped_universe():
    for symbol in ("BRK-B", "BF-B"):
        assert from_schwab_symbol(to_schwab_symbol(symbol)) == symbol


def test_a_dotted_symbol_still_reaches_schwab_correctly():
    """Tolerated input: a hand-maintained file may use either separator."""
    assert to_schwab_symbol("BRK.B") == "BRK/B"


def test_the_static_fallback_canonicalises_to_the_form_fmp_accepts():
    """The fallback must not produce symbols FMP answers 402 for.

    `config/constituents_static.txt` carries the dot form. FMP `/stable/` keys
    on the dash form and 402s on dots, and `enrich_with_liquidity` aborts the
    whole build on a systemic 402 — so an uncanonicalised fallback would be a
    path that works only while the budget is too exhausted to make the call
    that breaks it.
    """
    from ingest.universe import normalize_symbol, parse_static_constituents

    static = parse_static_constituents(
        Path("config/constituents_static.txt").read_text(), path="static"
    )
    symbols = {r["symbol"] for r in static.sp500} | {r["symbol"] for r in static.nasdaq100}
    assert "BRK-B" in symbols and "BF-B" in symbols
    assert not any("." in s for s in symbols), "a dotted symbol would 402 on FMP /stable/"
    assert normalize_symbol("brk.b") == "BRK-B"
