"""`features/sentiment.py`'s loading layer, against a real database.

    PGDATABASE=ie_test pytest -m integration

What a fixture cannot establish: that the window is bounded on `published_at`
rather than `ingested_at`, that an article tagged to several symbols is stored
and counted once PER SYMBOL, and that `news_coverage_start` reports when the
ENGINE began collecting rather than when a symbol first made news.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from core import db, settings
from features import sentiment as sent
from tests.dbguard import assert_safe_database

pytestmark = pytest.mark.integration

AS_OF = date(2026, 10, 21)
SYMBOLS = ("ZZNEWSA", "ZZNEWSB")


@pytest.fixture(scope="module", autouse=True)
def _guard_database():
    return assert_safe_database(settings.database_url())


@pytest.fixture(autouse=True)
def seeded():
    assert_safe_database(db.settings.database_url())
    _clean()
    db.executemany(
        "INSERT INTO tickers (symbol, is_active) VALUES (%s, TRUE) ON CONFLICT DO NOTHING",
        [(s,) for s in SYMBOLS],
    )
    yield
    _clean()


def _clean() -> None:
    db.execute("DELETE FROM news_articles WHERE symbol = ANY(%s)", (list(SYMBOLS),))
    db.execute("DELETE FROM tickers WHERE symbol = ANY(%s)", (list(SYMBOLS),))


def store(
    article_id: str,
    symbol: str,
    published: date,
    headline: str = "Beats estimates",
    *,
    ingested: datetime | None = None,
) -> None:
    db.execute(
        """
        INSERT INTO news_articles (article_id, source_provider, symbol, published_at,
                                   headline, summary, publisher, ingested_at)
        VALUES (%s, 'test', %s, %s, %s, '', 'test', %s)
        ON CONFLICT (source_provider, article_id, symbol) DO UPDATE
        SET headline = EXCLUDED.headline
        """,
        (
            article_id,
            symbol,
            datetime(published.year, published.month, published.day, 14, tzinfo=UTC),
            headline,
            ingested or datetime.now(UTC),
        ),
    )


def test_the_window_is_bounded_on_publication_not_ingestion():
    """An article is news when it is PUBLISHED; the ingest timestamp says when
    cron happened to notice, which is an artefact of our schedule."""
    store(
        "old-pub",
        SYMBOLS[0],
        AS_OF - timedelta(days=200),
        ingested=datetime(2026, 10, 21, 20, tzinfo=UTC),
    )

    rows = sent.load_articles(SYMBOLS[0], as_of=AS_OF, days=30)
    assert rows == [], "published 200 days ago, so outside a 30-day window"


def test_articles_inside_the_window_load_oldest_first():
    for i in range(1, 6):
        store(f"a{i}", SYMBOLS[0], AS_OF - timedelta(days=i))
    rows = sent.load_articles(SYMBOLS[0], as_of=AS_OF, days=30)
    assert len(rows) == 5
    assert [r["published_at"] for r in rows] == sorted(r["published_at"] for r in rows)


def test_an_article_tagged_to_two_symbols_counts_once_for_each():
    """THE storage shape: news_articles is keyed (provider, article_id, symbol),
    and an article about two companies really is news about both."""
    store("shared", SYMBOLS[0], AS_OF - timedelta(days=1))
    store("shared", SYMBOLS[1], AS_OF - timedelta(days=1))

    a = sent.load_articles(SYMBOLS[0], as_of=AS_OF, days=30)
    b = sent.load_articles(SYMBOLS[1], as_of=AS_OF, days=30)
    assert len(a) == len(b) == 1
    assert len(sent.dedupe_by_article(a + b)) == 1, (
        "within ONE symbol's list the same id collapses — which is why the "
        "dedupe is applied per symbol, never across a merged list"
    )


def test_a_re_ingest_does_not_duplicate_a_row():
    store("same", SYMBOLS[0], AS_OF - timedelta(days=1))
    store("same", SYMBOLS[0], AS_OF - timedelta(days=1))
    assert len(sent.load_articles(SYMBOLS[0], as_of=AS_OF, days=30)) == 1


def test_coverage_start_is_when_the_engine_began_not_when_a_symbol_first_appeared():
    """Global, not per symbol. A symbol's earliest article is a fact about the
    symbol; when we started watching is a fact about us, and it is the one that
    tells a data gap apart from a quiet week.

    Seeds its own rows rather than leaning on whatever the database happens to
    hold — the first version asserted against the dev database's 17k articles
    and failed on the scratch one, which is the right failure for the wrong
    reason.
    """
    earliest = AS_OF - timedelta(days=20)
    store("first-ever", SYMBOLS[0], earliest)
    assert sent.news_coverage_start() == earliest

    # A symbol that only starts being covered today must not move it.
    store("brand-new", SYMBOLS[1], AS_OF)
    assert sent.news_coverage_start() == earliest, (
        "a newly-covered symbol must not move the engine's coverage start"
    )


def test_compute_end_to_end_abstains_on_a_short_series():
    """Exactly the real situation on 2026-10-21: eight days of history against
    a 30-day baseline window."""
    for i in range(1, 8):
        store(f"t{i}", SYMBOLS[0], AS_OF - timedelta(days=i))

    rows = sent.load_articles(SYMBOLS[0], as_of=AS_OF)
    features = sent.compute(SYMBOLS[0], rows, as_of=AS_OF, coverage_start=AS_OF - timedelta(days=8))
    assert features.tone_delta is None, "no baseline window to compare against"
    assert features.volume_spike is None, "and no established normal"
    assert features.trailing.scored > 0, "but the trailing level IS known"
    assert features.scorer_id == "lexicon/1"


def test_the_scorer_column_exists_for_persisting_identity():
    """Phase 2 adds a second scorer; without this column the switch is silent
    and D39 calibration would average two instruments."""
    store("s1", SYMBOLS[0], AS_OF - timedelta(days=1))
    db.execute(
        "UPDATE news_articles SET sentiment_score = 0.5, sentiment_scorer = %s, "
        "classified_at = now() WHERE article_id = 's1'",
        ("lexicon/1",),
    )
    row = db.fetch_one(
        "SELECT sentiment_score, sentiment_scorer FROM news_articles WHERE article_id = 's1'"
    )
    assert float(row["sentiment_score"]) == pytest.approx(0.5)
    assert row["sentiment_scorer"] == "lexicon/1"
