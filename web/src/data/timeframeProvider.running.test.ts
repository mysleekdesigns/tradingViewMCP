// The aggregated stream keeps each bucket as running OHLCV. Every candle it emits must equal
// aggregateBucket over the latest send of each base bar of that bucket, bit for bit, through
// forming re-sends (values up or down), late re-sends of earlier base bars, exact duplicates,
// closes and bucket roll-overs. Seeds, gap fills and reseeds keep their exact-value tests in
// timeframeProvider.test.ts.
import { describe, expect, it } from 'vitest'
import type { Candle, Interval } from '../types'
import { aggregateBucket } from './aggregate'
import type { MarketDataProvider } from './provider'
import { createTimeframeProvider } from './timeframeProvider'
import { bucketStart } from './timeframes'

function mulberry32(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let x = a
    x = Math.imul(x ^ (x >>> 15), x | 1)
    x ^= x + Math.imul(x ^ (x >>> 7), x | 61)
    return ((x ^ (x >>> 14)) >>> 0) / 4294967296
  }
}

type OnCandle = (c: Candle, closed: boolean, provisional?: boolean) => void

/** A base provider whose stream the test drives; REST is never expected. */
class StreamOnly implements MarketDataProvider {
  onCandle: OnCandle | null = null
  restCalls = 0
  getCandles(): Promise<Candle[]> {
    this.restCalls += 1
    return Promise.resolve([])
  }
  subscribeCandles(_s: string, _i: Interval, onCandle: OnCandle) {
    this.onCandle = onCandle
    return () => {
      this.onCandle = null
    }
  }
  getSymbols(): Promise<never> {
    return Promise.reject(new Error('unused'))
  }
  getTickers(): Promise<never> {
    return Promise.reject(new Error('unused'))
  }
  subscribeTickers(): () => void {
    return () => {}
  }
}

const M = 60
const T0 = bucketStart(1_700_000_000, '7m')

function randomBar(time: number, rnd: () => number): Candle {
  const open = 100 + (rnd() - 0.5) * 10
  const close = 100 + (rnd() - 0.5) * 10
  return {
    time,
    open,
    high: Math.max(open, close) + rnd() * 3,
    low: Math.min(open, close) - rnd() * 3,
    close,
    volume: rnd() < 0.1 ? 0 : rnd() * 10 ** Math.floor(rnd() * 6 - 2),
  }
}

function expectSame(got: Candle, want: Candle | null): void {
  expect(want).not.toBeNull()
  if (want === null) return
  for (const k of ['time', 'open', 'high', 'low', 'close', 'volume'] as const) {
    expect(Object.is(got[k], want[k]), `${k}: ${got[k]} vs ${want[k]}`).toBe(true)
  }
}

describe('aggregated stream: running bucket equals a full re-aggregation', () => {
  it('every emit matches aggregateBucket of the latest base sends (random streams, 7m from 1m)', () => {
    for (let seed = 1; seed <= 60; seed++) {
      const rnd = mulberry32(seed)
      const base = new StreamOnly()
      const emitted: [Candle, boolean][] = []
      const unsubscribe = createTimeframeProvider(base).subscribeCandles(
        'BTCUSDT',
        '7m',
        (c, closed) => emitted.push([c, closed]),
      )
      /** Latest send per base time, per bucket the stream still accepts. */
      const shadow = new Map<number, Map<number, Candle>>()
      const closedBuckets = new Set<number>()
      let newest = T0 // entered at a bucket's first base bar: no seed
      let newestBar = randomBar(newest, rnd)
      const send = (c: Candle, closed: boolean) => {
        const start = bucketStart(c.time, '7m')
        if (!closedBuckets.has(start)) {
          let m = shadow.get(start)
          if (m === undefined) shadow.set(start, (m = new Map()))
          m.set(c.time, c)
        }
        const from = emitted.length
        base.onCandle?.(c, closed)
        for (const [candle, isClosed] of emitted.slice(from)) {
          const bars = shadow.get(candle.time)
          expectSame(candle, aggregateBucket(candle.time, bars?.values() ?? []))
          if (isClosed) {
            expect(closedBuckets.has(candle.time)).toBe(false)
            closedBuckets.add(candle.time)
          }
        }
      }
      send(newestBar, false)
      for (let step = 0; step < 400; step++) {
        const r = rnd()
        if (r < 0.2) {
          // The newest base bar closes; the next one opens.
          send(newestBar, true)
          newest += M
          newestBar = randomBar(newest, rnd)
          send(newestBar, false)
        } else if (r < 0.85) {
          // Forming re-send with new values (high, low and volume may go down).
          newestBar = randomBar(newest, rnd)
          send(newestBar, false)
        } else if (r < 0.95) {
          // A late re-send of an earlier base bar of the current bucket.
          const start = bucketStart(newest, '7m')
          const t = start + Math.floor(rnd() * ((newest - start) / M + 1)) * M
          send(t === newest ? newestBar : randomBar(t, rnd), false)
        } else {
          // An exact duplicate of the newest send.
          send(newestBar, false)
        }
      }
      expect(base.restCalls).toBe(0)
      expect(closedBuckets.size).toBeGreaterThan(2)
      unsubscribe()
    }
  })
})
