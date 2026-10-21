-- Step 10: benchmark symbols. SPY has to exist before a regime can be tagged.
--
-- The universe (D14) is S&P 500 + Nasdaq 100 constituents, which excludes the
-- index ETFs — so `prices_daily` held no market series at all, and three
-- separate decisions need one:
--
--   D17  risk-off is defined as "SPY below its 200-day MA and VIX above 25".
--   D35  the Growth sleeve is benchmarked to SPY total return.
--   D39  a backtest reports expectancy PER REGIME, and the regime is a fact
--        about the market rather than about the symbol being traded.
--
-- A benchmark is NOT a candidate. It carries prices and nothing else: no
-- chains, no fundamentals, no news, no score, no idea. `v_active_universe` is
-- what every ingestion and scoring job iterates, so excluding benchmarks there
-- is what keeps an ETF out of the swing recipes — while `is_active` stays TRUE
-- so the row is not mistaken for a delisting (`deactivated_on` means something
-- specific).
--
-- Putting it in `tickers` rather than a table of its own is deliberate:
-- `prices_daily.symbol` has a foreign key to `tickers`, and a second home for
-- price-bearing symbols would need a second price table or a broken FK.

ALTER TABLE tickers
    ADD COLUMN IF NOT EXISTS is_benchmark BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN tickers.is_benchmark IS
    'A market series carried for D17 risk-off, D35 sleeve benchmarking and '
    'D39 regime tagging. Excluded from v_active_universe, so it is never '
    'scored, never ingested for chains or fundamentals, and never surfaced as '
    'an idea. ingest/schwab_prices.py iterates active universe + benchmarks.';

-- Rebuilt to exclude benchmarks. CREATE OR REPLACE rather than DROP: the
-- column list is unchanged, and dropping a view other objects may depend on is
-- a bigger operation than this needs to be.
CREATE OR REPLACE VIEW v_active_universe AS
SELECT symbol, company_name, sector, industry, avg_volume_20d,
       in_sp500, in_nasdaq100, in_watchlist
FROM   tickers
WHERE  is_active AND NOT is_benchmark
ORDER  BY symbol;

CREATE INDEX IF NOT EXISTS ix_tickers_benchmark
    ON tickers (symbol) WHERE is_benchmark;

-- SPY itself. `passes_liquidity` stays FALSE: the D14 gate is about swing
-- candidates, and leaving it false means a benchmark cannot leak into
-- `sector_members` or any other query that filters on it.
INSERT INTO tickers (symbol, company_name, exchange, is_benchmark, is_active,
                     passes_liquidity, source)
VALUES ('SPY', 'SPDR S&P 500 ETF Trust', 'NYSEARCA', TRUE, TRUE, FALSE, 'benchmark')
ON CONFLICT (symbol) DO UPDATE SET
    is_benchmark     = TRUE,
    is_active        = TRUE,
    deactivated_on   = NULL,
    passes_liquidity = FALSE;
