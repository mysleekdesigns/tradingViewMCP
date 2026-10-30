import type { Candle } from '../types'

/** Index of the element with `time`, or -1. `candles` must be ascending by time. */
function indexOfTime(candles: readonly Candle[], time: number): number {
  let lo = 0
  let hi = candles.length - 1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    const t = candles[mid].time
    if (t === time) return mid
    if (t < time) lo = mid + 1
    else hi = mid - 1
  }
  return -1
}

function sameFields(a: Candle, b: Candle): boolean {
  return (
    a.time === b.time &&
    a.open === b.open &&
    a.high === b.high &&
    a.low === b.low &&
    a.close === b.close &&
    a.volume === b.volume
  )
}

/**
 * Apply one live candle to an ascending, unique-time array. Never mutates input.
 *  - empty input -> [c]
 *  - c.time === last.time -> copy with last replaced
 *  - c.time > last.time -> copy with c appended
 *  - c.time < last.time -> copy with the same-time element replaced if present;
 *    otherwise return `candles` itself (same reference)
 *  - the bar with c.time already has identical fields -> `candles` itself (same reference),
 *    so repeated read-backs and matching gap rows cost no copy and trigger no re-render
 * All untouched elements keep object identity.
 */
export function applyLiveCandle(candles: readonly Candle[], c: Candle): Candle[] {
  if (candles.length === 0) return [c]
  const last = candles[candles.length - 1]
  if (c.time > last.time) {
    // The one O(n) step left per tick: a pointer copy (memcpy speed). Kept because every
    // consumer relies on "new array = something changed, same array = nothing changed".
    const appended = [...candles, c]
    recordLineage(appended, candles, candles.length)
    return appended
  }
  const idx = c.time === last.time ? candles.length - 1 : indexOfTime(candles, c.time)
  if (idx === -1 || sameFields(candles[idx], c)) return candles as Candle[]
  const out = candles.slice()
  out[idx] = c
  recordLineage(out, candles, idx)
  return out
}

/* ------------------------------------------------------------------------------------------ */
/* Lineage: which array a candle array was derived from, and how much of it it kept            */
/* ------------------------------------------------------------------------------------------ */

/**
 * What a producer (`applyLiveCandle`, `plottableCandles`) knows about an array it returned:
 * `next[i] === parent[i]` (same object) for every `i < samePrefix`. Consumers use it to skip
 * the O(n) identity scans on a live tick (`planSeriesSync`, `BarLineage`, the overlay layers)
 * and always fall back to the scan when nothing is recorded.
 *
 * Arrays are never mutated after they are returned (every producer copies). As a guard, an
 * entry is ignored when either array's length changed since it was recorded.
 *
 * Memory: an entry holds its parent strongly (a `WeakRef` would keep every array of a
 * synchronous burst alive until the task ends, and costs more GC work than it saves). To keep
 * the chain short, recording a step forgets the link MAX_LINEAGE_HOPS steps back, so at most
 * that many earlier arrays per stream are kept alive by this map.
 */
interface LineageEntry {
  parent: readonly Candle[]
  parentLength: number
  length: number
  samePrefix: number
}

const lineages = new WeakMap<readonly Candle[], LineageEntry>()

/**
 * Parents `knownSamePrefix` follows: 2 = one tick applied between two chart renders (a burst
 * of messages before React renders) still takes the fast path.
 */
export const MAX_LINEAGE_HOPS = 2

/**
 * Record that `next` was derived from `parent` and keeps its first `samePrefix` elements by
 * identity. Callers state an exact prefix where they can (the first index that differs), but
 * consumers only rely on it as a lower bound.
 */
export function recordLineage(
  next: readonly Candle[],
  parent: readonly Candle[],
  samePrefix: number,
): void {
  if (next === parent) return
  // Cut the chain behind the oldest ancestor `knownSamePrefix` can still reach from `next`.
  let oldest = parent
  for (let hop = 1; hop < MAX_LINEAGE_HOPS; hop++) {
    const entry = lineages.get(oldest)
    if (entry === undefined) break
    oldest = entry.parent
  }
  lineages.delete(oldest)
  lineages.set(next, {
    parent,
    parentLength: parent.length,
    length: next.length,
    samePrefix: Math.max(0, Math.min(samePrefix, next.length, parent.length)),
  })
}

/** The recorded parent of `next` and the identity prefix they share, or null if unknown. */
export function lineageOf(
  next: readonly Candle[],
): { parent: readonly Candle[]; samePrefix: number } | null {
  const entry = lineages.get(next)
  if (
    entry === undefined ||
    entry.length !== next.length ||
    entry.parent.length !== entry.parentLength
  )
    return null
  return entry
}

/**
 * A lower bound on the identity prefix of `older` and `newer` (`older[i] === newer[i]` for
 * every i below it) known from recorded lineage, following `newer`'s parents up to
 * MAX_LINEAGE_HOPS steps until `older` is reached; null when the pair is not related that way.
 * O(hops), no element is read.
 */
export function knownSamePrefix(older: readonly Candle[], newer: readonly Candle[]): number | null {
  if (older === newer) return older.length
  let cur = newer
  let bound = Number.POSITIVE_INFINITY
  for (let hop = 0; hop < MAX_LINEAGE_HOPS; hop++) {
    const step = lineageOf(cur)
    if (step === null) return null
    if (step.samePrefix < bound) bound = step.samePrefix
    if (step.parent === older) return bound
    cur = step.parent
  }
  return null
}

/**
 * Exact length of the common prefix of `a` and `b` by element identity. Starts at the bound
 * recorded lineage gives (either direction), so a live tick costs O(changed tail), and scans
 * from 0 for unrelated arrays.
 */
export function samePrefixLength(a: readonly Candle[], b: readonly Candle[]): number {
  const n = Math.min(a.length, b.length)
  let i = Math.min(n, knownSamePrefix(a, b) ?? knownSamePrefix(b, a) ?? 0)
  while (i < n && a[i] === b[i]) i++
  return i
}

/* ------------------------------------------------------------------------------------------ */
/* Sorted merges: binary-search splices of ascending, unique-time arrays                        */
/* ------------------------------------------------------------------------------------------ */

/** First index of ascending `candles` whose time is >= `time` (`candles.length` if none). */
export function lowerBoundTime(candles: readonly Candle[], time: number): number {
  let lo = 0
  let hi = candles.length
  while (lo < hi) {
    const mid = (lo + hi) >>> 1
    if (candles[mid].time < time) lo = mid + 1
    else hi = mid
  }
  return lo
}

/** First index of ascending `candles` whose time is > `time` (`candles.length` if none). */
export function upperBoundTime(candles: readonly Candle[], time: number): number {
  let lo = 0
  let hi = candles.length
  while (lo < hi) {
    const mid = (lo + hi) >>> 1
    if (candles[mid].time <= time) lo = mid + 1
    else hi = mid
  }
  return lo
}

/** True when every time is strictly greater than the one before it (ascending, unique). */
function strictlyAscending(candles: readonly Candle[]): boolean {
  for (let i = 1; i < candles.length; i++)
    if (!(candles[i].time > candles[i - 1].time)) return false
  return true
}

/**
 * `rows` itself when already ascending and unique (the normal case: exchange pages and the
 * controller's arrays); otherwise an ascending, unique copy in which the last row with each
 * time wins. O(n) check, the sort only for out-of-order input.
 */
function ascendingUnique(rows: readonly Candle[]): readonly Candle[] {
  if (strictlyAscending(rows)) return rows
  const byTime = new Map<number, Candle>()
  for (const c of rows) byTime.set(c.time, c)
  return [...byTime.values()].sort((a, b) => a.time - b.time)
}

/**
 * Merge ascending, unique `fresh` into ascending, unique `existing` (`fresh` wins on equal
 * times; with `keepUnchanged`, a fresh row whose fields equal the existing bar keeps the
 * existing object). The bars before and after `fresh`'s time span are copied as they are; only
 * the overlapping window (found by binary search) is merged element by element.
 * O(n) pointer copy + O(m + log n).
 */
function spliceMerge(
  existing: readonly Candle[],
  fresh: readonly Candle[],
  keepUnchanged: boolean,
): Candle[] {
  if (fresh.length === 0) return existing.slice()
  const lo = lowerBoundTime(existing, fresh[0].time)
  const hi = upperBoundTime(existing, fresh[fresh.length - 1].time)
  const out = existing.slice(0, lo)
  let i = lo
  let j = 0
  while (i < hi || j < fresh.length) {
    if (j >= fresh.length || (i < hi && existing[i].time < fresh[j].time)) {
      out.push(existing[i++])
    } else if (i >= hi || fresh[j].time < existing[i].time) {
      out.push(fresh[j++])
    } else {
      const e = existing[i++]
      const f = fresh[j++]
      out.push(keepUnchanged && sameFields(e, f) ? e : f)
    }
  }
  for (let k = hi; k < existing.length; k++) out.push(existing[k])
  return out
}

/**
 * Union by time, `fresh` wins on equal times, ascending, unique. Never mutates
 * inputs. Elements of `existing` not overridden keep identity. Always a new array.
 * Inputs may be unsorted or hold duplicate times (the later duplicate wins).
 */
export function mergeCandles(existing: readonly Candle[], fresh: readonly Candle[]): Candle[] {
  return spliceMerge(ascendingUnique(existing), ascendingUnique(fresh), false)
}

/**
 * `mergeCandles(existing, fresh)`, except that a fresh row whose values equal the existing bar
 * at its time keeps the existing object (so a gap fill that only touched the tail lets the chart
 * use `series.update()` instead of `setData()`). Always a new array. When `existing` is
 * ascending and unique, records the exact identity prefix the result shares with it (lineage),
 * so consumers skip their prefix scans.
 */
export function mergeCandlesKeepingUnchanged(
  existing: readonly Candle[],
  fresh: readonly Candle[],
): Candle[] {
  const base = ascendingUnique(existing)
  const rows = ascendingUnique(fresh)
  const out = spliceMerge(base, rows, true)
  if (base === existing && existing.length > 0) {
    // Exact prefix: the bars before the first fresh time are copied as they are; scan on from
    // there (the merged window, plus the rest when every fresh row kept its existing bar).
    const n = Math.min(out.length, existing.length)
    let same = rows.length === 0 ? n : Math.min(n, lowerBoundTime(existing, rows[0].time))
    while (same < n && out[same] === existing[same]) same++
    recordLineage(out, existing, same)
  }
  return out
}

/**
 * Prepend an older page to an ascending, unique-time array. Only rows strictly older than
 * `existing[0]` are used (ascending, unique, later duplicate wins). Never mutates inputs;
 * every element of `existing` keeps identity. Returns `existing` itself (same reference)
 * when no row is older. With empty `existing`, returns the sorted, unique `older`.
 * An ascending page (the normal case) is cut by binary search: no map, no sort.
 */
export function prependOlder(existing: readonly Candle[], older: readonly Candle[]): Candle[] {
  const first = existing.length > 0 ? existing[0].time : Infinity
  const page = ascendingUnique(older)
  const cut = lowerBoundTime(page, first)
  if (cut === 0) return existing as Candle[]
  return page.slice(0, cut).concat(existing)
}

/* ------------------------------------------------------------------------------------------ */
/* Bar times: one barsKey and one time Set per bar set, shared by the chart's overlays          */
/* ------------------------------------------------------------------------------------------ */

const barsKeys = new WeakMap<readonly Candle[], { length: number; key: string }>()

/**
 * `count:firstTime:lastTime` ('' when empty). It changes when the bar count or either end
 * changes (a bar close, a prepend, a new window) and stays the same on a live tick of the
 * forming bar, so overlays keyed on it stay idle while a bar forms. Cached per array.
 */
export function barsKeyOf(bars: readonly Candle[]): string {
  const cached = barsKeys.get(bars)
  if (cached !== undefined && cached.length === bars.length) return cached.key
  const key =
    bars.length === 0 ? '' : `${bars.length}:${bars[0].time}:${bars[bars.length - 1].time}`
  barsKeys.set(bars, { length: bars.length, key })
  return key
}

/**
 * The open times of one bar set, built on first use and then shared: the ascending `times`
 * array (backtest marker snapping) and the `set` (filtering overlay points to plotted bars).
 * Read-only.
 */
export class BarTimes {
  readonly key: string
  private readonly bars: readonly Candle[]
  private timesCache: readonly number[] | null = null
  private setCache: ReadonlySet<number> | null = null

  constructor(bars: readonly Candle[]) {
    this.bars = bars
    this.key = barsKeyOf(bars)
  }

  /** Open times, ascending (the bars' order). */
  get times(): readonly number[] {
    if (this.timesCache === null) this.timesCache = this.bars.map((c) => c.time)
    return this.timesCache
  }

  /** The same times as a Set. */
  get set(): ReadonlySet<number> {
    if (this.setCache === null) this.setCache = new Set(this.times)
    return this.setCache
  }
}

/**
 * One `BarTimes` per bar set (per `barsKeyOf`), so the chart, the backtest overlay, the regime
 * ribbon and the derivatives panes share one times array and one Set instead of each building
 * its own on every bar close.
 */
export class BarTimesMemo {
  private current: BarTimes | null = null

  /**
   * The shared times for `bars`: the same object while `barsKeyOf(bars)` is unchanged (a live
   * tick on the forming bar), a new one when it changes. `refresh` forces a new one for the
   * same key (a full data replacement, whose interior bars may differ).
   */
  get(bars: readonly Candle[], refresh = false): BarTimes {
    const cur = this.current
    if (!refresh && cur !== null && cur.key === barsKeyOf(bars)) return cur
    const next = new BarTimes(bars)
    this.current = next
    return next
  }
}
