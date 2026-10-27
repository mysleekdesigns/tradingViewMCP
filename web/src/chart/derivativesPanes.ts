// Funding rate and open interest in two panes below the price / indicator panes, from the lab's
// historical USDⓈ-M archive (`POST /lab/derivatives`; no live futures stream).
//
// Each value is what was visible at its bar's close (aligned by the lab). Null values are
// whitespace, never interpolated. Points are drawn on plotted bars only (a point at a time the
// main series lacks would add a bar to the time scale). The panes sit after the indicator panes;
// the chart takes them out (`suspend`) while indicator panes change and puts them back
// (`resume`) as the last panes, before the backtest equity pane.

import {
  HistogramSeries,
  LineSeries,
  LineStyle,
  type HistogramData,
  type HistogramSeriesPartialOptions,
  type IChartApi,
  type LineData,
  type LineSeriesPartialOptions,
  type PriceLineOptions,
  type UTCTimestamp,
  type WhitespaceData,
} from 'lightweight-charts'
import type { DerivativesPoint } from '../data/labTypes'
import { withHexAlpha, type ChartPalette } from './theme'

/** What the chart draws: one point per closed bar sent to the lab (value null = no data). */
export interface DerivativesOverlay {
  /** Funding rate as a fraction (0.0001 = 0.01%). */
  funding: readonly DerivativesPoint[]
  /** Open interest value, USD notional. */
  openInterest: readonly DerivativesPoint[]
}

export interface DerivativesColors {
  /** Funding ≥ 0 (longs pay shorts). */
  positive: string
  /** Funding < 0. */
  negative: string
  /** Open-interest line. */
  line: string
  /** Zero line in the funding pane. */
  zero: string
}

export const FUNDING_ALPHA = 0.8

/** Funding bars follow the user's candle colours; the OI line is the theme's line colour. */
export function derivativesColorsFor(
  palette: Pick<ChartPalette, 'up' | 'down' | 'line' | 'mutedText'>,
): DerivativesColors {
  return {
    positive: withHexAlpha(palette.up, FUNDING_ALPHA),
    negative: withHexAlpha(palette.down, FUNDING_ALPHA),
    line: palette.line,
    zero: withHexAlpha(palette.mutedText, 0.6),
  }
}

export const FUNDING_TITLE = 'Funding %'
export const OPEN_INTEREST_TITLE = 'Open interest (USD)'
/** Pane heights relative to the price pane (stretch 1); indicator panes use 0.3. */
export const DERIVATIVES_PANE_STRETCH = 0.25

/** A funding fraction in percent with 4 decimals: 0.00037409 -> "0.0374%". */
export function formatFundingPct(fraction: number): string {
  return `${(fraction * 100).toFixed(4)}%`
}

/** Funding axis labels: the series holds percent already (value × 100). */
function formatPercentAxis(pct: number): string {
  return `${pct.toFixed(4)}%`
}

const COMPACT_UNITS: readonly [number, string][] = [
  [1e12, 'T'],
  [1e9, 'B'],
  [1e6, 'M'],
  [1e3, 'K'],
]

/** Compact USD notional: 3131493738.9 -> "3.13B", 950 -> "950". */
export function formatCompactUsd(value: number): string {
  if (!Number.isFinite(value)) return '—'
  const abs = Math.abs(value)
  for (const [size, unit] of COMPACT_UNITS) {
    // Round first so 999_999 reads "1.00M", not "1000.00K".
    if (abs >= size * 0.9995) return `${(value / size).toFixed(2)}${unit}`
  }
  return value.toFixed(0)
}

export const fundingSeriesOptions: HistogramSeriesPartialOptions = {
  base: 0,
  lastValueVisible: true,
  priceLineVisible: false,
  title: '',
  priceFormat: { type: 'custom', formatter: formatPercentAxis, minMove: 0.0001 },
}

export const openInterestSeriesOptions: LineSeriesPartialOptions = {
  lineWidth: 2,
  lastValueVisible: true,
  priceLineVisible: false,
  crosshairMarkerVisible: true,
  title: '',
  priceFormat: { type: 'custom', formatter: formatCompactUsd, minMove: 1 },
}

export function openInterestColorOptions(colors: DerivativesColors): LineSeriesPartialOptions {
  return { color: colors.line, crosshairMarkerBackgroundColor: colors.line }
}

export function zeroLineOptions(colors: DerivativesColors): Partial<PriceLineOptions> {
  return {
    price: 0,
    color: colors.zero,
    lineWidth: 1,
    lineStyle: LineStyle.Dotted,
    axisLabelVisible: false,
    title: '',
  }
}

type Whitespace = WhitespaceData<UTCTimestamp>

/**
 * Funding histogram points in percent (value × 100), green at or above zero and red below;
 * whitespace for null values. With `plotted`, points on bars the chart does not plot are dropped.
 */
export function fundingHistogramData(
  points: readonly DerivativesPoint[],
  colors: DerivativesColors,
  plotted?: ReadonlySet<number>,
): (HistogramData<UTCTimestamp> | Whitespace)[] {
  const out: (HistogramData<UTCTimestamp> | Whitespace)[] = []
  for (const p of points) {
    if (plotted !== undefined && !plotted.has(p.time)) continue
    const time = p.time as UTCTimestamp
    if (p.value === null) {
      out.push({ time })
      continue
    }
    out.push({
      time,
      value: p.value * 100,
      color: p.value >= 0 ? colors.positive : colors.negative,
    })
  }
  return out
}

/** Open-interest line points (USD); whitespace for null values, plotted bars only. */
export function openInterestLineData(
  points: readonly DerivativesPoint[],
  plotted?: ReadonlySet<number>,
): (LineData<UTCTimestamp> | Whitespace)[] {
  const out: (LineData<UTCTimestamp> | Whitespace)[] = []
  for (const p of points) {
    if (plotted !== undefined && !plotted.has(p.time)) continue
    const time = p.time as UTCTimestamp
    out.push(p.value === null ? { time } : { time, value: p.value })
  }
  return out
}

/**
 * Index of the last point at or before `time` (binary search; points ascending), or -1.
 */
function lastIndexAtOrBefore(points: readonly DerivativesPoint[], time: number): number {
  let lo = 0
  let hi = points.length - 1
  let found = -1
  while (lo <= hi) {
    const mid = (lo + hi) >>> 1
    if (points[mid].time <= time) {
      found = mid
      lo = mid + 1
    } else hi = mid - 1
  }
  return found
}

/**
 * The legend value: at the hovered bar `time` exactly, else (not hovering) at the newest point
 * at or before `lastPlotted` (the chart's last bar; in bar replay the cursor, so no value from
 * a later bar shows). Null when there is no such point or its value is null: never the nearest
 * other value.
 */
export function derivativesValueAt(
  points: readonly DerivativesPoint[],
  time: number | undefined,
  lastPlotted: number | undefined,
): number | null {
  if (time !== undefined) {
    const i = lastIndexAtOrBefore(points, time)
    return i >= 0 && points[i].time === time ? points[i].value : null
  }
  if (lastPlotted === undefined) return null
  const i = lastIndexAtOrBefore(points, lastPlotted)
  return i >= 0 ? points[i].value : null
}

/** True when at least one point has a value (else the pane shows a "no data" note). */
export function hasAnyValue(points: readonly DerivativesPoint[]): boolean {
  return points.some((p) => p.value !== null)
}

// ---------------------------------------------------------------------------------------------
// Pane manager
// ---------------------------------------------------------------------------------------------

export interface DerivativesPriceLine {
  applyOptions(options: Partial<PriceLineOptions>): void
}

/** The parts of an LWC series the panes use. */
export interface DerivativesSeries<D> {
  setData(data: D[]): void
  getPane(): {
    paneIndex(): number
    getStretchFactor(): number
    setStretchFactor(factor: number): void
  }
}

export interface DerivativesHistogram extends DerivativesSeries<
  HistogramData<UTCTimestamp> | Whitespace
> {
  createPriceLine(options: Partial<PriceLineOptions> & { price: number }): DerivativesPriceLine
}

export interface DerivativesLine extends DerivativesSeries<LineData<UTCTimestamp> | Whitespace> {
  applyOptions(options: LineSeriesPartialOptions): void
}

/** The parts of the chart the panes use (an adapter over IChartApi; a fake in tests). */
export interface DerivativesChart {
  addHistogram(options: HistogramSeriesPartialOptions, paneIndex: number): DerivativesHistogram
  addLine(options: LineSeriesPartialOptions, paneIndex: number): DerivativesLine
  removeSeries(series: DerivativesHistogram | DerivativesLine): void
  paneCount(): number
}

export function derivativesChartAdapter(chart: IChartApi): DerivativesChart {
  return {
    addHistogram: (options, paneIndex) => chart.addSeries(HistogramSeries, options, paneIndex),
    addLine: (options, paneIndex) => chart.addSeries(LineSeries, options, paneIndex),
    removeSeries: (series) =>
      chart.removeSeries(series as Parameters<IChartApi['removeSeries']>[0]),
    paneCount: () => chart.panes().length,
  }
}

interface Built {
  funding: DerivativesHistogram
  zero: DerivativesPriceLine
  openInterest: DerivativesLine
}

/**
 * Owns the two derivatives panes of one chart. The panes exist while data is set (non-null),
 * the chart is attached and the panes are not suspended. Each pane holds one series, so
 * removing the series drops the pane (LWC removes empty panes) and later panes move up.
 * Pane heights the user dragged are remembered across suspend/resume and redraws.
 */
export class DerivativesPanes {
  private chart: DerivativesChart | null = null
  private built: Built | null = null
  private data: DerivativesOverlay | null = null
  private plotted: ReadonlySet<number> | undefined
  private colors: DerivativesColors
  private suspended = false
  private stretch: { funding: number; openInterest: number } | null = null
  private version = 0
  private readonly listeners = new Set<() => void>()

  constructor(colors: DerivativesColors) {
    this.colors = colors
  }

  // ---- store (pane legends re-measure when panes come and go) ----

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

  attachChart(chart: DerivativesChart): void {
    this.detachChart(true)
    this.chart = chart
    this.apply()
  }

  /** `alive` false = the chart is already removed (with the series). */
  detachChart(alive: boolean): void {
    if (alive) this.removeSeries()
    this.built = null
    this.chart = null
    this.emit()
  }

  /** Null removes both panes. `plotted` = the chart's bar times (points elsewhere are dropped). */
  setData(data: DerivativesOverlay | null, plotted?: ReadonlySet<number>): void {
    this.data = data
    this.plotted = plotted
    this.apply()
  }

  setColors(colors: DerivativesColors): void {
    const c = this.colors
    if (
      c.positive === colors.positive &&
      c.negative === colors.negative &&
      c.line === colors.line &&
      c.zero === colors.zero
    ) {
      return
    }
    this.colors = colors
    const built = this.built
    if (built === null || this.data === null) return
    built.funding.setData(fundingHistogramData(this.data.funding, colors, this.plotted))
    built.zero.applyOptions({ color: colors.zero })
    built.openInterest.applyOptions(openInterestColorOptions(colors))
  }

  /** Before indicator panes change: take the panes out (their heights are remembered). */
  suspend(): void {
    if (this.suspended) return
    this.suspended = true
    this.removeSeries()
  }

  /** After indicator panes changed: put the panes back as the last panes. */
  resume(): void {
    if (!this.suspended) return
    this.suspended = false
    this.apply()
  }

  // ---- queries ----

  /** True while the panes are drawn. */
  shown(): boolean {
    return this.built !== null
  }

  /** Whether `setData` with non-null data would create the panes now (not yet drawn). */
  wouldCreate(): boolean {
    return this.built === null && this.chart !== null && !this.suspended
  }

  /** Pane indices of the two panes, or null while they are not drawn. */
  paneIndices(): { funding: number; openInterest: number } | null {
    const built = this.built
    if (built === null) return null
    return {
      funding: built.funding.getPane().paneIndex(),
      openInterest: built.openInterest.getPane().paneIndex(),
    }
  }

  overlay(): DerivativesOverlay | null {
    return this.data
  }

  // ---- internals ----

  private apply(): void {
    const chart = this.chart
    const data = this.data
    if (chart === null || this.suspended) return
    if (data === null) {
      this.removeSeries()
      return
    }
    let built = this.built
    if (built === null) {
      // One series per pane, appended after every existing pane.
      const funding = chart.addHistogram(fundingSeriesOptions, chart.paneCount())
      const zero = funding.createPriceLine({ ...zeroLineOptions(this.colors), price: 0 })
      const openInterest = chart.addLine(
        { ...openInterestSeriesOptions, ...openInterestColorOptions(this.colors) },
        chart.paneCount(),
      )
      built = { funding, zero, openInterest }
      this.built = built
      funding.getPane().setStretchFactor(this.stretch?.funding ?? DERIVATIVES_PANE_STRETCH)
      openInterest
        .getPane()
        .setStretchFactor(this.stretch?.openInterest ?? DERIVATIVES_PANE_STRETCH)
      this.emit()
    }
    built.funding.setData(fundingHistogramData(data.funding, this.colors, this.plotted))
    built.openInterest.setData(openInterestLineData(data.openInterest, this.plotted))
  }

  private removeSeries(): void {
    const built = this.built
    if (built === null) return
    this.stretch = {
      funding: built.funding.getPane().getStretchFactor(),
      openInterest: built.openInterest.getPane().getStretchFactor(),
    }
    // Later pane first, so the earlier pane's index is still valid when it goes.
    this.chart?.removeSeries(built.openInterest)
    this.chart?.removeSeries(built.funding)
    this.built = null
    this.emit()
  }
}
