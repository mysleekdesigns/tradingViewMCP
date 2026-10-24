import type { CandlestickData, LineData, UTCTimestamp } from 'lightweight-charts'
import type { Candle, ChartType } from '../types'
import { heikinAshi, heikinAshiFrom, type HeikinAshiBar } from './heikinAshi'
import { colors } from './theme'
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

/** Price line at the last real close, coloured by the last real bar's direction (doji = up). */
export function realPriceLine(candles: readonly Candle[]): RealPriceLine {
  const last = candles[candles.length - 1]
  if (!last) return { price: 0, color: colors.up, visible: false }
  return {
    price: last.close,
    color: last.close >= last.open ? colors.up : colors.down,
    visible: true,
  }
}
