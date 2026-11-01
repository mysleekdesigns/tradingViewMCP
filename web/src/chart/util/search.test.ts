import { describe, expect, it } from 'vitest'
import {
  indexByTime,
  lastIndexAtOrBeforeTime,
  lowerBound,
  lowerBoundByTime,
  upperBound,
  upperBoundByTime,
} from './search'

function naiveLower(xs: number[], x: number): number {
  const i = xs.findIndex((v) => !(v < x))
  return i === -1 ? xs.length : i
}
function naiveUpper(xs: number[], x: number): number {
  const i = xs.findIndex((v) => !(v <= x))
  return i === -1 ? xs.length : i
}

describe('binary search helpers', () => {
  it('match a linear scan on random sorted arrays with duplicates', () => {
    let seed = 7
    const rand = () => (seed = (seed * 1103515245 + 12345) % 2 ** 31) / 2 ** 31
    for (let trial = 0; trial < 300; trial++) {
      const xs = Array.from({ length: Math.floor(rand() * 40) }, () =>
        Math.floor(rand() * 20),
      ).sort((a, b) => a - b)
      for (let x = -2; x < 23; x++) {
        expect(lowerBound(xs.length, (i) => xs[i], x)).toBe(naiveLower(xs, x))
        expect(upperBound(xs.length, (i) => xs[i], x)).toBe(naiveUpper(xs, x))
        const items = xs.map((time) => ({ time }))
        expect(lowerBoundByTime(items, x)).toBe(naiveLower(xs, x))
        expect(upperBoundByTime(items, x)).toBe(naiveUpper(xs, x))
      }
    }
  })

  it('empty input returns 0', () => {
    expect(lowerBound(0, () => 0, 5)).toBe(0)
    expect(upperBoundByTime([], 5)).toBe(0)
  })
})

// The hand-rolled searches the shared helpers replaced (verbatim logic), kept here as references:
// every caller's rewrite must return what its old copy returned on ascending input.
function legacyIndexOfTime(items: readonly { time: number }[], time: number): number {
  let lo = 0
  let hi = items.length - 1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    const t = items[mid].time
    if (t === time) return mid
    if (t < time) lo = mid + 1
    else hi = mid - 1
  }
  return -1
}
function legacyLastIndexAtOrBefore(items: readonly { time: number }[], time: number): number {
  let lo = 0
  let hi = items.length - 1
  let found = -1
  while (lo <= hi) {
    const mid = (lo + hi) >>> 1
    if (items[mid].time <= time) {
      found = mid
      lo = mid + 1
    } else hi = mid - 1
  }
  return found
}
/** backtestOverlay `equityValueAt`'s search (caller guarantees n > 0 and time >= items[0]). */
function legacyEquityIndex(items: readonly { time: number }[], time: number): number {
  let lo = 0
  let hi = items.length - 1
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1
    if (items[mid].time <= time) lo = mid
    else hi = mid - 1
  }
  return lo
}
/** backtestOverlay `snapMarkersToBars`'s search over [0, n - 1]. */
function legacySnapIndex(times: readonly number[], x: number): number {
  let lo = 0
  let hi = times.length - 1
  while (lo < hi) {
    const mid = (lo + hi) >>> 1
    if (times[mid] < x) lo = mid + 1
    else hi = mid
  }
  return lo
}

describe('indexByTime', () => {
  it('finds exact times, -1 otherwise, and never finds NaN', () => {
    const items = [10, 20, 30].map((time) => ({ time }))
    expect(indexByTime(items, 10)).toBe(0)
    expect(indexByTime(items, 30)).toBe(2)
    expect(indexByTime(items, 25)).toBe(-1)
    expect(indexByTime(items, 5)).toBe(-1)
    expect(indexByTime(items, 35)).toBe(-1)
    expect(indexByTime(items, Number.NaN)).toBe(-1)
    expect(indexByTime([], 10)).toBe(-1)
  })

  it('returns the first index among duplicates', () => {
    const items = [1, 2, 2, 2, 3].map((time) => ({ time }))
    expect(indexByTime(items, 2)).toBe(1)
  })
})

describe('lastIndexAtOrBeforeTime', () => {
  it('returns the last item at or before the time, -1 before the first or on NaN', () => {
    const items = [10, 20, 20, 30].map((time) => ({ time }))
    expect(lastIndexAtOrBeforeTime(items, 5)).toBe(-1)
    expect(lastIndexAtOrBeforeTime(items, 10)).toBe(0)
    expect(lastIndexAtOrBeforeTime(items, 15)).toBe(0)
    expect(lastIndexAtOrBeforeTime(items, 20)).toBe(2)
    expect(lastIndexAtOrBeforeTime(items, 99)).toBe(3)
    expect(lastIndexAtOrBeforeTime(items, Number.NaN)).toBe(-1)
    expect(lastIndexAtOrBeforeTime([], 10)).toBe(-1)
  })
})

describe('shared helpers reproduce the copies they replaced', () => {
  it('on random ascending unique times, every probe (and NaN) gives the old answer', () => {
    let seed = 11
    const rand = () => (seed = (seed * 1103515245 + 12345) % 2 ** 31) / 2 ** 31
    for (let trial = 0; trial < 300; trial++) {
      const times: number[] = []
      let t = Math.floor(rand() * 5)
      for (let k = Math.floor(rand() * 30); k > 0; k--)
        times.push((t += 1 + Math.floor(rand() * 3)))
      const items = times.map((time) => ({ time }))
      const probes = [-1, Number.NaN, ...Array.from({ length: (t || 0) + 4 }, (_, k) => k - 1)]
      for (const x of probes) {
        // candleStore indexOfTime / liveCandles hasTime -> indexByTime
        expect(indexByTime(items, x)).toBe(legacyIndexOfTime(items, x))
        // derivativesPanes lastIndexAtOrBefore, crosshairSync barIndexAt -> lastIndexAtOrBeforeTime
        expect(upperBoundByTime(items, x) - 1).toBe(legacyLastIndexAtOrBefore(items, x))
        expect(lastIndexAtOrBeforeTime(items, x)).toBe(legacyLastIndexAtOrBefore(items, x))
        const n = times.length
        if (n === 0) continue
        // backtestOverlay equityValueAt (guard: time < first returns null before the search)
        if (!(x < times[0]))
          expect(Math.max(0, upperBoundByTime(items, x) - 1)).toBe(legacyEquityIndex(items, x))
        // backtestOverlay snapMarkersToBars (guard drops markers outside [first, last])
        if (!(x < times[0] || x > times[n - 1]))
          expect(lowerBound(n - 1, (i) => times[i], x)).toBe(legacySnapIndex(times, x))
      }
    }
  })
})
