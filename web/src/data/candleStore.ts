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

/**
 * Union by time, `fresh` wins on equal times, ascending, unique. Never mutates
 * inputs. Elements of `existing` not overridden keep identity.
 */
export function mergeCandles(existing: readonly Candle[], fresh: readonly Candle[]): Candle[] {
  const byTime = new Map<number, Candle>()
  for (const c of existing) byTime.set(c.time, c)
  for (const c of fresh) byTime.set(c.time, c)
  return [...byTime.values()].sort((a, b) => a.time - b.time)
}

/**
 * Prepend an older page to an ascending, unique-time array. Only rows strictly older than
 * `existing[0]` are used (ascending, unique, later duplicate wins). Never mutates inputs;
 * every element of `existing` keeps identity. Returns `existing` itself (same reference)
 * when no row is older. With empty `existing`, returns the sorted, unique `older`.
 */
export function prependOlder(existing: readonly Candle[], older: readonly Candle[]): Candle[] {
  const first = existing.length > 0 ? existing[0].time : Infinity
  const byTime = new Map<number, Candle>()
  for (const c of older) if (c.time < first) byTime.set(c.time, c)
  if (byTime.size === 0) return existing as Candle[]
  const head = [...byTime.values()].sort((a, b) => a.time - b.time)
  return [...head, ...existing]
}
