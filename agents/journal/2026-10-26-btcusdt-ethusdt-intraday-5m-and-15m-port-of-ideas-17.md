---
run_id: "r-20261026-144959-b1524c"
date: 2026-10-26
symbols:
  - "BTCUSDT"
  - "ETHUSDT"
interval: "5m, 15m"
outcome: no_edge
idea_ids:
  - 17
criteria_sha256: "cc544fe9302e2ffdfeb9af8d5c741753812f362e6e508dbd40f4113d55b72126"
tags:
  - research
  - phase-8
---
# Intraday 5m and 15m port of ideas 17

Run [[run-r-20261026-144959-b1524c]] · BTCUSDT, ETHUSDT 5m, 15m

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

- 2026-10-26T14:50:11Z **design** — Owner request (2026-10-26): test every expressible idea on the 5m and 15m timeframes. Pre-registered porting rule, fixed before any backtest: each plan is the idea's original plan with every bar-count parameter, threshold literal and risk setting unchanged; only symbol (BTCUSDT, ETHUSDT) and interval (15m, 5m) change. Calendar-time scaling was rejected because most windows would exceed the feature bound of 1000 bars, and re-picking literals per timeframe would be literal-tuning. Consequence: the same bar counts cover 16x (15m) or 48x (5m) less calendar time than on 4h. Base backtests only at first; a plan gets a sweep or walk-forward only if it clears the screen (net Sharpe above vol-targeted buy-and-hold, random-entry percentile at least 95, at least 100 trades, max DD at most 25%). Ideas 5, 10, 11 and 12 are not expressible in the current lab and are not tested. Costs unchanged: 0.10% fee + 0.05% slippage per side. · [[idea-17]]
- 2026-10-26T14:50:11Z **design** — Plans in this run: idea 17 large_move_macd_momentum_btc_15m `947d875cc035`; idea 17 large_move_macd_momentum_eth_15m `53761a8cd847`; idea 17 large_move_macd_momentum_btc_5m `27b4fd5aef5d`; idea 17 large_move_macd_momentum_eth_5m `0a83977ecb4c` · [[idea-17]]
- 2026-10-26T16:00:54Z **backtest** — Idea 17 large_move_macd_momentum_btc_15m: net Sharpe -2.73 (gross 0.60), max DD 95%, 961 trades, vol-target 1.22, random-entry percentile 98.5. Screen: fail. · [[idea-17]] [[trial-3221]] [[run-r-20261026-144959-b1524c]] plan `947d875cc03538c4ebbd5505c9380a6f74305db674152035cf116d0d21bb10d3`
- 2026-10-26T16:00:54Z **backtest** — Idea 17 large_move_macd_momentum_eth_15m: net Sharpe -2.84 (gross 0.20), max DD 98%, 1050 trades, vol-target 1.20, random-entry percentile 73. Screen: fail. · [[idea-17]] [[trial-3350]] [[run-r-20261026-144959-b1524c]] plan `53761a8cd8475a1936368f243bac279f2750069799f0017ae7caddb7333a31c9`
- 2026-10-26T16:00:54Z **backtest** — Idea 17 large_move_macd_momentum_btc_5m: net Sharpe -8.27 (gross 0.63), max DD 100%, 2863 trades, vol-target 1.22, random-entry percentile 99.5. Screen: fail. · [[idea-17]] [[trial-3479]] [[run-r-20261026-144959-b1524c]] plan `27b4fd5aef5d81212d0bffa8a5cc7c427bc85f2fe777fd6b4c555bd23f99792b`
- 2026-10-26T16:00:54Z **backtest** — Idea 17 large_move_macd_momentum_eth_5m: net Sharpe -8.63 (gross -0.35), max DD 100%, 3070 trades, vol-target 1.14, random-entry percentile 71. Screen: fail. · [[idea-17]] [[trial-3612]] [[run-r-20261026-144959-b1524c]] plan `0a83977ecb4cefb72717bf01b4dbe5c689e3ad69e7332667880305f140f17556`
- 2026-10-26T16:00:54Z **decide** — No plan in this run clears the pre-registered screen, so none gets a sweep, walk-forward, adversarial check or cross-check, and no holdout check is spent. Every plan is below vol-targeted buy-and-hold after costs. High random-entry percentiles on some plans only mean equally frequent random entries lose even more to costs. Critic not launched: there is no surviving result to be sceptical about. · [[idea-17]]
- 2026-10-26T16:00:54Z **lesson** — Porting 4h/1h rules to 15m and 5m with the same bar counts multiplies trade counts 10-50x; at retail spot costs (0.10% fee + 0.05% slippage per side) no rule in the library survives. Intraday research on this lab needs either a cost model the owner can actually get (e.g. maker or discounted fees, set in lab.toml by the owner) or rules with far fewer, larger trades. · [[idea-17]]

## End-of-run report

Run [[run-r-20261026-144959-b1524c]] · BTCUSDT, ETHUSDT 5m, 15m · started 2026-10-26T14:49:59Z · finished 2026-10-26T16:05:00Z

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
| Hypotheses | 1 | 5 |  |
| LLM proposals | 4 | 15 |  |
| Backtests (ledger trials) | 132 | 3000 |  |
| Holdout checks | 0 | 1 |  |
| Model spend (USD) | 0.00 | 20.00 |  |

### Ledger size used for DSR

N = 3147: distinct plans tried across every research run in the ledger (this run is [[run-r-20261026-144959-b1524c]]), Optuna and neighbourhood variants included. Every Deflated Sharpe Ratio is deflated by this count, so starting a new run never resets it.

### Ideas tried

| idea | title (scout-sourced, untrusted) | status | reason |
|---|---|---|---|
| [[idea-17]] | Time-limited momentum after a large up-move | abandoned | 5m/15m port fails after costs: BTC 15m net -2.73; ETH 15m net -2.84; BTC 5m net -8.27; ETH 5m net -8.63 |

### Failures and lessons

- [[idea-17]] Time-limited momentum after a large up-move (abandoned): 5m/15m port fails after costs: BTC 15m net -2.73; ETH 15m net -2.84; BTC 5m net -8.27; ETH 5m net -8.63
- Pre-registered port: bar-count parameters, literals and risk unchanged from each idea's original plan; only symbol and interval changed.
- Base backtests executed by background agents from fixed plan files; every reported number was re-checked against the ledger (0 mismatches).
- Ideas 5, 10, 11, 12 are not expressible in the current lab and were not tested.

### Candidates

None. No plan enters paper trading from this run.

### Disclaimer

Backtests are historical, not forward-looking. A candidate is not "successful" until it has passed every success criterion and 4-8 weeks of forward paper trading consistent with its backtest.
