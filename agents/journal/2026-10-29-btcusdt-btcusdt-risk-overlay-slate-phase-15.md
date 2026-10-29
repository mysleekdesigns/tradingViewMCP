---
run_id: "r-20261029-173704-59d618"
date: 2026-10-29
question: risk_overlay
symbols:
  - "BTCUSDT"
interval: "1d+4h"
outcome: no_overlay_benefit
idea_ids: []
criteria_sha256: "f88ea5ff6ffbfa48a9ffd72d85ace51919f35a14cfdc387a443a53911a6772d2"
plan_hashes:
  - "def5121e230e2fd8073e9e15e1ba11e2b4b83e59f83684c0a702c009879a154d"
  - "c0059253b165df7fc9c75d5cbe56ca87e5c67f1e0711eea6a146416dfef1dd13"
  - "73b34c074fa1252f343ef3120bc1126461f685aedb9b03e004af932420ddc0d8"
tags:
  - research
  - phase-15
  - risk-overlay
---
# BTCUSDT risk-overlay slate (Phase 15)

Run [[run-r-20261029-173704-59d618]] · BTCUSDT 1d+4h · question `risk_overlay`

Holdout metrics are never written to this journal; only that a check was spent and the resulting status.

## Pre-registration (fixed before any backtest)

Everything in this section was written before the first backtest of this run and is never edited afterwards.

### Question

**Does one slow overlay on buy-and-hold cut drawdown, without a significant Sharpe loss, with a break-even cost of at least twice the base cost?**

A pass is reported as a *risk overlay*, never as an edge. The base is buy-and-hold of one symbol; a rule decided on bar t's close sets the exposure for bar t+1's open to 1 or 0. Overlays use their published parameters, untuned: `run_sweep` and `walk_forward` are refused in overlay runs, and each overlay logic may be tried with one published parameter set only, across every overlay run. Before anything is recorded the lab refuses another parameter set or a renamed copy of a submitted overlay logic, the same registry features with other parameters (or another interval) as an earlier overlay submission, and a logic whose parameters were already tried outside the overlay runs. A copy on other features is not refused; alike overlays (design-window returns correlated at or above `family_return_corr`) make `pbo_max` fail.

### Criteria (frozen at start_run)

Overlay criteria file sha256: `f88ea5ff6ffbfa48a9ffd72d85ace51919f35a14cfdc387a443a53911a6772d2`. These values were frozen into the ledger by `start_run` before the first backtest and are never relaxed mid-run.

| criterion | key | value |
|---|---|---|
| Overlay CDaR as a fraction of buy-and-hold's CDaR, at most | `cdar_vs_buy_hold_max` | 0.75 |
| Overlay CDaR below exposure-matched buy-and-hold's (paired bootstrap, p at most alpha / N_overlay) | `beat_exposure_matched` | required |
| One-sided lower bound of Sharpe(overlay) minus Sharpe(buy-and-hold), 365-day, paired bootstrap, at least minus this | `sharpe_non_inferiority` | 0.2 |
| Drawdown, episodes-improved and Sharpe rows still hold at this cost multiple | `break_even_cost_multiple_min` | 2 |
| Buy-and-hold drawdown episodes deeper than the episode depth, at least (else not testable) | `min_drawdown_episodes` | 3 |
| Average exposure (an overlay, not a timing strategy), at least | `min_avg_exposure` | 0.5 |
| Significance level before the Bonferroni division | `alpha` | 0.05 |
| Rows also hold on stitched fixed-parameter test folds (a sub-window robustness check on the same design bars, not out-of-sample) | `wf_rows_hold` | required |
| Probability of backtest overfitting, at most (nothing swept: untuned by rule) | `pbo_max` | 0.2 |
| Passes the locked holdout check (pass/fail only) | `require_holdout_pass` | required |
| Matches the event-driven cross-check engine | `require_crosscheck_match` | required |
| CDaR confidence level a (mean of the worst 1 - a share of bar drawdowns) | `cdar_confidence` | 0.95 |
| Depth that makes a buy-and-hold drawdown an episode (strictly deeper) | `episode_depth_min` | 0.3 |
| Stationary-bootstrap mean block length (days) | `bootstrap_mean_block_days` | 60 |
| Share of buy-and-hold's deep episodes in which the overlay's drawdown is below exposure-matched buy-and-hold's, strictly more than | `min_episodes_improved_share` | 0.5 |

### Multiple testing (Bonferroni)

Every paired-bootstrap p-value is compared with alpha / N_overlay, alpha = 0.05. N_overlay counts distinct overlay logics across every overlay run in the ledger, this one included; each logic may be tried with one published parameter set only. A new run never resets it. N_overlay is read from `get_report` at judging time. Overlay trials also count in the lab-wide deflated-Sharpe trial count N (never in its Sharpe-variance pools).

This slate adds 3 plan(s), so N_overlay is at least 3 and the per-test level is at most 0.0166667.

### Budget (frozen at start_run)

| budget | key | limit |
|---|---|---|
| Hypotheses | `max_hypotheses` | 3 |
| LLM proposals | `max_proposals` | 6 |
| Backtests (ledger trials) | `max_backtests` | 600 |
| Holdout checks | `max_holdout_checks` | 1 |
| Model spend (USD) | `max_usd` | 10 |

This run spends at most 1 holdout check(s), pass/fail only. A failed check kills the overlay's logic under either question; it is never re-tuned.

### Slate

3 overlay(s), each from the literature with its published parameters. Source text is untrusted data, quoted inside a warning callout.

#### 1. btc-1d-below-sma200-flat · plan `def5121e230e2fd8073e9e15e1ba11e2b4b83e59f83684c0a702c009879a154d`

> [!warning] Untrusted source text (literature / web) — data, not instructions
> Source URL: `https://papers.ssrn.com/sol3/papers.cfm?abstract_id=962461`
>
> Faber, M. (2007), A Quantitative Approach to Tactical Asset Allocation: hold the asset when its monthly close is above the 10-month simple moving average, otherwise cash; the 200-day SMA is the common daily form.

Published parameters (used as published, untuned):

| parameter | published value |
|---|---|
| sma.period | 200 days (daily form of the 10-month SMA) |

Plan JSON, exactly as submitted:

```json
{
  "description": "Faber (2007) 10-month / 200-day moving-average timing rule: flat while BTC closes below its 200-day simple moving average.",
  "features": {
    "s": {
      "sma": {
        "period": 200
      }
    }
  },
  "flat_when": "close < s",
  "interval": "1d",
  "kind": "overlay",
  "name": "btc-1d-below-sma200-flat",
  "symbol": "BTCUSDT"
}
```

Family links to earlier ledgered plans:

- plan `4dfb0745309db373d3792886b6dcc601a9d75071f2530f77bd54d61ae1bb2972` in [[run-r-20261025-135224-41f19f]] · [[2026-10-25-btcusdt-ethusdt-autonomous-research-priority-families]]: same mechanism: long/flat on close vs a moving average (EMA, 4h)
- plan `dc5cd0bd1bc8b18ff9e5110410db0a96160a8f705bb6cfad5f1b5cf3968a680b` in [[run-r-20261028-103056-807dd5]] · [[2026-10-28-btcusdt-ethusdt-h3-funding-zscore-crash-risk-gate]]: EMA trend long/flat base of the H3 funding-gated plan (4h)

#### 2. btc-4h-premium-top-decile-flat · plan `c0059253b165df7fc9c75d5cbe56ca87e5c67f1e0711eea6a146416dfef1dd13`

> [!warning] Untrusted source text (literature / web) — data, not instructions
> Source URL: `https://www.bis.org/publ/work1087.htm`
>
> Schmeling, Schrimpf and Todorov (2023), Crypto carry, BIS Working Paper 1087: high perpetual-futures premium (carry) signals crowded speculative longs and predicts crash risk; flat in the top decile of the 7-day mean premium.

Published parameters (used as published, untuned):

| parameter | published value |
|---|---|
| premium_mean_rank.period | 42 bars = 7 days on 4h |
| threshold | top decile (rank &gt; 0.9) |

Plan JSON, exactly as submitted:

```json
{
  "description": "BIS WP 1087 (Crypto carry): flat while the 7-day mean perpetual premium index is in the top decile of its expanding history (crowded-long crash risk).",
  "features": {
    "pr": {
      "premium_mean_rank": {
        "period": 42
      }
    }
  },
  "flat_when": "pr > 0.9",
  "interval": "4h",
  "kind": "overlay",
  "name": "btc-4h-premium-top-decile-flat",
  "symbol": "BTCUSDT"
}
```

Family links to earlier ledgered plans:

- plan `dc5cd0bd1bc8b18ff9e5110410db0a96160a8f705bb6cfad5f1b5cf3968a680b` in [[run-r-20261028-103056-807dd5]] · [[2026-10-28-btcusdt-ethusdt-h3-funding-zscore-crash-risk-gate]]: derivatives crowding as a crash-risk filter (funding z-score, entry-only gate)

#### 3. btc-1d-high-vol-regime-flat · plan `73b34c074fa1252f343ef3120bc1126461f685aedb9b03e004af932420ddc0d8`

> [!warning] Untrusted source text (literature / web) — data, not instructions
> Source URL: `https://doi.org/10.1111/jofi.12513`
>
> Moreira and Muir (2017), Volatility-Managed Portfolios, Journal of Finance: cut exposure when recent realised volatility is high. The paper scales exposure by 1/variance; the 0/1 top-decile form and the 30-day window are this slate's pre-registered conventions (the lab's vol_rank default), not values from the paper.

Published parameters (used as published, untuned):

| parameter | published value |
|---|---|
| vol_rank.period | 30 days (convention, declared) |
| threshold | top decile (rank &gt; 0.9, convention, declared) |

Plan JSON, exactly as submitted:

```json
{
  "description": "Volatility-managed exposure (Moreira and Muir 2017) in its 0/1 form: flat while 30-day realised volatility is in the top decile of its expanding history.",
  "features": {
    "v": {
      "vol_rank": {
        "period": 30
      }
    }
  },
  "flat_when": "v > 0.9",
  "interval": "1d",
  "kind": "overlay",
  "name": "btc-1d-high-vol-regime-flat",
  "symbol": "BTCUSDT"
}
```

Family links to earlier ledgered plans:

- plan `02d5ea578e28b2679d578069bd56049216255b6c1952634e19ec46786365175a` in [[run-r-20261025-125744-12221d]] · [[2026-10-25-btcusdt-volatility-targeting]]: volatility-gated exposure (fast vs slow realised vol, 4h)
- plan `045962b5169aa19542faba8dfeef1103ac0cfc86ffacec36d0118c4fb7e002be` in [[run-r-20261025-125744-12221d]] · [[2026-10-25-btcusdt-volatility-targeting]]: volatility-gated entry with trend exit (4h)

### Seen-data declaration

The design window (2019-01 to 2025-10) has been seen. Edge runs on 2026-10-25 and 2026-10-26 already examined trend filters, volatility gating and regime filters on it, so every overlay here comes from the literature with its published parameters, untuned, and declares its family links to earlier ledgered plans. The locked holdout (from 2025-10-01) is unspent; it and the forward paper test are the real tests.
  
  Unledgered development probes (disclosed before the first backtest): on 2026-10-29, during the Phase 15 build and its quant review, the lab's overlay statistics were computed on the real BTCUSDT 1d design window outside the ledger for two slate members exactly as submitted here: btc-1d-below-sma200-flat (sma 200, close &lt; s) and btc-1d-high-vol-regime-flat (vol_rank 30, v &gt; 0.9). They showed: sma200 exposure about 0.63, CDaR95 overlay 0.663 vs buy-and-hold 0.733 vs exposure-matched 0.543 (so it already looked like it fails the exposure-matched comparison), Sharpe about 0.94 vs 1.12, and it improved on exposure-matched buy-and-hold in 1 of 3 deep episodes; vol_rank improved in 0 of 3 and is flat on about 1.3% of bars; the stitched walk-forward folds hold only 2 deep buy-and-hold episodes. A review script may also have run 4h variants (sma 1200, vol_rank 180); this is not confirmed, and neither variant is on the slate. The premium overlay was not probed. The slate and its intervals were fixed in PLAN.md (commit 7680942) before these probes and are not changed because of them: every probed configuration is submitted exactly as probed, so it is ledgered and counted in N_overlay. The fold-check rule was kept strict by the owner after the probes (no loosening).

### Traps this design closes

- **Holding less always cuts drawdown.** 50% BTC has about half of BTC's drawdown and the same Sharpe. Each overlay is compared with buy-and-hold scaled to the overlay's own average exposure (a constant mix rebalanced every bar at zero cost, which favours the benchmark) as well as with plain buy-and-hold. Beating plain buy-and-hold on drawdown proves nothing by itself.
- **Max drawdown is one number from one path.** Rows use CDaR (the mean of the worst tail of bar drawdowns) and paired stationary-bootstrap tests. If buy-and-hold has fewer than `min_drawdown_episodes` separate drawdowns deeper than `episode_depth_min` in the scored window, the result is **not testable**, and not testable is never a pass. Each deep episode is also compared one by one: the overlay's drawdown inside it (from its own equity at the episode peak) must be below exposure-matched buy-and-hold's in strictly more than `min_episodes_improved_share` of them.
- **The walk-forward row is not out-of-sample.** `wf_rows_hold` is a sub-window robustness check on stitched fixed-parameter test folds (same design bars, not out-of-sample). Its drawdown rows are judged only when the folds themselves hold at least `min_drawdown_episodes` deep episodes; otherwise they are not testable there, and not testable is never a pass.
- **Sharpe non-inferiority** is judged on the one-sided bootstrap lower bound, not on the point estimate, and the **break-even cost** row asks that the drawdown, episodes-improved and Sharpe rows still hold at `break_even_cost_multiple_min` times the base costs.
- **The design window has been seen** (declaration above), so parameters are the published ones and each overlay declares its family links.

### Outcome vocabulary

- `candidate_overlay`: passed every overlay row and the locked check; still needs 4-8 weeks of `freqtrade --dry-run` before it is called a working risk overlay.
- `no_overlay_benefit`: no overlay passed every row (a correct, complete outcome).
- `not_testable`: too few drawdown episodes to judge; never a pass.
- `budget_exhausted`, `error`, `stopped`: partial; no conclusion either way.

## Log

- 2026-10-29T17:44:45Z **backtest** — sma200 1d: exposure 0.631, CDaR95 0.663 vs buy-and-hold 0.733 vs exposure-matched 0.543; Sharpe 0.940 vs 1.117 (lower bound of the gap -0.604); 1 of 3 deep episodes improved; 27 trades · [[trial-5588]] [[run-r-20261029-173704-59d618]] plan `def5121e230e2fd8073e9e15e1ba11e2b4b83e59f83684c0a702c009879a154d`
- 2026-10-29T17:44:45Z **backtest** — premium 4h (scored from 2020-10-01): exposure 0.952, CDaR ratio 1.000, CDaR 0.745 vs exposure-matched 0.724; Sharpe 1.018 vs 1.083 (lower bound -0.210); 1 of 4 episodes improved; 11 trades · [[trial-5605]] [[run-r-20261029-173704-59d618]] plan `c0059253b165df7fc9c75d5cbe56ca87e5c67f1e0711eea6a146416dfef1dd13`
- 2026-10-29T17:44:45Z **backtest** — vol_rank 1d: exposure 0.987, CDaR ratio 1.000, CDaR 0.733 vs exposure-matched 0.727; Sharpe 1.036 vs 1.117 (lower bound -0.214); 0 of 3 episodes improved; 3 trades · [[trial-5619]] [[run-r-20261029-173704-59d618]] plan `73b34c074fa1252f343ef3120bc1126461f685aedb9b03e004af932420ddc0d8`
- 2026-10-29T17:44:45Z **adversarial** — permutation p 0.287 / 0.634 / 0.842 (sma200 / premium / vol_rank): no timing information; sign flips blew up or lost; costs x4 Sharpe 0.72 / 0.98 / 1.02; delay flags expected for slow filters · [[trial-5636]] [[trial-5644]] [[trial-5652]] [[run-r-20261029-173704-59d618]] plan `def5121e230e2fd8073e9e15e1ba11e2b4b83e59f83684c0a702c009879a154d` plan `c0059253b165df7fc9c75d5cbe56ca87e5c67f1e0711eea6a146416dfef1dd13` plan `73b34c074fa1252f343ef3120bc1126461f685aedb9b03e004af932420ddc0d8`
- 2026-10-29T17:44:45Z **crosscheck** — event-driven engine matched all three (27/27, 11/11, 3/3 trades; return gap &lt; 2e-6 %) · plan `def5121e230e2fd8073e9e15e1ba11e2b4b83e59f83684c0a702c009879a154d` plan `c0059253b165df7fc9c75d5cbe56ca87e5c67f1e0711eea6a146416dfef1dd13` plan `73b34c074fa1252f343ef3120bc1126461f685aedb9b03e004af932420ddc0d8`
- 2026-10-29T17:44:45Z **critic** — strategy-critic (fresh context): abandon all three; outcome no_overlay_benefit; holdout not warranted (pre_holdout_passed false for every plan; the one budgeted check stays unspent) · plan `def5121e230e2fd8073e9e15e1ba11e2b4b83e59f83684c0a702c009879a154d` plan `c0059253b165df7fc9c75d5cbe56ca87e5c67f1e0711eea6a146416dfef1dd13` plan `73b34c074fa1252f343ef3120bc1126461f685aedb9b03e004af932420ddc0d8`

## End-of-run report

Run [[run-r-20261029-173704-59d618]] · question `risk_overlay`

### Outcome

**No overlay benefit found.** No overlay in this run passed every pre-registered overlay row. This is a correct, complete outcome; the criteria and budget were not relaxed.

### N_overlay

N_overlay = 3: distinct overlay logics across every overlay run (each logic may be tried with one published parameter set only); each p-value was compared with alpha / N_overlay.

### Overlay rows

#### plan `def5121e230e2fd8073e9e15e1ba11e2b4b83e59f83684c0a702c009879a154d`

| criterion | status | value | threshold | note |
|---|---|---|---|---|
| cdar_vs_buy_hold_max | fail | 0.9056 | 0.75 | design window: CDaR at 95% overlay 0.6634 vs buy-and-hold 0.7326 at base costs; ratio 0.9056 must be &lt;= 0.75 |
| beat_exposure_matched | fail | 0.8661 | 0.01667 | design window: CDaR overlay - CDaR exposure-matched buy-and-hold 0.1203 (bootstrap bias 0.03425; matched CDaR 0.5431 at average exposure 0.6315); needs a point below 0 and paired-bootstrap p 0.8661 &lt;= alpha / N_overlay = 0.01667 |
| min_episodes_improved | fail | 0.3333 | 0.5 | design window: the overlay's max drawdown inside the episode (from its own equity at the peak) is below exposure-matched buy-and-hold's in 1 of 3 buy-and-hold episode(s) deeper than 30%; needs a share strictly above 0.5 (2 of 3) |
| sharpe_non_inferiority | fail | -0.6036 | -0.2 | design window: Sharpe overlay 0.9402 - buy-and-hold 1.117 = -0.1766 (365-day annualised; bootstrap bias -0.006465); one-sided 95% lower bound -0.6036 must be &gt;= -0.2, and the non-inferiority p 0.4725 (share of resampled gaps &lt;= -0.2) must be &lt;= alpha / N_overlay = 0.01667 (divided like the drawdown test: a false pass needs only one of the two to be wrong) |
| break_even_cost_multiple_min | fail | — | 2 | design window: largest cost multiple up to which the CDaR, exposure-matched, episodes-improved and Sharpe rows all hold = n/a (needs &gt;= 2); 1x fails, 1.5x fails, 2x fails, 3x fails, 4x fails |
| min_drawdown_episodes | pass | 3 | 3 | buy-and-hold has 3 drawdown episode(s) (peak to recovery) deeper than 30% in the scored window; at least 3 are needed to compare drawdowns (fewer = not_testable, never a pass) |
| min_avg_exposure | pass | 0.6315 | 0.5 | design window: invested on 63.1% of the bars after the first decision (an overlay must hold the coin at least 50% of the time) |
| wf_rows_hold | fail | no | yes | sub-window robustness check on stitched fixed-parameter test folds (same design bars, not out-of-sample): the rows on 9 stitched fixed-parameter walk-forward test fold(s) (1642 bars; nothing tuned; 2 buy-and-hold episode(s) deeper than 30% in the stitched folds, 3 needed to judge the episode-based rows there). Judged: sharpe_non_inferiority, min_avg_exposure; not_testable: cdar_vs_buy_hold_max, beat_exposure_matched, min_episodes_improved, break_even_cost_multiple_min (not_testable is never a pass). cdar_vs_buy_hold_max not_testable (0.8895); beat_exposure_matched not_testable (0.8412); min_episodes_improved not_testable (n/a); sharpe_non_inferiority fail (-0.8528); break_even_cost_multiple_min not_testable (n/a); min_avg_exposure pass (0.6038) |
| pbo_max | pass | — | 0.2 | nothing swept: overlays are untuned by rule (run_sweep and walk_forward are refused in risk-overlay runs, and the plan's behaviour family holds no other submitted overlay), so no parameter was selected in-sample |
| require_holdout_pass | missing | — | — | holdout metrics are never recorded in the journal |
| require_crosscheck_match | pass | yes | yes | event-driven cross-check: matched = True; 27 of 27 vectorbt trades matched by 27 event-driven trades, return diff 3.93e-07% |

#### plan `c0059253b165df7fc9c75d5cbe56ca87e5c67f1e0711eea6a146416dfef1dd13`

| criterion | status | value | threshold | note |
|---|---|---|---|---|
| cdar_vs_buy_hold_max | fail | 1 | 0.75 | design window: CDaR at 95% overlay 0.7446 vs buy-and-hold 0.7446 at base costs; ratio 1 must be &lt;= 0.75 |
| beat_exposure_matched | fail | 0.7413 | 0.01667 | design window: CDaR overlay - CDaR exposure-matched buy-and-hold 0.02007 (bootstrap bias -0.01022; matched CDaR 0.7245 at average exposure 0.9516); needs a point below 0 and paired-bootstrap p 0.7413 &lt;= alpha / N_overlay = 0.01667 |
| min_episodes_improved | fail | 0.25 | 0.5 | design window: the overlay's max drawdown inside the episode (from its own equity at the peak) is below exposure-matched buy-and-hold's in 1 of 4 buy-and-hold episode(s) deeper than 30%; needs a share strictly above 0.5 (3 of 4) |
| sharpe_non_inferiority | fail | -0.2103 | -0.2 | design window: Sharpe overlay 1.018 - buy-and-hold 1.083 = -0.06538 (365-day annualised; bootstrap bias 0.01183); one-sided 95% lower bound -0.2103 must be &gt;= -0.2, and the non-inferiority p 0.06194 (share of resampled gaps &lt;= -0.2) must be &lt;= alpha / N_overlay = 0.01667 (divided like the drawdown test: a false pass needs only one of the two to be wrong) |
| break_even_cost_multiple_min | fail | — | 2 | design window: largest cost multiple up to which the CDaR, exposure-matched, episodes-improved and Sharpe rows all hold = n/a (needs &gt;= 2); 1x fails, 1.5x fails, 2x fails, 3x fails, 4x fails |
| min_drawdown_episodes | pass | 4 | 3 | buy-and-hold has 4 drawdown episode(s) (peak to recovery) deeper than 30% in the scored window; at least 3 are needed to compare drawdowns (fewer = not_testable, never a pass) |
| min_avg_exposure | pass | 0.9516 | 0.5 | design window: invested on 95.2% of the bars after the first decision (an overlay must hold the coin at least 50% of the time) |
| wf_rows_hold | fail | no | yes | sub-window robustness check on stitched fixed-parameter test folds (same design bars, not out-of-sample): the rows on 6 stitched fixed-parameter walk-forward test fold(s) (6576 bars; nothing tuned; 1 buy-and-hold episode(s) deeper than 30% in the stitched folds, 3 needed to judge the episode-based rows there). Judged: sharpe_non_inferiority, min_avg_exposure; not_testable: cdar_vs_buy_hold_max, beat_exposure_matched, min_episodes_improved, break_even_cost_multiple_min (not_testable is never a pass). cdar_vs_buy_hold_max not_testable (0.998); beat_exposure_matched not_testable (0.954); min_episodes_improved not_testable (n/a); sharpe_non_inferiority fail (-0.2003); break_even_cost_multiple_min not_testable (n/a); min_avg_exposure pass (0.9802) |
| pbo_max | fail | — | 0.2 | 2 alike overlays submitted: 73b34c074fa1, c0059253b165 (the plan's behaviour family across every overlay run: the same logic, or design-window returns correlated at or above family_return_corr). Choosing among alike overlays is a selection in-sample that no PBO here measures, so this row fails |
| require_holdout_pass | missing | — | — | holdout metrics are never recorded in the journal |
| require_crosscheck_match | pass | yes | yes | event-driven cross-check: matched = True; 11 of 11 vectorbt trades matched by 11 event-driven trades, return diff 1.37e-06% |

#### plan `73b34c074fa1252f343ef3120bc1126461f685aedb9b03e004af932420ddc0d8`

| criterion | status | value | threshold | note |
|---|---|---|---|---|
| cdar_vs_buy_hold_max | fail | 1 | 0.75 | design window: CDaR at 95% overlay 0.7326 vs buy-and-hold 0.7326 at base costs; ratio 1 must be &lt;= 0.75 |
| beat_exposure_matched | fail | 0.995 | 0.01667 | design window: CDaR overlay - CDaR exposure-matched buy-and-hold 0.005368 (bootstrap bias 0.04221; matched CDaR 0.7272 at average exposure 0.987); needs a point below 0 and paired-bootstrap p 0.995 &lt;= alpha / N_overlay = 0.01667 |
| min_episodes_improved | fail | 0 | 0.5 | design window: the overlay's max drawdown inside the episode (from its own equity at the peak) is below exposure-matched buy-and-hold's in 0 of 3 buy-and-hold episode(s) deeper than 30%; needs a share strictly above 0.5 (2 of 3) |
| sharpe_non_inferiority | fail | -0.2141 | -0.2 | design window: Sharpe overlay 1.036 - buy-and-hold 1.117 = -0.0806 (365-day annualised; bootstrap bias 0.0006295); one-sided 95% lower bound -0.2141 must be &gt;= -0.2, and the non-inferiority p 0.06893 (share of resampled gaps &lt;= -0.2) must be &lt;= alpha / N_overlay = 0.01667 (divided like the drawdown test: a false pass needs only one of the two to be wrong) |
| break_even_cost_multiple_min | fail | — | 2 | design window: largest cost multiple up to which the CDaR, exposure-matched, episodes-improved and Sharpe rows all hold = n/a (needs &gt;= 2); 1x fails, 1.5x fails, 2x fails, 3x fails, 4x fails |
| min_drawdown_episodes | pass | 3 | 3 | buy-and-hold has 3 drawdown episode(s) (peak to recovery) deeper than 30% in the scored window; at least 3 are needed to compare drawdowns (fewer = not_testable, never a pass) |
| min_avg_exposure | pass | 0.987 | 0.5 | design window: invested on 98.7% of the bars after the first decision (an overlay must hold the coin at least 50% of the time) |
| wf_rows_hold | not testable (never a pass) | no | yes | sub-window robustness check on stitched fixed-parameter test folds (same design bars, not out-of-sample): the rows on 9 stitched fixed-parameter walk-forward test fold(s) (1642 bars; nothing tuned; 2 buy-and-hold episode(s) deeper than 30% in the stitched folds, 3 needed to judge the episode-based rows there). Judged: sharpe_non_inferiority, min_avg_exposure; not_testable: cdar_vs_buy_hold_max, beat_exposure_matched, min_episodes_improved, break_even_cost_multiple_min (not_testable is never a pass). cdar_vs_buy_hold_max not_testable (1); beat_exposure_matched not_testable (0.995); min_episodes_improved not_testable (n/a); sharpe_non_inferiority pass (-0.009904); break_even_cost_multiple_min not_testable (n/a); min_avg_exposure pass (0.9988) |
| pbo_max | fail | — | 0.2 | 2 alike overlays submitted: 73b34c074fa1, c0059253b165 (the plan's behaviour family across every overlay run: the same logic, or design-window returns correlated at or above family_return_corr). Choosing among alike overlays is a selection in-sample that no PBO here measures, so this row fails |
| require_holdout_pass | missing | — | — | holdout metrics are never recorded in the journal |
| require_crosscheck_match | pass | yes | yes | event-driven cross-check: matched = True; 3 of 3 vectorbt trades matched by 3 event-driven trades, return diff 5.93e-07% |


### Notes

- Outcome: no overlay benefit. All three overlays were testable on the full window (3, 4 and 3 buy-and-hold drawdowns deeper than 30%) and fail every substantive row at base costs.
- No plan qualified for the locked check (pre-check criteria failed for every plan), so it was not used.
- N_overlay is 3 (alpha / N_overlay = 0.0167).
- The exposure-matched comparison is decisive. The only overlay that materially changes exposure, the 200-day SMA (63% invested), cuts CDaR95 to 0.906 of buy-and-hold, while simply holding 63% BTC cuts it to 0.741; the timing rule is 12 CDaR points worse than static de-risking and costs 0.18 Sharpe. Its one improved episode is the 2021-23 bear (0.369 vs 0.578); in 2019-20 and 2021 whipsaw left it worse than the static mix (0.546 vs 0.431, 0.624 vs 0.364; in 2021 deeper than buy-and-hold's own 0.531, which with 0/1 exposure means invested on down legs and flat on rebounds).
- The premium (11 trades) and vol_rank (3 trades) gates almost never fire: CDaR ratio 1.000, they were buy-and-hold under another name, and their near-pass on Sharpe non-inferiority only reflects that. Their pbo_max fail ('2 alike overlays') is a beta artefact: two 95-99% invested overlays correlate through buy-and-hold above family_return_corr whatever their logic. It fails closed and changes no verdict; any two high-exposure overlays will be classed alike (and share one locked check).
- Post-hoc disclosures (the pre-registration above is not edited): (1) the sma200 overlay's family links omit the 2026-10-26 Markov regime long/flat filter, \[\[idea-13\]\], plan `ee653bdb90cc58bd72211e34a007e2b18e78a1db7c047f8b3a3e55f80e5d98cf`, which that run placed in the trend-filter family; (2) vol_rank's 30-day window and 0/1 top-decile form are declared conventions in the source text but sit under the 'published parameters' header, so this result tests the lab's 0/1 convention, not Moreira-Muir's continuous 1/variance scaling, and must not be read as 'volatility management fails on BTC'; (3) the 42-bar / top-decile form of the premium rule was pre-committed in PLAN.md and attributed to BIS WP 1087, but the critic could not confirm those exact parameters or the Binance premium index as the paper's carry measure, so treat them as a translation.
- Unledgered development probes were disclosed before the first backtest; the ledgered numbers match them (sma200 exposure 0.63, CDaR 0.663 / 0.733 / 0.543, 1 of 3 episodes; vol_rank 0 of 3). The slate predates them (PLAN.md, 7680942), nothing was dropped or changed, and no rule was loosened.
- Lesson: on BTC 2019-2025 the only 0/1 overlay that materially changed exposure had worse drawdowns than simply holding the same share of BTC, and the crash-risk top-decile gates were buy-and-hold under another name. Compare any crash-avoidance claim with static de-risking at the same exposure before believing it. With only 3-4 deep episodes in this window, more slate members would mostly lower alpha / N_overlay without adding power; the 3 unused proposals stay unused.
- Results are historical and not forward-looking.

### Disclaimer

Backtests are historical, not forward-looking. A risk overlay is a drawdown result measured against exposure-matched buy-and-hold, not a return forecast.
