---
run_id: "r-20261028-103056-807dd5"
date: 2026-10-28
symbols:
  - "BTCUSDT"
  - "ETHUSDT"
interval: "4h"
outcome: no_edge
idea_ids:
  - H3
criteria_sha256: "fc17a9c46c359fbc117b3f5ba3d141bc7e6a116511f154950a3c57746140e94c"
derivatives_manifest_sha256: "e556899564e75c176d17e17e9289f132f58e820a5d92fb3800f5488fc726cba3"
tags:
  - research
  - phase-12
---
# H3 funding z-score as a crash-risk gate on a spot long-only EMA trend (pre-registered)

Run [[run-r-20261028-103056-807dd5]] · BTCUSDT and ETHUSDT spot, 4h, gated by USD-M perpetual funding (public `data.binance.vision` archive, historical only).

Holdout metrics are never written to this journal; only that a check was spent and the resulting status.

## Success criteria and budget (fixed before any backtest)

Criteria file sha256: `fc17a9c46c359fbc117b3f5ba3d141bc7e6a116511f154950a3c57746140e94c`. It was frozen by `start_run` at 2026-10-28 10:30:56 UTC. The derivatives manifest sha256 `e5568995…` is frozen with the run as a per-series snapshot. These values were fixed before the first backtest of this run and are never relaxed mid-run.

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

## Pre-registered hypothesis and plans

**H3.** When perpetual funding is unusually high against its own trailing month, leveraged longs are crowded and the market is prone to long-liquidation cascades. A spot long-only trend entry that skips those bars should keep most of the trend's return and lose less in crashes. Funding is used only as an exposure / crash-risk filter on entries, never as a direction signal.

Framing note: the strategy report (`agents/research/reports/Crypto strategy edge research.md`, H3) words this as an overlay on buy-and-hold. The lab only allows derivatives features as an `and` gate beside a price clause, and never in exits. So H3 is tested here as a gate on a simple EMA trend entry. The question is whether gating the entry improves on the same trend rule without the gate.

The logic is one hypothesis, run on two symbols:

```json
{"name":"H3 ema trend + funding z gate BTC","symbol":"BTCUSDT","interval":"4h",
 "features":{"ema_slow":{"ema":{"period":100}},"fz":{"funding_zscore":{"period":180}}},
 "long_entry":"close > ema_slow and fz < 2","long_exit":"close < ema_slow",
 "risk":{"position_size_pct":100}}
```

The ETHUSDT plan is the same plan with `"symbol":"ETHUSDT"`. The plan hashes are BTC `dc5cd0bd…a680b` and ETH `85577a7b…a36d2`.

- The literal `fz < 2` is a fixed hypothesis and is never hand-tuned.
- There are 2 tunable params. EMA 100 on 4h is about 17 days. The funding z-score window of 180 bars is 30 days, the lab's look-back cap.
- **Scored start** (shared by every derivatives plan on the market):
  - BTCUSDT 4h from 2020-10-01;
  - ETHUSDT 4h from 2021-12-31, because ETH metrics only exist from 2021-12, so ETH has a shorter design window.
  - `scoring.scored_from_utc` is quoted from the report.
- **Missing data:** gates fail closed. A NaN z-score (a gap in the window) means no entry.
- **Ungated comparison:** `get_report` → `informational.ungated_baseline` gives the same plan without `fz < 2`, on the same window. It is not a trial and not a criterion. It is the direct answer to "does the gate help?".
- **Decision rule:** a plan is a candidate only if it passes every pre-holdout criterion. At most one holdout check is spent, the owner approves it through the hook, and a failed check kills H3. If neither plan passes, the outcome is "no edge found". The logic is not refined after seeing results.

Sweep and walk-forward ranges (fixed now, the same for both symbols):

| param | low | high |
|---|---|---|
| `ema_slow.period` | 50 | 200 |
| `fz.period` | 42 | 180 |

Trials: sweep 50, walk-forward 50 per fold.

## Log

- 2026-10-28 10:3x UTC. Ran `run_backtest` on both pre-registered plans: BTC [[trial-4984]] and ETH [[trial-5000]], design window, configured costs.
  - **BTC** (scored from 2020-10-01T00:00:00Z):
    - net Sharpe 0.74, gross 1.35; max DD −66.6%; 239 trades; random-entry percentile 88.
    - Benchmarks: buy-and-hold Sharpe 1.08, vol-target 1.22.
    - Ungated baseline Sharpe 0.73, max DD −67.0%. The gate blocks 5.2% of entry bars; plan − ungated Sharpe is +0.01.
  - **ETH** (scored from 2021-12-31T00:00:00Z):
    - net Sharpe 0.35, gross 0.97; max DD −45.2%; 193 trades; random-entry percentile 90.
    - Benchmarks: buy-and-hold Sharpe 0.38, vol-target 0.51.
    - Ungated baseline Sharpe 0.39. The gate blocks 4.6% of entry bars; plan − ungated Sharpe is −0.04.
- Ran `run_sweep` (50 trials each, ranges as pre-registered).
  - BTC best: in-sample Sharpe 1.13 at ema 185 / fz 61.
  - ETH best: in-sample Sharpe 0.92 at ema 197 / fz 116.
  - Both best results sit at the slow edge of the EMA range. DSR N is about 3,750.
- **Lab-imposed range change (not a tuning choice):** `walk_forward` refused the pre-registered EMA range 50–200.
  - The gate is nearly a no-op, so the gated plans trade like plain EMA-trend plans from earlier runs (returns correlate at or above the `[research] family_return_corr` 0.8). The lab treats H3 as the same behaviour family.
  - Walk-forward must search every value that family already tried: `ema_slow.period` 10–1000 for BTC and 50–300 for ETH. Walk-forward is rerun with those wider ranges; `fz.period` stays at 42–180. A wider search is more conservative.
  - The finding itself is evidence about H3: at `fz < 2`, the funding gate does not make a distinct strategy.
- Ran `walk_forward` with the wider ranges (50 trials per fold, params tuned on train windows only).
  - **BTC**, 6 folds: stitched OOS net Sharpe 0.78, max DD −45%, 108 trades.
    - Fold test Sharpes: 1.70, −1.73, 4.04, −2.70, 0.69, 0.56.
    - The chosen EMA periods drift from 188 to 540.
  - **ETH**, 3 folds: OOS Sharpe 0.81 on only 49 trades. Fold test Sharpes: 0.52, 1.65, 0.37.
  - `get_report` judges the minimum over every walk-forward of the behaviour family across runs: BTC 0.27, ETH 0.41.
- Ran `run_adversarial`.
  - **BTC** (base trial 5572; each adversarial variant is its own ledger trial):
    - Signal delay 0/1/3/5 bars: Sharpe 0.74 / 0.90 / 0.73 / 0.73. Flagged: the edge is unchanged after a 5-bar delay, so the timing isn't what earns the return.
    - Sign flip: blew up.
    - Cost ×2: Sharpe 0.14, total return −13%. Cost ×4: Sharpe −1.02.
    - Permutation null: p 0.020.
  - **ETH** (base trial 5580):
    - Signal delay: Sharpe 0.35 / 0.28 / 0.30 / 0.10.
    - Sign flip: Sharpe −0.96.
    - Cost ×2: Sharpe −0.27.
    - Monte Carlo: the realised trade order flattered the drawdown.
    - Permutation null: p 0.030.
- Ran `run_crosscheck` (nautilus_trader): matched for both plans, BTC 239/239 trades and ETH 193/193, return difference ≈ 1e-7%.
- **`get_report`: `pre_holdout_passed` is false for both plans.**
  - BTC fails: `wf_oos_sharpe_min` 0.27, `dsr_min` ≈ 0 (N 4198), `pbo_max` 0.80, `beat_buy_hold`, `beat_vol_target`, `random_entry_percentile_min` 88, `cost_stress_factor` (−13% at ×2), `require_gradual_delay_decay` and `max_drawdown_max` 0.67.
  - BTC passes: `min_trades`, `min_profitable_regimes` and the neighbourhood (6/6). The cross-check matched.
  - ETH fails: `wf_oos_sharpe_min` 0.41, `dsr_min` ≈ 0, `pbo_max` 0.66, `beat_buy_hold`, `beat_vol_target`, `random_entry_percentile_min` 90, `cost_stress_factor`, `max_drawdown_max` 0.45 and `neighbourhood_pct` (5/6).
  - ETH passes: `min_trades`, `min_profitable_regimes`, delay decay and the cross-check.
- Ungated baseline (informational): the gate changes almost nothing.
  - BTC: Sharpe +0.010, max DD +0.3 points.
  - ETH: Sharpe −0.042, max DD −0.5 points.
- **No holdout check is possible or spent.**
- `strategy-critic` (fresh context): **abandon both plans; no edge found for H3; do not spend the holdout.**
  - These are slow EMA trend-followers, which is lagged beta. That is why BTC's Sharpe barely moves under a 5-bar delay. This is not look-ahead.
  - The permutation p-values only show that trend timing beats shuffled timing. They show no edge over beta or over the random-entry null.
  - The two "profitable regime" passes are coin flips: BTC bear +1%, ETH sideways +2%.
  - No follow-up in this run. A lower threshold (`fz < 1`) would be a literal tuned after seeing results. A basis or premium gate has the same entry-only structure, so it would very likely be a no-op in the same family.

## End-of-run report

# Research run r-20261028-103056-807dd5 (2026-10-28; budget: 5 hypotheses / 15 proposals / 3000 backtests / 1 holdout check / $20)

**Outcome: no edge found.**
- Both pre-registered plans fail the pre-holdout criteria, 9 rows each.
- The funding gate is nearly inert: it blocks about 5% of entry bars and changes Sharpe by +0.01 on BTC and −0.04 on ETH.
- The holdout was not spent. It stays available to a genuinely different hypothesis.

Used:
- 1 hypothesis (H3);
- 2 proposals (one logic on two symbols);
- 604 backtests (ledger N 4198 distinct plans across every run);
- 0 holdout checks;
- $0 of LLM budget (interactive run).

## H3 ema trend + funding z gate: BTCUSDT 4h (scored 2020-10-01 to 2025-10-01, costs 0.10% + 0.05% per side)

Hypothesis: skipping trend entries while funding is unusually high (crowded leveraged longs) cuts crash losses without giving up the trend. Rationale: crowded longs pay funding and are the forced sellers in a liquidation cascade.
- Plan hash `dc5cd0bd…a680b`.
- Features: `ema` (100), `funding_zscore` (180).
- Tunable params: 2. The literal `fz < 2` is fixed.

| metric | gross | net | net ×2 cost | buy&hold | vol-tgt B&H | ungated (same window) |
|---|---|---|---|---|---|---|
| CAGR | 60.6% | 25.0% | — | 60.1% | 51.3% | 24.5% |
| Sharpe (365d) | 1.35 | 0.74 | 0.14 | 1.08 | 1.22 | 0.73 |
| Sortino | 1.99 | 1.09 | — | 1.56 | 1.76 | 1.07 |
| max DD | −52.0% | −66.6% | −80.9% | −77.0% | −64.7% | −67.0% |
| win rate | 17.2% | 17.2% | — | — | — | 17.2% |
| profit factor | 1.26 | 1.26 | — | — | — | 1.25 |
| trades | 239 | 239 | 239 | 1 | 1 | 240 |
| exposure | 55% | 55% | — | 100% | 100% | 55% |

The table above is the fixed-parameter design-window backtest. The walk-forward out-of-sample results:
- Stitched net Sharpe 0.78, max DD −45%, 108 trades.
- The report's minimum over the family's walk-forwards across runs is 0.27.

Other evidence:
- **Overfitting:** DSR ≈ 0 (N 4198). PBO 0.80 (neighbourhood) and 0.67 (sweep CSCV). The bootstrap 95% CI on Sharpe is [−0.18, 1.71].
- **Nulls:** random-entry percentile 88; permutation null p 0.020 (99th percentile of 100).
- **Robustness:** signal-delay Sharpe 0 bars 0.74 → 1 bar 0.90 → 3 bars 0.73 → 5 bars 0.73 (flagged as unchanged). Sign flip blew up. Parameter neighbours (±20%) profitable: 6/6.
- **Regimes** (total return): bull +139%, bear +1%, sideways +26%.
- **Holdout:** not checked (0/1 used). **Cross-check:** nautilus matched 239/239 trades.

Success criteria: 4 of 13 measured rows pass. **Verdict: abandoned.**

## H3 ema trend + funding z gate: ETHUSDT 4h (scored 2021-12-31 to 2025-10-01, same costs)

- Plan hash `85577a7b…a36d2`.
- **Returns:** net Sharpe 0.35 (gross 0.97; 2× cost −0.27); max DD −45.2%; 193 trades.
- **Benchmarks:** buy-and-hold 0.38, vol-target 0.51, ungated 0.39.
- **Walk-forward:** OOS Sharpe 0.81 on 49 trades; the family minimum is 0.41.
- **Overfitting:** DSR ≈ 0. PBO 0.66 (sweep) and 0.52 (neighbourhood).
- **Nulls:** random-entry percentile 90; permutation p 0.030.
- **Robustness:** delay 0.35 → 0.28 → 0.30 → 0.10. Sign flip −0.96. Neighbours 5/6.
- **Regimes:** bear +67%, bull −27%, sideways +2%.
- Monte Carlo says the trade order flattered the drawdown. The cross-check matched 193/193.

Success criteria: 4 of 13 measured rows pass. **Verdict: abandoned.**

| hypothesis | family | best walk-forward net Sharpe (family min) | DSR | PBO | verdict | why |
|---|---|---|---|---|---|---|
| H3 BTCUSDT | EMA trend (funding-gated; correlates ≥ 0.8 with plain EMA trend) | 0.78 (0.27) | ≈ 0 | 0.80 | abandoned | below both benchmarks, −13% at ×2 costs, DD 67%, gate inert |
| H3 ETHUSDT | same | 0.81 (0.41) | ≈ 0 | 0.66 | abandoned | below both benchmarks, negative at ×2 costs, DD 45%, gate inert |

### What the evidence says

- **This is a narrow negative, not a refutation of H3.** The lab only allows derivatives features as entry `and` gates, never in exits. So the gate:
  - can only veto new entries;
  - binds on 4.6–5.2% of entry bars;
  - never removes exposure already held when funding spikes in the middle of a trend, which is the crash-risk case H3 describes.
  
  The supported conclusion is "funding z < 2 as an entry-only filter on a trend entry is inert". H3 as an exposure overlay was not tested here.
- **The underlying strategy is lagged beta.** An EMA trend rule on BTC/ETH loses to buy-and-hold and to vol-targeted buy-and-hold. About half the gross Sharpe goes to costs, and the walk-forward folds swing from −2.7 to +4.0. Earlier runs had already rejected this family.
- **The pipeline is sound.** Exact cross-check matches, the shared scored start, the lab-enforced family walk-forward range and the ungated baseline all worked as designed. They do not show an edge.

### Lessons (for the next run)

- **Entry-only derivatives gates on a trend entry hardly bind** (about 5% of entry bars) and cannot cut exposure already held, so they can't test a crash-risk hypothesis. A future H3 test needs a gate that also acts on bars already in a position. That is a lab change (exit-side or exposure gating) for the owner to propose and approve, under the lab lock.
- **State the expected gate binding share before the first backtest.** A gate expected to bind on under ~20% of entries should be redesigned before any trial is spent.
- **Don't follow up with a threshold or a sibling gate.** `fz < 1`, or a basis or premium gate with the same entry-only structure, would be a literal tuned after seeing results, or very likely the same behaviour family. Either would need its own pre-registration in a new run.

Candidates entering paper trading: none.

Backtests are historical, not forward-looking.
