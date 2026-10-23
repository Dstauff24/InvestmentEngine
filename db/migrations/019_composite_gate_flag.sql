-- D53: what the COMPOSITE alone said, on every call.
--
-- Until O18 shows the composite carries information about recipe outcomes, a
-- certified recipe's plan surfaces on its own R:R gate and the composite only
-- decides SIZE. That change would destroy the evidence it depends on unless
-- every row records what the old gate would have done — so it does.
--
-- `composite_tier` is what the composite alone reached: 'A', 'B', or NULL for
-- no tier at all. `composite_gate_passed` is the same fact as the boolean D53
-- poses, and the redundancy is deliberate: the question is "split expectancy
-- on whether the composite gate would have passed", and a reader should not
-- have to know that NULL in another column meant refused.
--
-- **THIS IS THE MEASUREMENT, not bookkeeping.** After N closed calls,
-- expectancy split on `composite_gate_passed` IS the answer to whether the
-- composite adds anything to a recipe signal — and expectancy split on
-- `composite_tier = 'A'` is the answer to whether the Tier A upgrade is worth
-- the extra size. Neither question can be asked of a book that did not record
-- the flag, which is why the flag ships WITH the change rather than after it.

ALTER TABLE shadow_book
    ADD COLUMN IF NOT EXISTS composite_tier        TEXT,
    ADD COLUMN IF NOT EXISTS composite_gate_passed BOOLEAN;

ALTER TABLE shadow_book DROP CONSTRAINT IF EXISTS shadow_book_composite_tier_check;
ALTER TABLE shadow_book
    ADD CONSTRAINT shadow_book_composite_tier_check
    CHECK (composite_tier IS NULL OR composite_tier IN ('A', 'B'));

COMMENT ON COLUMN shadow_book.composite_tier IS
    'D53: what the COMPOSITE alone reached (A/B/NULL), independent of whether '
    'the plan surfaced. With composite_gate_passed, this is what lets the '
    'forward record answer O18 — does the composite add anything to a recipe '
    'signal — rather than the question being argued.';
