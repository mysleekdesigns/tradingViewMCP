// The regime ribbon: a thin strip along the bottom of the main pane coloured by hedgeFund's
// regime of each bar (Bull / Sideways / Bear), labelled by the lab (`POST /lab/regime`).
//
// Filtered, never back-dated: only closed bars are sent (the forming last bar is dropped), and
// the lab labels bar t from closes at or before t, so a new bar never changes an earlier colour.
// Warm-up bars (no label yet) are whitespace. The strip is a constant-height histogram on its
// own overlay price scale pinned to the bottom few percent, so it never moves the price scale.

import {
  HistogramSeries,
  type AutoscaleInfo,
  type HistogramData,
  type HistogramSeriesPartialOptions,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
  type WhitespaceData,
} from 'lightweight-charts'
import type { RegimePoint, RegimeState } from '../data/labTypes'
import type { Candle } from '../types'
import { withHexAlpha, type ChartPalette } from './theme'

export interface RegimeColors {
  bull: string
  bear: string
  sideways: string
}

/** Bull/Bear follow the user's candle colours; Sideways is the theme's muted grey. */
export const REGIME_ALPHA = 0.55
export const REGIME_SIDEWAYS_ALPHA = 0.35

export function regimeColorsFor(
  palette: Pick<ChartPalette, 'up' | 'down' | 'mutedText'>,
): RegimeColors {
  return {
    bull: withHexAlpha(palette.up, REGIME_ALPHA),
    bear: withHexAlpha(palette.down, REGIME_ALPHA),
    sideways: withHexAlpha(palette.mutedText, REGIME_SIDEWAYS_ALPHA),
  }
}

export const REGIME_LABELS: Record<RegimeState, string> = {
  [-1]: 'Bear',
  [0]: 'Sideways',
  [1]: 'Bull',
}

/** The lab's request limit (`MAX_REGIME_BARS` in lab/lab/api.py). */
export const MAX_REGIME_BARS = 20_000

export const REGIME_RIBBON_PRICE_SCALE_ID = 'regime-ribbon'
/** The strip fills the bottom 3% of the main pane. */
export const regimeRibbonScaleMargins = { top: 0.97, bottom: 0 } as const

export const regimeRibbonOptions: HistogramSeriesPartialOptions = {
  priceScaleId: REGIME_RIBBON_PRICE_SCALE_ID,
  base: 0,
  lastValueVisible: false,
  priceLineVisible: false,
  title: '',
  // Each bar is 1 tall on a 0..1 scale of its own; the main price scale never sees it.
  autoscaleInfoProvider: (): AutoscaleInfo => ({ priceRange: { minValue: 0, maxValue: 1 } }),
}

function colorOf(state: RegimeState, colors: RegimeColors): string {
  return state === 1 ? colors.bull : state === -1 ? colors.bear : colors.sideways
}

/**
 * Histogram points for the ribbon: value 1 coloured by state, whitespace for warm-up bars.
 * With `plotted`, points on bars the chart does not plot are dropped (a point at a time the
 * main series lacks would add a bar to the time scale).
 */
export function regimeRibbonData(
  points: readonly RegimePoint[],
  colors: RegimeColors,
  plotted?: ReadonlySet<number>,
): (HistogramData<UTCTimestamp> | WhitespaceData<UTCTimestamp>)[] {
  const out: (HistogramData<UTCTimestamp> | WhitespaceData<UTCTimestamp>)[] = []
  for (const p of points) {
    if (plotted !== undefined && !plotted.has(p.time)) continue
    const time = p.time as UTCTimestamp
    out.push(p.state === null ? { time } : { time, value: 1, color: colorOf(p.state, colors) })
  }
  return out
}

/**
 * The bars the ribbon asks the lab about: every loaded bar, no-trade bars included, so the
 * trailing window counts the same rows as the lab's `regime_state` feature (which keeps
 * zero-volume rows). Live (`replayVisible` null) the last bar is still forming; in replay only
 * the bars up to the replay's last visible bar are sent, all closed. Points are drawn on plotted
 * times only (`regimeRibbonData`'s `plotted`).
 */
export function regimeSourceBars(
  raw: readonly Candle[],
  replayVisible: readonly Candle[] | null,
): { candles: readonly Candle[]; formingLast: boolean } {
  if (replayVisible === null) return { candles: raw, formingLast: true }
  if (replayVisible.length === 0) return { candles: [], formingLast: false }
  const cursor = replayVisible[replayVisible.length - 1].time
  // First raw bar after the cursor (raw is ascending).
  let lo = 0
  let hi = raw.length
  while (lo < hi) {
    const mid = (lo + hi) >>> 1
    if (raw[mid].time <= cursor) lo = mid + 1
    else hi = mid
  }
  return { candles: lo === raw.length ? raw : raw.slice(0, lo), formingLast: false }
}

/** The closed bars of `candles`: all of them, or all but the last while it is still forming. */
export function closedBarsForRegime(
  candles: readonly Candle[],
  formingLast: boolean,
): readonly Candle[] {
  return formingLast ? candles.slice(0, -1) : candles
}

/** Number of closed bars without copying the array (see `closedBarsForRegime`). */
function closedCount(candles: readonly Candle[], formingLast: boolean): number {
  return formingLast ? Math.max(0, candles.length - 1) : candles.length
}

export interface RegimeQueryInput {
  symbol: string
  timeframe: string
  candles: readonly Candle[]
  /** The last bar is still forming (live data). Default true. */
  formingLast?: boolean
  windowBars?: number
  threshold?: number
}

/**
 * A key that changes only when the closed bars change: a new close, a history prepend, or a
 * corrected last closed bar. Live ticks on the forming bar keep it. Null when nothing is closed.
 * O(1): cheap enough to compute on every render.
 */
export function regimeQueryKey(q: RegimeQueryInput): string | null {
  const n = closedCount(q.candles, q.formingLast ?? true)
  if (n === 0) return null
  const first = q.candles[Math.max(0, n - MAX_REGIME_BARS)]
  const last = q.candles[n - 1]
  return [
    q.symbol,
    q.timeframe,
    q.windowBars ?? '',
    q.threshold ?? '',
    n,
    first.time,
    last.time,
    last.close,
  ].join('|')
}

/** The newest `MAX_REGIME_BARS` closed bars as request arrays (times in UNIX seconds). */
export function regimeRequestBars(
  candles: readonly Candle[],
  formingLast = true,
): { times: number[]; closes: number[] } {
  const closed = closedBarsForRegime(candles, formingLast).slice(-MAX_REGIME_BARS)
  return { times: closed.map((c) => c.time), closes: closed.map((c) => c.close) }
}

/** The ribbon series on pane 0. `remove` is idempotent; nothing else works after it. */
export class RegimeRibbon {
  private chart: IChartApi | null
  private series: ISeriesApi<'Histogram'> | null
  private colors: RegimeColors
  private points: readonly RegimePoint[] = []
  private plotted: ReadonlySet<number> | undefined

  constructor(chart: IChartApi, colors: RegimeColors) {
    this.chart = chart
    this.colors = colors
    this.series = chart.addSeries(HistogramSeries, regimeRibbonOptions, 0)
    this.series.priceScale().applyOptions({ scaleMargins: regimeRibbonScaleMargins })
  }

  /** Null clears the strip (e.g. the lab is offline or the overlay is loading). */
  setPoints(points: readonly RegimePoint[] | null, plotted?: ReadonlySet<number>): void {
    this.points = points ?? []
    this.plotted = plotted
    this.render()
  }

  setColors(colors: RegimeColors): void {
    if (
      colors.bull === this.colors.bull &&
      colors.bear === this.colors.bear &&
      colors.sideways === this.colors.sideways
    ) {
      return
    }
    this.colors = colors
    this.render()
  }

  remove(): void {
    if (this.chart !== null && this.series !== null) this.chart.removeSeries(this.series)
    this.chart = null
    this.series = null
    this.points = []
  }

  private render(): void {
    this.series?.setData(regimeRibbonData(this.points, this.colors, this.plotted))
  }
}
