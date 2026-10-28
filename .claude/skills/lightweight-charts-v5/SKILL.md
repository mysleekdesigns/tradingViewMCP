---
name: lightweight-charts-v5
description: Lightweight Charts v5 (5.2) API patterns for this app - createChart options, addSeries with series definitions, volume on an overlay scale, panes via paneIndex, crosshair legend, live update, infinite history, price lines, series markers, primitives for drawing tools, screenshots, conflation, series hit testing (hoveredInfo), the official @tradingview/lwc-plugin-* packages (accessibility), and the common time/scale/marker/plugin foot-guns. Use when writing or reviewing anything in web/src/chart/, indicator rendering, drawing tools, or backtest trade markers.
user-invocable: false
---

# Lightweight Charts v5

The app pins `lightweight-charts` 5.2.x. The v5 API differs from v4 and from most examples online. **Never** use `chart.addCandlestickSeries()` / `addLineSeries()` / `series.setMarkers()` / the `watermark` chart option: those are v4.

**Source lookup order:** `node_modules/lightweight-charts/package.json` (version) → `node_modules/lightweight-charts/dist/typings.d.ts` (API surface; it wins over docs, release notes and this file) → official docs → the `researcher` subagent. If a name is not in the installed typings, it does not exist; say what you could not verify rather than inventing an option, method or export.

| v4 / stale | v5 |
|---|---|
| `chart.addLineSeries(o)` / `addCandlestickSeries(o)` | `chart.addSeries(LineSeries, o)` / `chart.addSeries(CandlestickSeries, o)` |
| `series.setMarkers(m)` | `createSeriesMarkers(series, m)` → `.setMarkers()` / `.markers()` |
| chart `watermark` option | `createTextWatermark(pane, opts)`, `createImageWatermark(pane, url, opts)` |
| `IPanePrimitivePaneRenderer` | `IPrimitivePaneRenderer` (the asymmetry is deliberate) |
| `timeScale: { timezone }` | does not exist: format with `Intl.DateTimeFormat` in formatters |
| `time: Date.now()` | `Math.floor(Date.now() / 1000) as UTCTimestamp` |

## Create, theme, clean up

```ts
import { createChart, CandlestickSeries, HistogramSeries, LineSeries, ColorType, CrosshairMode,
         type IChartApi, type ISeriesApi, type UTCTimestamp } from 'lightweight-charts';

const chart = createChart(container, {
  autoSize: true,                                    // resizes with the container; no ResizeObserver needed
  layout: { background: { type: ColorType.Solid, color: '#131722' }, textColor: '#d1d4dc' },
  grid: { vertLines: { color: '#1e222d' }, horzLines: { color: '#1e222d' } },
  crosshair: { mode: CrosshairMode.Normal },
  timeScale: { timeVisible: true, secondsVisible: false },
});
// in the React effect cleanup:
return () => chart.remove();
```

- Create the chart **once** per container in an effect; never per render. Apply theme changes with `chart.applyOptions(...)`.
- `applyOptions`: `undefined` means "not specified", not "reset". To clear `localization.priceFormatter` / `timeFormatter` or an `autoscaleInfoProvider`, pass `null`; other options have no reset value, so pass the default explicitly.
- Series types are tree-shaken: import every series definition you pass to `addSeries`.

## Series

```ts
const candles = chart.addSeries(CandlestickSeries, {
  upColor: '#26a69a', downColor: '#ef5350', borderVisible: false,
  wickUpColor: '#26a69a', wickDownColor: '#ef5350',
});
const volume = chart.addSeries(HistogramSeries, { priceFormat: { type: 'volume' }, priceScaleId: '' });
volume.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });   // bottom 20% of the pane

candles.setData(bars);   // bars: { time: UTCTimestamp, open, high, low, close }[], ascending, unique times
volume.setData(bars.map(b => ({ time: b.time, value: b.volume, color: b.close >= b.open ? '#26a69a80' : '#ef535080' })));
```

- Per-bar colours go on the **data point** (`color`, `borderColor`, `wickColor` on candles; `color` on histogram/line points). Never add series or markers to recolour bars.
- Other definitions: `LineSeries`, `AreaSeries`, `BarSeries`, `BaselineSeries`. Hollow candles: transparent `upColor` + visible borders. Heikin-Ashi: compute bars yourself, then `CandlestickSeries`.
- `lastValueVisible` / `priceLineVisible` control the live price label and line. Per-series `priceFormat: { type: 'custom', formatter, minMove }` beats the chart-level `localization.priceFormatter`.
- Overlay series (`priceScaleId: ''` or a custom id) get a hidden autoscaled scale. `autoscaleInfoProvider` runs every layout pass: keep it cheap, no allocation.

## Time and data (foot-guns)

- `time` is UNIX **seconds** cast to `UTCTimestamp`. Strictly ascending, unique per series, one time format per series (never mix `UTCTimestamp` with business-day strings).
- Gaps are **whitespace points** `{ time }` (no value). Never `value: null` / `NaN`. (The app never forward-fills gaps.)
- `setData` replaces the dataset and can move the view; `update(bar)` replaces the last bar when `time` matches and appends when newer. Live ticks use `update`, never `setData` per tick. `update(point, true)` can rewrite an older bar but cannot insert one.
- No prepend API: to add older history, merge + dedupe by `time`, `setData` once, and keep the view (the logical range shifts by the number of bars prepended).
- `setVisibleLogicalRange` is in bar indices; `setVisibleRange` is in time; `fitContent()` resets.
- Time zones are display-only: `timeScale.tickMarkFormatter` for axis ticks, `localization.timeFormatter` for crosshair labels, both via `Intl.DateTimeFormat(..., { timeZone })`. Never shift timestamps.

## Panes (oscillators)

```ts
const rsi = chart.addSeries(LineSeries, { color: '#7e57c2', lineWidth: 1 }, 1);   // paneIndex one past the last creates a pane
rsi.createPriceLine({ price: 70, color: '#787b86', lineStyle: 2, axisLabelVisible: false, title: '' });
chart.panes()[1].setHeight(120);
```
- Remove an indicator with `chart.removeSeries(series)`; drop an emptied pane with `chart.removePane(index)`.
- Indices shift when panes are added, removed, swapped (`swapPanes`) or moved (`pane.moveTo`). Hold the `IPaneApi` and read `pane.paneIndex()` instead of caching numbers. `pane.getHTMLElement()` (null until built) is the place for DOM overlays.

## Crosshair legend and coordinates

```ts
chart.subscribeCrosshairMove(param => {
  const bar = param.time ? param.seriesData.get(candles) : undefined;   // undefined off-chart and between bars
  setLegend(bar ?? lastBar);
});
```
- Unsubscribe in cleanup with `chart.unsubscribeCrosshairMove(handler)` (keep the handler reference).
- Convert with the public APIs only: `series.coordinateToPrice(y)` / `priceToCoordinate(price)`, `timeScale().coordinateToLogical(x)` / `logicalToCoordinate` / `timeToCoordinate(time)`. All may return `null`; handle it. Never infer price/time from canvas DOM geometry.

## Infinite history

```ts
chart.timeScale().subscribeVisibleLogicalRangeChange(range => {
  if (range && range.from < 10 && !loading) loadOlder();   // fetch with endTime = oldest.time * 1000 - 1
});
```
Merge older bars, dedupe by `time`, then `setData` once. Guard against concurrent loads and against reaching the listing date (empty response).

## Markers (backtest trades)

```ts
import { createSeriesMarkers } from 'lightweight-charts';
const markers = createSeriesMarkers(candles, []);
markers.setMarkers([{ time, position: 'belowBar', color: '#26a69a', shape: 'arrowUp', text: 'L' }]);   // sorted by time
```
`time`, `position`, `shape` and `color` are required. A marker whose `time` is not a bar of that series is **dropped silently**: snap fills to bar open times first.

## Price lines (alerts, horizontal lines)

`const line = candles.createPriceLine({ price, color, lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: 'Alert' });` and `candles.removePriceLine(line)`.

## Drawing tools = primitives

Attach with `candles.attachPrimitive(p)` (series primitive: tied to that series' scale) or `pane.attachPrimitive(p)` (pane primitive: spans the pane, outlives any series); detach with the matching `detachPrimitive`. A primitive implements `attached({ chart, series, requestUpdate })`, `detached()`, `updateAllViews()`, and `paneViews()` returning views whose `renderer()` returns an `IPrimitivePaneRenderer` with `draw(target)`.
- `CanvasRenderingTarget2D` is imported from `fancy-canvas`, not `lightweight-charts`.
- Crisp strokes in `target.useBitmapCoordinateSpace(scope => …)`, scaling by `scope.horizontalPixelRatio` / `verticalPixelRatio`; text and CSS-pixel layout in `useMediaCoordinateSpace`. Mixing them blurs on HiDPI.
- Restore what you set on the context (`save`/`restore`, dash, `globalAlpha`): the next renderer shares it. A `null` coordinate means off-scale: skip the point, never draw to `NaN`.
- Store anchors as `{ time, price }`, never pixels; convert per frame. Call `requestUpdate()` after any change the chart cannot see. `detached()` undoes everything `attached()` did (listeners, DOM, timers), because React effects attach and detach often.
- Hit testing: implement `hitTest(x, y)` on the **primitive** (returns `PrimitiveHoveredItem`: `externalId`, `zOrder`, optional `distance`, `hitTestPriority` 0 range / 1 line / 2 point, `cursorStyle`), or compare `param.point` from `subscribeClick`. Interaction (click, drag) stays in chart subscriptions.
- Prefer a primitive over a custom series (`ICustomSeriesPaneView`) unless the thing needs its own data, autoscale and price line. Start from `npm create lwc-plugin@latest` or the upstream `plugin-examples/` rather than a blank file.

## 5.1 / 5.2 features (check typings before use)

- **Conflation (5.1)**, render-only, off by default: `timeScale: { enableConflation: true, conflationThresholdFactor?: number, precomputeConflationOnInit?: boolean, precomputeConflationPriority?: 'background' | 'user-visible' | 'user-blocking' }`. Bars that would draw narrower than ~0.5 px are merged for drawing; `series.data()`, indicators and the legend still see every bar. Per-series `conflationThresholdFactor` overrides the time-scale value (higher factors suit line/area, look odd on candles). Custom series opt in with `conflationReducer`.
- **Series hit testing (5.2)**: `MouseEventParams.hoveredInfo?: HoveredInfo` with `type` (`'series-point' | 'series-line' | 'series-range' | 'marker' | 'price-line' | 'primitive' | 'custom'`), `sourceKind` (`'series' | 'series-primitive' | 'pane-primitive'`), `objectKind`, `series?`, `objectId?`, `paneIndex?`. `hoveredSeries` / `hoveredObjectId` are deprecated in favour of it. The 5.2 release notes' `hoveredItem` / `hoveredTarget` names are **stale**: the 5.2.1 typings only have `hoveredInfo`. Custom series can return `hitTest` results from their renderer; keep the same `hitTestData` object while the same item stays hovered (compared by reference, otherwise every move repaints).
- `hoveredSeriesOnTop` (chart option, default `true`) draws and hit-tests the hovered series above its pane siblings without changing stored order.
- **Screenshots**: `chart.takeScreenshot(addTopLayer?: boolean, includeCrosshair?: boolean)` returns an `HTMLCanvasElement` → `.toDataURL('image/png')` / `.toBlob()`. Positional booleans, not an options object. Without `addTopLayer: true` the top layer and its primitives (our drawings) are left out; `includeCrosshair` only works with `addTopLayer`.
- **Pane API (5.0.8+)**: `chart.addPane(preserveEmpty?)`, `swapPanes`, `pane.moveTo`, `pane.getStretchFactor/setStretchFactor`, `pane.setPreserveEmptyPane`, `pane.getSeries()`.

## Official plugin packages (`@tradingview/lwc-plugin-*`, Apache-2.0)

Adopt one only where it replaces hand-written code or adds a capability we lack; pin exact versions; peer `lightweight-charts ^5.0.0`.
- **`@tradingview/lwc-plugin-accessibility`** (adopted, `web/src/chart/chartAccessibility.ts`): `addAccessibilityPlugin(chart, opts)` → `{ plugins, detach(), focus(paneIndex?), refresh(), applyOptions() }`. One focusable `role="application"` layer per pane (new panes picked up on redraw): ←/→ bars, ↑/↓ series, PageUp/PageDown, Home/End, +/- zoom, Enter/Space summary, aria-live announcements. Its defaults H (help), T (table) and Escape (always `preventDefault`s) collide with type-to-search and with drawing/replay Escape, so the wrapper rebinds help to `,` and table to `.` and unbinds Escape via `keyBindings` (a `null` removes a built-in; keys by `e.key`, physical fallbacks by `e.code`); its spoken `messages` are reworded to match. It ignores Alt/Ctrl/Meta chords and Shift+non-printable keys. Use `dataUpdates: { mode: 'none' }` on live feeds. `detach()` before `chart.remove()` (it tolerates a removed chart).
- **`@tradingview/lwc-plugin-vertical-line`**: `new VerticalLine(time, opts)` + `series.attachPrimitive`; `snap: 'exact'` (default) draws nothing on a non-bar time. Not adopted: no hand-written vertical line exists to replace.
- `lwc-plugin-anchored-text` exists only in the GitHub repo (not on npm); the watermark already uses `createTextWatermark`. `@tradingview/lwc-toolkit` holds authoring helpers; `create-lwc-plugin` scaffolds a plugin.

## Misc

- Multi-chart sync: mirror `subscribeVisibleLogicalRangeChange` into the others' `timeScale().setVisibleLogicalRange()`, crosshair via `setCrosshairPosition(price, time, series)`. Publish only user-originated moves and guard against feedback loops.
- Price-scale width is readable (`chart.priceScale(id).width()`), not settable; use it to align neighbouring containers.

Parts of this skill are condensed from TradingView's official Lightweight Charts Agent Skills (`.github/skills/lightweight-charts/SKILL.md` and `.github/skills/lightweight-charts-plugin-authoring/SKILL.md` in https://github.com/tradingview/lightweight-charts, Apache-2.0), merged 2026-10-28 against the installed 5.2.1 typings.
