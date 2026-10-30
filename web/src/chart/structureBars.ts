// Which plotted bars the market-structure overlay (Phase 16) may use: only bars whose final
// values are confirmed. Pure, so the wiring in ChartPanel is tested.

import { knownSamePrefix as storeSamePrefix, samePrefixLength } from '../data/candleStore'
import type { LiveCandlesSnapshot } from '../state/liveCandles'
import type { Candle } from '../types'
import type { SeriesSyncPlan } from './seriesSync'

/**
 * The `closedThrough` time the chart's market-structure layer gets (it uses plotted bars with
 * time <= it, never the forming bar or a bar whose final values are not confirmed yet):
 * - nothing loaded (live load not ready) → null: no structure;
 * - live → the controller's `closedThrough` (see `LiveCandlesSnapshot.closedThrough`);
 * - bar replay (`replayBars` set) → the last replay bar (replay shows closed bars only), but
 *   never past the live `closedThrough` of the same stream, so a partial bar that replay can
 *   still see while a gap fill is pending is not used either. No replay bars → null.
 */
export function structureClosedThrough(
  live: Pick<LiveCandlesSnapshot, 'load' | 'closedThrough'>,
  replayBars: readonly Candle[] | null,
): number | null {
  if (live.load.kind !== 'ready') return null
  const through = live.closedThrough
  if (replayBars === null) return through
  if (replayBars.length === 0 || through === null) return null
  return Math.min(replayBars[replayBars.length - 1].time, through)
}

/** How many leading bars of ascending `bars` open at or before `closedThrough` (0 for null). */
export function closedBarCount(bars: readonly Candle[], closedThrough: number | null): number {
  if (closedThrough === null) return 0
  let lo = 0
  let hi = bars.length
  while (lo < hi) {
    const mid = (lo + hi) >>> 1
    if (bars[mid].time <= closedThrough) lo = mid + 1
    else hi = mid
  }
  return lo
}

/**
 * What the chart's data effect already knows about each bars array it applies: how many leading
 * bars are the same objects as the array before it (a tail update keeps every bar before its
 * `from`). The overlay layers check their pushed bars by identity; with this they skip the O(n)
 * scan on a live tick. Only the latest step is kept; any other pair is scanned.
 */
export class BarLineage {
  private prev: readonly Candle[] | null = null
  private next: readonly Candle[] | null = null
  private same = 0

  /**
   * `next` replaced `prev`, and next[i] === prev[i] exactly for i < `same`. A null `same`
   * (not known) forgets the step, so the next check scans.
   */
  record(prev: readonly Candle[] | null, next: readonly Candle[], same: number | null): void {
    this.prev = same === null ? null : prev
    this.next = same === null ? null : next
    this.same = same ?? 0
  }

  /**
   * Length of the common prefix of `a` and `b` by element identity. The recorded step is
   * answered as recorded; any other pair scans, starting after the prefix the candle store's
   * lineage already vouches for (`data/candleStore` `samePrefixLength`), so a pair a few live
   * ticks apart costs O(changed tail) and an unrelated pair O(n).
   */
  samePrefixLength(a: readonly Candle[], b: readonly Candle[]): number {
    if (a === b) return a.length
    if (a === this.prev && b === this.next) return this.same
    return samePrefixLength(a, b)
  }

  /**
   * What is known without reading any bar: a lower bound on the identity prefix of `older`
   * and `newer` (the recorded step's own value, or the candle store's lineage when `newer`
   * was derived from `older`), or null when nothing is known. For callers whose own scan is
   * cheaper than an O(n) prefix scan (a fixed volume-profile range).
   */
  knownSamePrefix(older: readonly Candle[], newer: readonly Candle[]): number | null {
    if (older === newer) return older.length
    if (older === this.prev && newer === this.next) return this.same
    return storeSamePrefix(older, newer)
  }
}

/**
 * The identical-prefix length a series sync plan implies: a tail update from `from` keeps every
 * bar before it (and no more), 'none' keeps them all. Null for 'set' and 'prepend', which do not
 * say (a long gap fill is a 'set' that may keep every old bar), so those are scanned.
 */
export function planSamePrefix(plan: SeriesSyncPlan, next: readonly Candle[]): number | null {
  if (plan.kind === 'none') return next.length
  if (plan.kind === 'update') return plan.from
  return null
}
