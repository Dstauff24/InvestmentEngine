-- Insider transactions from SEC Form 4 (D39: insider clusters as a Pillar C input).
--
-- POINT-IN-TIME HONESTY IS THE WHOLE DESIGN. A Form 4 becomes known to the
-- market when the SEC ACCEPTS it, not when the trade happened, and the two can
-- differ by two business days. Scoring or backtesting on `transaction_date`
-- would leak information nobody had — a backtest would "see" a purchase up to
-- two days before anyone could have acted on it, which is exactly the kind of
-- lookahead that makes a strategy look profitable and then lose money live.
--
-- So BOTH are stored, and every as_of read keys on `filed_at`:
--
--   filed_at          when the SEC accepted the filing. The knowledge moment.
--   filed_session     ET trading day of filed_at, for joining to daily series.
--   transaction_date   when the insider actually traded. Descriptive only.
--
-- NOT a hypertable: D20/CLAUDE.md fix the hypertable list at four, and filings
-- are sparse rather than a dense daily series.
--
-- Immutable in practice: an amended Form 4 is filed under a NEW accession
-- number rather than editing the old one, so the upsert exists for re-fetch
-- idempotence and never to rewrite history.

CREATE TABLE IF NOT EXISTS insider_transactions (
    accession_number   TEXT NOT NULL,
    txn_index          INTEGER NOT NULL,
    symbol             TEXT NOT NULL REFERENCES tickers (symbol),
    issuer_cik         TEXT,

    -- The knowledge moment. NOT NULL on purpose: a row whose filing time is
    -- unknown cannot be read point-in-time, so it must not be stored at all.
    filed_at           TIMESTAMPTZ NOT NULL,
    filed_session      DATE NOT NULL,
    transaction_date   DATE,

    insider_cik        TEXT,
    insider_name       TEXT,
    insider_title      TEXT,
    is_director        BOOLEAN,
    is_officer         BOOLEAN,
    is_ten_percent_owner BOOLEAN,

    -- SEC transaction codes. 'P' is an OPEN-MARKET PURCHASE, the only code the
    -- cluster rule counts: 'A' is a grant or award, 'M' an option exercise,
    -- 'F' shares withheld for tax. Treating those as conviction buying is the
    -- classic way to build an insider signal that measures compensation
    -- schedules instead of opinion. Stored unfiltered so the rule can change
    -- in config without a re-fetch.
    transaction_code   TEXT,
    acquired_disposed  TEXT CHECK (acquired_disposed IS NULL
                                   OR acquired_disposed IN ('A', 'D')),
    shares             NUMERIC(20, 4),
    price_per_share    NUMERIC(18, 6),
    value_usd          NUMERIC(20, 2),
    shares_owned_after NUMERIC(20, 4),

    -- A 10b5-1 plan sale was scheduled months earlier and carries no opinion
    -- about today. Recorded so SALE context can discount it; purchases under a
    -- plan are rare but equally uninformative.
    is_10b5_1          BOOLEAN,

    security_title     TEXT,
    is_derivative      BOOLEAN NOT NULL DEFAULT FALSE,
    source             TEXT NOT NULL DEFAULT 'sec_edgar',
    ingested_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_insider_transactions PRIMARY KEY (accession_number, txn_index)
);

-- The cluster query: one symbol, purchases, within a rolling window of FILING
-- time. Ordered by filed_at so the window scan is a range scan.
CREATE INDEX IF NOT EXISTS ix_insider_txn_symbol_filed
    ON insider_transactions (symbol, filed_at DESC);
CREATE INDEX IF NOT EXISTS ix_insider_txn_purchases
    ON insider_transactions (symbol, filed_session DESC)
    WHERE transaction_code = 'P' AND NOT is_derivative;

COMMENT ON COLUMN insider_transactions.filed_at IS
    'When the SEC ACCEPTED the filing — the moment the market could know. '
    'Every as_of read and every backtest keys on this, never on '
    'transaction_date, which can precede it by two business days.';
COMMENT ON COLUMN insider_transactions.transaction_date IS
    'When the insider traded. DESCRIPTIVE ONLY — keying a read on this leaks '
    'information nobody had yet.';
