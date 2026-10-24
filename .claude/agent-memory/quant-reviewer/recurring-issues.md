---
name: recurring-issues
description: Statistical/accounting traps found in lab/ reviews (permutation drift, short equity < 0, fake WF OOS, run_id-scoped DSR N, cross-window DSR variance, stop-slippage masking the cross-check, unledgered cost model) - re-check on every lab/agents review
metadata:
  type: project
---

Found in the Phase 7 review (2026-10-24). Re-check each one whenever `lab/` or `agents/` changes, because they are easy to reintroduce.

1. **Permutation null drift.** `adversarial_checks_signals` permuted the WHOLE `hist` (2018 warm-up plus the 2019-2025 window). That moves the 2018 bear bars into the window. On real BTC 4h the window log-return fell from 3.43 (real) to 1.80 (mean over permuted paths). The EMA 20/50 p-value was 0.049 this way and 0.195 when only the window was permuted. Fix: keep the warm-up rows as they are and permute only from the window start (Masters' method). Tests used driftless random walks, so they could not catch this.
   **How to apply:** any null that permutes or resamples must keep the evaluated window's drift. Test it on a trending synthetic series.
2. **Short equity can go below zero.** vectorbt `shortonly` does not liquidate. The engine raises ("equity exhausted") only if the cover fails at the exit. Equity that goes negative mid-trade and then recovers produces nonsense per-bar returns (for example -1.4 on a bar where equity rose) and a max drawdown below -100%. In the real sign flip, MDD was -1.009 and Sharpe -0.12 on a -99.96% loss.
   **How to apply:** check that any short or leveraged path treats value <= 0 as blown.
3. **Walk-forward rows are not OOS.** `runner._walk_forward` slices one fixed plan into test folds and never re-optimises on the train folds. They cannot serve as the Phase 8 "walk-forward OOS Sharpe".
4. **DSR N is scoped to a client-chosen run_id** (`RunBody.run_id`). A new run_id per request resets N. Phase 8 must assign run_id server-side.
5. **PBO comes from the 8 ±20% neighbours plus the base plan**, not from the run's real search matrix. It measures parameter stability, not selection overfitting. The runner's warning thresholds are hard-coded (PBO 0.5, not the Phase 8 limit of 0.2).
6. **DSR V[SR_n] mixes windows.** `selection_sharpe_variance` takes the latest per-plan Sharpe across all of the run's windows. Optuna WF test rows are `source="optuna"`, so they overwrite a plan's design Sharpe with a 6-month test Sharpe. This is conservative but inconsistent. Scope it by window.
7. **Cross-check stop slippage.** nautilus does not slip stop fills, so `agree` is False for any stop plan with slippage > 0, and that difference hides any real stop-mechanics bug. Verified on 2026-10-24: with zero slippage the 3% stop agrees exactly (74 stops), and the sum of log(vb_exit/nt_exit) over the stop exits equals the whole equity gap. Fix: add a zero-slippage mechanics pass plus a slippage attribution.
8. **Cost model is not in the ledger rows.** If config costs change (vol_mult 0.5 -> 0.1 plus a cap, 2026-10-24), the trials in one run_id mix cost models without any trace.
9. **Config fields the engine ignores.** `slippage_cap` was added to CostModel and lab.toml, but `slippage_series` did not apply it, which leaves a dead knob. Grep each new config key through to its use.

Related: [[lab-conventions]]
