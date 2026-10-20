-- Record WHEN a symbol's average volume was last verified (D29).
--
-- `updated_at` cannot serve: its trigger fires on any column change, so a
-- membership-flag edit would make a year-old volume look freshly checked. The
-- budgeted enrichment pass in ingest/universe.py orders its leftover calls by
-- this column, oldest first, so a converged universe keeps its liquidity data
-- from going stale instead of letting the budget idle.
--
-- NULL means "never verified, or verified before this column existed" and
-- sorts first, which is the behaviour we want.

ALTER TABLE tickers ADD COLUMN IF NOT EXISTS avg_volume_asof DATE;

CREATE INDEX IF NOT EXISTS ix_tickers_volume_staleness
    ON tickers (avg_volume_asof NULLS FIRST)
    WHERE is_active;
