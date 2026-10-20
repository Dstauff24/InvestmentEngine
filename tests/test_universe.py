"""Universe builder tests (D14/D29). Fixtures only — no network, no database.

All the judgement lives in pure functions: `parse_static_constituents` reads the
offline fallback, `merge_constituents` decides who is a candidate,
`apply_liquidity_filter` decides who survives, `check_universe_size` refuses a
nonsense result. Each is tested directly; the I/O wrappers are thin by design.
"""

from __future__ import annotations

from datetime import date

import pytest

from core import config
from ingest.universe import (
    STATIC_FILE_STALE_AFTER_DAYS,
    Candidate,
    UniverseError,
    apply_liquidity_filter,
    average_volume,
    check_universe_size,
    enrich_with_liquidity,
    merge_constituents,
    normalize_symbol,
    parse_static_constituents,
)
from tests.fixtures import fmp_payloads as fx


@pytest.fixture
def liquidity():
    """The shipped D14 liquidity config."""
    return config.universe().liquidity


class StubFMP:
    """Stands in for FMPClient. Records calls; makes no network requests."""

    def __init__(self, *, volumes=None, fail_for: set[str] | None = None, plan_gated=False):
        self.volumes = fx.VOLUMES if volumes is None else volumes
        self.fail_for = fail_for or set()
        self.plan_gated = plan_gated
        self.quote_batches: list[list[str]] = []
        self.bar_calls: list[str] = []

    def sp500_constituents(self):
        if self.plan_gated:
            from ingest.fmp_client import FMPPlanError

            raise FMPPlanError("402 for /stable/sp500-constituent")
        return fx.SP500

    def nasdaq100_constituents(self):
        if self.plan_gated:
            from ingest.fmp_client import FMPPlanError

            raise FMPPlanError("402 for /stable/nasdaq-constituent")
        return fx.NASDAQ100

    def quotes(self, symbols):
        self.quote_batches.append(list(symbols))
        wanted = set(symbols)
        return [q for q in fx.QUOTES if q["symbol"] in wanted]

    def daily_bars(self, symbol, *, days):
        self.bar_calls.append(symbol)
        if symbol in self.fail_for:
            raise RuntimeError("simulated provider error")
        volume = self.volumes.get(symbol)
        return fx.daily_bars(volume, days=days) if volume is not None else []


def make(symbol="TEST", **kwargs) -> Candidate:
    return Candidate(symbol=symbol, **kwargs)


# ══════════════════════════════════════════════════════════════════════════
# The D29 static fallback
# ══════════════════════════════════════════════════════════════════════════


def test_the_static_file_parses_symbols_and_the_as_of_header():
    static = parse_static_constituents(fx.STATIC_FILE)
    assert static.as_of == date(2026, 10, 20)
    assert [r["symbol"] for r in static.sp500] == ["AAPL", "MSFT", "JNJ", "BRK.B", "NWSA"]
    assert [r["symbol"] for r in static.nasdaq100] == ["AAPL", "MSFT", "ZS"]
    assert static.total_rows == 8


def test_the_file_age_is_computable_so_the_caller_can_warn():
    static = parse_static_constituents(fx.STATIC_FILE)
    assert static.age_days(date(2026, 10, 20)) == 0
    assert static.age_days(date(2026, 12, 20)) == 61


def test_a_file_old_enough_to_be_wrong_is_detectable():
    """Index membership changes; a months-old list silently misses names."""
    static = parse_static_constituents(fx.STATIC_FILE)
    stale_day = date(2026, 10, 20) + __import__("datetime").timedelta(
        days=STATIC_FILE_STALE_AFTER_DAYS + 1
    )
    assert static.age_days(stale_day) > STATIC_FILE_STALE_AFTER_DAYS


def test_a_missing_as_of_header_yields_none_rather_than_failing():
    """The symbols are still usable; a loud warning beats a refusal."""
    static = parse_static_constituents(fx.STATIC_FILE_NO_HEADER)
    assert static.as_of is None
    assert static.age_days(date(2026, 10, 20)) is None
    assert static.total_rows == 2


def test_an_unparseable_as_of_header_yields_none():
    static = parse_static_constituents(fx.STATIC_FILE_BAD_HEADER)
    assert static.as_of is None
    assert static.total_rows == 1


def test_a_comment_only_file_parses_to_zero_rows():
    """The caller turns this into a refusal — an empty universe must not be written."""
    static = parse_static_constituents(fx.STATIC_FILE_EMPTY)
    assert static.total_rows == 0
    assert static.as_of == date(2026, 10, 20)


def test_index_tokens_are_case_insensitive_and_aliased():
    text = "# AS_OF: 2026-10-20\nAAPL,SP500\nMSFT,Nasdaq100\nZS,NDX\n"
    static = parse_static_constituents(text)
    assert [r["symbol"] for r in static.sp500] == ["AAPL"]
    assert [r["symbol"] for r in static.nasdaq100] == ["MSFT", "ZS"]


def test_an_unrecognised_index_token_is_skipped_not_guessed():
    text = "# AS_OF: 2026-10-20\nAAPL,sp500\nFOO,russell2000\n"
    static = parse_static_constituents(text)
    assert static.total_rows == 1


def test_malformed_rows_are_skipped():
    text = "# AS_OF: 2026-10-20\nAAPL,sp500\njust-one-field\nA,B,C\n,sp500\n"
    assert parse_static_constituents(text).total_rows == 1


def test_the_shipped_static_file_parses_to_the_expected_universe():
    """519 unique symbols, AS_OF 2026-10-20, as committed."""
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "config" / "constituents_static.txt"
    static = parse_static_constituents(path.read_text())
    assert static.as_of == date(2026, 10, 20)
    assert len(static.sp500) == 503
    assert len(static.nasdaq100) == 101
    merged = merge_constituents(static.sp500, static.nasdaq100, [])
    assert len(merged) == 519
    assert sum(1 for c in merged if c.in_sp500 and c.in_nasdaq100) == 85


# ══════════════════════════════════════════════════════════════════════════
# Symbol normalisation and merging
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("aapl", "AAPL"),
        ("  MSFT  ", "MSFT"),
        ("BRK.B", "BRK.B"),
        ("brk.b", "BRK.B"),
        ("BF B", "BFB"),
    ],
)
def test_normalize_symbol(raw, expected):
    assert normalize_symbol(raw) == expected


def test_merge_unions_the_two_indices():
    merged = merge_constituents(fx.SP500, fx.NASDAQ100, [])
    assert [c.symbol for c in merged] == ["AAPL", "BRK.B", "JNJ", "MSFT", "NWSA", "ZS"]


def test_overlapping_symbols_produce_one_row_with_both_flags():
    """D14's "~550 unique" out of 500 + 100 depends entirely on this."""
    merged = {c.symbol: c for c in merge_constituents(fx.SP500, fx.NASDAQ100, [])}
    assert merged["AAPL"].in_sp500 and merged["AAPL"].in_nasdaq100
    assert merged["JNJ"].in_sp500 and not merged["JNJ"].in_nasdaq100
    assert merged["ZS"].in_nasdaq100 and not merged["ZS"].in_sp500


def test_metadata_is_carried_through():
    merged = {c.symbol: c for c in merge_constituents(fx.SP500, fx.NASDAQ100, [])}
    assert merged["JNJ"].company_name == "Johnson & Johnson"
    assert merged["JNJ"].sector == "Healthcare"
    assert merged["JNJ"].industry == "Drug Manufacturers"


def test_watchlist_symbols_are_added_and_flagged():
    merged = {c.symbol: c for c in merge_constituents(fx.SP500, fx.NASDAQ100, ["SOFI"])}
    assert merged["SOFI"].in_watchlist
    assert not merged["SOFI"].in_sp500


def test_a_watchlist_symbol_already_in_an_index_is_flagged_not_duplicated():
    apple = [c for c in merge_constituents(fx.SP500, fx.NASDAQ100, ["AAPL"]) if c.symbol == "AAPL"]
    assert len(apple) == 1
    assert apple[0].in_sp500 and apple[0].in_nasdaq100 and apple[0].in_watchlist


def test_watchlist_is_case_insensitive():
    merged = {c.symbol: c for c in merge_constituents([], [], ["sofi", "SOFI"])}
    assert list(merged) == ["SOFI"]


def test_rows_without_a_symbol_are_skipped():
    """A provider hiccup should drop one row, not raise on the whole pull."""
    merged = merge_constituents([{"name": "no symbol here"}, *fx.SP500], [], [])
    assert len(merged) == len(fx.SP500)


def test_merging_is_deterministic():
    assert merge_constituents(fx.SP500, fx.NASDAQ100, ["SOFI"]) == merge_constituents(
        fx.SP500, fx.NASDAQ100, ["SOFI"]
    )


# ══════════════════════════════════════════════════════════════════════════
# The D14 liquidity filter
# ══════════════════════════════════════════════════════════════════════════


def test_volume_above_one_million_passes(liquidity):
    assert (
        len(apply_liquidity_filter([make(avg_volume=1_400_000, last_price=50.0)], liquidity).kept)
        == 1
    )


def test_volume_below_one_million_is_rejected(liquidity):
    result = apply_liquidity_filter([make(avg_volume=900_000, last_price=50.0)], liquidity)
    assert result.reasons == {"avg volume below minimum": 1}


def test_the_threshold_is_strictly_greater_than(liquidity):
    """D14 says ">1M". Exactly 1,000,000 does not pass."""
    assert (
        apply_liquidity_filter([make(avg_volume=1_000_000, last_price=50.0)], liquidity).kept == []
    )
    assert (
        len(apply_liquidity_filter([make(avg_volume=1_000_001, last_price=50.0)], liquidity).kept)
        == 1
    )


def test_unknown_volume_is_rejected_not_waved_through(liquidity):
    """An unverified name in a universe built for liquidity defeats the filter."""
    result = apply_liquidity_filter([make(avg_volume=None, last_price=50.0)], liquidity)
    assert result.reasons == {"no volume data": 1}


def test_price_below_minimum_is_rejected(liquidity):
    result = apply_liquidity_filter([make(avg_volume=9_000_000, last_price=2.5)], liquidity)
    assert result.reasons == {"price below minimum": 1}


def test_unknown_price_does_not_reject(liquidity):
    assert (
        len(apply_liquidity_filter([make(avg_volume=9_000_000, last_price=None)], liquidity).kept)
        == 1
    )


# ── the has_options abstain behaviour ───────────────────────────────────────


def test_a_name_known_to_have_no_options_is_rejected(liquidity):
    result = apply_liquidity_filter(
        [make(avg_volume=9_000_000, last_price=50.0, has_options=False)], liquidity
    )
    assert result.reasons == {"no options chain": 1}


def test_unknown_options_availability_abstains(liquidity):
    """D14: the gate abstains on None.

    Schwab is the only source for chain availability, and until schwab_chains
    has run nothing can set the flag. Guessing False would empty the universe;
    guessing True would be a claim we cannot support. Abstaining is the only
    honest option, and it is why the flag is nullable.
    """
    result = apply_liquidity_filter(
        [make(avg_volume=9_000_000, last_price=50.0, has_options=None)], liquidity
    )
    assert len(result.kept) == 1


def test_the_gate_enforces_once_the_flag_is_populated(liquidity):
    """Mixed population: abstain for unknown, reject for known-false."""
    result = apply_liquidity_filter(
        [
            make("KNOWN_YES", avg_volume=9_000_000, last_price=50.0, has_options=True),
            make("KNOWN_NO", avg_volume=9_000_000, last_price=50.0, has_options=False),
            make("UNCHECKED", avg_volume=9_000_000, last_price=50.0, has_options=None),
        ],
        liquidity,
    )
    assert sorted(c.symbol for c in result.kept) == ["KNOWN_YES", "UNCHECKED"]
    assert [(c.symbol, why) for c, why in result.rejected] == [("KNOWN_NO", "no options chain")]


def test_the_options_gate_can_be_disabled(liquidity):
    lax = liquidity.model_copy(update={"require_options": False})
    result = apply_liquidity_filter(
        [make(avg_volume=9_000_000, last_price=50.0, has_options=False)], lax
    )
    assert len(result.kept) == 1


# ── watchlist exemption ─────────────────────────────────────────────────────


def test_watchlist_names_are_exempt_from_every_gate(liquidity):
    """D14: an explicit human override. If you put it there, you want it scored."""
    illiquid = make("TINY", in_watchlist=True, avg_volume=1_000, last_price=0.4, has_options=False)
    assert [c.symbol for c in apply_liquidity_filter([illiquid], liquidity).kept] == ["TINY"]


def test_the_exemption_is_configurable(liquidity):
    strict = liquidity.model_copy(update={"exempt_watchlist": False})
    illiquid = make("TINY", in_watchlist=True, avg_volume=1_000, last_price=0.4)
    assert apply_liquidity_filter([illiquid], strict).kept == []


def test_rejection_reasons_are_counted_for_the_job_run_record(liquidity):
    result = apply_liquidity_filter(
        [
            make("A", avg_volume=900_000, last_price=50.0),
            make("B", avg_volume=800_000, last_price=50.0),
            make("C", avg_volume=None),
            make("D", avg_volume=9_000_000, last_price=50.0),
        ],
        liquidity,
    )
    assert result.reasons == {"avg volume below minimum": 2, "no volume data": 1}
    assert [c.symbol for c in result.kept] == ["D"]


# ══════════════════════════════════════════════════════════════════════════
# Average volume over the configured window
# ══════════════════════════════════════════════════════════════════════════


def test_average_volume_uses_exactly_the_lookback_window():
    bars = [{"volume": 1_000_000}] * 20 + [{"volume": 99_000_000}] * 30
    assert average_volume(bars, 20) == 1_000_000


def test_average_volume_needs_a_full_window():
    """Six sessions says nothing about whether we can exit a position."""
    assert average_volume([{"volume": 5_000_000}] * 6, 20) is None


def test_average_volume_of_an_empty_series_is_none():
    assert average_volume([], 20) is None


def test_average_volume_ignores_malformed_bars():
    bars = [{"volume": None}] + [{"volume": 2_000_000}] * 19
    assert average_volume(bars, 20) is None  # 19 usable < 20 required


# ══════════════════════════════════════════════════════════════════════════
# The max-size guard
# ══════════════════════════════════════════════════════════════════════════


def test_an_implausibly_large_universe_is_refused():
    with pytest.raises(UniverseError, match="max_universe_size"):
        check_universe_size([make(f"S{i}") for i in range(5000)], 600)


def test_an_empty_universe_is_refused():
    """Writing it would deactivate every symbol we have."""
    with pytest.raises(UniverseError, match="refusing to deactivate everything"):
        check_universe_size([], 600)


def test_a_normal_universe_passes():
    check_universe_size([make(f"S{i}") for i in range(500)], 600)


def test_the_shipped_universe_size_fits_the_guard():
    assert config.universe().maintenance.max_universe_size >= 519


# ══════════════════════════════════════════════════════════════════════════
# Two-pass enrichment
# ══════════════════════════════════════════════════════════════════════════


def test_enrichment_attaches_price_exchange_market_cap_and_volume():
    client = StubFMP()
    enriched = {
        c.symbol: c
        for c in enrich_with_liquidity(
            client, merge_constituents(fx.SP500, fx.NASDAQ100, []), lookback_days=20
        )
    }
    assert enriched["AAPL"].avg_volume == 52_000_000  # pass 2
    assert enriched["AAPL"].last_price == 232.5  # pass 1
    assert enriched["AAPL"].exchange == "NASDAQ"  # pass 1
    assert enriched["AAPL"].market_cap == 3_500_000_000_000
    assert enriched["BRK.B"].avg_volume == 3_900_000


def test_pass_one_batches_its_quote_calls():
    """Price/exchange/market cap for the whole universe in a handful of calls."""
    client = StubFMP()
    enrich_with_liquidity(client, [make(f"S{i}") for i in range(250)], lookback_days=20)
    assert [len(b) for b in client.quote_batches] == [100, 100, 50]


def test_pass_two_is_one_call_per_symbol():
    """FMP's own avgVolume uses an undocumented window; D14 specifies 20 sessions."""
    client = StubFMP()
    candidates = merge_constituents(fx.SP500, fx.NASDAQ100, [])
    enrich_with_liquidity(client, candidates, lookback_days=20)
    assert sorted(client.bar_calls) == sorted(c.symbol for c in candidates)


def test_one_failing_symbol_does_not_kill_the_run():
    """550 symbols, one bad response: the universe still gets built."""
    client = StubFMP(fail_for={"MSFT"})
    seen: list[str] = []
    enriched = {
        c.symbol: c
        for c in enrich_with_liquidity(
            client,
            merge_constituents(fx.SP500, fx.NASDAQ100, []),
            lookback_days=20,
            on_error=lambda s, _e: seen.append(s),
        )
    }
    assert seen == ["MSFT"]
    assert enriched["MSFT"].avg_volume is None  # and so it gets filtered out
    assert enriched["AAPL"].avg_volume == 52_000_000


def test_the_call_budget_caps_the_expensive_pass():
    """D29: the free-tier daily budget must bound the per-symbol crawl."""
    client = StubFMP()
    candidates = merge_constituents(fx.SP500, fx.NASDAQ100, [])
    enriched = enrich_with_liquidity(client, candidates, lookback_days=20, call_budget=2)
    assert len(client.bar_calls) == 2
    # Symbols beyond the budget keep avg_volume=None, so the filter rejects
    # them rather than admitting them unverified.
    assert sum(1 for c in enriched if c.avg_volume is None) == len(candidates) - 2


def test_a_budget_exhausted_run_still_filters_safely(liquidity):
    client = StubFMP()
    candidates = merge_constituents(fx.SP500, fx.NASDAQ100, [])
    enriched = enrich_with_liquidity(client, candidates, lookback_days=20, call_budget=2)
    result = apply_liquidity_filter(enriched, liquidity)
    assert all(c.avg_volume is not None for c in result.kept)


# ══════════════════════════════════════════════════════════════════════════
# End to end, through the stub
# ══════════════════════════════════════════════════════════════════════════


def test_the_full_pipeline_keeps_the_liquid_names_and_drops_the_rest(liquidity):
    client = StubFMP()
    enriched = enrich_with_liquidity(
        client, merge_constituents(fx.SP500, fx.NASDAQ100, []), lookback_days=20
    )
    result = apply_liquidity_filter(enriched, liquidity)
    assert [c.symbol for c in result.kept] == ["AAPL", "BRK.B", "JNJ", "MSFT", "ZS"]
    assert result.reasons == {"avg volume below minimum": 1}  # NWSA at 900k


# ══════════════════════════════════════════════════════════════════════════
# The D29 budget must EXPAND coverage, not churn it
# ══════════════════════════════════════════════════════════════════════════


def test_known_volumes_seed_the_candidates():
    """A symbol verified on a previous run keeps its volume without a call."""
    client = StubFMP()
    candidates = [make("AAPL"), make("MSFT")]
    enriched = {
        c.symbol: c
        for c in enrich_with_liquidity(
            client, candidates, lookback_days=20, call_budget=0, known_volumes={"AAPL": 9_000_000}
        )
    }
    assert client.bar_calls == []  # budget was zero
    assert enriched["AAPL"].avg_volume == 9_000_000  # seeded from `tickers`
    assert enriched["MSFT"].avg_volume is None  # never verified


def test_the_budgeted_pass_visits_unknown_symbols_first():
    """A tight budget should buy NEW coverage, not re-verify known names."""
    client = StubFMP()
    candidates = [make("AAPL"), make("MSFT"), make("JNJ")]
    enrich_with_liquidity(
        client,
        candidates,
        lookback_days=20,
        call_budget=1,
        known_volumes={"AAPL": 9_000_000, "MSFT": 8_000_000},
    )
    assert client.bar_calls == ["JNJ"]  # the only unknown


def test_coverage_expands_across_successive_runs():
    """The property that stops the free-tier universe oscillating.

    Without seeding, run 2 would verify a different subset and reject the names
    run 1 had verified — the universe would flip-flop rather than converge.
    """
    client = StubFMP()
    candidates = merge_constituents(fx.SP500, fx.NASDAQ100, [])
    known: dict[str, int] = {}

    for _ in range(4):
        enriched = enrich_with_liquidity(
            client, candidates, lookback_days=20, call_budget=2, known_volumes=known
        )
        known = {c.symbol: c.avg_volume for c in enriched if c.avg_volume is not None}

    # Four runs x 2 calls covers all six fixture symbols.
    assert len(known) == len(candidates)


def test_a_thin_pull_does_not_erase_a_known_volume():
    """A short response is not evidence the name became illiquid."""
    client = StubFMP(volumes={})  # every daily_bars returns []
    enriched = enrich_with_liquidity(
        client, [make("AAPL")], lookback_days=20, known_volumes={"AAPL": 9_000_000}
    )
    assert enriched[0].avg_volume == 9_000_000


def test_a_fresh_pull_overwrites_a_stale_known_volume():
    client = StubFMP(volumes={"AAPL": 52_000_000})
    enriched = enrich_with_liquidity(
        client, [make("AAPL")], lookback_days=20, known_volumes={"AAPL": 1_000}
    )
    assert enriched[0].avg_volume == 52_000_000


def test_unverified_symbols_are_still_never_admitted(liquidity):
    """Seeding must not become a loophole: unknown still means rejected."""
    client = StubFMP()
    enriched = enrich_with_liquidity(
        client, [make("AAPL"), make("UNKNOWN")], lookback_days=20, call_budget=0
    )
    result = apply_liquidity_filter(enriched, liquidity)
    assert result.kept == []
    assert result.reasons == {"no volume data": 2}
