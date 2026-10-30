import { beforeEach, describe, expect, it } from 'vitest'
import {
  createCandleCache,
  LIVE_KLINE_MAX_AGE_MS,
  type CacheLogger,
  type CandleCache,
} from '../src/cache/candleCache.ts'
import { openDb, type Db } from '../src/db/index.ts'
import type { LastKline } from '../src/upstream/hub.ts'
import { createRestClient } from '../src/upstream/rest.ts'
import { INTERVAL_SECONDS, type Candle, type Interval } from '../src/types.ts'

const hour = (h: number, m = 0, s = 0) => Date.UTC(2026, 9, 21, h, m, s) / 1000

/**
 * Fake Binance klines endpoint: one bar per interval, close = open time / 1000 (+ 0.5 while the
 * bar is forming by the upstream clock). Filters on open time and honours `limit`.
 */
class FakeUpstream {
  calls: URL[] = []
  /** Upstream clock = local clock − skewSec. */
  skewSec = 0
  /** Open times with no bar upstream (exchange halt). */
  gaps = new Set<number>()
  private readonly clock: { ms: number }
  constructor(clock: { ms: number }) {
    this.clock = clock
  }

  fetch = (async (input: string | URL | Request) => {
    const url = new URL(String(input))
    this.calls.push(url)
    const step = INTERVAL_SECONDS[url.searchParams.get('interval') as Interval]
    const endMs = Number(url.searchParams.get('endTime') ?? Infinity)
    const limit = Number(url.searchParams.get('limit'))
    const nowSec = Math.floor(this.clock.ms / 1000) - this.skewSec
    const rawStart = url.searchParams.get('startTime')
    const times: number[] = []
    if (rawStart === null) {
      const last = Math.floor(Math.min(endMs / 1000, nowSec) / step) * step
      for (let t = last; t >= 0 && times.length < limit; t -= step) {
        if (!this.gaps.has(t)) times.unshift(t)
      }
    } else {
      for (let t = Number(rawStart) / 1000; t * 1000 <= endMs && t <= nowSec; t += step) {
        if (!this.gaps.has(t)) times.push(t)
        if (times.length >= limit) break
      }
    }
    const rows = times.map((t) => {
      const c = t / 1000 + (t + step > nowSec ? 0.5 : 0)
      return [
        t * 1000,
        String(c),
        String(c + 1),
        String(c - 1),
        String(c),
        '10',
        (t + step) * 1000 - 1,
      ]
    })
    return new Response(JSON.stringify(rows), { status: 200 })
  }) as typeof fetch

  starts(): (number | null)[] {
    return this.calls.map((u) => {
      const v = u.searchParams.get('startTime')
      return v === null ? null : Number(v) / 1000
    })
  }
}

/** The bar upstream REST would return for forming open time `t` (1h). */
const formingBar = (t: number): Candle => {
  const c = t / 1000 + 0.5
  return { time: t, open: c, high: c + 1, low: c - 1, close: c, volume: 10 }
}

describe('candle cache: forming bar from the live stream', () => {
  let db: Db
  let clock: { ms: number }
  let up: FakeUpstream
  let cache: CandleCache
  let live: LastKline | undefined
  let logged: { level: 'info' | 'debug'; obj: Record<string, unknown>; msg: string }[]

  const rows = () =>
    db.prepare('SELECT time, close FROM candles ORDER BY time').all() as {
      time: number
      close: number
    }[]
  const upstreamLines = () => logged.filter((l) => l.msg === 'upstream klines')

  beforeEach(() => {
    db = openDb(':memory:')
    clock = { ms: hour(13, 37, 42) * 1000 }
    up = new FakeUpstream(clock)
    live = undefined
    logged = []
    const log: CacheLogger = {
      info: (obj, msg) => logged.push({ level: 'info', obj, msg }),
      debug: (obj, msg) => logged.push({ level: 'debug', obj, msg }),
    }
    const client = createRestClient({
      fetch: up.fetch,
      now: () => clock.ms,
      restBase: 'https://up.test',
    })
    cache = createCandleCache({
      db,
      client,
      now: () => clock.ms,
      live: (symbol, interval) =>
        symbol === 'BTCUSDT' && interval === '1h' && live !== undefined
          ? { ...live, candle: { ...live.candle } }
          : undefined,
      log,
    })
  })

  /** Warm the cache through the REST path (no live stream yet). */
  const warm = async (limit = 5): Promise<Candle[]> => {
    const first = await cache.getCandles('BTCUSDT', '1h', limit)
    up.calls = []
    logged = []
    return first
  }

  it('warm cache + live stream: zero upstream calls, same candles as the REST path', async () => {
    const viaRest = await warm()
    expect(viaRest[4]).toEqual(formingBar(hour(13)))
    live = { candle: formingBar(hour(13)), closed: false, receivedAtMs: clock.ms - 1_000 }
    const viaLive = await cache.getCandles('BTCUSDT', '1h', 5)
    expect(up.calls).toHaveLength(0)
    expect(viaLive).toEqual(viaRest)
    expect(upstreamLines()).toEqual([])
    expect(logged).toContainEqual({
      level: 'debug',
      obj: { symbol: 'BTCUSDT', interval: '1h', time: hour(13) },
      msg: 'forming bar from live stream',
    })
  })

  it('limit 1 and an endTime at or after now are served from the live stream too', async () => {
    await warm()
    live = { candle: formingBar(hour(13)), closed: false, receivedAtMs: clock.ms }
    expect(await cache.getCandles('BTCUSDT', '1h', 1)).toEqual([formingBar(hour(13))])
    expect(await cache.getCandles('BTCUSDT', '1h', 3, hour(20))).toHaveLength(3)
    expect(up.calls).toHaveLength(0)
  })

  it('serves the live values and never stores the forming bar', async () => {
    await warm()
    const before = rows()
    const pushed = { ...formingBar(hour(13)), close: 777, high: 800 }
    live = { candle: pushed, closed: false, receivedAtMs: clock.ms }
    const out = await cache.getCandles('BTCUSDT', '1h', 5)
    expect(out[4]).toEqual(pushed)
    expect(rows()).toEqual(before)
    expect(rows().map((r) => r.time)).not.toContain(hour(13))

    // Bar 13 closes and settles: it is fetched from upstream and stored with upstream's
    // final values, not the pushed forming ones.
    clock.ms = hour(14, 0, 30) * 1000
    live = { candle: formingBar(hour(14)), closed: false, receivedAtMs: clock.ms }
    await cache.getCandles('BTCUSDT', '1h', 5)
    expect(up.calls).toHaveLength(1)
    expect(rows().find((r) => r.time === hour(13))?.close).toBe(hour(13) / 1000)
  })

  it.each<[string, () => LastKline | undefined]>([
    ['no live stream', () => undefined],
    [
      'a push older than the max age',
      () => ({
        candle: formingBar(hour(13)),
        closed: false,
        receivedAtMs: clock.ms - LIVE_KLINE_MAX_AGE_MS - 1,
      }),
    ],
    [
      'a push stamped in the future',
      () => ({ candle: formingBar(hour(13)), closed: false, receivedAtMs: clock.ms + 1_000 }),
    ],
    [
      'a final (x: true) kline',
      () => ({ candle: formingBar(hour(13)), closed: true, receivedAtMs: clock.ms }),
    ],
    [
      'a kline of another bar',
      () => ({ candle: formingBar(hour(12)), closed: false, receivedAtMs: clock.ms }),
    ],
  ])('falls back to one upstream call with %s', async (_name, make) => {
    const viaRest = await warm()
    live = make()
    const out = await cache.getCandles('BTCUSDT', '1h', 5)
    expect(up.starts()).toEqual([hour(13)])
    expect(out).toEqual(viaRest)
    expect(upstreamLines().map((l) => l.obj.reason)).toEqual(['fresh'])
  })

  it('a push exactly at the max age is still used', async () => {
    await warm()
    live = {
      candle: formingBar(hour(13)),
      closed: false,
      receivedAtMs: clock.ms - LIVE_KLINE_MAX_AGE_MS,
    }
    await cache.getCandles('BTCUSDT', '1h', 5)
    expect(up.calls).toHaveLength(0)
  })

  it('falls back when the tail also holds a just-closed, unsettled bar', async () => {
    await warm()
    clock.ms = hour(14, 0, 3) * 1000 // bar 13 closed 3 s ago: inside SETTLE_SEC
    live = { candle: formingBar(hour(14)), closed: false, receivedAtMs: clock.ms }
    const out = await cache.getCandles('BTCUSDT', '1h', 5)
    expect(up.starts()).toEqual([hour(13)])
    expect(out.map((c) => c.time)).toEqual([10, 11, 12, 13, 14].map((h) => hour(h)))
    expect(rows().map((r) => r.time)).not.toContain(hour(13))
  })

  it('falls back when closed bars are missing from the store', async () => {
    await warm()
    clock.ms = hour(15, 10) * 1000 // bars 13 and 14 closed, settled, not stored
    live = { candle: formingBar(hour(15)), closed: false, receivedAtMs: clock.ms }
    const out = await cache.getCandles('BTCUSDT', '1h', 5)
    expect(up.starts()).toEqual([hour(13)])
    expect(upstreamLines().map((l) => l.obj.reason)).toEqual(['missing+fresh'])
    expect(out.map((c) => c.time)).toEqual([11, 12, 13, 14, 15].map((h) => hour(h)))
    expect(out[4]).toEqual(formingBar(hour(15)))
  })

  it('a closed-history page never consults the live stream', async () => {
    let asked = 0
    const c2 = createCandleCache({
      db,
      client: createRestClient({
        fetch: up.fetch,
        now: () => clock.ms,
        restBase: 'https://up.test',
      }),
      now: () => clock.ms,
      live: () => {
        asked += 1
        return undefined
      },
    })
    await c2.getCandles('BTCUSDT', '1h', 5, hour(10))
    expect(asked).toBe(0)
  })
})

describe('candle cache: getPage settled flag and upstream log', () => {
  let db: Db
  let clock: { ms: number }
  let up: FakeUpstream
  let cache: CandleCache
  let reasons: unknown[]

  beforeEach(() => {
    db = openDb(':memory:')
    clock = { ms: hour(13, 37, 42) * 1000 }
    up = new FakeUpstream(clock)
    reasons = []
    cache = createCandleCache({
      db,
      client: createRestClient({
        fetch: up.fetch,
        now: () => clock.ms,
        restBase: 'https://up.test',
      }),
      now: () => clock.ms,
      log: {
        info: (obj, msg) => {
          if (msg === 'upstream klines') reasons.push(obj.reason)
        },
        debug: () => {},
      },
    })
  })

  it('a page with no endTime is never settled; getCandles returns the same candles', async () => {
    const page = await cache.getPage('BTCUSDT', '1h', 5)
    expect(page.settled).toBe(false)
    expect(await cache.getCandles('BTCUSDT', '1h', 5)).toEqual(page.candles)
  })

  it('a page of stored closed bars is settled, cold and warm', async () => {
    const cold = await cache.getPage('BTCUSDT', '1h', 3, hour(10, 59))
    expect(cold).toEqual({
      candles: [8, 9, 10].map((h) => ({
        time: hour(h),
        open: hour(h) / 1000,
        high: hour(h) / 1000 + 1,
        low: hour(h) / 1000 - 1,
        close: hour(h) / 1000,
        volume: 10,
      })),
      settled: true,
    })
    const n = up.calls.length
    expect(await cache.getPage('BTCUSDT', '1h', 3, hour(10, 59))).toEqual(cold)
    expect(up.calls).toHaveLength(n)
  })

  it('a page ending on the forming bar is not settled', async () => {
    expect((await cache.getPage('BTCUSDT', '1h', 3, hour(13))).settled).toBe(false)
  })

  it('a page ending on a closed but unsettled bar is not settled', async () => {
    clock.ms = hour(14, 0, 3) * 1000
    const page = await cache.getPage('BTCUSDT', '1h', 3, hour(13, 30))
    expect(page.candles.map((c) => c.time)).toEqual([11, 12, 13].map((h) => hour(h)))
    expect(page.settled).toBe(false)
  })

  it('a boundary bar upstream has not confirmed (clock skew) is not settled', async () => {
    clock.ms = hour(14, 0, 30) * 1000
    up.skewSec = 60 // upstream still at 13:59:30: bar 13 is forming there
    const page = await cache.getPage('BTCUSDT', '1h', 3, hour(13, 30))
    expect(page.candles.map((c) => c.time)).toEqual([11, 12, 13].map((h) => hour(h)))
    expect(page.settled).toBe(false)
    // Once upstream has bar 14, the same request is settled.
    up.skewSec = 0
    expect((await cache.getPage('BTCUSDT', '1h', 3, hour(13, 30))).settled).toBe(true)
  })

  it('an unconfirmed boundary bar upstream did not return is not settled', async () => {
    // endTime in the storable boundary bar 13 (no fresh tail); upstream has neither bar 13 nor
    // the next bar yet, so nothing confirms 13: it is not stored and not served.
    clock.ms = hour(14, 0, 30) * 1000
    up.skewSec = 60
    up.gaps.add(hour(13))
    const page = await cache.getPage('BTCUSDT', '1h', 3, hour(13, 30))
    expect(page.candles.map((c) => c.time)).toEqual([10, 11, 12].map((h) => hour(h)))
    expect(page.settled).toBe(false)
    // Once upstream has opened bar 14, the empty bar 13 is confirmed and the page is settled.
    up.skewSec = 0
    const later = await cache.getPage('BTCUSDT', '1h', 3, hour(13, 30))
    expect(later.candles.map((c) => c.time)).toEqual([10, 11, 12].map((h) => hour(h)))
    expect(later.settled).toBe(true)
  })

  it('logs one line per upstream klines call with its reason', async () => {
    await cache.getPage('BTCUSDT', '1h', 5) // cold: closed window + forming bar in one call
    await cache.getPage('BTCUSDT', '1h', 5) // warm: forming bar only
    await cache.getPage('BTCUSDT', '1h', 3, hour(5)) // older closed window
    expect(reasons).toEqual(['missing+fresh', 'fresh', 'missing'])
    expect(reasons).toHaveLength(up.calls.length)
  })

  it('logs backfill calls', async () => {
    // A gap in the window leaves it short, so older bars come from an endTime-only call.
    up.gaps.add(hour(4))
    const page = await cache.getPage('BTCUSDT', '1h', 3, hour(5))
    expect(page.candles.map((c) => c.time)).toEqual([2, 3, 5].map((h) => hour(h)))
    expect(page.settled).toBe(true)
    expect(reasons).toEqual(['missing', 'backfill'])
    expect(reasons).toHaveLength(up.calls.length)
  })
})
