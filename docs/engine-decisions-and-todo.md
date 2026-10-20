# Investment Engine — Decision Log & To-Do Tracker
**v2.0 (rebuild baseline) — reconstructed Oct 20, 2026 after loss of the original repo. This file is canonical (D26); the design chat supplies paste-ready entries.**

Companion: `CLAUDE.md` (conventions + validated findings), `docs/open-parameters.md`, `docs/REBUILD_KICKOFF.md`.

---

## WHAT HAPPENED (session note, Oct 20)
The original build completed all 7 kickoff steps between Aug 31 and Sep 3, 2026: 411 unit tests + 9 integration tests, live-validated against FMP `/stable/` and the Schwab Trader API. The work lived only in a GitHub Codespace and was never committed or pushed. The Codespace auto-deleted after ~30 days of inactivity; the repo on GitHub held only a README. The prior Claude Code session was recovered and can be resumed, but its filesystem is empty. Nothing irreplaceable was lost: the D7 snapshot clock had not started (no VM, no cron), the Schwab token had already expired (7-day life, created Sep 3), and FMP was never upgraded. Rebuild proceeds from this log + CLAUDE.md + the resumed session's own transcript.

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
| D12 | Aug 30 | **Two-tier ideas.** Tier A: ≥3 pillars (each ≥ min_score), composite ≥70, R:R ≥2:1, standard size. Tier B: compelling asymmetry, 2 pillars or composite 55–69, R:R ≥3:1, reduced size, monthly cap, defined-risk structures preferred. Sizing is the safety valve, not prohibition. |
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
| D29 | Sep 3 | **FMP upgrade deferred; engine runs in free-tier mode.** Account is on `/stable/`; free tier 402-gates sp500-constituent, nasdaq-constituent, analyst-estimates, ratios, key-metrics. Mitigations: static constituents file, degrade-on-402 fundamentals, estimates marked expected-blocked, 250/day call-budget guard. Explicit cost: the estimate-revision history clock starts on upgrade day. Target on upgrade remains Premium. Phase 2 note: transcripts are Ultimate-gated; decide then. |
| D30 | Oct 20 | **Commit and push at the end of every session and after every completed step. No exceptions.** GitHub is the system of record; Codespaces are disposable. A step is not "complete" without a pushed commit named in the report. |
| D31 | Oct 20 | **Rebuild authorized** from this log + CLAUDE.md + the resumed Claude Code session's transcript, per `docs/REBUILD_KICKOFF.md`. All prior approvals carry over; no design re-litigation needed. |

---

## ⏳ OPEN ITEMS

| # | Item | Status |
|---|------|--------|
| O7 | Backtest depth before live alerts | Open. Suggest 3–5 yrs daily, walk-forward. Prerequisite: run `--backfill-actions` back to the start of price history (silent-failure risk otherwise: wrong total-return series for every early bar). |
| O8 | Schwab weekly re-auth | Manual copy-paste flow is settled. Open: token sync mechanism to VM (scp vs object storage). `token_expiry_check.py` emails 24h before expiry. |
| O9 | [OPEN] config parameters | Reviewed Sep 1: all provisional defaults approved with three adjustments (max_days_since_earnings 5→10, assignment_extrinsic_buffer 1.0→1.25, deactivate_after_absent_days 3→5). |

---

## 🎯 EDGE SOURCES (unchanged)
1. Selectivity (the engine can say "nothing today"). 2. Estimate-revision momentum. 3. Post-earnings drift. 4. Volatility risk premium via self-built IV rank. 5. Event-risk awareness (never blindsided by earnings/ex-div inside a position). 6. Retail time/size flexibility. 7. The Shadow Book feedback loop. 8. Emotion removal: plans computed before money is at risk.

---

## 📋 GO-LIVE CHECKLIST (rebuilt)

**Gate 0 — Rebuild the repo (NEW).** Follow `docs/REBUILD_KICKOFF.md`. First commit = docs + scaffold, pushed before anything else. Every step ends with a pushed commit (D30).
**Gate 1a — Free-tier live.** FMP/Finnhub keys in `.env` (keys still exist in their dashboards); probe confirms `/stable/`; universe from static constituents; events + news + degraded fundamentals running.
**Gate 1b — Premium unlock (deferred).** Upgrade → re-probe 15/15 → estimates flips to is_critical → record the estimate-clock start date → universe from API.
**Gate 2 — Schwab live.** App credentials still exist in the Dev Portal (approvals persist; nothing to re-apply for). Keys into `.env` → `python scripts/schwab_auth.py` (manual flow) → fixture diff against a fresh live chain → `has_options` backfill → universe re-run.
**Gate 3 — VM.** Provision (Hetzner/DO), Postgres 16 + Timescale, deploy, `--backfill-actions`, restore-test the backup.
**Gate 4 — Cron on.** First `day_complete = true`; record the date; the D7 IV clock starts. After gate 4 the engine is collecting, not yet deciding: scoring, recipes, decision layer, Shadow Book, digest are the remaining Phase 1 work.

---

## 🗒 SESSION NOTES
- Aug 30 – Sep 3: original build, steps 1–7 complete and live-validated (see CLAUDE.md "Validated API findings" for everything learned). Full detail preserved in the design chat and the prior Claude Code session transcript.
- Oct 20: loss discovered (unpushed Codespace auto-deleted). D30, D31 adopted. Rebuild package authored: CLAUDE.md, this log v2.0, open-parameters.md, REBUILD_KICKOFF.md.
