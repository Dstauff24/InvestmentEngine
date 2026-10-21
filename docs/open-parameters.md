# Open Parameters (rebuild baseline, Oct 20 2026)

Config values no decision (D1–D42) fixes a number for. Each carries a provisional default in `config/*.yaml` tagged `# [OPEN]` and a row here; `test_every_open_value_is_documented` keeps the tag count and this doc in sync. Rows 1–31 were approved Sep 1, 2026 as provisional starting values, with three adjustments (#12, #15, #18) applied below. Rows 32–37 arrived with the D32–D39 strategy refinements on Oct 20, 2026 and have never been exercised against data. Rows 38–44 are D40's promotion-ladder thresholds, added Oct 21, 2026; they cannot be exercised until a recipe has closed ideas. D13's quarterly Shadow Book review is where they stop being guesses.

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
| 30 | `events.backward_days` | `100` | Catches actuals landing on known dates — AND feeds `post_earnings_drift` | Raised from 7 on 2026-10-21. Earnings are QUARTERLY, so a 7-day window held a symbol's last report for 7 of every ~92 days: the events table held zero past earnings and the drift recipe refused on all 433 symbols. A cross-file invariant in `validate_all` now requires this to exceed `post_earnings_drift.max_days_since_earnings`. | High |
| 31 | `fmp.daily_call_budget` | `12000` | Premium from 2026-10-21 (was `250` under D29's free tier, `9000` until O7's backfill) | Sized from measurement, not taste: a cold start costs 6,759 calls (universe 527 + estimates 1,039 + fundamentals 5,190 + events 3). Raised to 12,000 on 2026-10-21 because O7's one-time history backfill adds ~1,321 (actions 866 + bars 434 + earnings windows 21) and can land on a day the cold start already spent ~9,000. Its job changed with the upgrade — no longer "stay inside the allowance" but "stop a runaway loop before it makes 200,000 calls", which 12,000 still does. | Medium |

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

## Added by step 5 — the feature→pillar-score mapping (Oct 21 2026)

Every pillar score is 0-100. Each sub-signal maps its feature value onto that
scale, and a pillar is the **mean of the sub-signals that scored** (D41's
renormalization, one level down).

**Equal weighting within a pillar is a decision, not a missing parameter.**
D2/D32 fix the weights BETWEEN pillars because the log states them; nothing
states whether revision momentum should outrank margin trend inside Pillar B,
and a number there would be false precision dressed as configuration. D13's
quarterly review introduces sub-weights when there is evidence to set them. So
the rows below are the mapping SCALES and WINDOWS — the judgment calls that
cannot be avoided rather than the ones that can.

A signed signal maps as `50 + 50 × clamp(x / full_credit, −1, +1)`: neutral at
50, saturating at ±full_credit. Saturation is deliberate — a company growing
400% is not sixteen times better than one growing 25%, and an unbounded map
would let one wild sub-signal dominate its pillar.

| # | Parameter | Default | Derives from | What moves if it changes | Sens. |
|---|---|---|---|---|---|
| 52 | `mapping.min_scored_pillars` | `2` | D41 sets the rule, not the floor | How few pillars may carry a composite. Technical is required separately, so this is really "Technical plus one". Raise it and thin-evidence ideas vanish; lower it and a single pillar decides. | High |
| 53 | `mapping.technical.rs_full_credit_pct` | `15.0` | D33 makes relative strength a Pillar A input; no scale | Quarterly outperformance (percentage points) earning full credit. Lower makes every sector leader look exceptional. | High |
| 54 | `mapping.technical.rsi_healthy_low` | `45.0` | Nothing; RSI "health" is undefined | Bottom of the band scoring 100. NOT recipe 1's 40-50 reset band, which is separate — this asks "is momentum healthy", not "is this a pullback entry". | Medium |
| 55 | `mapping.technical.rsi_healthy_high` | `70.0` | Same | Top of that band. Above it the score falls off, because an RSI of 95 is not better than 60 — it is a different and worse thing. | Medium |
| 56 | `mapping.fundamental.growth_full_credit_pct` | `25.0` | D2 names growth; sets no scale | YoY revenue/EPS growth earning full credit. Revenue and EPS are averaged into ONE growth sub-signal, so a company reporting both does not get double the influence. | High |
| 57 | `mapping.fundamental.acceleration_full_credit_pp` | `10.0` | Pillar B reads acceleration | Change in the growth RATE earning full credit, in points. The signal D2 cares about more than level. | High |
| 58 | `mapping.fundamental.margin_trend_full_credit_pp` | `2.0` | Same | Margin expansion earning full credit, in points. Small on purpose: two points of gross-margin expansion in a year is a lot. | Medium |
| 59 | `mapping.fundamental.revision_full_credit_pct` | `5.0` | D7's revision momentum; no scale | Consensus EPS revision earning full credit. The live 2.00→2.39 example is +19.5%, which saturates — deliberately, since a 20% revision and a 40% one are both "sharply up". | High |
| 60 | `mapping.fundamental.revision_min_observations` | `4` | D41 + `features/evidence.py` | Where "correct at 2 snapshots, trustworthy at 60" becomes "trustworthy enough to SCORE". Below it revision abstains instead of contributing a noisy trend estimate. **Nothing has 4 snapshots yet** — the series started 2026-10-21. | High |
| 61 | `mapping.catalyst.earnings_near_days` | `10` | D2 names catalyst proximity; no window | Earnings inside this many days score 100. | Medium |
| 62 | `mapping.catalyst.earnings_horizon_days` | `45` | Same | Beyond this, earnings score `None` rather than 0 — a catalyst 200 days out is no news, not bad news, and scoring it 0 would drag Pillar C down for every stock with nothing scheduled. | Medium |
| 63 | `mapping.catalyst.drift_window_days` | `30` | Recipe 3's territory | How long after earnings still counts as a catalyst (post-earnings drift). | Medium |
| 64 | `mapping.catalyst.ex_div_penalty_days` | `3` | D19(3) forbids a new swing long within 2 days | An ex-date inside this many days scores 0 — a RISK, so it subtracts rather than abstaining. One day wider than D19's rule so the gate bites before the rule does. | Medium |
| 65 | `mapping.sentiment.tone_delta_full_credit` | `0.25` | D32 says Pillar D measures change; no scale | Tone delta earning full credit on the scorer's −1..+1 scale. Scorer-specific: **a Phase 2 Claude scorer will need this re-tuned**, which is exactly why `scorer_id` is recorded. | Medium |
| 66 | `mapping.sentiment.volume_spike_full_credit` | `3.0` | Same | Coverage multiple earning full credit. Maps from 1.0 (normal) UPWARD only and never below neutral — quiet news is not bearish. | Low |

## Added by step 6 — sector membership and trade-plan geometry (Oct 21 2026)

**The geometry rows should have existed since D25 and did not.** `risk.yaml`
carried the trim fractions and the move-stop-to-breakeven rule, but the stop
DISTANCE and the target levels were nowhere — so the first recipe would have
hard-coded them.

**Targets come from STRUCTURE, not from R multiples.** Setting TP2 at "3R"
would make R:R a *constant*, and D12's "Tier A needs R:R ≥ 2, Tier B ≥ 3"
would then be satisfied by construction on every idea — a gate that can never
bind. Hanging targets on levels the market actually made means R:R varies and
the gate does real work.

| # | Parameter | Default | Derives from | What moves if it changes | Sens. |
|---|---|---|---|---|---|
| 67 | `sector.min_member_fraction` | `0.8` | D33 needs a sector index; no rule for membership | Fraction of members that must have data on a date for it to count. Stops the early part of a window becoming a one-stock index: on a date where 3 of 40 had listed, the "sector" would be those three. `min_members` (3) is a HARD floor over this fraction, not the reverse. | Medium |
| 68 | `geometry.stop_atr_buffer` | `0.5` | D17 sizes against a stop; no distance | ATR multiple below the structural level. Too tight and ordinary noise takes the trade out at the exact level everyone can see; too wide and position size shrinks for the same conviction. | High |
| 69 | `geometry.entry_zone_atr` | `0.3` | Nothing; "entry zone" is undefined | Half-width of the entry zone. A zone rather than a price because a limit at one number either fills or watches the trade leave. | Medium |
| 70 | `geometry.min_stop_atr` | `0.5` | Nothing | Below this the plan is REFUSED: a stop inside the noise produces a flattering R:R the market takes out on an ordinary day. | High |
| 71 | `geometry.pullback_to_trend.swing_lookback_days` | `60` | Recipe 1 buys a pullback; no window | Window for the prior swing high the pullback should reclaim, which is TP1. | Medium |
| 72 | `geometry.pullback_to_trend.tp2_measured_move` | `1.6` | D11's TP1/TP2 runner structure | TP2 as a multiple of the TP1 *distance*. Loader requires >1.0, or the runner has nowhere to run and D11's structure collapses to one target. | High |
| 73 | `geometry.base_breakout.tp1_depth_multiple` | `1.0` | Recipe 2; no target rule | TP1 as a multiple of base depth — the classic measured move. | High |
| 74 | `geometry.base_breakout.tp2_depth_multiple` | `1.8` | Same | TP2's extension. Loader requires it to exceed TP1's. | High |
| 75 | `geometry.post_earnings_drift.tp1_gap_multiple` | `1.0` | Recipe 3; no target rule | TP1 as a multiple of the gap — the market's own statement about what the news was worth. | High |
| 76 | `geometry.post_earnings_drift.tp2_gap_multiple` | `2.0` | Same | TP2's extension. Loader requires it to exceed TP1's. | High |

## How to change one
1. Edit the value in `config/*.yaml`, keep the `# [OPEN]` tag. 2. Update its row here. 3. `python -m core.config` (cross-field invariants may reject it). 4. `pytest`. If a decision fixes a value, drop the tag, remove the row, log the decision, add a named assertion in `tests/test_config.py`.
