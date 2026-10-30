---
title: Web performance research (web/ + server/ hot paths)
date: 2026-10-30
scope: web/src (chart, state, data, components, CSS), server/src (candles route, hub, stream)
method: static code audit by five read-only subagents, production build measurement, web research via CrawlForge
status: research only, no code changed
---

# Web performance research

## 1. Verdict

The app is already built on the right primitives: `series.update()` on ticks, incremental indicator checkpoints, bar-lineage prefix checks, `barsKey` gating, throttled ticker emits, a single upstream socket per key on the server, and canvas primitives that draw from cached geometry. There is no single catastrophic hot spot.

What remains is a layer of **steady-state overhead that scales with history length and panel count**, plus a **startup payload that is one monolithic chunk**:

| Area | Current | Where it hurts |
|---|---|---|
| Per-tick work | 3 full passes over the candle array, then an App-root re-render | 150k-bar histories, 1s/1m streams, 4-panel layouts, Coinbase (per-trade ticks) |
| Per-frame work (crosshair, drag) | Drawings renderer re-does string/Intl work; three overlay layers rebuild geometry per repaint | Any chart with drawings or overlays while the mouse moves |
| Ticker stream | Whole-market `!miniTicker@arr` parsed every second to keep ~10 symbols | Every tab, continuously |
| History load | Every `/api/candles` hits upstream once even on warm cache; no compression or caching headers | Symbol/timeframe switches, multi-panel |
| Startup | One 1.02 MB JS chunk (310 KB gzip), 147 KB CSS, 6 font requests | First load and every deploy (full cache invalidation) |
| DRY | ~1,700 to 2,000 removable duplicate lines across data, chart layers and CSS | Maintenance and consistency, indirectly perf (fixes land in one place) |

None of this was measured at runtime in this session (the Vite dev server was not running and the sandbox cannot drive a browser profile). Every item below cites file and line and should be confirmed with a Chrome Performance trace before and after (section 7).

## 2. Measurements

Production build (`npm run build`, Vite 8.3.5 / rolldown, 182 modules, 3.8 s):

| Asset | Raw | Gzip |
|---|---|---|
| `index-*.js` (single chunk) | 1,022,789 B | 306,903 B |
| `index-*.css` (single stylesheet) | 147,207 B | 23,390 B |
| Fonts: 6 woff2 (fetched) + 6 woff (emitted, never fetched) | ~115 KB woff2 | n/a |

Dependency share of the JS chunk (minified, before tree-shaking):

| Package | Raw | Gzip |
|---|---|---|
| react-dom client | 625 KB | 112 KB |
| lightweight-charts 5.2.1 | 189 KB | 61 KB |
| lwc-plugin-accessibility | 104 KB | 31 KB |
| react | 18 KB | 5 KB |

So roughly 100 KB gzip is app code, 210 KB is vendor. The vendor half never changes between deploys but is re-downloaded on every one because it shares the hash with app code.

Other facts: `tsconfig` targets ES2023; no `React.lazy` or `Suspense` anywhere; 6 `memo()`, 77 `useMemo`, 127 `useCallback`, 164 `useEffect`, 6 `useSyncExternalStore`, 0 `createContext`, 0 `useTransition`; no Web Worker; React Compiler not configured; server has no `@fastify/compress`, ETag or Cache-Control.

## 3. Findings, ranked by impact

Format: what happens, evidence, cost, one-line fix. Paths are relative to `web/src/` unless prefixed.

### A. Steady state: every WebSocket tick

**A1. Three O(n) passes over the whole candle array per tick.**
`data/candleStore.ts:42-47` copies the array (`[...candles, c]` or `slice()`) whenever the forming bar changes; `data/plottable.ts:31` runs `raw.every(isTradedBar)` over all bars; `chart/seriesSync.ts:64-66` `tailMatch` pointer-compares every bar before the last. With CHART_MAX_BARS at 150k that is ~450k operations per message. Fix: track a monotonically increasing `version` plus `changedFrom` index in the controller (the `BarLineage` idea that `structureBars.ts:47-71` already uses) and let `planSeriesSync`, `plottable` and the store read that instead of scanning.

**A2. Every tick re-renders the App root and the whole shell.**
`state/useLiveCandles.ts:63` calls `setEntry` on each controller emit; `components/ChartPanel.tsx:217-219` reports a fresh object up; `App.tsx:524-552` stores it in `lives` state. The 2070-line App body then runs (68 `useCallback` + 17 `useMemo` dep checks, `visible.map`, `assignSlots`, `liveQuotesOf`) and re-renders Header (with TimeframePicker, ChartTypeMenu, LayoutMenu, ~15 icon components), DrawingToolbar, SidebarRail, StatusBar, AlertToasts and the open sidebar panel, none memoised. With 4 panels that is 2 to 4 shell renders per second; on 1s charts, one per second per panel. Chart itself renders twice per tick because `indicatorSeries.ts:627` emits on 'update' plans into `useSyncExternalStore` (`Chart.tsx:426`). Fix: keep `lives` in a small external store (ref + subscribe) read only by the quote, StatusBar and paper feed; lift only status/closedThrough/error into React state; drop the indicator emit on 'update' plans; `memo()` the shell components.

**A3. Window keydown listener re-bound every tick.**
The keyboard effect at `App.tsx:1507-1606` depends on `openAlertAtLast`, which depends on `lastClose` (`App.tsx:1453-1456`), which changes on every active-panel tick. Fix: read `lastClose` from a ref inside the handler.

**A4. Whole-market ticker stream parsed to keep a handful of symbols.**
`data/binance.ts:431` subscribes to `!miniTicker@arr`; `data/binanceTicker.ts:113-130` builds a `Ticker` object for every element (1,500 to 2,500 per second), then `state/tickers.ts:150-157` keeps only watched symbols. Via the server path, `server/src/routes/stream.ts:41-50` re-stringifies the full array to every `/ws/tickers` client and `data/server.ts:147-154` re-parses it. Roughly 100 to 300 KB/s and ~1,000 allocations per second per tab, forever. Fix: client sends its watched set; hub filters before serialising; on the direct path, check `raw.s` against the watched set before building objects, or subscribe per-symbol `<sym>@miniTicker` on a combined stream.

**A5. Coinbase forming bar emits per trade with no coalescing.**
`data/coinbaseStream.ts:749-751` emits on every match (10 to 50 Hz on BTC-USD), each going through A1 and A2. The ticker side of the same file already flushes on a timer (`coinbaseStream.ts:921,1037`). Fix: coalesce candle emits per animation frame or 100 to 250 ms.

**A6. Watchlist re-renders every row per ticker emit and per App render.**
`components/Watchlist.tsx:179-207`: `WatchlistRow` is not memoised and receives 8 fresh lambdas per row; each row calls `toLocaleString` three times (`state/tickers.ts:285-315`). Watchlist itself gets `onAdd={() => openSearch()}` (`App.tsx:1856`). Fix: `memo(WatchlistRow)` with symbol-keyed stable callbacks, `memo(Watchlist)`, hoist `onAdd`, cache `Intl.NumberFormat` instances.

**A7. Aggregated timeframes rebuild the bucket per base tick.**
`data/timeframeProvider.ts:419-422` spreads and maps all base bars; `data/aggregate.ts:12-15` builds a Map and sorts per 1s/1m tick. Fix: keep running open/high/low/close/volume in `BucketState`.

**A8. Smaller per-tick allocations.**
Volume-profile cache misses every tick because it keys on array identity (`chart/volumeProfileLayer.ts:355,378,405`) instead of the shared `BarLineage` that structure and session layers receive (`Chart.tsx:457-467`); `inputsKey` string built per tick (`:354`); param/toggle objects re-derived per tick in `marketStructureLayer.ts:837-856`, `sessionRangesLayer.ts:634`; `volumePaletteOf` allocates a palette per volume point (`Chart.tsx:122,810`); `useLiveCandles.ts:81` returns a fresh object each render so ChartPanel's `onLive` effect fires on unrelated renders; `uidOfSeries` is a linear scan per crosshair move (`indicatorSeries.ts:631-635`).

### B. Per frame: crosshair move, scroll, zoom, drawing drag

**B1. Drawings renderer does string and Intl work on every paint.**
`chart/drawings/primitives.ts:311,336,342` call `withAlpha` (regex + parseInt + template) per rectangle and per fib band; `:359-365` format 7 fib labels through `toLocaleString` per fib per paint; `:245` allocates dash patterns per stroked item; `:381` allocates handle points; the primitive is zOrder `'top'` (`:103`) so this also runs on every crosshair paint (~60 Hz while the mouse moves). Fix: precompute rgba strings, scaled dash arrays and label strings into `RenderItem` in `updateAllViews`; keep `draw()` to canvas calls.

**B2. Three overlay layers rebuild geometry on every model recalculation even when nothing moved.**
`marketStructureLayer.ts:680-685`, `volumeProfileLayer.ts:594-605`, `sessionRangesLayer.ts:469-474` always rebuild and `coords()` allocates a closure object each time. A drawing drag calls `requestUpdate` per pointer move (`drawings/primitives.ts:169-171`, `controller.ts:644-647`), so dragging a trendline rebuilds every overlay at 60 Hz. Fix: a cheap signature (visible logical range, width, barSpacing, two reference `priceToCoordinate` values, view version) that returns the cached geometry when unchanged.

**B3. Volume profile builds full geometry before the off-screen check, and deep-clones time-scale options.**
`volumeProfileLayer.ts:594-604`: up to 400 `priceToCoordinate` calls plus a row object per row, then `profileOffscreen` (`:601`) discards it; `isValueAreaRow` and `maxVolume` recomputed per frame. `:590` reads barSpacing via `timeScale().options()`, which Lightweight Charts returns as a clone of the whole options object; `sessionRangesLayer.ts:452-453` already derives barSpacing from two `logicalToCoordinate` calls. Fix: cull first, cache profile constants on `setProfile`, share the barSpacing helper.

**B4. Drawings hit test re-projects every drawing per mouse move.**
`drawings/controller.ts:317-340` and `geometry.ts:135-160`: 2 `timeToLogical` binary searches + 2 `logicalToCoordinate` + 2 `priceToCoordinate` per drawing per move (fib adds 7 more), the selected drawing projected twice, `:393` linear find. Fix: hit-test against the shapes the renderer already holds for the frame.

**B5. Legend rows rebuilt twice per tick.**
`Chart.tsx:1258` rebuilds title strings and runs 2 binary searches per indicator each render; `:1292,1298` filter the rows twice. Fix: memoise on `[indicatorSnapshot.version, hoveredTime, lastBarTime]`.

**B6. Per-element canvas state churn.**
`setLineDash` with fresh array literals and per-element style sets in `marketStructureLayer.ts:495-497,535`, `sessionRangesLayer.ts:331`, `volumeProfileLayer.ts:484`, `drawings/primitives.ts:245,344`. Hoist scaled dash arrays once per draw and batch by colour.

### C. History load, bar close, network

**C1. Every `/api/candles` makes one upstream call even on a warm cache.**
`server/src/cache/candleCache.ts:174-175`: the fresh tail is always non-null when `endTime` is undefined. Fix: hub keeps the last kline per key; the candles route serves the forming bar from it while the stream is live.

**C2. No compression, ETag or Cache-Control on the server.**
A 1,000-candle response is about 105 KB as array-of-objects (~60 KB as compact arrays, ~30 KB gzipped). Closed-history pages (with `endTime`) are immutable yet uncacheable. Fix: `@fastify/compress` (brotli/gzip), `Cache-Control: immutable` on closed pages, ETag on the rest; consider compact arrays for the kline payload.

**C3. Full indicator recompute on every history page or gap fill, with O(n * period) primitives.**
`indicatorSeries.ts:617` calls `runner.reset(candles)` for every indicator on 'set' and 'prepend'; `indicators/primitives.ts:19` uses `buf.shift()` (O(period) per bar) and `sum()`/`stdev()` (`:31-47`) re-sum the window per bar. SMA/Bollinger over 150k bars with period 500 is ~75M operations per reset. Fix: ring buffer with running sum and sum-of-squares (re-sum every N bars for drift); on 'prepend', compute only the new head and splice.

**C4. Prepend and gap fill do O(n log n) bookkeeping, then `setData` every series.**
`candleStore.ts:54-59` builds a Map of all bars and sorts; `liveCandles.ts:153-172` builds another Map; `Chart.tsx:796-800` then `setData` main, volume (full re-map, `volume.ts:13`) and all indicators. Fix: merge sorted inputs by binary-search splice; `setData` only main/volume, runner head-splice for indicators. (Lightweight Charts has no prepend API; `setData` after building the new array is the documented path, but it should be the only full pass.)

**C5. Three O(n) time Sets and two `barsKey` strings per bar close.**
`Chart.tsx:1161,1174` each build `new Set(applied.map(c => c.time))`; `backtestOverlay.ts:447-448` builds a third; `barsKey` is built at `Chart.tsx:1154-1157` and again at `backtestOverlay.ts:441-444`. `regimeRibbon.ts:210` and `derivativesPanes.ts:421-422` then `setData` whole series. Fix: one memoised plotted-times Set keyed on `barsKey`; `series.update()` for the newest point when only the tail grew.

**C6. Appearance effect re-maps and re-sets the whole volume series on any change.**
`Chart.tsx:875` runs `setData(toVolumeData(applied))` for theme, upColor, downColor and timezone alike. Fix: gate on upColor/downColor actually changing.

**C7. Chart-type switch and pane reorder tear down series unnecessarily.**
`Chart.tsx:654-715` removes the main series and re-attaches 6 primitives for candles/hollow/heikin-ashi, which are all `CandlestickSeries`; `indicatorSeries.ts:513-517` destroys and rebuilds every pane series on reorder. Fix: `applyOptions` + `setData` for same-kind switches; check `IPaneApi.moveTo` in 5.2.1 for reorders.

**C8. No client-side socket or request sharing across panels.**
`useLiveCandles.ts:60-67`: the same symbol/timeframe in two panels opens two sockets and two REST loads; the server dedupes upstream, direct Binance does not. Fix: a ref-counted controller cache keyed on symbol/interval/provider.

**C9. Coinbase REST paging is sequential through a 125 ms throttle** (`data/coinbase.ts:562-565`): 500 bars of 4h is 7 pages (~1 s), 1w is 12, plus up to 12 step-back pages. Fix: parallelise within the throttle budget. Seconds timeframes pull 15 parallel 1,000-row 1s pages (~2.7 MB) for a 30s chart (`timeframeProvider.ts:281-284`).

### D. Startup and bundle

**D1. One 1.02 MB chunk, no code splitting.**
Heavy views are correctly mounted only when open (ResearchPage 1,946 lines, BacktestPanel 1,584, PaperTradingPanel 1,021, SeasonalsView 885, SettingsDialog 648, AlertsPanel 623, IndicatorSettings 477, AlertDialog 447) but all are imported eagerly in `App.tsx:25-82`, and each pulls its CSS at module top (`ResearchPage.tsx:73`, `SeasonalsView.tsx:36`, `PaperTradingPanel.tsx:32`). Blocker: `SidebarRail.tsx:2-3` imports `TesterIcon` from BacktestPanel and `ResearchIcon` from ResearchPage, and App imports helpers from `backtestView`/`researchView`, which would keep those modules in the main chunk. Fix: move the two icons to a small module, then `React.lazy` + `Suspense` the five largest views (their CSS splits automatically with Vite's default `cssCodeSplit`).

**D2. Vendor code shares a hash with app code.**
Every deploy invalidates the whole 310 KB. Fix: `build.rolldownOptions.output.codeSplitting.groups` with `react`/`react-dom`, `lightweight-charts`, and `lwc-plugin-accessibility` groups (Vite 8 replaces `manualChunks`; the object form is gone and the function form is deprecated).

**D3. Fonts: 6 requests, two weights likely droppable.**
`main.tsx:4-9` imports sans 400/500/600 and mono 400/500/600 (`font-display: swap`, woff2 + woff fallback). Sans 500 and 600 are used 51 and 69 times respectively. Fix: drop to sans 400/600 + mono 400/500 (4 requests, ~75 KB) or `@fontsource-variable/ibm-plex-sans` (one file per family), and `<link rel="preload" as="font">` the two body weights in `index.html`.

**D4. React Compiler is not configured.**
`vite.config.ts:16` has `plugins: [react()]` only. The compiler would auto-memoise Header, Watchlist rows, StatusBar and SidebarRail and resolve A2, A6 and most of the unstable-props findings without hand-written `memo`/`useCallback`; comments at `App.tsx:414-415,671` show it was anticipated. Patterns to fix first because the compiler skips components that violate the Rules of React: setState during render at `App.tsx:356,636-654,731-735,1329-1344` and `state/useReplay.ts:85,92-95`; the intentionally over-specified memo deps at `state/drawings.ts:823-825`; the closure over render-computed `slots` at `App.tsx:361-367`.

### E. DRY consolidation (estimated removable lines)

| Cluster | Evidence | Est. lines |
|---|---|---|
| Data: `requestJson`/`withAbort`/`parseRetryAfter`/`clampLimit`/`resolveWebSocket`/memoised symbols triplicated | `binance.ts`, `coinbase.ts`, `server.ts` | ~170 |
| API clients re-implement `createLocalServerRequest` and `num/str/fail/record/readJson/errorFor` | `persistence.ts:107-134`, `labApi.ts:499-534`, `researchApi.ts:403-430` vs `drawingsApi.ts:95-170` | ~150 |
| Type guards (`isRecord` x8, `finite` x3, …), candle equality x4, `toFiniteNumber`/`dedupeByTime`, bucket aggregation x2, `makeTicker` x2 | `web/src/data/*` | ~160 |
| Server mirrors of web stream parsing and backoff | `server/src/upstream/streamParse.ts`, `hub.ts` vs `web/src/data/binanceStream.ts`, `binanceTicker.ts` | ~120 (needs a shared package) |
| Chart layers: shared primitive base (uid, visible, attach/detach, paneViews, updateAllViews, types) | `marketStructureLayer.ts:52-53,593-690`, `volumeProfileLayer.ts:48-49,520-609`, `sessionRangesLayer.ts:54-55,378-479`, `drawings/primitives.ts:33-34` | ~160 |
| Chart layers: shared layer host (entries Map, keep-set sweep, setColors fan-out, uids) and coords adapter | three layers | ~160 |
| Lineage prefix check verbatim x2 (+ volume profile missing it), incremental engine entry block x2 | `marketStructureLayer.ts:796-808,837-873`, `sessionRangesLayer.ts:567-579,615-640` | ~60 |
| `lowerBound` binary search x9, luminance x4 (two different formulas), `clampX`/`hLine`/`pad2`, store+suspend/resume x2, "plotted or whitespace" builders x2 | listed in audit | ~165 |
| Candle to chart point mappers x3, volume mappers x2, `toLineData` x2, `as UTCTimestamp` casts in 9 files | `volume.ts`, `seriesSync.ts`, `chartTypes.ts`, `indicatorSeries.ts`, `backtestOverlay.ts` | ~40 |
| React: localStorage try/catch load/save x6, persist-on-change effects x8, hand-rolled Escape x12 (vs `useModalKeys` used by 3), three dropdown menus re-implementing open/roving/outside-click, controller-in-state hook pattern x7 | `prefs.ts`, `regimePref.ts`, `derivativesPref.ts`, `settings.ts`, `chartTools.ts`, `watchlist.ts`; `App.tsx:575-592,658,665,1219`; `TimeframePicker`, `ChartTypeMenu`, `LayoutMenu` | ~250 |
| CSS: icon button x7, tab primitive x7, dialog shell x4, focus ring x54, thin scrollbar x14, panel head/title/empty chrome x9-20 | `Alerts.css`, `Backtest.css`, `Research.css`, `Paper.css`, `Watchlist.css`, `Replay.css`, `Indicators.css`, `Settings.css`, `Seasonals.css`, `Shortcuts.css` | 600 to 900 |

Total: roughly 1,700 to 2,000 lines, about 4% of the 52k-line web tree, concentrated where fixes would otherwise have to land several times.

## 4. What is already good (do not re-do)

- Live ticks use `series.update()` for main, volume, Heikin-Ashi and indicators (`Chart.tsx:806-814`); no `setData` per tick. This matches the Lightweight Charts guidance exactly.
- `IndicatorRunner` checkpoint replay is exact and O(tail) (`indicators/runner.ts:71-108`).
- `BarLineage` gives O(1) prefix checks for structure and session layers (`structureBars.ts:47-71`, `Chart.tsx:826`).
- `barsKey` gating keeps regime, derivatives and backtest overlays idle on forming-bar ticks.
- All three `useSyncExternalStore` stores return cached snapshots (`indicatorSeries.ts:703-706`); no tearing or loop risk.
- All six `memo()` boundaries hold; a tick on one ChartPanel does not re-render the others.
- Clients and providers are module-level singletons; nothing is created per render.
- Ticker emits are throttled to 250 ms and the socket survives watchlist edits.
- Reconnect/backoff is bounded, with no leaked-socket path on fast switching; the server hub does one upstream per key with serialize-once fan-out, backpressure and a 5 s grace.
- Canvas: all layers draw from cached geometry inside `useBitmapCoordinateSpace`, one save/restore per draw, no `measureText` in the frame path, visible-range culling by binary search, incremental O(1)/O(k) engines for structure and sessions, O(count + rows) volume-profile histogram.
- CSS: 82 tokens in `index.css`, no literal colours in component files beyond four swatches, zero `backdrop-filter`/`transition: all`/`will-change`, keyframes animate only opacity and transform, `prefers-reduced-motion` honoured, theme flash prevented by the inline script in `index.html`, theme toggle recolours via `applyOptions` without rebuilding the chart.
- Heavy views are conditionally mounted, not hidden with CSS.

## 5. What current guidance says, applied here

Sources were read via CrawlForge on 2026-10-30; primary sources preferred.

**Lightweight Charts (official skill file and plugin docs).** Load history once with `setData`, push ticks with `update`; never drive `setData` from props on every render; `autoscaleInfoProvider` runs on every layout pass so keep it allocation-free; primitives should do their work in `updateAllViews` and keep `draw()` to coordinate conversion and canvas calls; `useBitmapCoordinateSpace` for pixel-aligned strokes; `useBitmapCoordinateSpace`/`useMediaCoordinateSpace` already save and restore the context, so extra `save`/`restore` inside them is redundant; there is no prepend API, so build the new array and `setData` once, saving and restoring the visible logical range. This project follows all of it on the tick path; findings B1 to B4 are where the `updateAllViews`-versus-`draw()` split is not yet clean.

**React 19 and the React Compiler (react.dev, @vitejs/plugin-react README).** The compiler went 1.0 stable in October 2025 and inserts per-expression memoisation; Meta reported ~12% render-time reduction on heavy screens and 2.5x fewer re-renders in navigation-heavy surfaces, with the biggest wins exactly where an expensive child re-renders because its parent re-renders every frame (this app's A2). With `@vitejs/plugin-react` v6 (installed here: 6.1.2) Babel is gone, so the old `react({ babel: ... })` form no longer works. Two supported routes: the experimental Rust port (`npm i -D oxc-transform-react`, then `react({ compiler: true })`), or Babel via `@rolldown/plugin-babel` with the exported `reactCompilerPreset()`. Recommended adoption: install `eslint-plugin-react-compiler` first and fix violations, start with `compilationMode: 'annotation'` and `"use memo"` on the shell components, then switch to `'all'`; do not bulk-delete existing `useMemo`/`useCallback`; the compiler is additive. `useSyncExternalStore` docs: `getSnapshot` must return the same reference while the store is unchanged (already true here), and it is the recommended way to keep high-frequency external data out of parent state.

**High-frequency WebSocket UI (websocket.org React guide, several 2026 practitioner posts).** Under ~1 message/s plain state is fine; 1 to 50 messages/s should buffer in a ref and flush on a timer or animation frame; above ~50/s bypass React and write to canvas or DOM via refs. Binance klines (~1 push per 2 s) sit in the first band, Coinbase per-trade bars (A5) and 1s klines sit in the second, so the rAF-flush pattern is warranted there and the existing ticker throttle is the right model. Components should "select narrowly so a single symbol's tick does not re-render an entire grid" (A2, A6).

**Vite 8 / rolldown (rolldown.rs manual code splitting, Vite performance guide).** `build.rolldownOptions.output.codeSplitting.groups` with regex tests is the replacement for `manualChunks`; splitting vendor from app code stops full cache invalidation per deploy and lets the browser download chunks in parallel; `advancedChunks` is deprecated. Vite's own guide also recommends avoiding barrel files and warming frequently used large files in dev (`server.warmup`) for faster dev-server starts; `App.tsx` at 2,070 lines and `Chart.tsx` at 1,462 are the natural warm-up candidates.

**Fonts (web.dev font best practices, Fontsource).** Self-hosted woff2 with `font-display: swap` is already in place; remaining gains are fewer weights or a variable font (one file per family) and preloading the two body weights so the first paint does not wait on a late-discovered stylesheet reference.

**Web Workers.** Several sources recommend moving indicator computation off the main thread. For this app it is a last resort rather than a first move: once C3 (running-sum primitives) and A1 (no per-tick scans) land, the remaining compute per tick is O(tail) and the worker's structured-clone cost would exceed it. A worker becomes worthwhile only for full recomputes on very long histories (150k bars with many indicators), and could be scoped to `runner.reset` alone.

## 6. Proposed plan (for the owner to add to PLAN.md)

Ordered by impact per unit of work. Each wave is independently shippable and verifiable.

**Wave 1: measure, then remove per-tick waste (small, highest payoff)**
1. Record a baseline (section 7) on BTCUSDT 1m with 2 indicators and 1 drawing, single and 4-panel.
2. A1: lineage/version-based tail detection in `candleStore`, `plottable`, `seriesSync`; volume profile adopts the shared `BarLineage` (A8).
3. A2/A3/A6: `lives` into an external store; memo the shell and `WatchlistRow`; keydown handler reads `lastClose` from a ref; stable `useLiveCandles` return.
4. A5: coalesce Coinbase candle emits per frame.
5. A4: watched-symbol filter before object construction; server hub filters per client.

**Wave 2: frame-path hygiene in primitives**
6. B1: precompute strings/dashes/labels in `updateAllViews` for drawings.
7. B2/B3: geometry signature cache across the three layers; cull before building volume-profile geometry; shared barSpacing helper.
8. B4/B5/B6: hit-test against frame shapes; memoised legend rows; hoisted dash arrays.

**Wave 3: history and network**
9. C3: ring-buffer primitives with running sums; head-splice on prepend.
10. C1/C2: serve the forming bar from the hub; `@fastify/compress`; `Cache-Control: immutable` for closed pages; compact kline arrays if the win justifies the client change.
11. C4 to C9 as follow-ups.

**Wave 4: startup**
12. D1/D2: move the two icons out of the heavy panels; `React.lazy` the five large views; vendor `codeSplitting.groups`.
13. D3: four font weights or variable fonts; preload body weights.
14. D4: `eslint-plugin-react-compiler`, fix the setState-during-render sites, enable the compiler in annotation mode, then `'all'`.

**Wave 5: DRY consolidation (can interleave with waves 1 to 4 where a fix touches the duplicate anyway)**
15. `web/src/data/http.ts` (typed request helper) and `web/src/data/guards.ts`; one `storedValue<T>` + `useStoredState`; `useMenu` hook; extend `useModalKeys`.
16. `chart/layers/base.ts` (primitive base, layer host, coords adapter, lineage prefix memo), `chart/util/search.ts` (`lowerBound`), one luminance function.
17. `components/primitives.css` for icon buttons, tabs, dialog shell, focus ring, scrollbars, panel chrome.

## 7. How to verify (before any wave is called done)

- Chrome DevTools Performance: 10 s trace on BTCUSDT 1m, single panel, 2 indicators, 1 trendline, then with the mouse moving over the chart; record main-thread busy %, long tasks, and scripting time per tick. Repeat in a 4-panel layout. Target: no React commit on a forming-bar tick outside ChartPanel; per-tick scripting independent of history length.
- React DevTools Profiler with "highlight updates": confirm Header, SidebarRail, StatusBar and Watchlist rows do not flash on ticks for other panels.
- Memory: allocation sampling during 60 s of ticks; expect allocations per tick to be O(indicators), not O(bars).
- Network: `/api/candles` on a warm cache should make zero upstream calls (server log); `/ws/tickers` bytes per second should drop by the ratio of watched to total symbols.
- Build: `vite build` should print several chunks with `react`, `lightweight-charts` and panel chunks; main chunk under ~150 KB gzip; `dist/assets/*.css` split per lazy chunk.
- Existing tests stay green (`npx tsc -b`, `npx vitest run`, `npm run lint` in `web/`; `npx vitest run` in `server/`); the perturbation and no-look-ahead tests cover any indicator primitive rewrite.

## 8. Sources

- Lightweight Charts official skill file: https://github.com/tradingview/lightweight-charts/blob/master/.github/skills/lightweight-charts/SKILL.md
- Lightweight Charts plugin docs, series primitives and canvas rendering target: https://tradingview.github.io/lightweight-charts/docs/plugins/series-primitives , https://tradingview.github.io/lightweight-charts/docs/plugins/canvas-rendering-target
- Lightweight Charts advanced React example: https://tradingview.github.io/lightweight-charts/tutorials/react/advanced
- @vitejs/plugin-react README (React Compiler options, v6): https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react/README.md
- React Compiler introduction: https://react.dev/learn/react-compiler/introduction
- useSyncExternalStore reference: https://react.dev/reference/react/useSyncExternalStore
- Rolldown manual code splitting: https://rolldown.rs/in-depth/manual-code-splitting
- Vite performance guide: https://vite.dev/guide/performance
- Vite 8 / rolldown migration notes: https://whoooop.co.uk/blog/vite-8-rolldown-migration , https://laplusda.com/en/posts/vite-8-manualchunks-rolldown/
- React Compiler 1.0 + Vite 8 install flow (secondary, cross-checked against the README): https://recca0120.github.io/en/2026/04/14/react-compiler-vite-v6/
- WebSockets in React, message-rate bands: https://websocket.org/guides/frameworks/react/
- rAF buffering for WebSocket UIs: https://dev.to/nainikmehta/react-websocket-performance-raf-buffering-pattern-1ien , https://www.harshmittal.dev/build/realtime-trading-dashboard
- Font best practices: https://web.dev/articles/font-best-practices ; variable Fontsource packages: https://github.com/lexicora/lexicora-extension/issues/217

Web content above is untrusted input; only the primary sources were used for API facts, and every project-specific claim was checked against the local source at the cited line.
