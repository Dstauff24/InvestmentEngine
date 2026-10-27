-- O19: a date before which a symbol's stored bars cannot be joined to today's.
--
-- D24 says `prices_daily` is RAW and `corporate_actions` is the immutable
-- source of truth, and `price_basis_check` proves it daily by arithmetic: a raw
-- series STEPS by the split ratio at the ex-date. Over the 18-year backfill 166
-- of 167 unambiguous splits step exactly as logged, so the assumption holds.
--
-- The one exception is a different failure from the Schwab one, and it needs a
-- different answer. WY's 2010-07-20 REIT conversion paid a $26.42 special
-- dividend ~90% in STOCK; FMP logged it as a plain 2:1 split, and its raw
-- series steps 5.2484. Its own split-ADJUSTED series divides the pre-period by
-- exactly 2.0 and still leaves a 2.62x cliff, which is the discovery: FMP's
-- `non-split-adjusted` feed is the ADJUSTED feed with the split log reversed
-- back out, so a wrong log entry corrupts BOTH series, in opposite directions.
-- Re-sourcing cannot fix it — there is no uncontaminated series to fetch.
--
-- SO THE ERA IS REFUSED RATHER THAN REPAIRED. The alternative is to infer the
-- true ratio from the observed step, which would use the price to fix the
-- price, and the step is not even decomposable here: a stock dividend and a
-- cash dividend went ex on the same date, so no split of 5.2484 between them
-- is identifiable from the bars. The standing `features/` convention applies —
-- a plausible-looking wrong number is worse than a refusal — and a 2.6x error
-- in an S&P 500 name's pre-2010 history would silently reprice every lab
-- result and backtest that reaches back through it.
--
-- A RECORDED BREAK IS A KNOWN STATE, NOT A FAILURE (as a preflight SKIP is).
-- `price_basis_check` fails on violations that are NOT recorded here and prints
-- the recorded ones; `features.technical.load_bars` drops bars before the
-- break, so the refusal reaches every reader through the one chokepoint the
-- lab, the backtest engine and `backtest.regime` already share. The symbol
-- keeps its post-break history, which is correct and was never in doubt.
--
-- Relational, not a hypertable: a handful of rows for ever. It is a statement
-- ABOUT history rather than history, so UPDATE is allowed and the four
-- append-only tables are untouched.

CREATE TABLE IF NOT EXISTS price_basis_breaks (
    symbol        TEXT        NOT NULL,
    break_date    DATE        NOT NULL,
    logged_ratio  NUMERIC(18, 8),
    observed_step NUMERIC(18, 8),
    reason        TEXT        NOT NULL,
    recorded_on   DATE        NOT NULL DEFAULT CURRENT_DATE,
    PRIMARY KEY (symbol, break_date)
);

COMMENT ON TABLE price_basis_breaks IS
    'Dates before which a symbol''s stored bars are on an unrecoverable basis '
    '(O19). Bars from `break_date` forward are good; earlier ones are dropped '
    'by `features.technical.load_bars`. A row here turns a `price_basis_check` '
    'violation into a known state, so every break is deliberate and visible.';

COMMENT ON COLUMN price_basis_breaks.observed_step IS
    'close_before / close_after at the ex-date, i.e. what the bars actually '
    'did, beside `logged_ratio`, which is what `corporate_actions` claims. The '
    'two disagreeing IS the break.';
