---
name: phase14-chart-upgrades-review
description: Phase 14 (LWC 5.2 hover hit testing, conflation, a11y plugin, volume profile, AVWAP) review conventions + open risks after round 2
metadata:
  type: project
---

Round 1 on 2026-10-28: no Critical, 3 Warnings. Round 2 (same day, still uncommitted): all three fixed, nothing Critical or Warning left. tsc, vitest (2277) and lint all green.

Fixed in round 2 (check these first if they regress):
- Open-ended VP (`to` = 0) is now incremental: `nextProfileCache` keeps an `OpenProfileBase` (closed bars on the grid) and `extendOpenProfile` adds only the forming bar. A property test checks that this matches a full recompute bit for bit. It relies on the candle store replacing a bar's object whenever the bar changes (`applyLiveCandle` / `mergeCandles` keep identity for untouched bars). If anything ever mutates a candle in place, the cached profile goes stale silently.
- `historyRoom(candleCount)` = min(20k per load, 150k - count). The tester text names both limits.
- AVWAP and VP draw nothing when the anchor is older than the first loaded bar. The legend `anchorNote` uses the same `anchor < candles[0].time` test as the stepper/`computeVolumeProfile`. Keep them in sync.

Still open (Notes only):
- The full O(range) rebuild still runs once per new bar (the cache needs `count` to be unchanged) and on every tick that widens the range high/low. That is about 22 ms at 150k bars with a far anchor, so roughly once per second on 1s charts.
- In `historyPlan`, `unreachable` is decided per load (20k), so a window that two loads would reach gets no button.
- An anchor after the last plotted bar (typed in Settings, or set on another panel while this one is in replay) draws nothing and shows no note.

Confirmed conventions (do not re-flag):
- `hoveredSeriesOnTop` is already true by default in LWC 5.2.1. `timeScale().options().barSpacing` returns the zoomed spacing.
- The a11y plugin uses pane primitives, so LWC still removes empty panes. The plugin re-syncs itself through rAF, and its `detach()` is guarded with `paneAlive`.
- The a11y data table and shortcuts panel are attached to the pane content element as siblings of the canvas. LWC's pane mouse handler listens on the top canvas, so presses on the table never pan the chart. LWC's wheel listener sits on the chart element, so Chart.tsx stops wheel events from the overlay with a capture-phase listener on the container (removed with the same capture flag). Normal wheel zoom is untouched.
- Keys: Alt/Ctrl/Meta are skipped. H/T/Escape are rebound to `,`/`.`/none.
- `setHighlighted` and `setHovered` return early when nothing changed.
- Derivatives and ribbon points are filtered to plotted bars, so logical index == candle index.
- `plottedBarTime` uses `appliedRef.current` (the plotted slice during replay), so context-menu anchors are always real bars.

**Why:** these are the parts of Phase 14 most likely to regress, or to be re-flagged by mistake.
**How to apply:** in later chart reviews, check the open items first and skip the confirmed ones. Related: [[review-watchlist]].
