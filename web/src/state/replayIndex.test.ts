// Phase 19 Wave 5: replay's lastIndexAtOrBefore now delegates to the shared
// chart/util/search lastIndexAtOrBeforeTime. Same answers as the hand-written loop it replaced
// on ascending bars, duplicates, empty input and a NaN cursor.
import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { lastIndexAtOrBefore } from './replay'

/** The pre-Wave-5 loop, verbatim. */
function legacy(bars: readonly Candle[], t: number): number {
  let lo = 0
  let hi = bars.length - 1
  let found = -1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (bars[mid].time <= t) {
      found = mid
      lo = mid + 1
    } else {
      hi = mid - 1
    }
  }
  return found
}

const bar = (time: number): Candle => ({ time, open: 1, high: 1, low: 1, close: 1, volume: 0 })

describe('lastIndexAtOrBefore', () => {
  it('matches the old loop on ascending bars, with and without duplicate times', () => {
    let seed = 7
    const rand = () => (seed = (seed * 48271) % 2147483647) / 2147483647
    for (let n = 0; n <= 40; n++) {
      const bars: Candle[] = []
      let time = 1_000
      for (let i = 0; i < n; i++) {
        time += rand() < 0.2 ? 0 : 60
        bars.push(bar(time))
      }
      const probes = [-Infinity, 0, 999, 1_000, Infinity, NaN, ...bars.map((b) => b.time)]
      for (const b of bars) probes.push(b.time - 1, b.time + 1, b.time + 30)
      for (const t of probes) expect(lastIndexAtOrBefore(bars, t)).toBe(legacy(bars, t))
    }
  })

  it('is -1 for no bars and for a NaN cursor', () => {
    expect(lastIndexAtOrBefore([], 5)).toBe(-1)
    expect(lastIndexAtOrBefore([bar(1), bar(2)], NaN)).toBe(-1)
  })
})
