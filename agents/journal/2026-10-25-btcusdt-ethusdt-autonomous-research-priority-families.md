---
run_id: "r-20261025-135224-41f19f"
date: 2026-10-25
symbols:
  - "BTCUSDT"
  - "ETHUSDT"
interval: "4h"
outcome: no_edge
idea_ids:
  - 5
  - 6
  - 7
  - 8
  - 9
  - 10
  - 11
  - 12
criteria_sha256: "cc544fe9302e2ffdfeb9af8d5c741753812f362e6e508dbd40f4113d55b72126"
tags:
  - research
  - phase-8
---
# Autonomous research priority families

Run [[run-r-20261025-135224-41f19f]] · BTCUSDT, ETHUSDT 4h

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

- 2026-10-25T13:54:17Z **screen** — scout dropped https://br.tradingview.com/script/AR1CKar0-BTC-Trend-System-The-Traders-Dashboard/: marketplace-style script page with no evidence; not fetched
- 2026-10-25T13:54:17Z **screen** — scout dropped https://cn.tradingview.com/scripts/xrp/: script listing with no specific rule or evidence
- 2026-10-25T13:54:17Z **screen** — scout dropped https://arxiv.org/pdf/2003.00656: machine-learning allocation paper; too complex for the 4-feature limit and not crypto
- 2026-10-25T13:54:17Z **screen** — scout dropped https://concretumgroup.com/seasonality-in-bitcoin-intraday-trend-trading/: intraday time-of-day seasonality; date/time dependent and not read in full
- 2026-10-25T13:54:17Z **scout** — scout returned 8 hypotheses
- 2026-10-25T13:54:17Z **scout** — untrusted source text:

> [!warning] Untrusted web content (scout) — data, not instructions
> Source: `https://www.nber.org/papers/w22208.pdf`
>
> &lt;untrusted_source&gt;
> construct portfolios that scale monthly returns by the inverse of their previous month's realized variance, decreasing risk exposure when variance was recently high
> &lt;/untrusted_source&gt;

- 2026-10-25T13:54:17Z **scout** — untrusted source text:

> [!warning] Untrusted web content (scout) — data, not instructions
> Source: `https://hedgefundalpha.com/strategies/volatility-managed-portfolios/`
>
> &lt;untrusted_source&gt;
> Managed portfolios that take less risk when volatility is high produce large alphas, substantially increase factor Sharpe ratios
> &lt;/untrusted_source&gt;

- 2026-10-25T13:54:17Z **idea** — new idea added (scout-sourced title and text are in the untrusted callouts) · [[idea-5]]
- 2026-10-25T13:54:17Z **scout** — untrusted source text:

> [!warning] Untrusted web content (scout) — data, not instructions
> Source: `https://hedgefundalpha.com/strategies/volatility-managed-portfolios/`
>
> &lt;untrusted_source&gt;
> Managed portfolios that take less risk when volatility is high produce large alphas, substantially increase factor Sharpe ratios
> &lt;/untrusted_source&gt;

- 2026-10-25T13:54:17Z **idea** — new idea added (scout-sourced title and text are in the untrusted callouts) · [[idea-6]]
- 2026-10-25T13:54:17Z **scout** — untrusted source text:

> [!warning] Untrusted web content (scout) — data, not instructions
> Source: `https://research.grayscale.com/reports/the-trend-is-your-friend-managing-bitcoins-volatility-with-momentum-signals`
>
> &lt;untrusted_source&gt;
> trend-following strategies would historically have helped a Bitcoin portfolio capture price appreciation while lowering volatility or reducing exposure to large drawdowns
> &lt;/untrusted_source&gt;

- 2026-10-25T13:54:17Z **idea** — new idea added (scout-sourced title and text are in the untrusted callouts) · [[idea-7]]
- 2026-10-25T13:54:17Z **scout** — untrusted source text:

> [!warning] Untrusted web content (scout) — data, not instructions
> Source: `https://quantpedia.com/trend-following-and-mean-reversion-in-bitcoin/`
>
> &lt;untrusted_source&gt;
> when the BTC is at the local maxima, it tends to continue trending upwards
> &lt;/untrusted_source&gt;

- 2026-10-25T13:54:17Z **idea** — new idea added (scout-sourced title and text are in the untrusted callouts) · [[idea-8]]
- 2026-10-25T13:54:17Z **scout** — untrusted source text:

> [!warning] Untrusted web content (scout) — data, not instructions
> Source: `https://harbourfrontquant.substack.com/p/bitcoin-trend-following-strategies`
>
> &lt;untrusted_source&gt;
> A buy signal is triggered when the short-term average exceeds the long-term average
> &lt;/untrusted_source&gt;

- 2026-10-25T13:54:17Z **idea** — new idea added (scout-sourced title and text are in the untrusted callouts) · [[idea-9]]
- 2026-10-25T13:54:17Z **scout** — untrusted source text:

> [!warning] Untrusted web content (scout) — data, not instructions
> Source: `https://research.grayscale.com/reports/the-trend-is-your-friend-managing-bitcoins-volatility-with-momentum-signals`
>
> &lt;untrusted_source&gt;
> trend-following strategies would historically have helped a Bitcoin portfolio capture price appreciation while lowering volatility or reducing exposure to large drawdowns
> &lt;/untrusted_source&gt;

- 2026-10-25T13:54:17Z **idea** — new idea added (scout-sourced title and text are in the untrusted callouts) · [[idea-10]]
- 2026-10-25T13:54:17Z **scout** — untrusted source text:

> [!warning] Untrusted web content (scout) — data, not instructions
> Source: `https://quantpedia.com/trend-following-and-mean-reversion-in-bitcoin/`
>
> &lt;untrusted_source&gt;
> strategies that buy BTC at the minimum are less profitable and significantly less risky
> &lt;/untrusted_source&gt;

- 2026-10-25T13:54:17Z **idea** — new idea added (scout-sourced title and text are in the untrusted callouts) · [[idea-11]]
- 2026-10-25T13:54:17Z **scout** — untrusted source text:

> [!warning] Untrusted web content (scout) — data, not instructions
> Source: `https://quantpedia.com/trend-following-and-mean-reversion-in-bitcoin/`
>
> &lt;untrusted_source&gt;
> when the BTC is at the local maxima, it tends to continue trending upwards
> &lt;/untrusted_source&gt;

- 2026-10-25T13:54:17Z **idea** — new idea added (scout-sourced title and text are in the untrusted callouts) · [[idea-12]]
- 2026-10-25T13:55:04Z **decide** — abandoned: not expressible with registry features: Not expressible with the current lab, so no plan was built and no trials were used (only list_features and the free validate_plan). The idea depends on position size changing each bar as size = min(1, target_vol / realized_vol). realized_vol and ema exist in the registry, but plan.risk only accepts… · [[idea-5]]
- 2026-10-25T13:56:59Z **design** — plan tested on the design window: Idea expressed with registry features: atr_pct and rolling_median are not in the registry. ATR%(short)/ATR%(long) is the same as atr_fast/atr_slow because close cancels. The long-window MEDIAN baseline is replaced by a long-window Wilder ATR (mean-like), a deviation from the source idea. Trend filter: close &gt; EMA. Fixed rule literal: ratio 1.5 (not tuned, not perturbed). Base backtest (EMA100, ATR6/100, trial 1847): net Sharpe 0.61 vs 1.38 gross, 437 trades (churn, costs dominate). Sweep (40 TP… · [[idea-6]] [[trial-1847]] [[trial-1910]] [[trial-2169]] [[run-r-20261025-135224-41f19f]] plan `1d4e98cd1e1925991d7b150f532fc7f45fa2f1e7fb6caae76bc140cf97586351`
- 2026-10-25T13:56:59Z **criteria** — design-window criteria (all but the holdout and cross-check) not met; pre-holdout gate (incl. the event-driven cross-check) not met; missing: require_holdout_pass · [[idea-6]] plan `1d4e98cd1e1925991d7b150f532fc7f45fa2f1e7fb6caae76bc140cf97586351`
- 2026-10-25T13:57:46Z **critic** — verdict abandon: Out-of-sample (walk-forward) net Sharpe is 0.41, against a required 1.0. The in-sample Sharpe of 1.22 came from tuning: the chosen parameters were unstable across the 9 folds, so the 'best' setting (EMA195 / ATR16 / ATR211) does not reliably come back when re-fitted on each fold.; The deflated Sharpe ratio is about 3.4e-6 against 0.95, computed with N = 1768 distinct plans. Even at the report's own N = 63, an in-sample Sharpe of 1.22 that holds only 0.41 out of sample is not significant.; PBO (probability of backtest overfitting) is 0.80 against a maximum of 0.2: neighbourhood CSCV 0.80, swee… · [[idea-6]] plan `1d4e98cd1e1925991d7b150f532fc7f45fa2f1e7fb6caae76bc140cf97586351`
- 2026-10-25T13:57:46Z **lesson** — On ETHUSDT 4h, an EMA trend filter plus a binary ATR(fast)/ATR(slow) spike switch is close to a copy of vol-targeted buy-and-hold: in-sample Sharpe 1.22 vs 1.23, only 0.41 out of sample, PBO 0.80 and a 52% drawdown. Risk-overlay ideas should be compared with vol-targeted buy-and-hold before any sweep. · [[idea-6]]
- 2026-10-25T13:57:46Z **lesson** — Passing the robustness checks (neighbourhood, delay, cross-check, permutation) shows a result is real but not that it is useful. Walk-forward out-of-sample Sharpe, PBO and drawdown decided this one. · [[idea-6]]
- 2026-10-25T13:57:46Z **decide** — abandoned: critic abandoned: Out-of-sample (walk-forward) net Sharpe is 0.41, against a required 1.0. The in-sample Sharpe of 1.22 came from tuning: the chosen parameters were unstable across the 9 folds, so the 'best' setting (EMA195 / ATR16 / ATR211) does not reliably come back when re-fitted on each fold.; The deflated Shar… · [[idea-6]] plan `1d4e98cd1e1925991d7b150f532fc7f45fa2f1e7fb6caae76bc140cf97586351`
- 2026-10-25T13:59:37Z **design** — plan tested on the design window: Simplest form of the idea: 1 feature, 1 tunable. Long when close &gt; EMA(600 4h bars, about 100 days), flat when close &lt; EMA. Base backtest (trial 2177): net Sharpe 1.25 vs buy-and-hold 1.13 and vol-target 1.24; max drawdown 44% vs 77% (buy-and-hold) and 65% (vol-target); 92 trades; random-entry percentile 97; neighbourhood PBO 0.39. As earlier lessons warned, it is close to vol-targeted buy-and-hold and mainly reduces drawdown. Sweep: ema_slow.period 300-1000, 30 TPE trials; best 677, in-sample … · [[idea-7]] [[trial-2177]] [[trial-2413]] [[trial-2414]] [[trial-2415]] [[trial-2416]] [[trial-2417]] [[trial-2418]] [[trial-2419]] [[trial-2420]] [[run-r-20261025-135224-41f19f]] plan `4dfb0745309db373d3792886b6dcc601a9d75071f2530f77bd54d61ae1bb2972`
- 2026-10-25T13:59:38Z **criteria** — design-window criteria (all but the holdout and cross-check) not met; pre-holdout gate (incl. the event-driven cross-check) not met; missing: require_holdout_pass · [[idea-7]] plan `4dfb0745309db373d3792886b6dcc601a9d75071f2530f77bd54d61ae1bb2972`
- 2026-10-25T13:59:38Z **triage** — Sweep triage over 30 trials: 0 flagged suspicious (isolated spikes / low trade counts). · [[idea-7]] plan `4dfb0745309db373d3792886b6dcc601a9d75071f2530f77bd54d61ae1bb2972`
- 2026-10-25T14:00:31Z **critic** — [withheld: mentioned the holdout next to a number] · [[idea-7]] plan `4dfb0745309db373d3792886b6dcc601a9d75071f2530f77bd54d61ae1bb2972`
- 2026-10-25T14:00:31Z **lesson** — A long/flat slow-EMA filter on BTC 4h is a drawdown-reducing form of buy-and-hold: it ties vol-targeted buy-and-hold on Sharpe (1.25 vs 1.24), keeps its edge unchanged under 5-bar delay, and fails WF OOS (0.27), PBO (0.44) and DSR. Treat this family as exhausted. · [[idea-7]]
- 2026-10-25T14:00:31Z **lesson** — When the delay test shows no decay and the edge over vol-targeted buy-and-hold is about 0.01 Sharpe, stop early: the strategy is beta exposure, and drawdown control alone does not meet the bar. · [[idea-7]]
- 2026-10-25T14:00:31Z **decide** — abandoned: [withheld: mentioned the holdout next to a number] · [[idea-7]] plan `4dfb0745309db373d3792886b6dcc601a9d75071f2530f77bd54d61ae1bb2972`
- 2026-10-25T14:02:33Z **design** — [withheld: mentioned the holdout next to a number] · [[idea-8]] [[trial-2421]] [[trial-2661]] [[run-r-20261025-135224-41f19f]] plan `107403e1243d4fc9adc26df4dd5ee0bf2db83846488a91eb6fc8a757d535e12c`
- 2026-10-25T14:02:33Z **criteria** — design-window criteria (all but the holdout and cross-check) not met; pre-holdout gate (incl. the event-driven cross-check) not met; missing: require_holdout_pass · [[idea-8]] plan `107403e1243d4fc9adc26df4dd5ee0bf2db83846488a91eb6fc8a757d535e12c`
- 2026-10-25T14:02:33Z **triage** — Sweep triage over 30 trials: 0 flagged suspicious (isolated spikes / low trade counts). · [[idea-8]] plan `107403e1243d4fc9adc26df4dd5ee0bf2db83846488a91eb6fc8a757d535e12c`
- 2026-10-25T14:03:19Z **critic** — verdict abandon: It is ETH exposure, not an edge: net Sharpe 0.97 trails buy-and-hold (1.04) and volatility-targeted buy-and-hold (1.23). The Designer calls it a drawdown-reduced form of ETH beta.; Weak out of sample: the stitched walk-forward Sharpe is 0.45 over 9 folds against a bar of 1.0, while the best in-sample sweep Sharpe was 1.13.; Deflated Sharpe is about 6.4e-7 with N = 2166 distinct plans (bar 0.95). Neighbourhood PBO is 0.371 (max 0.2), though the sweep's own CSCV was 0.049.; Max drawdown 45.8% (limit 25%) and only 77 trades (minimum 100), so the per-trade statistics are thin.; Random-entry perce… · [[idea-8]] plan `107403e1243d4fc9adc26df4dd5ee0bf2db83846488a91eb6fc8a757d535e12c`
- 2026-10-25T14:03:19Z **lesson** — Long/flat Donchian breakouts with ATR trailing exits on ETH 4h reproduce ETH beta with smaller drawdowns: they pass the robustness checks (smooth parameter surface, costs, delay, regimes) but lose to vol-targeted buy-and-hold and fall apart in walk-forward (OOS Sharpe 0.45). · [[idea-8]]
- 2026-10-25T14:03:19Z **lesson** — Passing robustness checks while failing the benchmark and walk-forward criteria is the signature of market exposure, not an edge. Screen out trend-filter ideas whose exposure profile resembles buy-and-hold before spending backtests on them. · [[idea-8]]
- 2026-10-25T14:03:19Z **decide** — abandoned: critic abandoned: It is ETH exposure, not an edge: net Sharpe 0.97 trails buy-and-hold (1.04) and volatility-targeted buy-and-hold (1.23). The Designer calls it a drawdown-reduced form of ETH beta.; Weak out of sample: the stitched walk-forward Sharpe is 0.45 over 9 folds against a bar of 1.0, while the best in-samp… · [[idea-8]] plan `107403e1243d4fc9adc26df4dd5ee0bf2db83846488a91eb6fc8a757d535e12c`
- 2026-10-25T14:05:10Z **design** — plan tested on the design window: Idea 9 (dual EMA plus trend-strength gate), BTCUSDT 4h. The registry has no ADX, efficiency-ratio or vol-percentile feature, so trend strength is approximated by (ema_fast - ema_slow)/atr &gt; 1.0. The 1.0 threshold was fixed a priori and never hand-tuned. 3 features, 3 tunables. Backtest (20/100/14): net Sharpe 1.23 vs vol-target B&amp;H 1.24 and B&amp;H 1.13; maxDD -52% vs -65% for vol-target; 58 trades; exposure 0.49; neighbourhood PBO 0.61; run-local DSR 0.85. Sweep (30 TPE trials; fast 10-40, slow 60… · [[idea-9]] [[trial-2669]] [[trial-2868]] [[trial-2869]] [[trial-2870]] [[trial-2871]] [[trial-2872]] [[trial-2873]] [[trial-2874]] [[run-r-20261025-135224-41f19f]] plan `74a87864a3b77bc45975e88706a6bb5d83c103e8660ece28004d433222862256`
- 2026-10-25T14:05:10Z **criteria** — design-window criteria (all but the holdout and cross-check) not met; pre-holdout gate (incl. the event-driven cross-check) not met; missing: require_holdout_pass · [[idea-9]] plan `74a87864a3b77bc45975e88706a6bb5d83c103e8660ece28004d433222862256`
- 2026-10-25T14:05:10Z **triage** — Sweep triage over 30 trials: 0 flagged suspicious (isolated spikes / low trade counts). · [[idea-9]] plan `74a87864a3b77bc45975e88706a6bb5d83c103e8660ece28004d433222862256`
- 2026-10-25T14:05:58Z **critic** — verdict abandon: No better than volatility-targeted buy-and-hold: net Sharpe 1.23 vs 1.24 (beat_vol_target fails). Beating plain buy-and-hold (1.13) comes only from 0.49 exposure, so this is trend-weighted beta, not timing skill.; Delay test flags it as suspicious: Sharpe is 1.23 / 1.21 / 1.25 / 1.14 at 0 / 1 / 3 / 5 bars of delay. The edge barely changes with timing, which points to beta capture, not a timing signal.; Walk-forward out-of-sample Sharpe for the behaviour family is 0.27 vs the 1.0 minimum. This plan's own walk-forward gave 0.65, and every fold fell back to the default parameters, so parameter s… · [[idea-9]] plan `74a87864a3b77bc45975e88706a6bb5d83c103e8660ece28004d433222862256`
- 2026-10-25T14:05:58Z **lesson** — On BTC 4h, gating an EMA crossover by (fast-slow)/ATR does not create a new hypothesis: it ties vol-targeted buy-and-hold, does not weaken with signal delay, and lands in the exhausted EMA-trend family. Stop spending proposals on EMA-trend variants for BTC. · [[idea-9]]
- 2026-10-25T14:05:58Z **lesson** — Report to the owner: every walk_forward fold returned selected=false with train_sharpe null and fell back to the defaults, and sweep/walk-forward trial IDs were not returned. Both look like tool behaviour worth checking. · [[idea-9]]
- 2026-10-25T14:05:58Z **decide** — abandoned: critic abandoned: No better than volatility-targeted buy-and-hold: net Sharpe 1.23 vs 1.24 (beat_vol_target fails). Beating plain buy-and-hold (1.13) comes only from 0.49 exposure, so this is trend-weighted beta, not timing skill.; Delay test flags it as suspicious: Sharpe is 1.23 / 1.21 / 1.25 / 1.14 at 0 / 1 / 3 /… · [[idea-9]] plan `74a87864a3b77bc45975e88706a6bb5d83c103e8660ece28004d433222862256`
- 2026-10-25T14:05:58Z **done-check** — done-check (advisory only, never extends a budget): done=None, decided by fallback

## End-of-run report

Run [[run-r-20261025-135224-41f19f]] · BTCUSDT, ETHUSDT 4h · started 2026-10-25T13:52:24Z · finished 2026-10-25T14:05:58Z

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
| Backtests (ledger trials) | 1029 | 3000 |  |
| Holdout checks | 0 | 1 |  |
| Model spend (USD) | 3.52 | 20.00 |  |

### Ledger size used for DSR

N = 2344: distinct plans tried across every research run in the ledger (this run is [[run-r-20261025-135224-41f19f]]), Optuna and neighbourhood variants included. Every Deflated Sharpe Ratio is deflated by this count, so starting a new run never resets it.

### Ideas tried

| idea | title (scout-sourced, untrusted) | status | reason |
|---|---|---|---|
| [[idea-5]] | Inverse-realized-variance exposure scaling on a long/flat trend base | abandoned | not expressible with registry features: Not expressible with the current lab, so no plan was built and no trials were used (only list_features and the free validate_plan). The idea depends on position size changing each bar as size = min(1, target_vol / realized_vol). realized_vol and ema exist in the registry, but plan.risk only accepts… |
| [[idea-6]] | Volatility-spike de-risking switch | abandoned | critic abandoned: Out-of-sample (walk-forward) net Sharpe is 0.41, against a required 1.0. The in-sample Sharpe of 1.22 came from tuning: the chosen parameters were unstable across the 9 folds, so the 'best' setting (EMA195 / ATR16 / ATR211) does not reliably come back when re-fitted on each fold.; The deflated Shar… |
| [[idea-7]] | Long/flat EMA trend filter on BTC and ETH | abandoned | [withheld: mentioned the holdout next to a number] |
| [[idea-8]] | Rolling-high breakout, long/flat (MAX rule) | abandoned | critic abandoned: It is ETH exposure, not an edge: net Sharpe 0.97 trails buy-and-hold (1.04) and volatility-targeted buy-and-hold (1.23). The Designer calls it a drawdown-reduced form of ETH beta.; Weak out of sample: the stitched walk-forward Sharpe is 0.45 over 9 folds against a bar of 1.0, while the best in-samp… |
| [[idea-9]] | Dual-MA trend with a trend-strength regime gate | abandoned | critic abandoned: No better than volatility-targeted buy-and-hold: net Sharpe 1.23 vs 1.24 (beat_vol_target fails). Beating plain buy-and-hold (1.13) comes only from 0.49 exposure, so this is trend-weighted beta, not timing skill.; Delay test flags it as suspicious: Sharpe is 1.23 / 1.21 / 1.25 / 1.14 at 0 / 1 / 3 /… |
| [[idea-10]] | BTC-state filter for ETH positions | new | not tested: run stopped first |
| [[idea-11]] | ATR trailing-stop circuit breaker with cooldown on a trend base | new | not tested: run stopped first |
| [[idea-12]] | Cross-sectional relative-strength rotation with a BTC gate (10-15 coins) | new | not tested: run stopped first |

### Failures and lessons

- [[idea-5]] Inverse-realized-variance exposure scaling on a long/flat trend base (abandoned): not expressible with registry features: Not expressible with the current lab, so no plan was built and no trials were used (only list_features and the free validate_plan). The idea depends on position size changing each bar as size = min(1, target_vol / realized_vol). realized_vol and ema exist in the registry, but plan.risk only accepts…
- [[idea-6]] Volatility-spike de-risking switch (abandoned): critic abandoned: Out-of-sample (walk-forward) net Sharpe is 0.41, against a required 1.0. The in-sample Sharpe of 1.22 came from tuning: the chosen parameters were unstable across the 9 folds, so the 'best' setting (EMA195 / ATR16 / ATR211) does not reliably come back when re-fitted on each fold.; The deflated Shar…
- [[idea-7]] Long/flat EMA trend filter on BTC and ETH (abandoned): [withheld: mentioned the holdout next to a number]
- [[idea-8]] Rolling-high breakout, long/flat (MAX rule) (abandoned): critic abandoned: It is ETH exposure, not an edge: net Sharpe 0.97 trails buy-and-hold (1.04) and volatility-targeted buy-and-hold (1.23). The Designer calls it a drawdown-reduced form of ETH beta.; Weak out of sample: the stitched walk-forward Sharpe is 0.45 over 9 folds against a bar of 1.0, while the best in-samp…
- [[idea-9]] Dual-MA trend with a trend-strength regime gate (abandoned): critic abandoned: No better than volatility-targeted buy-and-hold: net Sharpe 1.23 vs 1.24 (beat_vol_target fails). Beating plain buy-and-hold (1.13) comes only from 0.49 exposure, so this is trend-weighted beta, not timing skill.; Delay test flags it as suspicious: Sharpe is 1.23 / 1.21 / 1.25 / 1.14 at 0 / 1 / 3 /…
- done-check (advisory only, never extends a budget): done=None, decided by fallback
- Loop stopped: the hypothesis budget for this run was used.
- DSR N used by the success criteria: 2344 distinct plans across every research run (the global search count), not only this run's trials.
- Results are historical backtests on the design window and are not forward-looking. A candidate passed every frozen criterion, the locked holdout, and the event-driven cross-check; it becomes successful only after 4-8 weeks of forward paper trading consistent with its backtest.

### Candidates

None. No plan enters paper trading from this run.

### Disclaimer

Backtests are historical, not forward-looking. A candidate is not "successful" until it has passed every success criterion and 4-8 weeks of forward paper trading consistent with its backtest.
