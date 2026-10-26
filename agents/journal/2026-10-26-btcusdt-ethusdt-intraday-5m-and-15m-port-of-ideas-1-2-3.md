---
run_id: "r-20261026-144959-3dda69"
date: 2026-10-26
symbols:
  - "BTCUSDT"
  - "ETHUSDT"
interval: "5m, 15m"
outcome: no_edge
idea_ids:
  - 1
  - 2
  - 3
criteria_sha256: "cc544fe9302e2ffdfeb9af8d5c741753812f362e6e508dbd40f4113d55b72126"
tags:
  - research
  - phase-8
---
# Intraday 5m and 15m port of ideas 1, 2, 3

Run [[run-r-20261026-144959-3dda69]] · BTCUSDT, ETHUSDT 5m, 15m

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

- 2026-10-26T14:50:11Z **design** — Owner request (2026-10-26): test every expressible idea on the 5m and 15m timeframes. Pre-registered porting rule, fixed before any backtest: each plan is the idea's original plan with every bar-count parameter, threshold literal and risk setting unchanged; only symbol (BTCUSDT, ETHUSDT) and interval (15m, 5m) change. Calendar-time scaling was rejected because most windows would exceed the feature bound of 1000 bars, and re-picking literals per timeframe would be literal-tuning. Consequence: the same bar counts cover 16x (15m) or 48x (5m) less calendar time than on 4h. Base backtests only at first; a plan gets a sweep or walk-forward only if it clears the screen (net Sharpe above vol-targeted buy-and-hold, random-entry percentile at least 95, at least 100 trades, max DD at most 25%). Ideas 5, 10, 11 and 12 are not expressible in the current lab and are not tested. Costs unchanged: 0.10% fee + 0.05% slippage per side. · [[idea-1]] [[idea-2]] [[idea-3]]
- 2026-10-26T14:50:11Z **design** — Plans in this run: idea 1 vt-lowvol-gate_btc_15m `2dead16a04c1`; idea 1 vt-lowvol-gate_eth_15m `cd4644b11aad`; idea 1 vt-lowvol-gate_btc_5m `bd8fd2ab3a7d`; idea 1 vt-lowvol-gate_eth_5m `395a4b988b5c`; idea 2 vt-volgated-entry-trend-exit_btc_15m `175183dc8038`; idea 2 vt-volgated-entry-trend-exit_eth_15m `91d5068fff7d`; idea 2 vt-volgated-entry-trend-exit_btc_5m `1c8103ba8bce`; idea 2 vt-volgated-entry-trend-exit_eth_5m `a7698bdaa861`; idea 3 vt-squeeze-breakout_btc_15m `e5c3a5b6ac7d`; idea 3 vt-squeeze-breakout_eth_15m `2191fb4451f9`; idea 3 vt-squeeze-breakout_btc_5m `564b47b52ede`; idea 3 vt-squeeze-breakout_eth_5m `d5c500e13784` · [[idea-1]] [[idea-2]] [[idea-3]]
- 2026-10-26T16:00:54Z **backtest** — Idea 1 vt-lowvol-gate_btc_15m: net Sharpe -2.77 (gross 0.70), max DD 100%, 2741 trades, vol-target 1.22, random-entry percentile 0. Screen: fail. · [[idea-1]] [[trial-3146]] [[run-r-20261026-144959-3dda69]] plan `2dead16a04c1dedf43bb9065f8d6db70b8d4a24fe3a6c9aefd5258078faec2df`
- 2026-10-26T16:00:54Z **backtest** — Idea 1 vt-lowvol-gate_eth_15m: net Sharpe -2.44 (gross 0.51), max DD 100%, 2860 trades, vol-target 1.20, random-entry percentile 0. Screen: fail. · [[idea-1]] [[trial-3275]] [[run-r-20261026-144959-3dda69]] plan `cd4644b11aad6a94ecad7546a01c1763246676f6d2ea88f05ed5485fa4882722`
- 2026-10-26T16:00:54Z **backtest** — Idea 1 vt-lowvol-gate_btc_5m: net Sharpe -6.13 (gross 0.59), max DD 100%, 5182 trades, vol-target 1.22, random-entry percentile 0. Screen: fail. · [[idea-1]] [[trial-3404]] [[run-r-20261026-144959-3dda69]] plan `bd8fd2ab3a7db1dd0989f9a0c6f49ea6c68d1fa0eddda0db59de36fb75e3bbe5`
- 2026-10-26T16:00:54Z **backtest** — Idea 1 vt-lowvol-gate_eth_5m: net Sharpe -5.61 (gross 0.34), max DD 100%, 5677 trades, vol-target 1.14, random-entry percentile 0. Screen: fail. · [[idea-1]] [[trial-3125]] [[run-r-20261026-144959-3dda69]] plan `395a4b988b5c8940779ab14df876815edfabe1fb809df9c23c55a3946e8d3842`
- 2026-10-26T16:00:54Z **backtest** — Idea 2 vt-volgated-entry-trend-exit_btc_15m: net Sharpe -1.81 (gross 1.72), max DD 100%, 2895 trades, vol-target 1.22, random-entry percentile 92. Screen: fail. · [[idea-2]] [[trial-3533]] [[run-r-20261026-144959-3dda69]] plan `175183dc80385c10136deb09f1156d88046d0026fb4e41e425de0dc661949a1e`
- 2026-10-26T16:00:54Z **backtest** — Idea 2 vt-volgated-entry-trend-exit_eth_15m: net Sharpe -1.19 (gross 1.79), max DD 100%, 2981 trades, vol-target 1.20, random-entry percentile 99. Screen: fail. · [[idea-2]] [[trial-3666]] [[run-r-20261026-144959-3dda69]] plan `91d5068fff7d93f516a1df0b54beb2ef083eab544b1f83b9cb2486ffc4c18bee`
- 2026-10-26T16:00:54Z **backtest** — Idea 2 vt-volgated-entry-trend-exit_btc_5m: net Sharpe -7.23 (gross 0.67), max DD 100%, 5682 trades, vol-target 1.22, random-entry percentile 1. Screen: fail. · [[idea-2]] [[trial-3766]] [[run-r-20261026-144959-3dda69]] plan `1c8103ba8bcebe1543dbcc574e4bfc070757a49d951085467b95e5d8e9c5c335`
- 2026-10-26T16:00:54Z **backtest** — Idea 2 vt-volgated-entry-trend-exit_eth_5m: net Sharpe -6.24 (gross 0.92), max DD 100%, 6776 trades, vol-target 1.14, random-entry percentile 6. Screen: fail. · [[idea-2]] [[trial-3866]] [[run-r-20261026-144959-3dda69]] plan `a7698bdaa861cfd1e9b708728c133027a65a052df28a4cf55fc9305a4596464d`
- 2026-10-26T16:00:54Z **backtest** — Idea 3 vt-squeeze-breakout_btc_15m: net Sharpe -0.88 (gross 0.44), max DD 92%, 619 trades, vol-target 1.22, random-entry percentile 16.5. Screen: fail. · [[idea-3]] [[trial-3966]] [[run-r-20261026-144959-3dda69]] plan `e5c3a5b6ac7d82a0a3fd7076e28cef742b16f02cda2dc77e94e7f034d33f5107`
- 2026-10-26T16:00:54Z **backtest** — Idea 3 vt-squeeze-breakout_eth_15m: net Sharpe -0.49 (gross 0.64), max DD 89%, 588 trades, vol-target 1.20, random-entry percentile 32.5. Screen: fail. · [[idea-3]] [[trial-4070]] [[run-r-20261026-144959-3dda69]] plan `2191fb4451f96abf0e26c2f1def21e7b8b0975e829e80344a7455baeb445e6cc`
- 2026-10-26T16:00:54Z **backtest** — Idea 3 vt-squeeze-breakout_btc_5m: net Sharpe -3.73 (gross -0.16), max DD 100%, 1958 trades, vol-target 1.22, random-entry percentile 0. Screen: fail. · [[idea-3]] [[trial-4164]] [[run-r-20261026-144959-3dda69]] plan `564b47b52edefa238f08d2a0734a31ebd13a1979e39e9064fe1c5a69fdb89cb4`
- 2026-10-26T16:00:54Z **backtest** — Idea 3 vt-squeeze-breakout_eth_5m: net Sharpe -2.84 (gross 0.17), max DD 100%, 1938 trades, vol-target 1.14, random-entry percentile 0.5. Screen: fail. · [[idea-3]] [[trial-4258]] [[run-r-20261026-144959-3dda69]] plan `d5c500e13784efa2b68a0d77e7fe820278e3fabca9a16ba270b40e1d013a2681`
- 2026-10-26T16:00:54Z **decide** — No plan in this run clears the pre-registered screen, so none gets a sweep, walk-forward, adversarial check or cross-check, and no holdout check is spent. Every plan is below vol-targeted buy-and-hold after costs. 2 plan(s) had gross Sharpe above 1.0 (vt-volgated-entry-trend-exit_btc_15m, vt-volgated-entry-trend-exit_eth_15m) but trade hundreds to thousands of times, and about 0.3% round-trip costs turn them negative. High random-entry percentiles on some plans only mean equally frequent random entries lose even more to costs. Critic not launched: there is no surviving result to be sceptical about. · [[idea-1]] [[idea-2]] [[idea-3]]
- 2026-10-26T16:00:54Z **lesson** — Porting 4h/1h rules to 15m and 5m with the same bar counts multiplies trade counts 10-50x; at retail spot costs (0.10% fee + 0.05% slippage per side) no rule in the library survives. Intraday research on this lab needs either a cost model the owner can actually get (e.g. maker or discounted fees, set in lab.toml by the owner) or rules with far fewer, larger trades. · [[idea-1]] [[idea-2]] [[idea-3]]

## End-of-run report

Run [[run-r-20261026-144959-3dda69]] · BTCUSDT, ETHUSDT 5m, 15m · started 2026-10-26T14:49:59Z · finished 2026-10-26T16:05:00Z

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
| Backtests (ledger trials) | 300 | 3000 |  |
| Holdout checks | 0 | 1 |  |
| Model spend (USD) | 0.00 | 20.00 |  |

### Ledger size used for DSR

N = 3147: distinct plans tried across every research run in the ledger (this run is [[run-r-20261026-144959-3dda69]]), Optuna and neighbourhood variants included. Every Deflated Sharpe Ratio is deflated by this count, so starting a new run never resets it.

### Ideas tried

| idea | title (scout-sourced, untrusted) | status | reason |
|---|---|---|---|
| [[idea-1]] | Pure low-vol gate (control) | abandoned | 5m/15m port fails after costs: BTC 15m net -2.77; ETH 15m net -2.44; BTC 5m net -6.13; ETH 5m net -5.61 |
| [[idea-2]] | Trend filter with vol-spike gate | abandoned | 5m/15m port fails after costs: BTC 15m net -1.81; ETH 15m net -1.19; BTC 5m net -7.23; ETH 5m net -6.24 |
| [[idea-3]] | Vol compression then Donchian breakout | abandoned | 5m/15m port fails after costs: BTC 15m net -0.88; ETH 15m net -0.49; BTC 5m net -3.73; ETH 5m net -2.84 |

### Failures and lessons

- [[idea-1]] Pure low-vol gate (control) (abandoned): 5m/15m port fails after costs: BTC 15m net -2.77; ETH 15m net -2.44; BTC 5m net -6.13; ETH 5m net -5.61
- [[idea-2]] Trend filter with vol-spike gate (abandoned): 5m/15m port fails after costs: BTC 15m net -1.81; ETH 15m net -1.19; BTC 5m net -7.23; ETH 5m net -6.24
- [[idea-3]] Vol compression then Donchian breakout (abandoned): 5m/15m port fails after costs: BTC 15m net -0.88; ETH 15m net -0.49; BTC 5m net -3.73; ETH 5m net -2.84
- Pre-registered port: bar-count parameters, literals and risk unchanged from each idea's original plan; only symbol and interval changed.
- Base backtests executed by background agents from fixed plan files; every reported number was re-checked against the ledger (0 mismatches).
- Ideas 5, 10, 11, 12 are not expressible in the current lab and were not tested.

### Candidates

None. No plan enters paper trading from this run.

### Disclaimer

Backtests are historical, not forward-looking. A candidate is not "successful" until it has passed every success criterion and 4-8 weeks of forward paper trading consistent with its backtest.
