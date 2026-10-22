-- D45: the regime a call was made in, stamped on the call.
--
-- The step 10 backtest measured `post_earnings_drift` at +1.12R in
-- bear_volatile (54 closed, PF 7.2) against -0.08R in bull_calm (156 closed).
-- The blended +0.23R describes no market that will ever occur again in that
-- mixture, so D45 rules that the ladder (D40) is evaluated per recipe x
-- REGIME — a strategy can be LIVE in one regime and SHADOW in another, and
-- D39(2)'s 30-closed-idea floor applies to each cell rather than to the recipe
-- as a whole.
--
-- STAMPED AT CALL TIME, not derived later. The regime is a function of the
-- benchmark series, and that series gets revised: a late print, a split
-- backfill or a repaired basis all change what a 200-day MA said about a day
-- two years ago. Recomputing the regime for an old call would silently
-- reassign it between cells and move the evidence under a promotion decision
-- that had already been made. Same reasoning as `gates_in_force` (D42).
--
-- NULLABLE on purpose: the benchmark needs 200 sessions before a regime
-- exists at all, and `regime.RegimeSeries.tag` returns `unknown` rather than
-- guessing. A call made before that is honestly unclassified, and a
-- placeholder would be a claim.

ALTER TABLE shadow_book
    ADD COLUMN IF NOT EXISTS regime_at_call TEXT;

COMMENT ON COLUMN shadow_book.regime_at_call IS
    'Market regime when the call was made (D45): <trend>_<volatility> from '
    'D17''s SPY-vs-200MA test and SPY realised-vol terciles, or `unknown` '
    'before the benchmark has 200 sessions. Stamped at call time because the '
    'benchmark series gets revised and a recomputed regime would move the '
    'evidence under a promotion decision already made.';

-- The promotion query's working set: closed ideas grouped per recipe x regime.
CREATE INDEX IF NOT EXISTS ix_shadow_book_recipe_regime
    ON shadow_book (recipe, regime_at_call, outcome);
