"""Company news for the universe (D5) -> `news_articles`.

    ALLOW_LIVE_CALLS=1 python -m ingest.finnhub_news

Pillar D input. Nothing here classifies or scores: sentiment is Phase 2 (Claude
API for tone, per D6), and the `sentiment_*` columns stay NULL until then. This
job's only responsibility is to accumulate the corpus STARTING NOW, so the
sentiment pillar has history to work with the day it is switched on — the same
logic as D7, applied to text.

Upserted by provider article id, so re-running the rolling window never
duplicates. One article tagged to three tickers stores three rows, because the
sentiment pillar scores per symbol.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, date, datetime, timedelta

from core import clock, config, db
from core.jobs import job_run
from ingest.finnhub_client import FinnhubClient
from ingest.runner import for_each_symbol

log = logging.getLogger(__name__)

JOB_NAME = "finnhub_news"

UPSERT = """
    INSERT INTO news_articles (
        article_id, source_provider, symbol, published_at, headline, summary,
        url, publisher, category, image_url
    ) VALUES (
        %(article_id)s, 'finnhub', %(symbol)s, %(published_at)s, %(headline)s,
        %(summary)s, %(url)s, %(publisher)s, %(category)s, %(image_url)s
    )
    ON CONFLICT (source_provider, article_id, symbol) DO UPDATE SET
        headline    = EXCLUDED.headline,
        summary     = EXCLUDED.summary,
        url         = EXCLUDED.url,
        publisher   = EXCLUDED.publisher,
        category    = EXCLUDED.category,
        image_url   = EXCLUDED.image_url,
        ingested_at = now()
"""


def parse_articles(rows: list[dict], symbol: str) -> list[dict]:
    """Map Finnhub `/company-news` rows to `news_articles` params.

    Finnhub's `datetime` is a Unix epoch in SECONDS; the column is TIMESTAMPTZ
    in UTC. An article without an id or a timestamp is dropped — both are needed
    for the upsert key and for any time-windowed sentiment delta.
    """
    parsed: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        article_id = row.get("id")
        epoch = row.get("datetime")
        if article_id in (None, "") or not epoch:
            continue
        key = str(article_id)
        if key in seen:  # Finnhub occasionally repeats a story inside one window
            continue
        seen.add(key)
        try:
            published_at = datetime.fromtimestamp(int(epoch), tz=UTC)
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        parsed.append(
            {
                "article_id": key,
                "symbol": symbol,
                "published_at": published_at,
                "headline": row.get("headline") or None,
                "summary": row.get("summary") or None,
                "url": row.get("url") or None,
                "publisher": row.get("source") or None,
                "category": row.get("category") or None,
                "image_url": row.get("image") or None,
            }
        )
    return parsed


def load_symbol(client: FinnhubClient, symbol: str, *, start: date, end: date) -> int:
    rows = parse_articles(client.company_news(symbol, start, end), symbol)
    return db.executemany(UPSERT, rows) if rows else 0


def run(client: FinnhubClient | None = None, *, symbols: list[str] | None = None) -> int:
    client = client or FinnhubClient()
    lookback = config.universe().news.lookback_days
    today = clock.session_date()
    start = today - timedelta(days=lookback)
    universe = symbols if symbols is not None else db.active_symbols()
    if not universe:
        raise RuntimeError("no active tickers; run `python -m ingest.universe` first")

    with job_run(JOB_NAME, session_date=today, triggered_by="manual") as job:
        outcome = for_each_symbol(
            universe,
            lambda s: load_symbol(client, s, start=start, end=today),
            run=job,
            label="news",
        )
        job.note(window=f"{start}..{today}", failed_symbols=sorted(outcome.failed)[:50])
    return outcome.rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--symbols", nargs="*", help="override the universe (debugging)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    print(f"news: {run(symbols=args.symbols)} article rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
