// Pure OHLCV aggregation of base bars into a coarser timeframe. A bucket only ever reads base
// bars that open inside it, so bars after a bucket's end can never change it (no look-ahead).
import type { Candle, Timeframe } from '../types'
import { bucketStart } from './timeframes'

/**
 * One bucket from its base bars (any order; at most one bar per time, later duplicates win):
 * first open, max high, min low, last close, summed volume, `time = start`. Null when `bars`
 * is empty.
 */
export function aggregateBucket(start: number, bars: Iterable<Candle>): Candle | null {
  const byTime = new Map<number, Candle>()
  for (const c of bars) byTime.set(c.time, c)
  if (byTime.size === 0) return null
  const sorted = [...byTime.values()].sort((a, b) => a.time - b.time)
  let high = -Infinity
  let low = Infinity
  let volume = 0
  for (const c of sorted) {
    if (c.high > high) high = c.high
    if (c.low < low) low = c.low
    volume += c.volume
  }
  return {
    time: start,
    open: sorted[0].open,
    high,
    low,
    close: sorted[sorted.length - 1].close,
    volume,
  }
}

/**
 * Aggregate base bars into `tf` buckets: ascending, unique times, one bucket per group of base
 * bars (a bucket with no base bars is skipped, never forward-filled). Input in any order;
 * duplicate base times keep the later row. A bucket whose base bars are incomplete (e.g. the
 * first one of a page that starts mid-bucket, or the forming last one) is aggregated from what
 * is there; trimming partial edges is the caller's job. Never mutates input.
 */
export function aggregateCandles(base: readonly Candle[], tf: Timeframe): Candle[] {
  const groups = new Map<number, Candle[]>()
  for (const c of base) {
    const start = bucketStart(c.time, tf)
    const group = groups.get(start)
    if (group === undefined) groups.set(start, [c])
    else group.push(c)
  }
  const out: Candle[] = []
  for (const [start, bars] of groups) {
    const bucket = aggregateBucket(start, bars)
    if (bucket !== null) out.push(bucket)
  }
  return out.sort((a, b) => a.time - b.time)
}
