---
name: web-range-indicators
description: Phase 14 Anchored VWAP / fixed-range volume profile / conflation review (rounds 1-2, 2026-10-28) - all round-1 issues fixed; what to re-check on later edits + probe harness
metadata:
  type: project
---

Reviewed 2026-10-28 (Phase 14, uncommitted at the time). Round 2: all round-1 findings verified fixed, no
Critical/Warning left.

Probe harness that worked: scratch dir with
`vitest.config.mts` = `{ root: <scratch>, test: { globals: true, include: ['*.probe.ts'] }, server: { fs: { strict: false } } }`,
probe files import web/src by absolute path, run from web/ with `npx vitest run --config <cfg> --reporter=verbose --silent=false`.
Useful probe: random op sequence (forming tick incl. high/low shifts + NaN volume, append, closed bar
replaced by new object, prepend, from/to/rows changes, same-values new object) feeding
`nextProfileCache` with counting ops vs `computeVolumeProfile`, compared via JSON with NaN/-0 tagging.
Round 2: 58.8k steps, ~26.6k extends on reused bases, 0 mismatches. AVWAP runner via planSeriesSync
vs computeIndicator incl. prepends: 0 mismatches.

Confirmed design (round 2):
- AVWAP decides "anchor not loaded" once at its first bar (`c.time > anchor`), carried by clone();
  prepends/trims change cpTime -> runner full reset, so the decision is re-made. Fail-closed: an
  anchor in the gap before the first loaded bar suppresses a line that "next bar" semantics would
  allow (values identical once older bars load) - acceptable.
- computeVolumeProfile null when from < candles[0].time; layer cache keys on inputsKey, count,
  anchorLoaded, candle object identity per in-range index (candleStore replaces objects on edit).
- Open-ended incremental (openProfileBase/openProfileFits/extendOpenProfile) is bit-identical:
  same grid from same (minLow,maxHigh,n), same sequential sums; fits() uses closed-bar span + forming.
- degenerateHeight is relative (|p|*1e-6); rowOf settles against edges; bad volume -> 0.
- Hindsight caveat in VP description + legend row `hint`; anchorNote in legend.

Remaining nits (not fixed, low value): ANCHOR_NOT_LOADED_NOTE says "scroll left" even when no older
bars exist (listing start, CHART_MAX_BARS cap); title time is minute precision (seconds charts).

**Why:** these are where the chart can show wrong or hindsight numbers without a cue.
**How to apply:** on later edits to web/src/indicators/{anchoredVwap,volumeProfile}.ts, chart/volumeProfileLayer.ts
or indicators/runner.ts, re-run the cache/runner fuzz above first. See also [[web-data-honesty-conventions]].
