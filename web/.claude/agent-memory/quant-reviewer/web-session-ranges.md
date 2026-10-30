---
name: web-session-ranges
description: Phase 18 session ranges overlay - r2 (2026-10-30) all r1 findings fixed (alignment display rule, hindsight caveat, as-of-t fixture, real perturbation tests); only inherited Binance reconnect hole remains
metadata:
  type: project
---

Engine `web/src/indicators/sessionRanges.ts` is push-only, membership by open time
`s <= t < s + dur`, wrap via `(end-start+24)%24 || 24`. All fixture boxes (defaults, Asia 22->06,
OR N=3, 08->08, and r2's EXPECTED_AS_OF_DEFAULTS after bars 3/6/7/15/17) re-derived by hand: correct.

r2 fixes verified (2026-10-30):
- Alignment: `sessionAligned` = integer L>0, 86400%L==0, start*3600%L==0, dur*3600%L==0. Sufficient
  because sub-day timeframes are epoch-aligned (`web/src/data/timeframes.ts` bucketStart; only d/w
  use the Monday anchor, and the layer draws nothing at >= 1d). Wrapping windows are fine: start and
  dur on grid => end and next-day start on grid. Applied in layer via `sessionDrawn`, legend note
  `sessionAlignmentNote`. useLiveCandles never shows the previous interval's candles, so no
  stale-candles/new-interval mismatch.
- Hindsight caveat in SESSION_RANGES_HINT and README.
- Engine perturbation test now uses copies + extreme post-t bars + non-vacuity control; layer test
  perturbs bars after closedThrough with a "counted as closed" control.

Still open: inherited Binance silent-reconnect hole from [[web-market-structure]] (now documented
in README). README's 3h straddle example describes boxes the chart no longer draws (cosmetic).

**Why:** owner uses the overlay to decide whether session structure deserves lab budget; fixture is
the parity source for a future lab hour-of-day / opening-range feature.
**How to apply:** on later edits re-check the alignment rule if timeframe anchoring changes (e.g.
non-epoch sub-day buckets or drawing on 1d), and require a lab port to match the as-of-t snapshots,
not just the final boxes.
