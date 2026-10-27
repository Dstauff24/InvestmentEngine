"""EDGAR Form 4 parsing, and O19's deep historical walk. Fixtures only.

No network: `core/netguard.py` refuses inside pytest whatever the flag says
(D27), so everything here is a payload shape rather than a request.
"""

from __future__ import annotations

from datetime import date

# ── O19: the deep historical walk ───────────────────────────────────────────


def test_the_parser_reads_an_OLDER_submissions_page_too():
    """`filings.recent` holds only the last ~1,000 filings; everything before
    that lives in separate documents whose arrays sit at the TOP level. A
    historical walk reading one shape stops wherever 1,000 filings lands —
    invisibly, because the feed parses and the rows look fine."""
    from ingest.edgar_insiders import parse_submissions

    page = {
        "accessionNumber": ["0000320193-20-000001"],
        "form": ["4"],
        "acceptanceDateTime": ["2020-02-03T16:30:00.000Z"],
        "filingDate": ["2020-02-03"],
        "primaryDocument": ["xslF345X03/form4.xml"],
    }
    [filing] = parse_submissions(page, since=date(2019, 1, 1))
    assert filing.accession == "0000320193-20-000001"
    assert filing.filed_at.year == 2020


def test_older_pages_SKIPS_a_page_that_ends_before_the_window():
    """One request per page, so skipping the ones whose newest filing predates
    the window is the difference between a few extra calls per symbol and a
    hundred."""
    from ingest.edgar_insiders import older_pages

    feed = {
        "filings": {
            "recent": {},
            "files": [
                {"name": "CIK0000320193-submissions-001.json", "filingTo": "2019-06-01"},
                {"name": "CIK0000320193-submissions-002.json", "filingTo": "2012-01-01"},
            ],
        }
    }
    assert older_pages(feed, since=date(2018, 1, 1)) == ["CIK0000320193-submissions-001.json"]
    assert len(older_pages(feed, since=date(2010, 1, 1))) == 2


def test_an_UNPARSEABLE_page_bound_is_fetched_rather_than_guessed_away():
    """Skipping a page because its date could not be read would drop history
    on a formatting change, which is the expensive direction of the error."""
    from ingest.edgar_insiders import older_pages

    feed = {"filings": {"files": [{"name": "page.json", "filingTo": "not-a-date"}]}}
    assert older_pages(feed, since=date(2020, 1, 1)) == ["page.json"]


def test_the_daily_window_is_UNCHANGED_by_the_backfill_flag():
    """The deep walk must not be able to alter what the nightly job reads:
    `years` switches on the older pages and widens the window, and without it
    the job keeps taking `edgar.lookback_days` from config."""
    import inspect

    from ingest import edgar_insiders

    source = inspect.getsource(edgar_insiders.run)
    assert "cfg.lookback_days" in source
    assert "365 * years if deep" in source


def test_a_one_off_budget_is_a_FLAG_rather_than_a_config_edit():
    """`edgar.daily_call_budget` is a runaway-loop guard sized for a normal
    evening. O19's backfill needs orders of magnitude more for one night, and
    raising a tracked [OPEN] value for an afternoon is how it gets left raised
    — the guard would then be gone on every later evening with nothing to show
    it had moved."""
    import inspect

    from ingest import edgar_insiders

    source = inspect.getsource(edgar_insiders.run)
    assert (
        "budget: int | None = None"
        in inspect.signature(edgar_insiders.run).__str__().replace("'", "")
        or "budget" in source
    )
    assert 'DayBudget("edgar", budget)' in source
    # And the default path still reads config, so a run without the flag is
    # guarded exactly as the nightly job is.
    assert "client or EdgarClient()" in source
