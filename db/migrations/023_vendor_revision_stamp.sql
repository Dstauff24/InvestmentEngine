-- O23: the vendor's own revision stamp on an earnings row, captured from now on.
--
-- D48's caveat is that historical estimates are MUTABLE — one row carried
-- `lastUpdated` sixteen months after the report it belongs to — and the
-- positive control's pre-registered failure branch depends on being able to
-- separate revised rows from untouched ones. The field was never stored.
--
-- MEASURED LIVE 2026-10-27, BEFORE THIS WAS BUILT, and the result is why the
-- columns are shaped this way: `/stable/earnings` DOES publish `lastUpdated`,
-- and across 2,607 reported rows over 20 symbols **100% carry a stamp AFTER
-- their own report date**, only 3 of them within 30 days. The stamps come in
-- per-symbol BULK BLOCKS (MSFT: 157 of 165 rows stamped 2025-04-24), so the
-- vendor rewrites whole histories at once.
--
-- SO THE STAMP ALONE CANNOT ANSWER THE QUESTION RETROSPECTIVELY — on history it
-- excludes everything and discriminates nothing. What makes it evidence is
-- pairing it with WHEN WE SAW IT, which only accrues forward. Hence three
-- columns rather than one:
--
--   vendor_last_updated             the vendor's current stamp; latest wins.
--   vendor_last_updated_first_seen  the date WE first recorded a stamp for this
--                                   row, never overwritten. Fixes what the
--                                   stamp said at a knowable moment.
--   vendor_revisions                how many times we have OBSERVED the stamp
--                                   change. 0 means every pull since we started
--                                   watching agreed, which is the only form of
--                                   "not revised" the vendor permits us to
--                                   establish.
--
-- The forward diagnostic is then `vendor_revisions = 0 AND vendor_last_updated
-- <= event_date`: the row was already stamped at or before its report when we
-- first saw it, and has not moved since.
--
-- NOTHING IS ADDED TO `estimate_snapshots`, deliberately.
-- `/stable/analyst-estimates` publishes NO revision field (probed the same
-- day — the payload's 21 keys carry none), and `estimate_snapshots.snapshot_date`
-- is already the point-in-time record by construction: it is OUR capture date,
-- so a consensus we stored is the consensus we saw. A column nothing can write
-- is how an operator comes to believe a safeguard is in force when it is not.

ALTER TABLE events ADD COLUMN IF NOT EXISTS vendor_last_updated DATE;
ALTER TABLE events ADD COLUMN IF NOT EXISTS vendor_last_updated_first_seen DATE;
ALTER TABLE events ADD COLUMN IF NOT EXISTS vendor_revisions INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN events.vendor_last_updated IS
    'FMP''s `lastUpdated` for this earnings row, as of our most recent pull. '
    'Measured 2026-10-27: 100% of reported rows carry a stamp after their own '
    'report date, in per-symbol bulk blocks, so this alone proves nothing about '
    'any one row — it is only evidence paired with the two columns below.';

COMMENT ON COLUMN events.vendor_last_updated_first_seen IS
    'The date WE first recorded a stamp for this row. Never overwritten, '
    'because it is what fixes the vendor''s claim to a knowable moment.';

COMMENT ON COLUMN events.vendor_revisions IS
    'Times we have OBSERVED `lastUpdated` change. 0 is the only form of "not '
    'revised" the vendor lets us establish, and it is only meaningful from the '
    'date in `vendor_last_updated_first_seen` onward.';
