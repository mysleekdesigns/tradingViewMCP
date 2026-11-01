---
run_id: "r-20261101-153240-81f6cb"
date: 2026-11-01
symbols:
  - "ONDOUSDT"
interval: "5m"
outcome: no_edge
idea_ids:
  - R1
criteria_sha256: "fc17a9c46c359fbc117b3f5ba3d141bc7e6a116511f154950a3c57746140e94c"
derivatives_manifest_sha256: "e556899564e75c176d17e17e9289f132f58e820a5d92fb3800f5488fc726cba3"
tags:
  - research
  - phase-8
---
# ONDO 5m: buy when RSI(14) crosses above 30, sell above 50

Run [[run-r-20261101-153240-81f6cb]] · spot, design window only.

Owner's request (2026-11-01), after noticing on the 5m chart that RSI "goes back up" every time it drops under 30: "test buy ONDO 5m when RSI crosses above 30, sell at 50". One owner-specified hypothesis with thresholds fixed a priori (30 / 50, RSI period 14). No scouting.

Data: ONDOUSDT 5m klines downloaded 2026-11-01 from data.binance.vision (archive starts 2025-04; earlier months not in the spot archive). Design window therefore covers only 2025-04-01 to 2025-09-30 (about 6 months, ~52k bars). Holdout starts 2025-10-01 and is not looked at.

Holdout metrics are never written to this journal; only that a check was spent and the resulting status.

## Success criteria and budget (fixed before any backtest)

Criteria file sha256: `fc17a9c46c359fbc117b3f5ba3d141bc7e6a116511f154950a3c57746140e94c`, frozen by `start_run` at 2026-11-01 15:32:40 UTC, same file as earlier runs. Fixed before the first backtest and never relaxed.

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
| Profitable regimes of 3, at least | `min_profitable_regimes` | 2 |
| Regime label window (trailing days) | `regime_window_days` | 90 |
| Regime threshold (± simple return over the window) | `regime_threshold` | 0.1 |
| Parameter neighbourhood (±%) that must stay profitable | `neighbourhood_pct` | 0.2 |
| Matched cross-check required | `require_crosscheck_match` | required |
| Holdout pass required | `require_holdout_pass` | required |

Budget: 5 hypotheses, 15 proposals, 3000 backtests, 1 holdout check (interactive run).

## Plan

[[idea-R1]] `ondo_5m_rsi14_cross30_exit50`, plan_hash `8f32da1bb6d50aa5d6ee0ef6e6a709120d652b345e4dfbbbb0a4bf8aad905556`

- features: `r = rsi(14)`
- long_entry: `crosses_above(r, 30)`
- long_exit: `r > 50`
- no stop, no trend filter; signal at bar t close, fill at bar t+1 open (lab default costs)

## Log

- 2026-11-01 15:32 UTC: `start_run` returned [[run-r-20261101-153240-81f6cb]]; criteria, budget and plan above written before any backtest.
- 2026-11-01 15:35 UTC: `validate_plan` valid; `run_backtest` [[trial-7565]] (8 ledger trials incl. benchmarks, ±20% neighbours).
- 2026-11-01 15:36 UTC: `get_report`: pre_holdout_passed false. Abandoned at a-priori parameters; no sweep, walk-forward, adversarial or cross-check (the edge before costs is smaller than costs, so tuning cannot rescue it). No holdout check spent; holdout untouched.

## Results (design window 2025-04-11 to 2025-09-30, 49,656 bars, ~0.47 years)

Costs: fee 0.10% + slippage 0.05% per side (lab default), signal at close t, fill at open t+1.

| | net | gross (no costs) |
|---|---|---|
| total return | −47.2% | +59.5% |
| Sharpe (365-day) | −3.73 | 3.08 |
| max drawdown | 50.7% | 12.3% |
| trades | 325 | 325 |
| exposure | 11.1% | 11.1% |

Benchmarks: buy-and-hold net Sharpe 0.44 (total −0.3%, DD 44%); vol-targeted −0.36; random-entry median −6.94 with the same trade count (plan at the 100th percentile).

Average trade: about +0.14% before costs vs about 0.30% round-trip cost; average hold about 17 bars (~85 minutes). Breakeven cost is roughly 0.07% per side, below Binance's 0.075% BNB-discounted fee even with zero slippage.

| criterion | status | value |
|---|---|---|
| dsr_min | fail | ~0 (N = 5890) |
| beat_buy_hold | fail | −3.73 vs 0.44 |
| beat_vol_target | fail | −3.73 vs −0.36 |
| random_entry_percentile_min | pass | 100 |
| max_drawdown_max | fail | 0.507 |
| min_trades | pass | 325 |
| min_profitable_regimes | fail | 1 of 3 (bull −7.9%, sideways −17.6%) |
| neighbourhood_pct | fail | 0 of 4 ±20% RSI-period neighbours profitable |
| wf_oos_sharpe_min, pbo_max, cost_stress, delay decay, crosscheck, holdout | missing | not run (abandoned) |

## Verdict: no edge found

The RSI(14) 30-cross bounce is real in the gross sense on ONDO 5m in this window (it beats random entry, gross Sharpe 3.1), which matches what the owner saw on the chart. But each bounce averages about 0.14%, half of what a round trip costs, so the net result is a 47% loss. The same pattern as the BTC/ETH 5m/15m dip-buy ports ([[idea-14]]): short-horizon reversal exists before costs and dies after them. Even the gross figure would not be significant after deflating for 5890 plans over a 6-month window.

Lesson: on 5m bars, a mean-reversion signal needs an average move several times the ~0.3% round-trip cost. RSI 30→50 on 14 bars captures far less.

> [!note] Historical simulation on Binance spot data; not forward-looking. Design window only; the locked holdout (from 2025-10-01) was not checked.
