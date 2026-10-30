---
name: phase18-session-ranges-review
description: Phase 18 session ranges overlay review round 1 (2026-10-30) - sound, no Critical/Warning; conventions confirmed and the small open notes
metadata:
  type: project
---

Round 1 (2026-10-30): sound. tsc -b, eslint and 124 related tests were green. No Critical or Warning findings.

Confirmed conventions (do not re-flag):
- `SessionRangesLayer` copies `MarketStructureLayer` exactly (attach/detach(alive), identity-prefix
  restart, `closedBarCount(bars, closedThrough)`, hidden = not advanced). Chart wires it in the
  main-series effect (attach/detach next to the profile and structure layers), a sync effect declared
  after the data effect, and the theme effect.
- Interval switch is safe: `useLiveCandles` returns `initialSnapshot` until the new controller's
  first snapshot, so `closedThrough` is null and the bars are null. The engine restarts empty and
  never sees the old interval's bars with the new `interval`.
- The engine's box order (by start) holds even for wrapping windows. A box opened by bar t has
  start s, and every earlier bar is < s (otherwise it would already be in that instance). The
  culling lower bound `from - 24h` is correct because lastTime < end <= start + 24h.
- `timeToCoordinate` (exact lookup) is fine here because box edges are always plotted bars. Market
  structure uses `timeToIndex(..., true)` instead because its anchors may not be bars.
- zOrder 'bottom' in LWC 5.2.1 draws below the grid too ("below everything except the background").
  That is fine for 8% fills.
- `push` throws RangeError on a non-ascending bar. LWC setData would already throw on such data, so
  this adds no new way to crash.

Open notes (low priority):
- Each layer runs its own O(n) identity prefix scan on every tick, so with both overlays on there
  are two scans per tick (part of [[review-watchlist]]'s O(n) per tick item). A plan hint from the
  data effect would remove them.
- A replay step back recomputes from scratch (the engine cannot pop). It is cheap: 100k bars take
  less than 400 ms in the test, in practice about 10 ms.
- A hidden entry keeps its `synced` reference to an old candles array until it is shown or removed.

**Why:** the next overlay probably copies this template too, so these are the checks it needs.
**How to apply:** for later primitive overlays, check that they match this layer's lifecycle, then
look only at what is new. Related: [[phase16-market-structure-review]].

Round 2 (2026-10-30): sound, no Critical/Warning. Alignment rule (`sessionAligned`: 86400 % L,
start*3600 % L, duration*3600 % L) is display only via `sessionDrawn`; compute ignores interval.
Verified: interval-only change gives a new view object (v.show !== entry.show) and setView ->
requestUpdate redraws; legend reads `timeframeSeconds(interval)` at render (interval is a prop,
so no stale note); hidden/1d+ path nulls engine/synced/boxes/view and setView(null); reshow
recomputes from a fresh engine. No cycle: indicatorSeries -> sessionRangesLayer -> registry /
sessionRanges / structureBars / theme, none import indicatorSeries. Do not re-flag these.

Round 3 (2026-10-30, follow-up): sound. `BarLineage` + `planSamePrefix` closes the "two identity
scans per tick" note above. Proof to reuse: tailMatch(offset 0) returns prev.length when the last
bar is identical, else lastIdx, and every earlier index is checked identical, so 'update'.from is
exactly the identity prefix; 'none' implies equal lengths. A recorded (prev, next, same) is a fact
about two immutable arrays, so it can never go stale (store arrays are never mutated after publish;
keepUnchangedIdentity edits only its fresh `next`). Early returns skip both record and appliedRef.
planSeriesSync's own tailMatch is still one O(n) identity loop per tick (accepted).
