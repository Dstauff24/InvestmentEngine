"""The one-time deep-history backfill (O7).

What matters here is not the HTTP plumbing — that is `fmp_client`'s — but the
two rules that keep five years of borrowed history honest: the seam never
falls inside a session, and the two providers' disagreement is measured rather
than merged.
"""

from __future__ import annotations

from datetime import date

import pytest

from core import clock
from ingest import fmp_history


def row(day: str, close: float, **kw) -> dict:
    base = {
        "date": day,
        "open": close - 1,
        "high": close + 1,
        "low": close - 2,
        "close": close,
        "volume": 1_000_000,
    }
    base.update(kw)
    return base


# ── parsing ─────────────────────────────────────────────────────────────────


def test_bars_parse_oldest_first_whatever_order_they_arrive_in():
    """FMP returns newest first; `prices_daily` is read oldest first."""
    rows = fmp_history.parse_bars([row("2024-03-05", 12), row("2024-03-04", 11)], "AAPL")
    assert [r["session_date"] for r in rows] == [date(2024, 3, 4), date(2024, 3, 5)]


def test_a_bar_lands_on_the_same_instant_as_a_schwab_bar_for_that_session():
    """Dual time keying: one session, one `ts`, whoever reported it."""
    parsed = fmp_history.parse_bars([row("2024-03-04", 11)], "AAPL")[0]
    assert parsed["ts"] == clock.session_close_utc(date(2024, 3, 4))
    assert parsed["session_date"] == date(2024, 3, 4)


def test_a_row_with_no_date_or_no_close_is_dropped_not_guessed():
    rows = fmp_history.parse_bars(
        [row("2024-03-04", 11), {"date": None, "close": 5}, {"date": "2024-03-05"}],
        "AAPL",
    )
    assert len(rows) == 1


def test_a_malformed_date_is_dropped_rather_than_raising():
    """One bad row in a five-year response must not cost the other 1,253."""
    assert fmp_history.parse_bars([row("not-a-date", 11), row("2024-03-04", 11)], "AAPL") == [
        fmp_history.parse_bars([row("2024-03-04", 11)], "AAPL")[0]
    ]


def test_volume_defaults_to_zero_rather_than_null():
    parsed = fmp_history.parse_bars([row("2024-03-04", 11, volume=None)], "AAPL")[0]
    assert parsed["volume"] == 0


# ── the seam ────────────────────────────────────────────────────────────────


def test_the_raw_endpoints_adj_prefixed_fields_are_read_as_prices():
    """`non-split-adjusted` returns `adjClose` and NO `close`, carrying the
    UNADJUSTED price. The name is not the basis; the endpoint is."""
    rows = fmp_history.parse_bars(
        [
            {
                "date": "2024-10-03",
                "adjOpen": 79.93,
                "adjHigh": 82.12,
                "adjLow": 79.88,
                "adjClose": 81.11,
                "volume": 6_725_809,
                "close": None,
            }
        ],
        "LRCX",
    )
    assert rows[0]["close"] == pytest.approx(81.11)
    assert rows[0]["open"] == pytest.approx(79.93)
    assert rows[0]["high"] == pytest.approx(82.12)
    assert rows[0]["low"] == pytest.approx(79.88)


def test_the_overlap_is_measured_against_another_providers_bar():
    parsed = fmp_history.parse_bars([row("2024-03-04", 100.0), row("2024-03-05", 200.0)], "AAPL")
    stored = {date(2024, 3, 4): (100.5, "schwab"), date(2024, 3, 5): (200.0, "schwab")}
    overlap = fmp_history.Overlap()

    fmp_history.compare_overlap(parsed, stored, overlap)

    assert overlap.sessions == 2
    assert overlap.max_pct == pytest.approx(0.4975, abs=1e-3)
    assert overlap.median_pct == pytest.approx(0.2487, abs=1e-3)


def test_our_own_earlier_rows_are_not_compared_against_themselves():
    """Tens of thousands of exact zeros would bury a real disagreement."""
    parsed = fmp_history.parse_bars([row("2024-03-04", 100.0)], "AAPL")
    overlap = fmp_history.Overlap()
    fmp_history.compare_overlap(parsed, {date(2024, 3, 4): (100.0, "fmp")}, overlap)
    assert overlap.sessions == 0


def test_a_session_only_one_provider_has_is_not_a_disagreement():
    parsed = fmp_history.parse_bars([row("2024-03-04", 100.0)], "AAPL")
    overlap = fmp_history.Overlap()
    fmp_history.compare_overlap(parsed, {}, overlap)
    assert overlap.sessions == 0
    assert overlap.median_pct is None
    assert "no overlapping sessions" in str(overlap)


def test_a_zero_stored_close_is_skipped_rather_than_dividing_by_it():
    parsed = fmp_history.parse_bars([row("2024-03-04", 100.0)], "AAPL")
    overlap = fmp_history.Overlap()
    fmp_history.compare_overlap(parsed, {date(2024, 3, 4): (0.0, "schwab")}, overlap)
    assert overlap.sessions == 0


def test_the_writer_never_replaces_a_session_another_provider_already_wrote(monkeypatch):
    """THE SEAM RULE. A session keeps the provider that first wrote it."""
    written: list[dict] = []
    monkeypatch.setattr(
        fmp_history,
        "stored_bars",
        lambda s: {date(2025, 9, 15): (10.0, "schwab"), date(2025, 9, 12): (9.5, "schwab")},
    )
    monkeypatch.setattr(
        fmp_history.db, "executemany", lambda sql, rows: written.extend(rows) or len(rows)
    )

    class Client:
        def historical_bars_raw(self, symbol, *, start, end):
            return [row("2025-09-15", 10.1), row("2025-09-12", 9.0), row("2021-01-04", 5.0)]

    count = fmp_history.load_symbol_bars(
        Client(),
        "AAPL",
        start=date(2021, 1, 1),
        end=date(2026, 10, 21),
        overlap=fmp_history.Overlap(),
    )

    assert count == 1
    assert [r["session_date"] for r in written] == [date(2021, 1, 4)]


def test_a_session_this_job_wrote_itself_is_rewritten(monkeypatch):
    """What made a wrong first run correctable in place: the first pass stored
    split-ADJUSTED bars from the wrong endpoint, and a backfill that refused to
    touch its own rows could never have fixed them."""
    written: list[dict] = []
    monkeypatch.setattr(fmp_history, "stored_bars", lambda s: {date(2021, 1, 4): (50.0, "fmp")})
    monkeypatch.setattr(
        fmp_history.db, "executemany", lambda sql, rows: written.extend(rows) or len(rows)
    )

    class Client:
        def historical_bars_raw(self, symbol, *, start, end):
            return [row("2021-01-04", 5.0)]

    assert (
        fmp_history.load_symbol_bars(
            Client(),
            "AAPL",
            start=date(2021, 1, 1),
            end=date(2026, 10, 21),
            overlap=fmp_history.Overlap(),
        )
        == 1
    )
    assert written[0]["close"] == pytest.approx(5.0)


def test_a_symbol_with_no_stored_bars_takes_the_whole_window(monkeypatch):
    """A benchmark added today has no seam to respect."""
    written: list[dict] = []
    monkeypatch.setattr(fmp_history, "stored_bars", lambda s: {})
    monkeypatch.setattr(
        fmp_history.db, "executemany", lambda sql, rows: written.extend(rows) or len(rows)
    )

    class Client:
        def historical_bars_raw(self, symbol, *, start, end):
            return [row("2021-01-04", 5.0), row("2026-10-21", 9.0)]

    assert (
        fmp_history.load_symbol_bars(
            Client(),
            "SPY",
            start=date(2021, 1, 1),
            end=date(2026, 10, 21),
            overlap=fmp_history.Overlap(),
        )
        == 2
    )
    assert len(written) == 2


def test_a_provider_returning_nothing_writes_nothing_and_says_so(monkeypatch, caplog):
    monkeypatch.setattr(fmp_history, "stored_bars", lambda s: {})

    class Client:
        def historical_bars_raw(self, symbol, *, start, end):
            return []

    with caplog.at_level("WARNING"):
        assert (
            fmp_history.load_symbol_bars(
                Client(),
                "AAPL",
                start=date(2021, 1, 1),
                end=date(2026, 10, 21),
                overlap=fmp_history.Overlap(),
            )
            == 0
        )
    assert "no bars" in caplog.text


# ── the earnings window walk ────────────────────────────────────────────────


def test_the_earnings_backfill_asks_per_symbol_at_the_requested_depth(monkeypatch):
    """The windowed calendar walk this replaced returned an arbitrary slice of
    the days it was asked for, with nothing in the payload to say so."""
    asked: list[tuple[str, int]] = []
    monkeypatch.setattr(fmp_history.db, "active_symbols", lambda: ["AAPL", "MSFT"])
    monkeypatch.setattr(fmp_history.db, "executemany", lambda sql, rows: len(rows))

    class Client:
        def symbol_earnings(self, symbol, *, limit):
            asked.append((symbol, limit))
            return [{"symbol": symbol, "date": "2024-02-01", "epsActual": 1.0}]

    with monkeypatch.context() as m:
        m.setattr(fmp_history, "job_run", _fake_job_run)
        written = fmp_history.backfill_earnings(Client(), years=5, as_of=date(2026, 10, 21))

    assert written == 2
    assert asked == [("AAPL", 22), ("MSFT", 22)], "5 years of quarters, plus a margin"


class _FakeJob:
    """Just enough of `JobRun` for `for_each_symbol` to report into."""

    def note(self, **kw):
        pass

    def add_rows(self, n):
        pass

    def symbol_ok(self):
        pass

    def symbol_failed(self):
        pass


def _fake_job_run(*args, **kwargs):
    import contextlib

    @contextlib.contextmanager
    def ctx():
        yield _FakeJob()

    return ctx()
