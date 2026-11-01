// Which plotted bars the market-structure overlay (Phase 16) may use: only bars whose final
// values are confirmed. Pure, so the wiring in ChartPanel is tested.

import { knownSamePrefix as storeSamePrefix, samePrefixLength } from '../data/candleStore'
import type { LiveCandlesSnapshot } from '../state/liveCandles'
import type { Candle } from '../types'
import type { SeriesSyncPlan } from './seriesSync'
import { upperBoundByTime } from './util/search'

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
  return upperBoundByTime(bars, closedThrough)
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

  /**
   * The prefix checks of one sync against `bars`, of which only the first `usable` may be
   * read (the closed bars): see `PrefixChecks`. Make one per sync; it holds `bars`.
   */
  prefixChecks(bars: readonly Candle[], usable: number): PrefixChecks {
    return new PrefixChecks(this, bars, usable)
  }
}

/**
 * "Are the bars an engine already pushed still the first bars?" for every instance of one
 * layer sync. `keeps(synced, n)` is true when the engine pushed nothing (n 0), or when the
 * first `n` bars of `synced` (the array the engine was last synced with) are the same objects
 * as the first `n` of the sync's bars and all of them are still usable (a `closedThrough` that
 * moved back fails). The identity prefix is asked of the lineage (free for the chart's own last
 * step, else an O(n) pointer scan) once per distinct `synced` array, so instances synced
 * together share one scan.
 */
export class PrefixChecks {
  private readonly lineage: BarLineage
  private readonly bars: readonly Candle[]
  private readonly usable: number
  /** Identity prefix of each `synced` array asked about so far (made on first use). */
  private memo: Map<readonly Candle[], number> | null = null

  constructor(lineage: BarLineage, bars: readonly Candle[], usable: number) {
    this.lineage = lineage
    this.bars = bars
    this.usable = usable
  }

  keeps(synced: readonly Candle[] | null, n: number): boolean {
    if (n === 0) return true
    if (this.usable < n || synced === null) return false
    const memo = (this.memo ??= new Map())
    let same = memo.get(synced)
    if (same === undefined) {
      same = this.lineage.samePrefixLength(synced, this.bars)
      memo.set(synced, same)
    }
    return same >= n
  }
}

/** An incremental engine fed one closed bar at a time (market structure, session ranges). */
export interface BarEngine {
  readonly count: number
  push(bar: Candle): void
}

/**
 * Push `bars[engine.count .. usable - 1]` (the usable bars the engine has not seen) into
 * `engine`, in order; returns how many were pushed (0 when it is up to date).
 */
export function pushNewBars(engine: BarEngine, bars: readonly Candle[], usable: number): number {
  const n = engine.count
  if (usable <= n) return 0
  for (let i = n; i < usable; i++) engine.push(bars[i])
  return usable - n
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
