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

/**
 * One bucket's running OHLCV, updated per base bar instead of re-aggregating every base bar of
 * the bucket per tick. `value(start)` always equals `aggregateBucket(start, bars)` over every
 * bar `set` so far (later sends of a time replace earlier ones), bit for bit: high, low and
 * volume are kept as the same left-to-right fold aggregateBucket runs in time order, split
 * into "every bar but the newest" plus the newest bar. A new newest bar or a re-send of the
 * newest one (the forming base bar) is O(1); a bar inserted or replaced before the newest
 * (out of order, a REST seed of earlier bars) marks the fold stale and the next `value` re-folds
 * once from the stored bars.
 */
export class RunningBucket {
  private readonly byTime = new Map<number, Candle>()
  /** Oldest and newest bar (null while empty). */
  private first: Candle | null = null
  private last: Candle | null = null
  /** aggregateBucket's high / low / volume fold over every bar but `last`, in time order. */
  private highBefore = -Infinity
  private lowBefore = Infinity
  private volumeBefore = 0
  /** A bar before `last` changed: first and the folds must be rebuilt from `byTime`. */
  private stale = false

  /** Bars held (one per time). */
  get size(): number {
    return this.byTime.size
  }

  /** Record base bar `c` (replacing an earlier send with the same time). */
  set(c: Candle): void {
    this.byTime.set(c.time, c)
    if (!this.stale) this.advance(c)
  }

  /** Fold `c` into the running state (or mark it stale when `c` is older than `last`). */
  private advance(c: Candle): void {
    const last = this.last
    if (last === null) {
      this.first = c
      this.last = c
    } else if (c.time === last.time) {
      // The forming base bar re-sent: the folds before it are unchanged.
      if (this.first === last) this.first = c
      this.last = c
    } else if (c.time > last.time) {
      if (last.high > this.highBefore) this.highBefore = last.high
      if (last.low < this.lowBefore) this.lowBefore = last.low
      this.volumeBefore += last.volume
      this.last = c
    } else {
      this.stale = true
    }
  }

  /** The bucket's candle (`time = start`), or null when no bar has been set. */
  value(start: number): Candle | null {
    if (this.stale) this.refold()
    const first = this.first
    const last = this.last
    if (first === null || last === null) return null
    return {
      time: start,
      open: first.open,
      high: last.high > this.highBefore ? last.high : this.highBefore,
      low: last.low < this.lowBefore ? last.low : this.lowBefore,
      close: last.close,
      volume: this.volumeBefore + last.volume,
    }
  }

  private refold(): void {
    this.stale = false
    const sorted = [...this.byTime.values()].sort((a, b) => a.time - b.time)
    this.first = null
    this.last = null
    this.highBefore = -Infinity
    this.lowBefore = Infinity
    this.volumeBefore = 0
    for (const c of sorted) this.advance(c)
  }
}
