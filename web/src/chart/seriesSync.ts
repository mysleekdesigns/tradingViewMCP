import type { CandlestickData, HistogramData, UTCTimestamp } from 'lightweight-charts'
import type { Candle } from '../types'
import type { VolumeColors } from './volume'

/** Live ticks append at most one bar; a short gap fill may add a few. More than this → setData. */
export const MAX_APPENDED_BARS = 5

export type SeriesSyncPlan =
  | { kind: 'none' }
  | { kind: 'set' }
  | { kind: 'update'; from: number }
  | { kind: 'prepend'; count: number }

/**
 * Decide how to push `next` into a series that currently shows `prev`.
 *
 * - 'none': nothing changed.
 * - 'update': only the tail changed. Every bar before prev's last is the same object, prev's
 *   last bar keeps its time (possibly with new values), and at most MAX_APPENDED_BARS newer
 *   bars follow in ascending order. Call `series.update()` for next[from..].
 * - 'prepend': `count` older bars (strictly ascending, all older than prev's first) were added
 *   in front; after them `prev` follows unchanged by identity, except that its last bar may have
 *   ticked and up to MAX_APPENDED_BARS newer bars may follow (same rules as 'update').
 *   Call `setData()` and shift the visible logical range right by `count` so the view holds still.
 * - 'set': anything else (first load, history replaced, bars removed).
 */
export function planSeriesSync(
  prev: readonly Candle[] | null,
  next: readonly Candle[],
): SeriesSyncPlan {
  if (prev === next) return { kind: 'none' }
  if (prev === null || prev.length === 0) return { kind: 'set' }
  if (next.length < prev.length) return { kind: 'set' }

  if (next[0].time === prev[0].time) {
    const from = tailMatch(prev, next, 0)
    if (from === null) return { kind: 'set' }
    if (from >= next.length) return { kind: 'none' }
    return { kind: 'update', from }
  }

  // Older bars prepended: locate prev's first bar in next.
  const count = next.findIndex((c) => c.time === prev[0].time)
  if (count <= 0) return { kind: 'set' }
  for (let i = 1; i <= count; i++) {
    if (!(next[i].time > next[i - 1].time)) return { kind: 'set' }
  }
  if (tailMatch(prev, next, count) === null) return { kind: 'set' }
  return { kind: 'prepend', count }
}

/**
 * Check that `next[offset..]` is `prev` with at most a ticked last bar and a few appended bars.
 * Returns the first index of `next` that needs `series.update()` (next.length if none), or null.
 */
function tailMatch(
  prev: readonly Candle[],
  next: readonly Candle[],
  offset: number,
): number | null {
  const appended = next.length - offset - prev.length
  if (appended < 0 || appended > MAX_APPENDED_BARS) return null
  const lastIdx = prev.length - 1
  for (let i = 0; i < lastIdx; i++) {
    if (next[offset + i] !== prev[i]) return null
  }
  // series.update() with a different time would append rather than replace the last bar.
  const last = offset + lastIdx
  if (next[last].time !== prev[lastIdx].time) return null
  for (let i = last + 1; i < next.length; i++) {
    if (!(next[i].time > next[i - 1].time)) return null
  }
  return next[last] === prev[lastIdx] ? last + 1 : last
}

/** Bars of slack left of the oldest loaded bar before Chart asks for older history. */
export const LEFT_EDGE_THRESHOLD_BARS = 50

/**
 * True when the visible logical range starts within `threshold` bars of the oldest loaded bar
 * (logical index 0). Negative `from` means empty space is showing left of the data.
 */
export function isNearLeftEdge(
  range: { from: number; to: number } | null,
  threshold: number = LEFT_EDGE_THRESHOLD_BARS,
): boolean {
  return range !== null && range.from < threshold
}

/** Visible logical range after `count` bars were inserted on the left: same bars stay in view. */
export function shiftLogicalRange(
  range: { from: number; to: number },
  count: number,
): { from: number; to: number } {
  return { from: range.from + count, to: range.to + count }
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
