---
run_id: "r-20261101-093620-283543"
date: 2026-11-01
symbols:
  - "BTCUSDT"
  - "ETHUSDT"
interval: "4h"
outcome: no_edge
idea_ids:
  - I1
  - I2
  - I3
criteria_sha256: "fc17a9c46c359fbc117b3f5ba3d141bc7e6a116511f154950a3c57746140e94c"
derivatives_manifest_sha256: "e556899564e75c176d17e17e9289f132f58e820a5d92fb3800f5488fc726cba3"
tags:
  - research
  - phase-8
---
# Untested strategy families on the paper-trading coins

Run [[run-r-20261101-093620-283543]] · spot, design window only. Coins: the seven already in forward paper tests (BTC, ETH, SOL, NEAR, AAVE, SUI, ONDO), plus the lab's point-in-time top-30 universe for portfolio plans. Interval 4h unless a hypothesis needs 1d.

Owner's request (2026-11-01): "start a new research run to find strategies to paper trade". Paper-trade any plan that passes every criterion. If none passes, whether to register anything as an **exploratory** forward test (which can never count as a success) is the owner's decision, asked after the report.

Families already dead in earlier runs, not re-tested: volatility targeting and vol overlays, long/flat trend filters, rolling-high and Donchian breakouts, Markov regime filters, regime-conditional dip buying, volume-climax reversal, short-horizon shock reversal, short-term momentum after large moves, cross-sectional momentum (H1), funding z-score gate (H3), conditional reversal (HA), OI-flush re-entry (HB), MACD + RSI (HC, now in exploratory paper tests). Ledger N at run start: about 5811 distinct plans, so the Deflated Sharpe bar alone needs roughly a 1.7+ net Sharpe.

Holdout metrics are never written to this journal; only that a check was spent and the resulting status.

## Success criteria and budget (fixed before any backtest)

Criteria file sha256: `fc17a9c46c359fbc117b3f5ba3d141bc7e6a116511f154950a3c57746140e94c`. It was frozen by `start_run` at 2026-11-01 09:36:20 UTC. These values were fixed before the first backtest of this run and are never relaxed mid-run.

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

Portfolio plans add: beats the equal-weight universe (required), alpha to BTC p-value at most 0.05, annual alpha above 0.

Budget: 5 hypotheses, 15 proposals, 3000 backtests, 1 holdout check, $20 (model spend applies to autonomous runs; this is an interactive run).

## Log

- 2026-11-01 09:36 UTC: `start_run` returned [[run-r-20261101-093620-283543]]; criteria and budget above written before any backtest.

## Scouted hypotheses

`strategy-scout` searched the web with CrawlForge (2 search batches, 1 batch scrape, 1 highlights scrape). Its headline: the evidence is thin. Every expressible idea it found is a long-only reversal or pullback variant, and the only source that tests 4h reversal rules on BTC/ETH (a vendor blog) found them negative on average. No prompt injection was found.

> [!warning] Untrusted web content (scout)
> - I1, volume-conditioned dip buy: z-score of close below -2 while volume is below its average (low-volume selling is liquidity-driven, so it reverts); exit back at the mean. Sources: J. Banking & Finance 2022 and 2025 papers on crypto liquidity provision (snippets only). Evidence: moderate for the mechanism, weak for this 4h rule.
> - I2, Bollinger lower-band reversion gated by a volatility-stress rank, no trend filter. Sources: arXiv 2608.21888 (reversal at 15m, not 4h), a vendor blog (negative on 4h BTC/ETH on average). Evidence: weak. Close to the dead z-score dip family.
> - I3, RSI(2)-style short-term pullback, ungated. Sources: an equities Substack, the same vendor blog. Evidence: weak for crypto.
> - I4, wide-range down bar closing near its low (forced selling) reversal. No direct source. Evidence: weak; close to the dead volume-climax family.
> - I5, re-entry as volatility decays after stress: likely under 100 trades. Not tested.
> - I6, RSI(2) pullback gated by a futures discount (basis below zero). No source; close to the dead OI-flush family.
> - I7, volume-confirmed continuation bar: close to the dead breakout families. Not tested.
> - I8, vol-scaled weekly reversal: likely under 100 trades. Not tested.

Selected for the lab, in order: I1, I3, I2 (screened at a-priori parameters on BTCUSDT and ETHUSDT 4h first; only plans with a positive net result go on to a sweep and walk-forward). I4 and I6 are held back. I5, I7 and I8 are dropped (too few trades, or a dead family).

## Lab screens (a-priori parameters, design window)

Each plan was backtested once at parameters chosen before any test. Benchmarks: BTCUSDT buy-and-hold net Sharpe 1.13, vol-targeted 1.24; ETHUSDT 1.04 and 1.23.

| idea | plan | trial | net Sharpe | gross Sharpe | trades | max DD | random-entry pct |
|---|---|---|---|---|---|---|---|
| I1 low-volume dip | `i1_lowvol_dip_btc_4h` | [[trial-7419]] | -0.15 | 0.21 | 97 | 48% | 32.5 |
| I1 low-volume dip | `i1_lowvol_dip_eth_4h` | [[trial-7492]] | -0.49 | -0.19 | 94 | 62% | 8 |
| I3 RSI(2) pullback | `i3_rsi2_pullback_btc_4h` | [[trial-7444]] | -1.94 | -0.22 | 743 | 99% | 2.5 |
| I3 RSI(2) pullback | `i3_rsi2_pullback_eth_4h` | [[trial-7517]] | -1.94 | -0.28 | 809 | 99.8% | 0 |
| I2 Bollinger + vol gate | `i2_bb_volgate_btc_4h` | [[trial-7463]] | -0.89 | -0.22 | 168 | 84% | 1 |
| I2 Bollinger + vol gate | `i2_bb_volgate_eth_4h` | [[trial-7536]] | -1.53 | -0.88 | 173 | 97% | 0 |

Plans (all 4h, 10% stop-loss, long/flat):
- I1: entry `z < -2 and volume < vavg` (zscore 20, volume_sma 20), exit `z > 0`.
- I3: entry `r < 10` (rsi 2), exit `close > m` (sma 5).
- I2: entry `close < bb.lower and vr < 0.8` (bollinger 20 × 2.5, vol_rank 30), exit `close > bb.mid`.

Five of six are negative before costs. The only positive gross result (I1 on BTC, 0.21) has 97 trades, 0 of 12 ±20% neighbours profitable, and profits only in the bull regime. `get_report` for I1 BTC: DSR fails at ledger N 5885, below both benchmarks, drawdown, trades, regimes and neighbourhood all fail. No sweep was run on any plan: a sweep over a mechanism with no gross edge would be data mining. Budget after the screens: 6 of 15 proposals and 146 of 3000 backtests used; 0 holdout checks.

## Critic

`strategy-critic` (fresh context) verdict: **abandon** all three. I3 and I2 have negative gross Sharpe on both coins, so there is nothing to refine. I1's one positive gross result is bull-regime beta: it is profitable only in the bull regime, sits at the 32nd percentile of random entries with the same exposure, flips sign on ETH, and has a uniformly negative ±20% neighbourhood. Sweeping I1 or porting any of the three to SOL, NEAR, AAVE, SUI or ONDO would be choosing the coin or the parameters after seeing the failure (data mining), and every trial raises N for later runs. The critic saw no mechanism class left in the feature set with a prior strong enough for the DSR bar, and recommended stopping with the remaining budget unspent.

Lesson: long-only 4h short-term reversal (z-score dip with a volume condition, RSI(2) pullback, Bollinger lower band with a vol gate) has zero-to-negative gross Sharpe on BTCUSDT and ETHUSDT in the design window. It joins the dead families and should not be ported to the alts.

## End-of-run report

**Outcome: No edge found.** This is a correct and complete result.

- **Ideas tried:** 3 hypotheses (I1, I2, I3), 6 plans, each at a-priori parameters on BTCUSDT and ETHUSDT 4h. All abandoned. I4–I8 not tested (dead-family variants or too few trades).
- **Budget used vs limits:** 3 of 5 hypotheses, 6 of 15 proposals, 146 of 3000 backtests (60 plan trials, 68 neighbourhood, 18 benchmark), 0 of 1 holdout checks, no model spend (interactive). The remaining budget is deliberately left unspent.
- **Ledger:** DSR N = 5885 distinct plans across every research run (was about 5811 at run start). Run hash chain verified: 146 trials, head `74ae2ada0262e1367774d79aa4849f3d901795108361578adb87a5e20dead4d6`.
- **Candidates:** none. No plan reached a sweep, walk-forward, adversarial check or cross-check, and the holdout was not touched.
- **Paper trading:** nothing from this run is registered. The seven HC exploratory forward tests (2026-10-30 to 2026-12-25) continue unchanged.
- **What would change the picture:** a mechanism outside what the lab can express today, for example cross-asset inputs (a BTC-state filter for alts, idea 10), shorting or market-neutral spreads, or a different cost model for intraday. Each needs an owner decision and lab changes under the lab lock.

Results are historical simulations on Binance spot data over the design window and are not forward-looking.
