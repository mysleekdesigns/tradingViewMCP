---
name: derivatives-review-2026-10-27
description: Phase 12 derivatives archive + alignment review (lab/lab/data/derivatives*.py, /lab/derivatives) - verified conventions, open Warnings to re-check when registry features land
metadata:
  type: project
---

Reviewed 2026-10-27 (uncommitted on 6214dc3). No Critical. Verified: align_to_bar_closes uses searchsorted side="right" on ns ints (event at the close visible, +1 ms not); klines shifted to open+interval; staleness uses the chosen row's own limit -> NaN (no ffill anywhere: loader, manifest, API); perturbation tests really perturb post-close rows and assert the suffix changes; spot archive refactor is mechanical (test_data_archive.py untouched, passes); checksum path has no bypass (404 .CHECKSUM, no hash line, mismatch all raise); API load margins (funding 24h x stale, OI 5m x stale) cover the first bar.

Data facts (BTCUSDT fundingRate 2020-01..2026-09, local parquet): calc_time is never early, 45% of settlements are 1-47 ms LATE, so they first show at the NEXT bar close (4h bars: 171/2196 bars in 2024 see an 8h-old rate). Honest but a random 1-bar lag; parquet index is datetime64[us].

Open (re-check when the owner adds registry features / fingerprint):
- W: kline visibility trusts a caller-supplied interval_seconds separate from load_series(interval=...). 1d premium klines aligned with interval_seconds=4h -> 20h look-ahead (probed). CSV close_time is parsed away; suggest visibility from close_time+1ms or one interval source.
- W: manifest sha256 hashes sidecar zip sha + first/last/rows/gaps, NOT parquet content or parser version: x100 edit of a month's funding_rate left the sha identical (probed). Same family as universe "data content not fingerprinted".
- W: metrics create_time treated as instant snapshot; unverified whether taker/ratio rows are window START (then +5 min look-ahead at bar close). Conservative fix: visible at create_time + 5 min.
- [derivatives] (stale_after_intervals) is not in research.runs.lab_values; must join when a plan consumes it.
- Note: 8h->4h switch: last 8h row's 16h limit bridges up to 3 missed 4h settlements (PIT-correct, can't know the next interval).

**How to apply:** for any consumer of derivatives_align (registry funding_*/basis/oi features), confirm interval provenance, metrics visibility offset, and fingerprint coverage first. Related: [[recurring-issues]], [[universe-review-2026-10-26]]
