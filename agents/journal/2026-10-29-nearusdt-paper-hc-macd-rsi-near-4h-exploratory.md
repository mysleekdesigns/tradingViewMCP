---
candidate: "hc-macd-rsi-near-4h-exploratory"
plan_hash: "4e6a23d73f899b2e37e5efa4f095a2a5a87653ef8bfa110b9d43ff839839a0fc"
logic_hash: "4085e107d0952b2578a3162aa76f6e7c9af4186d391be6498c90be9ca56d8ac3"
run_id: "r-20261029-223426-f875f3"
trial_id: 6288
date: "2026-10-29"
symbol: "NEARUSDT"
interval: "4h"
protocol_sha256: "fad112aaf87f5e0ed0d3c0529256abd32a8606d14a437f7b74baeb7e5c28d1d4"
mode: "implementation_only"
locked_split_check: "unchecked"
outcome: "pending"
tags: [paper-trade]
---

# Paper-trade protocol: hc-macd-rsi-near-4h-exploratory (NEARUSDT 4h)

Registered 2026-10-29T22:37:13+00:00 before the first dry-run trade. Venue: freqtrade `--dry-run` (paper only; no exchange keys, no orders). The protocol below is frozen: `paper_trade.py compare` refuses to judge if `protocol.json` no longer matches the sha256 above, only appends to the verdicts section, and writes the first final verdict into `outcome` once.

## Registered protocol (frozen before the first dry-run trade)

- Plan `4e6a23d73f899b2e37e5efa4f095a2a5a87653ef8bfa110b9d43ff839839a0fc` (logic `4085e107d095`); protocol sha256 `fad112aaf87f5e0ed0d3c0529256abd32a8606d14a437f7b74baeb7e5c28d1d4`; config sha256 `105fbbd5b0bdd58923fb014c3c20c035767241e936754caa96b31596076e0e33`.
- Ledger: research-run design-window trial 6288 of run r-20261029-223426-f875f3 (2020-10-14 04:00 UTC to 2025-10-01 00:00 UTC); walk-forward evidence ids [143].
- Candles: api (data-api.binance.vision), 200 warm-up bars; data grace 7 days.
- Mode: **implementation_only**. Verdicts: pass / fail / continue / inconclusive.
- Expectations from the lab backtest of the plan on the design window 2020-10-14 04:00 UTC to 2025-10-01 00:00 UTC (10877 bars, 1813 days); 251 rolling design blocks (32 non-overlapping), each scored flat at its start.
- Honest false-fail: single-path estimate 5.8% (can be off in either direction; excludes regime change; simulated range on a stationary random walk ~0%-10%); leave-overlap-out bootstrap of design blocks, neither a floor nor a bound.
- Locked-split check: UNCHECKED: the lab's locked-split check was never run for this behaviour family. A pass here means 'implementation consistent, but NOT a success candidate'; a forward test cannot replace the lab's check.
- Ledger read at registration: `/Users/simonlacey/Documents/GitHub/tradingView/lab/data/ledger.db` (the lab ledger); run r-20261029-223426-f875f3 chain head `fdc98ce8d81719917d42d0451800d91569b0867efd4448a39822f42e46679caa`.

| Check | Expected | Tolerance |
| --- | --- | --- |
| Entries over 56.0 days | 7.4 | [1, 14] (Poisson [3, 13], design blocks [2, 12], predictive [1, 14], 95%) |
| Exposure (bars in market) | 0.213 | [0.011, 0.425] (design blocks [0.054, 0.405], predictive [0.031, 0.383], +/- 0.02) |
| Timing vs the lab replay | every entry and exit (any reason) on the lab's fill bar | +/-1 bar(s) to match; irregular fills (unmatched paper or lab entries and exits, plus matched fills off the lab's bar) at most max(1, floor(0.05 x all fills)) (late = possible look-ahead in the lab, early = possible use of the still-forming candle) |
| Implementation shortfall (lag-0 entries and signal exits) | -5.0 bps for an honest open fill vs the modelled fill (fee 10.0 + slippage 5.0 bps per side) | overall median in [-20.0, 5.0] bps (P false fail 0.20%); weekly / 3-fill median > 20.0 bps fails (union bound 0.52%) |
| Claimed Sharpe (annualised, 365 d) | -0.42 (walk-forward OOS) | MinTRL undefined d at 95%; undefined d at 80% power; smallest testable 6.20 |

### Decision rules

1. Clock: the test starts at 2026-10-30 00:00 UTC; the decision bar closes at 2026-12-25 00:00 UTC (56.0 days, 336 bars). Before that, compare says continue unless a fail rule trips.
2. Data: candles come from public API data-api.binance.vision (frozen; compare refuses another source) with 200 warm-up bars before the start: the lab replay and the generated bot both compute every indicator from 2026-09-26 16:00 UTC (WARM_FROM 1790438400, rendered into the strategy; compare refuses a strategy file with another one), so recursive features start from the same bar, and the bot enters only on signal bars from the start (START 1793318400, also rendered; a bot launched earlier holds nothing into the test). A verdict at the decision bar needs every bar time in [start, decision bar) and a candle for every paper fill. With data missing, a rule still fails when it fails whatever the missing bars hold (exposure bounded by held/N and (held + missing)/N; entries, timing and shortfall do not depend on bars without fills); otherwise compare says continue ('incomplete data: N of M bars, K fills unjudged'), and 7 days after the decision bar the verdict is a final inconclusive ('data'). The first final verdict (fail at any time; pass, fail or inconclusive at the decision bar) is written once into the journal frontmatter; later compare runs never change it.
3. Entries: dry-run entries over the decision horizon must lie in [1, 14] (expected 7.4 from 239 design trades; union of Poisson [3, 13], design blocks [2, 12] and the predictive interval [1, 14] from 32 non-overlapping blocks); more than 14 at any time fails at once.
4. Exposure: the share of bars holding a position at bar close must lie in [0.011, 0.425] at the decision bar (design blocks scored flat at their start; quantiles [0.054, 0.405] united with the predictive interval [0.031, 0.383], +/- 0.02); bars in market above 0.425 x 336 at any time fail at once.
5. Timing: the lab engine replays the plan on the same candles (frozen warm-up, flat at the start, signals at bar close, fills at the next open). Paper entries are matched one-to-one to lab entries, and paper exits of every reason to lab exits of the same kind (signal, stop-loss, take-profit/ROI), within +/-1 bar(s); a stop or ROI exit where the lab exited on a signal is unmatched. The bot runs the lab's own compile_plan on closed candles from the same WARM_FROM as the replay, so by construction an honest bot fills on the lab's fill bars unless the two candle feeds differ. At the decision bar ONE count of irregular fills (unmatched paper entries + unmatched lab entries + unmatched paper exits + unmatched lab exits + matched fills off the lab's fill bar) must be at most max(1, floor(0.05 x (all lab fills + all paper fills))); the one allowance covers a bar on which the exchange feed and the comparison's candles disagree. A skipped, extra or far-shifted trade is 2-4 irregular fills, a 1-bar shift 1 per fill. A late paper fill means the backtest acted earlier than live: possible look-ahead in the lab; an early one means the bot acted before the bar closed: possible use of the still-forming candle; an unmatched paper fill means the bot traded where the lab did not (or more than the match tolerance off its bar): it is not running the plan as backtested; an unmatched lab fill means the lab traded where the bot did not (a skipped or far-shifted trade): the backtest includes trades live did not make. Settled unmatched paper entries above max(1, floor(0.05 x 4 x 14)), the most the decision bar could allow, fail at once.
6. Implementation shortfall: judged on lag-0 entries and signal exits only (their lab fill is the next bar's open x (1 +/- modelled slippage), fee 10.0 bps); stop-loss and take-profit fills are at a level in the lab, so their timing is matched but their price is not judged. At the decision bar the median of all judged fills must be at most 5.0 bps against the modelled fill (drift), and the median against the raw bar open must be at least -20.0 bps (below that is implausibly favourable: possible look-ahead, e.g. fills at the signal bar's open). Weekly medians (weeks with >= 3 fills) above 5.0 bps are alerts only; a week whose median exceeds 4 x 5.0 = 20.0 bps is gross breakage and fails at once, and so is any run of 3 consecutive judged fills (rolling, any time) whose median exceeds 20.0 bps.
7. False-fail arithmetic (normal approximation of the median): an honest bot fills at the open, so mu = -5.0 bps, with an assumed fill noise sigma = 10 bps. Expected judged fills 13.7, taken as n = 13: sd = sqrt(pi/2) x 10 / sqrt(13) = 3.48 bps; P(median > tol) = 1 - Phi((5.0 - (-5.0)) / 3.48) = 0.20%; the favourable side is judged against the raw bar open, where an honest fill has mean 0 whatever the vol-scaled slippage: P(median vs open < -gross) = Phi(-20.0 / 3.48) = 0.000%. Gross breakage per week at 3 fills: sd = 7.24 bps, P = 1 - Phi((20.0 - (-5.0)) / 7.24) = 0.028%; over 8 weekly looks 1 - (1 - P)^8 = 0.220%. Rolling windows of 3 consecutive fills: n - 3 + 1 = 13 - 3 + 1 = 11 windows at the same P, union bound min(1, 11 x P) = 0.303%; every early gross look together (weeks + windows), min(1, (8 + 11) x P) = 0.523%.
8. Honest false-fail, single-path estimate 5.8% (can be off in either direction; excludes regime change; simulated range on a stationary random walk ~0%-10%) (leave-overlap-out bootstrap: 2000 design blocks at random starts, seed 20261028, each judged as the forward period of an honest bot against bands rebuilt without the design blocks that overlap it; by rule: entries 2.7%, exposure 3.1%, shortfall 0.7%, Sharpe 0.0%). It is neither a floor nor a bound, and a fail on entries or exposure alone is reported as a possible regime difference.
9. Sharpe NOT tested: the claimed Sharpe -0.42 annualised (walk-forward OOS: minimum stitched walk-forward OOS Sharpe over this plan's own 1 result(s) in the ledger (evidence ids 143); the lab's wf_oos_sharpe_min criterion uses the whole behaviour family and can be lower) needs a track record of undefined days (MinTRL at 95%; undefined days with 80% power), beyond the 56-day maximum. The smallest annualised Sharpe the 56-day maximum can test (at 95% confidence, 80% power, design moments) is 6.20. This forward test can only check implementation (entries, exposure, timing, shortfall); a pass means 'implementation consistent with the backtest', never 'Sharpe confirmed'.
10. Locked-split check: UNCHECKED: the lab's locked-split check was never run for this behaviour family. A pass here means 'implementation consistent, but NOT a success candidate'; a forward test cannot replace the lab's check.
11. Verdicts: fail if any rule fails (a fail on entries/exposure while every fill matched the lab at lag 0 is labelled a regime difference, timing/shortfall fails an implementation difference); continue before the decision bar or while data may still arrive; inconclusive at the decision bar when every check is inside tolerance but the protocol cannot discriminate (the reasons above, or fewer than 3 judged fills) or the data never completed; pass otherwise. The protocol is frozen: a changed protocol needs a new candidate name, and a plan (or its logic) already registered under another name is refused.

### Frozen protocol JSON

```json
{
  "bootstrap": {
    "draws": 2000,
    "entries_rate": 0.027,
    "exposure_rate": 0.031,
    "fail_rate": 0.058,
    "seed": 20261028,
    "sharpe_rate": 0.0,
    "shortfall_rate": 0.007
  },
  "candidate": "hc-macd-rsi-near-4h-exploratory",
  "config_sha256": "105fbbd5b0bdd58923fb014c3c20c035767241e936754caa96b31596076e0e33",
  "created_at": "2026-10-29T22:37:13+00:00",
  "data": {
    "candle_host": "data-api.binance.vision",
    "candle_source": "api",
    "grace_days": 7.0,
    "warm_from": 1790438400,
    "warmup_bars": 200
  },
  "decision_bars": 336,
  "decision_days": 56.0,
  "decision_end": 1798156800,
  "design_blocks": 251,
  "exposure": {
    "confidence": 0.95,
    "empirical_hi": 0.40476190476190477,
    "empirical_lo": 0.05357142857142857,
    "expected": 0.21274248414084765,
    "hi": 0.4247619047619048,
    "lo": 0.010928998761564546,
    "max_band_width": 0.8,
    "predictive_hi": 0.38329505739476255,
    "predictive_lo": 0.030928998761564547,
    "slack": 0.02
  },
  "inconclusive_reasons": [],
  "interval": "4h",
  "locked_split_check": "unchecked",
  "logic_hash": "4085e107d0952b2578a3162aa76f6e7c9af4186d391be6498c90be9ca56d8ac3",
  "matching": {
    "irregular_share": 0.05,
    "tolerance_bars": 1
  },
  "max_days": 56.0,
  "min_days": 28.0,
  "mode": "implementation_only",
  "plan_hash": "4e6a23d73f899b2e37e5efa4f095a2a5a87653ef8bfa110b9d43ff839839a0fc",
  "rules": [
    "Clock: the test starts at 2026-10-30 00:00 UTC; the decision bar closes at 2026-12-25 00:00 UTC (56.0 days, 336 bars). Before that, compare says continue unless a fail rule trips.",
    "Data: candles come from public API data-api.binance.vision (frozen; compare refuses another source) with 200 warm-up bars before the start: the lab replay and the generated bot both compute every indicator from 2026-09-26 16:00 UTC (WARM_FROM 1790438400, rendered into the strategy; compare refuses a strategy file with another one), so recursive features start from the same bar, and the bot enters only on signal bars from the start (START 1793318400, also rendered; a bot launched earlier holds nothing into the test). A verdict at the decision bar needs every bar time in [start, decision bar) and a candle for every paper fill. With data missing, a rule still fails when it fails whatever the missing bars hold (exposure bounded by held/N and (held + missing)/N; entries, timing and shortfall do not depend on bars without fills); otherwise compare says continue ('incomplete data: N of M bars, K fills unjudged'), and 7 days after the decision bar the verdict is a final inconclusive ('data'). The first final verdict (fail at any time; pass, fail or inconclusive at the decision bar) is written once into the journal frontmatter; later compare runs never change it.",
    "Entries: dry-run entries over the decision horizon must lie in [1, 14] (expected 7.4 from 239 design trades; union of Poisson [3, 13], design blocks [2, 12] and the predictive interval [1, 14] from 32 non-overlapping blocks); more than 14 at any time fails at once.",
    "Exposure: the share of bars holding a position at bar close must lie in [0.011, 0.425] at the decision bar (design blocks scored flat at their start; quantiles [0.054, 0.405] united with the predictive interval [0.031, 0.383], +/- 0.02); bars in market above 0.425 x 336 at any time fail at once.",
    "Timing: the lab engine replays the plan on the same candles (frozen warm-up, flat at the start, signals at bar close, fills at the next open). Paper entries are matched one-to-one to lab entries, and paper exits of every reason to lab exits of the same kind (signal, stop-loss, take-profit/ROI), within +/-1 bar(s); a stop or ROI exit where the lab exited on a signal is unmatched. The bot runs the lab's own compile_plan on closed candles from the same WARM_FROM as the replay, so by construction an honest bot fills on the lab's fill bars unless the two candle feeds differ. At the decision bar ONE count of irregular fills (unmatched paper entries + unmatched lab entries + unmatched paper exits + unmatched lab exits + matched fills off the lab's fill bar) must be at most max(1, floor(0.05 x (all lab fills + all paper fills))); the one allowance covers a bar on which the exchange feed and the comparison's candles disagree. A skipped, extra or far-shifted trade is 2-4 irregular fills, a 1-bar shift 1 per fill. A late paper fill means the backtest acted earlier than live: possible look-ahead in the lab; an early one means the bot acted before the bar closed: possible use of the still-forming candle; an unmatched paper fill means the bot traded where the lab did not (or more than the match tolerance off its bar): it is not running the plan as backtested; an unmatched lab fill means the lab traded where the bot did not (a skipped or far-shifted trade): the backtest includes trades live did not make. Settled unmatched paper entries above max(1, floor(0.05 x 4 x 14)), the most the decision bar could allow, fail at once.",
    "Implementation shortfall: judged on lag-0 entries and signal exits only (their lab fill is the next bar's open x (1 +/- modelled slippage), fee 10.0 bps); stop-loss and take-profit fills are at a level in the lab, so their timing is matched but their price is not judged. At the decision bar the median of all judged fills must be at most 5.0 bps against the modelled fill (drift), and the median against the raw bar open must be at least -20.0 bps (below that is implausibly favourable: possible look-ahead, e.g. fills at the signal bar's open). Weekly medians (weeks with >= 3 fills) above 5.0 bps are alerts only; a week whose median exceeds 4 x 5.0 = 20.0 bps is gross breakage and fails at once, and so is any run of 3 consecutive judged fills (rolling, any time) whose median exceeds 20.0 bps.",
    "False-fail arithmetic (normal approximation of the median): an honest bot fills at the open, so mu = -5.0 bps, with an assumed fill noise sigma = 10 bps. Expected judged fills 13.7, taken as n = 13: sd = sqrt(pi/2) x 10 / sqrt(13) = 3.48 bps; P(median > tol) = 1 - Phi((5.0 - (-5.0)) / 3.48) = 0.20%; the favourable side is judged against the raw bar open, where an honest fill has mean 0 whatever the vol-scaled slippage: P(median vs open < -gross) = Phi(-20.0 / 3.48) = 0.000%. Gross breakage per week at 3 fills: sd = 7.24 bps, P = 1 - Phi((20.0 - (-5.0)) / 7.24) = 0.028%; over 8 weekly looks 1 - (1 - P)^8 = 0.220%. Rolling windows of 3 consecutive fills: n - 3 + 1 = 13 - 3 + 1 = 11 windows at the same P, union bound min(1, 11 x P) = 0.303%; every early gross look together (weeks + windows), min(1, (8 + 11) x P) = 0.523%.",
    "Honest false-fail, single-path estimate 5.8% (can be off in either direction; excludes regime change; simulated range on a stationary random walk ~0%-10%) (leave-overlap-out bootstrap: 2000 design blocks at random starts, seed 20261028, each judged as the forward period of an honest bot against bands rebuilt without the design blocks that overlap it; by rule: entries 2.7%, exposure 3.1%, shortfall 0.7%, Sharpe 0.0%). It is neither a floor nor a bound, and a fail on entries or exposure alone is reported as a possible regime difference.",
    "Sharpe NOT tested: the claimed Sharpe -0.42 annualised (walk-forward OOS: minimum stitched walk-forward OOS Sharpe over this plan's own 1 result(s) in the ledger (evidence ids 143); the lab's wf_oos_sharpe_min criterion uses the whole behaviour family and can be lower) needs a track record of undefined days (MinTRL at 95%; undefined days with 80% power), beyond the 56-day maximum. The smallest annualised Sharpe the 56-day maximum can test (at 95% confidence, 80% power, design moments) is 6.20. This forward test can only check implementation (entries, exposure, timing, shortfall); a pass means 'implementation consistent with the backtest', never 'Sharpe confirmed'.",
    "Locked-split check: UNCHECKED: the lab's locked-split check was never run for this behaviour family. A pass here means 'implementation consistent, but NOT a success candidate'; a forward test cannot replace the lab's check.",
    "Verdicts: fail if any rule fails (a fail on entries/exposure while every fill matched the lab at lag 0 is labelled a regime difference, timing/shortfall fails an implementation difference); continue before the decision bar or while data may still arrive; inconclusive at the decision bar when every check is inside tolerance but the protocol cannot discriminate (the reasons above, or fewer than 3 judged fills) or the data never completed; pass otherwise. The protocol is frozen: a changed protocol needs a new candidate name, and a plan (or its logic) already registered under another name is refused."
  ],
  "sharpe": {
    "benchmark_annual": 0.0,
    "benchmark_per_bar": 0.0,
    "claim_note": "minimum stitched walk-forward OOS Sharpe over this plan's own 1 result(s) in the ledger (evidence ids 143); the lab's wf_oos_sharpe_min criterion uses the whole behaviour family and can be lower",
    "claim_source": "walk_forward_oos",
    "claimed_annual": -0.4165543631485333,
    "claimed_per_bar": -0.008901221953415358,
    "confidence": 0.95,
    "decision_trl_bars": null,
    "decision_trl_days": null,
    "kurtosis": 39.65587018230051,
    "min_testable_annual": 6.196068444404743,
    "min_trl_bars": null,
    "min_trl_days": null,
    "power": 0.8,
    "skew": 1.6581344426563607,
    "testable": false
  },
  "shortfall": {
    "expected_fills": 13.746437436793236,
    "false_fail_favourable": 4.367428468666331e-09,
    "false_fail_gross_any_week": 0.002199529472051487,
    "false_fail_gross_any_window": 0.0030272673484415557,
    "false_fail_gross_early": 0.0052289163291263235,
    "false_fail_gross_week": 0.000275206122585596,
    "false_fail_overall": 0.0020085634537899692,
    "fee_rate": 0.001,
    "fill_noise_bps": 10.0,
    "gross_bps": 20.0,
    "gross_breakage_multiple": 4.0,
    "honest_mean_bps": -5.0,
    "min_fills": 3,
    "modelled_fill_cost_bps": 15.0,
    "overall_n": 13,
    "overall_sd_bps": 3.4760679895038082,
    "round_trip_cost_bps": 30.0,
    "slippage_cap": 0.01,
    "slippage_rate": 0.0005,
    "slippage_vol_mult": 0.1,
    "tolerance_bps": 5.0,
    "tolerance_mult": 1.0,
    "weekly_sd_bps": 7.236012545582676,
    "weeks": 8,
    "windows_expected": 11
  },
  "source": {
    "bars": 10877,
    "chain_head": "fdc98ce8d81719917d42d0451800d91569b0867efd4448a39822f42e46679caa",
    "days": 1812.8333333333333,
    "kind": "design_window_backtest",
    "ledger_is_default": true,
    "ledger_path": "/Users/simonlacey/Documents/GitHub/tradingView/lab/data/ledger.db",
    "run_id": "r-20261029-223426-f875f3",
    "trial_id": 6288,
    "trial_window_end": 1759276800,
    "trial_window_start": 1602648000,
    "wf_evidence_ids": [
      143
    ],
    "window_end": 1759276800,
    "window_start": 1602648000
  },
  "start": 1793318400,
  "symbol": "NEARUSDT",
  "trades": {
    "confidence": 0.95,
    "design_trades": 239,
    "design_trades_per_day": 0.1318378229291165,
    "empirical_hi": 12,
    "empirical_lo": 2,
    "expected": 7.382918084030524,
    "hi": 14,
    "independent_blocks": 32,
    "lo": 1,
    "min_expected": 5,
    "poisson_hi": 13,
    "poisson_lo": 3,
    "predictive_hi": 14,
    "predictive_lo": 1,
    "weak": false
  },
  "version": 7
}
```

> Results are historical, not forward-looking. Paper trading only.

## Verdicts (appended by `paper_trade.py compare`)
