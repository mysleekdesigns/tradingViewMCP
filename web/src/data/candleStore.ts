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
  if (c.time > last.time) return [...candles, c]
  const idx = c.time === last.time ? candles.length - 1 : indexOfTime(candles, c.time)
  if (idx === -1 || sameFields(candles[idx], c)) return candles as Candle[]
  const out = candles.slice()
  out[idx] = c
  return out
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
