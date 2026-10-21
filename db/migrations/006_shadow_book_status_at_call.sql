-- D40: the Shadow Book records the strategy's status AT TIME OF CALL.
--
-- Without this stamp, expectancy measured across a promotion boundary silently
-- averages two different strategies: the SHADOW ideas a recipe produced while
-- it was still being judged, and the PAPER or LIVE ideas it produced after. The
-- promotion decision is made FROM that expectancy, so contaminating it makes
-- the ladder circular — and the mistake is undetectable after the fact, because
-- nothing in the row says which regime it belonged to.
--
-- Nullable with no default: rows written before today genuinely do not know,
-- and back-filling them with a guess would be worse than admitting it. Every
-- row the shadow_book logger writes from now on sets it.

ALTER TABLE shadow_book
    ADD COLUMN IF NOT EXISTS status_at_call TEXT
        CHECK (status_at_call IS NULL OR status_at_call IN ('shadow', 'paper', 'live'));

CREATE INDEX IF NOT EXISTS ix_shadow_book_status_recipe
    ON shadow_book (recipe, status_at_call, called_on DESC);

COMMENT ON COLUMN shadow_book.status_at_call IS
    'D40 promotion-ladder status of the recipe when this idea was called. '
    'Expectancy must be computed per (recipe, status_at_call) — mixing regimes '
    'averages two different strategies and makes the promotion rule circular. '
    'NULL on rows predating 2026-10-21.';
