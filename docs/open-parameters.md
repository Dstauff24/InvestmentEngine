# Open Parameters (rebuild baseline, Oct 20 2026)

Config values no decision (D1–D31) fixes a number for. Each carries a provisional default in `config/*.yaml` tagged `# [OPEN]` and a row here; `test_every_open_value_is_documented` keeps the tag count and this doc in sync. All were approved Sep 1, 2026 as provisional starting values, with three adjustments (#12, #15, #18) applied below. D13's quarterly Shadow Book review is where they stop being guesses.

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
| 31 | `fmp.daily_call_budget` | `250` | D29 free-tier operation | Keeps the fundamentals crawl inside the free plan; raise on upgrade. | Medium |

## How to change one
1. Edit the value in `config/*.yaml`, keep the `# [OPEN]` tag. 2. Update its row here. 3. `python -m core.config` (cross-field invariants may reject it). 4. `pytest`. If a decision fixes a value, drop the tag, remove the row, log the decision, add a named assertion in `tests/test_config.py`.
