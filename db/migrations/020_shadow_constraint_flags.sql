-- D54: SHADOW MAXIMIZES EVIDENCE.
--
-- Every plan from a CERTIFIED recipe that passes its RECIPE gates is logged,
-- regardless of `ranking.max_ideas_per_day` and regardless of D17/D18/D38's
-- risk caps — a shadow call costs nothing and a day not recorded cannot be
-- recovered, so the digest's slot limit and the capital constraints have no
-- business deciding what the engine LEARNS from. They decide what it would
-- TRADE, which is a different question and binds from PAPER (D40).
--
-- So a row must say which of them WOULD have refused it, or the book becomes a
-- pile of calls nobody can tell apart: "the engine would have taken this" and
-- "the engine recorded this but its sector was full" are different claims
-- about the same plan, and D13's expectancy means different things over the
-- two populations.
--
-- `would_refuse` is the list, EMPTY (not NULL) when nothing would have refused
-- it — so "actionable" is `cardinality(would_refuse) = 0` and a NULL can only
-- mean a row written before this migration. The vocabulary is fixed in
-- `decision.idea_builder.CONSTRAINTS` and pinned by a test rather than a CHECK,
-- because a CHECK on array elements cannot be read back by the code that has
-- to render them.
--
-- `slot_rank` is where the idea sat in the day's walk (1-based, by composite).
-- Beyond `max_ideas_per_day` it is the reason the idea was not actionable, and
-- it is also what O21's replay needs to reconstruct the queue it is measuring.

ALTER TABLE shadow_book
    ADD COLUMN IF NOT EXISTS would_refuse TEXT[],
    ADD COLUMN IF NOT EXISTS slot_rank    INT;

COMMENT ON COLUMN shadow_book.would_refuse IS
    'D54: which constraints WOULD have refused this call — tier_b_budget, heat, '
    'sector, concurrency, slot_rank. Empty array = actionable; NULL = row '
    'predates migration 020. Shadow logs it anyway, because the caps decide '
    'what would be TRADED and not what the engine learns from.';

COMMENT ON COLUMN shadow_book.slot_rank IS
    'D54: 1-based position in the day''s composite ranking at the time of the '
    'call. Beyond ranking.max_ideas_per_day the call is recorded and not '
    'actionable; O21''s replay reads this to rebuild the queue.';

CREATE INDEX IF NOT EXISTS ix_shadow_book_actionable
    ON shadow_book ((cardinality(would_refuse)))
    WHERE would_refuse IS NOT NULL;
