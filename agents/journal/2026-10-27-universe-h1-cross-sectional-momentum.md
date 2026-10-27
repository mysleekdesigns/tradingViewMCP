---
run_id: "r-20261027-123040-26eca9"
date: 2026-10-27
symbols:
  - "UNIVERSE"
interval: "1d"
outcome: no_edge
idea_ids:
  - H1
criteria_sha256: "fc17a9c46c359fbc117b3f5ba3d141bc7e6a116511f154950a3c57746140e94c"
universe_manifest_sha256: "ad467c415023d750c92373f4fe73614fb701a38107a1d25d942b390fa9d03a84"
tags:
  - research
  - phase-10
---
# H1 cross-sectional momentum (pre-registered)

Run [[run-r-20261027-123040-26eca9]] · point-in-time Binance spot universe (676 symbols in the manifest), 1d

Holdout metrics are never written to this journal; only that a check was spent and the resulting status.

## Success criteria and budget (fixed before any backtest)

Criteria file sha256: `fc17a9c46c359fbc117b3f5ba3d141bc7e6a116511f154950a3c57746140e94c` (frozen by `start_run`). Config sha256 `fc17a9c4…` and universe manifest sha256 `ad467c41…` are frozen with the run. These values were fixed before the first backtest of this run and are never relaxed mid-run.

| criterion | key | value |
|---|---|---|
| Walk-forward OOS Sharpe after costs (365-day), at least | `wf_oos_sharpe_min` | 1 |
| Deflated Sharpe Ratio (full trial count), at least | `dsr_min` | 0.95 |
| Probability of backtest overfitting, at most | `pbo_max` | 0.2 |
| Beats buy-and-hold (risk-adjusted) | `beat_buy_hold` | required |
| Beats volatility-targeted buy-and-hold (risk-adjusted) | `beat_vol_target` | required |
| Percentile vs the random-selection null (liquidity-matched), at least | `random_entry_percentile_min` | 95 |
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
| **[portfolio]** Beats the equal-weight universe benchmark | `beat_equal_weight` | required |
| **[portfolio]** Alpha to BTC, HAC p-value at most | `alpha_p_max` | 0.05 |
| **[portfolio]** Annualised alpha to BTC, at least | `alpha_min_annual` | 0.0 |

Budget: 5 hypotheses, 15 proposals, 3000 backtests, 1 holdout check, $20.

Costs and frictions (lab config, fingerprinted): fees and slippage as `lab.toml`; square-root impact (coef 0.7, AUM $1M, 30-day ADV and vol, cap 5%); liquidity cap 10× liquidity share; rebalance offset pinned at 0.

## Pre-registered hypothesis and plan

H1: coins with the highest trailing ~4-week return keep outperforming the rest of the point-in-time universe over the next week (cross-sectional momentum), net of costs and impact, with alpha to BTC that survives a HAC test.

This is **one** pre-registered trial. The plan logic below is not refined after seeing results; if it fails any criterion the outcome is "no edge found".

```json
{"kind":"portfolio","name":"H1 cross-sectional momentum","interval":"1d",
 "score":{"trailing_return":{"lookback":28}},"rebalance":{"every":7},
 "selection":{"quantile":0.2,"exit_quantile":0.3,"min_names":3},
 "weighting":{"equal":{}},"max_weight":0.25}
```

- plan_hash `637d41af3914254d590288e00e0f1aeb76d713574bcc6e6ef48e60d3773c2538`
- plan_logic `portfolio|score=trailing_return|weighting=equal|banding=on|gate=off`
- No dispersion gate. 6 tunable params.

Sweep and walk-forward ranges (fixed now):

| param | low | high |
|---|---|---|
| `score.trailing_return.lookback` | 14 | 56 |
| `rebalance.every` | 7 | 14 |
| `selection.quantile` | 0.1 | 0.3 |
| `selection.exit_quantile` | 0.2 | 0.4 |
| `selection.min_names` | 3 | 5 |
| `max_weight` | 0.2 | 0.35 |

Trials: sweep 50 (lab default `n_trials`), walk-forward 50 per fold.

Splits: design window from 2019-01-01; locked holdout from 2025-10-01 (reachable only through `holdout_check`, at most once, only if `pre_holdout_passed`).

## Reading rules (fixed before any backtest)

- **Vol tilt.** If the liquidity-matched random-selection null passes, but the vol-only sidebar has p > 0.05 **and** the strategy's top-vol-tercile book share exceeds the null's by more than 0.05, the result is reported as "vol tilt not excluded", not as a clean momentum edge.
- **Null power.** On H1-length synthetic histories the liquidity-matched null detects a +19%/yr edge about 24/60 times (vol-only null: 30/60), and over-rejects a no-skill book about 2× when volatile coins carry a σ²/2 premium (9/80 vs ~4 nominal). The random-selection row may bind even for a genuine edge; that is accepted, not a reason to relax it.
- **Alpha/beta.** The report decomposes returns into beta and alpha to BTC (full sample and rolling 60-day, Newey-West HAC). If alpha is not significant after costs, the outcome is "no edge found" even if the raw Sharpe looks good.
- Weekday "timing luck" table is descriptive only.

## Log

- 2026-10-27 run started; criteria, budget, plan and ranges recorded above before the first backtest.
- [[trial-4417]] `run_backtest` of the pre-registered plan (design window, scored from 2019-01-03, 201 symbols ever members, 352 rebalances). Net CAGR 22.3%, net Sharpe 0.64 (gross 0.85), max drawdown −83.4%, annual turnover 23.3×; fees 67% and impact 112% of initial equity over the window. Benchmarks: equal-weight universe Sharpe 0.64, BTC buy-and-hold 1.12, BTC vol-target 1.16. Alpha to BTC −10.6%/yr (HAC t −0.63, p 0.53, 8 lags), beta 0.73, R² 0.52; rolling-60d beta 0.30–1.63 (median 0.72), rolling alpha median −22%/yr. Liquidity-matched random-selection null: 96th percentile (p 0.042, 500/500 paths, median Sharpe 0.45, rank gap 1.77, overlap 0.42 vs chance 0.26). Vol-only sidebar (descriptive): 99.8th percentile (p 0.004). Neighbourhood PBO 0.66 (21 trials). Regime returns: bull +338%, sideways −1.1%, bear −10.1%.
- `run_sweep`, 50 trials over the pre-registered ranges (1 failed, 1 pruned infeasible): best in-sample Sharpe 0.73 (lookback 20, every 11, quantile 0.11, exit 0.32, min_names 4, max_weight 0.32), biased upward. DSR N now 3217.
- `walk_forward` with the pre-registered ranges was **refused by the lab**: the backtest's ±20% neighbourhood had already tried `rebalance.every` 6 and `selection.min_names` 2, and walk-forward must cover every value the family tried. Deviation (lab-forced, in the stricter direction): walk-forward ranges widened to `rebalance.every` 6–14 and `selection.min_names` 2–5; all other ranges as pre-registered.
- `walk_forward`, 9 folds (2-year train, 6-month test), 50 trials per fold, 450 train trials (29 pruned as infeasible, exit_quantile below quantile; still ledgered): stitched out-of-sample net Sharpe **0.17**, CAGR −10.7%, total return −39.9%, max drawdown −86.6%, 186 filled rebalances. Test Sharpe by fold: 2021H1 1.26, 2021H2 1.29, 2022H1 −2.54, 2022H2 −0.91, 2023H1 0.06, 2023H2 1.77, 2024H1 −0.36, 2024H2 1.33, 2025H1 −0.40. The chosen lookback moves between 15 and 37 days; train Sharpe does not predict test Sharpe (fold 2: 1.90 → −2.54; fold 5: −0.18 → 1.77).
- [[trial-4951]] `run_adversarial`: delay of 1/3/5 rebalance periods → Sharpe 0.28 / 0.14 / 0.23 (gradual decay); cost stress ×2 → 0.42 (total return +53%), ×4 → −0.01 (−76%); timing luck (descriptive) other weekdays 0.43–0.62 vs offset 0 at 0.64.
- `run_crosscheck`: matched (independent accounting replay, max relative equity difference 1.7e-15, 3024 orders on both sides).
- `get_report`: `pre_holdout_passed = false`. **No holdout check spent.**
- `strategy-critic` (fresh context): **abandon**; see the report below.

## End-of-run report

**Outcome: no edge found.** The pre-registered H1 plan fails 9 of the 18 measured criteria, including the deciding one: alpha to BTC after costs is −10.6%/yr and not significant (HAC p 0.53). The holdout was not spent and remains available to a future, genuinely different hypothesis.

### Criteria (frozen at `start_run`)

| criterion | threshold | value | result |
|---|---|---|---|
| Walk-forward OOS Sharpe | ≥ 1 | 0.168 | fail |
| Deflated Sharpe (N = 3636) | ≥ 0.95 | 0.722 | fail |
| PBO | ≤ 0.2 | 0.657 (sweep CSCV 0.534) | fail |
| Beats BTC buy-and-hold | Sharpe | 0.64 vs 1.12 | fail |
| Beats vol-targeted BTC | Sharpe | 0.64 vs 1.16 | fail |
| Random-selection null (liquidity-matched) percentile | ≥ 95 | 96 (p 0.042) | pass |
| Positive at ×2 costs | > 0 | +52.7% | pass |
| Gradual delay decay | required | 0.64 → 0.28 → 0.14 → 0.23 | pass |
| Max drawdown | ≤ 0.25 | 0.834 | fail |
| Rebalances with fills | ≥ 100 | 352 | pass |
| Profitable regimes | ≥ 2 of 3 | 1 (bull +338%, sideways −1.1%, bear −10.1%) | fail |
| ±20% neighbourhood profitable | all | 20 of 20 | pass |
| Cross-check matched | required | yes | pass |
| Beats equal-weight universe | Sharpe | 0.6366 vs 0.6361 | pass (noise) |
| Alpha to BTC, HAC p | ≤ 0.05 | 0.531 | fail |
| Annualised alpha to BTC | ≥ 0 | −10.6% | fail |
| Locked holdout | pass | not spent | missing |

### Alpha and beta to BTC

OLS of net daily returns on BTC close-to-close returns from the first decision (n = 2463, Newey-West HAC with 8 lags): alpha **−10.6%/yr** (t −0.63, p 0.53), beta **0.73**, R² 0.52. A long-only top quintile mostly carries crypto beta, as expected.

Rolling 60-day: beta from 0.30 to 1.63 (median 0.72, last 1.50); annualised alpha from −281% to +413% (median −22%). Sampled points:

| date | alpha (ann.) | beta |
|---|---|---|
| 2019-03-03 | +1.03 | 1.09 |
| 2019-10-07 | −0.08 | 0.65 |
| 2020-05-13 | −0.41 | 0.50 |
| 2020-12-17 | −0.99 | 0.70 |
| 2021-07-24 | −1.24 | 0.67 |
| 2022-02-27 | −1.05 | 1.12 |
| 2022-10-04 | −0.39 | 0.54 |
| 2023-05-10 | −1.40 | 0.81 |
| 2023-12-15 | +1.09 | 0.88 |
| 2024-07-20 | −1.14 | 0.89 |
| 2025-02-24 | −0.95 | 1.23 |
| 2025-09-30 | +0.21 | 1.50 |

### Reading rules applied

- **Vol tilt (rule 1).** Not formally triggered: the vol-only sidebar p is 0.004, not above 0.05. The tilt is real all the same. The strategy's top-vol-tercile share is 0.60 by count (0.56 by weight) against the null's 0.39 (0.36). The null is known to over-reject about 2× when volatile coins carry a premium, so the 96th-percentile pass (p 0.042) is marginal on a test that is generous to exactly this tilt.
- **Null power (rule 2).** Noted. The random-selection row passed, so it was not the binding constraint here.
- **Alpha (rule 3).** Alpha is not significant after costs, so the outcome is "no edge found".

### What the evidence says

- The book is the liquid alt universe with a volatility tilt. Its Sharpe equals the equal-weight universe's to the third decimal (gap 0.0005 against a standard error near 0.39), and it loses to BTC and to vol-targeted BTC.
- Every positive walk-forward test half is an alt rally (2021, 2023H2, 2024H2). All of 2022, 2024H1 and 2025H1 lost. That pattern is lagged, leveraged alt beta, not a persistent cross-sectional effect, and it matches the February 2026 study cited in PLAN.md (alpha in 2020–22, none in 2023–24, losses in 2025–26).
- Costs matter but are not the whole story: gross Sharpe is only 0.85. Fees came to 67% and impact to 112% of initial equity over the window at 23× annual turnover.
- The dispersion terciles (descriptive, ex post cut points) show the strategy ahead of equal weight in low-dispersion periods and behind it in high-dispersion ones. A gate built from those cut points would be a threshold tuned after seeing results, which is forbidden, and would very likely share H1's family and holdout. It is **not** a follow-up.
- The passing rows (cross-check, neighbourhood, delay decay, random null) show the pipeline is sound. They do not show an edge.

### Lessons

- Long-only cross-sectional momentum on a top-30 liquid Binance universe (28-day score, weekly rebalance, banding, liquidity caps and impact) is alt beta with a vol tilt, not alpha.
- A narrow pass against a random-selection null does not overturn a failed alpha test. Read the alpha row first for long-only books.
- A genuinely new multi-coin hypothesis needs a different mechanism that these tables did not suggest (for example a non-price signal), pre-registered in a fresh run. It inherits DSR N ≥ 3636.

### Budget and ledger

Proposals 1 / 15, backtests 546 / 3000, holdout checks 0 / 1. DSR N across every run: 3636 distinct plans.

Results are historical simulations on Binance spot data and are not forward-looking. "No edge found" is the complete, correct outcome of this pre-registered trial.
