-- D42 AMENDED: the day's top-ranked candidates, kept as rows rather than
-- rolled into a count.
--
-- D42 made the aggregate deliberate: only SURFACED ideas become Shadow Book
-- calls, and everything else rolls into one `daily_scoring_stats` row, because
-- tracking all 190 qualifying ideas would make the Shadow Book a record of
-- coverage rather than judgement. That reasoning still holds for the Shadow
-- Book. What it cost is a different question the aggregate cannot answer:
-- **does the composite predict returns at all, independent of any recipe?**
-- A count and a distribution cannot say, because they carry no symbol and
-- therefore no forward price.
--
-- So the top N by rank (N = `ranking.max_ideas_per_day`) are persisted with
-- their scores, their abstention mask, and the reason no plan exists — and
-- their FORWARD RETURNS at fixed horizons are recorded as the sessions pass.
--
-- **THESE ARE NOT SHADOW BOOK CALLS AND ARE NOT TRACKED AS TRADES.** No entry
-- zone, no stop, no target, no R multiple, no outcome: there is nothing to
-- resolve, because no plan was ever made for most of them. D13's expectancy
-- and D40's ladder count CLOSED SHADOW BOOK IDEAS and must never read a row
-- from this table — a candidate's forward return is a property of the SCORE,
-- not of a trade, and mixing the two would let a number nobody risked
-- anything on promote a strategy.
--
-- WHY THE HORIZONS ARE IN SESSIONS. 5, 10 and 20 SESSIONS, not calendar days:
-- a holiday week would otherwise shorten the window it claims to measure, and
-- the measurement is about how price behaved over N opportunities to move.
-- Each horizon stores the session it was measured TO, so the count is
-- checkable rather than implied.
--
-- WHY TOTAL RETURN. D19 measures performance on total return everywhere, and a
-- dividend paid inside a 20-session window is part of what holding earned. The
-- technical features stay split-only (a dividend-adjusted moving average does
-- not match the chart a human reads), so these two series differ ON PURPOSE.
--
-- FILLED ONCE, NEVER REWRITTEN. A later restatement (a repaired basis, a late
-- bar) must not silently move a number a study has already read; the writer
-- only ever fills a NULL. Not a hypertable: N rows a day is a few thousand a
-- year, and the four history tables in D20 are the ones that grow without
-- bound.
--
-- NO FOREIGN KEY on `symbol`, for the same reason `shadow_book` has none: this
-- is a record of what the engine SAW on a date, and a universe rebuild that
-- dropped a ticker must not be able to delete the evidence.

CREATE TABLE IF NOT EXISTS daily_candidates (
    session_date      DATE NOT NULL,
    symbol            TEXT NOT NULL,

    -- Rank within the WHOLE universe (1 = best composite), so a row says where
    -- it stood among everything scored rather than among the rows kept.
    rank              INTEGER NOT NULL CHECK (rank > 0),
    composite         NUMERIC(6, 2),
    -- D33's percentile within the universe, 0-100.
    percentile        NUMERIC(6, 2),

    -- D41: WHICH pillars scored and how many. 72 from four pillars and 72 from
    -- two are different claims, and the calibration question this table exists
    -- for is exactly whether thinner composites predict worse.
    scored_pillars    TEXT,
    pillars_scored    SMALLINT CHECK (pillars_scored IS NULL
                                      OR pillars_scored BETWEEN 0 AND 4),

    tier              TEXT CHECK (tier IS NULL OR tier IN ('A', 'B')),
    tier_reason       TEXT,
    -- Did it become a Shadow Book call (D42's "surfaced")? A surfaced idea is
    -- in BOTH places on purpose: here as a score with a forward return, there
    -- as a trade with an outcome. They answer different questions.
    surfaced          BOOLEAN NOT NULL DEFAULT FALSE,

    -- The plan, if one existed at all: enough to tell "the score was high and
    -- no setup fired" from "a setup fired and the gate refused it".
    recipe            TEXT,
    risk_reward       NUMERIC(10, 4),
    no_plan_reason    TEXT,

    -- The gates in force, travelling with the row exactly as they do on
    -- `daily_scoring_stats` and `shadow_book`: a threshold change must not
    -- silently reinterpret what yesterday's rank meant.
    gates_in_force    JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- The RAW close on the session, for a human checking against a chart. The
    -- forward returns below are computed from an adjusted series at
    -- measurement time, so they are ratios and this is reference only.
    close_raw         NUMERIC(18, 6),

    fwd_5_pct         NUMERIC(10, 4),
    fwd_5_on          DATE,
    fwd_10_pct        NUMERIC(10, 4),
    fwd_10_on         DATE,
    fwd_20_pct        NUMERIC(10, 4),
    fwd_20_on         DATE,

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (session_date, symbol)
);

CREATE INDEX IF NOT EXISTS ix_daily_candidates_symbol
    ON daily_candidates (symbol, session_date DESC);

-- The query the forward-return job runs: rows still missing a horizon.
CREATE INDEX IF NOT EXISTS ix_daily_candidates_pending
    ON daily_candidates (session_date)
    WHERE fwd_20_pct IS NULL;

COMMENT ON TABLE daily_candidates IS
    'D42 amended: the day''s top-ranked candidates with forward returns at 5/10/20 '
    'SESSIONS. Scores, not trades — no entry, stop, target or outcome. D13''s '
    'expectancy and D40''s ladder count closed Shadow Book ideas and must never '
    'read this table.';
