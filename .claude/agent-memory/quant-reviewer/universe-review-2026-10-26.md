---
name: universe-review-2026-10-26
description: Multi-coin point-in-time universe (lab/lab/data/universe*.py, listing.py) review outcome and open follow-ups for the cross-sectional momentum work
metadata:
  type: project
---

Reviewed 2026-10-26 (uncommitted then). Point-in-time property of eligibility_mask / trailing_dollar_volume / liquidity_rank verified clean (fuzzed 200 random perturbations incl. new listings after cut: 0 failures). pandas 3.1, microsecond index: unit-safe.

Open follow-ups to re-check when the portfolio/momentum backtest lands:
- (CLOSED 2026-10-26 for config: `[universe]` now in lab_values; built universe/kline data content still NOT fingerprinted) `[universe]` in lab.toml was NOT in `lab_values()` (lab/lab/research/runs.py). Acceptable only while no plan reads it. Once a plan consumes the universe, universe settings + parquet sha256 must be frozen into the run, else top_n/window/exclude_bases tweaks are unledgered trials (lab.toml is not under the lab lock).
- `build` step does not check download completeness; a mid-history checksum failure leaves a truncated symbol that looks delisted.
- Renamed tickers (MATIC->POL etc.) re-season for a year; short-gap ticker reuse / redenominations are not detected (no price-jump check).
- Universe `time` is bar OPEN time and rank includes bar t's volume: consumer must act at t+1 open.
- (CLOSED 2026-10-26) Downstream engine books delisting exits at last close; see [[recurring-issues]] #37-38 for the portfolio null/segment-exit findings.

Delta re-review same day: download record + build refusal fixed W1; relist_jump rule added for W2. NEW issue found: segment_starts inferred "hole" as step > step.min() over the WHOLE frame (look-ahead; later bars change earlier segments). Fix proposed: nominal step from interval. Perturbation test did not enable relist_jump. `--allow-incomplete` builds must never back a reported result.
Recurring pattern: frame-wide statistics (min/median/max over the full series) used as thresholds inside "point-in-time" code; grep for `.min()`/`.max()`/`.median()` on whole series in PIT paths.

**Why:** these are the places a later cross-sectional result could quietly lie.
**How to apply:** on any review touching portfolio backtests or universe consumers, check each item above first. Related: [[lab-conventions]]
