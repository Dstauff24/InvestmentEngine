-- Day-level API spend counter (D29).
--
-- The free-tier budget is a DAY-level limit shared by every FMP call site, but
-- it was previously enforced per job: universe enrichment capped its own pass
-- and fundamentals capped its own, so two jobs in one evening could jointly
-- exceed `fmp.daily_call_budget` while each believed it was within bounds.
--
-- The counter has to live in the database rather than in a process, because the
-- jobs run as separate `python -m` invocations from cron. Keyed on the ET
-- session date (core.clock), so it resets on the trading-day boundary rather
-- than on UTC midnight.

CREATE TABLE IF NOT EXISTS api_budget (
    provider       TEXT NOT NULL,
    session_date   DATE NOT NULL,
    calls_spent    INTEGER NOT NULL DEFAULT 0,
    -- Calls a job asked for and did not get. Recorded so a run that stopped on
    -- budget can REPORT the shortfall instead of looking like a clean run that
    -- happened to do less work.
    calls_declined INTEGER NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_api_budget PRIMARY KEY (provider, session_date),
    CONSTRAINT ck_api_budget_nonneg CHECK (calls_spent >= 0 AND calls_declined >= 0)
);

CREATE INDEX IF NOT EXISTS ix_api_budget_date ON api_budget (session_date DESC);
