# CLAUDE.md — Investment Decision Engine (Rebuild, Oct 2026)

## What this is
A personal investment decision-support engine for a Schwab brokerage account (swings, long holds, options). It ingests quant + qual data, scores tickers through a 4-pillar confluence model, surfaces complete trade plans (entry/stop/TP1/TP2/R:R/size) in two tiers, tracks every idea in a Shadow Book, and sends a daily email digest. **The human makes every final decision.** Phase 1 = the brain, no UI.

**Context:** this repo is a REBUILD. A prior build completed all 7 scaffold steps (411 tests, live-validated against Schwab and FMP) and was lost when an unpushed Codespace auto-deleted on 2026-10-20. Nothing here is speculative: every convention and finding below was implemented and verified once already. `docs/REBUILD_KICKOFF.md` is the work order; `docs/engine-decisions-and-todo.md` is the authoritative decision log (D1–D39).

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
- **Scoring (D2, reweighted by D32):** Technical 40 / Fundamental 30 / Catalyst 20 / Sentiment 10 → composite 0–100. Weights live in `config/scoring.yaml`, never hard-coded. D32 demoted Sentiment (free-tier news is the thinnest input) and gave the weight to Fundamental ahead of the revision signal; the conflict veto still lets a demoted Sentiment cap an idea, so demotion is not disabling.
- **Ranking (D33):** an absolute score decides nothing. Ideas rank by composite percentile within the universe; the digest surfaces at most `ranking.max_ideas_per_day` by rank. Ranking selects among ideas that already cleared a D12 gate — it never promotes one that failed.
- **Tier A diversity (D34):** agreeing pillars must include Technical plus at least one of Fundamental or Catalyst. Technical + Sentiment is one signal measured twice.
- **Vehicle (D36):** IV rank picks the vehicle on every Tier A idea and is printed on the card — buy optionality below `iv_vehicle_low`, shares in the middle, defined-risk premium selling above `iv_vehicle_high`. Needs the self-built IV rank (D7/D22).
- **Sizing (D37/D38):** adds only once the stop is at breakeven, with total risk capped at the original allocation — initial risk never rises. Max 2 concurrent swings per sector; the third surfaces watch-only.
- **Allocation (D35):** two sleeves with separate scoreboards — Core 65% (ETFs + holds, benchmarked to its blend) and Growth 35% (swings + options + tactical cash, benchmarked to SPY total return). Quarterly one-way ratchet sweeps half of any excess above 3 points into Core permanently; drawdowns are never replenished mid-quarter.
- **Idea gating (D12):** Tier A = ≥3 pillars agree AND composite ≥70, min R:R 2:1. Tier B = compelling asymmetry, 2 pillars or composite 55–69, min R:R 3:1, reduced size.
- **Risk (D17/D18):** Tier A 2.5% of account per trade; Tier B 0.75%/idea with hybrid monthly budget (4% base at entry, 50% of realized Tier B gains replenish, 6% ceiling, calendar month); max 6 concurrent swings; total open heat ≤10% (active trades only; holds/ETFs excluded, they run on thesis triggers with no hard stops); options defined-risk only, 5%/mo realized-loss budget, 3% max per position; circuit breaker (2 losing weeks or −5% month → Tier B paused, Tier A halved until a winning week). Buckets: ETF 35 / holds 30 / swings 20 / options 10 / cash 5; risk-off (SPY<200MA + VIX>25) → swings 10, options 5, cash 20+. Single name ≤10%, sector ≤30%. All in `config/risk.yaml`.
- **Dividends (D19):** total-return measurement everywhere; dividend-adjusted stops; no new swing long within 2 days of ex-date; flag short-call assignment risk when extrinsic (use `timeValue`) < dividend × buffer; dividend cut = thesis-review trigger; ETF DRIP on, hold dividends → cash bucket.
- **Shadow Book (D13):** every surfaced idea (Tier A, Tier B, and discretionary) logged with projections at time of call, tracked to outcome whether or not taken. `shadow_book.symbol` has NO FK to `tickers` (discretionary ideas may be off-universe). North-star metric = expectancy per recipe and tier.
- **Alerts (D16, amended Oct 21 2026):** daily email digest, Tier A and Tier B clearly separated, PLUS Telegram as the page channel. `alerts/notify.py` fans out to whatever is configured; `alerts/telegram.py` and `alerts/mailer.py` never raise, sit behind the D27 gate, refuse under pytest, and write one `alerts` row per channel with a namespaced dedupe key. Telegram's token travels in the URL (redact it everywhere) and 4096 chars is a hard limit Telegram rejects rather than truncates (split on line boundaries).
- **Network consent (D27):** `core/netguard.py`. No live network call without `ALLOW_LIVE_CALLS=1` on the invocation (CLI/cron, never `.env`). Inside pytest the answer is always no, flag or not. The gate sits ahead of the rate limiter and transport in every client. A blocked call is a failed job step, never a silent zero-row success.
- **Exits:** per-strategy shape enforced by the loader. `swing` has `max_holding_days` (45, [OPEN]); `option` times out at expiration (`max_holding_days` must be null); `hold` exits on thesis triggers only (null). A day-count on options or holds is a hard config error.
- **Promotion ladder (D40):** every recipe carries a status — `shadow` (surfaced, tracked, FLAGGED NOT ACTIONABLE), `paper` (also sized against a virtual account with the full D17/D18 stack), `live` (the human trades it). New strategies always enter `shadow`, backtest or no backtest. SHADOW→PAPER needs ≥30 closed ideas with positive expectancy; PAPER→LIVE needs sustained expectancy plus calibration within tolerance; demotion is automatic. Thresholds are `[OPEN]` in `scoring.promotion`, and the loader rejects a ladder that contradicts itself. `shadow_book.status_at_call` stamps every idea — expectancy mixed across a boundary averages two different strategies and makes the rule circular.
- **STANDING CONVENTION for every `features/` module: insufficient data returns `None` — never a number, never `False`.** The scoring layer owns the meaning of abstention. A 200-day MA from 40 bars, a YoY growth rate against a quarter that does not exist, or `stacked=False` because the slow MA is unknown are all plausible-looking wrong answers that propagate into a composite with nothing to flag them. "We cannot see X" and "X is absent" are different claims.
- **Evidence depth (`features/evidence.py`):** any feature computed from a series the ENGINE accumulates — IV history (D7/D22), estimate snapshots — reports an `EvidenceDepth` beside its value: distinct observations AND calendar span, which differ (ten snapshots in a week ≠ ten over a year). Without it, the Shadow Book cannot record how much evidence an idea stood on, and D39's calibration work could never ask whether thin evidence predicts unreliability. Two kinds of thin, deliberately not collapsed: **mathematically undefined** (an IV rank needs a high and a low, so `features/iv.py` gates hard on `iv_rank_min_history_days` and records `iv_daily.history_days`), versus **correct but noisy** (a consensus moving 2.00 → 2.39 across two snapshots moved exactly that much, so revision momentum reports the value and lets depth carry the uncertainty). Never fold depth into the value — a feature that quietly shrank its own number on thin evidence would be making a scoring decision.
- **Pillar A inputs (`features/technical.py`):** split-only adjusted prices (D24), newest bar LAST (a descending frame is refused, not measured backwards), and **insufficient history returns None, never a number** — a 200-day MA from 40 bars is a plausible-looking wrong answer that propagates into a composite undetectably. RSI is Wilder's, because the recipe's 40-50 band was written against what a charting package shows. Relative volume excludes today from its own baseline. Relative strength vs sector (D33) is a return DIFFERENCE, not a ratio, which is unstable exactly in the quiet-sector case that matters most.
- **[OPEN] parameters (D25):** every number no decision fixes lives in config tagged `# [OPEN]`, with a row in `docs/open-parameters.md` and a sync test. Changing a decision-fixed value without a decision fails a named test.

## FMP PREMIUM (current, from 2026-10-21). Free-tier mode is the fallback.
The account is on `/stable/` **Premium**: probe 15/15, all five formerly-402 endpoints pass. `/api/v3/` now answers 403 (key rejected) — not merely ungated but unavailable, which is fine since the client is `/stable/`-only.
- **The estimate-revision clock started 2026-10-21.** First `analyst-estimates` snapshot stored that day. No history exists before it and none can be bought. D32's Fundamental weight of 30 rides on a series that is still shallow.
- Budget `fmp.daily_call_budget` = **9000**, limiter `FMP_RATE_LIMIT_PER_MIN` = **600** (Premium allows 750). The budget's job changed: not "stay inside the allowance" but "stop a runaway loop before it makes 200,000 calls". Keep it.
- A cold start costs ~6,759 calls: universe 527 + estimates 1,039 + fundamentals 5,190 + events 3.
- **Quote endpoints are SPLIT and the split is silent.** One symbol: `/stable/quote?symbol=X`. Many: `/stable/batch-quote?symbols=A,B` — note the PLURAL parameter; `symbol` there is a 400. Passing a comma list to `/stable/quote` returns HTTP 200 and `[]`. The free tier 402'd that same request, which hid the bug for the whole free-tier period. `quotes()` raises on a wholly empty batch.
- **A 402 does NOT prove a plan gate.** `/stable/ratios?symbol=BRK.B` 402s while `?symbol=AAPL` 200s, and an unknown symbol returns 200 + `[]`. `_optional` therefore confirms with a known-good canary symbol before marking an endpoint unavailable — inferring a gate from one symbol's 402 would NULL the valuation columns for every remaining symbol under a log line naming the wrong cause.
- Retained fallbacks (a plan can lapse; billing can 402 an endpoint overnight): `config/constituents_static.txt` stays maintained and tested; `fmp_fundamentals` keeps degrade-on-402 with gaps in `job_runs.unavailable_endpoints`; `fmp_estimates` keeps the probe that sets its own `is_critical`.
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
features/        adjustment.py, iv.py, evidence.py, technical.py, fundamental.py, (later: catalyst.py, sentiment.py)
scoring/         pillars.py, composite.py, recipes/
decision/        idea_builder.py, tiering.py, risk_manager.py
shadow_book/     logger.py, outcome_tracker.py, analytics.py
alerts/          notify.py (the facade), mailer.py, telegram.py, store.py, email_digest.py
jobs/            daily_close.py, snapshot jobs
backtest/        (Phase 2)
scripts/         fmp_probe.py, schwab_auth.py, sync_token.sh, backup_db.sh, token_expiry_check.py, telegram_test.py
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
- Never commit secrets. `.gitignore` blocks `.env*` (except `.env.example`), `*token*.json`, `data/*`. `.env.example` documents every variable (FMP_API_KEY, FINNHUB_API_KEY, SCHWAB_APP_KEY, SCHWAB_APP_SECRET, SCHWAB_CALLBACK_URL=https://127.0.0.1, SCHWAB_TOKEN_PATH=data/schwab_token.json, DB vars incl. separate admin creds, SMTP + ALERT_EMAIL_ONFAIL, TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID).
- Idempotent ingestion (re-running a day upserts, never duplicates). Rate limiter is a sliding window, not a token bucket, shared via core. `for_each_symbol` fails a job above 25% symbol-failure rate with a 10-symbol minimum sample; total failure always raises regardless of sample size (never "ok, 0 rows" when everything failed).
- Scoring logic unit-testable with fixture DataFrames, no network. Integration tests only under `pytest -m integration`.
- Prefer boring, explicit code. This runs unattended.

## What NOT to do
- No UI work in Phase 1. No auto-trading or order placement; read-only broker access only.
- No hard-coded weights, thresholds, or risk numbers; config files only.
- No DELETE against the four history tables, ever.
- No live network call without explicit per-invocation consent (D27).
- Don't skip the Shadow Book "because it's just logging." It is the learning loop.
