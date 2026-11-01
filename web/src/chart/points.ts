// Candle → Lightweight Charts point mappers, shared by the main series, volume, chart types,
// indicator lines and the backtest overlay (Phase 19 DRY: one set instead of a copy per module,
// and one place that turns UNIX seconds into LWC's `UTCTimestamp`).
import type { CandlestickData, HistogramData, LineData, UTCTimestamp } from 'lightweight-charts'
import type { Candle } from '../types'

/** Volume bar colours (one shared object per palette: see `volumeColorsOf` in volume.ts). */
export interface VolumeColors {
  up: string
  down: string
}

/** The OHLC fields a candlestick point reads (a `Candle` or a Heikin-Ashi bar). */
export interface OhlcBar {
  time: number
  open: number
  high: number
  low: number
  close: number
}

/** UNIX seconds as LWC's `UTCTimestamp` (a type-only brand: the number is unchanged). */
export function toTime(seconds: number): UTCTimestamp {
  return seconds as UTCTimestamp
}

/** One bar as a candlestick point: OHLC only, any other field of the bar is dropped. */
export function toCandlestickPoint(b: OhlcBar): CandlestickData<UTCTimestamp> {
  return { time: b.time as UTCTimestamp, open: b.open, high: b.high, low: b.low, close: b.close }
}

/** One candle as a line/area point on its close. */
export function toCloseLinePoint(c: Candle): LineData<UTCTimestamp> {
  return { time: c.time as UTCTimestamp, value: c.close }
}

/** A `{ time, value }` point (indicator, equity) as a line point. */
export function toLineData(p: { time: number; value: number }): LineData<UTCTimestamp> {
  return { time: p.time as UTCTimestamp, value: p.value }
}

/**
 * One candle as a volume histogram point. A bar is "up" when close >= open (a doji counts as up,
 * matching the candlestick series colouring).
 */
export function toVolumePoint(c: Candle, palette: VolumeColors): HistogramData<UTCTimestamp> {
  return {
    time: c.time as UTCTimestamp,
    value: c.volume,
    color: c.close >= c.open ? palette.up : palette.down,
  }
}

/** Candles (UNIX seconds) as candlestick points. */
export function toCandlestickData(candles: readonly Candle[]): CandlestickData<UTCTimestamp>[] {
  return candles.map(toCandlestickPoint)
}

/** Candles as volume histogram points (`toVolumePoint` per candle). */
export function toVolumeData(
  candles: readonly Candle[],
  palette: VolumeColors,
): HistogramData<UTCTimestamp>[] {
  return candles.map((c) => toVolumePoint(c, palette))
}
