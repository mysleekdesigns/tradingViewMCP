---
run_id: "r-20261026-144959-adcbbd"
date: 2026-10-26
symbols:
  - "BTCUSDT"
  - "ETHUSDT"
interval: "5m, 15m"
outcome: no_edge
idea_ids:
  - 14
  - 15
  - 16
criteria_sha256: "cc544fe9302e2ffdfeb9af8d5c741753812f362e6e508dbd40f4113d55b72126"
tags:
  - research
  - phase-8
---
# Intraday 5m and 15m port of ideas 14, 15, 16

Run [[run-r-20261026-144959-adcbbd]] · BTCUSDT, ETHUSDT 5m, 15m

Holdout metrics are never written to this journal; only that a check was spent and the resulting status.

## Success criteria and budget (fixed before any backtest)

Criteria file sha256: `cc544fe9302e2ffdfeb9af8d5c741753812f362e6e508dbd40f4113d55b72126`. These values were fixed before the first backtest of this run and are never relaxed mid-run.

| criterion | key | value |
|---|---|---|
| Walk-forward OOS Sharpe after costs (365-day), at least | `wf_oos_sharpe_min` | 1 |
| Deflated Sharpe Ratio (full trial count), at least | `dsr_min` | 0.95 |
| Probability of backtest overfitting, at most | `pbo_max` | 0.2 |
| Beats buy-and-hold (risk-adjusted) | `beat_buy_hold` | required |
| Beats volatility-targeted buy-and-hold (risk-adjusted) | `beat_vol_target` | required |
| Percentile vs the random-entry null, at least | `random_entry_percentile_min` | 95 |
| Net result stays positive at this cost multiple | `cost_stress_factor` | 2 |
| Edge decays gradually under signal delay (no cliff) | `require_gradual_delay_decay` | required |
| Max drawdown (fraction), at most | `max_drawdown_max` | 0.25 |
| Trades, at least | `min_trades` | 100 |
| Profitable regimes of 3 (unless regime-gated), at least | `min_profitable_regimes` | 2 |
| Regime label window (trailing days) | `regime_window_days` | 90 |
| Regime threshold (± simple return over the window) | `regime_threshold` | 0.1 |
| Parameter neighbourhood (±%) that must stay profitable | `neighbourhood_pct` | 0.2 |
| Passes the locked holdout | `require_holdout_pass` | required |
| Matches the event-driven cross-check engine | `require_crosscheck_match` | required |

| budget | key | limit |
|---|---|---|
| Hypotheses | `max_hypotheses` | 5 |
| LLM proposals | `max_proposals` | 15 |
| Backtests (ledger trials) | `max_backtests` | 3000 |
| Holdout checks | `max_holdout_checks` | 1 |
| Model spend (USD) | `max_usd` | 20 |

## Log

- 2026-10-26T14:50:11Z **design** — Owner request (2026-10-26): test every expressible idea on the 5m and 15m timeframes. Pre-registered porting rule, fixed before any backtest: each plan is the idea's original plan with every bar-count parameter, threshold literal and risk setting unchanged; only symbol (BTCUSDT, ETHUSDT) and interval (15m, 5m) change. Calendar-time scaling was rejected because most windows would exceed the feature bound of 1000 bars, and re-picking literals per timeframe would be literal-tuning. Consequence: the same bar counts cover 16x (15m) or 48x (5m) less calendar time than on 4h. Base backtests only at first; a plan gets a sweep or walk-forward only if it clears the screen (net Sharpe above vol-targeted buy-and-hold, random-entry percentile at least 95, at least 100 trades, max DD at most 25%). Ideas 5, 10, 11 and 12 are not expressible in the current lab and are not tested. Costs unchanged: 0.10% fee + 0.05% slippage per side. · [[idea-14]] [[idea-15]] [[idea-16]]
- 2026-10-26T14:50:11Z **design** — Plans in this run: idea 14 bull_regime_zscore_dip_btc_15m `68240bcce70e`; idea 14 bull_regime_zscore_dip_eth_15m `b339a241cc9e`; idea 14 bull_regime_zscore_dip_btc_5m `20bd422d010e`; idea 14 bull_regime_zscore_dip_eth_5m `510ce4e8286b`; idea 15 volume_climax_reversal_btc_15m `cde878b18d20`; idea 15 volume_climax_reversal_eth_15m `09d5679f668a`; idea 15 volume_climax_reversal_btc_5m `cbee7eae6d32`; idea 15 volume_climax_reversal_eth_5m `4b6d76724414`; idea 16 vol_unit_shock_reversal_btc_15m `dec6a120d4d4`; idea 16 vol_unit_shock_reversal_eth_15m `9d2ad84a2afc`; idea 16 vol_unit_shock_reversal_btc_5m `dde89ab386f8`; idea 16 vol_unit_shock_reversal_eth_5m `bb97aedd606b` · [[idea-14]] [[idea-15]] [[idea-16]]
- 2026-10-26T16:00:54Z **backtest** — Idea 14 bull_regime_zscore_dip_btc_15m: net Sharpe -0.76 (gross 0.91), max DD 57%, 405 trades, vol-target 1.22, random-entry percentile 99.5. Screen: fail. · [[idea-14]] [[trial-3192]] [[run-r-20261026-144959-adcbbd]] plan `68240bcce70e2544deac5dfead3ff7567810c01f8551ce754ed7d747c44f245a`
- 2026-10-26T16:00:54Z **backtest** — Idea 14 bull_regime_zscore_dip_eth_15m: net Sharpe -1.35 (gross 0.44), max DD 86%, 608 trades, vol-target 1.20, random-entry percentile 84.5. Screen: fail. · [[idea-14]] [[trial-3321]] [[run-r-20261026-144959-adcbbd]] plan `b339a241cc9e1ccc9e5a8f6f9317a17d153ecaeb86e47627b2016eb8279fe5ce`
- 2026-10-26T16:00:54Z **backtest** — Idea 14 bull_regime_zscore_dip_btc_5m: net Sharpe -0.88 (gross 0.56), max DD 48%, 260 trades, vol-target 1.22, random-entry percentile 100. Screen: fail. · [[idea-14]] [[trial-3450]] [[run-r-20261026-144959-adcbbd]] plan `20bd422d010e0fc0995c758ad63c26efbdf3c51c9094c6ca7b44801136aa1c32`
- 2026-10-26T16:00:54Z **backtest** — Idea 14 bull_regime_zscore_dip_eth_5m: net Sharpe -1.29 (gross 0.77), max DD 74%, 519 trades, vol-target 1.14, random-entry percentile 100. Screen: fail. · [[idea-14]] [[trial-3583]] [[run-r-20261026-144959-adcbbd]] plan `510ce4e8286b1cad8cb6949e7b9a2fd21297e0941fab66120e04aac79e4cc6a4`
- 2026-10-26T16:00:54Z **backtest** — Idea 15 volume_climax_reversal_btc_15m: net Sharpe -3.85 (gross 0.42), max DD 100%, 2205 trades, vol-target 1.22, random-entry percentile 82.5. Screen: fail. · [[idea-15]] [[trial-3716]] [[run-r-20261026-144959-adcbbd]] plan `cde878b18d2028bcf76166c6acdafb064592a849f7704ed05c96923d2974fce4`
- 2026-10-26T16:00:54Z **backtest** — Idea 15 volume_climax_reversal_eth_15m: net Sharpe -3.75 (gross 0.08), max DD 100%, 2494 trades, vol-target 1.20, random-entry percentile 30.5. Screen: fail. · [[idea-15]] [[trial-3816]] [[run-r-20261026-144959-adcbbd]] plan `09d5679f668a744e0a4afad81e1422097da9ab1ae8e91ca5e5e039443c01beb4`
- 2026-10-26T16:00:54Z **backtest** — Idea 15 volume_climax_reversal_btc_5m: net Sharpe -8.25 (gross 1.03), max DD 100%, 5291 trades, vol-target 1.22, random-entry percentile 100. Screen: fail. · [[idea-15]] [[trial-3916]] [[run-r-20261026-144959-adcbbd]] plan `cbee7eae6d3279c635f9ef65170f34929b24020a3d3d8428fc011b4210a68b02`
- 2026-10-26T16:00:54Z **backtest** — Idea 15 volume_climax_reversal_eth_5m: net Sharpe -7.86 (gross 0.49), max DD 100%, 6042 trades, vol-target 1.14, random-entry percentile 100. Screen: fail. · [[idea-15]] [[trial-4020]] [[run-r-20261026-144959-adcbbd]] plan `4b6d7672441429cdfc96e744a92754112cb048ebb123d7a0a04d1a5b77a03730`
- 2026-10-26T16:00:54Z **backtest** — Idea 16 vol_unit_shock_reversal_btc_15m: net Sharpe -1.42 (gross 0.37), max DD 73%, 271 trades, vol-target 1.22, random-entry percentile 95.5. Screen: fail. · [[idea-16]] [[trial-4116]] [[run-r-20261026-144959-adcbbd]] plan `dec6a120d4d4c84f1e84a5b509962b5a720d6e52c409fcb4167f76017ac70754`
- 2026-10-26T16:00:54Z **backtest** — Idea 16 vol_unit_shock_reversal_eth_15m: net Sharpe -1.09 (gross 0.49), max DD 71%, 255 trades, vol-target 1.20, random-entry percentile 92.5. Screen: fail. · [[idea-16]] [[trial-4210]] [[run-r-20261026-144959-adcbbd]] plan `9d2ad84a2afc849e415a1f04aa74e07dae2234e8964f38a0d74d36d77af35ec6`
- 2026-10-26T16:00:54Z **backtest** — Idea 16 vol_unit_shock_reversal_btc_5m: net Sharpe -2.43 (gross 0.39), max DD 95%, 709 trades, vol-target 1.22, random-entry percentile 100. Screen: fail. · [[idea-16]] [[trial-4304]] [[run-r-20261026-144959-adcbbd]] plan `dde89ab386f8539020409b84bf0820f5849d01afd53300126c4cb3b63a57b104`
- 2026-10-26T16:00:54Z **backtest** — Idea 16 vol_unit_shock_reversal_eth_5m: net Sharpe -1.95 (gross 0.57), max DD 95%, 698 trades, vol-target 1.14, random-entry percentile 100. Screen: fail. · [[idea-16]] [[trial-4369]] [[run-r-20261026-144959-adcbbd]] plan `bb97aedd606b4dae42ab3f84f18d05c1ba31dddb3ec604e1106a1c8e3a68b033`
- 2026-10-26T16:00:54Z **decide** — No plan in this run clears the pre-registered screen, so none gets a sweep, walk-forward, adversarial check or cross-check, and no holdout check is spent. Every plan is below vol-targeted buy-and-hold after costs. 1 plan(s) had gross Sharpe above 1.0 (volume_climax_reversal_btc_5m) but trade hundreds to thousands of times, and about 0.3% round-trip costs turn them negative. High random-entry percentiles on some plans only mean equally frequent random entries lose even more to costs. Critic not launched: there is no surviving result to be sceptical about. · [[idea-14]] [[idea-15]] [[idea-16]]
- 2026-10-26T16:00:54Z **lesson** — Porting 4h/1h rules to 15m and 5m with the same bar counts multiplies trade counts 10-50x; at retail spot costs (0.10% fee + 0.05% slippage per side) no rule in the library survives. Intraday research on this lab needs either a cost model the owner can actually get (e.g. maker or discounted fees, set in lab.toml by the owner) or rules with far fewer, larger trades. · [[idea-14]] [[idea-15]] [[idea-16]]

## End-of-run report

Run [[run-r-20261026-144959-adcbbd]] · BTCUSDT, ETHUSDT 5m, 15m · started 2026-10-26T14:49:59Z · finished 2026-10-26T16:05:00Z

### Outcome

**No edge found.** No hypothesis tested in this run passed every success criterion. This is a correct, complete outcome: the loop has a fixed bar, a fixed budget, and an honest exit, and a run that ends with "no edge found" has worked correctly. The criteria and budget were not relaxed.

### Success criteria and budget

Criteria file sha256: `cc544fe9302e2ffdfeb9af8d5c741753812f362e6e508dbd40f4113d55b72126`. These values were fixed before the first backtest of this run and are never relaxed mid-run.

| criterion | key | value |
|---|---|---|
| Walk-forward OOS Sharpe after costs (365-day), at least | `wf_oos_sharpe_min` | 1 |
| Deflated Sharpe Ratio (full trial count), at least | `dsr_min` | 0.95 |
| Probability of backtest overfitting, at most | `pbo_max` | 0.2 |
| Beats buy-and-hold (risk-adjusted) | `beat_buy_hold` | required |
| Beats volatility-targeted buy-and-hold (risk-adjusted) | `beat_vol_target` | required |
| Percentile vs the random-entry null, at least | `random_entry_percentile_min` | 95 |
| Net result stays positive at this cost multiple | `cost_stress_factor` | 2 |
| Edge decays gradually under signal delay (no cliff) | `require_gradual_delay_decay` | required |
| Max drawdown (fraction), at most | `max_drawdown_max` | 0.25 |
| Trades, at least | `min_trades` | 100 |
| Profitable regimes of 3 (unless regime-gated), at least | `min_profitable_regimes` | 2 |
| Regime label window (trailing days) | `regime_window_days` | 90 |
| Regime threshold (± simple return over the window) | `regime_threshold` | 0.1 |
| Parameter neighbourhood (±%) that must stay profitable | `neighbourhood_pct` | 0.2 |
| Passes the locked holdout | `require_holdout_pass` | required |
| Matches the event-driven cross-check engine | `require_crosscheck_match` | required |

| budget | key | limit |
|---|---|---|
| Hypotheses | `max_hypotheses` | 5 |
| LLM proposals | `max_proposals` | 15 |
| Backtests (ledger trials) | `max_backtests` | 3000 |
| Holdout checks | `max_holdout_checks` | 1 |
| Model spend (USD) | `max_usd` | 20 |

### Budget used vs limits

| resource | used | limit | |
|---|---|---|---|
| Hypotheses | 3 | 5 |  |
| LLM proposals | 12 | 15 |  |
| Backtests (ledger trials) | 308 | 3000 |  |
| Holdout checks | 0 | 1 |  |
| Model spend (USD) | 0.00 | 20.00 |  |

### Ledger size used for DSR

N = 3147: distinct plans tried across every research run in the ledger (this run is [[run-r-20261026-144959-adcbbd]]), Optuna and neighbourhood variants included. Every Deflated Sharpe Ratio is deflated by this count, so starting a new run never resets it.

### Ideas tried

| idea | title (scout-sourced, untrusted) | status | reason |
|---|---|---|---|
| [[idea-14]] | Bull-regime z-score dip buy with reversion exit | abandoned | 5m/15m port fails after costs: BTC 15m net -0.76; ETH 15m net -1.35; BTC 5m net -0.88; ETH 5m net -1.29 |
| [[idea-15]] | Volume-climax capitulation reversal | abandoned | 5m/15m port fails after costs: BTC 15m net -3.85; ETH 15m net -3.75; BTC 5m net -8.25; ETH 5m net -7.86 |
| [[idea-16]] | Vol-unit shock reversal on 1h BTC | abandoned | 5m/15m port fails after costs: BTC 15m net -1.42; ETH 15m net -1.09; BTC 5m net -2.43; ETH 5m net -1.95 |

### Failures and lessons

- [[idea-14]] Bull-regime z-score dip buy with reversion exit (abandoned): 5m/15m port fails after costs: BTC 15m net -0.76; ETH 15m net -1.35; BTC 5m net -0.88; ETH 5m net -1.29
- [[idea-15]] Volume-climax capitulation reversal (abandoned): 5m/15m port fails after costs: BTC 15m net -3.85; ETH 15m net -3.75; BTC 5m net -8.25; ETH 5m net -7.86
- [[idea-16]] Vol-unit shock reversal on 1h BTC (abandoned): 5m/15m port fails after costs: BTC 15m net -1.42; ETH 15m net -1.09; BTC 5m net -2.43; ETH 5m net -1.95
- Pre-registered port: bar-count parameters, literals and risk unchanged from each idea's original plan; only symbol and interval changed.
- Base backtests executed by background agents from fixed plan files; every reported number was re-checked against the ledger (0 mismatches).
- Ideas 5, 10, 11, 12 are not expressible in the current lab and were not tested.

### Candidates

None. No plan enters paper trading from this run.

### Disclaimer

Backtests are historical, not forward-looking. A candidate is not "successful" until it has passed every success criterion and 4-8 weeks of forward paper trading consistent with its backtest.
