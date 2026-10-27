-- O17: `core` joins D17's bucket vocabulary.
--
-- D17's five buckets — etf / hold / swing / option / cash — describe the GROWTH
-- sleeve's risk categories, and the heat cap counts `swing` + `option` only.
-- They have no name for the thing the core layer actually holds, and D35 is
-- explicit that the two sleeves keep separate scoreboards: calling the S&P 500
-- index fund an `etf` would file the primary product under a growth-sleeve
-- bucket, and it is not even an ETF (SWPPX is a mutual fund, bought in dollars
-- at NAV).
--
-- ADDING A VALUE CHANGES NO CAP. Heat counts swing and option; `core` is
-- excluded from heat for the same reason holds and ETFs are (D17: they run on
-- thesis triggers with no hard stops), and a named test pins that.
--
-- The sync stamps this bucket for `accounts.core_symbol` and for nothing else:
-- every other position's bucket stays NULL for the operator, because a bucket
-- is a statement of INTENT and the broker has no idea which.

ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_bucket_check;

ALTER TABLE positions
    ADD CONSTRAINT positions_bucket_check
    CHECK (bucket IS NULL OR bucket IN ('core', 'etf', 'hold', 'swing', 'option', 'cash'));

COMMENT ON COLUMN positions.bucket IS
    'D17''s bucket, plus O17''s `core`. The sync stamps `core` for '
    '`accounts.core_symbol` and leaves every other position NULL, because a '
    'bucket is a statement of intent the broker cannot know. Heat counts '
    '`swing` + `option` only, so `core` is outside the cap by construction.';
