---
name: web-phase19-wave3
description: Phase 19 wave 3 (history load, bar close, network) - shared LiveCandles cache, splice merges, BarTimesMemo, in-place chart-type switch, point sync, Coinbase parallel pages, server live forming bar/ETag/compress; confirmed sound 2026-10-30, open items
metadata:
  type: project
---

Confirmed design (reviewed 2026-10-30; tests/tsc green apart from a quant probe file; do not re-flag):
- liveCandlesCache: key = providerId(WeakMap identity) + symbol/interval/limit/source/maxBars; listeners added before start(); late acquire gets getSnapshot() synchronously; 0 ms grace (StrictMode + same-commit remount reuse); timer deletes the entry then stop(); released lease no-ops reload/loadOlder; fan-out copies the listener set.
- mergeCandles/prependOlder splice == old Map+sort (later duplicate wins; ascendingUnique fallback). mergeCandlesKeepingUnchanged == old keepUnchangedIdentity; records lineage with an exact identity prefix (valid lower bound).
- BarTimesMemo keyed on barsKeyOf; same key with different interior only on a 'set' plan (refresh=true). backtestOverlay.setBars early-returns on equal key (keeps old BarTimes) = old behaviour.
- planPointSync only calls update() for the last shown point at its own time or strictly newer appends (<=2); everything else setData. Safe vs LWC "older time" throw and the conflation index-shift crash.
- Chart-type: requestedTypeRef effect declared before the kind effect; in-place effect no-ops when the kind effect already set chartTypeRef. All three candlestick option builders set the same key set (livePriceLine incl. lastValueVisible/priceLineVisible), so applyOptions leaves no stale HA options. createdRealLine -> alertLinesManager.attach re-orders lines (attach detaches first).
- Volume recolor: volumeShownRef set on every full setData; tail updates use paletteRef.
- Coinbase fetchPages: one pending throttle reservation at a time (await race(sent, settled)); slot return only if nextRequestAt still equals this reservation (LIFO-safe, 429 pause never undone); earliest rejected index thrown via allSettled.
- Server: hub `last` cleared on connect/drop/teardown; lastKline requires ws open + live + age <= 5 s + !closed + time === e === forming-now; live path only when missing empty and fresh == [e,e] (so first 5 s after a boundary uses REST). Forming bar never stored. In server mode the hub's last kline is the same push the client already applied (no regression vs REST). ETag weak sha1 of JSON.stringify body (same bytes as default serializer); 304 empty body; compress skips WS upgrades; no SSE routes exist.

Open (wave 3):
- Shared controller is a cross-panel behaviour change: one panel's scroll-back / Strategy Tester history load / failed older page (refreshError, loadingOlder) shows on every panel on that market; owner sign-off + browser check.
- `Cache-Control: immutable` (1 year) on endTime pages survives a TV_MARKET_VENUE switch (ensureVenue clears the DB, browser cache not) and any future server fix to page contents; URL has no venue/version token.
- settled can be true when the deferred boundary bar was absent upstream (endTime == storable edge); negligible for web paging.

**Why:** saves re-deriving the cache/merge/sync proofs in waves 4-5.
**How to apply:** start later Phase 19 reviews here; related [[web-phase19-wave1]], [[server-conventions]], [[market-venue]], [[web-phase9-wave2]].
