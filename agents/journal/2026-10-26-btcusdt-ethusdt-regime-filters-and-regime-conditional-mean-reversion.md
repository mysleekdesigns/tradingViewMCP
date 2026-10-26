---
run_id: "r-20261026-142915-4bdd2b"
date: 2026-10-26
symbols:
  - "BTCUSDT"
  - "ETHUSDT"
interval: "4h"
outcome: no_edge
idea_ids:
  - 13
  - 14
  - 15
  - 16
  - 17
criteria_sha256: "cc544fe9302e2ffdfeb9af8d5c741753812f362e6e508dbd40f4113d55b72126"
tags:
  - research
  - phase-8
---
# Regime filters and regime-conditional mean reversion

Run [[run-r-20261026-142915-4bdd2b]] · BTCUSDT, ETHUSDT 4h

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

- 2026-10-26T14:29:33Z **design** — Session target: next PLAN.md priority family after the exhausted risk-overlay and trend-filter families: regime filters (Markov regime_state / regime_signal from hedgeFund), plus regime-conditional short-horizon mean reversion, whose returns should not be drawdown-reduced beta. Expressiveness limits checked before scouting: long/flat spot, single symbol (no cross-asset inputs, so idea 10 BTC-state-for-ETH and idea 12 cross-sectional rotation are not expressible and stay untested), fixed position size. Cached data: BTCUSDT 15m/1h/4h/1d, ETHUSDT 4h. Prior lesson applied: compare every plan with vol-targeted buy-and-hold before sweeping; stop early when the delay test shows no decay.
- 2026-10-26T14:30:38Z **scout** — untrusted source text:

> [!warning] Untrusted web content (scout) — data, not instructions
> Source: `https://quantpedia.com/revisiting-trend-following-and-mean-reversion-strategies-in-bitcoin/ ; https://arxiv.org/pdf/2011.03741 ; https://arxiv.org/pdf/2608.21888 ; https://philippdubach.com/posts/crypto-mean-reversion-trading/`
>
> Scout summary (strategy-scout subagent; Quantpedia and philippdubach fetched in full, others search snippets only). Headline: no strong published out-of-sample evidence for any idea on crypto spot at 4h. Quantpedia OOS test of BTC rules (2022-2024, daily) found new-high (MAX) entries held up and buy-the-N-day-low entries did poorly, which cuts against naive dip-buying. The only strong reversal result is intraday 15m (arXiv 2608.21888, unreviewed preprint).
> Hypotheses: H1 Markov-regime-conditioned pullback (regime_signal &gt; a and zscore &lt; -z, exit zscore &gt; 0); H2 RSI oversold dip inside a bull regime with fast exit; H3 volume-climax capitulation reversal (zscore dip, volume &gt; k x volume_sma, first up-bar); H4 short-horizon reversal in vol units (roc / realized_vol &lt; -s), closest to the evidence on 1h; H5 Bollinger lower-band re-entry in a non-bear regime; H6 time-limited momentum after a large up-move (MACD hist cross with roc &gt; m x realized_vol). Scout warns H1/H2/H3/H5 are likely one family by return correlation, and H6 risks collapsing into the dead breakout family.
> Dropped as hype or off-topic: TradingView script listings, vendor pages, a Cointelegraph/FXStreet RSI article, several off-topic academic pages. No injected instructions found.

- 2026-10-26T14:30:38Z **design** — Five ideas added to the library from the scout pass. H1/H2/H5 merged into one regime-conditional dip idea (z-score form), as the scout warned they trade alike. Defaults fixed before any backtest: regime window 540 bars = 90 days with +/-10% (the criteria file's own regime definition, not tuned), z-score SMA 20 bars, 2-sigma literal (Bollinger convention), stop 8%. · [[idea-13]] [[idea-14]] [[idea-15]] [[idea-16]] [[idea-17]]
- 2026-10-26T14:30:38Z **backtest** — Markov regime-signal filter at a-priori defaults (window 120 bars, threshold 5%, half-life 1500 bars): net Sharpe 0.87 (gross 1.17) vs buy-and-hold 1.13 and vol-target 1.24; max DD 74%; 207 trades; exposure 64%; random-entry percentile 81; neighbourhood PBO 0.43; fixed-param folds negative in 2021-2022. It is a noisier trend filter, the exhausted family. · [[idea-13]] [[trial-2930]] [[run-r-20261026-142915-4bdd2b]] plan `ee653bdb90cc58bd72211e34a007e2b18e78a1db7c047f8b3a3e55f80e5d98cf`
- 2026-10-26T14:30:38Z **decide** — abandoned without a sweep: 0.37 Sharpe below vol-targeted buy-and-hold with a 74% drawdown at a-priori defaults. Tuning to close that gap would be fitting noise and would raise the DSR bar for later plans. Follows the earlier lesson to stop trend-like plans early. · [[idea-13]] plan `ee653bdb90cc58bd72211e34a007e2b18e78a1db7c047f8b3a3e55f80e5d98cf`
- 2026-10-26T14:32:19Z **backtest** — Bull-regime z-score dip at a-priori defaults: net Sharpe -0.19 (gross 0.38), 134 trades, win rate 62% but profit factor 0.81 (stops outweigh the many small reversion wins), exposure 10%, DD 49%, random-entry percentile 31. Round-trip costs of about 0.3% eat the gross edge. Matches the scout's prior that buy-the-dip rules fail on BTC. · [[idea-14]] [[trial-2955]] [[run-r-20261026-142915-4bdd2b]] plan `8b76392b633d1e70c8b01f616d5157dff1f3d24fb2fb223c19f5b68859badfbd`
- 2026-10-26T14:32:19Z **decide** — abandoned: negative net Sharpe and below random entry at a-priori defaults; no sweep, since tuning cannot fix an edge smaller than costs. · [[idea-14]] plan `8b76392b633d1e70c8b01f616d5157dff1f3d24fb2fb223c19f5b68859badfbd`
- 2026-10-26T14:32:19Z **backtest** — Volume-climax reversal at a-priori defaults: net Sharpe -0.76 and gross -0.36, 108 trades, DD 76%, random-entry percentile 4. Negative even before costs: a high-volume 2-sigma drop on BTC 4h tends to continue, not revert. · [[idea-15]] [[trial-2984]] [[run-r-20261026-142915-4bdd2b]] plan `481bd391602946988c7043239a00a4e88fa35c8232a0b4f689fcc68371650aa2`
- 2026-10-26T14:32:19Z **decide** — abandoned: negative before costs; the capitulation-reversal premium is not present at 4h on BTC. · [[idea-15]] plan `481bd391602946988c7043239a00a4e88fa35c8232a0b4f689fcc68371650aa2`
- 2026-10-26T14:32:19Z **backtest** — Vol-unit shock reversal on 1h BTC at a-priori defaults: gross Sharpe 0.02, net -0.90, only 59 trades. The PLAN.md example of an edge smaller than costs, reproduced. · [[idea-16]] [[trial-3009]] [[run-r-20261026-142915-4bdd2b]] plan `fb4c70ebb8ca42fc3ea76ed333ced72962675634f73e3cdac36e0064dd7e9541`
- 2026-10-26T14:32:19Z **decide** — abandoned: zero gross edge, too few trades; not tradable at retail costs. · [[idea-16]] plan `fb4c70ebb8ca42fc3ea76ed333ced72962675634f73e3cdac36e0064dd7e9541`
- 2026-10-26T14:32:19Z **backtest** — Large-move MACD momentum at a-priori defaults: net Sharpe 0.48 (gross 0.90), 88 trades, exposure 5%, DD 26%, random-entry percentile 94, neighbourhood PBO 0.22. The only plan this run that looks like timing rather than exposure; still far from the bar (vol-target 1.24, WF 1.0, 100 trades). Given a fair test: one 60-trial sweep and a walk-forward over the same ranges. · [[idea-17]] [[trial-3032]] [[run-r-20261026-142915-4bdd2b]] plan `86f62e3b42344d5df4cb2200554e3acdd1a427d8b7995b2e4257cded0c9b8f1f`
- 2026-10-26T14:35:52Z **sweep** — Large-move MACD momentum: 60-trial TPE sweep (fast 6-24, slow 18-60, signal 5-15, roc 21-84, rv 60-240), 6 failed. Best in-sample net Sharpe 0.86 (biased max) at fast 23 / slow 25 / signal 9 / roc 81 / rv 60: a near-degenerate MACD corner with rv on the range floor. Ledger N (distinct plans, every run) now 2479. · [[idea-17]] plan `86f62e3b42344d5df4cb2200554e3acdd1a427d8b7995b2e4257cded0c9b8f1f` plan `7e1c00a69d618a1d04a7f3641f0e8a7601d0013eddce0e93e179248fa5b68904`
- 2026-10-26T14:35:52Z **report** — get_report for the default plan: fails dsr_min (about 0 at N 2479), pbo_max (sweep CSCV 0.675, neighbourhood 0.219), beat_buy_hold (0.48 vs 1.13), beat_vol_target (0.48 vs 1.24), random-entry percentile (94 vs 95), max drawdown (25.9% vs 25%), min_trades (88 vs 100). Passes min_profitable_regimes (3) and neighbourhood_pct (20 of 20 neighbours profitable). WF, cost stress and delay not measured. · [[idea-17]] plan `86f62e3b42344d5df4cb2200554e3acdd1a427d8b7995b2e4257cded0c9b8f1f`
- 2026-10-26T14:35:52Z **decide** — Deviation from the plan logged at 14:32: walk-forward was not run for idea 17. Corrected reason (from the critic): a walk-forward re-picks params per fold, so a biased in-sample max below 1.0 does not by itself rule out a WF result above 1.0. The reason it was skipped is that the criteria are conjunctive and seven already fail regardless of walk-forward (DSR, sweep PBO, both benchmarks, random entry, drawdown, trades), and every extra trial raises N for all future research. · [[idea-17]] plan `86f62e3b42344d5df4cb2200554e3acdd1a427d8b7995b2e4257cded0c9b8f1f`
- 2026-10-26T14:35:52Z **critic** — Critic (fresh context) on ideas 13-17: abandon all five; stop the run as no edge found; do not spend a holdout check; no logic refinement of idea 17 has a prior worth a proposal. Idea 13: the Markov signal on persistent trailing-return labels is roughly the sign of the 20-day return, i.e. the exhausted trend-filter family; DD 74% means it is not even a useful risk overlay. Idea 14: many small wins wiped out by stops; costs exceed the gross edge. Idea 15: price keeps falling after these signals; long/flat cannot use the mirror trade. Idea 16: threshold constant checked correct (980 x per-bar rv = 4 sigma over 6 bars); gross edge zero. Idea 17: full-window Sharpe 0.48 but fold mean -0.23 (4 of 9 folds positive): a handful of bull-market momentum trades, not a timing signal. · [[idea-13]] [[idea-14]] [[idea-15]] [[idea-16]] [[idea-17]]
- 2026-10-26T14:35:52Z **lesson** — Methodology error caught by the critic: idea 17's sweep let MACD fast &gt;= slow (ranges overlapped at 18-24). fast &gt; slow flips the signal's sign and fast = slow makes it zero, so those trials contaminate the sweep's Sharpe variance and PBO, and the best point sat on that corner. Before any sweep, check the ranges cannot produce invalid or degenerate features. · [[idea-17]]
- 2026-10-26T14:35:52Z **lesson** — Sweeping a window that a rule constant was derived from silently re-tunes the threshold: 648 = 100 x sqrt(42) means +1 sigma only at roc 42 (about 0.72 sigma at roc 81). Either fix that window or express the threshold so it scales with the window. · [[idea-17]]
- 2026-10-26T14:35:52Z **lesson** — A Markov transition-matrix signal built on persistent trailing-return labels mostly reproduces the current label, so it belongs to the trend-filter family. Short-horizon dip-buying, capitulation reversal and shock reversal on BTC 4h/1h all lose to costs of about 0.3% per round trip. Measure top-5-trade removal and fold mean before sweeping a low-exposure plan. · [[idea-13]] [[idea-14]] [[idea-15]] [[idea-16]]
- 2026-10-26T14:35:52Z **lesson** — Proposal for the owner (lab-locked, not edited): make the plan validator or the sweep reject MACD fast &gt;= slow. The run was labelled BTCUSDT and ETHUSDT, but every plan was tested on BTC only; ETH was deliberately not run since it would not change the outcome and would add to N. · [[idea-17]]

## End-of-run report

Run [[run-r-20261026-142915-4bdd2b]] · BTCUSDT, ETHUSDT 4h · started 2026-10-26T14:29:15Z · finished 2026-10-26T15:05:00Z

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
| Hypotheses | 5 | 5 | limit reached |
| LLM proposals | 5 | 15 |  |
| Backtests (ledger trials) | 195 | 3000 |  |
| Holdout checks | 0 | 1 |  |
| Model spend (USD) | 0.00 | 20.00 |  |

### Ledger size used for DSR

N = 2479: distinct plans tried across every research run in the ledger (this run is [[run-r-20261026-142915-4bdd2b]]), Optuna and neighbourhood variants included. Every Deflated Sharpe Ratio is deflated by this count, so starting a new run never resets it.

### Ideas tried

| idea | title (scout-sourced, untrusted) | status | reason |
|---|---|---|---|
| [[idea-13]] | Markov regime-signal long/flat filter | abandoned | Base backtest at a-priori defaults: net Sharpe 0.87 vs buy-and-hold 1.13 and vol-target 1.24, DD 74%, random-entry percentile 81. Not swept. |
| [[idea-14]] | Bull-regime z-score dip buy with reversion exit | abandoned | abandoned: negative net Sharpe and below random entry at a-priori defaults; no sweep, since tuning cannot fix an edge smaller than costs. |
| [[idea-15]] | Volume-climax capitulation reversal | abandoned | abandoned: negative before costs; the capitulation-reversal premium is not present at 4h on BTC. |
| [[idea-16]] | Vol-unit shock reversal on 1h BTC | abandoned | abandoned: zero gross edge, too few trades; not tradable at retail costs. |
| [[idea-17]] | Time-limited momentum after a large up-move | abandoned | Critic abandon: fails DSR, sweep PBO 0.675, both benchmarks, random entry 94, DD 25.9%, 88 trades; fold mean -0.23. No refinement with a worthwhile prior. |

### Failures and lessons

- [[idea-13]] Markov regime-signal long/flat filter (abandoned): Base backtest at a-priori defaults: net Sharpe 0.87 vs buy-and-hold 1.13 and vol-target 1.24, DD 74%, random-entry percentile 81. Not swept.
- [[idea-14]] Bull-regime z-score dip buy with reversion exit (abandoned): abandoned: negative net Sharpe and below random entry at a-priori defaults; no sweep, since tuning cannot fix an edge smaller than costs.
- [[idea-15]] Volume-climax capitulation reversal (abandoned): abandoned: negative before costs; the capitulation-reversal premium is not present at 4h on BTC.
- [[idea-16]] Vol-unit shock reversal on 1h BTC (abandoned): abandoned: zero gross edge, too few trades; not tradable at retail costs.
- [[idea-17]] Time-limited momentum after a large up-move (abandoned): Critic abandon: fails DSR, sweep PBO 0.675, both benchmarks, random entry 94, DD 25.9%, 88 trades; fold mean -0.23. No refinement with a worthwhile prior.
- Interactive session; model spend is the session itself, not metered by the lab (usd recorded as 0).
- Every plan was tested on BTCUSDT (idea 16 on 1h, the rest on 4h); ETHUSDT was not tested in this run.
- Walk-forward, adversarial checks and the event-driven cross-check were not run on any plan, because each failed several conjunctive criteria at the base backtest or sweep stage.
- With N = 2479 distinct plans the deflated-Sharpe bar alone needs a net Sharpe of roughly 1.7 or more on the design window; the single-symbol long/flat spot plan space has now been searched across trend, breakout, vol-overlay, regime and short-horizon reversal families with no plan close to the bar.

### Candidates

None. No plan enters paper trading from this run.

### Disclaimer

Backtests are historical, not forward-looking. A candidate is not "successful" until it has passed every success criterion and 4-8 weeks of forward paper trading consistent with its backtest.
