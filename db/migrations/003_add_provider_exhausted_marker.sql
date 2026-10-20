-- Provider-exhausted marker (D29 amendment, after the Oct 20 2026 live run).
--
-- `calls_spent` counts what THIS engine spent. It does not and cannot know what
-- the provider already served — a probe run outside the engine, a second
-- deployment, a manual curl. On Oct 20 the engine began its first live universe
-- run believing 247 calls were available when FMP's own allowance was nearly
-- gone, and spent 225 further calls discovering that fact one symbol at a time.
--
-- FMP answers an exhausted allowance with HTTP 200 and an "Error Message" body
-- (or a 429 saying "Limit Reach"), so every one of those looked like a fresh
-- request worth making. This marker records the provider's own verdict the first
-- time it is seen, keyed to the ET session date like the counters, so every
-- later job that day stops at the budget gate without opening a socket.
--
-- Deliberately NOT a reset of `calls_spent` to the cap: the two numbers answer
-- different questions ("what did we spend" vs "what did the provider say"), and
-- collapsing them would lose the divergence that is the whole point.

ALTER TABLE api_budget
    ADD COLUMN IF NOT EXISTS provider_exhausted_at   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS provider_exhausted_note TEXT;

COMMENT ON COLUMN api_budget.provider_exhausted_at IS
    'First moment the provider itself reported its allowance spent for this ET '
    'session date. Set once and never overwritten, so it records the earliest '
    'observation rather than the latest. NULL means never observed.';

COMMENT ON COLUMN api_budget.provider_exhausted_note IS
    'What the provider actually said, verbatim-ish, for the operator reading '
    'job_runs the next morning.';
