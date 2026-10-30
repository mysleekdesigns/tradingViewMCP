---
name: web-phase19-wave1
description: Phase 19 wave 1 (per-tick perf, no behaviour change) - lineage registry, liveStore, shell memo, Coinbase coalescing, ticker watch protocol; confirmed sound 2026-10-30, open minor gaps
metadata:
  type: project
---

Confirmed design (reviewed 2026-10-30, all tests/tsc/lint green; do not re-flag):
- candleStore lineage: WeakMap next -> {parent, samePrefix, lengths}; only applyLiveCandle and plottableCandles record; merge/prepend/replay never do (full-scan fallback). recordLineage cuts the parent's grandparent link, so knownSamePrefix reaches 2 hops; min over hops is a valid lower bound. Length guard is the only mutation guard; no code mutates store arrays (keepUnchangedIdentity mutates only fresh mergeCandles output, never a lineage key).
- plottable: tradedInfo/plottedOf WeakMaps; filteredFromLineage equals raw.filter by construction (keep = lowerBound on parent[s].time).
- liveStore (state/liveStore.ts): reports from ChartPanel passive effect; useLiveSelector = memoSelector(state identity + isEqual) over panelSelector (entry identity). App root subscribes only to activeHasBars, replaySource (NO_BARS unless a replay session), btLive (candleCount only while a report is drawn). Paper feed is a store subscription (liveFeedUpdates dedupes, StrictMode-safe). useReplay startPicking ignores candles, so NO_BARS while idle is safe.
- useLiveCandles returns useMemo object; sameLive in applyReport still guards if memo is dropped.
- indicatorSeries: 'update' plan -> emitValues (full grain only); Chart reads layout grain and uses only paneVersion; LiveIndicatorRows reads full grain.
- Coinbase forming coalescing (100 ms, leading+trailing): closed bars, emitSettled and status changes flush the held bar first; new bucket goes at once; onDispose clears timer.
- Ticker watch: client sends watch on open/change/push-fallback via WatchingWebSocket subclass guarded by latest.sock; >500 or bad symbol -> unwatch; client also filters. Server serialises once per (event, set key); empty-filter pushes send nothing, 20 s status heartbeat < 60 s client stale.
- RunningBucket fold equals aggregateBucket bit for bit (same comparisons, same left-fold volume; out-of-order -> refold).

Open minor (wave 1):
- Two LiveIndicatorRows each compute full legendRows per tick/hover (2x); wave 2 "partitioned once" item covers it.
- liveStore.report has no per-listener try/catch; a throwing paper feed listener skips later listeners and throws into ChartPanel's effect.
- Lineage keeps ~2 extra full-history arrays per stream alive (<=1.2 MB each at 150k bars).
- Coinbase paper limit fills now see <=10 Hz closes (deliberate PLAN item).

**Why:** saves re-deriving the lineage/selector proofs in later Phase 19 waves.
**How to apply:** start wave 2+ reviews here; related [[web-phase9-wave2]], [[web-no-trade-bars]], [[server-conventions]].
