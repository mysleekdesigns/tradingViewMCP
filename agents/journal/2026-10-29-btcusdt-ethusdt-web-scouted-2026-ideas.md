---
run_id: "r-20261029-220805-606d9a"
date: 2026-10-29
symbols:
  - "BTCUSDT"
  - "ETHUSDT"
interval: "4h"
outcome: no_edge
idea_ids:
  - HA
  - HB
  - HC
criteria_sha256: "fc17a9c46c359fbc117b3f5ba3d141bc7e6a116511f154950a3c57746140e94c"
derivatives_manifest_sha256: "e556899564e75c176d17e17e9289f132f58e820a5d92fb3800f5488fc726cba3"
tags:
  - research
  - phase-8
---
# Web-scouted 2026 strategy ideas, lab-tested, then an exploratory forward paper test

Run [[run-r-20261029-220805-606d9a]] · BTCUSDT and ETHUSDT spot, 4h, design window only.

Owner's request (2026-10-29): search the web for the latest trading and quant strategies and test them with paper trading. Owner's decision: run them through the lab first. Paper-trade any that pass. If none pass, register the 1–2 best (with walk-forward evidence) as **exploratory** freqtrade dry-run forward tests, which can never count as a success. The owner launches the bots in their own terminal.

Holdout metrics are never written to this journal; only that a check was spent and the resulting status.

## Success criteria and budget (fixed before any backtest)

Criteria file sha256: `fc17a9c46c359fbc117b3f5ba3d141bc7e6a116511f154950a3c57746140e94c`. It was frozen by `start_run` at 2026-10-29 22:08:05 UTC. These values were fixed before the first backtest of this run and are never relaxed mid-run.

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

Budget: 5 hypotheses, 15 proposals, 3000 backtests, 1 holdout check, $20.

## Scouted hypotheses

`strategy-scout` searched the web with CrawlForge (7 queries, 1 batch scrape). Its headline: the 2025–26 evidence for long/flat BTC/ETH rules is thin. Most of it is vendor blogs and news, and no source gives academic out-of-sample results for these specific rules. No prompt injection was found.

> [!warning] Untrusted web content (scout)
> - HA, daily-horizon conditional reversal inside an intact long-horizon uptrend. Source: J. Risk Financial Management 2026, doi 10.3390/jrfm19090692. Peer-reviewed, with block bootstrap, an SPA test, and a 2016–2020 / 2021–2025 holdout. Only the abstract was readable (the page was blocked), so whether the holdout survived is unknown. Evidence: moderate.
> - HB, leverage-flush re-entry: buy the first recovery from a pullback while open interest is falling. Sources: an Amberdata blog (2026-03-25) and SSRN "Anatomy of a Crypto Cascade" (2026-04). Evidence: mechanism only, weak.
> - HC, MACD histogram turn plus RSI > 50 momentum state, BTCUSDT 4h. Source: a Coinquant vendor blog (2026-07-22) claiming Sharpe 1.24 and 174 trades for 2020–2026, best of three variants, in-sample. Evidence: anecdote, weak.
> - HD, 8–10 week time-series reversal. Source: SSRN 6703978 (2026-05), which is cross-sectional and doesn't transfer as-is. Expected 2–5 trades a year. **Not tested:** it cannot reach `min_trades`.
> - HE, vol compression then expansion. Its own sources say a squeeze gives no direction, and a counter-source shows a loss. Close to the dead breakout family. **Not tested.**

Deduplicated against the dead families listed in the Phase 8–15 journals: trend, breakout, vol overlays, Markov regime, short-horizon and volume-climax reversal, RSI/BB mean-reversion seeds, cross-sectional momentum, the funding crash gate, risk overlays, and 5m/15m ports.

## Pre-registered plans

All plans run on 4h bars, because 1d gives too few trades for `min_trades` 100 and for a useful forward test. Rule literals are fixed hypotheses and are never hand-tuned. Only the listed params are tuned by Optuna.

**HA** (BTCUSDT; the ETHUSDT plan is identical apart from `symbol`). A z-score window of 30 bars is 5 days; SMA 600 is 100 days.

```json
{"name":"HA daily-horizon conditional reversal in uptrend BTC 4h","symbol":"BTCUSDT","interval":"4h",
 "features":{"z":{"zscore":{"period":30}},"trend":{"sma":{"period":600}}},
 "long_entry":"z < -2 and close > trend","long_exit":"z > 0",
 "risk":{"position_size_pct":100,"stop_loss_pct":6}}
```

Ranges: `z.period` 12–60, `trend.period` 300–1000, `risk.stop_loss_pct` 3–10.

**HB** (BTCUSDT). `oi < 0` is an entry-only derivatives gate beside a price clause.

```json
{"name":"HB OI-flush pullback re-entry BTC 4h","symbol":"BTCUSDT","interval":"4h",
 "features":{"z":{"zscore":{"period":20}},"trend":{"sma":{"period":600}},"oi":{"oi_change":{"period":6}}},
 "long_entry":"crosses_above(z, -1) and close > trend and oi < 0","long_exit":"z > 1",
 "risk":{"position_size_pct":100,"stop_loss_pct":5}}
```

Ranges: `z.period` 12–40, `trend.period` 300–1000, `oi.period` 3–18, `risk.stop_loss_pct` 3–10.

**HC** (BTCUSDT, the source's own market and interval):

```json
{"name":"HC MACD hist turn with RSI momentum state BTC 4h","symbol":"BTCUSDT","interval":"4h",
 "features":{"m":{"macd":{"fast":12,"slow":26,"signal":9}},"r":{"rsi":{"period":14}}},
 "long_entry":"crosses_above(m.hist, 0) and r > 50","long_exit":"crosses_below(m.hist, 0)",
 "risk":{"position_size_pct":100,"stop_loss_pct":8}}
```

Ranges: `m.fast` 8–16, `m.slow` 20–40, `m.signal` 6–12, `r.period` 10–20, `risk.stop_loss_pct` 4–12.

Trials: sweep 60, walk-forward 40 per fold. If the lab widens a walk-forward range to cover the behaviour family's history, the wider range is used and logged.

**Decision rule.** A plan is a candidate only if `get_report` shows `pre_holdout_passed` and a `strategy-critic` agrees. At most one holdout check is spent (the owner approves it through the hook). If nothing passes, the outcome is "no edge found". The best 1–2 plans with walk-forward evidence are then paper-traded as exploratory forward tests, labelled as such.

## Log

- 2026-10-29 22:1x UTC. Ran `run_backtest` on the four pre-registered plans (design window 2019-01-01 to 2025-10-01, configured costs). Benchmarks: BTC buy-and-hold Sharpe 1.13, vol-target 1.24; ETH buy-and-hold 1.04, vol-target 1.23.
  - **HA BTC** [[trial-5681]]: net Sharpe −0.29 (gross 0.24); max DD −64%; 131 trades; random-entry percentile 15.5; neighbourhood PBO 0.67. Bull-regime return −48%: in an uptrend a −2σ dip keeps falling into the 6% stop more often than it reverts.
  - **HA ETH** [[trial-5706]]: net Sharpe −0.62 (gross −0.18); max DD −82%; 155 trades; percentile 6.5; PBO 0.93.
  - **HB BTC** [[trial-5731]] (derivatives plan, scored from 2020-10-01): net Sharpe −0.70 (gross −0.08); 132 trades; percentile 7.5; PBO 0.84. The OI gate blocks 48% of price-clause entries. Ungated baseline Sharpe −0.77, so the gate adds +0.07 Sharpe and reduces max DD by 12 points, but the underlying pullback entry has no edge.
  - **HC BTC** [[trial-5755]]: net Sharpe 0.52 (gross 1.30); CAGR 11.8%; max DD −48%; 356 trades; win rate 33%; profit factor 1.09; exposure 26%; percentile 96.5; neighbourhood PBO 0.22. Costs take most of the gross edge (turnover 105).
- **Deviation from the pre-registered schedule:** HA (both symbols) and HB were abandoned after the base backtest, without the planned sweep and walk-forward. Gross Sharpe ≤ 0.24 and random-entry percentiles ≤ 16 leave nothing for a sweep to find, and every sweep trial would raise the DSR bar for every later plan. This is more conservative, not less: no plan was promoted on less evidence.
- Ran `run_sweep` on HC (60 trials, pre-registered ranges). Best in-sample result: Sharpe 0.87 at macd 12/26/7, rsi 12, stop 6.7%. That is the maximum of 60, so it is biased upward. DSR N is now 4335.
- Ran `walk_forward` on HC (9 folds, 40 trials per fold, pre-registered ranges; no lineage widening, so HC did not merge into a dead family).
  - Stitched OOS: net Sharpe **0.33**, max DD −45%, 243 trades.
  - Fold test Sharpes: 1.36, −0.07, −1.43, −0.63, 0.59, 1.65, −1.31, 2.58, −0.45.
  - The chosen params drift across folds (stop 5.5–12%, slow 21–32).
- Ran `run_adversarial` on HC:
  - Delay 0/1/3/5 bars: Sharpe 0.52 / 0.40 / 0.11 / 0.19, a gradual decay.
  - Sign flip: −2.20.
  - Cost ×2: Sharpe −0.26, total return −57%. Cost ×4: −1.73.
  - Monte Carlo: the realised drawdown is typical.
  - Permutation null: p 0.020.
- Ran `run_crosscheck` on HC (nautilus_trader): matched, 356/356 trades. The 1.04% return gap is fully explained by stop-fill slippage.
- **`get_report` HC: `pre_holdout_passed` is false.**
  - Fails: `wf_oos_sharpe_min` 0.33; `dsr_min` ≈ 0 (N 4685; MinBTL 50 years vs a 6.75-year window); `pbo_max` 0.224; `beat_buy_hold`; `beat_vol_target`; `cost_stress_factor` (−57% at ×2); `max_drawdown_max` 0.48.
  - Passes: `random_entry_percentile_min` 96.5; `require_gradual_delay_decay`; `min_trades` 356; `min_profitable_regimes` 3/3; `neighbourhood_pct` 20/20; `require_crosscheck_match`.
  - CPCV paths (an upper bound; information only): 0.57–0.78.
- **No holdout check is possible or spent.**
- `strategy-critic` (fresh context): **abandon all three hypotheses; no edge found.**
  - **HA** fails before costs. Its bull-regime loss (−48%) contradicts the mechanism. A refine would mean tuning the −2 / 0 literals on data already seen.
  - **HB**: the price clause loses money gross, and the OI gate's +0.07 is noise on a losing base.
  - **HC** has 7 hard fails, most of them structural:
    - Costs: it breaks even at about ×1.6 costs, and about 53 round trips a year cost about 16% a year.
    - It trails vol-targeted buy-and-hold by about 0.7 Sharpe.
    - Max DD is 48%, nearly double the 25% cap.
    - The walk-forward is carried by one fold: without fold 8 the mean of the other folds is about −0.04.
  - HC's passing rows (random-entry 96.5, permutation p 0.02, neighbourhood 20/20) are single-test statistics. At N 4685 they are not evidence of edge.
  - The vendor's in-sample Sharpe of 1.24 matches our **gross** 1.30. The vendor quoted a near-gross figure, and its 2020–2026 selection overlaps both our design window and the locked holdout.
  - **No refine of HC in this run.** A lower-turnover variant would be fitted to the cost failure just seen. It cannot close the benchmark and drawdown gaps, and it drifts back into the dead trend family.
- **Exploratory forward paper test (owner's choice before the run).** HC is registered as an implementation-only freqtrade dry-run test, on its base params exactly as walk-forwarded, not the sweep winner: [[2026-10-29-btcusdt-paper-hc-macd-rsi-btc-4h-exploratory]], protocol `20e006dc6d6e`.
  - It runs from 2026-10-30 00:00 UTC to the decision bar on 2026-12-25 (56 days).
  - The Sharpe is NOT tested: MinTRL is about 8,800 days. `locked_split_check` is UNCHECKED, so a pass can only mean "implementation consistent with the backtest", never "success candidate".
  - What the test checks: entries in [2, 14] (8 expected), exposure in [0.036, 0.475], fill timing against the lab replay, and implementation shortfall.
  - HA and HB have no walk-forward evidence, so they were not registered. The slate was not padded to two.
  - Accepted cost: this permanently uses the forward test for the HC behaviour family. A later MACD/RSI-family plan cannot be registered for paper trading.
- Process note: the paper runner treats every `*-paper-*.md` journal file as a paper registration. This journal was first named `...-ideas-for-paper-trading.md`, which made `prepare` refuse (`'candidate'` key missing). It was renamed before any registration. A fix is proposed for the owner; the runner code was not edited.

## End-of-run report

# Research run r-20261029-220805-606d9a (2026-10-29; budget: 5 hypotheses / 15 proposals / 3000 backtests / 1 holdout check / $20)

**Outcome: no edge found.**
- None of the three tested hypotheses (four plans) passes the pre-holdout criteria.
- The holdout was not spent; it stays available.
- One plan (HC) is registered as an **exploratory** forward paper test of the implementation only. It is not a candidate.

Used:
- 3 hypotheses (HA, HB, HC);
- 4 proposals;
- 544 backtests (ledger N 4685 distinct plans across every run);
- 0 holdout checks;
- $0 of LLM budget (interactive run).

| hypothesis | source (untrusted, scout) | net Sharpe (gross) | best walk-forward net Sharpe | random-entry pct | PBO | verdict | why |
|---|---|---|---|---|---|---|---|
| HA BTCUSDT 4h | JRFM 2026 conditional reversal | −0.29 (0.24) | not run | 15.5 | 0.67 | abandoned | no gross edge; loses in bull regimes |
| HA ETHUSDT 4h | same | −0.62 (−0.18) | not run | 6.5 | 0.93 | abandoned | negative gross |
| HB BTCUSDT 4h | Amberdata / SSRN cascade mechanism | −0.70 (−0.08) | not run | 7.5 | 0.84 | abandoned | price clause loses gross; gate +0.07 only |
| HC BTCUSDT 4h | Coinquant vendor blog | 0.52 (1.30) | 0.33 | 96.5 | 0.224 | abandoned (exploratory paper test of implementation only) | below buy-and-hold (1.13) and vol-target (1.24); negative at ×2 costs; DD 48%; DSR ≈ 0 |

### HC detail (BTCUSDT 4h, design window 2019-01-01 to 2025-10-01, costs 0.10% + 0.05% per side)

Plan hash `724ce614…99342180`. Features: `macd` (12/26/9), `rsi` (14). Tunable params: 5 (the stop included). The literals `0` and `50` are fixed.

| metric | gross | net | net ×2 cost | buy&hold | vol-tgt B&H |
|---|---|---|---|---|---|
| CAGR | 41.9% | 11.8% | — | 66.2% | 53.4% |
| Sharpe (365d) | 1.30 | 0.52 | −0.26 | 1.13 | 1.24 |
| max DD | −32.5% | −48.1% | −81.6% | −77.0% | −64.7% |
| win rate | 33.1% | 33.1% | — | — | — |
| profit factor | 1.09 | 1.09 | — | — | — |
| trades | 356 | 356 | 356 | 1 | 1 |
| exposure | 26% | 26% | — | 100% | 100% |

Other evidence:
- **Walk-forward:** stitched OOS net Sharpe 0.33, max DD −45%, 243 trades. 4 of 9 folds are positive.
- **Overfitting:** DSR ≈ 0 (MinBTL 50 years vs a 6.75-year window). PBO 0.224 (neighbourhood) and 0.217 (sweep CSCV). CPCV paths 0.57–0.78 (an upper bound).
- **Robustness:** delay 0.52 → 0.40 → 0.11 → 0.19 (gradual). Sign flip −2.20. Neighbours profitable 20/20. Regimes: bear +41%, bull +48%, sideways +2%.
- **Cross-check:** nautilus matched 356/356.

### Lessons

- **The 2025–26 web offers little new for long/flat spot BTC/ETH rules.** The scout found mostly vendor blogs and news. The one peer-reviewed source (HA) did not transfer to 4h bars with a trend gate, and it has no gross edge on the 2019–2025 design window.
- **Vendor Sharpe claims are close to gross.** Before designing a plan, screen scouted ideas on turnover × 0.30% round-trip cost and against vol-targeted buy-and-hold (about 1.24 on BTC 4h). Any idea trading about 50 or more times a year needs a gross Sharpe well above 2 to survive.
- **At N 4685 the DSR bar alone needs a net Sharpe of about 1.7** on this window. Single-symbol long/flat rules built from these registry features have now failed across trend, breakout, mean reversion, regime, derivatives gates, overlays, and now momentum-state and conditional-reversal ideas. New evidence would need new inputs (cross-asset features, a different cost model, or shorting), which are owner decisions.

Backtest results are historical simulations on Binance spot data, not forward-looking. The forward paper test checks implementation only and can never make HC a success.
