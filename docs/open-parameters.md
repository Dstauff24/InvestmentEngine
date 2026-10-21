# Open Parameters (rebuild baseline, Oct 20 2026)

Config values no decision (D1–D40) fixes a number for. Each carries a provisional default in `config/*.yaml` tagged `# [OPEN]` and a row here; `test_every_open_value_is_documented` keeps the tag count and this doc in sync. Rows 1–31 were approved Sep 1, 2026 as provisional starting values, with three adjustments (#12, #15, #18) applied below. Rows 32–37 arrived with the D32–D39 strategy refinements on Oct 20, 2026 and have never been exercised against data. Rows 38–44 are D40's promotion-ladder thresholds, added Oct 21, 2026; they cannot be exercised until a recipe has closed ideas. D13's quarterly Shadow Book review is where they stop being guesses.

Everything NOT tagged `[OPEN]` traces to a decision and is asserted by a named test in `tests/test_config.py`.

Sensitivity: High = changes which ideas surface or how they are sized. Medium = changes how often a recipe fires or how much data is stored. Low = housekeeping.

## config/scoring.yaml

| # | Parameter | Default | Derives from | What moves if it changes | Sens. |
|---|---|---|---|---|---|
| 1 | `pillar_agreement.min_score` | `60` | D2/D12 count "agreeing pillars"; neither defines agree | The single most load-bearing number in the repo. Sets the Tier A ≥3 and Tier B 2-pillar gates. First sensitivity sweep once O7 backtests exist. | High |
| 2 | `composite.conflict_below` | `30` | Framework: conflicting pillars cap the score; level undefined | Which pillars count as arguing against an idea. | High |
| 3 | `composite.capped_at` | `69` | Same; one point below the Tier A gate on purpose | Ceiling for a conflicted idea: never Tier A, still Tier B. Loader rejects ≥70. | High |
| 4 | `recipes.pullback_to_trend.rsi_period` | `14` | Recipe 1 gives the 40–50 band, not the period | Standard Wilder default. | Low |
| 5 | `recipes.pullback_to_trend.volume_dryup_ratio` | `0.8` | Recipe 1 requires "volume dry-up" | Pullback volume vs 20-day average. | Medium |
| 6 | `recipes.base_breakout.min_base_weeks` | `4` | "multi-week base" | Main driver of breakout candidate count. | Medium |
| 7 | `recipes.base_breakout.max_base_depth_pct` | `25` | Implied by "base" | A deep base is a downtrend in base's clothes. | Medium |
| 8 | `recipes.base_breakout.range_contraction_ratio` | `0.6` | "range contraction" | Recent ATR vs base-period ATR. Loader rejects ≥1. | Medium |
| 9 | `recipes.base_breakout.breakout_buffer_pct` | `0.5` | "on the break" | Confirmation distance past the base high. | Medium |
| 10 | `recipes.base_breakout.horizon_days` | `[5, 30]` | Swing horizon given for recipe 1 only | Shadow Book timeout-labelling window for this recipe. | Medium |
| 11 | `recipes.post_earnings_drift.min_gap_up_pct` | `2.0` | "gap-up that holds day-1 low" | What counts as a gap. Likely tightening to 3.0 if noisy. | Medium |
| 12 | `recipes.post_earnings_drift.max_days_since_earnings` | `10` | Drift is documented over weeks | Entry window after the print. Adjusted 5→10 on Sep 1: day-1-low confirmation takes sessions, and the day-5-to-8 gap retest is the cleanest entry. | Medium |
| 13 | `recipes.post_earnings_drift.horizon_days` | `[5, 45]` | Same anomaly, no exact horizon | Outcome-labelling window. | Medium |

## config/risk.yaml

| # | Parameter | Default | Derives from | What moves if it changes | Sens. |
|---|---|---|---|---|---|
| 14 | `tier_b_budget.resets_on` | `calendar_month` | D18 says monthly | Calendar vs rolling 30d. Visible reset date is a feature. | Medium |
| 15 | `options.assignment_extrinsic_buffer` | `1.25` | D19(4) | Multiplier on extrinsic-vs-dividend. Adjusted 1.0→1.25 on Sep 1: a false warning costs nothing, a surprise assignment costs money. | Medium |
| 16 | `exits.swing.max_holding_days` | `45` | Needed to label Shadow Book outcomes | Swings only; options expire; holds have no clock. Loader rejects a day-count on option/hold. | Medium |
| 17 | `exits.option.early_exit_dte` | `21` | Standard practice, pre-gamma/theta acceleration | Phase 3 options module; declared, nothing reads it yet. | Medium |

## config/universe.yaml

| # | Parameter | Default | Derives from | What moves if it changes | Sens. |
|---|---|---|---|---|---|
| 18 | `liquidity.min_price` | `5.00` | Not in D14 | Protects option strategies from sub-$5 names. | Low |
| 19 | `maintenance.deactivate_after_absent_days` | `5` | D14 implies maintenance | Adjusted 3→5 on Sep 1: a holiday-adjacent API hiccup can span 3 days. | Low |
| 20 | `maintenance.max_universe_size` | `600` | D14 says ~500 | Sanity ceiling; malformed constituent responses must fail the job. | Low |
| 21 | `option_chains.min_open_interest` | `0` | D22 fixes strikes/expirations, not an OI floor | The one value expensive to get wrong in only one direction: it governs what gets STORED. Narrowing later leaves a permanent hole in the D7 series. Filter at read time instead. | Medium |
| 22 | `option_chains.iv_rank_lookback_days` | `252` | D7 promises IV rank | One trading year, the conventional window. | Medium |
| 23 | `option_chains.iv_rank_min_history_days` | `60` | Step 6 behavior (59d → None, 60d → rank) | Minimum stored history before a rank is reported instead of NULL. | Low |
| 24 | `estimates.quarters_ahead` | `4` | D7 dated estimate snapshots | Raw material for revision momentum. | Medium |
| 25 | `estimates.annuals_ahead` | `2` | Same | Same, annual periods. | Low |
| 26 | `fundamentals.quarters_history` | `12` | Pillar B reads acceleration and margin trend | 3 years of quarters. | Medium |
| 27 | `fundamentals.annuals_history` | `5` | Same | | Low |
| 28 | `fundamentals.refresh_after_days` | `7` | Cost control | Staleness-driven refresh (also re-pulls when a symbol has reported since). | Low |
| 29 | `events.forward_days` | `90` | Must exceed the longest recipe horizon (45d) | Event radar depth. | Low |
| 30 | `events.backward_days` | `7` | Catches actuals landing on known dates | | Low |
| 31 | `fmp.daily_call_budget` | `9000` | Premium from 2026-10-21 (was `250` under D29's free tier) | Sized from measurement, not taste: a cold start costs 6,759 calls (universe 527 + estimates 1,039 + fundamentals 5,190 + events 3), so 9,000 leaves ~30% headroom. Its job changed with the upgrade — no longer "stay inside the allowance" but "stop a runaway loop before it makes 200,000 calls". | Medium |

## Added by the D32–D39 strategy refinements (Oct 20 2026)

These bind layers that **do not exist yet** — scoring, decision, allocation. They
are validated by the loader so a typo fails at startup, and nothing reads them
until those layers are built. Their defaults are therefore weaker than rows
1–31: those at least describe data the engine already handles.

| # | Parameter | Default | Derives from | What moves if it changes | Sens. |
|---|---|---|---|---|---|
| 32 | `ranking.max_ideas_per_day` | `5` | D33 ranks by percentile and caps the digest; sets no number | How much the digest shows. A reading limit, not a market fact — but set it high and the ranking stops being a filter, set it to 1 and a single bad day's top rank becomes the whole recommendation. | High |
| 33 | `vehicle_by_iv_rank.iv_vehicle_low` | `30` | D36 defines three bands, no boundaries | Below this the engine BUYS optionality. Too high and it pays up for premium in ordinary vol; too low and the convexity D35 wants never appears. | High |
| 34 | `vehicle_by_iv_rank.iv_vehicle_high` | `50` | Same | Above this premium SELLING is allowed. The loader rejects `high <= low`, which would erase the shares-by-default middle band. | High |
| 35 | `sleeves.ratchet.trigger_excess_points` | `3.0` | D35 fixes the ratchet, calls 3 points a judgement | How far Growth may run above its 35% target before gains are swept. Smaller = the floor rises often in small steps; larger = fewer, bigger sweeps and more time spent over-allocated to risk. | High |
| 36 | `sleeves.ratchet.sweep_fraction` | `0.5` | Same | How much of the excess moves to Core permanently. 1.0 would pin Growth to its target and forfeit compounding inside the sleeve; 0 disables the ratchet, which the loader rejects. | High |
| 37 | `sentiment_reinstatement.min_sample` | `30` | D32 says "30 or more closed ideas"; the floor is itself a sample-size judgement | How much evidence Sentiment must produce before its weight may be restored toward 20. Too low and noise gets promoted on a lucky streak — which is exactly how the pillar earned its demotion. | Medium |

## Added by D40's promotion ladder (Oct 21 2026)

D40 fixes the **structure** — three statuses, a 30-closed-idea floor, automatic
demotion — and deliberately leaves the thresholds open, because no decision
sets them and burying a guess in prose would make it unreviewable. **These are
the numbers that decide whether real money follows a strategy**, so they are
the highest-stakes rows in this document and the least informed: nothing has 30
closed ideas yet. Expect all seven to move at the first quarterly review that
has data (D13).

The loader rejects combinations that contradict each other — a LIVE bar below
the PAPER bar, a demotion threshold at or above the promotion one (which would
oscillate for ever), or a demotion sample floor above the promotion floor
(which would make a bad run unable to remove a strategy it was able to add).

| # | Parameter | Default | Derives from | What moves if it changes | Sens. |
|---|---|---|---|---|---|
| 38 | `promotion.shadow_to_paper.min_expectancy_r` | `0.10` | D40 fixes the 30-idea floor, not the bar | How good a strategy must look before it is sized against a virtual account. Too low and PAPER fills with noise; too high and nothing ever leaves SHADOW. In R-multiples, so it is comparable across recipes. | High |
| 39 | `promotion.paper_to_live.sustained_window_days` | `90` | D40 says "sustained"; no window | What "sustained" means. One quarter is the shortest window that spans an earnings cycle, which matters because two of three recipes are earnings-adjacent. | High |
| 40 | `promotion.paper_to_live.min_expectancy_r` | `0.20` | D40 says "sustained positive expectancy" | The bar for real money. Loader requires it ≥ row 38. | High |
| 41 | `promotion.paper_to_live.max_calibration_error_pct` | `15.0` | D39(1) requires a calibration curve; no tolerance | How far predicted hit rate may sit from realised before LIVE is refused. A composite is a claim about probability; this is how wrong that claim may be. | High |
| 42 | `promotion.demotion.window_days` | `60` | D40 says "sustained underperformance" | How long a strategy may underperform before it is automatically demoted. Shorter than row 39 on purpose: removing a broken strategy should be quicker than promoting a good one. | High |
| 43 | `promotion.demotion.min_closed_ideas` | `20` | Same | How much evidence is needed to demote. Below the promotion floor deliberately — a bad run should be able to remove what a good run added. | High |
| 44 | `promotion.demotion.expectancy_r_below` | `0.0` | Same | The line below which a strategy is demoted. Zero means "stops making money", not "stops being impressive". Loader requires it < row 38 so a strategy cannot be demoted the moment it is promoted. | High |

## Added by step 3 — insider clusters and EDGAR (Oct 21 2026)

D39 names insider clusters as an early Pillar C input and states the rule's
SHAPE — N distinct insiders, a D-day window, a dollar floor — without fixing
any of the three. The code filter is NOT open: "open-market purchase" means SEC
transaction code `P`, and the loader rejects a config that tries to count
derivatives, because an option grant or exercise is compensation on a schedule
somebody set months ago rather than an opinion about today.

| # | Parameter | Default | Derives from | What moves if it changes | Sens. |
|---|---|---|---|---|---|
| 45 | `insider_cluster.min_distinct_insiders` | `3` | D39 says "N or more DISTINCT insiders" | How many separate people must act before it counts as a signal. The loader refuses `1`: a cluster of one is just a purchase, and independent agreement is the whole point. Raise it and clusters become rare but meaningful; lower it and routine director buying fires it. | High |
| 46 | `insider_cluster.window_days` | `90` | D39 says "a rolling window of D days" | How far apart those purchases may be. About a quarter, so it spans one earnings cycle — long enough that insiders acting on the same information are caught together, short enough that two unrelated decisions a season apart are not. | High |
| 47 | `insider_cluster.min_aggregate_usd` | `250000` | D39 says "a minimum aggregate dollar value" | The floor that stops three directors buying $2,000 each from reading as conviction. Absolute dollars, not a fraction of market cap, which would make the rule fire far more readily on small caps. | High |
| 48 | `edgar.daily_call_budget` | `2000` | No decision; SEC publishes no daily cap | A RUNAWAY-LOOP GUARD, not an allowance: the risk is a retry bug hammering a public service that has asked for restraint, and being the reason they introduce a cap. A full universe pass is ~1 + 1/symbol + 1 per new filing, so this clears a busy filing day across ~430 symbols with room to spare. | Medium |

## Added by step 4 — sentiment windows (Oct 21 2026)

D32 demoted Pillar D to weight 10 and says the pillar measures CHANGE; it fixes
none of the windows. The abstention floor is the one the user named explicitly,
and the two window lengths are open for the same D25 reason — no decision sets
them.

The loader rejects `baseline_days <= trailing_days`: the delta would compare a
window against itself and be permanently zero.

| # | Parameter | Default | Derives from | What moves if it changes | Sens. |
|---|---|---|---|---|---|
| 49 | `sentiment.min_articles` | `5` | The abstention floor; no decision sets it | How much coverage a symbol needs before a tone delta is reported at all. Three articles can disagree wildly and average to anything, so below this the reading is `None`. Applies to SCORED articles on BOTH windows — five headlines the lexicon cannot read are not five articles of evidence. | High |
| 50 | `sentiment.trailing_days` | `7` | D32 says "change"; no window | What counts as "just happened". Matches `news.lookback_days`, so the trailing window is exactly what one ingest cycle refreshes. | Medium |
| 51 | `sentiment.baseline_days` | `30` | Same | What "normal" means for this symbol. The baseline EXCLUDES the trailing window, so this is effectively a 23-day comparison period. Longer is more stable and slower to notice a regime change. | Medium |

## How to change one
1. Edit the value in `config/*.yaml`, keep the `# [OPEN]` tag. 2. Update its row here. 3. `python -m core.config` (cross-field invariants may reject it). 4. `pytest`. If a decision fixes a value, drop the tag, remove the row, log the decision, add a named assertion in `tests/test_config.py`.
