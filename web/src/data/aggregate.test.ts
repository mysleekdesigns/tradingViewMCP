import { describe, expect, it } from 'vitest'
import type { Candle, Timeframe } from '../types'
import { aggregateBucket, aggregateCandles } from './aggregate'
import { bucketStart, timeframeSeconds } from './timeframes'

function bar(time: number, open: number, high: number, low: number, close: number, volume = 1) {
  return { time, open, high, low, close, volume }
}

/** Deterministic pseudo-random walk of `n` base bars, `step` seconds apart. */
function walk(n: number, start: number, step: number, seed = 7): Candle[] {
  let s = seed
  const rand = () => {
    s = (s * 1_103_515_245 + 12_345) % 2_147_483_648
    return s / 2_147_483_648
  }
  const out: Candle[] = []
  let price = 100
  for (let i = 0; i < n; i++) {
    const open = price
    const close = open + (rand() - 0.5) * 2
    const high = Math.max(open, close) + rand()
    const low = Math.min(open, close) - rand()
    out.push({ time: start + i * step, open, high, low, close, volume: Math.round(rand() * 10) })
    price = close
  }
  return out
}

describe('aggregateBucket', () => {
  it('first open, max high, min low, last close, summed volume, in any order', () => {
    const b = aggregateBucket(60, [
      bar(120, 11, 15, 10, 14, 3),
      bar(60, 10, 12, 9, 11, 2),
      bar(180, 14, 14, 8, 9, 5),
    ])
    expect(b).toEqual({ time: 60, open: 10, high: 15, low: 8, close: 9, volume: 10 })
  })

  it('keeps the later duplicate and returns null for no bars', () => {
    expect(aggregateBucket(0, [bar(0, 1, 2, 0, 1, 1), bar(0, 1, 3, 0, 2, 4)])).toEqual(
      bar(0, 1, 3, 0, 2, 4),
    )
    expect(aggregateBucket(0, [])).toBeNull()
  })
})

describe('aggregateCandles', () => {
  it('groups 1m bars into 3m buckets', () => {
    const base = [
      bar(0, 1, 2, 0.5, 1.5, 1),
      bar(60, 1.5, 3, 1, 2, 2),
      bar(120, 2, 2.5, 1.8, 2.2, 3),
      bar(180, 2.2, 2.4, 2, 2.1, 4),
      bar(240, 2.1, 5, 2, 4, 5),
    ]
    expect(aggregateCandles(base, '3m')).toEqual([
      { time: 0, open: 1, high: 3, low: 0.5, close: 2.2, volume: 6 },
      { time: 180, open: 2.2, high: 5, low: 2, close: 4, volume: 9 },
    ])
  })

  it('skips empty buckets (no forward fill) and sorts unsorted input', () => {
    const base = [bar(600, 5, 6, 4, 5.5), bar(0, 1, 2, 0, 1.5), bar(60, 1.5, 2, 1, 1.8)]
    const out = aggregateCandles(base, '3m')
    expect(out.map((c) => c.time)).toEqual([0, 540])
    expect(out[1]).toEqual({ time: 540, open: 5, high: 6, low: 4, close: 5.5, volume: 1 })
  })

  it('aggregates 1s bars into 5s buckets and 1d into Binance-aligned 3d', () => {
    const secs = walk(23, 1_700_000_003, 1)
    const fives = aggregateCandles(secs, '5s')
    expect(fives[0].time).toBe(1_700_000_000)
    expect(fives.every((c) => c.time % 5 === 0)).toBe(true)
    expect(fives.reduce((s, c) => s + c.volume, 0)).toBe(secs.reduce((s, c) => s + c.volume, 0))

    const days = walk(9, 1_792_713_600, 86_400)
    const threes = aggregateCandles(days, '3d')
    expect(threes.map((c) => c.time)).toEqual([1_792_713_600, 1_792_972_800, 1_793_232_000])
  })

  it('returns ascending unique times and never mutates the input', () => {
    const base = walk(500, 1_700_000_040, 60)
    const copy = base.map((c) => ({ ...c }))
    const out = aggregateCandles(base, '7m')
    expect(base).toEqual(copy)
    for (let i = 1; i < out.length; i++) expect(out[i].time).toBeGreaterThan(out[i - 1].time)
  })

  it('no look-ahead: changing base bars after a bucket ends never changes that bucket', () => {
    const tfs: [Timeframe, number][] = [
      ['5s', 1],
      ['30s', 1],
      ['3m', 60],
      ['7m', 60],
      ['2h', 3_600],
      ['3d', 86_400],
      ['2w', 604_800],
    ]
    for (const [tf, step] of tfs) {
      const base = walk(400, bucketStart(1_700_000_000, tf) + step * 3, step, 11)
      const before = aggregateCandles(base, tf)
      const len = timeframeSeconds(tf)
      for (const bucket of before.slice(0, -1)) {
        const end = bucket.time + len
        const perturbed = base.map((c) =>
          c.time >= end
            ? { ...c, open: c.open * 3, high: c.high * 5, low: c.low / 7, close: -1, volume: 1e9 }
            : c,
        )
        // Also drop and add future bars: neither may leak backwards.
        const dropped = perturbed.filter((c, i) => c.time < end || i % 2 === 0)
        for (const variant of [perturbed, dropped]) {
          const after = aggregateCandles(variant, tf)
          expect(
            after.find((c) => c.time === bucket.time),
            `${tf} bucket ${bucket.time}`,
          ).toEqual(bucket)
        }
      }
    }
  })
})
