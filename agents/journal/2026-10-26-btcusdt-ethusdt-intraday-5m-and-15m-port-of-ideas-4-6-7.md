---
run_id: "r-20261026-144959-a1d646"
date: 2026-10-26
symbols:
  - "BTCUSDT"
  - "ETHUSDT"
interval: "5m, 15m"
outcome: no_edge
idea_ids:
  - 4
  - 6
  - 7
criteria_sha256: "cc544fe9302e2ffdfeb9af8d5c741753812f362e6e508dbd40f4113d55b72126"
tags:
  - research
  - phase-8
---
# Intraday 5m and 15m port of ideas 4, 6, 7

Run [[run-r-20261026-144959-a1d646]] · BTCUSDT, ETHUSDT 5m, 15m

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

- 2026-10-26T14:50:11Z **design** — Owner request (2026-10-26): test every expressible idea on the 5m and 15m timeframes. Pre-registered porting rule, fixed before any backtest: each plan is the idea's original plan with every bar-count parameter, threshold literal and risk setting unchanged; only symbol (BTCUSDT, ETHUSDT) and interval (15m, 5m) change. Calendar-time scaling was rejected because most windows would exceed the feature bound of 1000 bars, and re-picking literals per timeframe would be literal-tuning. Consequence: the same bar counts cover 16x (15m) or 48x (5m) less calendar time than on 4h. Base backtests only at first; a plan gets a sweep or walk-forward only if it clears the screen (net Sharpe above vol-targeted buy-and-hold, random-entry percentile at least 95, at least 100 trades, max DD at most 25%). Ideas 5, 10, 11 and 12 are not expressible in the current lab and are not tested. Costs unchanged: 0.10% fee + 0.05% slippage per side. · [[idea-4]] [[idea-6]] [[idea-7]]
- 2026-10-26T14:50:11Z **design** — Plans in this run: idea 4 vt-chandelier-trend_btc_15m `a511f072c8cb`; idea 4 vt-chandelier-trend_eth_15m `e398c2d956a6`; idea 4 vt-chandelier-trend_btc_5m `122e514d11f1`; idea 4 vt-chandelier-trend_eth_5m `5cdac5aff68f`; idea 6 trend-volspike-derisk_btc_15m `6c817e8c3887`; idea 6 trend-volspike-derisk_eth_15m `9ce1f68fa6ef`; idea 6 trend-volspike-derisk_btc_5m `3bf4d97d7090`; idea 6 trend-volspike-derisk_eth_5m `ad769042ebac`; idea 7 ema_long_flat_btc_15m `e2689cd45a76`; idea 7 ema_long_flat_eth_15m `9813c1ed5089`; idea 7 ema_long_flat_btc_5m `9f039c9686ab`; idea 7 ema_long_flat_eth_5m `347b1b3be651` · [[idea-4]] [[idea-6]] [[idea-7]]
- 2026-10-26T16:00:54Z **backtest** — Idea 4 vt-chandelier-trend_btc_15m: net Sharpe -2.28 (gross 0.50), max DD 91%, 742 trades, vol-target 1.22, random-entry percentile 27. Screen: fail. · [[idea-4]] [[trial-3167]] [[run-r-20261026-144959-a1d646]] plan `a511f072c8cb2afbe8f5cbaac044f9a170c9884f0fb87c60bf147b86d27d5693`
- 2026-10-26T16:00:54Z **backtest** — Idea 4 vt-chandelier-trend_eth_15m: net Sharpe -1.13 (gross 1.05), max DD 80%, 691 trades, vol-target 1.20, random-entry percentile 83. Screen: fail. · [[idea-4]] [[trial-3296]] [[run-r-20261026-144959-a1d646]] plan `e398c2d956a669e07aec93b55b858fc40b0fa487694117687eaf1c210a9a9be7`
- 2026-10-26T16:00:54Z **backtest** — Idea 4 vt-chandelier-trend_btc_5m: net Sharpe -7.87 (gross 0.26), max DD 100%, 2667 trades, vol-target 1.22, random-entry percentile 1. Screen: fail. · [[idea-4]] [[trial-3425]] [[run-r-20261026-144959-a1d646]] plan `122e514d11f1c192c8d7e9c1dcef0314b039998fd5142b81bb01588ad976e67c`
- 2026-10-26T16:00:54Z **backtest** — Idea 4 vt-chandelier-trend_eth_5m: net Sharpe -5.96 (gross 0.28), max DD 100%, 2467 trades, vol-target 1.14, random-entry percentile 13.5. Screen: fail. · [[idea-4]] [[trial-3558]] [[run-r-20261026-144959-a1d646]] plan `5cdac5aff68f910bcf100d9983df13b12bf2a80c8749a604b0da1030c3957d78`
- 2026-10-26T16:00:54Z **backtest** — Idea 6 trend-volspike-derisk_btc_15m: net Sharpe -5.73 (gross 1.06), max DD 100%, 5228 trades, vol-target 1.22, random-entry percentile 0. Screen: fail. · [[idea-6]] [[trial-3691]] [[run-r-20261026-144959-a1d646]] plan `6c817e8c388703e3ba24a3e176752e84eade49508d6d8990823674bde7d961ab`
- 2026-10-26T16:00:54Z **backtest** — Idea 6 trend-volspike-derisk_eth_15m: net Sharpe -4.41 (gross 1.24), max DD 100%, 5331 trades, vol-target 1.20, random-entry percentile 1. Screen: fail. · [[idea-6]] [[trial-3791]] [[run-r-20261026-144959-a1d646]] plan `9ce1f68fa6ef814ab8b7fe97f7cbd25df85f85e603c19ea231ed96a8976a1df4`
- 2026-10-26T16:00:54Z **backtest** — Idea 6 trend-volspike-derisk_btc_5m: net Sharpe -10.80 (gross 0.23), max DD 100%, 5488 trades, vol-target 1.22, random-entry percentile 0. Screen: fail. · [[idea-6]] [[trial-3891]] [[run-r-20261026-144959-a1d646]] plan `3bf4d97d7090ed6cf8ec39e4ce18568a1b840704f3dc9b1de2b1ffd14ae37560`
- 2026-10-26T16:00:54Z **backtest** — Idea 6 trend-volspike-derisk_eth_5m: net Sharpe -7.66 (gross 1.40), max DD 100%, 6754 trades, vol-target 1.14, random-entry percentile 39. Screen: fail. · [[idea-6]] [[trial-3995]] [[run-r-20261026-144959-a1d646]] plan `ad769042ebaca4949f19943e3d203895a644e204261354b9d67de582aa29d942`
- 2026-10-26T16:00:54Z **backtest** — Idea 7 ema_long_flat_btc_15m: net Sharpe -1.36 (gross 1.58), max DD 99%, 2452 trades, vol-target 1.22, random-entry percentile 88. Screen: fail. · [[idea-7]] [[trial-4099]] [[run-r-20261026-144959-a1d646]] plan `e2689cd45a767db442e4387afb5344d2c8491a5cf756598962f94dc2cabf0cb9`
- 2026-10-26T16:00:54Z **backtest** — Idea 7 ema_long_flat_eth_15m: net Sharpe -0.88 (gross 1.58), max DD 99%, 2480 trades, vol-target 1.20, random-entry percentile 90.5. Screen: fail. · [[idea-7]] [[trial-4193]] [[run-r-20261026-144959-a1d646]] plan `9813c1ed508972cfa6c091da36eeb259a9debc0a7f49ed9ce722b72a14612f60`
- 2026-10-26T16:00:54Z **backtest** — Idea 7 ema_long_flat_btc_5m: net Sharpe -6.18 (gross 1.22), max DD 100%, 5862 trades, vol-target 1.22, random-entry percentile 1. Screen: fail. · [[idea-7]] [[trial-4287]] [[run-r-20261026-144959-a1d646]] plan `9f039c9686abdeec29cb7376c857210d18194090523045fce71da26d11892596`
- 2026-10-26T16:00:54Z **backtest** — Idea 7 ema_long_flat_eth_5m: net Sharpe -5.69 (gross 1.13), max DD 100%, 6729 trades, vol-target 1.14, random-entry percentile 0.5. Screen: fail. · [[idea-7]] [[trial-4352]] [[run-r-20261026-144959-a1d646]] plan `347b1b3be6513bbab7b4c1a2166ce0fa4f1ca14aac733942151d4122d7a7049e`
- 2026-10-26T16:00:54Z **decide** — No plan in this run clears the pre-registered screen, so none gets a sweep, walk-forward, adversarial check or cross-check, and no holdout check is spent. Every plan is below vol-targeted buy-and-hold after costs. 8 plan(s) had gross Sharpe above 1.0 (vt-chandelier-trend_eth_15m, trend-volspike-derisk_btc_15m, trend-volspike-derisk_eth_15m, trend-volspike-derisk_eth_5m, ema_long_flat_btc_15m, ema_long_flat_eth_15m, ema_long_flat_btc_5m, ema_long_flat_eth_5m) but trade hundreds to thousands of times, and about 0.3% round-trip costs turn them negative. High random-entry percentiles on some plans only mean equally frequent random entries lose even more to costs. Critic not launched: there is no surviving result to be sceptical about. · [[idea-4]] [[idea-6]] [[idea-7]]
- 2026-10-26T16:00:54Z **lesson** — Porting 4h/1h rules to 15m and 5m with the same bar counts multiplies trade counts 10-50x; at retail spot costs (0.10% fee + 0.05% slippage per side) no rule in the library survives. Intraday research on this lab needs either a cost model the owner can actually get (e.g. maker or discounted fees, set in lab.toml by the owner) or rules with far fewer, larger trades. · [[idea-4]] [[idea-6]] [[idea-7]]

## End-of-run report

Run [[run-r-20261026-144959-a1d646]] · BTCUSDT, ETHUSDT 5m, 15m · started 2026-10-26T14:49:59Z · finished 2026-10-26T16:05:00Z

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
| Backtests (ledger trials) | 268 | 3000 |  |
| Holdout checks | 0 | 1 |  |
| Model spend (USD) | 0.00 | 20.00 |  |

### Ledger size used for DSR

N = 3147: distinct plans tried across every research run in the ledger (this run is [[run-r-20261026-144959-a1d646]]), Optuna and neighbourhood variants included. Every Deflated Sharpe Ratio is deflated by this count, so starting a new run never resets it.

### Ideas tried

| idea | title (scout-sourced, untrusted) | status | reason |
|---|---|---|---|
| [[idea-4]] | Trend entry with ATR chandelier exit | abandoned | 5m/15m port fails after costs: BTC 15m net -2.28; ETH 15m net -1.13; BTC 5m net -7.87; ETH 5m net -5.96 |
| [[idea-6]] | Volatility-spike de-risking switch | abandoned | 5m/15m port fails after costs: BTC 15m net -5.73; ETH 15m net -4.41; BTC 5m net -10.80; ETH 5m net -7.66 |
| [[idea-7]] | Long/flat EMA trend filter on BTC and ETH | abandoned | 5m/15m port fails after costs: BTC 15m net -1.36; ETH 15m net -0.88; BTC 5m net -6.18; ETH 5m net -5.69 |

### Failures and lessons

- [[idea-4]] Trend entry with ATR chandelier exit (abandoned): 5m/15m port fails after costs: BTC 15m net -2.28; ETH 15m net -1.13; BTC 5m net -7.87; ETH 5m net -5.96
- [[idea-6]] Volatility-spike de-risking switch (abandoned): 5m/15m port fails after costs: BTC 15m net -5.73; ETH 15m net -4.41; BTC 5m net -10.80; ETH 5m net -7.66
- [[idea-7]] Long/flat EMA trend filter on BTC and ETH (abandoned): 5m/15m port fails after costs: BTC 15m net -1.36; ETH 15m net -0.88; BTC 5m net -6.18; ETH 5m net -5.69
- Pre-registered port: bar-count parameters, literals and risk unchanged from each idea's original plan; only symbol and interval changed.
- Base backtests executed by background agents from fixed plan files; every reported number was re-checked against the ledger (0 mismatches).
- Ideas 5, 10, 11, 12 are not expressible in the current lab and were not tested.

### Candidates

None. No plan enters paper trading from this run.

### Disclaimer

Backtests are historical, not forward-looking. A candidate is not "successful" until it has passed every success criterion and 4-8 weeks of forward paper trading consistent with its backtest.
