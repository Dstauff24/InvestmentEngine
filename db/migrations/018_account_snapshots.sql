-- O17: the account's own numbers, once a day.
--
-- `positions` already holds what is HELD. Nothing holds what the ACCOUNT is —
-- its value, its cash, its type — and every question the portfolio-adviser
-- layer exists to answer needs that: drift is a percentage OF something,
-- concentration is a share OF something, and "prefer rebalancing through new
-- contributions" is a statement about cash arriving.
--
-- ONE ROW PER ACCOUNT PER DAY, append-and-update like `positions`: the series
-- is what makes drift readable over time, and a single mutable row would lose
-- the only history that shows a sleeve drifting.
--
-- **`tax_type` IS NOT POPULATED BY THE SYNC.** Schwab's account `type` says
-- CASH or MARGIN, which is a settlement mode and tells you nothing about tax
-- treatment; a taxable brokerage and a Roth IRA both come back as CASH. So the
-- tax type is OPERATOR-SUPPLIED configuration, and it is NULL until somebody
-- states it — a guess here would produce advice about someone else's tax bill
-- (D52: a guess in this layer is advice). The adviser REFUSES to recommend a
-- sale in an account whose tax type it does not know, rather than assuming the
-- cheaper answer.
--
-- `account_hash` is Schwab's own hash, which is what the API returns. The plain
-- account number is never fetched and therefore cannot be stored.

CREATE TABLE IF NOT EXISTS account_snapshots (
    as_of_date        DATE NOT NULL,
    account_hash      TEXT NOT NULL,

    -- Schwab's settlement type (CASH / MARGIN), kept as reported.
    account_type      TEXT,
    -- OPERATOR-SUPPLIED (O17): taxable, traditional_ira, roth_ira, 401k, hsa,
    -- other. NULL means "not stated", and the adviser treats that as a refusal
    -- to advise rather than as a default.
    tax_type          TEXT CHECK (tax_type IS NULL OR tax_type IN (
                          'taxable', 'traditional_ira', 'roth_ira',
                          '401k', 'hsa', 'other')),

    total_value       NUMERIC(20, 2),
    cash              NUMERIC(20, 2),
    long_market_value NUMERIC(20, 2),
    buying_power      NUMERIC(20, 2),
    -- Settled vs unsettled matters for "can this contribution be deployed",
    -- which is the first question the rebalance-by-contribution rule asks.
    cash_available    NUMERIC(20, 2),

    source            TEXT NOT NULL DEFAULT 'schwab',
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (as_of_date, account_hash)
);

CREATE INDEX IF NOT EXISTS ix_account_snapshots_latest
    ON account_snapshots (account_hash, as_of_date DESC);

COMMENT ON TABLE account_snapshots IS
    'O17: one row per account per day — value, cash and type. `tax_type` is '
    'OPERATOR-SUPPLIED and NULL until stated, because Schwab reports settlement '
    'mode (CASH/MARGIN) and not tax treatment, and an adviser that guessed would '
    'be giving advice about someone else''s tax bill.';
