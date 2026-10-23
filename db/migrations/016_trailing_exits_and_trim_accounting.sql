-- O20: a trailing exit for the runner, and R measured in LEGS.
--
-- TWO THINGS, both of which the framework has always said and nothing has
-- implemented. `exits.swing.runner_trail` has read `ma_20` since the beginning
-- while nothing trailed anything; `trim_at_target_1` has said a third comes
-- off at TP1 while every R multiple in this engine described a whole position
-- held to one exit.
--
-- `proj_trail_ma` / `proj_trail_confirm_closes`: the remainder's exit rule,
-- frozen with the call exactly as `proj_stop` is. A trailing plan has no TP2
-- (`proj_target_2` is NULL), which is why that column was always nullable and
-- is now nullable on purpose.
--
-- `trimmed_r_multiple`: R as the WEIGHTED SUM OF THE LEGS, on the ORIGINAL
-- risk — a third at TP1, the remainder at whatever closed it. It sits BESIDE
-- `r_multiple` rather than replacing it, and that is the whole design:
--
--   * `r_multiple` stays the whole-position number, so **every decision already
--     made on it remains reproducible** (D48's citation, D46's adoption, O15's
--     refusal). A column that changed meaning under a stored number would make
--     those unreadable.
--   * `trimmed_r_multiple` is the strategy AS DEFINED, and it is what a
--     trailing recipe is certified on, because a trade that trims at TP1 and
--     trails the rest is not the trade a whole-position R describes.
--
-- Storing both and choosing at READ time is D24's principle applied to
-- outcomes: keep what happened, decide what it means when you ask.
--
-- **THE ACCOUNTING DOES NOT MOVE ANY EXIT.** Trims are measured over the same
-- resolution the tracker already produces — same fills, same pessimistic
-- ambiguous bar, same timeout. The one new exit path is the trail, which only
-- a trailing plan can have. So no historical row's `r_multiple` changes, and
-- the framework's "runner keeps going past TP2" is deliberately NOT modelled:
-- that would move every past exit, which is a different decision.

ALTER TABLE shadow_book
    ADD COLUMN IF NOT EXISTS proj_trail_ma             SMALLINT,
    ADD COLUMN IF NOT EXISTS proj_trail_confirm_closes SMALLINT,
    ADD COLUMN IF NOT EXISTS trimmed_r_multiple        NUMERIC(10, 4);

-- `trailed` joins the outcomes: a runner that left on its trail did not hit a
-- target, was not stopped, and did not run out of clock, and calling it any of
-- those would file the strategy's defining exit under someone else's name.
ALTER TABLE shadow_book DROP CONSTRAINT IF EXISTS shadow_book_outcome_check;
ALTER TABLE shadow_book
    ADD CONSTRAINT shadow_book_outcome_check
    CHECK (outcome IN ('open', 'target_1', 'target_2', 'trailed',
                       'stopped', 'timeout', 'invalidated', 'never_triggered'));

COMMENT ON COLUMN shadow_book.trimmed_r_multiple IS
    'R as the weighted sum of the legs on the ORIGINAL risk (trim at TP1, '
    'remainder at whatever closed it). Beside r_multiple, never replacing it: '
    'the whole-position number keeps every past decision reproducible, and this '
    'one is the strategy as defined.';
