---
run_id: "r-20261104-235252-f77c6d"
date: 2026-11-04
question: risk_overlay
symbols:
  - "BTCUSDT"
interval: "4h"
outcome: pending
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

Written after the run.

### Disclaimer

Backtests are historical, not forward-looking. A risk overlay is a drawdown result measured against exposure-matched buy-and-hold, not a return forecast.
