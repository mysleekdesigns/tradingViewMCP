---
run_id: r-20261025-125328-e6f036
date: 2026-10-25
outcome: calibration
tags: [research, seed-calibration, freqtrade, calibration]
seeds: 6
pre_holdout_passed: 0
---

# Seed calibration 2026-10-25

Ledger run: [[r-20261025-125328-e6f036]]

## Expectation (stated before the results)

Popular published strategies are expected to FAIL honest tests. This run calibrates the pipeline (if well-known strategies passed easily, the tests would be suspect) and seeds a library of known failures for the research agents.

> Results are historical simulations on the design window only; they are not forward-looking. Calibration never calls the holdout check and records no holdout data.

Success criteria file sha256: `cc544fe9302e2ffdfeb9af8d5c741753812f362e6e508dbd40f4113d55b72126` (frozen into the run).

Ledger size used for DSR (distinct plans across every research run): 202

## Results

6 seeds, 0 passed every pre-holdout criterion, 0 with errors. `pass` / `fail` / `missing` per criterion as reported by the criteria evaluator; holdout and cross-check criteria are not evaluated here.

| Seed | Overall | wf_oos_sharpe_min | dsr_min | pbo_max | beat_buy_hold | beat_vol_target | random_entry_percentile_min | cost_stress_factor | require_gradual_delay_decay | max_drawdown_max | min_trades | min_profitable_regimes | neighbourhood_pct | require_holdout_pass | require_crosscheck_match |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| freqtrade-strategies Bandtastic | failed | missing | fail | missing | fail | fail | fail | fail | fail | fail | pass | fail | fail | missing | missing |
| freqtrade SampleStrategy | failed | missing | fail | missing | fail | fail | fail | fail | fail | fail | fail | pass | fail | missing | missing |
| freqtrade-strategies Strategy001 | failed | missing | fail | missing | fail | fail | fail | fail | pass | fail | pass | pass | fail | missing | missing |
| freqtrade-strategies Strategy002 | failed | missing | fail | missing | fail | fail | fail | fail | fail | fail | pass | fail | fail | missing | missing |
| freqtrade-strategies Strategy003 | failed | missing | fail | missing | fail | fail | fail | fail | fail | fail | pass | fail | fail | missing | missing |
| freqtrade-strategies Strategy005 | failed | missing | fail | missing | fail | fail | fail | fail | fail | fail | fail | fail | missing | missing | missing |

## Seeds

### freqtrade-strategies Bandtastic

- Source: https://github.com/freqtrade/freqtrade-strategies/blob/main/user_data/strategies/Bandtastic.py
- Plan hash: `a11fcc5ff06a594e0d886e354210b4cb17f563f1f262f7f0a0a90a87b80658f0`
- Backtest trial: 178
- Overall: failed
- Missing evidence: wf_oos_sharpe_min, pbo_max, require_holdout_pass, require_crosscheck_match
- Translation notes: Uses the strategy's default hyperopt parameters. Entry (default trigger bb_lower1; the optional RSI/MFI/EMA buy guards are disabled by default): close below the lower Bollinger(20, 1) band. Exit (default trigger sell-bb_upper2): close above the upper Bollinger(20, 2) band; the default-enabled MFI > 46 exit guard is dropped (no MFI in the registry). The band multiplier differs between entry and exit, so the plan uses two Bollinger aliases (bb1, bb2). Bands use close; the original uses typical price. The ROI table (0 min: 16.2%, 69: 9.7%, 229: 6.1%, 566: 0%) becomes a single 16.2% take-profit. Stop-loss 34.5% kept: it is inside the lab's (0, 50] stop-loss bound, so no clamping was needed. The trailing stop (+1% once +5.8% is reached) is dropped: the lab engine has no trailing stop. Timeframe changed 15m -> 1h to match the other seeds.

| Criterion | Status | Value | Threshold | Note |
| --- | --- | --- | --- | --- |
| wf_oos_sharpe_min | missing |  | 1.0 | no walk_forward evidence: run walk_forward |
| dsr_min | fail | 0.006877621474871913 | 0.95 | re-deflated now with N = 85 distinct plans tried in every run of the lab (research runs and the web UI; all symbols and intervals; the report used N = 25) and V[SR] 1.68e-06 = max(V over 25 commensurable trial Sharpes in this run 1.68e-06, V over 25 in every research run 1.68e-06 (same interval, design window, and costs), V over 25 plans pooled across 1 interval(s) 1.68e-06 (every research run, same costs, any design window; each Sharpe annualised with 365-day bars of its interval and rescaled to 1h bars)); latest Sharpe per distinct plan |
| pbo_max | missing |  | 0.2 | PBO needs the sweep basis (CSCV over the design-window Optuna trials of this plan's behaviour family; the ±20% neighbourhood alone measures stability, not selection): 0 sweep(s) of the family with too few usable trials. run_sweep first |
| beat_buy_hold | fail | False | True | net Sharpe -0.65 vs buy-and-hold 1.12 |
| beat_vol_target | fail | False | True | net Sharpe -0.65 vs vol-targeted buy-and-hold 1.24 |
| random_entry_percentile_min | fail | 1.5 | 95.0 | Sharpe percentile vs 200 random-entry paths with the same trade count |
| cost_stress_factor | fail | -0.9993657595465695 | 2.0 | net total return at x2 costs |
| require_gradual_delay_decay | fail | False | True | no edge to decay (Sharpe 0 bars -0.647, 1 bars -0.355, 3 bars -0.284, 5 bars -0.269) |
| max_drawdown_max | fail | 0.9649859586699762 | 0.25 | net max drawdown (fraction) on the design window |
| min_trades | pass | 941.0 | 100 | trades on the design window |
| min_profitable_regimes | fail | 0.0 | 2 | bear -0.632 (11325 active bars), bull -0.517 (18008 active bars), sideways -0.757 (8210 active bars). trailing 90-day simple return (2160 bars): above +10% bull, below -10% bear, else sideways (regime definition from the run's frozen success criteria); bar t's return is assigned to the regime at close t-1 |
| neighbourhood_pct | fail | 0.0 | 0.2 | 0 of 24 one-at-a-time ±20% neighbours profitable (net total return > 0; every tunable has both sides) |
| require_holdout_pass | missing |  | True | no locked holdout evidence yet |
| require_crosscheck_match | missing |  | True | no event-driven cross-check evidence yet: run_crosscheck (a holdout check needs a matched cross-check first) |

### freqtrade SampleStrategy

- Source: https://raw.githubusercontent.com/freqtrade/freqtrade/develop/freqtrade/templates/sample_strategy.py
- Plan hash: `10806a3c2ae9b1ec2f2b7648b285c9bd4e4761cf079ce1f5f4e43379cc52913e`
- Backtest trial: 223
- Overall: failed
- Missing evidence: wf_oos_sharpe_min, pbo_max, require_holdout_pass, require_crosscheck_match
- Translation notes: Long side only (the template's short rules dropped: spot long/flat engine). Entry: RSI(14) crosses above 30 while TEMA(9) is at or below the Bollinger(20, 2) middle band and rising versus the previous bar. Exit: RSI(14) crosses above 70 while TEMA(9) is above the middle band and falling. TEMA is not in the registry, so EMA(9) stands in for it. Bollinger bands use close here; freqtrade's template uses typical price (H+L+C)/3. The ROI table (0 min: 4%, 30 min: 2%, 60 min: 1%) becomes a single 4% take-profit, because time-decaying ROI has no registry equivalent. Stop-loss 10% kept. The template's volume > 0 guard is implicit (zero-volume bars are untradable in the lab). Timeframe changed 5m -> 1h (5m archive data is not kept for research).

| Criterion | Status | Value | Threshold | Note |
| --- | --- | --- | --- | --- |
| wf_oos_sharpe_min | missing |  | 1.0 | no walk_forward evidence: run walk_forward |
| dsr_min | fail | 0.015467832263091773 | 0.95 | re-deflated now with N = 110 distinct plans tried in every run of the lab (research runs and the web UI; all symbols and intervals; the report used N = 50) and V[SR] 1.34e-05 = max(V over 50 commensurable trial Sharpes in this run 1.34e-05, V over 50 in every research run 1.34e-05 (same interval, design window, and costs), V over 50 plans pooled across 1 interval(s) 1.34e-05 (every research run, same costs, any design window; each Sharpe annualised with 365-day bars of its interval and rescaled to 1h bars)); latest Sharpe per distinct plan |
| pbo_max | missing |  | 0.2 | PBO needs the sweep basis (CSCV over the design-window Optuna trials of this plan's behaviour family; the ±20% neighbourhood alone measures stability, not selection): 0 sweep(s) of the family with too few usable trials. run_sweep first |
| beat_buy_hold | fail | False | True | net Sharpe 0.05 vs buy-and-hold 1.12 |
| beat_vol_target | fail | False | True | net Sharpe 0.05 vs vol-targeted buy-and-hold 1.24 |
| random_entry_percentile_min | fail | 20.5 | 95.0 | Sharpe percentile vs 200 random-entry paths with the same trade count |
| cost_stress_factor | fail | -0.24045116387013443 | 2.0 | net total return at x2 costs |
| require_gradual_delay_decay | fail | False | True | suspicious: edge unchanged at 5 bars (Sharpe 0 bars 0.0486, 1 bars 0.135, 3 bars 0.124, 5 bars 0.276) |
| max_drawdown_max | fail | 0.48384956046945593 | 0.25 | net max drawdown (fraction) on the design window |
| min_trades | fail | 56.0 | 100 | trades on the design window |
| min_profitable_regimes | pass | 2.0 | 2 | bear -0.385 (3261 active bars), bull 0.461 (3260 active bars), sideways 0.0525 (2251 active bars). trailing 90-day simple return (2160 bars): above +10% bull, below -10% bear, else sideways (regime definition from the run's frozen success criteria); bar t's return is assigned to the regime at close t-1 |
| neighbourhood_pct | fail | 0.125 | 0.2 | 3 of 24 one-at-a-time ±20% neighbours profitable (net total return > 0; every tunable has both sides); rule literals (thresholds, lags) are NOT perturbed: 30, 1, 70, 1 |
| require_holdout_pass | missing |  | True | no locked holdout evidence yet |
| require_crosscheck_match | missing |  | True | no event-driven cross-check evidence yet: run_crosscheck (a holdout check needs a matched cross-check first) |

### freqtrade-strategies Strategy001

- Source: https://github.com/freqtrade/freqtrade-strategies/blob/main/user_data/strategies/Strategy001.py
- Plan hash: `fcf15706e84dbd0051d1020499ee23a133a7d4d2df5e1589a4aeccb47bf5dee2`
- Backtest trial: 268
- Overall: failed
- Missing evidence: wf_oos_sharpe_min, pbo_max, require_holdout_pass, require_crosscheck_match
- Translation notes: Entry: EMA(20) crosses above EMA(50). The original also requires a green Heikin-Ashi candle closing above EMA(20); Heikin-Ashi is not in the registry, so both HA filters are dropped (more entries than the original). Exit: EMA(50) crosses above EMA(100) - kept as written even though it is odd (a bullish cross used as an exit); the original also requires a red Heikin-Ashi candle closing below EMA(20), dropped. The ROI table (0 min: 5%, 20: 4%, 30: 3%, 60: 1%) becomes a single 5% take-profit. Stop-loss 10% kept; the (disabled) trailing-stop settings are ignored. Timeframe changed 5m -> 1h.

| Criterion | Status | Value | Threshold | Note |
| --- | --- | --- | --- | --- |
| wf_oos_sharpe_min | missing |  | 1.0 | no walk_forward evidence: run walk_forward |
| dsr_min | fail | 0.02874577269392358 | 0.95 | re-deflated now with N = 131 distinct plans tried in every run of the lab (research runs and the web UI; all symbols and intervals; the report used N = 71) and V[SR] 1.57e-05 = max(V over 71 commensurable trial Sharpes in this run 1.57e-05, V over 71 in every research run 1.57e-05 (same interval, design window, and costs), V over 71 plans pooled across 1 interval(s) 1.57e-05 (every research run, same costs, any design window; each Sharpe annualised with 365-day bars of its interval and rescaled to 1h bars)); latest Sharpe per distinct plan |
| pbo_max | missing |  | 0.2 | PBO needs the sweep basis (CSCV over the design-window Optuna trials of this plan's behaviour family; the ±20% neighbourhood alone measures stability, not selection): 0 sweep(s) of the family with too few usable trials. run_sweep first |
| beat_buy_hold | fail | False | True | net Sharpe 0.24 vs buy-and-hold 1.12 |
| beat_vol_target | fail | False | True | net Sharpe 0.24 vs vol-targeted buy-and-hold 1.24 |
| random_entry_percentile_min | fail | 49.0 | 95.0 | Sharpe percentile vs 200 random-entry paths with the same trade count |
| cost_stress_factor | fail | -0.6429612551711716 | 2.0 | net total return at x2 costs |
| require_gradual_delay_decay | pass | True | True | edge decays gradually (Sharpe 0 bars 0.241, 1 bars 0.243, 3 bars 0.0904, 5 bars 0.165) |
| max_drawdown_max | fail | 0.43579492071912673 | 0.25 | net max drawdown (fraction) on the design window |
| min_trades | pass | 289.0 | 100 | trades on the design window |
| min_profitable_regimes | pass | 2.0 | 2 | bear 0.151 (5888 active bars), bull 0.0611 (8899 active bars), sideways -0.013 (5524 active bars). trailing 90-day simple return (2160 bars): above +10% bull, below -10% bear, else sideways (regime definition from the run's frozen success criteria); bar t's return is assigned to the regime at close t-1 |
| neighbourhood_pct | fail | 0.6 | 0.2 | 12 of 20 one-at-a-time ±20% neighbours profitable (net total return > 0; every tunable has both sides) |
| require_holdout_pass | missing |  | True | no locked holdout evidence yet |
| require_crosscheck_match | missing |  | True | no event-driven cross-check evidence yet: run_crosscheck (a holdout check needs a matched cross-check first) |

### freqtrade-strategies Strategy002

- Source: https://github.com/freqtrade/freqtrade-strategies/blob/main/user_data/strategies/Strategy002.py
- Plan hash: `6bfb87462e80f582dcf375cadce00cd92c75f5613afec1605dd6526992fa847d`
- Backtest trial: 309
- Overall: failed
- Missing evidence: wf_oos_sharpe_min, pbo_max, require_holdout_pass, require_crosscheck_match
- Translation notes: Entry: RSI(14) below 30 and close below the lower Bollinger(20, 2) band (bands on close here; the original uses typical price). Dropped from the entry: slow stochastic %K < 20 (no stochastic in the registry) and a TA-Lib hammer candle (no candle patterns). Exit: the original exits when Parabolic SAR is above close and Fisher-RSI > 0.3. Fisher-RSI is tanh(0.1 x (RSI - 50)), a monotone transform of RSI, so Fisher-RSI > 0.3 is exactly RSI > 53.1 (50 + 10 x atanh(0.3)). SAR is not in the registry; 'SAR above close' (price has turned down) is approximated by close below the previous close. The ROI table (0 min: 5%, 20: 4%, 30: 3%, 60: 1%) becomes a single 5% take-profit. Stop-loss 10% kept. Timeframe changed 5m -> 1h.

| Criterion | Status | Value | Threshold | Note |
| --- | --- | --- | --- | --- |
| wf_oos_sharpe_min | missing |  | 1.0 | no walk_forward evidence: run walk_forward |
| dsr_min | fail | 1.9828148501006217e-12 | 0.95 | re-deflated now with N = 152 distinct plans tried in every run of the lab (research runs and the web UI; all symbols and intervals; the report used N = 92) and V[SR] 3.31e-05 = max(V over 92 commensurable trial Sharpes in this run 3.31e-05, V over 92 in every research run 3.31e-05 (same interval, design window, and costs), V over 92 plans pooled across 1 interval(s) 3.31e-05 (every research run, same costs, any design window; each Sharpe annualised with 365-day bars of its interval and rescaled to 1h bars)); latest Sharpe per distinct plan |
| pbo_max | missing |  | 0.2 | PBO needs the sweep basis (CSCV over the design-window Optuna trials of this plan's behaviour family; the ±20% neighbourhood alone measures stability, not selection): 0 sweep(s) of the family with too few usable trials. run_sweep first |
| beat_buy_hold | fail | False | True | net Sharpe -1.18 vs buy-and-hold 1.12 |
| beat_vol_target | fail | False | True | net Sharpe -1.18 vs vol-targeted buy-and-hold 1.24 |
| random_entry_percentile_min | fail | 0.0 | 95.0 | Sharpe percentile vs 200 random-entry paths with the same trade count |
| cost_stress_factor | fail | -0.9961613482494868 | 2.0 | net total return at x2 costs |
| require_gradual_delay_decay | fail | False | True | no edge to decay (Sharpe 0 bars -1.18, 1 bars -0.626, 3 bars -0.581, 5 bars -0.341) |
| max_drawdown_max | fail | 0.963453528594169 | 0.25 | net max drawdown (fraction) on the design window |
| min_trades | pass | 377.0 | 100 | trades on the design window |
| min_profitable_regimes | fail | 0.0 | 2 | bear -0.749 (4483 active bars), bull -0.672 (6094 active bars), sideways -0.485 (2947 active bars). trailing 90-day simple return (2160 bars): above +10% bull, below -10% bear, else sideways (regime definition from the run's frozen success criteria); bar t's return is assigned to the regime at close t-1 |
| neighbourhood_pct | fail | 0.0 | 0.2 | 0 of 20 one-at-a-time ±20% neighbours profitable (net total return > 0; every tunable has both sides); rule literals (thresholds, lags) are NOT perturbed: 30, 53.1, 1 |
| require_holdout_pass | missing |  | True | no locked holdout evidence yet |
| require_crosscheck_match | missing |  | True | no event-driven cross-check evidence yet: run_crosscheck (a holdout check needs a matched cross-check first) |

### freqtrade-strategies Strategy003

- Source: https://github.com/freqtrade/freqtrade-strategies/blob/main/user_data/strategies/Strategy003.py
- Plan hash: `d5251b0e476f15bc97d16af662d5b7fa1ec281736d12a8f750532c52a10d3cd4`
- Backtest trial: 350
- Overall: failed
- Missing evidence: wf_oos_sharpe_min, pbo_max, require_holdout_pass, require_crosscheck_match
- Translation notes: Entry: RSI(14) below 28 (and above 0), close below SMA(40), and EMA(50) above EMA(100). Four features, the plan limit. Dropped from the entry: MFI < 16 (no MFI), fast stochastic fastd > fastk (no stochastic), and the alternative trigger 'EMA(5) crosses above EMA(10)' that the original ORs with the EMA(50) > EMA(100) trend filter (it would need two more features; dropping it gives fewer entries). The original's Fisher-RSI < -0.94 condition is RSI < 32.6 and therefore already implied by RSI < 28, so dropping it loses nothing. Exit as in the Strategy002 seed: Fisher-RSI > 0.3 is exactly RSI > 53.1; 'SAR above close' is approximated by close below the previous close. The ROI table (0 min: 5%, 20: 4%, 30: 3%, 60: 1%) becomes a single 5% take-profit. Stop-loss 10% kept. Timeframe changed 5m -> 1h.

| Criterion | Status | Value | Threshold | Note |
| --- | --- | --- | --- | --- |
| wf_oos_sharpe_min | missing |  | 1.0 | no walk_forward evidence: run walk_forward |
| dsr_min | fail | 5.212636008177123e-11 | 0.95 | re-deflated now with N = 177 distinct plans tried in every run of the lab (research runs and the web UI; all symbols and intervals; the report used N = 117) and V[SR] 3.23e-05 = max(V over 117 commensurable trial Sharpes in this run 3.23e-05, V over 117 in every research run 3.23e-05 (same interval, design window, and costs), V over 117 plans pooled across 1 interval(s) 3.23e-05 (every research run, same costs, any design window; each Sharpe annualised with 365-day bars of its interval and rescaled to 1h bars)); latest Sharpe per distinct plan |
| pbo_max | missing |  | 0.2 | PBO needs the sweep basis (CSCV over the design-window Optuna trials of this plan's behaviour family; the ±20% neighbourhood alone measures stability, not selection): 0 sweep(s) of the family with too few usable trials. run_sweep first |
| beat_buy_hold | fail | False | True | net Sharpe -0.97 vs buy-and-hold 1.12 |
| beat_vol_target | fail | False | True | net Sharpe -0.97 vs vol-targeted buy-and-hold 1.24 |
| random_entry_percentile_min | fail | 0.0 | 95.0 | Sharpe percentile vs 200 random-entry paths with the same trade count |
| cost_stress_factor | fail | -0.8982550347065282 | 2.0 | net total return at x2 costs |
| require_gradual_delay_decay | fail | False | True | no edge to decay (Sharpe 0 bars -0.967, 1 bars -0.78, 3 bars -0.777, 5 bars -0.586) |
| max_drawdown_max | fail | 0.7748147135678832 | 0.25 | net max drawdown (fraction) on the design window |
| min_trades | pass | 138.0 | 100 | trades on the design window |
| min_profitable_regimes | fail | 0.0 | 2 | bear -0.345 (1949 active bars), bull -0.51 (2974 active bars), sideways -0.202 (1143 active bars). trailing 90-day simple return (2160 bars): above +10% bull, below -10% bear, else sideways (regime definition from the run's frozen success criteria); bar t's return is assigned to the regime at close t-1 |
| neighbourhood_pct | fail | 0.0 | 0.2 | 0 of 24 one-at-a-time ±20% neighbours profitable (net total return > 0; every tunable has both sides); rule literals (thresholds, lags) are NOT perturbed: 28, 0, 53.1, 1 |
| require_holdout_pass | missing |  | True | no locked holdout evidence yet |
| require_crosscheck_match | missing |  | True | no event-driven cross-check evidence yet: run_crosscheck (a holdout check needs a matched cross-check first) |

### freqtrade-strategies Strategy005

- Source: https://github.com/freqtrade/freqtrade-strategies/blob/main/user_data/strategies/Strategy005.py
- Plan hash: `3dc84564e3809077127cf542bc760b5c8f6d93dffa7cc6cdff6cedc6e007d7bd`
- Backtest trial: 395
- Overall: failed
- Missing evidence: wf_oos_sharpe_min, pbo_max, neighbourhood_pct, require_holdout_pass, require_crosscheck_match
- Translation notes: Uses the strategy's own hyperopt results (buy_params / sell_params) as the parameter values. Entry: volume above 4x its 150-bar average, close below SMA(40), RSI(14) above 26, and Fisher-RSI-normalised below 5; the latter is 50 x (tanh(0.1 x (RSI - 50)) + 1) < 5, which is exactly RSI < 35.28. Dropped from the entry: fast stochastic fastd > fastk and fastd > 1 (no stochastic), and a minimum-price guard (irrelevant for BTC). Exit (the hyperopt-selected 'rsi-macd-minusdi' trigger): RSI(14) crosses above 74 and the MACD(12, 26, 9) line is below 0; the third condition, minus-DI > 4, is dropped (no DMI in the registry; it is true almost always). The ROI table (0 min: 5%, 20: 4%, 40: 3%, 80: 2%, 1440: 1%) becomes a single 5% take-profit. Stop-loss 10% kept. Timeframe changed 5m -> 1h. Four features and six params, both at the plan limit.

| Criterion | Status | Value | Threshold | Note |
| --- | --- | --- | --- | --- |
| wf_oos_sharpe_min | missing |  | 1.0 | no walk_forward evidence: run walk_forward |
| dsr_min | fail | 0.00015980043977272042 | 0.95 | re-deflated now with N = 202 distinct plans tried in every run of the lab (research runs and the web UI; all symbols and intervals; the report used N = 142) and V[SR] 3.36e-05 = max(V over 142 commensurable trial Sharpes in this run 3.36e-05, V over 142 in every research run 3.36e-05 (same interval, design window, and costs), V over 142 plans pooled across 1 interval(s) 3.36e-05 (every research run, same costs, any design window; each Sharpe annualised with 365-day bars of its interval and rescaled to 1h bars)); latest Sharpe per distinct plan |
| pbo_max | missing |  | 0.2 | PBO needs the sweep basis (CSCV over the design-window Optuna trials of this plan's behaviour family; the ±20% neighbourhood alone measures stability, not selection): 0 sweep(s) of the family with too few usable trials. run_sweep first |
| beat_buy_hold | fail | False | True | net Sharpe 0.12 vs buy-and-hold 1.12 |
| beat_vol_target | fail | False | True | net Sharpe 0.12 vs vol-targeted buy-and-hold 1.24 |
| random_entry_percentile_min | fail | 26.5 | 95.0 | Sharpe percentile vs 200 random-entry paths with the same trade count |
| cost_stress_factor | fail | -0.23701621268184625 | 2.0 | net total return at x2 costs |
| require_gradual_delay_decay | fail | False | True | edge vanishes after 1 bar(s) (Sharpe 0 bars 0.116, 1 bars -0.0195, 3 bars 0.127, 5 bars 0.0935) |
| max_drawdown_max | fail | 0.5232001344085193 | 0.25 | net max drawdown (fraction) on the design window |
| min_trades | fail | 83.0 | 100 | trades on the design window |
| min_profitable_regimes | fail | 1.0 | 2 | bear -0.135 (2567 active bars), bull 0.378 (7582 active bars), sideways -0.171 (2383 active bars). trailing 90-day simple return (2160 bars): above +10% bull, below -10% bear, else sideways (regime definition from the run's frozen success criteria); bar t's return is assigned to the regime at close t-1 |
| neighbourhood_pct | missing |  | 0.2 | only 24 of 32 expected ±20% neighbours were run on the design window with the same costs (lab.toml neighbourhood_max_trials = 25 caps the grid at 24; or an older backtest, or trials run with other costs); robustness is not established |
| require_holdout_pass | missing |  | True | no locked holdout evidence yet |
| require_crosscheck_match | missing |  | True | no event-driven cross-check evidence yet: run_crosscheck (a holdout check needs a matched cross-check first) |

---

Results are historical simulations on the design window only; they are not forward-looking. Calibration never calls the holdout check and records no holdout data.
