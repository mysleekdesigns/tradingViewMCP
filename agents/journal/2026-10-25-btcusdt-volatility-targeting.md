---
run_id: "r-20261025-125744-12221d"
date: 2026-10-25
symbols:
  - "BTCUSDT"
interval: "4h"
outcome: no_edge
idea_ids:
  - 1
  - 2
  - 3
  - 4
criteria_sha256: "cc544fe9302e2ffdfeb9af8d5c741753812f362e6e508dbd40f4113d55b72126"
tags:
  - research
  - phase-8
---
# volatility targeting

Run [[run-r-20261025-125744-12221d]] · BTCUSDT 4h

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

- 2026-10-25T12:58:47Z **design** — Session target: BTCUSDT 4h, family 'risk overlays and volatility targeting' (top of the PLAN.md list). Constraint found before scouting: plans have a fixed risk.position_size_pct and a long/flat spot engine, so continuous inverse-vol sizing cannot be expressed. Volatility targeting is tested here as binary vol-regime gating (long when realized vol is calm relative to its own history, flat when it is elevated), optionally combined with a trend filter. Every plan must also beat the lab's vol-targeted buy-and-hold benchmark, which is the continuous version of this idea, so a pure binary gate on buy-and-hold is expected to struggle. Idea library was empty at start (no dedupe hits possible).
- 2026-10-25T13:00:02Z **scout** — untrusted source text:

> [!warning] Untrusted web content (scout) — data, not instructions
> Source: `https://www.nber.org/papers/w22208 ; https://www.anserpress.org/journal/fel/5/2/53`
>
> Scout summary (strategy-scout subagent, two quick web searches, no pages fetched; evidence ranking mostly from background knowledge, unverified this session).
> Headline from scout: no peer-reviewed evidence found that vol-managed or vol-gated rules work on BTC. One 2026 article snippet reports that vol-targeted EMA trend on BTC shows no significant variation across vol regimes and suggests cutting exposure on vol spikes may suit BTC poorly. Moreira-Muir gains come mostly from continuous scaling, which a fixed-size long/flat engine removes.
> Ranked hypotheses: (1) trend filter with vol-spike gate; (2) low-vol regime gate, vol-managed on/off, as a control; (3) vol compression then Donchian breakout with trend exit; (4) ATR Chandelier-style exit on a trend entry; (5) regime_state plus vol ratio; (6) vol spike as exit only; (7) Markov regime signal gated by vol; (8) z-score pullback in calm uptrend.
> Sources: nber.org/papers/w22208 (Moreira and Muir 2017), anserpress.org/journal/fel/5/2/53 (snippet, caveat), eco3min.fr (context, low quality), tradingview script qiYGOp28 (idea only). Dropped: marketplace product pages (mql5, luxalgo) as hype. No injected instructions found in any source.
>

- 2026-10-25T13:03:48Z **design** — Four ideas added to the library from the scout pass (1 low-vol gate control, 2 trend filter with vol-spike gate, 3 vol compression Donchian breakout, 4 trend entry with ATR chandelier exit). Scout items 5 and 7 (regime_state / regime_signal plus vol ratio) were judged near-duplicates of idea 2; items 6 and 8 weaker variants. Defaults chosen a priori on a 4h scale: 30 bars ~5d, 540 ~90d, 300 ~50d, 132 ~22d. Only rule literal: the chandelier 3 (standard). · [[idea-1]] [[idea-2]] [[idea-3]] [[idea-4]]
- 2026-10-25T13:03:49Z **backtest** — Idea 1 control vt-lowvol-gate at defaults: net Sharpe 0.41 (gross 0.81), max DD 75%, 187 trades, random-entry percentile 7.5. Binary vol gating alone is worse than random entry and far below vol-targeted buy-and-hold (1.24). Confirms the scout caveat. · [[idea-1]] [[trial-440]] [[run-r-20261025-125744-12221d]] plan `02d5ea578e28b2679d578069bd56049216255b6c1952634e19ec46786365175a`
- 2026-10-25T13:03:49Z **backtest** — Idea 2a vt-trend-volgate at defaults: net Sharpe 0.48 (gross 1.07), DD 55%, 237 trades. Exits on every vol-ratio flicker; costs eat half the edge. · [[idea-2]] [[trial-461]] [[run-r-20261025-125744-12221d]] plan `c8b74889c98ad843810834b86a06ae7220516022498899c73eae89f20c6b689c`
- 2026-10-25T13:03:49Z **backtest** — Idea 3 vt-squeeze-breakout at defaults: net Sharpe 1.09, DD 41%, only 42 trades, random-entry percentile 96.5. Sweep 120 trials: best in-sample 1.75 (biased). Walk-forward (60 trials/fold): stitched OOS Sharpe 0.79, 36 OOS trades; in 7 of 9 folds no train params reached 30 trades so defaults were kept. Fails WF, trade count, DD. · [[idea-3]] [[trial-486]] [[run-r-20261025-125744-12221d]] plan `dbf194dd23a47f6ae9ccdfcd51477ef30ced4c51b94bc7043a1d70714b173790`
- 2026-10-25T13:03:49Z **backtest** — Idea 4 vt-chandelier-trend at defaults: net Sharpe 0.18, exposure 5%, 48 trades. Design error: 3x ATR is a daily-bar convention; 3x 4h ATR is about 1.2 daily ATR so the exit fires almost at once. Not re-tuned (changing the literal would be literal-tuning). Lesson: scale ATR multipliers by sqrt(bars per day) when porting daily rules to intraday bars, decided before the first backtest. · [[idea-4]] [[trial-515]] [[run-r-20261025-125744-12221d]] plan `b48353da3eb078786f02ff28241eb6f6f3b94db7b0ddcd08d2a25d69d351d6d2`
- 2026-10-25T13:03:50Z **design** — Idea 2 refined (logic change, not params): vol gates entries only, trend alone exits (2b vt-volgated-entry-trend-exit). · [[idea-2]]
- 2026-10-25T13:03:50Z **backtest** — Idea 2b at defaults: net Sharpe 1.31 vs buy-and-hold 1.13 and vol-target 1.24; 149 trades; random-entry percentile 99.5; all three regimes profitable; but max DD 47%. · [[idea-2]] [[trial-1209]] [[run-r-20261025-125744-12221d]] plan `045962b5169aa19542faba8dfeef1103ac0cfc86ffacec36d0118c4fb7e002be`
- 2026-10-25T13:03:51Z **backtest** — Idea 2c vt-volgated-trend-size40: 2b at 40% position size, chosen a priori as a static vol target (25% annual / ~62% BTC vol). DD only fell to 29.8% (losing trades compound) and net Sharpe 1.23 dropped below vol-target 1.24. Not shrunk further: a second size picked to clear the DD bar would be fitting the criterion. · [[idea-2]] [[trial-1234]] [[run-r-20261025-125744-12221d]] plan `6edba3b5cda848809f0c101a19e1559aa4c38bbb7e4b02bd6bf21a7c098ba0fb`
- 2026-10-25T13:04:13Z **backtest** — Idea 2b sweep (100 trials): best in-sample 1.42 at 36/528/282, close to defaults; sweep CSCV PBO 0.44. Walk-forward (lab required EMA range 11-1000 because this behaviour family had already tried 11-978 in earlier runs): stitched OOS Sharpe 0.27, OOS DD 64%. Train Sharpes near 2 led to negative test Sharpes in folds 0-3 and 8. Adversarial: gradual delay decay, positive at x2 and x4 costs, permutation null beaten. · [[idea-2]] [[trial-1818]] [[run-r-20261025-125744-12221d]] plan `045962b5169aa19542faba8dfeef1103ac0cfc86ffacec36d0118c4fb7e002be`
- 2026-10-25T13:04:13Z **report** — get_report for 2b fails wf_oos_sharpe (0.27 vs 1.0), dsr (about 0 at global N 1480), pbo (0.44 vs 0.2) and max_drawdown (0.47 vs 0.25); passes both benchmarks, random entry, cost stress, delay decay, trades, regimes, neighbours. · [[idea-2]] plan `045962b5169aa19542faba8dfeef1103ac0cfc86ffacec36d0118c4fb7e002be`
- 2026-10-25T13:04:13Z **decide** — Not every pre-locked-window criterion passes, so no locked check is reachable for idea 2; the event-driven cross-check was skipped to save budget. · [[idea-2]]
- 2026-10-25T13:04:13Z **critic** — Critic (fresh context) on ideas 1, 3, 4: abandon all three. Idea 1 is worse than random timing (percentile 7.5) and its continuous version is the benchmark itself. Idea 3 rests on 42 trades (about 4 per fold), the tuner fell back to defaults in 7 of 9 folds, the sweep optimum sits at range edges, and OOS Sharpe 0.79 is below buy-and-hold with 41% DD. Idea 4 was mis-specified at design time and cannot be repaired honestly without literal-tuning. Optional refinement offered for idea 3 (vol-expansion exit, pre-registered kill conditions) was declined: idea 2a already showed vol-ratio exits churn, the prior is very low, and every extra plan raises the DSR bar for later research. · [[idea-1]] [[idea-3]] [[idea-4]]
- 2026-10-25T13:06:45Z **critic** — Critic (fresh context) on idea 2 family (2a, 2b, 2c as one hypothesis): abandon. The 1.31 default Sharpe came from defaults that sit on the in-sample optimum of a famous BTC rule (50-day trend filter), so they are hindsight, not a prior. Every test that removes that choice fails: tuned walk-forward 0.27 with an inverted train-to-test ranking, sweep PBO 0.44, DSR about 0. The lab already groups the plan with earlier EMA-trend plans, and the vol gate never showed marginal value (as an exit it cut Sharpe 1.31 to 0.48). Unmeasured: top-5-trade removal and a plain close &gt; EMA(300) control. · [[idea-2]]
- 2026-10-25T13:06:45Z **design** — Checked a critic query on 2c: drawdown 29.8% at 40% size exceeds the constant-fraction bound (about 22%). Not a lab bug: lab/engine/backtest.py sizes position_size_pct of cash at entry and never rebalances inside a trade, so BTC's share of equity drifts up on big winners. A fixed size_pct is not a static vol target on this engine. · [[idea-2]]
- 2026-10-25T13:06:45Z **decide** — Run stopped with 9 proposals and 1 hypothesis slot unused. Remaining scout ideas are variants of the vol-gate component that failed in ideas 1 and 2, and each extra plan raises the DSR bar for later research. No locked check was spent.

## End-of-run report

Run [[run-r-20261025-125744-12221d]] · BTCUSDT 4h · started 2026-10-25T12:57:44Z · finished 2026-10-25T13:06:45Z

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
| Hypotheses | 4 | 5 |  |
| LLM proposals | 6 | 15 |  |
| Backtests (ledger trials) | 1386 | 3000 |  |
| Holdout checks | 0 | 1 |  |
| Model spend (USD) | 0.00 | 20.00 |  |

### Ledger size used for DSR

N = 1480: distinct plans tried across every research run in the ledger (this run is [[run-r-20261025-125744-12221d]]), Optuna and neighbourhood variants included. Every Deflated Sharpe Ratio is deflated by this count, so starting a new run never resets it.

### Ideas tried

| idea | title (scout-sourced, untrusted) | status | reason |
|---|---|---|---|
| [[idea-1]] | Pure low-vol gate (control) | abandoned | Net Sharpe 0.41, DD 75%, worse than random entry (percentile 7.5); the continuous version is the vol-target benchmark itself. |
| [[idea-2]] | Trend filter with vol-spike gate | abandoned | Best design-window result (2b, net Sharpe 1.31) was hindsight defaults; tuned walk-forward OOS Sharpe 0.27, PBO 0.44, DSR about 0, DD 47% (29.8% at 40% size, then below vol-target). Vol gate adds no shown value over a plain EMA trend filter. |
| [[idea-3]] | Vol compression then Donchian breakout | abandoned | Walk-forward OOS Sharpe 0.79, 36 OOS trades (42 on the design window), DD 41%; below both benchmarks; optimiser fell back to defaults in 7 of 9 folds. |
| [[idea-4]] | Trend entry with ATR chandelier exit | abandoned | Mis-specified: a daily 3x ATR multiplier on 4h ATR exits almost at once (5% exposure, net Sharpe 0.18). Not repairable without literal-tuning. |

### Failures and lessons

- [[idea-1]] Pure low-vol gate (control) (abandoned): Net Sharpe 0.41, DD 75%, worse than random entry (percentile 7.5); the continuous version is the vol-target benchmark itself.
- [[idea-2]] Trend filter with vol-spike gate (abandoned): Best design-window result (2b, net Sharpe 1.31) was hindsight defaults; tuned walk-forward OOS Sharpe 0.27, PBO 0.44, DSR about 0, DD 47% (29.8% at 40% size, then below vol-target). Vol gate adds no shown value over a plain EMA trend filter.
- [[idea-3]] Vol compression then Donchian breakout (abandoned): Walk-forward OOS Sharpe 0.79, 36 OOS trades (42 on the design window), DD 41%; below both benchmarks; optimiser fell back to defaults in 7 of 9 folds.
- [[idea-4]] Trend entry with ATR chandelier exit (abandoned): Mis-specified: a daily 3x ATR multiplier on 4h ATR exits almost at once (5% exposure, net Sharpe 0.18). Not repairable without literal-tuning.
- Interactive Claude Code session: model spend is not metered by the lab, so USD is recorded as 0.
- On a fixed-size long/flat engine, volatility targeting can only be a binary gate, and the lab benchmarks against continuous vol-targeted buy-and-hold (Sharpe 1.24 on this window). No binary gate tested here beat it out of sample.
- Defaults that match a famous rule (50-day trend filter on BTC) are hindsight, not a prior; judge them by tuned walk-forward only. Run the plain-filter control first next time.
- Port daily conventions to intraday bars before the first backtest (ATR multiples scale with sqrt(bars per day)); a wrong literal cannot be fixed afterwards.
- position_size_pct is committed at entry and not rebalanced inside a trade, so it is not a static vol target; drawdown does not scale linearly with size.
- Squeeze breakouts on long lookbacks give too few trades on one symbol at 4h (about 40 in 6.75 years) to judge.

### Candidates

None. No plan enters paper trading from this run.

### Disclaimer

Backtests are historical, not forward-looking. A candidate is not "successful" until it has passed every success criterion and 4-8 weeks of forward paper trading consistent with its backtest.
