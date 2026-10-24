// A lab backtest drawn on the chart: trade markers on the main series (createSeriesMarkers) and
// an equity pane below the price and indicator panes (strategy vs buy-and-hold vs vol-target).
//
// Only bars that are loaded on the chart are drawn: markers and equity points are filtered to
// candle times, so the overlay never stretches the time scale past the loaded history (the lab
// window usually starts years before the chart's first bar). Loading older history widens it.
// The chart plots traded bars only, so a trade whose bar had no trades on the exchange (thin
// pairs) has its marker moved to the next plotted bar instead of disappearing.

import {
  LineStyle,
  type LineData,
  type LineSeriesPartialOptions,
  type SeriesMarker,
  type UTCTimestamp,
} from 'lightweight-charts'
import type { BacktestReport, EquityPoint, ExitReason, LabTrade } from '../data/labTypes'
import type { Candle } from '../types'

export type EquityLineId = 'strategy' | 'buy_and_hold' | 'vol_target'

export interface EquityLine {
  id: EquityLineId
  title: string
  color: string
  dashed: boolean
  width: 1 | 2
  /** Ascending, unique times. */
  points: readonly EquityPoint[]
}

/** Everything the chart needs to draw one backtest. `key` changes per report. */
export interface BacktestChartData {
  key: string
  markers: readonly SeriesMarker<UTCTimestamp>[]
  equity: readonly EquityLine[]
}

export const backtestColors = {
  entry: '#2962ff',
  win: '#089981',
  loss: '#f23645',
  strategy: '#2962ff',
  buyAndHold: '#9598a1',
  volTarget: '#f7a600',
} as const

export const EXIT_LABELS: Record<ExitReason, string> = {
  signal: 'Exit',
  stop_loss: 'SL',
  take_profit: 'TP',
  end: 'End',
}

/** "+4.2%" / "−1.0%" (one decimal, real minus sign). */
export function signedPct(fraction: number, dp = 1): string {
  const pct = fraction * 100
  const rounded = Number(pct.toFixed(dp))
  if (rounded === 0) return `${(0).toFixed(dp)}%`
  return `${rounded > 0 ? '+' : '−'}${Math.abs(rounded).toFixed(dp)}%`
}

/**
 * One arrowUp below the entry bar and one arrowDown above the exit bar per trade (an open trade
 * has no exit marker). Exit colour = win/loss; exit text = reason and return, e.g. "SL −2.0%".
 * Sorted by time (LWC requires it); at equal times the exit comes first.
 */
export function buildTradeMarkers(trades: readonly LabTrade[]): SeriesMarker<UTCTimestamp>[] {
  const out: { marker: SeriesMarker<UTCTimestamp>; order: number }[] = []
  trades.forEach((t, i) => {
    out.push({
      order: 1,
      marker: {
        id: `bt-entry-${i}`,
        time: t.entry_time as UTCTimestamp,
        position: 'belowBar',
        shape: 'arrowUp',
        color: backtestColors.entry,
        text: 'Long',
      },
    })
    if (t.exit_time === null) return
    out.push({
      order: 0,
      marker: {
        id: `bt-exit-${i}`,
        time: t.exit_time as UTCTimestamp,
        position: 'aboveBar',
        shape: 'arrowDown',
        color: t.pnl >= 0 ? backtestColors.win : backtestColors.loss,
        text: `${EXIT_LABELS[t.exit_reason]} ${signedPct(t.return_pct)}`,
      },
    })
  })
  out.sort((a, b) => a.marker.time - b.marker.time || a.order - b.order)
  return out.map((m) => m.marker)
}

/** Markers + equity lines for a report. The vol-target line is drawn only when present. */
export function buildChartData(report: BacktestReport): BacktestChartData {
  const equity: EquityLine[] = [
    {
      id: 'strategy',
      title: 'Strategy',
      color: backtestColors.strategy,
      dashed: false,
      width: 2,
      points: report.equity,
    },
    {
      id: 'buy_and_hold',
      title: 'Buy & hold',
      color: backtestColors.buyAndHold,
      dashed: false,
      width: 1,
      points: report.benchmark_equity.buy_and_hold,
    },
  ]
  if (report.benchmark_equity.vol_target.length > 0) {
    equity.push({
      id: 'vol_target',
      title: 'Vol-target',
      color: backtestColors.volTarget,
      dashed: true,
      width: 1,
      points: report.benchmark_equity.vol_target,
    })
  }
  return {
    key: `${report.run_id}#${report.trial_id}#${report.plan_hash}`,
    markers: buildTradeMarkers(report.trades),
    equity,
  }
}

/**
 * Markers on the plotted bars `times` (ascending). A marker on a plotted bar stays; one inside
 * the plotted range but on a bar that is not plotted (no trades) moves to the next plotted bar;
 * one before the first or after the last plotted bar is dropped. `markers` must be sorted by
 * time; the mapping never decreases time, so the result stays sorted and keeps marker order.
 */
export function snapMarkersToBars(
  markers: readonly SeriesMarker<UTCTimestamp>[],
  times: readonly number[],
): SeriesMarker<UTCTimestamp>[] {
  const n = times.length
  if (n === 0) return []
  const out: SeriesMarker<UTCTimestamp>[] = []
  for (const m of markers) {
    if (m.time < times[0] || m.time > times[n - 1]) continue
    let lo = 0
    let hi = n - 1
    while (lo < hi) {
      const mid = (lo + hi) >>> 1
      if (times[mid] < m.time) lo = mid + 1
      else hi = mid
    }
    out.push(times[lo] === m.time ? m : { ...m, time: times[lo] as UTCTimestamp })
  }
  return out
}

/** Keep only items whose time is a loaded bar. Order is kept. */
export function filterToBars<T extends { time: number }>(
  items: readonly T[],
  bars: ReadonlySet<number>,
): T[] {
  return items.filter((p) => bars.has(p.time))
}

export function toLineData(points: readonly EquityPoint[]): LineData<UTCTimestamp>[] {
  return points.map((p) => ({ time: p.time as UTCTimestamp, value: p.value }))
}

/** Equity axis label: change since the window start, e.g. 1.234 → "+23.4%". */
export function formatEquityAxis(value: number): string {
  return Number.isFinite(value) ? signedPct(value - 1) : ''
}

/** Value at `time` (the last point at or before it), or the last value when `time` is undefined. */
export function equityValueAt(points: readonly EquityPoint[], time?: number): number | null {
  if (points.length === 0) return null
  if (time === undefined) return points[points.length - 1].value
  let lo = 0
  let hi = points.length - 1
  if (time < points[0].time) return null
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1
    if (points[mid].time <= time) lo = mid
    else hi = mid - 1
  }
  return points[lo].value
}

/** How many of the report's trades have their entry bar loaded on the chart. */
export function tradesOnChart(trades: readonly LabTrade[], firstBar: number | null): number {
  if (firstBar === null) return 0
  return trades.filter((t) => t.entry_time >= firstBar).length
}

/** Most bars the automatic zoom after a backtest shows: readable candles, like TradingView. */
export const TRADES_ZOOM_MAX_BARS = 300
/** Bars of context on each side of the trades. */
const TRADES_ZOOM_PAD_BARS = 5

/**
 * Where the chart zooms once a backtest's history is loaded: the trades, ending at the most
 * recent one (as TradingView's strategy tester does). All trades when they span at most
 * `maxBars` bars, else the last `maxBars` bars up to the last trade's exit (an open trade runs
 * to the window end), with a few bars of padding. No trades: the last `maxBars` bars of the
 * window. `step` is the interval in seconds. The chart clamps the range to its loaded bars.
 */
export function tradesZoomRange(
  trades: readonly Pick<LabTrade, 'entry_time' | 'exit_time'>[],
  window: { start: number; end: number },
  step: number,
  maxBars = TRADES_ZOOM_MAX_BARS,
): { from: number; to: number } {
  const span = Math.max(1, maxBars) * step
  if (trades.length === 0) {
    return { from: Math.max(window.start, window.end - span), to: window.end }
  }
  let first = Infinity
  let last = -Infinity
  for (const t of trades) {
    first = Math.min(first, t.entry_time)
    last = Math.max(last, t.exit_time ?? window.end, t.entry_time)
  }
  const pad = TRADES_ZOOM_PAD_BARS * step
  const to = last + pad
  return { from: Math.max(first - pad, to - span), to }
}

export type FocusPlan =
  | { kind: 'apply'; from: number; to: number }
  /** The whole range is older than the first loaded bar (load more history first). */
  | { kind: 'before-data' }
  /** The whole range is newer than the last loaded bar. */
  | { kind: 'after-data' }
  | { kind: 'no-data' }

/**
 * Where a focus request [from, to] lands on the loaded bars [first, last]. LWC maps times only
 * inside the data, so a range overlapping it is clamped; a range wholly outside is skipped.
 */
export function planFocus(
  from: number,
  to: number,
  first: number | null,
  last: number | null,
): FocusPlan {
  if (first === null || last === null) return { kind: 'no-data' }
  const lo = Math.min(from, to)
  const hi = Math.max(from, to)
  if (hi < first) return { kind: 'before-data' }
  if (lo > last) return { kind: 'after-data' }
  const a = Math.max(lo, first)
  return { kind: 'apply', from: a, to: Math.max(Math.min(hi, last), a) }
}

// ---------------------------------------------------------------------------------------------
// Overlay manager
// ---------------------------------------------------------------------------------------------

/** The parts of an LWC line series the overlay uses. */
export interface OverlayLineSeries {
  setData(data: LineData<UTCTimestamp>[]): void
  getPane(): {
    paneIndex(): number
    getStretchFactor(): number
    setStretchFactor(factor: number): void
  }
}

/** The parts of the chart the overlay uses (an adapter over IChartApi; a fake in tests). */
export interface OverlayChart<L extends OverlayLineSeries = OverlayLineSeries> {
  addLine(options: LineSeriesPartialOptions, paneIndex: number): L
  removeSeries(series: L): void
  paneCount(): number
}

export interface MarkersHandle {
  setMarkers(markers: SeriesMarker<UTCTimestamp>[]): void
  detach(): void
}

export interface EquityLegendItem {
  id: EquityLineId
  title: string
  color: string
  text: string
}

/** Default height of the equity pane relative to the price pane (stretch 1). */
export const EQUITY_PANE_STRETCH = 0.35

export function equityLineOptions(line: EquityLine): LineSeriesPartialOptions {
  return {
    color: line.color,
    lineWidth: line.width,
    lineStyle: line.dashed ? LineStyle.Dashed : LineStyle.Solid,
    priceLineVisible: false,
    lastValueVisible: line.id === 'strategy',
    crosshairMarkerVisible: line.id === 'strategy',
    title: '',
    priceFormat: { type: 'custom', formatter: formatEquityAxis, minMove: 0.0001 },
  }
}

/**
 * Owns the backtest drawing on one chart. The chart effects drive it:
 * attachChart/detachChart with the chart, attachSeries/detachSeries with the main series
 * (re-created on a chart-type switch), setBars after candles change, setData per report, and
 * suspendPane/resumePane around indicator pane changes so the equity pane stays the last pane
 * and never shares a pane with an indicator. `alive` false = the chart is already removed.
 */
export class BacktestOverlay<S> {
  private chart: OverlayChart | null = null
  private series: S | null = null
  private markers: MarkersHandle | null = null
  private data: BacktestChartData | null = null
  private bars: ReadonlySet<number> | null = null
  /** The same bar times, ascending (for snapping markers to plotted bars). */
  private barTimes: readonly number[] = []
  private barsKey = ''
  private markerSig = ''
  private readonly lines = new Map<EquityLineId, OverlayLineSeries>()
  private shown: { line: EquityLine; points: EquityPoint[] }[] = []
  private suspended = false
  /** Equity pane height the user dragged to, kept across pane moves and redraws. */
  private stretch: number | null = null
  private version = 0
  private readonly listeners = new Set<() => void>()
  private readonly createMarkers: (series: S) => MarkersHandle

  constructor(createMarkers: (series: S) => MarkersHandle) {
    this.createMarkers = createMarkers
  }

  // ---- store (for the equity legend) ----

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  getSnapshot = (): number => this.version

  private emit(): void {
    this.version += 1
    for (const l of this.listeners) l()
  }

  // ---- lifecycle ----

  attachChart(chart: OverlayChart): void {
    this.detachChart(true)
    this.chart = chart
    this.applyEquity()
  }

  detachChart(alive: boolean): void {
    if (alive) this.removeLines()
    this.lines.clear()
    this.chart = null
    this.emit()
  }

  attachSeries(series: S): void {
    this.detachSeries(true)
    this.series = series
    this.markerSig = ''
    this.applyMarkers()
  }

  detachSeries(alive: boolean): void {
    if (this.markers && alive) this.markers.detach()
    this.markers = null
    this.series = null
    this.markerSig = ''
  }

  /** null clears the markers and removes the equity pane. */
  setData(data: BacktestChartData | null): void {
    if (data === this.data) return
    this.data = data
    this.markerSig = ''
    this.removeLines()
    this.recompute()
    this.applyMarkers()
    this.applyEquity()
  }

  /** The chart's candles changed. Cheap unless the first/last bar or the bar count changed. */
  setBars(candles: readonly Candle[]): void {
    const key =
      candles.length === 0
        ? ''
        : `${candles.length}:${candles[0].time}:${candles[candles.length - 1].time}`
    if (key === this.barsKey) return
    this.barsKey = key
    this.barTimes = candles.map((c) => c.time)
    this.bars = new Set(this.barTimes)
    const before = this.shown.map((s) => s.points.length).join(',')
    this.recompute()
    this.applyMarkers()
    if (this.shown.map((s) => s.points.length).join(',') !== before) this.applyEquity(true)
  }

  /** Before indicator panes change: take the equity pane out (its height is remembered). */
  suspendPane(): void {
    if (this.suspended) return
    this.suspended = true
    this.removeLines()
  }

  /** After indicator panes changed: put the equity pane back as the last pane. */
  resumePane(): void {
    if (!this.suspended) return
    this.suspended = false
    this.applyEquity()
  }

  // ---- queries ----

  /** Pane index of the equity curves, or null when none is drawn. */
  equityPaneIndex(): number | null {
    const first = this.lines.values().next()
    return first.done ? null : first.value.getPane().paneIndex()
  }

  /** Number of markers drawn (for tests and the panel's "N trades on chart"). */
  markerCount(): number {
    return this.markerSig === '' ? 0 : Number(this.markerSig.split(':')[0])
  }

  /** Legend values of the drawn equity lines at `time` (or their last value). */
  legend(time?: number): EquityLegendItem[] {
    if (this.lines.size === 0) return []
    return this.shown.map(({ line, points }) => {
      const v = equityValueAt(points, time)
      return {
        id: line.id,
        title: line.title,
        color: line.color,
        text: v === null ? '—' : formatEquityAxis(v),
      }
    })
  }

  // ---- internals ----

  private recompute(): void {
    const data = this.data
    const bars = this.bars
    if (!data || !bars) {
      this.shown = []
      return
    }
    this.shown = data.equity.map((line) => ({ line, points: filterToBars(line.points, bars) }))
  }

  private applyMarkers(): void {
    const series = this.series
    if (series === null) return
    const data = this.data
    const markers = data && this.bars ? snapMarkersToBars(data.markers, this.barTimes) : []
    const sig =
      markers.length === 0
        ? '0'
        : `${markers.length}:${markers[0].time}:${markers[markers.length - 1].time}`
    if (sig === this.markerSig && this.markers) return
    this.markerSig = sig
    if (markers.length === 0) {
      // Nothing to draw: drop the primitive entirely rather than keep an empty one.
      if (this.markers) {
        this.markers.detach()
        this.markers = null
      }
      return
    }
    if (!this.markers) this.markers = this.createMarkers(series)
    this.markers.setMarkers(markers)
  }

  /**
   * Create the equity pane when the strategy curve has loaded bars, refresh it, or remove it
   * when it has none. `refresh` re-sets data on existing lines (history grew).
   */
  private applyEquity(refresh = false): void {
    const chart = this.chart
    if (!chart || this.suspended) return
    const strategy = this.shown.find((s) => s.line.id === 'strategy')
    if (!strategy || strategy.points.length === 0) {
      if (this.lines.size > 0) this.removeLines()
      return
    }
    if (this.lines.size > 0) {
      if (refresh) {
        for (const { line, points } of this.shown)
          this.lines.get(line.id)?.setData(toLineData(points))
        this.emit()
      }
      return
    }
    let paneIndex = chart.paneCount()
    for (const { line, points } of this.shown) {
      const series = chart.addLine(equityLineOptions(line), paneIndex)
      series.setData(toLineData(points))
      this.lines.set(line.id, series)
      paneIndex = series.getPane().paneIndex()
    }
    this.lines
      .get('strategy')
      ?.getPane()
      .setStretchFactor(this.stretch ?? EQUITY_PANE_STRETCH)
    this.emit()
  }

  private removeLines(): void {
    const chart = this.chart
    if (this.lines.size === 0) return
    const first = this.lines.values().next().value
    if (first) this.stretch = first.getPane().getStretchFactor()
    if (chart) for (const series of this.lines.values()) chart.removeSeries(series)
    this.lines.clear()
    this.emit()
  }
}
