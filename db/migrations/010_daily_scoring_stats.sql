-- D42: the daily aggregate for ideas that qualified but were not surfaced.
--
-- "Surfaced" means ranked into the digest under `ranking.max_ideas_per_day`
-- (D33). Only surfaced ideas become Shadow Book calls with projections and
-- outcome tracking. On the live 2026-10-21 universe 189 of 433 symbols cleared
-- a tier gate, and tracking all of them to outcome would make the Shadow Book
-- a record of the engine's COVERAGE rather than its JUDGEMENT — expectancy per
-- recipe (D11's north star) computed over 189 daily calls measures something
-- nobody will ever trade.
--
-- But the near-misses are the only record of what the engine ALMOST said, and
-- D39(3)'s paper phase tunes the Tier A/B gates. Without this row a threshold
-- change would be guesswork about the distribution just outside the cut.
--
-- What it deliberately CANNOT answer: whether a different cut would have made
-- money. Only closed Shadow Book ideas answer that, which is why D42 keeps
-- gate changes on D39 evidence and treats this as the shape of the
-- distribution rather than a verdict on it.
--
-- One row per session date. Re-running a day overwrites it, because the day's
-- scoring is a function of the data as it stood, not an append-only history.

CREATE TABLE IF NOT EXISTS daily_scoring_stats (
    session_date        DATE PRIMARY KEY,
    symbols_scored      INTEGER NOT NULL,
    symbols_abstained   INTEGER NOT NULL,

    tier_a_count        INTEGER NOT NULL,
    tier_b_count        INTEGER NOT NULL,
    surfaced_count      INTEGER NOT NULL,
    -- Qualifying but NOT surfaced: the near-misses D42 exists to remember.
    not_surfaced_count  INTEGER NOT NULL,

    -- D41's abstention mask, as {pillars_scored: symbol_count}. The single
    -- most important column for interpreting any of the others: a day where
    -- most symbols scored two pillars is not comparable to one where most
    -- scored four, and the composite distribution below means something
    -- different in each.
    mask_histogram      JSONB NOT NULL,

    -- Composite percentile distribution across symbols that HAVE a composite.
    -- Deciles rather than every value: the shape is what a gate change needs,
    -- and 433 numbers a day would be a series nobody queries.
    composite_min       NUMERIC(6, 2),
    composite_p25       NUMERIC(6, 2),
    composite_median    NUMERIC(6, 2),
    composite_p75       NUMERIC(6, 2),
    composite_p90       NUMERIC(6, 2),
    composite_max       NUMERIC(6, 2),

    -- The gates in force when this was recorded. A threshold change must not
    -- silently reinterpret yesterday's distribution, and D39 compares across
    -- time — so the thresholds travel with the row, exactly as
    -- `shadow_book.status_at_call` and `scored_pillars` do.
    tier_a_min_composite  NUMERIC(6, 2) NOT NULL,
    tier_b_min_composite  NUMERIC(6, 2) NOT NULL,
    max_ideas_per_day     INTEGER NOT NULL,

    recipe_counts       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- NO explicit updated_at trigger here: the discovery loop at the end of
-- schema.sql attaches `trg_set_updated_at` to every table that HAS an
-- updated_at column, dropping any existing one first. A named trigger of my
-- own was both redundant and non-idempotent — `CREATE TRIGGER` has no
-- IF NOT EXISTS in Postgres, so re-running db.init failed on it.

COMMENT ON TABLE daily_scoring_stats IS
    'D42: one row per session date aggregating the tier-qualifying ideas that '
    'were NOT surfaced. The evidence base for tuning the Tier A/B gates during '
    'the paper phase (D39(3)) — it shows the shape of the distribution just '
    'outside the cut, and deliberately cannot say whether a different cut would '
    'have made money. Only closed Shadow Book ideas answer that.';
