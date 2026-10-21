-- Which scorer produced a sentiment reading (D32 / Pillar D, step 4).
--
-- `news_articles` already carries `sentiment_score`, `sentiment_label`,
-- `materiality` and `classified_at` — but nothing saying WHAT scored it. A
-- lexicon scorer ships in Phase 1 and a Claude API scorer arrives in Phase 2,
-- and the two will disagree: differently calibrated, differently wrong.
--
-- Without this column the migration between them is silent. Expectancy and
-- D39's calibration curve would average readings from two different
-- instruments, and nothing in the row would say which one produced it — the
-- same mistake `shadow_book.status_at_call` exists to prevent for the
-- promotion ladder (D40), arriving by a different route.
--
-- Stored as "name/version" so a change to the lexicon ITSELF is visible, not
-- just a change of scorer. A reworded word list is a different instrument even
-- though the name is the same.
--
-- Nullable: the 17,536 rows already stored were never scored at all
-- (`classified_at` is NULL on every one), and inventing a scorer name for them
-- would be worse than admitting it.

ALTER TABLE news_articles
    ADD COLUMN IF NOT EXISTS sentiment_scorer TEXT;

CREATE INDEX IF NOT EXISTS ix_news_scorer
    ON news_articles (symbol, sentiment_scorer, published_at DESC);

COMMENT ON COLUMN news_articles.sentiment_scorer IS
    'Which scorer produced sentiment_score, as "name/version" (e.g. '
    '"lexicon/1" or "claude-opus-5/1"). Readings from different scorers must '
    'never be averaged together: they are differently calibrated instruments, '
    'and D39 calibration on a mixed series measures neither. NULL means the '
    'row predates scoring.';
