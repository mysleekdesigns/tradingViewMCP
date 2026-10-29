---
run_id: "r-20261029-223426-f875f3"
date: 2026-10-29
symbols:
  - "ETHUSDT"
  - "SOLUSDT"
  - "NEARUSDT"
  - "AAVEUSDT"
  - "SUIUSDT"
  - "ONDOUSDT"
interval: "4h"
outcome: no_edge
idea_ids:
  - HC
criteria_sha256: "fc17a9c46c359fbc117b3f5ba3d141bc7e6a116511f154950a3c57746140e94c"
tags:
  - research
  - phase-8
---
# HC MACD+RSI on more coins: lab evidence for exploratory forward tests

Run [[run-r-20261029-223426-f875f3]] · spot, 4h, design window only.

**Purpose.** The owner asked (2026-10-29) to add AERO, ONDO, SUI, NEAR, HYPE, AAVE and SOL to the paper trading, alongside BTC and ETH, not instead of them.
- HC was abandoned in [[run-r-20261029-220805-606d9a]] (no edge on BTC). Its BTC exploratory forward test is [[2026-10-29-btcusdt-paper-hc-macd-rsi-btc-4h-exploratory]].
- This run is **not** a search for edge. It records the design-window evidence the paper runner needs to register one exploratory forward test per coin: a ledgered `run_backtest` and a walk-forward claim.
- Every forward test it feeds is implementation-only, with the locked split UNCHECKED. None can make HC a success candidate.

**Coins left out:**
- HYPEUSDT and AEROUSDT spot klines on `data.binance.vision` start 2026-09 and 2026-07. Both dates are after the design window ends (2025-10-01), so the lab has no design-window history for them. Using later data would read the locked holdout. They are therefore not tested here.
- ETHUSDT is added so that BTC and ETH are both covered, as the owner intended.

**Selection caveat.** The coin list was chosen in 2026 by the owner from coins that are popular now. That is hindsight (survivorship) selection. It is acceptable only because nothing here can become a candidate.

Holdout metrics are never written to this journal; only that a check was spent and the resulting status.

## Success criteria and budget (fixed before any backtest)

Criteria file sha256: `fc17a9c46c359fbc117b3f5ba3d141bc7e6a116511f154950a3c57746140e94c`, the same file as the previous run. It was frozen by `start_run` at 2026-10-29 22:34:26 UTC and is never relaxed mid-run.

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

## Pre-registered plans

Each coin gets the BTC HC plan unchanged except for `symbol`. That means MACD 12/26/9, RSI 14, an 8% stop, and the literals `0` and `50`:

```json
{"name":"HC MACD hist turn with RSI momentum state <SYM> 4h","symbol":"<SYM>","interval":"4h",
 "features":{"m":{"macd":{"fast":12,"slow":26,"signal":9}},"r":{"rsi":{"period":14}}},
 "long_entry":"crosses_above(m.hist, 0) and r > 50","long_exit":"crosses_below(m.hist, 0)",
 "risk":{"position_size_pct":100,"stop_loss_pct":8}}
```

- There is no sweep, and nothing is chosen per coin. The forward tests run the base params.
- Walk-forward uses the BTC run's pre-registered ranges: `m.fast` 8–16, `m.slow` 20–40, `m.signal` 6–12, `r.period` 10–20, `risk.stop_loss_pct` 4–12, with 40 trials per fold. Its stitched OOS Sharpe is the claim frozen into each paper protocol.
- Where a coin's history is too short for a walk-forward fold, that is logged. Such a coin is registered only if the runner accepts it without an owner-supplied claim.
- Outcome rule: the run is `no_edge` by construction unless some plan passes every pre-holdout criterion. Any such plan is reported to the owner and is **not** holdout-checked in this run.

## Log

- 2026-10-29 22:3x UTC. Downloaded 4h spot klines from `data.binance.vision` for SOL, NEAR, AAVE, SUI, ONDO, HYPE and AERO. They start 2020-08, 2020-10, 2020-10, 2023-05, 2025-04, 2026-09 and 2026-07. HYPE and AERO have no design-window bars, so they are excluded as pre-registered.
- Ran `run_backtest` on the six pre-registered plans (design window through 2025-10-01; each coin scored from its first bar; configured costs).

| coin | trial | net Sharpe (gross) | max DD | trades | random-entry pct | buy&hold / vol-target Sharpe | walk-forward OOS Sharpe |
|---|---|---|---|---|---|---|---|
| ETHUSDT | [[trial-6225]] | 0.75 (1.37) | −45% | 341 | 99.5 | 1.04 / 1.23 | **1.09** (9 folds, 215 trades) |
| SOLUSDT | [[trial-6258]] | 0.19 (0.89) | −74% | 271 | 49.5 | 1.28 / 1.43 | 0.22 (6 folds) |
| NEARUSDT | [[trial-6288]] | −0.17 (0.34) | −87% | 239 | 37 | 0.75 / 0.75 | −0.42 (5 folds) |
| AAVEUSDT | [[trial-6317]] | −0.04 (0.48) | −84% | 256 | 49.5 | 0.89 / 0.75 | −0.16 (5 folds) |
| SUIUSDT | [[trial-6346]] | 0.42 (0.89) | −48% | 123 | 81.5 | 0.89 / 0.73 | no fold fits (design window 2.4 years) |
| ONDOUSDT | [[trial-6370]] | −1.96 (−1.38) | −32% | 20 | 10.5 | 0.38 / 0.04 | no fold fits (design window 6 months) |

- Walk-forward used the pre-registered ranges with 40 trials per fold. There was no lineage widening, so no coin's plan merged into an earlier behaviour family.
- **`get_report` ETHUSDT: `pre_holdout_passed` is false.**
  - Passes: `wf_oos_sharpe_min` 1.09, `random_entry_percentile_min` 99.5, `min_trades`, regimes 3/3, neighbourhood 20/20.
  - Fails: `dsr_min` ≈ 0 (N 5811; MinBTL 24 years vs 6.75), `beat_buy_hold` (0.75 vs 1.04), `beat_vol_target` (0.75 vs 1.23), `max_drawdown_max` 0.45.
  - Missing: PBO (no sweep, by design), adversarial, and crosscheck. No further trials were spent on ETH, because four hard fails are already decisive.
  - The other coins are below the walk-forward bar and below random-entry percentile 95. No report was needed for them.
- **No holdout check is possible or spent.**
- Interpretation: HC's design-window record is mostly a BTC/ETH result. Across the four other coins with history, net Sharpe is −0.17 to 0.19 and the strategy is no better than random entry. ETH's walk-forward 1.09 is one coin out of seven chosen with hindsight. It does not change the HC verdict (abandoned).
- **Exploratory forward paper tests registered**, all implementation-only with the locked split UNCHECKED, each running from 2026-10-30 00:00 UTC to the decision bar on 2026-12-25:
  - [[2026-10-29-ethusdt-paper-hc-macd-rsi-eth-4h-exploratory]]: claim 1.09, entries [2, 14].
  - [[2026-10-29-solusdt-paper-hc-macd-rsi-sol-4h-exploratory]]: claim 0.22, entries [2, 14].
  - [[2026-10-29-nearusdt-paper-hc-macd-rsi-near-4h-exploratory]]: claim −0.42, entries [1, 14].
  - [[2026-10-29-aaveusdt-paper-hc-macd-rsi-aave-4h-exploratory]]: claim −0.16, entries [2, 15].
  - The BTC test from the previous run continues unchanged: [[2026-10-29-btcusdt-paper-hc-macd-rsi-btc-4h-exploratory]].
- `prepare` refused SUIUSDT and ONDOUSDT ("no walk_forward evidence"). As pre-registered, they were not registered with an owner-supplied `--claimed-sharpe`. That is the owner's call.

## End-of-run report

# Research run r-20261029-223426-f875f3 (2026-10-29; budget: 5 hypotheses / 15 proposals / 3000 backtests / 1 holdout check / $20)

**Outcome: no edge found**, by construction. This run collected forward-test evidence for HC, an already-abandoned logic, on more coins. It was not an edge search.

Used:
- 6 proposals;
- 1194 backtests (ledger N 5811 distinct plans across every run);
- 0 holdout checks;
- $0 of LLM budget.

Registered: 4 new exploratory forward tests (ETH, SOL, NEAR, AAVE), which makes 5 with BTC. None of them can become a success candidate.

Not registered:
- SUI and ONDO: no walk-forward fold fits their design windows.
- HYPE and AERO: no design-window history on Binance spot.

Lesson: one indicator rule applied across coins is mostly a bet on each coin's trend. Where the coin trended up with clean swings (BTC, ETH), it beats random entry. Elsewhere (SOL, NEAR, AAVE, ONDO) it is noise that costs push negative. Adding coins chosen today adds hindsight, not evidence.

Backtest results are historical simulations on Binance spot data, not forward-looking. The forward tests check implementation only.
