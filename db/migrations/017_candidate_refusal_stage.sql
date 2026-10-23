-- D42 amended: WHY a high scorer did not become a call.
--
-- `no_plan_reason` answered one case — no recipe fired at all. It could not
-- answer the other: a plan existed and a LATER stage declined it. On
-- 2026-10-21 that distinction was the whole story: of 310 tier-qualifying
-- ideas, 305 produced no plan, 4 were refused on D12's R:R gate, and exactly
-- ONE passed every stage — at rank 51, which the old surfacing logic had
-- already discarded by taking the top five by composite.
--
-- So the stage travels with the row. `refused_stage` is the gate that declined
-- it (`plan`, `risk_reward`, `events`, `sector`, `sizing`) and
-- `refused_reason` is that stage's own sentence. NULL on a row that became a
-- call, which is how a reader tells "surfaced" from "refused" without joining
-- anything.

ALTER TABLE daily_candidates
    ADD COLUMN IF NOT EXISTS refused_stage  TEXT,
    ADD COLUMN IF NOT EXISTS refused_reason TEXT;

COMMENT ON COLUMN daily_candidates.refused_stage IS
    'Which gate declined this candidate: plan, risk_reward, events, sector or '
    'sizing. NULL when it became a call. The column exists because "no recipe '
    'fired" and "a plan existed and a later stage refused it" are different '
    'facts and the digest has to be able to say which.';
