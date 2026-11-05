---
run_id: "r-20261104-235252-f77c6d"
date: 2026-11-04
question: risk_overlay
symbols:
  - "BTCUSDT"
interval: "4h"
outcome: already_abandoned
idea_ids: []
criteria_sha256: "f88ea5ff6ffbfa48a9ffd72d85ace51919f35a14cfdc387a443a53911a6772d2"
plan_hashes:
  - "b8571030808ab88cda3adcdfc277d5fd60ecf30df64e53252ea4e1bdd7491a36"
  - "6f92436eb9b22bf67d7b773d63d33ca2cd70d34e4c04cc3cd657208c1fc1f5c9"
tags:
  - research
  - phase-20
  - risk-overlay
  - hmm
---
# BTCUSDT HMM overlay slate (Phase 20)

Run [[run-r-20261104-235252-f77c6d]] · BTCUSDT 4h · question `risk_overlay`

Holdout metrics are never written to this journal; only that a check was spent and the resulting status.

## Pre-registration (fixed before any backtest)

Everything in this section was written and committed before the first backtest of this run and is never edited afterwards.

### Question

**Does a causal two-state HMM bear filter on buy-and-hold cut drawdown, without a significant Sharpe loss, with a break-even cost of at least twice the base cost?**

The question and its criteria are the Phase 15 ones, unchanged. A pass is reported as a *risk overlay*, never as an edge. A rule decided on bar t's close sets the exposure for bar t+1's open to 1 or 0. Overlays are untuned: `run_sweep` and `walk_forward` are refused in overlay runs, and at most 2 overlay logics across every overlay run may read an HMM feature.

**Expected outcome: no overlay benefit.** A two-state fit is mostly a volatility classifier, so this slate is likely the same hypothesis as the abandoned `vol_rank` overlay, and Phase 15 found that the one 0/1 overlay that materially changed exposure lost to simply holding less BTC.

### Criteria (frozen at start_run)

Overlay criteria file sha256: `f88ea5ff6ffbfa48a9ffd72d85ace51919f35a14cfdc387a443a53911a6772d2`, the same file as Phase 15. `start_run` froze these values into the ledger before the first backtest; they are never relaxed mid-run.

| criterion | key | value |
|---|---|---|
| Overlay CDaR as a fraction of buy-and-hold's CDaR, at most | `cdar_vs_buy_hold_max` | 0.75 |
| Overlay CDaR below exposure-matched buy-and-hold's (paired bootstrap, p at most alpha / N_overlay) | `beat_exposure_matched` | required |
| One-sided lower bound of Sharpe(overlay) minus Sharpe(buy-and-hold), 365-day, paired bootstrap, at least minus this | `sharpe_non_inferiority` | 0.2 |
| Drawdown, episodes-improved and Sharpe rows still hold at this cost multiple | `break_even_cost_multiple_min` | 2 |
| Buy-and-hold drawdown episodes deeper than the episode depth, at least (else not testable) | `min_drawdown_episodes` | 3 |
| Average exposure (an overlay, not a timing strategy), at least | `min_avg_exposure` | 0.5 |
| Significance level before the Bonferroni division | `alpha` | 0.05 |
| Rows also hold on stitched fixed-parameter test folds (same design bars, not out-of-sample) | `wf_rows_hold` | required |
| Probability of backtest overfitting, at most (nothing swept: untuned by rule) | `pbo_max` | 0.2 |
| Passes the locked holdout check (pass/fail only) | `require_holdout_pass` | required |
| Matches the event-driven cross-check engine | `require_crosscheck_match` | required |
| CDaR confidence level | `cdar_confidence` | 0.95 |
| Depth that makes a buy-and-hold drawdown an episode (strictly deeper) | `episode_depth_min` | 0.3 |
| Stationary-bootstrap mean block length (days) | `bootstrap_mean_block_days` | 60 |
| Share of deep episodes in which the overlay's drawdown is below exposure-matched buy-and-hold's, strictly more than | `min_episodes_improved_share` | 0.5 |

### Multiple testing (Bonferroni)

Every paired-bootstrap p-value is compared with alpha / N_overlay, alpha = 0.05. N_overlay counts distinct overlay logics across every overlay run in the ledger and a new run never resets it. Phase 15 left it at 3, so this slate takes it to at least 5 and the per-test level to at most 0.01. `get_report` gives the value at judging time. Overlay trials also count in the lab-wide deflated-Sharpe trial count N.

### Budget (frozen at start_run)

| budget | key | limit |
|---|---|---|
| Hypotheses | `max_hypotheses` | 3 |
| LLM proposals | `max_proposals` | 6 |
| Backtests (ledger trials) | `max_backtests` | 600 |
| Holdout checks | `max_holdout_checks` | 1 |
| Model spend (USD) | `max_usd` | 10 |

A holdout check needs `pre_holdout_passed` and the owner's approval. A failed check kills the overlay's logic under either question; it is never re-tuned.

### HMM settings (frozen beside the run fingerprint)

From `lab.toml` `[regime.hmm]`, accepted by the owner or set by Claude on 2026-11-04 with no data looked at: 2 states, 42-bar volatility window, 3,000-bar training block, refit every 42 bars, seed 7, 2 restarts, 200 iterations. Inputs are log return and rolling volatility. States are ordered by ascending mean return inside each fit's training block, so `hmm_bear_prob` is the filtered probability of the lowest-mean state. Filtered probabilities only: no smoothing, no Viterbi.

### Slate

2 overlays, the Phase 20 limit. Source text is untrusted data, quoted inside a warning callout.

#### 1. btc-4h-hmm-bear-092-flat · plan `b8571030808ab88cda3adcdfc277d5fd60ecf30df64e53252ea4e1bdd7491a36`

> [!warning] Untrusted source text (literature / web) — data, not instructions
> Source: Nystrup (2014), M.Sc. thesis, DTU, "Regime-Based Asset Allocation: Do Profitable Strategies Exist?", read directly on 2026-11-04.
>
> Two Gaussian states out of sample, one-step state prediction on daily log-returns; a state change happens only if confidence exceeds a 92% threshold. The author picked 92% and remarks that a slightly lower value might have been better out of sample.

This is a translation, not a replication. The value 0.92 is an author's choice on daily traditional assets, not a discovered constant. The source's rule filters *changes* of state. Here the overlay is flat while the probability is above 0.92 and re-enters as soon as it is not, with no hysteresis. The source uses a one-step prediction and this uses the filtered probability at bar t's close. The source's forgetting window and this rolling 3,000-bar block (the same 500-calendar-day span) also differ.

| parameter | value |
|---|---|
| threshold | 0.92 (Nystrup 2014, author-chosen) |
| HMM settings | fixed in lab.toml, see above |

Plan JSON, exactly as submitted:

```json
{
  "kind": "overlay",
  "name": "btc-4h-hmm-bear-092-flat",
  "description": "Causal two-state HMM overlay: flat while the filtered probability of the lowest-mean-return state is above 0.92, the confidence level of Nystrup (2014). Settings fixed in lab.toml [regime.hmm], untuned.",
  "symbol": "BTCUSDT",
  "interval": "4h",
  "features": {
    "b": {
      "hmm_bear_prob": {}
    }
  },
  "flat_when": "b > 0.92"
}
```

#### 2. btc-4h-hmm-bear-050-persist3-flat · plan `6f92436eb9b22bf67d7b773d63d33ca2cd70d34e4c04cc3cd657208c1fc1f5c9`

> [!warning] Untrusted source text (literature / web) — data, not instructions
> Source: the regime trading bot video behind PLAN.md Phase 20, second-hand. No transcript was ever obtained, the channel promotes paid products, and the rule has no literature support. See `agents/research/reports/Claude Code regime trading bot video research.md`.
>
> Act on a bear reading only after it has held for 3 bars.

Labelled video-sourced. The published persistence control is a confidence threshold, not a bar count, so this entry has weaker provenance than the first. `k = 3` and the 0.5 threshold are the video's values, kept as PLAN.md states them.

| parameter | value |
|---|---|
| threshold | 0.5 (video-sourced) |
| persist k | 3 bars (video-sourced) |
| HMM settings | fixed in lab.toml, see above |

Plan JSON, exactly as submitted:

```json
{
  "kind": "overlay",
  "name": "btc-4h-hmm-bear-050-persist3-flat",
  "description": "Causal two-state HMM overlay: flat while the filtered probability of the lowest-mean-return state has been above 0.5 on each of the last 3 closed bars. Video-sourced stability rule, not a literature value. Settings fixed in lab.toml [regime.hmm], untuned.",
  "symbol": "BTCUSDT",
  "interval": "4h",
  "features": {
    "b": {
      "hmm_bear_prob": {}
    }
  },
  "flat_when": "persist(b > 0.5, 3)"
}
```

### Family links (both overlays)

- [[idea-13]], plan `ee653bdb90cc58bd72211e34a007e2b18e78a1db7c047f8b3a3e55f80e5d98cf` · [[2026-10-26-btcusdt-ethusdt-regime-filters-and-regime-conditional-mean-reversion]]: Markov regime long/flat filter, abandoned, same regime-filter mechanism on a different model.
- Plan `73b34c074fa1252f343ef3120bc1126461f685aedb9b03e004af932420ddc0d8` in [[run-r-20261029-173704-59d618]] · [[2026-10-29-btcusdt-btcusdt-risk-overlay-slate-phase-15]]: the `vol_rank` overlay, abandoned, flat in high volatility.

### Decision rules, in the order they apply

Each overlay gets its own verdict. The rules below were fixed before any real HMM fit existed.

**1. Family check, before scoring (PLAN.md Part D).** After `run_backtest`, read `get_report`'s `pbo_max` row. If it names `73b34c074fa1` in the overlay's behaviour family, the overlay's design-window returns correlate at or above `family_return_corr` (0.8) with the abandoned `vol_rank` overlay. The verdict is then **already abandoned**, it is not re-tested, and `run_adversarial` and `run_crosscheck` are not run for it.

Known weakness, stated in advance: Phase 15 found that high-exposure overlays correlate through buy-and-hold whatever their logic, and `vol_rank` was invested on 98.7% of bars. This check can therefore fire on beta alone. The rule is applied as PLAN.md states it, because it fails closed and `pbo_max` fails the overlay either way. The write-up gives the overlay's average exposure, so a reader can see how much of the flag is beta. Any continuation after a flag needs the owner's explicit decision, is recorded as a deviation, and cannot change the verdict.

**2. Label check (owner decision 2026-11-04).** The state ordering stays mean-return. If `get_report`'s `hmm_diagnostics` shows the label is unreliable, the result is **inconclusive**, in either direction: not "no overlay benefit", and not a pass. Changing the ordering after reading a real run is a new hypothesis and is not allowed here.

The owner's words were "often the calm state" and "flips often". Claude set these numbers, before any real fit, as conventions rather than findings:

| condition | field | inconclusive when |
|---|---|---|
| Bear is often the calm state | `share_bear_is_high_vol` | below 0.75: on more than a quarter of decided bars, "bear" is the lower-volatility state |
| Flips often | `flips` | above `bars / 840`: more than one flip per 20 refits (840 bars, about 140 days), so the label changes meaning faster than about a quarter of the training block turns over |

If `hmm_diagnostics` is missing or returns an error, the label check counts as failed.

**3. Testability.** HMM plans are scored from the first finite HMM output, so about 500 days of early history drop out and the March 2020 crash may fall outside the scored window. If `min_drawdown_episodes` is below 3 the verdict is **not testable**, which is never a pass.

**4. Criteria rows.** Otherwise every row is read as frozen: **no overlay benefit** if any row fails. If `pre_holdout_passed` is true, the overlay is a **candidate**, and a holdout check needs the owner's approval. A candidate still needs 4-8 weeks of `freqtrade --dry-run` paper trading before it is called a working risk overlay.

`strategy-critic` reviews whatever comes out, in fresh context, before any holdout check.

### Seen-data declaration

The design window (2019-01 to 2025-10) has been seen. Earlier runs on it examined trend filters, volatility gating, regime filters and the Phase 15 overlays, so these overlays are not fresh on those bars. The locked holdout (from 2025-10-01) is unspent; it and the forward paper test are the real tests.

No HMM output has been computed on real BTC data. At this point `lab/data/hmm` does not exist, all HMM development used synthetic prices, and the ribbon picker's real-path check was held back until after this run so that nothing would be read from BTC first. No threshold, setting or rule above was chosen from an HMM result.

## Results

Written after the run. The pre-registration above is not edited.

### Verdict

**Both overlays: already abandoned (rule 1).** Their `pbo_max` rows name `73b34c074fa1`, the `vol_rank` overlay, so their design-window returns correlate at or above 0.8 with it. `run_adversarial` and `run_crosscheck` were not run, as rule 1 requires. No holdout check was spent: `pre_holdout_passed` is false for both.

**This is not "HMM regimes were tested and failed", and it is not "no overlay benefit".** Rule 2 would also have returned inconclusive on both of its conditions, and rule 4 was not reached.

| rule | result | plan `b8571030808a` | plan `6f92436eb9b2` |
|---|---|---|---|
| 1. Family check | fired, verdict set here | `73b34c074fa1` in family | `73b34c074fa1` in family |
| 2. Label check | would also fire | share 0.615 &lt; 0.75; flips 17 &gt; 16.2 | same fit, same values |
| 3. Testability | would pass | 5 episodes | 5 episodes |
| 4. Criteria rows | not reached, shown below | 7 rows fail | 7 rows fail |

### Strategy report: btc-4h-hmm-bear-092-flat: BTCUSDT 4h (design 2019-07-06 to 2025-10-01, costs 0.10% + 0.05% per side, slippage widened on wide bars)

Hypothesis: being flat while the filtered bear probability is above 0.92 cuts drawdown without a significant Sharpe loss. Rationale: regimes persist, so a confident bear reading comes before more of the loss. Plan hash: `b8571030808ab88cda3adcdfc277d5fd60ecf30df64e53252ea4e1bdd7491a36`. Features: `hmm_bear_prob`. Tunable params: 0.

### Strategy report: btc-4h-hmm-bear-050-persist3-flat: BTCUSDT 4h (same window and costs)

Hypothesis: being flat while the bear probability has held above 0.5 for 3 closed bars cuts drawdown the same way. Rationale: the video's stability claim, second-hand. Plan hash: `6f92436eb9b22bf67d7b773d63d33ca2cd70d34e4c04cc3cd657208c1fc1f5c9`. Features: `hmm_bear_prob`. Tunable params: 0.

### Metrics (both overlays)

Design window, fixed parameters. Overlay runs have no tuned walk-forward; the stitched-fold row is a sub-window check on the same bars, not out-of-sample. The net ×2 Sharpe comes from the 2x cost row's Sharpe gap plus buy-and-hold's Sharpe.

| metric | 0.92 gross | 0.92 net | 0.92 net ×2 | persist gross | persist net | persist net ×2 | buy&hold | vol-tgt B&H | random null (median) |
|---|---|---|---|---|---|---|---|---|---|
| CAGR | -1.4% | -20.3% | **missing** | 3.8% | -5.2% | **missing** | 44.6% | 38.1% | **missing** |
| Sharpe (365d) | 0.22 | -0.20 | -0.62 | 0.33 | 0.14 | -0.04 | 0.91 | 0.99 | 0.49 / 0.59 |
| Stitched-fold Sharpe | **missing** | -0.37 | **missing** | **missing** | -0.00 | **missing** | 0.80 / 0.79 | **missing** | **missing** |
| Sortino | 0.31 | -0.27 | **missing** | 0.45 | 0.20 | **missing** | 1.29 | 1.41 | **missing** |
| max DD | 80.6% | 88.6% | **missing** | 76.0% | 78.5% | **missing** | 77.0% | 64.7% | **missing** |
| win rate | 42.8% | 42.8% | **missing** | 46.0% | 46.0% | **missing** | n/a | n/a | n/a |
| profit factor | 0.75 | 0.75 | **missing** | 0.92 | 0.92 | **missing** | n/a | n/a | n/a |
| trades | 194 | 194 | 194 | 113 | 113 | 113 | 1 | 1 | same count |
| exposure | 68.0% | 68.0% | 68.0% | 66.6% | 66.6% | 66.6% | 100% | 100% | **missing** |
| turnover /yr | 62.2x | 62.2x | 62.2x | 36.2x | 36.2x | 36.2x | 0.2x | 5.0x | **missing** |

Ledger trials this run: 24, chain verified (head `cc97248d1332`). DSR N across every run: 5892. DSR: run-local 0.31 and 0.52 are dropped for overlay runs and neither is near 0.95. PBO: not computed (no tunable parameters); `pbo_max` fails on the family rule. Sharpe 95% CI: [-0.95, 0.58] and [-0.62, 0.92]. Random-entry percentile: 0 and 3.

Permutation null: **missing** (not run, rule 1). Signal delay: **missing** (not run, rule 1). Sign flip: **missing** (not run, rule 1). Parameter neighbours: none, no tunable parameters. Regimes by bull, bear, sideways: **missing** (not read for this write-up). Top-5 trades removed: **missing** (not run, rule 1).
Holdout: not checked (holdout checks used: 0/1). Cross-check engine: not run (rule 1).

Success criteria: 2 of 11 rows pass for each overlay, 7 fail, 2 missing. Verdict: abandoned.

### Criteria rows

N_overlay 5, per-test level 0.01.

| criterion | threshold | 0.92 status | 0.92 value | persist status | persist value |
|---|---|---|---|---|---|
| cdar_vs_buy_hold_max | 0.75 | fail | 1.185 | fail | 1.041 |
| beat_exposure_matched | p 0.01 | fail | p 0.998 (CDaR 0.876 vs 0.580) | fail | p 0.979 (CDaR 0.769 vs 0.572) |
| min_episodes_improved | above 0.5 | fail | 0 of 5 | fail | 0 of 5 |
| sharpe_non_inferiority | -0.2 | fail | -1.538 | fail | -1.219 |
| break_even_cost_multiple_min | 2 | fail | fails at every multiple | fail | fails at every multiple |
| min_drawdown_episodes | 3 | pass | 5 | pass | 5 |
| min_avg_exposure | 0.5 | pass | 0.680 | pass | 0.666 |
| wf_rows_hold | yes | fail | no | fail | no |
| pbo_max | 0.2 | fail | 3 alike overlays | fail | 3 alike overlays |
| require_holdout_pass | yes | missing | n/a | missing | n/a |
| require_crosscheck_match | yes | missing | n/a | missing | n/a |

### Named crashes (information only, net of costs)

| episode | buy-hold return | 0.92 return | 0.92 exposure | persist return | persist exposure |
|---|---|---|---|---|---|
| covid_crash_2020_03 | -24.8% | -37.2% | 0.21 | -38.9% | 0.21 |
| deleveraging_2021_05 | -35.4% | -17.1% | 0.45 | -16.6% | 0.46 |
| luna_2022_05 | -15.5% | -13.2% | 0.65 | -10.9% | 0.62 |
| ftx_2022_11 | -16.2% | -16.0% | 0.72 | -8.6% | 0.71 |
| carry_unwind_2024_08 | -8.7% | -6.6% | 0.98 | -6.6% | 0.98 |

Four of five named crashes look better than buy-and-hold, and that is what holding less produces in any falling market. The rows that count compare with the same exposure held statically, and there the overlays improved 0 of 5 deep drawdowns. In March 2020 both were 79% flat and still lost more than buy-and-hold: the flat bars missed the rebound, not the fall.

### Critic (fresh context): abandon

`strategy-critic` agreed with the order and the verdicts: rule 1 sets the verdict, the rest are shown but not applied, and skipping `run_adversarial` and `run_crosscheck` was required. No look-ahead smell, since leakage flatters and these sit at the 0th and 3rd percentile of the random-entry null. Refinement is not available: `MAX_HMM_OVERLAY_LOGICS = 2` is used up, so a third logic is refused. A new state ordering would be a new hypothesis, needing the owner's decision and its own pre-registration; the critic does not recommend one. Its biggest concern is that "already abandoned" gets misread as "regimes tested and failed".

### Notes

- **What may be claimed.** On seen data, these two fixed rules lost to static BTC at the same exposure. Nothing more.
- **What must not be claimed.** Nothing here is evidence about HMM regimes, because the bear label was the calmer state on about 38% of bars. Nothing is evidence about the thresholds: one value each, and the persist rule also changes the threshold, so 194 to 113 trades is not a `persist` effect. Costs are not the cause: gross Sharpe was 0.22 and 0.33 against 0.91.
- **The family flag is unproven as shared logic.** `get_report` reports membership, not the correlation. The Part I quant review measured it read-only on the 1d grid: 0.821 and 0.819 with `vol_rank`, while `vol_rank` itself is 0.98 with buy-and-hold. Correlation through buy-and-hold alone is roughly the square root of the share of variance held: √0.68 = 0.82 and √0.67 = 0.82, just over 0.8. Flat bars that carry more than average variance would pull that lower, and the flag cannot be split between beta and logic. As Phase 15 warned, this check cannot separate them near this exposure. The verdict stands because the rule fails closed.
- **The flips condition was close:** 17 against 16.2. The share condition was not: 0.615 against 0.75. Either one alone fails rule 2.
- **Label failure is the likely mechanism, but it is not shown here.** With bear on the calmer state for about 38% of bars, the overlay is sometimes flat in calm markets. No run separated that effect.
- **The 84 undecided bars are explained.** Checked on design bars only: two Binance 8-hour gaps, 2019-08-15 and 2020-02-19, each blank the 42-bar volatility window that follows, exactly as designed, and no fit failed. `hmm_bear_prob` is NaN on those bars, so the overlay stays invested there.
- **Cost drag is larger than the nominal rate suggests.** The 0.92 overlay paid roughly 0.34% per side across 194 round trips, against a nominal 0.15%. That is consistent with `slippage_vol_mult` widening slippage on the wide bars where these rules switch, but it was not measured directly. The quoted 0.05% understates cost for a rule that trades in volatility spikes.
- **Correction to the pre-registration's expectation, text not edited.** It said about 500 days of history would drop out and March 2020 might fall outside the window. Warm-up used pre-design history, so scoring began on 2019-07-06, March 2020 is included, and there were 5 deep episodes, not fewer than 3. The prediction was wrong; no rule or verdict depended on it.
- **Disclosure.** Claude set the rule 2 numbers (0.75, one flip per 20 refits) before any real fit, as conventions. The verdict does not depend on them, because rule 1 applied first.
- **Slots spent.** Both of the 2 HMM overlay slots are used, N_overlay is 5, and DSR N is 5892. The holdout is unspent.
- **Lesson.** A mean-ordered two-state label was not a stable bear state on BTC 4h, and the family check alone cannot separate shared logic from beta near 0.67 exposure. Compare any regime overlay with static de-risking at the same exposure first.
- Candidates entering paper trading: none. Results are historical and not forward-looking.

### Disclaimer

Backtests are historical, not forward-looking. A risk overlay is a drawdown result measured against exposure-matched buy-and-hold, not a return forecast.
