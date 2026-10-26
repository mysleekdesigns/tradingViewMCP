---
run_id: "r-20261026-144959-ea108c"
date: 2026-10-26
symbols:
  - "BTCUSDT"
  - "ETHUSDT"
interval: "5m, 15m"
outcome: no_edge
idea_ids:
  - 8
  - 9
  - 13
criteria_sha256: "cc544fe9302e2ffdfeb9af8d5c741753812f362e6e508dbd40f4113d55b72126"
tags:
  - research
  - phase-8
---
# Intraday 5m and 15m port of ideas 8, 9, 13

Run [[run-r-20261026-144959-ea108c]] · BTCUSDT, ETHUSDT 5m, 15m

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

- 2026-10-26T14:50:11Z **design** — Owner request (2026-10-26): test every expressible idea on the 5m and 15m timeframes. Pre-registered porting rule, fixed before any backtest: each plan is the idea's original plan with every bar-count parameter, threshold literal and risk setting unchanged; only symbol (BTCUSDT, ETHUSDT) and interval (15m, 5m) change. Calendar-time scaling was rejected because most windows would exceed the feature bound of 1000 bars, and re-picking literals per timeframe would be literal-tuning. Consequence: the same bar counts cover 16x (15m) or 48x (5m) less calendar time than on 4h. Base backtests only at first; a plan gets a sweep or walk-forward only if it clears the screen (net Sharpe above vol-targeted buy-and-hold, random-entry percentile at least 95, at least 100 trades, max DD at most 25%). Ideas 5, 10, 11 and 12 are not expressible in the current lab and are not tested. Costs unchanged: 0.10% fee + 0.05% slippage per side. · [[idea-8]] [[idea-9]] [[idea-13]]
- 2026-10-26T14:50:11Z **design** — Plans in this run: idea 8 rolling-high-breakout-atr-exit_btc_15m `614d57f2cf5b`; idea 8 rolling-high-breakout-atr-exit_eth_15m `f7450a17e8a1`; idea 8 rolling-high-breakout-atr-exit_btc_5m `17beffecffbf`; idea 8 rolling-high-breakout-atr-exit_eth_5m `6d9838f24ab5`; idea 9 dual_ema_trend_strength_gate_btc_15m `e072e8995f83`; idea 9 dual_ema_trend_strength_gate_eth_15m `d0b27899a414`; idea 9 dual_ema_trend_strength_gate_btc_5m `753430c6b0b0`; idea 9 dual_ema_trend_strength_gate_eth_5m `6569f7d55084`; idea 13 markov_regime_filter_btc_15m `8b2a377210a3`; idea 13 markov_regime_filter_eth_15m `7b80ae6e0030`; idea 13 markov_regime_filter_btc_5m `69bb63f09ec2`; idea 13 markov_regime_filter_eth_5m `fc1f53589482` · [[idea-8]] [[idea-9]] [[idea-13]]
- 2026-10-26T16:00:54Z **backtest** — Idea 8 rolling-high-breakout-atr-exit_btc_15m: net Sharpe -2.22 (gross 0.66), max DD 99%, 1217 trades, vol-target 1.22, random-entry percentile 37. Screen: fail. · [[idea-8]] [[trial-3254]] [[run-r-20261026-144959-ea108c]] plan `614d57f2cf5b5fc120d4932352e1fb36cc666730af7b878686f9af09527be619`
- 2026-10-26T16:00:54Z **backtest** — Idea 8 rolling-high-breakout-atr-exit_eth_15m: net Sharpe -1.26 (gross 1.27), max DD 96%, 1203 trades, vol-target 1.20, random-entry percentile 79.5. Screen: fail. · [[idea-8]] [[trial-3383]] [[run-r-20261026-144959-ea108c]] plan `f7450a17e8a138783482fca070cd758f0dec4fbe98570cefb9c93515a67e9bb8`
- 2026-10-26T16:00:54Z **backtest** — Idea 8 rolling-high-breakout-atr-exit_btc_5m: net Sharpe -6.77 (gross 0.66), max DD 100%, 3752 trades, vol-target 1.22, random-entry percentile 88. Screen: fail. · [[idea-8]] [[trial-3512]] [[run-r-20261026-144959-ea108c]] plan `17beffecffbf8772c47af450b7e771032a3a16fe90f0764df889e3ebf6978465`
- 2026-10-26T16:00:54Z **backtest** — Idea 8 rolling-high-breakout-atr-exit_eth_5m: net Sharpe -6.17 (gross 0.41), max DD 100%, 3847 trades, vol-target 1.14, random-entry percentile 25.5. Screen: fail. · [[idea-8]] [[trial-3645]] [[run-r-20261026-144959-ea108c]] plan `6d9838f24ab55eaa8bd3e5078a8ca27fd0900c97af78fb0ccd5e9db5a0a6a22b`
- 2026-10-26T16:00:54Z **backtest** — Idea 9 dual_ema_trend_strength_gate_btc_15m: net Sharpe 0.07 (gross 1.26), max DD 78%, 965 trades, vol-target 1.22, random-entry percentile 93. Screen: fail. · [[idea-9]] [[trial-3741]] [[run-r-20261026-144959-ea108c]] plan `e072e8995f83680729eb15e159d115dafe3d8653b02f8e254d68a94497bdb1d1`
- 2026-10-26T16:00:54Z **backtest** — Idea 9 dual_ema_trend_strength_gate_eth_15m: net Sharpe 0.21 (gross 1.20), max DD 92%, 985 trades, vol-target 1.20, random-entry percentile 96. Screen: fail. · [[idea-9]] [[trial-3841]] [[run-r-20261026-144959-ea108c]] plan `d0b27899a4145184a94c529670b6ed52d08a48ae6c8fdbed0a95b9b3ef85d02f`
- 2026-10-26T16:00:54Z **backtest** — Idea 9 dual_ema_trend_strength_gate_btc_5m: net Sharpe -2.57 (gross 0.94), max DD 100%, 3075 trades, vol-target 1.22, random-entry percentile 38. Screen: fail. · [[idea-9]] [[trial-3941]] [[run-r-20261026-144959-ea108c]] plan `753430c6b0b0f269a89e2994ff0fabb89305e06612d62299e1ec70e94e011802`
- 2026-10-26T16:00:54Z **backtest** — Idea 9 dual_ema_trend_strength_gate_eth_5m: net Sharpe -1.73 (gross 1.14), max DD 100%, 3033 trades, vol-target 1.14, random-entry percentile 69.5. Screen: fail. · [[idea-9]] [[trial-4045]] [[run-r-20261026-144959-ea108c]] plan `6569f7d5508401a905b181dfd44c984c778a3edf0ba7bf0635ec143d8f86b67d`
- 2026-10-26T16:00:54Z **backtest** — Idea 13 markov_regime_filter_btc_15m: net Sharpe -0.45 (gross 0.52), max DD 98%, 847 trades, vol-target 1.22, random-entry percentile 0. Screen: fail. · [[idea-13]] [[trial-4139]] [[run-r-20261026-144959-ea108c]] plan `8b2a377210a3f0ddbdf96945b84e59b9bc53361bb36fc9f39c16e7ab3bd3e47a`
- 2026-10-26T16:00:54Z **backtest** — Idea 13 markov_regime_filter_eth_15m: net Sharpe -0.38 (gross 0.80), max DD 98%, 1271 trades, vol-target 1.20, random-entry percentile 6.5. Screen: fail. · [[idea-13]] [[trial-4233]] [[run-r-20261026-144959-ea108c]] plan `7b80ae6e00306c1634b929c935c0219681c5e9c713fbb6fb7ff907ac50dad44b`
- 2026-10-26T16:00:54Z **backtest** — Idea 13 markov_regime_filter_btc_5m: net Sharpe -0.17 (gross 0.62), max DD 91%, 651 trades, vol-target 1.22, random-entry percentile 5. Screen: fail. · [[idea-13]] [[trial-4327]] [[run-r-20261026-144959-ea108c]] plan `69bb63f09ec2c3431b87da767475bc41ef5d1f7dcaeb60881f1a7a12bb3c298d`
- 2026-10-26T16:00:54Z **backtest** — Idea 13 markov_regime_filter_eth_5m: net Sharpe -0.38 (gross 0.82), max DD 98%, 1291 trades, vol-target 1.14, random-entry percentile 20. Screen: fail. · [[idea-13]] [[trial-4392]] [[run-r-20261026-144959-ea108c]] plan `fc1f53589482980a8bd768b26f98979764e5558b42bfd45e326466759b6b426e`
- 2026-10-26T16:00:54Z **decide** — No plan in this run clears the pre-registered screen, so none gets a sweep, walk-forward, adversarial check or cross-check, and no holdout check is spent. Every plan is below vol-targeted buy-and-hold after costs. 4 plan(s) had gross Sharpe above 1.0 (rolling-high-breakout-atr-exit_eth_15m, dual_ema_trend_strength_gate_btc_15m, dual_ema_trend_strength_gate_eth_15m, dual_ema_trend_strength_gate_eth_5m) but trade hundreds to thousands of times, and about 0.3% round-trip costs turn them negative. High random-entry percentiles on some plans only mean equally frequent random entries lose even more to costs. Critic not launched: there is no surviving result to be sceptical about. · [[idea-8]] [[idea-9]] [[idea-13]]
- 2026-10-26T16:00:54Z **lesson** — Porting 4h/1h rules to 15m and 5m with the same bar counts multiplies trade counts 10-50x; at retail spot costs (0.10% fee + 0.05% slippage per side) no rule in the library survives. Intraday research on this lab needs either a cost model the owner can actually get (e.g. maker or discounted fees, set in lab.toml by the owner) or rules with far fewer, larger trades. · [[idea-8]] [[idea-9]] [[idea-13]]

## End-of-run report

Run [[run-r-20261026-144959-ea108c]] · BTCUSDT, ETHUSDT 5m, 15m · started 2026-10-26T14:49:59Z · finished 2026-10-26T16:05:00Z

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
| Backtests (ledger trials) | 284 | 3000 |  |
| Holdout checks | 0 | 1 |  |
| Model spend (USD) | 0.00 | 20.00 |  |

### Ledger size used for DSR

N = 3147: distinct plans tried across every research run in the ledger (this run is [[run-r-20261026-144959-ea108c]]), Optuna and neighbourhood variants included. Every Deflated Sharpe Ratio is deflated by this count, so starting a new run never resets it.

### Ideas tried

| idea | title (scout-sourced, untrusted) | status | reason |
|---|---|---|---|
| [[idea-8]] | Rolling-high breakout, long/flat (MAX rule) | abandoned | 5m/15m port fails after costs: BTC 15m net -2.22; ETH 15m net -1.26; BTC 5m net -6.77; ETH 5m net -6.17 |
| [[idea-9]] | Dual-MA trend with a trend-strength regime gate | abandoned | 5m/15m port fails after costs: BTC 15m net 0.07; ETH 15m net 0.21; BTC 5m net -2.57; ETH 5m net -1.73 |
| [[idea-13]] | Markov regime-signal long/flat filter | abandoned | 5m/15m port fails after costs: BTC 15m net -0.45; ETH 15m net -0.38; BTC 5m net -0.17; ETH 5m net -0.38 |

### Failures and lessons

- [[idea-8]] Rolling-high breakout, long/flat (MAX rule) (abandoned): 5m/15m port fails after costs: BTC 15m net -2.22; ETH 15m net -1.26; BTC 5m net -6.77; ETH 5m net -6.17
- [[idea-9]] Dual-MA trend with a trend-strength regime gate (abandoned): 5m/15m port fails after costs: BTC 15m net 0.07; ETH 15m net 0.21; BTC 5m net -2.57; ETH 5m net -1.73
- [[idea-13]] Markov regime-signal long/flat filter (abandoned): 5m/15m port fails after costs: BTC 15m net -0.45; ETH 15m net -0.38; BTC 5m net -0.17; ETH 5m net -0.38
- Pre-registered port: bar-count parameters, literals and risk unchanged from each idea's original plan; only symbol and interval changed.
- Base backtests executed by background agents from fixed plan files; every reported number was re-checked against the ledger (0 mismatches).
- Ideas 5, 10, 11, 12 are not expressible in the current lab and were not tested.

### Candidates

None. No plan enters paper trading from this run.

### Disclaimer

Backtests are historical, not forward-looking. A candidate is not "successful" until it has passed every success criterion and 4-8 weeks of forward paper trading consistent with its backtest.
