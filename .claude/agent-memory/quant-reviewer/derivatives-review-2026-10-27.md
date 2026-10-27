---
name: derivatives-review-2026-10-27
description: Phase 12 derivatives review - archive/alignment (r1), registry/validator/snapshot (r2), fix verification (r3); verified conventions, closed items, open Warnings
metadata:
  type: project
---

Reviewed 2026-10-27 (uncommitted on 6214dc3). No Critical. Verified: align_to_bar_closes uses searchsorted side="right" on ns ints (event at the close visible, +1 ms not); klines shifted to open+interval; staleness uses the chosen row's own limit -> NaN (no ffill anywhere: loader, manifest, API); perturbation tests really perturb post-close rows and assert the suffix changes; spot archive refactor is mechanical (test_data_archive.py untouched, passes); checksum path has no bypass (404 .CHECKSUM, no hash line, mismatch all raise); API load margins (funding 24h x stale, OI 5m x stale) cover the first bar.

Data facts (BTCUSDT fundingRate 2020-01..2026-09, local parquet): calc_time is never early, 45% of settlements are 1-47 ms LATE, so they first show at the NEXT bar close (4h bars: 171/2196 bars in 2024 see an 8h-old rate). Honest but a random 1-bar lag; parquet index is datetime64[us].

Open (re-check when the owner adds registry features / fingerprint):
- W: kline visibility trusts a caller-supplied interval_seconds separate from load_series(interval=...). 1d premium klines aligned with interval_seconds=4h -> 20h look-ahead (probed). CSV close_time is parsed away; suggest visibility from close_time+1ms or one interval source.
- W: manifest sha256 hashes sidecar zip sha + first/last/rows/gaps, NOT parquet content or parser version: x100 edit of a month's funding_rate left the sha identical (probed). Same family as universe "data content not fingerprinted".
- W: metrics create_time treated as instant snapshot; unverified whether taker/ratio rows are window START (then +5 min look-ahead at bar close). Conservative fix: visible at create_time + 5 min.
- CLOSED r2: stale_after_intervals frozen per run beside manifest sha (runs.derivatives_snapshot), not in lab_values.
- CLOSED r2 (in attach path): attach_columns passes interval_seconds=bar_s and load_series(interval=same interval) -> one interval source.
- Note: 8h->4h switch: last 8h row's 16h limit bridges up to 3 missed 4h settlements (PIT-correct, can't know the next interval).

Round 2 (items 3-4, 2026-10-27, lab lock lifted): No Critical. Verified on real BTCUSDT 4h: design-load drv_* and funding_zscore/oi_change/basis are bit-identical to the full-load prefix (no holdout leak via load window or cache; cache keyed (sym, interval, end, manifest sha) + bar-index equality, values depend only on bar index). funding_zscore pandas rolling vs exact per-window numpy: max diff 1.5e-11, NaN masks identical. oi_change lag p / warm-up p correct. Permutation null shuffles returns while drv stays on real timestamps -> it DOES destroy drv timing edge (fair); derivatives_columns docstring claims the opposite.
Open r2 Warnings:
- Validator gate rule beaten by trivially-true price clause: `high >= low and fz < -1`, `close > 0`, `volume >= 0`, `rsi >= 0`, `close >= close`, `close > ema_slow - ema_slow` all validate (probed). Stats still honest; it's an H3 scope hole. Suggest selectivity check of derivatives-free conjuncts on design window and/or ungated-twin baseline in get_report.
- Coverage: drv NaN for 14.8% (funding/klines, from 2020-01) and 24.7% (OI/LS, from 2020-09) of design window (design_start 2019-01). Plan is structurally flat in 2019 while B&H/vol-target/random-entry null/maxDD are scored from 2019 -> gated-vs-ungated attribution confounded, maxDD criterion gets 2019 H2 -50% for free. Portfolio precedent: score from first decision.
- holdout._load_full require_plan_inputs checks the FULL frame (any finite), not the holdout slice -> spot downloaded past drv spends the one check on a NaN-gated holdout.
- Snapshot = whole-archive manifest bytes: ETH download refuses BTC plans mid-run (fail closed, OK); still not content-hashed.

**How to apply:** for any consumer of derivatives_align (registry funding_*/basis/oi features), confirm interval provenance, metrics visibility offset, and fingerprint coverage first. Related: [[recurring-issues]], [[universe-review-2026-10-26]]

Round 3 (fix verification, 2026-10-27, uncommitted): No Critical. 466 tests pass (run with lab/.venv/bin/python -m pytest -p no:cacheprovider; `uv run` is blocked by the sandbox's ~/.cache/uv).
CLOSED: r1 kline interval (visibility now from close_time+1ms), r1 metrics lag (+5 min), r1/r2 manifest content (per-series content_sha256 of the loaded frame, recomputed from disk, stat-cached: 1.7 s first metrics hash, 26 ms cached, matches manifest); r2 W1 (check_gate_base, threshold 0.9 frozen in snapshot; real BTC: close>ema 54%, rsi<75 96% refused, rsi<65 85% passes); W2 (scored start, benchmarks/nulls/neighbours/folds follow); W4 per-series; Note 2 stamped_before (index = archive stamp: funding calc_time, kline open_time, metrics create_time; nothing a design bar could legitimately see is dropped except the 00:00 settlement).
funding_zscore tails (-11..+13 at p=180) are REAL events (FTX 2022-11-10, COVID 2020-03-13, Jul 2020) with std 4e-5..4e-4, not near-flat artefacts; flooring std at 2.5e-5 changes nothing at p>=180. Population z is bounded by sqrt(p-1): p<=5 makes `fz < 2` a constant.
Open r3:
- W: scored start is per PLAN (feature choice + period): BTC 4h starts range 2019-12-31 .. 2021-02-14; a no-op `oc > -1000` (oi_change 1000) gate shifts the window 13.5 months (Sharpe 1.11 -> 0.49, same logic). Fold layout follows. Suggest one start per (symbol, interval) frozen in the run snapshot.
- W: require_holdout_coverage checks any-finite + last-finite staleness only; a drv column NaN for the first 11 of 12 holdout months passes.
- Note: gate check runs on the base plan only (sweep/WF/neighbour trials unchecked); 0.9 admits an 85% clause beside a 2% gate.

Round 4 (final verify, 2026-10-27, uncommitted): No Critical. 1951 tests pass (full suite), 559 in derivatives/validator files. Real data: one scored start per (symbol, 4h) regardless of feature/period/no-op gate (BTC 2020-10-01 for fz42/fz180/oc/no-op fr; ETH 2021-12-31; metrics first is the binding series); drv NaN in scored window <= 1.5%; holdout 2025-10..2026-10 coverage passes for all 6 features on BTC/ETH, no frozen gaps in holdout. Daily fill verified (2026-06-29 6 rows x 3 kline series, sha in sidecar + manifest daily_sources, merge rejects conflicts/out-of-day, never overwrites). Uncapped optuna_search/walk_forward_optimize only reachable from Python API (MCP + CLI pass capped spaces). CLOSED: r3 W1 (scored start), r3 W2 (first-finite + frozen gap overlap), Note 2 (fz low 12).
Open r4:
- W: criteria._neighbourhood_row / _side_gaps build EXPECTED neighbours from uncapped tunables while runner clips at derivatives_period_caps -> funding_zscore period 160..180 on 4h (default 180 = cap) gives a permanent "missing" neighbourhood row with a wrong reason; + side untested at 180.
- W: gate-base check on base plan only; sweep/WF trials can tune rsi period to 60 (`rsi < 65` 96% true) and feed wf_oos evidence. Holdout/backtest of such tuned params are refused (fail closed).
- Note/H3: price-only twin scores from 2019-01 vs gated 2020-10 (not comparable). No-op twin `close > ema_slow and fr > -1` is accepted (validate, window, gate-base 53.9%), same start; it is a family member of the price plan (corr ~1) and of the gated plan if the gate rarely binds -> never holdout-check the twin. Better: lab-computed ungated baseline in get_report + "gate must bind" check.
