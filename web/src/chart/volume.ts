import type { CandlestickData, HistogramData, UTCTimestamp } from 'lightweight-charts'
import type { Candle } from '../types'

export interface VolumeColors {
  up: string
  down: string
}

/**
 * Map candles to histogram points. A bar is "up" when close >= open (a doji counts as up,
 * matching the candlestick series colouring).
 */
export function toVolumeData(candles: readonly Candle[], palette: VolumeColors): HistogramData[] {
  return candles.map((c) => ({
    time: c.time as UTCTimestamp,
    value: c.volume,
    color: c.close >= c.open ? palette.up : palette.down,
  }))
}

/** Map candles (UNIX seconds) to Lightweight Charts candlestick points. */
export function toCandlestickData(candles: readonly Candle[]): CandlestickData[] {
  return candles.map((c) => ({
    time: c.time as UTCTimestamp,
    open: c.open,
    high: c.high,
    low: c.low,
    close: c.close,
  }))
}
