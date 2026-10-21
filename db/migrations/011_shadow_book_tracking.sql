-- Step 8: what the Shadow Book needs to log an idea and track it to outcome.
--
-- Two groups of columns, for two different reasons.
--
-- PROVENANCE (D40/D41/D42 + the scorer identity). The projections are already
-- frozen at call time; these record the MACHINERY that produced them, so a
-- later calibration query can group by it:
--
--   sentiment_scorer   which tone scorer scored Pillar D. A lexicon ships now
--                      and a Claude scorer arrives in Phase 2; they are
--                      differently calibrated and averaging them measures
--                      neither (the same reason news_articles carries this).
--   gates_in_force     the tier gates, weights and digest cap as they stood.
--                      D42 stores these on the daily aggregate for exactly
--                      this reason and an idea needs it too: a threshold
--                      change must not silently reinterpret yesterday's calls.
--
-- TRACKING. `proj_stop` is frozen — that is the whole point of a projection —
-- so the stop the tracker actually enforces needs somewhere else to live:
--
--   current_stop            moves to breakeven at TP1 per the trim rules, and
--                           is dividend-adjusted per D19(2).
--   stop_to_breakeven_on    when that happened. D18 zeroes the open heat here,
--                           so the date is what the heat cap reads.
--   last_evaluated_on       the tracker is idempotent on it: re-running a day
--                           must not double-count an excursion.

ALTER TABLE shadow_book
    ADD COLUMN IF NOT EXISTS sentiment_scorer     TEXT,
    ADD COLUMN IF NOT EXISTS gates_in_force       JSONB,
    ADD COLUMN IF NOT EXISTS current_stop         NUMERIC(18, 6),
    ADD COLUMN IF NOT EXISTS stop_to_breakeven_on DATE,
    ADD COLUMN IF NOT EXISTS last_evaluated_on    DATE;

-- The tracker's working set: open ideas it has not yet seen today.
CREATE INDEX IF NOT EXISTS ix_shadow_book_tracking
    ON shadow_book (outcome, last_evaluated_on) WHERE outcome = 'open';

COMMENT ON COLUMN shadow_book.current_stop IS
    'The stop the tracker enforces NOW. `proj_stop` is frozen at call time; '
    'this moves to breakeven at TP1 per exits.swing.move_stop_to_breakeven_at '
    'and is dividend-adjusted per D19(2).';
COMMENT ON COLUMN shadow_book.gates_in_force IS
    'Tier gates, pillar weights and the digest cap as they stood when this '
    'idea was called. A later threshold change must not silently reinterpret '
    'it — the same reason daily_scoring_stats carries them (D42).';
