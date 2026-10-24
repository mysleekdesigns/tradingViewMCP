import type { CandlestickData, HistogramData, UTCTimestamp } from 'lightweight-charts'
import type { Candle } from '../types'
import type { VolumeColors } from './volume'

/** Live ticks append at most one bar; a short gap fill may add a few. More than this → setData. */
export const MAX_APPENDED_BARS = 5

export type SeriesSyncPlan = { kind: 'none' } | { kind: 'set' } | { kind: 'update'; from: number }

/**
 * Decide how to push `next` into a series that currently shows `prev`.
 *
 * - 'none': nothing changed.
 * - 'update': only the tail changed. Every bar before prev's last is the same object, prev's
 *   last bar keeps its time (possibly with new values), and at most MAX_APPENDED_BARS newer
 *   bars follow in ascending order. Call `series.update()` for next[from..].
 * - 'set': anything else (first load, history replaced or prepended, bars removed).
 */
export function planSeriesSync(
  prev: readonly Candle[] | null,
  next: readonly Candle[],
): SeriesSyncPlan {
  if (prev === next) return { kind: 'none' }
  if (prev === null || prev.length === 0) return { kind: 'set' }
  if (next.length < prev.length) return { kind: 'set' }
  if (next.length - prev.length > MAX_APPENDED_BARS) return { kind: 'set' }

  const lastIdx = prev.length - 1
  for (let i = 0; i < lastIdx; i++) {
    if (next[i] !== prev[i]) return { kind: 'set' }
  }
  // series.update() with a different time would append rather than replace the last bar.
  if (next[lastIdx].time !== prev[lastIdx].time) return { kind: 'set' }
  for (let i = prev.length; i < next.length; i++) {
    if (!(next[i].time > next[i - 1].time)) return { kind: 'set' }
  }

  const from = next[lastIdx] === prev[lastIdx] ? prev.length : lastIdx
  if (from >= next.length) return { kind: 'none' }
  return { kind: 'update', from }
}

/** One candle as a Lightweight Charts candlestick point. */
export function toCandlestickPoint(c: Candle): CandlestickData {
  return { time: c.time as UTCTimestamp, open: c.open, high: c.high, low: c.low, close: c.close }
}

/** One candle as a volume histogram point; doji counts as up, like `toVolumeData`. */
export function toVolumePoint(c: Candle, palette: VolumeColors): HistogramData {
  return {
    time: c.time as UTCTimestamp,
    value: c.volume,
    color: c.close >= c.open ? palette.up : palette.down,
  }
}
