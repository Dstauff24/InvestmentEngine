# Investment Engine — Decision Log & To-Do Tracker
**v2.7 (D40 promotion ladder; scoring brain begun) — reconstructed Oct 20, 2026 after loss of the original repo. This file is canonical (D26); the design chat supplies paste-ready entries.**

Companion: `CLAUDE.md` (conventions + validated findings), `docs/open-parameters.md`, `docs/REBUILD_KICKOFF.md`.

---

## WHAT HAPPENED (session note, Oct 20)
The original build completed all 7 kickoff steps between Aug 31 and Sep 3, 2026: 411 unit tests + 9 integration tests, live-validated against FMP `/stable/` and the Schwab Trader API. The work lived only in a GitHub Codespace and was never committed or pushed. The Codespace auto-deleted after ~30 days of inactivity; the repo on GitHub held only a README. The prior Claude Code session was recovered and can be resumed, but its filesystem is empty. Nothing irreplaceable was lost: the D7 snapshot clock had not started (no VM, no cron), the Schwab token had already expired (7-day life, created Sep 3), and FMP was never upgraded. Rebuild proceeds from this log + CLAUDE.md + the resumed session's own transcript.

---

## ✅ REBUILD COMPLETE (Oct 20, 2026)

All seven kickoff steps rebuilt and pushed. **Final commit: `426cde7`** (`step 7: daily_close
orchestrator, mailer, backup + token expiry ops`). Every step ended in a pushed commit per D30:

| Step | Commits | What landed |
|------|---------|-------------|
| 0 | `c1be523` | Rebuild spec: CLAUDE.md + this log + open-parameters + REBUILD_KICKOFF, pushed before any code existed |
| 1 | `34418dc` | Scaffold, `.gitignore`, `.env.example` (38 vars), requirements split, ruff/pytest config, README |
| 2 | `19037e0`, `66a10be` | 519-symbol static constituents; `db/schema.sql` (16 tables, 4 hypertables, 2 views) + idempotent `db/init.py`, verified against real Postgres 16 + TimescaleDB 2.31 |
| 3 | `873adbc`, `e703758` | Destructive-database guard for the integration suite; three-layer config loader, cross-field invariants, 31 `[OPEN]` values with a sync test against `open-parameters.md` |
| 4 | `8453f3f`, `90a43a4` | D12 band encoding noted; settings accessors, `netguard`, sliding-window limiter, `core/jobs.py`, `clock`, `/stable/` FMP client, 15-endpoint probe, universe builder |
| 5 | `6e85ab2`, `05ff7f8` | Stale-volume refresh + `avg_volume_asof`; estimates/fundamentals/events/news ingestion, `for_each_symbol` guards, snapshot immutability tests, read-time price adjustment |
| 6 | `9cdb7d7`, `9c03aed` | Shared day-level FMP budget (`api_budget`); Schwab client/prices/chains, `features/iv.py`, auth + token-sync scripts |
| 7 | `426cde7` | `jobs/daily_close.py`, `alerts/mailer.py`, `scripts/backup_db.sh`, `scripts/token_expiry_check.py`, ET cron block in the README |

**State:** 658 unit tests + 74 integration tests pass; ruff clean on 72 files. 67 Python
modules, 23 test modules. Backup verified by an actual restore into a fresh database with
`timescaledb_pre_restore()`/`post_restore()`: 150/40/40/10 rows across the four hypertables,
6 chunks, 4 compression policies, **0 retention policies**, 16 tables, and the D7
estimate-revision signal intact across the restore (eps 2.00 -> 2.39).

**What is NOT done:** this is Phase 1 plumbing only — collection, storage and orchestration.
No scoring, no recipes, no decision layer, no Shadow Book, no digest. The engine can fill
the database; it cannot yet form an opinion. Gates 1b through 4 below are the path to a
first `day_complete = true`.

### Build map — which layer implements which decision

Written down so the D32–D39 refinements cannot be lost between now and the layer
that enforces them. **Every number they introduce already lives in `config/` and
is validated by the loader**, so a typo fails at startup today; what is missing
is the code that reads it.

| Layer (unbuilt) | Binding decisions | Config it will read |
|---|---|---|
| **Scoring** (`scoring/pillars.py`, `composite.py`, `recipes/`) | **D32** (weights 40/30/20/10 + reinstatement), **D33** (percentile ranking, relative strength as a Pillar A input, digest cap), **D34** (diversity gate) — with D2, D12, D25 | `scoring.yaml`: `pillar_weights`, `ranking`, `sentiment_reinstatement`, `pillar_agreement`, `tiers` |
| **Decision** (`decision/idea_builder.py`, `tiering.py`, `risk_manager.py`) | **D36** (IV rank selects the vehicle), **D37** (pyramiding), **D38** (sector guard) — with D17, D18, D19 | `risk.yaml`: `vehicle_by_iv_rank`, `pyramiding`, `sector_guard`, `per_trade_risk_pct`, `heat`, `exits` |
| **Allocation** | **D35** (barbell sleeves + profit ratchet) — with D17 | `risk.yaml`: `sleeves` (incl. `ratchet`), `allocation_targets`, `stable_buckets` |
| **Phase 2: Shadow Book + backtest** | **D39** (calibration curve, 30-idea minimum sample, 4–8 week paper phase, modelled fills) and **O7** | `scoring.yaml` thresholds under review; `shadow_book` tables |

Two items from D39 that are easy to lose because they are not parameters:
**EDGAR insider-cluster ingestion** is folded into the Pillar C (Catalyst) build
as an EARLY item — it is free, already in the source list, and insider clusters
are among the few genuinely predictive qualitative signals. And the standing
note: **the FMP Premium upgrade remains the highest-ROI improvement available**,
because it lights up the estimate-revision signal that D32 pre-weights
Fundamental for. Until then Fundamental carries 30 points of weight on data that
is missing its best input.

**Deliberate carry-overs into the running system (not bugs, but silent-wrongness classes):**
O7 (`--backfill-actions` before any backtest) and O10 (spinoff adjustment ignored). Both are
pinned by tests so they cannot drift into a plausible-looking guess.

---

## ✅ DECISIONS

| # | Date | Decision |
|---|------|----------|
| D1 | Aug 30 | **Brain before beauty.** Validate the scoring/confluence engine before any UI. |
| D2 | Aug 30 | **Architecture:** Ingestion → Features → 4-Pillar Confluence (Technical 35 / Fundamental 25 / Catalyst 20 / Sentiment 20) → Decision Layer → Alerts → UI. Ideas surface only when ≥3 pillars agree and composite ≥70 (Tier A gate). |
| D3 | Aug 30 | **Schwab Trader API** is the price/options/positions backbone (free with brokerage account; ~120 req/min, throttle to ~100). |
| D4 | Aug 30 | **FMP** for fundamentals/estimates/calendars. Amended Sep 1: target tier is Premium ($69/mo) for quarterly fundamentals, corporate calendars, 30y history. See D29 for current deferral. |
| D5 | Aug 30 | **Finnhub free tier** for news/sentiment prototyping. Never plan around paying for it. |
| D6 | Aug 30 | **Free fills:** SEC EDGAR (filings, Form 4), FRED (macro), Claude API for transcript tone scoring (Phase 2). |
| D7 | Aug 30 | **Self-build expensive signals via daily snapshots:** option chains and analyst estimates snapshotted daily; after 60–90 days we own IV rank and estimate-revision momentum. These histories cannot be backfilled at any price. Snapshot jobs are sacred. |
| D8 | Aug 30 | **Maintain this living decision log**, updated every working session. |
| D9 | Aug 30 | **Performance philosophy:** consistent compounding, not lottery tickets. Meet or beat market average via many well-planned trades. A 20–30% option gain is a strong win. |
| D10 | Aug 30 | **Confluence discipline is non-negotiable.** Every idea ships with entry, stop/invalidation, targets, risk amount, alerts. |
| D11 | Aug 30 | **Win definition:** breakeven-or-better = acceptable; true win = target/R:R hit. Engine optimizes **expectancy per recipe** (never raw win rate). TP1/TP2/runner structure protects both. |
| D12 | Aug 30 | **Two-tier ideas.** Tier A: ≥3 pillars (each ≥ min_score), composite ≥70, R:R ≥2:1, standard size. Tier B: compelling asymmetry, 2 pillars or composite 55–69, R:R ≥3:1, reduced size, monthly cap, defined-risk structures preferred. Sizing is the safety valve, not prohibition. **Band encoding:** the Tier B composite band is **[55, 70) — exclusive upper**, not an inclusive 69. `ideas.composite_score` is `NUMERIC(6,2)`, so an inclusive 69 would strand fractional scores (69.01-69.99) in no tier at all. The loader asserts `tiers.b.max_composite <= tiers.a.min_composite`. |
| D13 | Aug 30 | **Shadow Book:** every idea (A, B, discretionary) logged with projections at time of call and tracked to outcome whether taken or not. Quarterly review adjusts weights/thresholds from evidence. |
| D14 | Aug 31 | **Universe:** S&P 500 + Nasdaq 100, avg 20-day volume >1M, active options chain, + watchlist (~500 names). Options gate abstains on `has_options=None` until Schwab populates it. |
| D15 | Aug 31 | **Phase 1 cadence: daily bars only.** Intraday is a Phase 2 alert-timing layer. |
| D16 | Aug 31 | **MVP alert channel: daily post-close email digest**, Tier A and B clearly separated. |
| D17 | Aug 31 | **Risk parameters (locked).** Tier A swing 2.5%/trade; Tier B 0.75%/idea; min R:R 2:1 (A) / 3:1 (B); max 6 concurrent swings; total open heat ≤10% across active trades (swings+options ONLY; holds/ETFs excluded, governed by allocation targets, concentration limits of ≤10% name / ≤30% sector, and thesis triggers with review alert at ~−15%); circuit breaker: 2 consecutive losing weeks or −5% month → Tier B paused, Tier A halved until a winning week. Options: defined-risk only, 5% monthly realized-loss budget, 3% max per position. Cash: no risk profile. Buckets: ETF 35 / holds 30 / swings 20 / options 10 / cash 5; risk-off (SPY<200MA + VIX>25) → swings 10, options 5, cash 20+. All % off current account value. |
| D18 | Aug 31 | **Cap mechanics.** Heat cap (10%) = open risk, resets continuously (TP1 + stop-to-breakeven frees it). Tier B monthly cap = hybrid budget: 4% at-entry base, realized Tier B gains replenish at 50%, hard ceiling 6% deployed/calendar month. Options 5% budget = realized-loss budget. |
| D19 | Aug 31 | **Dividends.** (1) Total return everywhere incl. SPY benchmark. (2) ETF DRIP on; hold dividends → cash bucket. (3) Ex-div dates on event radar; dividend-adjusted stops; no new swing long within 2 days of ex-date. (4) Short-call assignment flag when extrinsic (`timeValue`) < dividend × buffer near ex-date. (5) Dividend growth/payout = Pillar B quality inputs; dividend cut = automatic thesis-review trigger. (6) Backtests use dividend-adjusted series. |
| D20 | Aug 31 | **Database: Postgres (≥15) + TimescaleDB.** Hypertables for prices/chains/estimates/IV; relational for ideas, Shadow Book, positions, alerts, job_runs. Nightly pg_dump to object storage. |
| D21 | Aug 31 | **Hosting: small cloud VM** (Hetzner ~$5/mo or DO ~$6/mo), cron-driven. Laptop re-auth → token synced to VM. |
| D22 | Aug 31 | **Chain snapshot scope:** strikes within ±15% of spot × nearest 4 MONTHLY expirations + per-ticker aggregate IV. |
| D23 | Aug 31 | **Built with Claude Code**; repo carries CLAUDE.md + docs; this chat is the design/decision layer. |
| D24 | Aug 31 | **Read-time price adjustment.** `prices_daily` append-only raw OHLCV; `corporate_actions` immutable source of truth; `features/adjustment.py` with `as_of` reproducibility, modes `split_only` and `total_return`. All four history tables protected by a static no-DELETE / no-DO-NOTHING test. |
| D25 | Sep 1 | **Config is the only home for numbers.** [OPEN]-tagged provisional defaults documented in `docs/open-parameters.md` with a sync test; decision-fixed values asserted by named tests. Key approvals: pillar "agrees" = score ≥60; any pillar <30 caps composite at 69 (blocks Tier A, leaves Tier B open); base breakout ≥4 weeks; post-earnings gap ≥2.0%; min price $5; swing timeout 45 days (swings only; options expire; holds thesis-trigger only). `min_score` is the first sensitivity sweep once O7 backtests exist. |
| D26 | Sep 1 | **The repo copy of this log is canonical.** |
| D27 | Sep 2 | **Network consent gate** (`core/netguard.py`). Live calls require `ALLOW_LIVE_CALLS=1` per invocation (CLI/cron, never `.env`); always-no inside pytest; gate sits ahead of limiter and transport; blocked call = failed step. Rationale: an unintended live Finnhub call showed that possessing a working credential says nothing about whether a call was intended. Total failure always raises (never "ok, 0 rows"). |
| D28 | Sep 2 | **Seeking Alpha rejected as a data source** (no official API; third-party wrappers are ToS-gray and fragile). SA remains human research only. |
| D29 | Sep 3 | **RETIRED 2026-10-21 — superseded by the Premium upgrade; kept as the documented FALLBACK.** The free-tier mode described below is no longer how the engine runs: all five gated endpoints pass, the universe builds from the API, and the estimate clock has started. What survives deliberately is the machinery, because a plan can lapse and a billing failure can 402 an endpoint overnight: `config/constituents_static.txt` stays maintained and tested, `fmp_fundamentals` keeps its degrade-on-402 path, `fmp_estimates` keeps the probe that decides its own criticality, and the day budget stays in force with its job changed from "stay inside the allowance" to "stop a runaway loop". Original free-tier decision follows. **FMP upgrade deferred; engine runs in free-tier mode.** Account is on `/stable/`; free tier 402-gates sp500-constituent, nasdaq-constituent, analyst-estimates, ratios, key-metrics. Mitigations: static constituents file, degrade-on-402 fundamentals, estimates marked expected-blocked, 250/day call-budget guard. Explicit cost: the estimate-revision history clock starts on upgrade day. Target on upgrade remains Premium. Phase 2 note: transcripts are Ultimate-gated; decide then. **Budget mechanics (added Oct 20):** the 250/day guard would otherwise leave 269 of 519 symbols with no volume on each weekly universe rebuild, and since the liquidity filter rejects an unverified symbol the universe would *oscillate* between different subsets rather than converge. So `ingest/universe.py` seeds enrichment from the `avg_volume_20d` already in `tickers` (**convergence property:** coverage expands monotonically, verified live at 250 -> 500 -> 519 over three runs, and a symbol verified last week stays in the universe this week), spends the budget on UNKNOWN symbols first, and then — once converged — spends what is left refreshing the **stalest** known volumes oldest-first, ordered by a dedicated `tickers.avg_volume_asof` column (`db/migrations/001_add_avg_volume_asof.sql`; `updated_at` cannot serve because its trigger fires on any column change). An undated volume sorts as maximally stale; the date advances only when a real figure was obtained, so a run of empty responses cannot make stale data look fresh. Unverified is still never admitted. **Shared counter (added Oct 20):** the budget was enforced PER JOB, so universe enrichment and fundamentals could each stay within bounds and jointly exceed the day. It is now a DB-backed day-level counter (`api_budget`, `db/migrations/002_add_api_budget.sql`) keyed on (provider, ET session date), drawn at a single chokepoint inside `FMPClient.get()` so every call site is covered automatically — the probe included. Invariant: total FMP calls per calendar day never exceed `fmp.daily_call_budget`. `fmp.reserve_for_events` (3, the exact number of market-wide calendar endpoints) is held back from the general pool so the event radar cannot be crowded out by a fundamentals crawl. Exhaustion is reported as DECLINED SPEND, never a failure: the job stops cleanly, records the shortfall in `job_runs.details`, and the work resumes next run. |
| D30 | Oct 20 | **Commit and push at the end of every session and after every completed step. No exceptions.** GitHub is the system of record; Codespaces are disposable. A step is not "complete" without a pushed commit named in the report. |
| D31 | Oct 20 | **Rebuild authorized** from this log + CLAUDE.md + the resumed Claude Code session's transcript, per `docs/REBUILD_KICKOFF.md`. All prior approvals carry over; no design re-litigation needed. |
| D32 | Oct 20 | **Sentiment demoted until it earns its weight. Pillar weights become Technical 40 / Fundamental 30 / Catalyst 20 / Sentiment 10.** Free-tier news is the thinnest input the engine has, and a pillar fed noise adds noise to every composite it touches. Fundamental gains the weight in anticipation of the estimate-revision signal (which is still dark — see D29). Sentiment is not removed, because the existing conflict rule already does the work that matters: `composite.conflict_below` vetoes Tier A when ANY pillar scores below it, so strongly negative sentiment still caps an idea at `capped_at` regardless of its weight. A demoted pillar can still veto. **Reinstatement condition, recorded so it is a test rather than a mood:** after **30 or more** closed Shadow Book ideas in which sentiment scored 60+, if those ideas outperform the recipe baseline, restore sentiment toward 20 at the quarterly review (D13). The sample floor is `[OPEN]` (`sentiment_reinstatement_min_sample`). Supersedes the D2 weights; `test_d32_pillar_weights` is now the named assertion and the old D2 weight test points at it. **Update 2026-10-21:** the revision signal D32 pre-weighted Fundamental for is **now live data** — FMP went Premium and `analyst-estimates` took its first snapshot that day. The bet is no longer on data arriving; it is on the data being good. But the series starts at zero depth (see "THE ESTIMATE-REVISION CLOCK"), so for the next several weeks Fundamental still carries 30 points on an input with almost no history. Re-examine at the first quarterly review. |
| D33 | Oct 20 | **Cross-sectional ranking. An absolute score decides nothing.** Ideas are ranked by composite **percentile within the universe** each day; relative strength vs sector becomes an explicit Pillar A (Technical) input; and the digest surfaces at most `max_ideas_per_day` (new `[OPEN]`, default 5) **by rank**. A composite of 72 means one thing on a day when it is the best in the universe and another when forty names beat it. Opportunity is relative to the day's alternatives, so the engine must rank before it recommends. Note the interaction with D12: the absolute gates still apply first — ranking selects AMONG qualifying ideas, it does not promote an idea that failed its tier gate. |
| D34 | Oct 20 | **Informational diversity gate on Tier A.** The agreeing pillars for Tier A must include **Technical plus at least one of Fundamental or Catalyst**. Technical and Sentiment agreeing is one signal measured twice, not two confirmations — price momentum and news tone are largely the same information arriving by different routes, and D12's "≥3 pillars agree" counts them as independent when they are not. This tightens Tier A without touching the count: three pillars can agree and still fail this gate. |
| D35 | Oct 20 | **Barbell formalized with a profit ratchet. Two named sleeves, separate scoreboards.** **Core** (ETFs + long holds, target **65%**, benchmarked against its own blend) and **Growth** (swings + options + tactical cash, target **35%**, benchmarked against SPY **total return** per D19(1)). **Quarterly ratchet:** when realized gains push the Growth sleeve more than **3 points** above its target weight (`[OPEN]`), sweep **half** the excess (`[OPEN]`) into Core **permanently**. Drawdowns in a sleeve are NOT replenished mid-quarter, and rebalancing toward targets happens only at scheduled rebalances. The asymmetry is the whole point: **winners raise the floor; losses never pull compounded gains back into the trading sleeve.** Without the ratchet a barbell is just a label on a drifting allocation, and a good year silently becomes a bigger bet rather than a bigger base. Separate benchmarks matter as much as separate targets — a Growth sleeve beating its own history while losing to SPY is not working. |
| D36 | Oct 20 | **IV rank selects the vehicle on every Tier A idea, printed on the card.** Below `iv_vehicle_low` (new `[OPEN]`, default 30): long call or debit spread candidate — optionality is cheap. Between low and high: **shares by default**. Above `iv_vehicle_high` (new `[OPEN]`, default 50): shares, or a defined-risk premium-SELLING structure — optionality is rich, so be the seller. Always defined risk (D17), and always checked against binary events inside the expiration (D19). **This is where the Growth sleeve's convexity comes from:** paying for optionality when it is cheap and selling it when it is rich, rather than defaulting to one vehicle and hoping the vol regime suits it. Requires the self-built IV rank (D7/D22), which is why the chain snapshot clock matters more than the estimate one right now. Printing it on the card is deliberate: the human sees WHY the vehicle was chosen and can overrule it. |
| D37 | Oct 20 | **Pyramiding instead of bigger entries.** Recipes may define add-points. An add is permitted **only after the stop has reached breakeven or better**, and total open risk on the position after the add must not exceed the **original** per-trade allocation (D17). **Initial risk never rises; winners earn their size.** This is the one way to get a larger position without a larger bet: the second tranche is funded by the first tranche's locked-in safety, not by raising the ante. It also closes the most common discretionary leak — deciding mid-trade that a thesis deserves more money, which is conviction talking, not evidence. |
| D38 | Oct 20 | **Sector correlation guard on the active sleeve.** Maximum **2 concurrent swings per sector**; a third same-sector Tier A idea surfaces **flagged watch-only, not actionable**. Six positions in one sector is one trade wearing six costumes — the heat cap (D17, ≤10% total) measures position count and size but is blind to correlation, so it would happily approve six semiconductor swings as a diversified book. Surfacing the third as watch-only rather than suppressing it keeps the information (the sector IS working) while refusing the concentration. Complements, not replaces, `stable_buckets.max_sector_pct` (30%), which governs the Core sleeve. |
| D39 | Oct 20 | **Learning-loop guardrails for Phase 2.** (1) **Calibration tracking:** the Shadow Book produces a score-to-hit-rate curve — of ideas scored 70-80, what fraction reached TP1 — and quarterly reviews adjust the **confidence mapping** as well as the weights. A composite is a claim about probability; until it is calibrated it is a number with opinions. (2) **Minimum sample:** no weight or threshold change on fewer than **30 closed ideas per recipe**. (3) **Paper phase:** the first **4-8 weeks** after the scoring brain goes live run **Shadow Book-only**, tuning the Tier A threshold to produce **2-5 ideas per week** — too many means the gate is loose, too few means it is unusable. (4) **Backtests model fills:** next-day entries, spreads and costs on options; a backtest filling at the signal price is a backtest of a different strategy. Also: fold **EDGAR insider-cluster ingestion** into the Pillar C (Catalyst) build as an EARLY item — it is free, already in the source list, and insider clusters are among the few genuinely predictive qualitative signals. And recorded standing: **the FMP Premium upgrade remains the highest-ROI improvement available**, because it lights up the estimate-revision signal that D32 pre-weights Fundamental for. |
| D40 | Oct 21 | **Strategy promotion ladder. Nothing is traded because it looked good in a backtest.** Every recipe/strategy carries a `status`, and the status decides what the engine is allowed to do with its ideas: **SHADOW** — ideas are surfaced and tracked in the Shadow Book, **flagged not-actionable**; **PAPER** — additionally sized against a VIRTUAL account applying the full D17/D18 risk stack and tracked as simulated positions; **LIVE** — the human trades them. **New strategies always enter SHADOW**, with no exception for one that backtests well, because a backtest is the one piece of evidence that cannot be contaminated by hindsight only if it was never looked at — and it always was. Promotion **SHADOW→PAPER** requires **≥30 closed ideas with positive expectancy** (the D39(2) sample floor, applied per recipe). **PAPER→LIVE** requires sustained positive expectancy AND calibration within tolerance over a defined window — the window, the tolerance and the demotion thresholds are `[OPEN]`, because no decision fixes them and guessing them in prose would make them unreviewable. **Demotion is automatic on sustained underperformance**: a strategy that stops working stops being actionable without anyone having to admit anything, which is the entire point of putting it in config rather than in judgement. Status lives in `config/scoring.yaml` (diffable, reviewable, one line per recipe) and is printed on **every idea card and every digest**, so an idea's standing is never inferred. **The Shadow Book records status-at-time-of-call** (`shadow_book.status_at_call`, `db/migrations/006`): expectancy measured across a promotion boundary is two different strategies averaged together, and without the stamp the mistake is undetectable after the fact. Interaction with D13: the quarterly review is where promotion and demotion are *acted* on; the ladder is what makes that review a rule rather than a conversation. |

---

## ⏳ OPEN ITEMS

| # | Item | Status |
|---|------|--------|
| O7 | Backtest depth before live alerts | Open. Suggest 3–5 yrs daily, walk-forward. Prerequisite: run `--backfill-actions` back to the start of price history (silent-failure risk otherwise: wrong total-return series for every early bar). |
| O8 | Schwab weekly re-auth | Manual copy-paste flow is settled. Open: token sync mechanism to VM (scp vs object storage). `token_expiry_check.py` emails 24h before expiry. |
| O9 | [OPEN] config parameters | Reviewed Sep 1: all provisional defaults approved with three adjustments (max_days_since_earnings 5→10, assignment_extrinsic_buffer 1.0→1.25, deactivate_after_absent_days 3→5). |
| O10 | **Spinoff price adjustment is unimplemented** | Open. `corporate_actions.action_type` accepts `'spinoff'` and `ingest/fmp_events.py` would store one, but `features/adjustment.py` has no factor rule for it, so a spinoff is IGNORED rather than applied. A test pins the ignore so it cannot silently become a dividend-shaped guess. Consequence: for a symbol that has spun off a subsidiary, the total-return series is wrong across the ex-date — the price drop looks like a real loss. Not blocking Phase 1 (no current holding has one), but it IS a silent-wrongness class like the O7 backfill gap, so it must be closed before any backtest is trusted on a symbol with a spinoff in its history. Correct treatment needs the spun-off entity's value at the ex-date, which FMP does not expose in the calendars — likely a manual `corporate_actions` row with a cash-equivalent amount. |

---

## 🎯 EDGE SOURCES (unchanged)
1. Selectivity (the engine can say "nothing today"). 2. Estimate-revision momentum. 3. Post-earnings drift. 4. Volatility risk premium via self-built IV rank. 5. Event-risk awareness (never blindsided by earnings/ex-div inside a position). 6. Retail time/size flexibility. 7. The Shadow Book feedback loop. 8. Emotion removal: plans computed before money is at risk.

---

## 🔬 WHAT THE FIRST LIVE RUN FOUND (Oct 20, 2026 — `64be4fd`)

Six defects, all the same shape: **something that could not possibly work still
reported success.** None were findable without real providers; every one is now
pinned by a test.

| # | Defect | Why it mattered |
|---|--------|-----------------|
| 1 | Batched `/stable/quote` is plan-gated (one symbol per call on free) | Universe enrichment died on its first call. Descriptive pass now abandoned wholesale rather than retried per symbol — it feeds no field the D14 filter needs. |
| 2 | Enrichment treated a systemic provider error as one bad symbol, 225 times | FMP signals a spent allowance with **HTTP 200 + an `Error Message` body**. The blanket `except Exception` logged and carried on, leaving a 22-symbol universe that looked like a legitimate narrow result. Now aborts on the first `FMPAuthError`, plus the 25%/10-sample guard `for_each_symbol` applies and this hand-rolled loop bypassed. |
| 3 | A spent budget killed the universe job outright | `BudgetExhausted` escaped `fetch_constituents` (caught only `FMPPlanError`), so the last run of the day would destroy a converged universe that the static file + stored volumes rebuild with **zero** calls. |
| 4 | `fmp_estimates` reported `success / 0 rows` on a CRITICAL job | `v_day_status` certified `day_complete = true` on a snapshot nobody took — the exact silent zero-row success D7/D22/D27 exist to prevent. Also: the plan probe swallowed `BudgetExhausted` into `return False`, reporting "the plan covers it" on a question never put. |
| 5 | `job_run` recorded `failed` for ANY exception | `job.skip()` before a raise was dead code — **including on the pre-existing plan-gated path, which has been writing failed rows, not the skipped ones the step 7 report claimed.** New explicit `clean_stop` allowlist (not "honour skip() if called", so a job that skips then crashes unrelatedly is still failed). This decides the exit code: a critical `failed` row blocks the day and exits 1; a skipped one leaves `day_complete` false and exits 0 on the known gap. |
| 6 | **The D14 options gate could never fire** | `schwab_chains` writes `has_options` correctly and the upsert COALESCEs it, but every rebuild constructed candidates with `has_options=None` and **nothing read the column back**, so the gate abstained on every symbol for ever. The writeback was working and no one was reading it. Verified live: the universe now rejects a symbol marked as having no chain (22 -> 21). |

**`extrinsicValue` — the CLAUDE.md finding, re-confirmed and worse.** 5,247 of
8,214 contracts disagree with `timeValue` (63.9%, up from 48% in September), and
4,113 report an `extrinsicValue` **larger than their own mark**, which is
impossible. The worst case names the bug: a Jan-2027 SPY put at strike 50 with
spot 771.38 is marked 0.01, `timeValue` 0.01, `extrinsicValue` 721.39 — exactly
`spot - strike`. The field returns distance-from-the-money, not extrinsic value.
Treat it as unusable, not merely imprecise.

**Honest state of the day's data:** the dev database was wiped mid-session by my
own integration suite (`tests/dbguard.py` permits destructive runs against a
test-named database, which this is). Nothing irreplaceable was lost — the D7
clock starts at gate 4 on the VM, not here.

**Known divergence to watch:** the engine's `api_budget` counter and FMP's own
accounting are independent. The counter starts each ET day at zero regardless of
what the provider already served, so a probe run outside the engine makes the
engine over-optimistic. That is what caused finding 2 to bite. The counter is a
courtesy guard, not a source of truth; the systemic-abort in finding 2 is the
real protection.

---

## 🔬 WHAT THE PREMIUM UPGRADE FOUND (Oct 21, 2026)

Six more defects, every one of them **hidden by the free tier** rather than
caused by the upgrade. The pattern from Oct 20 repeated exactly: a paid plan
removes the loud refusals that were masking quiet bugs underneath.

| # | Defect | Why the free tier hid it |
|---|--------|--------------------------|
| 1 | `quotes()` sent a comma list to `/stable/quote`, which returns **HTTP 200 and `[]`**. The batch endpoint is `/stable/batch-quote?symbols=` — note the PLURAL parameter; `symbol` there is a 400. | The free tier answered that exact request with a 402, so the mistake sat behind a plan gate for the whole free-tier period. The upgrade turned a loud refusal into a silent empty result — the more dangerous of the two. A wholly empty BATCH now raises; a single-symbol miss stays tolerated. |
| 2 | **A 402 does not prove a plan gate.** `/stable/ratios?symbol=BRK.B` answers 402 while `?symbol=AAPL` answers 200, and an unknown symbol answers 200 with `[]`. | On free, `ratios` was genuinely gated, so the inference was accidentally right. `_optional` now confirms against a known-good canary before marking an endpoint unavailable — otherwise one odd symbol NULLs the valuation columns for every remaining symbol under a log line naming the wrong cause. |
| 3 | **The canonical symbol form is DASH, and two functions disagreed.** FMP `/stable/` returns `BRK-B`/`BF-B`; `to_schwab_symbol` translated only `.` → `/`, so `BRK-B` reached Schwab verbatim and the only two share-class names in the S&P 500 got no option chain. With the D14 gate armed they would have been dropped from the universe over punctuation. Both docstrings asserted the dot form was canonical; both were wrong. | Constituents were plan-gated, so the universe came from the static file and never reached a Schwab chain call for those names. |
| 4 | **`config/constituents_static.txt` carries the DOT form**, which FMP 402s — so the D29 fallback would have stored `BRK.B` and `enrich_with_liquidity`'s systemic-error abort would have killed the ENTIRE universe build on the first one. | A fallback that worked only while the budget was too exhausted to make the call that broke it. `normalize_symbol` now canonicalises dots to dashes, pinned by a test. |
| 5 | **Seven ratio field names were still `/api/v3/` spellings.** `priceEarningsRatio` → `priceToEarningsRatio`, `debtEquityRatio` → `debtToEquityRatio`, `payoutRatio` → `dividendPayoutRatio`, `priceEarningsToGrowthRatio` → `priceToEarningsGrowthRatio`, `roic` → `returnOnInvestedCapital`, `enterpriseValueOverEBITDA` → `evToEBITDA`, and `dividendPerShare` moved from `key-metrics` into `ratios`. | `ratios` and `key-metrics` were 402-gated, so the mapping had never been exercised ONCE. The crawl reported success, `unavailable_endpoints` came back empty, and **six of ten valuation columns were silently NULL for every symbol**. `unavailable_endpoints == []` proves the endpoint answered; it says nothing about the answer being read. Now 99.7% populated with real values, pinned by a test that fails if any valuation column comes back NULL from a fixture payload. |
| 6 | **No retry on transient Schwab errors.** A chain crawl drew 429 on 2 of 433 symbols — a different two each run — and each became a permanent hole in an unbuyable series. | Nothing to do with FMP: this was simply never exercised at 433-symbol scale before. Now 408/429/5xx retry with backoff; 401 deliberately does not, because a stale token is not transient. Each attempt re-takes a limiter slot, so a backoff storm cannot exceed the limit it is backing off from. |

**And one that was not hidden, just wrong:** `v_day_status` counted every row
for a date, so one failed attempt poisoned that day permanently and no re-run
could recover it. A timed-out `daily_close` left estimates abandoned and chains
partial; the re-run succeeded at every step and `day_complete` stayed false.
Gate 4 starts the D7 clock on the first `day_complete = true`, so a bad first
night would have meant the clock never started while every step showed green.
Now judged by each job's LATEST run, with an `attempts` column so a day that
needed three goes does not look serene. `partial` still fails the day for a
critical job — 431 of 433 chains is a permanent hole, not a rounding error.

---

## 🚀 PRODUCTION LIVE — 2026-10-21

**Gates 3 and 4 closed the same day FMP went Premium.** The engine now runs
unattended on a DigitalOcean droplet, on cron, on ET host time, and the first
production run reported `DAY COMPLETE`.

**THE PRODUCTION D7 CLOCKS BOTH START 2026-10-21 — on the VM.** Two series, one
date, and neither can be backdated:

* the **IV / option-chain** series (D7/D22), which the self-built IV rank and
  therefore D36's vehicle selection depend on; and
* the **estimate-revision** series, which went live the same day with the
  Premium upgrade and which D32 pre-weighted Fundamental to 30 for.

**The Codespace database is DEV-ONLY and its history does not count.** Both
databases are named `investment_engine` and both contain a day that looks
complete for 2026-10-21, which is precisely why this needs writing down: the
production series is the one on the droplet. The Codespace copy was built and
rebuilt several times on 2026-10-21 while finding the Premium-upgrade bugs, its
`job_runs` shows 34 attempts for that date, and it must never be treated as the
start of anything. If a backtest or a revision diff is ever run against the
Codespace database, its early history is an artefact of debugging, not a record
of the market.

**Still open from gate 3:** `--backfill-actions` has not been run on the VM.
That is the O7 prerequisite — without it `features/adjustment.py` produces a
wrong total-return series for every bar earlier than the first corporate action
on record, silently and with no error.

---

## ⏱ THE ESTIMATE-REVISION CLOCK STARTED 2026-10-21

**Record the date; it cannot be backdated.** D7's edge is the day-over-day diff
in analyst estimates, and that series begins on the first day a snapshot was
stored. FMP went Premium on **2026-10-21** and `fmp_estimates` took its first
snapshot the same day. There is **no estimate-revision history before this
date** and no vendor sells it back — the engine's revision signal is blind
until it has two snapshots to compare, and thin until it has several weeks.

Consequences to hold in mind while the series is young:

* **D32 pre-weighted Fundamental to 30 for a signal that only just went live.**
  Until this series has depth, Fundamental carries its heaviest weight on its
  weakest input. That is a deliberate bet on the data arriving, not a claim
  that it has arrived, and it is the first thing to re-examine at the quarterly
  review (D13) if Fundamental underperforms.
* **D39(2)'s 30-closed-ideas floor applies here too.** No conclusion about
  revision momentum on a handful of diffs.
* The chain/IV snapshot clock (D7/D22) is separate and still has its own start
  date, which is whenever gate 4 first produced `day_complete = true` on the
  VM — **not** today's Codespace run.

---

## 🛡 HARDENING AFTER THE FIRST LIVE RUN (Oct 20, 2026)

Two follow-ups, both closing holes the live run exposed rather than adding
features.

**1. Provider-exhausted marker (`db/migrations/003`).** `api_budget.calls_spent`
counts what THIS engine spent and cannot know what the provider already served
to a probe, a second deployment or a manual call. The first live universe run
began believing it had 247 calls while FMP's allowance was nearly gone, and
spent 225 of them discovering that one symbol at a time — each answered with
**HTTP 200**, so each looked like a fresh request worth making. The client now
recognises the allowance-spent signal in both of its forms (a 200 with an
`Error Message` body, or a 429 whose text is about the plan rather than pacing)
and records `provider_exhausted_at` / `_note` against the ET session date. Every
later job that day is refused at the budget gate **without opening a socket**,
pinned by a test asserting the second job makes exactly zero transport calls.
Deliberately not a reset of `calls_spent` to the cap: "what did we spend" and
"what did the provider say" answer different questions, and collapsing them
would erase the divergence that is the whole point. A 429 about pacing stays
retryable; a plan statement that is not about today's allowance does not mark.

**2. Databases separated by protection class.**

| Database | Holds | Integration suite |
|---|---|---|
| `investment_engine` | live collected data (D7 history) | **refused outright** — PROTECTED name; `INTEGRATION_DB_OK=1` does not unlock it |
| `ie_test` | nothing of value | truncates freely, no override needed |

`PGDATABASE` points at `investment_engine` in development as well as on the VM,
so ordinary work touches the real database and the destructive suite must be
pointed elsewhere on purpose (`PGDATABASE=ie_test pytest -m integration`).
Previously one database served both roles, and a reflexive
`INTEGRATION_DB_OK=1 pytest -m integration` wiped a day of live collection
mid-session. **An override that gets typed from muscle memory is not a
protection**, so the live name is now refused rather than merely unmatched by the
scratch pattern. There is no legitimate reason for the suite to truncate the
collection database.

---

## 📋 GO-LIVE CHECKLIST (rebuilt)

**Gate 0 — Rebuild the repo. ✅ DONE Oct 20, 2026 (`426cde7`).** All 7 steps rebuilt from this log + CLAUDE.md + the resumed session transcript, each ending in a pushed commit (D30). 658 unit + 74 integration tests green.
**Gate 1a — Free-tier live. ⚠️ MOSTLY DONE Oct 20, 2026 (`64be4fd`).** Probe confirms `/stable/`, 10/15 usable, the 402s are exactly the known five. Universe builds from static constituents; news ran live (2,598 rows / 22 symbols). **Blocked on quota, not on code:** the free daily allowance was already largely spent when the first live universe run started, so only 22 of 519 symbols got a verified volume. Re-run on a fresh allowance to continue converging (250 -> 500 -> 519). Also learned: **batched `/stable/quote` is plan-gated — one symbol per call on free**, so the descriptive enrichment pass is skipped entirely and the whole budget goes to average volume (the only field the D14 filter needs).
**Gate 1b — Premium unlock. ✅ CLOSED Oct 21, 2026.** Probe **15/15** on `/stable/` (the five previously-402 endpoints — `sp500-constituent`, `nasdaq-constituent`, `ratios`, `key-metrics`, `analyst-estimates` — all pass). Universe now builds from the **API**, not the static file (`universe [fmp]`, sp500=503 + nasdaq100=101). `analyst-estimates` probes clean, so `fmp_estimates` is recorded **`is_critical=True`** and took its **first snapshot**. Budget raised 250 → 9,000 and the limiter 300 → 600/min. Note `/api/v3/` now answers **403 (key rejected)** rather than 402: the legacy generation is not merely ungated, it is unavailable on this plan, which is fine — the client has been `/stable/`-only since step 4.
**Gate 2 — Schwab live. ✅ DONE Oct 20, 2026 (`64be4fd`).** Token written Oct 20, **expires Oct 27** (7-day refresh life); live SPY quote verified. Fixture diff against a fresh live chain: **zero drift in 47 days** — same 20 top-level keys, same 54 contract fields, no renames, no type changes, all four `expirationType` values present (S 2088 / W 4912 / Q 650 / M 564). `has_options` writeback confirmed for all 22 active tickers, **and the D14 gate it arms was found permanently disarmed and fixed** (see below). End-to-end live: schwab_prices 6,094 rows, schwab_chains 2,908 rows, schwab_iv 22 rows.
**Gate 3 — VM. ✅ CLOSED Oct 21, 2026.** DigitalOcean droplet, Postgres 16 + TimescaleDB, deployed and running. Droplet-level snapshots are enabled as the outer backup layer, so `scripts/backup_db.sh` keeping compressed local dumps with 30-day retention (no object-storage bucket configured) is a supported configuration rather than a gap. **Still open from this gate:** `--backfill-actions` has NOT been run on the VM, which remains the O7 prerequisite before any backtest is trusted.
**Gate 4 — Cron on. ✅ CLOSED Oct 21, 2026.** Cron installed and running on ET host time. **First production `day_complete = true` on 2026-10-21** (prices 119,772, chains 39,462, iv 433, estimates 2,598, fundamentals 7,325, news 17,388). After gate 4 the engine is collecting, not yet deciding: scoring, recipes, decision layer, Shadow Book, digest are the remaining Phase 1 work — see the **build map** above for which decision binds each. Per **D39(3)** the first 4–8 weeks after the scoring brain goes live run **Shadow Book-only**, with the Tier A threshold tuned to 2–5 ideas per week before any of it informs a real order.

---

## 🗒 SESSION NOTES
- Aug 30 – Sep 3: original build, steps 1–7 complete and live-validated (see CLAUDE.md "Validated API findings" for everything learned). Full detail preserved in the design chat and the prior Claude Code session transcript.
- Oct 20: loss discovered (unpushed Codespace auto-deleted). D30, D31 adopted. Rebuild package authored: CLAUDE.md, this log v2.0, open-parameters.md, REBUILD_KICKOFF.md.
- Oct 21 (scoring brain): **D40 logged; steps 1-2 built.** `features/technical.py` (Pillar A) and `features/fundamental.py` (Pillar B). **Standing convention adopted for every feature module: insufficient data returns `None`, never a number or `False` — the scoring layer owns abstention.** And any feature computed from a series the engine accumulates itself reports an `EvidenceDepth` (observations AND calendar span) beside its value, so the Shadow Book can record how much evidence an idea stood on and D39's calibration can later test whether depth predicts reliability. Revision momentum is **correct at 2 snapshots and trustworthy at 60**: the value is reported from two, never gated to None, because consensus moving 2.00 -> 2.39 moved exactly that much — depth carries the uncertainty instead. That differs from IV rank, which IS gated, because a rank has no answer at all without a high and a low. **Depth is never folded into the value**: a feature that quietly shrank its own number on thin evidence would be making a scoring decision.
- **Oct 21: PRODUCTION LIVE — gates 3 and 4 CLOSED.** Droplet deployed, cron running on ET, first production `DAY COMPLETE` on 2026-10-21. **Both production D7 clocks start 2026-10-21 on the VM**; the Codespace database is dev-only and its 2026-10-21 history is a debugging artefact, not a record. D16 amended: Telegram added as the page channel behind `alerts/notify.py`. Two more real bugs fixed in `fmp_events`: `cuts` was reporting the UPDATE's rowcount (the live `cuts=84` was 84 rows examined, **0 actual cuts**, 82 with no prior dividend at all), and a restated amount left a stale `is_dividend_cut` flag that could never clear. Still open: `--backfill-actions` on the VM (O7).
- **Oct 21: FMP Premium live; gate 1b CLOSED.** Probe 15/15. Universe from the API (503 + 101 constituents, 433 kept of 518). `fmp_estimates` probed clean, flipped to `is_critical`, and took its **FIRST SNAPSHOT** — **the estimate-revision clock starts 2026-10-21**. Budget 250 → 9,000 (sized from a measured 6,759-call cold start), limiter 300 → 600. D29 free-tier mode retired to a documented fallback. **Six more defects**, five of them hidden by the free tier rather than caused by the upgrade — see "WHAT THE PREMIUM UPGRADE FOUND". Next: gate 3 (VM).
- Oct 20 (strategy): **D32–D39 adopted.** Sentiment demoted to 10 and Fundamental raised to 30 (D32); cross-sectional ranking (D33); Tier A diversity gate requiring Technical + one of Fundamental/Catalyst (D34); barbell sleeves with a one-way profit ratchet (D35); IV rank selecting the vehicle (D36); pyramiding that never raises initial risk (D37); sector correlation guard (D38); Phase 2 learning-loop guardrails (D39). Six new `[OPEN]` parameters (#32–#37), all validated by the loader so a typo fails at startup, none read by any code yet. No scoring/decision/allocation logic was written — these bind those layers when they are built, and the build map in "What is NOT done" records which decision binds which layer.
- Oct 20 (later still): **hardening.** Provider-exhausted marker so a spent allowance is discovered once per day rather than once per symbol; live and scratch databases separated by protection class after the integration suite wiped a day of collection. `extrinsicValue` diagnosed as distance-from-the-money and recorded in CLAUDE.md as unusable by construction, not merely imprecise.
- Oct 20 (later): **gate 1a mostly live, gate 2 live (`64be4fd`).** Schwab token written Oct 20, **expires Oct 27**. Chain fixture diff: zero drift in 47 days. Six defects found and fixed by running against real providers — see "WHAT THE FIRST LIVE RUN FOUND". **The estimate-revision clock remains STOPPED**: `/stable/analyst-estimates` is still 402 on this plan, so no snapshot has ever been taken and D7's revision history has not begun. It starts on FMP upgrade day, and that date must be recorded here. Next: fresh FMP allowance -> re-run the universe to convergence, then gate 3 (VM).
- Oct 20: **rebuild complete through step 7 (`426cde7`).** Reconstructed in one session; everything pushed as it was built. Three things the rebuild got *better* than the original: the FMP call budget is now a shared DB-backed day-level counter rather than per-job caps (two jobs could previously stay within budget and jointly exceed the day); universe enrichment seeds from stored volumes so coverage converges instead of oscillating, and spends leftover budget on the stalest symbols first; and `jobs/daily_close.py` reads `day_complete` from `v_day_status` rather than from its own step results, so a drifting job list cannot produce a false "complete". Next session: gate 1a — keys into `.env`, re-probe FMP, then Schwab auth and the chain fixture diff (gate 2).
