-- ============================================================================
-- Investment Decision Engine — canonical schema (D20: Postgres >=15 + Timescale)
-- ----------------------------------------------------------------------------
-- Applied by `python -m db.init`. IDEMPOTENT: every statement is safe to re-run
-- against a live database. Never edit a shipped table definition in place once
-- production data exists — add a numbered file under db/migrations/ instead.
--
-- Conventions (CLAUDE.md):
--   * Dual time keying. Every time-series table carries `ts TIMESTAMPTZ` (UTC,
--     the hypertable dimension) AND `session_date DATE` (the ET trading day,
--     which is the join key). Storage is UTC; trading-day semantics are
--     Eastern. Deriving one from the other in every query is how a bar ends up
--     filed under the wrong session.
--   * Money and prices are NUMERIC, never float. Scores and ratios too.
--   * State columns are TEXT + CHECK, not native ENUMs, so a new value is a
--     one-line migration instead of a type rewrite under an exclusive lock.
--   * Every ingestion table carries `source` and `ingested_at` for provenance.
--   * Postgres 15+ required: `UNIQUE NULLS NOT DISTINCT` below.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ── Shared helpers ──────────────────────────────────────────────────────────

-- Keeps `updated_at` honest without every writer having to remember it.
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

-- Migration bookkeeping for db/migrations/*.sql.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    checksum    TEXT
);


-- ============================================================================
-- 1. UNIVERSE
-- ============================================================================

-- The Phase 1 universe (D14). Rebuilt weekly by ingest/universe.py. Rows are
-- never deleted, only deactivated: ideas, fundamentals, events and news all
-- reference tickers.symbol, and a delete would cascade real history away.
CREATE TABLE IF NOT EXISTS tickers (
    symbol              TEXT PRIMARY KEY,
    company_name        TEXT,
    exchange            TEXT,
    sector              TEXT,
    industry            TEXT,
    market_cap          NUMERIC(20, 2),

    -- Membership: why this symbol is in the universe at all.
    in_sp500            BOOLEAN NOT NULL DEFAULT FALSE,
    in_nasdaq100        BOOLEAN NOT NULL DEFAULT FALSE,
    in_watchlist        BOOLEAN NOT NULL DEFAULT FALSE,

    -- D14 liquidity gate. `avg_volume_20d` is what the filter was applied to.
    avg_volume_20d      BIGINT,
    -- When that average was last verified. NOT `updated_at`, whose trigger
    -- fires on any column change and would make a stale volume look fresh.
    -- Orders the leftover D29 call budget, oldest first. Added by
    -- db/migrations/001_add_avg_volume_asof.sql.
    avg_volume_asof     DATE,
    -- NULL = nobody has checked yet. ingest/schwab_chains.py sets it from
    -- whether Schwab returned a chain; the D14 options gate abstains on NULL
    -- and enforces once populated.
    has_options         BOOLEAN,
    passes_liquidity    BOOLEAN NOT NULL DEFAULT FALSE,

    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    deactivated_on      DATE,

    first_seen_on       DATE NOT NULL DEFAULT CURRENT_DATE,
    last_seen_on        DATE NOT NULL DEFAULT CURRENT_DATE,
    source              TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_tickers_active ON tickers (is_active) WHERE is_active;
CREATE INDEX IF NOT EXISTS ix_tickers_sector ON tickers (sector);
CREATE INDEX IF NOT EXISTS ix_tickers_volume_staleness
    ON tickers (avg_volume_asof NULLS FIRST) WHERE is_active;


-- ============================================================================
-- 2. PRICES  — hypertable  (D15 / D24)
-- ============================================================================

-- Daily OHLCV, AS REPORTED, UNADJUSTED.
--
-- There are deliberately no adj_* columns. A vendor "adjusted close" is not a
-- fact about a past day, it is a derived view of the whole history that every
-- new split or dividend rewrites end-to-end. Materialising it would mean
-- restating years of rows on every corporate action — decompressing Timescale
-- chunks to do it — and would make a backtest run today silently disagree with
-- the same backtest run last quarter, which breaks O7's walk-forward
-- validation and D13's premise that outcomes are measured against what we
-- actually projected. Instead this table is append-only raw truth,
-- `corporate_actions` holds the immutable action log, and
-- features/adjustment.py multiplies the two at read time with an `as_of` date.
CREATE TABLE IF NOT EXISTS prices_daily (
    ts              TIMESTAMPTZ NOT NULL,
    session_date    DATE        NOT NULL,
    symbol          TEXT        NOT NULL,

    open            NUMERIC(18, 6),
    high            NUMERIC(18, 6),
    low             NUMERIC(18, 6),
    close           NUMERIC(18, 6),
    volume          BIGINT,

    source          TEXT NOT NULL DEFAULT 'schwab',
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_prices_daily PRIMARY KEY (symbol, ts)
);

SELECT create_hypertable('prices_daily', 'ts',
                         chunk_time_interval => INTERVAL '1 year',
                         if_not_exists       => TRUE);

CREATE INDEX IF NOT EXISTS ix_prices_daily_symbol_date
    ON prices_daily (symbol, session_date DESC);


-- ── Corporate actions ───────────────────────────────────────────────────────
-- The immutable log that turns raw prices into an adjusted series (D24). Small
-- (a few rows per symbol per year), so relational rather than a hypertable.
CREATE TABLE IF NOT EXISTS corporate_actions (
    symbol            TEXT NOT NULL REFERENCES tickers (symbol) ON DELETE CASCADE,
    action_type       TEXT NOT NULL CHECK (action_type IN (
                          'split', 'cash_dividend', 'special_dividend', 'spinoff')),
    -- First session on which the stock trades WITHOUT the distribution. The
    -- adjustment applies to every bar strictly BEFORE this date.
    ex_date           DATE NOT NULL,

    -- Shares held after, per share held before. 4-for-1 => 4.0;
    -- a 1-for-10 reverse split => 0.1.
    split_ratio       NUMERIC(18, 8),

    -- Cash distributions, per share, in `currency`.
    cash_amount       NUMERIC(18, 6),
    currency          TEXT NOT NULL DEFAULT 'USD',

    declaration_date  DATE,
    record_date       DATE,
    payment_date      DATE,

    -- D19(5): a cut is an automatic thesis-review trigger, so the prior regular
    -- amount is kept alongside for the comparison.
    prior_cash_amount NUMERIC(18, 6),
    is_dividend_cut   BOOLEAN,

    source            TEXT NOT NULL,
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_corporate_actions PRIMARY KEY (symbol, action_type, ex_date),
    -- A split row without a ratio, or a dividend row without an amount, is a
    -- silently wrong adjustment factor. Reject it at write time.
    CONSTRAINT ck_corporate_actions_payload CHECK (
        (action_type = 'split' AND split_ratio IS NOT NULL AND split_ratio > 0)
        OR (action_type <> 'split' AND cash_amount IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_corporate_actions_symbol_ex
    ON corporate_actions (symbol, ex_date DESC);


-- ============================================================================
-- 3. OPTION CHAIN SNAPSHOTS  — hypertable  (D7 / D22)
-- ============================================================================

-- SACRED JOB (D7): one snapshot per trading day, forever. This table is how we
-- build our own IV-rank history instead of renting one. Prior days are NEVER
-- overwritten — an upsert only ever refreshes the same (symbol, snapshot_ts).
--
-- D22 scope: strikes within +/-15% of spot x the nearest 4 MONTHLY expirations.
-- The filter lives in ingest/schwab_chains.py and config/universe.yaml; this
-- table records what came back.
CREATE TABLE IF NOT EXISTS option_chain_snapshots (
    snapshot_ts         TIMESTAMPTZ NOT NULL,
    session_date        DATE        NOT NULL,
    symbol              TEXT        NOT NULL,
    expiration          DATE        NOT NULL,
    strike              NUMERIC(18, 6) NOT NULL,
    option_type         TEXT        NOT NULL CHECK (option_type IN ('C', 'P')),

    contract_symbol     TEXT,
    underlying_price    NUMERIC(18, 6),
    days_to_expiration  INTEGER,
    is_monthly          BOOLEAN,

    bid                 NUMERIC(18, 6),
    ask                 NUMERIC(18, 6),
    last                NUMERIC(18, 6),
    mark                NUMERIC(18, 6),
    volume              BIGINT,
    open_interest       BIGINT,

    -- Stored as a DECIMAL FRACTION (0.285), not Schwab's percent (28.5). The
    -- conversion happens once, at the client boundary, so nothing downstream
    -- has to remember which unit it is holding.
    implied_volatility  NUMERIC(12, 6),
    delta               NUMERIC(12, 6),
    gamma               NUMERIC(12, 6),
    theta               NUMERIC(12, 6),
    vega                NUMERIC(12, 6),
    rho                 NUMERIC(12, 6),

    intrinsic_value     NUMERIC(18, 6),
    -- From Schwab's `timeValue`, NOT its `extrinsicValue` (which is wrong in the
    -- payload — it exceeds the mark on OTM contracts). D19(4)'s short-call
    -- assignment test depends on this being the real extrinsic value.
    extrinsic_value     NUMERIC(18, 6),
    in_the_money        BOOLEAN,

    source              TEXT NOT NULL DEFAULT 'schwab',
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_option_chain_snapshots
        PRIMARY KEY (symbol, snapshot_ts, expiration, strike, option_type)
);

SELECT create_hypertable('option_chain_snapshots', 'snapshot_ts',
                         chunk_time_interval => INTERVAL '7 days',
                         if_not_exists       => TRUE);

CREATE INDEX IF NOT EXISTS ix_chains_symbol_session
    ON option_chain_snapshots (symbol, session_date DESC);
CREATE INDEX IF NOT EXISTS ix_chains_expiration
    ON option_chain_snapshots (symbol, expiration, snapshot_ts DESC);


-- ============================================================================
-- 4. IV DAILY  — hypertable  (D7 / D22 aggregate)
-- ============================================================================

-- One row per ticker per day: the aggregate volatility summary derived from
-- that day's chain snapshot. IV rank/percentile stay NULL until
-- option_chains.iv_rank_min_history_days of history exist (D7) — that is an
-- expected state for the first quarter, not an error.
CREATE TABLE IF NOT EXISTS iv_daily (
    ts                      TIMESTAMPTZ NOT NULL,
    session_date            DATE        NOT NULL,
    symbol                  TEXT        NOT NULL,

    underlying_price        NUMERIC(18, 6),

    iv30                    NUMERIC(12, 6),  -- 30-day interpolated ATM IV
    iv_atm_front            NUMERIC(12, 6),  -- ATM IV, nearest monthly
    iv_call_atm             NUMERIC(12, 6),
    iv_put_atm              NUMERIC(12, 6),

    skew_25d                NUMERIC(12, 6),  -- 25-delta put IV - 25-delta call IV
    term_slope              NUMERIC(12, 6),  -- back-month ATM IV - front-month
    expected_move_front_pct NUMERIC(12, 6),  -- ATM straddle / spot, front month

    iv_rank_252             NUMERIC(12, 6),
    iv_percentile_252       NUMERIC(12, 6),
    history_days            INTEGER,         -- days of IV history behind the rank

    total_open_interest     BIGINT,
    total_option_volume     BIGINT,
    put_call_oi_ratio       NUMERIC(12, 6),

    source                  TEXT NOT NULL DEFAULT 'derived',
    ingested_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_iv_daily PRIMARY KEY (symbol, ts)
);

SELECT create_hypertable('iv_daily', 'ts',
                         chunk_time_interval => INTERVAL '1 year',
                         if_not_exists       => TRUE);

CREATE INDEX IF NOT EXISTS ix_iv_daily_symbol_session
    ON iv_daily (symbol, session_date DESC);


-- ============================================================================
-- 5. ESTIMATE SNAPSHOTS  — hypertable  (D7)
-- ============================================================================

-- SACRED JOB (D7). The estimate-revision-momentum edge is literally the diff
-- between consecutive snapshot_dates, so a day that gets overwritten is a day
-- of signal destroyed. Writers upsert on the full PK and never DELETE.
--
-- `snapshot_date` is the ET trading date the pull was taken (the hypertable
-- time dimension); `fiscal_period_end` is the period being estimated.
CREATE TABLE IF NOT EXISTS estimate_snapshots (
    snapshot_date            DATE NOT NULL,
    symbol                   TEXT NOT NULL,
    period_type              TEXT NOT NULL CHECK (period_type IN ('quarter', 'annual')),
    fiscal_period_end        DATE NOT NULL,

    estimated_revenue_avg    NUMERIC(24, 4),
    estimated_revenue_low    NUMERIC(24, 4),
    estimated_revenue_high   NUMERIC(24, 4),
    estimated_eps_avg        NUMERIC(18, 6),
    estimated_eps_low        NUMERIC(18, 6),
    estimated_eps_high       NUMERIC(18, 6),
    estimated_ebitda_avg     NUMERIC(24, 4),
    estimated_net_income_avg NUMERIC(24, 4),

    analyst_count_revenue    INTEGER,
    analyst_count_eps        INTEGER,

    source                   TEXT NOT NULL DEFAULT 'fmp',
    ingested_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_estimate_snapshots
        PRIMARY KEY (symbol, snapshot_date, period_type, fiscal_period_end)
);

SELECT create_hypertable('estimate_snapshots', 'snapshot_date',
                         chunk_time_interval => INTERVAL '3 months',
                         if_not_exists       => TRUE);

CREATE INDEX IF NOT EXISTS ix_estimates_symbol_period
    ON estimate_snapshots (symbol, fiscal_period_end, snapshot_date DESC);


-- ============================================================================
-- 6. FUNDAMENTALS  (relational)
-- ============================================================================

-- Reported financials + key ratios (Pillar B). Unlike estimate_snapshots this
-- is point-in-fact data keyed by fiscal period, so a restatement legitimately
-- overwrites the row.
--
-- D29 free-tier note: `ratios` and `key-metrics` are plan-gated, so the
-- valuation and quality columns below are NULL until the plan is upgraded.
-- Margins are computed from the statements, so they survive the gap.
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol               TEXT NOT NULL REFERENCES tickers (symbol) ON DELETE CASCADE,
    period_type          TEXT NOT NULL CHECK (period_type IN ('quarter', 'annual')),
    fiscal_date_ending   DATE NOT NULL,
    calendar_year        INTEGER,
    fiscal_period        TEXT,              -- 'Q1'..'Q4' / 'FY'
    reported_currency    TEXT,

    -- Income statement
    revenue              NUMERIC(24, 4),
    gross_profit         NUMERIC(24, 4),
    operating_income     NUMERIC(24, 4),
    net_income           NUMERIC(24, 4),
    ebitda               NUMERIC(24, 4),
    eps_diluted          NUMERIC(18, 6),
    shares_diluted       NUMERIC(24, 4),

    -- Cash flow
    operating_cash_flow  NUMERIC(24, 4),
    capital_expenditure  NUMERIC(24, 4),
    free_cash_flow       NUMERIC(24, 4),

    -- Balance sheet
    total_assets         NUMERIC(24, 4),
    total_equity         NUMERIC(24, 4),
    total_debt           NUMERIC(24, 4),
    cash_and_equivalents NUMERIC(24, 4),

    -- Margins / quality. Computed from the statements so numerator and
    -- denominator always come from the same filing.
    gross_margin         NUMERIC(12, 6),
    operating_margin     NUMERIC(12, 6),
    net_margin           NUMERIC(12, 6),
    roe                  NUMERIC(12, 6),
    roic                 NUMERIC(12, 6),
    current_ratio        NUMERIC(12, 6),
    debt_to_equity       NUMERIC(12, 6),

    -- Valuation
    pe_ratio             NUMERIC(12, 6),
    ps_ratio             NUMERIC(12, 6),
    pb_ratio             NUMERIC(12, 6),
    ev_to_ebitda         NUMERIC(12, 6),
    peg_ratio            NUMERIC(12, 6),

    -- Dividend quality (D19: growth + payout health; a cut is a thesis review)
    dividend_per_share   NUMERIC(18, 6),
    payout_ratio         NUMERIC(12, 6),

    filing_date          DATE,
    accepted_date        TIMESTAMPTZ,

    -- Provider fields not yet promoted to columns.
    raw                  JSONB,

    source               TEXT NOT NULL DEFAULT 'fmp',
    ingested_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_fundamentals PRIMARY KEY (symbol, period_type, fiscal_date_ending)
);

CREATE INDEX IF NOT EXISTS ix_fundamentals_symbol_recent
    ON fundamentals (symbol, fiscal_date_ending DESC);


-- ============================================================================
-- 7. EVENTS  (relational)  — the event radar
-- ============================================================================

-- Earnings dates, ex-dividend dates (D19), splits, guidance, macro prints.
-- `symbol` is NULL for market-wide macro events; the unique key uses
-- NULLS NOT DISTINCT (PG15+) so those still de-duplicate correctly.
CREATE TABLE IF NOT EXISTS events (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol           TEXT REFERENCES tickers (symbol) ON DELETE CASCADE,
    event_type       TEXT NOT NULL CHECK (event_type IN (
                         'earnings', 'ex_dividend', 'dividend_payment',
                         'split', 'guidance', 'macro')),
    event_date       DATE NOT NULL,
    -- 'bmo' before open, 'amc' after close, 'dmt' during market, 'unknown'
    event_time       TEXT CHECK (event_time IN ('bmo', 'amc', 'dmt', 'unknown')),
    is_confirmed     BOOLEAN NOT NULL DEFAULT FALSE,
    title            TEXT,

    -- Earnings
    eps_estimate     NUMERIC(18, 6),
    eps_actual       NUMERIC(18, 6),
    revenue_estimate NUMERIC(24, 4),
    revenue_actual   NUMERIC(24, 4),

    -- Dividends (D19)
    dividend_amount  NUMERIC(18, 6),
    declaration_date DATE,
    record_date      DATE,
    payment_date     DATE,

    details          JSONB,
    source           TEXT NOT NULL,
    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_events UNIQUE NULLS NOT DISTINCT (symbol, event_type, event_date)
);

CREATE INDEX IF NOT EXISTS ix_events_date_type ON events (event_date, event_type);
CREATE INDEX IF NOT EXISTS ix_events_symbol_upcoming
    ON events (symbol, event_date) WHERE symbol IS NOT NULL;


-- ============================================================================
-- 8. NEWS  (relational)  — Pillar D corpus
-- ============================================================================

-- Finnhub company news (D5), upserted by provider article id so a re-run of the
-- rolling window never duplicates. One article tagged to three tickers stores
-- three rows, because the sentiment pillar scores per symbol.
CREATE TABLE IF NOT EXISTS news_articles (
    article_id      TEXT NOT NULL,
    source_provider TEXT NOT NULL DEFAULT 'finnhub',
    symbol          TEXT REFERENCES tickers (symbol) ON DELETE CASCADE,
    published_at    TIMESTAMPTZ NOT NULL,
    headline        TEXT,
    summary         TEXT,
    url             TEXT,
    publisher       TEXT,
    category        TEXT,
    image_url       TEXT,

    -- Filled by the Phase 2 sentiment pillar; NULL until then.
    sentiment_score NUMERIC(12, 6),
    sentiment_label TEXT CHECK (sentiment_label IN ('bullish', 'bearish', 'neutral')),
    materiality     NUMERIC(12, 6),
    classified_at   TIMESTAMPTZ,

    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_news_articles PRIMARY KEY (source_provider, article_id, symbol)
);

CREATE INDEX IF NOT EXISTS ix_news_symbol_time ON news_articles (symbol, published_at DESC);


-- ============================================================================
-- 9. IDEAS  (relational)  — D2 / D12
-- ============================================================================

-- Every idea the engine surfaces, with the COMPLETE trade plan. An idea without
-- entry/stop/targets/R:R must not exist (D10).
CREATE TABLE IF NOT EXISTS ideas (
    id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    as_of_date            DATE NOT NULL,
    symbol                TEXT NOT NULL REFERENCES tickers (symbol) ON DELETE CASCADE,
    recipe                TEXT NOT NULL,   -- e.g. 'pullback_to_trend'
    strategy              TEXT NOT NULL CHECK (strategy IN ('swing', 'hold', 'option')),
    tier                  TEXT NOT NULL CHECK (tier IN ('A', 'B')),

    -- Scoring (D2). Weights come from config/scoring.yaml, never from code.
    composite_score       NUMERIC(6, 2) NOT NULL CHECK (composite_score BETWEEN 0 AND 100),
    score_technical       NUMERIC(6, 2),
    score_fundamental     NUMERIC(6, 2),
    score_catalyst        NUMERIC(6, 2),
    score_sentiment       NUMERIC(6, 2),
    pillars_agreeing      SMALLINT NOT NULL CHECK (pillars_agreeing BETWEEN 0 AND 4),

    -- Trade plan
    entry_low             NUMERIC(18, 6) NOT NULL,
    entry_high            NUMERIC(18, 6) NOT NULL,
    stop_price            NUMERIC(18, 6) NOT NULL,
    stop_is_div_adjusted  BOOLEAN NOT NULL DEFAULT FALSE,   -- D19
    target_1              NUMERIC(18, 6) NOT NULL,
    target_2              NUMERIC(18, 6),
    runner_rule           TEXT,
    risk_reward           NUMERIC(10, 4) NOT NULL,

    -- Sizing (D17/D18). Recorded WITH the account value it was computed from,
    -- so a historical idea can be audited against the rules of its own day.
    risk_pct              NUMERIC(10, 6) NOT NULL,
    account_value_at_call NUMERIC(20, 2),
    suggested_shares      INTEGER,
    suggested_notional    NUMERIC(20, 2),
    max_loss_amount       NUMERIC(20, 2),

    -- Explainability: what fired, and what argues against it.
    confluences           JSONB,
    risks                 JSONB,
    option_alternative    JSONB,

    status                TEXT NOT NULL DEFAULT 'surfaced'
                          CHECK (status IN ('surfaced', 'entered', 'expired',
                                            'invalidated', 'skipped')),
    valid_until           DATE,
    digest_sent_at        TIMESTAMPTZ,

    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_ideas UNIQUE (symbol, as_of_date, recipe, strategy),
    CONSTRAINT ck_ideas_entry_zone CHECK (entry_high >= entry_low)
);

CREATE INDEX IF NOT EXISTS ix_ideas_date_tier ON ideas (as_of_date DESC, tier);
CREATE INDEX IF NOT EXISTS ix_ideas_symbol    ON ideas (symbol, as_of_date DESC);


-- ============================================================================
-- 10. SHADOW BOOK  (relational)  — D13
-- ============================================================================

-- EVERY idea, taken or not, tracked to outcome. This is the learning loop, not
-- "just logging". Discretionary ideas are logged here too, with idea_id NULL.
--
-- `symbol` deliberately has NO FK to tickers: D13 says discretionary ideas get
-- logged, and those may be on names outside the universe.
CREATE TABLE IF NOT EXISTS shadow_book (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    idea_id                 BIGINT REFERENCES ideas (id) ON DELETE SET NULL,
    symbol                  TEXT NOT NULL,
    origin                  TEXT NOT NULL DEFAULT 'engine'
                            CHECK (origin IN ('engine', 'discretionary')),
    tier                    TEXT CHECK (tier IN ('A', 'B')),
    recipe                  TEXT,
    strategy                TEXT CHECK (strategy IN ('swing', 'hold', 'option')),
    called_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    called_on               DATE NOT NULL,

    -- D40: the recipe's promotion-ladder status WHEN THIS IDEA WAS CALLED.
    -- Expectancy must be computed per (recipe, status_at_call): mixing regimes
    -- averages the SHADOW ideas a recipe produced while it was being judged
    -- with the PAPER/LIVE ideas it produced after, and the promotion decision
    -- is made FROM that expectancy — so contaminating it makes the ladder
    -- circular, undetectably, because nothing else in the row says which
    -- regime it belonged to. Nullable: rows predating 2026-10-21 genuinely do
    -- not know, and back-filling a guess would be worse than admitting it.
    status_at_call          TEXT CHECK (status_at_call IS NULL
                                        OR status_at_call IN ('shadow', 'paper', 'live')),

    -- Projections AT TIME OF CALL. Frozen — never updated after insert. The
    -- whole point is measuring reality against what we said we expected.
    proj_entry_low          NUMERIC(18, 6),
    proj_entry_high         NUMERIC(18, 6),
    proj_stop               NUMERIC(18, 6),
    proj_target_1           NUMERIC(18, 6),
    proj_target_2           NUMERIC(18, 6),
    proj_risk_reward        NUMERIC(10, 4),
    proj_confidence         NUMERIC(6, 2),
    proj_horizon_days       INTEGER,

    -- Reality
    was_taken               BOOLEAN NOT NULL DEFAULT FALSE,
    actual_entry            NUMERIC(18, 6),
    actual_entry_at         TIMESTAMPTZ,
    actual_exit             NUMERIC(18, 6),
    actual_exit_at          TIMESTAMPTZ,
    dividends_collected     NUMERIC(18, 6) NOT NULL DEFAULT 0,   -- D19 total return

    hit_target_1            BOOLEAN,
    hit_target_2            BOOLEAN,
    hit_stop                BOOLEAN,
    max_favorable_excursion NUMERIC(18, 6),
    max_adverse_excursion   NUMERIC(18, 6),

    r_multiple              NUMERIC(12, 6),   -- realized P&L / initial risk
    return_pct              NUMERIC(12, 6),
    total_return_pct        NUMERIC(12, 6),   -- D19: includes dividends
    days_held               INTEGER,

    outcome                 TEXT NOT NULL DEFAULT 'open'
                            CHECK (outcome IN ('open', 'target_1', 'target_2',
                                               'stopped', 'timeout', 'invalidated',
                                               'never_triggered')),
    closed_at               TIMESTAMPTZ,
    notes                   TEXT,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_shadow_book_idea
    ON shadow_book (idea_id) WHERE idea_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_shadow_book_open
    ON shadow_book (outcome) WHERE outcome = 'open';
-- Expectancy is always read per recipe AND per status (D40).
CREATE INDEX IF NOT EXISTS ix_shadow_book_status_recipe
    ON shadow_book (recipe, status_at_call, called_on DESC);
CREATE INDEX IF NOT EXISTS ix_shadow_book_recipe_tier
    ON shadow_book (recipe, tier, called_on DESC);


-- ============================================================================
-- 11. POSITIONS  (relational)  — read-only broker sync
-- ============================================================================

-- Snapshot of the real account (Phase 2 sync; the table exists now so
-- risk_manager has somewhere to read from). READ-ONLY: nothing in this repo
-- places orders.
CREATE TABLE IF NOT EXISTS positions (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    as_of             TIMESTAMPTZ NOT NULL,
    as_of_date        DATE NOT NULL,
    account_hash      TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    asset_type        TEXT NOT NULL CHECK (asset_type IN (
                          'EQUITY', 'ETF', 'OPTION', 'MUTUAL_FUND',
                          'FIXED_INCOME', 'CASH', 'OTHER')),
    -- D17 bucket. The heat cap counts 'swing' + 'option' only.
    bucket            TEXT CHECK (bucket IN ('etf', 'hold', 'swing', 'option', 'cash')),

    quantity          NUMERIC(20, 6) NOT NULL,
    average_price     NUMERIC(18, 6),
    market_value      NUMERIC(20, 2),
    cost_basis        NUMERIC(20, 2),
    unrealized_pl     NUMERIC(20, 2),
    unrealized_pl_pct NUMERIC(12, 6),
    day_pl            NUMERIC(20, 2),

    -- Option legs
    underlying_symbol TEXT,
    expiration        DATE,
    strike            NUMERIC(18, 6),
    put_call          TEXT CHECK (put_call IN ('CALL', 'PUT')),

    -- Links back to the plan this position came from, if any.
    idea_id           BIGINT REFERENCES ideas (id) ON DELETE SET NULL,
    stop_price        NUMERIC(18, 6),
    open_risk_amount  NUMERIC(20, 2),   -- D18: ~0 once the stop is at breakeven
    thesis_status     TEXT CHECK (thesis_status IN ('green', 'yellow', 'red')),

    source            TEXT NOT NULL DEFAULT 'schwab',
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- NULLS NOT DISTINCT: equities have NULL option legs and must still dedupe.
    CONSTRAINT uq_positions UNIQUE NULLS NOT DISTINCT
        (account_hash, as_of_date, symbol, expiration, strike, put_call)
);

CREATE INDEX IF NOT EXISTS ix_positions_latest ON positions (as_of_date DESC, account_hash);


-- ============================================================================
-- 12. ALERTS  (relational)  — D16
-- ============================================================================

CREATE TABLE IF NOT EXISTS alerts (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alert_type TEXT NOT NULL CHECK (alert_type IN (
                   'daily_digest', 'new_idea', 'entry_hit', 'stop_warning',
                   'take_profit', 'thesis_change', 'allocation_drift',
                   'event_radar', 'job_failure', 'token_expiry')),
    severity   TEXT NOT NULL DEFAULT 'info'
               CHECK (severity IN ('info', 'warning', 'critical')),
    symbol     TEXT,
    idea_id    BIGINT REFERENCES ideas (id) ON DELETE SET NULL,
    subject    TEXT NOT NULL,
    body       TEXT,
    payload    JSONB,

    channel    TEXT NOT NULL DEFAULT 'email' CHECK (channel IN ('email', 'log', 'telegram')),
    status     TEXT NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'sent', 'failed', 'suppressed')),
    sent_at    TIMESTAMPTZ,
    error      TEXT,

    -- Stops the same alert firing twice, e.g. 'stop_warning:AAPL:2026-10-20'.
    dedupe_key TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_alerts_pending ON alerts (status) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS ix_alerts_created ON alerts (created_at DESC);


-- ============================================================================
-- 13. API BUDGET  (relational)  — day-level provider spend (D29)
-- ============================================================================

-- The free-tier FMP budget is a DAY-level limit shared by every call site, and
-- the jobs run as separate `python -m` invocations from cron — so the counter
-- has to survive process boundaries. Enforcing it per job instead would let two
-- jobs in one evening jointly exceed the budget while each believed it was
-- within bounds.
--
-- Keyed on the ET session date (core.clock), so it resets on the trading-day
-- boundary rather than at UTC midnight.
-- Added by db/migrations/002_add_api_budget.sql.
CREATE TABLE IF NOT EXISTS api_budget (
    provider       TEXT NOT NULL,
    session_date   DATE NOT NULL,
    calls_spent    INTEGER NOT NULL DEFAULT 0,
    -- Calls a job asked for and did not get. Recorded so a run that stopped on
    -- budget can REPORT the shortfall instead of looking like a clean run that
    -- happened to do less work.
    calls_declined INTEGER NOT NULL DEFAULT 0,
    -- The PROVIDER's own verdict, which `calls_spent` cannot infer: it counts
    -- only what this engine spent, never what the provider already served to a
    -- probe, a second deployment or a manual call. Set the first time FMP
    -- reports its allowance gone (HTTP 200 + "Error Message", or a 429 saying
    -- "Limit Reach"), and never overwritten, so it keeps the EARLIEST
    -- observation. Every later job that day then stops at the budget gate
    -- without opening a socket. See db/migrations/003.
    provider_exhausted_at   TIMESTAMPTZ,
    provider_exhausted_note TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_api_budget PRIMARY KEY (provider, session_date),
    CONSTRAINT ck_api_budget_nonneg CHECK (calls_spent >= 0 AND calls_declined >= 0)
);

CREATE INDEX IF NOT EXISTS ix_api_budget_date ON api_budget (session_date DESC);


-- ============================================================================
-- 13. JOB RUNS  (relational)  — the audit trail every job writes to
-- ============================================================================

-- CLAUDE.md: "Every job writes a row to job_runs (start, end, status,
-- rows_written, error). The orchestrator refuses to mark a day complete if a
-- snapshot job failed." `is_critical` marks the sacred snapshot jobs (D7) so
-- that rule is enforced by query rather than by a hard-coded job-name list.
CREATE TABLE IF NOT EXISTS job_runs (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_name       TEXT NOT NULL,
    session_date   DATE NOT NULL,          -- the ET trading date this run is FOR
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ,
    duration_ms    INTEGER,
    status         TEXT NOT NULL DEFAULT 'running'
                   CHECK (status IN ('running', 'success', 'failed', 'skipped', 'partial')),
    is_critical    BOOLEAN NOT NULL DEFAULT FALSE,

    rows_written   INTEGER NOT NULL DEFAULT 0,
    rows_failed    INTEGER NOT NULL DEFAULT 0,
    symbols_ok     INTEGER,
    symbols_failed INTEGER,

    error          TEXT,
    details        JSONB,
    hostname       TEXT,
    pid            INTEGER,
    triggered_by   TEXT                    -- 'cron' | 'manual' | 'orchestrator'
);

CREATE INDEX IF NOT EXISTS ix_job_runs_name_date ON job_runs (job_name, session_date DESC);
CREATE INDEX IF NOT EXISTS ix_job_runs_date      ON job_runs (session_date DESC, status);
-- One in-flight run per job per day; a second concurrent cron fire writes nothing.
CREATE UNIQUE INDEX IF NOT EXISTS uq_job_runs_inflight
    ON job_runs (job_name, session_date) WHERE status = 'running';


-- ============================================================================
-- 14. VIEWS
-- ============================================================================

-- The current universe, as every ingestion job should iterate it.
CREATE OR REPLACE VIEW v_active_universe AS
SELECT symbol, company_name, sector, industry, avg_volume_20d,
       in_sp500, in_nasdaq100, in_watchlist
FROM   tickers
WHERE  is_active
ORDER  BY symbol;

-- Per-day job health. `day_complete` is the gate the orchestrator honours: it
-- is FALSE while any critical (snapshot) job for that date is not 'success'.
-- The orchestrator reads THIS, never a hard-coded job list, so adding a
-- critical job cannot leave the rule out of date.
-- Judged by each job's LATEST run, so a successful retry RECOVERS the day.
--
-- The earlier version counted every row for the date, which meant one failed
-- attempt poisoned that day permanently: a killed run on 2026-10-21 left
-- estimates abandoned and chains partial, the re-run succeeded at every step,
-- and `day_complete` stayed false. Gate 4 starts the D7 clock on the first
-- `day_complete = true`, so a bad first night would have stopped the clock
-- starting at all while every step showed green.
--
-- Retry history is kept: all attempts stay in `job_runs`, and `attempts`
-- surfaces the count so a day that needed three goes does not look serene.
-- `attempts` is LAST because CREATE OR REPLACE VIEW may only append columns.
--
-- `partial` still fails the day for a critical job: an incomplete snapshot is a
-- snapshot we do not have (D7/D22), and 431 of 433 chains is a permanent hole.
CREATE OR REPLACE VIEW v_day_status AS
WITH latest AS (
    -- `id` breaks ties when two runs share a started_at.
    SELECT DISTINCT ON (session_date, job_name)
           session_date, job_name, status, is_critical
    FROM   job_runs
    ORDER  BY session_date, job_name, started_at DESC, id DESC
),
counts AS (
    SELECT session_date, count(*) AS attempts
    FROM   job_runs
    GROUP  BY session_date
)
SELECT l.session_date,
       count(*)                                                      AS jobs_run,
       count(*) FILTER (WHERE l.status = 'failed')                    AS jobs_failed,
       count(*) FILTER (WHERE l.status = 'running')                   AS jobs_running,
       count(*) FILTER (WHERE l.is_critical)                          AS critical_jobs,
       count(*) FILTER (WHERE l.is_critical AND l.status = 'success')  AS critical_ok,
       (count(*) FILTER (WHERE l.is_critical) > 0
        AND count(*) FILTER (WHERE l.is_critical AND l.status <> 'success') = 0)
                                                                      AS day_complete,
       c.attempts
FROM   latest l
JOIN   counts c USING (session_date)
GROUP  BY l.session_date, c.attempts;


-- ============================================================================
-- 15. COMPRESSION POLICIES  (D20)
-- ============================================================================
-- Chain snapshots are ~99% of the byte volume: ~500 tickers x 4 expirations x
-- ~40 strikes x 2 types is on the order of 100k+ rows/day, forever (D7 says the
-- history is never deleted). Columnar compression segmented by symbol typically
-- gives 10-20x and keeps the VM's disk affordable.
--
-- The delay before a chunk compresses is deliberately LONGER than any window a
-- job might backfill into, because compressed chunks reject ordinary UPDATEs.
-- Prices compress last: features/technical.py reads long trailing windows and
-- corporate actions can arrive late.
--
-- Syntax note: the ALTER ... SET (timescaledb.compress...) form is accepted by
-- TimescaleDB 2.x including versions that renamed it to "columnstore".
-- add_compression_policy is idempotent via if_not_exists.

ALTER TABLE option_chain_snapshots SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol, option_type',
    timescaledb.compress_orderby   = 'snapshot_ts DESC, expiration, strike'
);
SELECT add_compression_policy('option_chain_snapshots', INTERVAL '14 days',
                              if_not_exists => TRUE);

ALTER TABLE estimate_snapshots SET (
    timescaledb.compress,
    -- period_type is in the PK, so it must appear in segmentby or Timescale
    -- warns that the compressed chunk cannot enforce uniqueness on it.
    timescaledb.compress_segmentby = 'symbol, period_type',
    timescaledb.compress_orderby   = 'snapshot_date DESC, fiscal_period_end'
);
SELECT add_compression_policy('estimate_snapshots', INTERVAL '90 days',
                              if_not_exists => TRUE);

ALTER TABLE iv_daily SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby   = 'ts DESC'
);
SELECT add_compression_policy('iv_daily', INTERVAL '180 days',
                              if_not_exists => TRUE);

ALTER TABLE prices_daily SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby   = 'ts DESC'
);
SELECT add_compression_policy('prices_daily', INTERVAL '365 days',
                              if_not_exists => TRUE);

-- NO RETENTION POLICIES ANYWHERE, on purpose. D7's entire premise is that the
-- snapshot history accumulates; dropping old chunks would delete the edge.
-- `tests/test_schema.py::test_no_retention_policies` fails the build if an
-- `add_retention_policy` call ever appears in this file.


-- ============================================================================
-- 16. updated_at TRIGGERS
-- ============================================================================
-- Attached by discovery rather than a hand-maintained list, so a table added in
-- a later migration with an `updated_at` column gets the trigger for free on the
-- next `python -m db.init`.

DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOR tbl IN
        SELECT c.relname
        FROM   pg_class c
        JOIN   pg_namespace n ON n.oid = c.relnamespace
        JOIN   pg_attribute a ON a.attrelid = c.oid
        WHERE  n.nspname = 'public'
          AND  c.relkind = 'r'
          AND  a.attname = 'updated_at'
          AND  NOT a.attisdropped
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_set_updated_at ON %I', tbl);
        EXECUTE format(
            'CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON %I '
            'FOR EACH ROW EXECUTE FUNCTION set_updated_at()', tbl);
    END LOOP;
END
$$;
