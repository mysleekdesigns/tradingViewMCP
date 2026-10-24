---
name: lab-conventions
description: Lab engine/stat conventions verified as correct in the Phase 7 review, plus how to run probes in the sandbox
metadata:
  type: project
---

Checked by hand-built probes on 2026-10-24. Do not re-flag these unless the code changes.

- Engine two-row trick (row 2j = open, row 2j+1 = rest of bar). Verified:
  - stops are checked on the entry bar
  - a gap through the stop fills at the open with slippage applied
  - when SL and TP hit in the same bar, SL wins
  - an exit signal at the open runs before the intrabar stop
  - stops are skipped on untradable bars and checked at the next tradable open
  - a favourable TP gap fills at the open
  - fee and slippage are charged per side on traded value
- Fills happen at the next tradable bar's open. The `delay` parameter counts rows.
- Annualisation is 365 days throughout: `bars_per_year`, with `periods_per_year` passed explicitly to every hedgeFund metric (hedgeFund defaults to 252).
- DSR matches Bailey & López de Prado 2014 (0.9004 for N=100 and 0.9505 for N=46 are tested). Permutation p-value is `(count + 1) / (N + 1)` (from hedgeFund). PBO/CSCV ranks follow the paper.
- holdout_start = 2025-10-01. The runner loads with end=holdout_start, calls `design_view`, then `assert_no_holdout`.
- Data: missing bars are absent rows. tradable = volume > 0. No ffill anywhere.
- Probing: run `export UV_CACHE_DIR=$TMPDIR/uv-cache` first. Real data is at lab/data/klines/BTCUSDT/4h (2018-01..2026-09). A full report plus the adversarial run with 20 permutation paths takes about 7 s.
- Search/WF (verified on 2026-10-24): every Optuna trial is ledgered, errors included. Selection reads train windows only (`history_through(train)`), and a perturbation test covers it. The holdout is guarded by `assert_no_holdout` plus `window.end <= holdout_start`. The guard trusts the caller's `cfg`, so Phase 8 must pin `cfg` server-side.
- nautilus cross-check timing: the bar ts is at close and the open is a trade tick 1 ns after the open. The replay strategy only reads signal rows < the current row, so the close cannot leak into the fill. The no-stop EMA 20/50 run agrees exactly (137 trades).
- Files can change mid-review because implementers run in parallel. Check mtimes and config values before quoting numbers.

Related: [[recurring-issues]]
