# CLAUDE.md — Investment Decision Engine (Rebuild, Oct 2026)

## What this is
A personal investment decision-support engine for a Schwab brokerage account (swings, long holds, options). It ingests quant + qual data, scores tickers through a 4-pillar confluence model, surfaces complete trade plans (entry/stop/TP1/TP2/R:R/size) in two tiers, tracks every idea in a Shadow Book, and sends a daily email digest. **The human makes every final decision.** Phase 1 = the brain, no UI.

**Context:** this repo is a REBUILD. A prior build completed all 7 scaffold steps (411 tests, live-validated against Schwab and FMP) and was lost when an unpushed Codespace auto-deleted on 2026-10-20. Nothing here is speculative: every convention and finding below was implemented and verified once already. `docs/REBUILD_KICKOFF.md` is the work order; `docs/engine-decisions-and-todo.md` is the authoritative decision log (D1–D31).

## Non-negotiable process rule (D30)
**Commit and push to GitHub at the end of every working session, and after every completed step. No exceptions.** The repo on GitHub is the system of record; a Codespace is disposable. Never report a step complete without naming the commit that was pushed.

## Locked decisions that constrain code (full detail in the decision log)
- **Data sources:** Schwab Trader API (prices, option chains, positions, via `schwab-py`), Financial Modeling Prep on `/stable/` (fundamentals, estimates, calendars; currently FREE tier, see free-tier mode below), Finnhub free tier (news), SEC EDGAR + FRED (free), Claude API (transcript tone scoring, Phase 2).
- **Universe (D14):** S&P 500 + Nasdaq 100 constituents, avg 20-day volume >1M, active options chain, plus `config/watchlist.txt`. ~500 names. While FMP constituents endpoints are plan-gated, the universe builds from `config/constituents_static.txt` (manually sourced, dated header) with a logged warning.
- **Cadence (D15):** daily bars only in Phase 1. Jobs run post-close (~4:30 PM ET) on trading days. Host TZ = America/New_York for cron; DB stores UTC.
- **Snapshots (D7/D22):** every trading day, store option chains (strikes within ±15% of spot, nearest 4 MONTHLY expirations, per-ticker aggregate IV) and FMP analyst estimates (when the plan allows). **These histories cannot be bought back. Snapshot jobs are is_critical and must never silently fail.**
- **Database (D20):** Postgres (≥15, for UNIQUE NULLS NOT DISTINCT) + TimescaleDB. Hypertables: `prices_daily`, `option_chain_snapshots`, `estimate_snapshots`, `iv_daily`. Relational: `tickers`, `fundamentals`, `corporate_actions`, `events`, `news_articles`, `ideas`, `shadow_book`, `positions`, `alerts`, `job_runs`, `schema_migrations`.
- **Prices (D24):** `prices_daily` is append-only raw OHLCV. No stored adjusted columns. `corporate_actions` is the immutable source of truth; `features/adjustment.py` computes factors at read time with an `as_of` date, in two modes: `split_only` (technical structure) and `total_return` (performance, backtests, dividend-adjusted stops).
- **Append-only enforcement:** a static test fails on any DELETE (or ON CONFLICT DO NOTHING) against `prices_daily`, `option_chain_snapshots`, `estimate_snapshots`, `iv_daily`. Writers use ON CONFLICT DO UPDATE.
- **Scoring (D2):** Technical 35 / Fundamental 25 / Catalyst 20 / Sentiment 20 → composite 0–100. Weights live in `config/scoring.yaml`, never hard-coded.
- **Idea gating (D12):** Tier A = ≥3 pillars agree AND composite ≥70, min R:R 2:1. Tier B = compelling asymmetry, 2 pillars or composite 55–69, min R:R 3:1, reduced size.
- **Risk (D17/D18):** Tier A 2.5% of account per trade; Tier B 0.75%/idea with hybrid monthly budget (4% base at entry, 50% of realized Tier B gains replenish, 6% ceiling, calendar month); max 6 concurrent swings; total open heat ≤10% (active trades only; holds/ETFs excluded, they run on thesis triggers with no hard stops); options defined-risk only, 5%/mo realized-loss budget, 3% max per position; circuit breaker (2 losing weeks or −5% month → Tier B paused, Tier A halved until a winning week). Buckets: ETF 35 / holds 30 / swings 20 / options 10 / cash 5; risk-off (SPY<200MA + VIX>25) → swings 10, options 5, cash 20+. Single name ≤10%, sector ≤30%. All in `config/risk.yaml`.
- **Dividends (D19):** total-return measurement everywhere; dividend-adjusted stops; no new swing long within 2 days of ex-date; flag short-call assignment risk when extrinsic (use `timeValue`) < dividend × buffer; dividend cut = thesis-review trigger; ETF DRIP on, hold dividends → cash bucket.
- **Shadow Book (D13):** every surfaced idea (Tier A, Tier B, and discretionary) logged with projections at time of call, tracked to outcome whether or not taken. `shadow_book.symbol` has NO FK to `tickers` (discretionary ideas may be off-universe). North-star metric = expectancy per recipe and tier.
- **Alerts (D16):** daily email digest, Tier A and Tier B clearly separated.
- **Network consent (D27):** `core/netguard.py`. No live network call without `ALLOW_LIVE_CALLS=1` on the invocation (CLI/cron, never `.env`). Inside pytest the answer is always no, flag or not. The gate sits ahead of the rate limiter and transport in every client. A blocked call is a failed job step, never a silent zero-row success.
- **Exits:** per-strategy shape enforced by the loader. `swing` has `max_holding_days` (45, [OPEN]); `option` times out at expiration (`max_holding_days` must be null); `hold` exits on thesis triggers only (null). A day-count on options or holds is a hard config error.
- **[OPEN] parameters (D25):** every number no decision fixes lives in config tagged `# [OPEN]`, with a row in `docs/open-parameters.md` and a sync test. Changing a decision-fixed value without a decision fails a named test.

## Free-tier FMP mode (current, D29 amended)
FMP upgrade is deferred. The account is on `/stable/` (symbol in query string, NOT in path; several endpoint names differ from v3). Plan-gated (402) on free: `sp500-constituent`, `nasdaq-constituent`, `analyst-estimates`, `ratios`, `key-metrics`. Therefore:
- Universe builds from `config/constituents_static.txt` fallback when constituents 402.
- `fmp_fundamentals` degrades on 402: core statements populate what they can; valuation columns NULL, gaps reported in `job_runs.unavailable_endpoints`. One 402 must not fail the whole symbol.
- `fmp_estimates` is marked expected-blocked in the orchestrator (known gap, not failure; `day_complete` can be true without it). It flips back to is_critical the day the endpoint passes a probe. **The estimate-revision history clock starts on upgrade day; record that date.**
- A daily FMP call-budget guard (250/day, [OPEN]) keeps the fundamentals crawl trickling via the staleness filter.
- `scripts/fmp_probe.py` probes BOTH API generations across every endpoint the client calls and distinguishes 402 (plan) from 404 (path). Re-run after any plan change.

## Validated API findings (do not rediscover these)
**FMP `/stable/`:** quote at `/stable/quote?symbol=X`; `calendarYear` is now `fiscalYear` (accept both); market-wide calendars cost 3 calls/day; fundamentals cost 5 calls/symbol, refreshed on staleness (>7 days or reported since), not schedule; estimates must drop already-reported periods before diffing.
**Schwab (schwab-py 1.5.x, live-validated 2026-09-03):**
- `expirationType` has FOUR values: S=standard monthly (third Friday, what D22 means), W=weekly, Q=quarter-end, M=MONTH-END. `M` is a trap: it is not "monthly." Filter on S (startswith is fine), with third-Friday heuristic as fallback; both agreed on all 23 live expirations.
- `extrinsicValue` is **unusable by construction — it returns distance-from-the-money, not extrinsic value.** Diagnosed 2026-10-20 on a live SPY chain: a Jan-2027 put at strike 50 with spot 771.38 is marked 0.01 with `timeValue` 0.01 and reports `extrinsicValue` 721.39, i.e. `spot - strike` to the cent. 5,247 of 8,214 contracts disagree with `timeValue` (63.9%), and 4,113 report a value larger than their own mark, which is arithmetically impossible. It agrees on ITM contracts, so an ATM spot check misses it entirely. Use `timeValue` for extrinsic, always. Keep the regression test that pins `timeValue` and the fixture carrying the wrong `extrinsicValue` so a future switch to the better-named field fails.
- Refuse chains with `isChainTruncated` or `isDelayed` (a truncated surface stored whole is a permanent undetectable lie). Exclude `nonStandard`/mini contracts.
- IV arrives as percent → convert to decimal at the boundary. `-999.0` sentinel → NULL. ATM = nearest strike, not delta-0.50. `interpolate_iv30` never extrapolates past the curve ends. `expirationDate` is an ISO string, not epoch ms.
- Auth: `client_from_token_file`, never `easy_client` (hangs headless). Access token 30 min, refresh token 7 days, then full re-auth (manual copy-paste flow; the port-less `https://127.0.0.1` callback cannot bind a local server, and schwab-py's `client_from_manual_flow` is the settled mechanism). Redact `code=`/`session=`/tokens from all error output; clamp HTTP loggers to WARNING during auth. Throttle to ~100/min (limit 120). Live IV can print absurd values (seen 2473%) on 0-DTE/far strikes; the D22 filter already excludes them, no clamp.
- Sanity relationships that validated the whole pipeline once and should again: SPY IV30 below single names, SPY 25d skew positive vs ~0 for MSFT, SPY put/call OI far above single names.
- `schwab_chains` writes `tickers.has_options` true/false from whether a chain returned; that arms the D14 options gate (which abstains on None).

## Repo layout
```
config/          scoring.yaml, risk.yaml, universe.yaml, watchlist.txt, constituents_static.txt
core/            settings.py (only reader of os.environ), db.py, clock.py, ratelimit.py, jobs.py, config.py, netguard.py
db/              schema.sql, init.py, migrations/
ingest/          fmp_client.py, universe.py, fmp_fundamentals.py, fmp_estimates.py, fmp_events.py,
                 finnhub_client.py, finnhub_news.py, schwab_client.py, schwab_prices.py, schwab_chains.py, runner.py
features/        adjustment.py, iv.py, (later: technical.py, fundamental.py, catalyst.py, sentiment.py)
scoring/         pillars.py, composite.py, recipes/
decision/        idea_builder.py, tiering.py, risk_manager.py
shadow_book/     logger.py, outcome_tracker.py, analytics.py
alerts/          mailer.py, email_digest.py
jobs/            daily_close.py, snapshot jobs
backtest/        (Phase 2)
scripts/         fmp_probe.py, schwab_auth.py, sync_token.sh, backup_db.sh, token_expiry_check.py
tests/           (fixtures/ includes schwab_payloads.py, live-validated)
docs/            engine-decisions-and-todo.md (canonical log), open-parameters.md, REBUILD_KICKOFF.md
```

## Schema conventions (all previously verified against live PG16 + Timescale 2.29)
- Dual time keying on every time-series table: `ts TIMESTAMPTZ` (UTC, hypertable dimension) + `session_date DATE` (ET trading day, the join key).
- Compression policies staggered (chains 14d, estimates 90d, iv 180d, prices 365d), each delay longer than any backfill window. **No retention policies anywhere**, locked by `test_no_retention_policies`.
- `job_runs` with `is_critical`; `v_day_status` view derives `day_complete` by query (orchestrator reads the view, never a hard-coded job list). Partial unique index: one `running` row per job per day. `job_run` closes from `finally` catching BaseException; `clear_stale_runs()` for kill -9.
- `tickers` rows deactivate (`is_active=false` + `deactivated_on`), never delete. `UNIQUE NULLS NOT DISTINCT` on `events` and `positions`. `fundamentals.raw JSONB` escape hatch. TEXT + CHECK instead of native ENUMs. `news_articles` PK `(source_provider, article_id, symbol)`.
- A dividend writes to BOTH `events` (radar) and `corporate_actions` (adjustment log), with a round-trip test proving the parser and `features/adjustment.py` agree.

## Conventions
- Python 3.11+, `requirements.txt` + `requirements-dev.txt` split, `psycopg[binary]` v3, pydantic v2, `requests` + `tenacity` (no provider SDKs for FMP/Finnhub), `schwab-py`, pandas/numpy.
- ruff: py311, line 100, rules E/W/F/I/UP/B/C4/SIM/RET/PTH/DTZ/T20/ARG/RUF. DTZ is deliberate (bans naive datetimes). T20 waived for jobs/, scripts/, ingest/, db/init.py.
- Never commit secrets. `.gitignore` blocks `.env*` (except `.env.example`), `*token*.json`, `data/*`. `.env.example` documents every variable (FMP_API_KEY, FINNHUB_API_KEY, SCHWAB_APP_KEY, SCHWAB_APP_SECRET, SCHWAB_CALLBACK_URL=https://127.0.0.1, SCHWAB_TOKEN_PATH=data/schwab_token.json, DB vars incl. separate admin creds, SMTP + ALERT_EMAIL_ONFAIL).
- Idempotent ingestion (re-running a day upserts, never duplicates). Rate limiter is a sliding window, not a token bucket, shared via core. `for_each_symbol` fails a job above 25% symbol-failure rate with a 10-symbol minimum sample; total failure always raises regardless of sample size (never "ok, 0 rows" when everything failed).
- Scoring logic unit-testable with fixture DataFrames, no network. Integration tests only under `pytest -m integration`.
- Prefer boring, explicit code. This runs unattended.

## What NOT to do
- No UI work in Phase 1. No auto-trading or order placement; read-only broker access only.
- No hard-coded weights, thresholds, or risk numbers; config files only.
- No DELETE against the four history tables, ever.
- No live network call without explicit per-invocation consent (D27).
- Don't skip the Shadow Book "because it's just logging." It is the learning loop.
