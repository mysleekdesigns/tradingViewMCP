---
name: lightweight-charts-v5
description: Lightweight Charts v5 API patterns for this app - createChart options, addSeries with series definitions, volume on an overlay scale, panes via paneIndex, crosshair legend, live update, infinite history, price lines, series markers, primitives for drawing tools, screenshots. Use when writing or reviewing anything in web/src/chart/, indicator rendering, drawing tools, or backtest trade markers.
user-invocable: false
---

# Lightweight Charts v5

The v5 API differs from v4 and from most examples online. **Never** use `chart.addCandlestickSeries()` / `addLineSeries()` / `series.setMarkers()`: those are v4. If unsure of a signature, check `node_modules/lightweight-charts/dist/typings.d.ts` (the source of truth for the installed version) or ask the `researcher` subagent.

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

- `time` is UNIX **seconds** cast to `UTCTimestamp`. Data must be sorted ascending with unique times, or `setData` throws.
- Live ticks: `candles.update(bar)` updates the last bar if `time` matches, appends if newer. Never `setData` per tick.
- Other series definitions: `LineSeries`, `AreaSeries`, `BarSeries`, `BaselineSeries`. Hollow candles: transparent `upColor` + visible borders. Heikin-Ashi: compute the bars yourself, then use `CandlestickSeries`.
- `lastValueVisible` / `priceLineVisible` on series options control the live price label and line.

## Panes (oscillators)

```ts
const rsi = chart.addSeries(LineSeries, { color: '#7e57c2', lineWidth: 1 }, 1);   // third arg = paneIndex; 1 creates a pane below
rsi.createPriceLine({ price: 70, color: '#787b86', lineStyle: 2, axisLabelVisible: false, title: '' });
chart.panes()[1].setHeight(120);
```
Remove an indicator with `chart.removeSeries(series)`; drop an emptied pane with `chart.removePane(index)`.

## Crosshair legend

```ts
chart.subscribeCrosshairMove(param => {
  const bar = param.time ? param.seriesData.get(candles) : undefined;   // undefined when off-chart
  setLegend(bar ?? lastBar);
});
```
Unsubscribe in cleanup with `chart.unsubscribeCrosshairMove(handler)` (keep a reference to the handler).

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

## Price lines (alerts, horizontal lines)

`const line = candles.createPriceLine({ price, color, lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: 'Alert' });` and `candles.removePriceLine(line)`.

## Drawing tools = primitives

Attach with `candles.attachPrimitive(primitive)`; detach with `candles.detachPrimitive(primitive)`. A primitive implements `attached({ chart, series, requestUpdate })`, `updateAllViews()`, and `paneViews()` returning views whose `renderer().draw(target)` draws with `target.useBitmapCoordinateSpace(scope => { /* scope.context, scope.horizontalPixelRatio */ })`.
- Store anchors as `{ time, price }`, never pixels. Convert per frame with `chart.timeScale().timeToCoordinate(time)` and `series.priceToCoordinate(price)`. Both return `null` off-screen; handle it.
- Hit-testing for select/move: implement `hitTest(x, y)` on the pane view, or compare `param.point` from `subscribeClick`.
- Start with horizontal line → trendline → rectangle → Fibonacci (PLAN.md Phase 6).

## Misc

- Screenshot: `chart.takeScreenshot()` returns an `HTMLCanvasElement` → `.toDataURL('image/png')`.
- Multi-chart sync: mirror `subscribeVisibleLogicalRangeChange` into the other charts' `timeScale().setVisibleLogicalRange()`, and crosshair via `setCrosshairPosition(price, time, series)`. Guard against feedback loops.
- Apply theme changes with `chart.applyOptions(...)`; do not recreate the chart.
