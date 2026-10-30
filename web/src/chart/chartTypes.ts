import type {
  CandlestickData,
  CandlestickSeriesPartialOptions,
  CreatePriceLineOptions,
  LineData,
  UTCTimestamp,
} from 'lightweight-charts'
import type { Candle, ChartType } from '../types'
import { heikinAshi, heikinAshiFrom, type HeikinAshiBar } from './heikinAshi'
import { colors, mainSeriesOptionsFor, realPriceLineOptions, type ChartPalette } from './theme'
import { toCandlestickPoint } from './seriesSync'

/** Which Lightweight Charts series definition draws each chart type. */
export type MainSeriesKind = 'candlestick' | 'line' | 'area'

export function seriesKindFor(type: ChartType): MainSeriesKind {
  switch (type) {
    case 'candles':
    case 'hollow':
    case 'heikin-ashi':
      return 'candlestick'
    case 'line':
      return 'line'
    case 'area':
      return 'area'
  }
}

/**
 * True when both types draw with the same series definition (candles, hollow and Heikin-Ashi are
 * all `CandlestickSeries`), so a switch between them can keep the series and its primitives.
 */
export function sameSeriesKind(a: ChartType, b: ChartType): boolean {
  return seriesKindFor(a) === seriesKindFor(b)
}

/** A point for the main series: OHLC for candle types, close-only for line/area. */
export type MainPoint = CandlestickData | LineData

/** Line/area points plot the close. */
export function toLinePoint(c: Candle): LineData {
  return { time: c.time as UTCTimestamp, value: c.close }
}

function haPoint(b: HeikinAshiBar): CandlestickData {
  return { time: b.time as UTCTimestamp, open: b.open, high: b.high, low: b.low, close: b.close }
}

/** Full main-series data, plus the HA bars (Heikin-Ashi only) to reuse for tail updates. */
export interface MainSeriesData {
  points: MainPoint[]
  ha: HeikinAshiBar[] | null
}

export function mainSeriesData(type: ChartType, candles: readonly Candle[]): MainSeriesData {
  switch (type) {
    case 'candles':
    case 'hollow':
      return { points: candles.map(toCandlestickPoint), ha: null }
    case 'line':
    case 'area':
      return { points: candles.map(toLinePoint), ha: null }
    case 'heikin-ashi': {
      const ha = heikinAshi(candles)
      return { points: ha.map(haPoint), ha }
    }
  }
}

/**
 * Points for `candles[from..]` only, for `series.update()` on live ticks.
 * `prevHa` must be the HA bars for the previously applied candles (whose first `from` bars are
 * unchanged); only the tail is recomputed. A missing/short `prevHa` falls back to a full compute.
 */
export function mainSeriesTail(
  type: ChartType,
  candles: readonly Candle[],
  from: number,
  prevHa: readonly HeikinAshiBar[] | null,
): MainSeriesData {
  if (type === 'heikin-ashi') {
    const ha =
      prevHa && prevHa.length >= from ? heikinAshiFrom(prevHa, candles, from) : heikinAshi(candles)
    return { points: ha.slice(from).map(haPoint), ha }
  }
  const tail = candles.slice(from)
  const points =
    seriesKindFor(type) === 'candlestick' ? tail.map(toCandlestickPoint) : tail.map(toLinePoint)
  return { points, ha: null }
}

/**
 * Heikin-Ashi bars are synthetic, so the series' own last-value label would show the HA close.
 * In that mode the series label is hidden and Chart draws a price line at the real close.
 * Every other type plots real prices and keeps the series' native label.
 */
export function usesRealPriceLine(type: ChartType): boolean {
  return type === 'heikin-ashi'
}

export interface RealPriceLine {
  price: number
  color: string
  visible: boolean
}

/**
 * Price line at the last real close, coloured by the last real bar's direction (doji = up), in
 * the palette's candle colours (dark defaults when omitted).
 */
export function realPriceLine(
  candles: readonly Candle[],
  palette: Pick<ChartPalette, 'up' | 'down'> = colors,
): RealPriceLine {
  const last = candles[candles.length - 1]
  if (!last) return { price: 0, color: palette.up, visible: false }
  return {
    price: last.close,
    color: last.close >= last.open ? palette.up : palette.down,
    visible: true,
  }
}

/** The parts of a candlestick main series an in-place type switch uses (an LWC series fits). */
export interface CandleSeriesTarget<L> {
  applyOptions(options: CandlestickSeriesPartialOptions): void
  setData(points: CandlestickData[]): void
  createPriceLine(options: CreatePriceLineOptions): L
  removePriceLine(line: L): void
}

export interface InPlaceSwitch<L> {
  /** Heikin-Ashi bars for tail updates (null for the other types or without candles). */
  ha: HeikinAshiBar[] | null
  /** The real-close price line the series now has (Heikin-Ashi only), or null. */
  realLine: L | null
  /**
   * The real-close line was created by this switch. A fresh series creates it before the alert
   * lines, so the caller re-attaches the alert lines to keep the same price-line order.
   */
  createdRealLine: boolean
}

/**
 * Switch a candlestick-kind main series to another candlestick type (candles, hollow,
 * Heikin-Ashi) without removing it, so its primitives, markers and price lines stay attached.
 * Every candlestick type sets the same option keys (`mainSeriesOptionsFor`), so `applyOptions`
 * leaves exactly the options a fresh `addSeries` for `type` would have; the data is re-set from
 * `candles` (null = nothing applied yet), and the real-close line is created or removed in the
 * same order as on a fresh series (before `setData`). The caller syncs the line's price.
 */
export function switchCandleTypeInPlace<L>(
  series: CandleSeriesTarget<L>,
  type: ChartType,
  palette: ChartPalette,
  candles: readonly Candle[] | null,
  realLine: L | null,
): InPlaceSwitch<L> {
  series.applyOptions(mainSeriesOptionsFor(type, palette) as CandlestickSeriesPartialOptions)
  let line = realLine
  let createdRealLine = false
  if (usesRealPriceLine(type)) {
    if (line === null) {
      line = series.createPriceLine(realPriceLineOptions)
      createdRealLine = true
    }
  } else if (line !== null) {
    series.removePriceLine(line)
    line = null
  }
  if (candles === null) return { ha: null, realLine: line, createdRealLine }
  const { points, ha } = mainSeriesData(type, candles)
  series.setData(points as CandlestickData[])
  return { ha, realLine: line, createdRealLine }
}
