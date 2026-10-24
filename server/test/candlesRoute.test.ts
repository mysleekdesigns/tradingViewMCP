import type { FastifyInstance } from 'fastify'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { buildApp } from '../src/app.ts'
import { openDb, type Db } from '../src/db/index.ts'
import type { Candle } from '../src/types.ts'

const hour = (h: number, m = 0) => Date.UTC(2026, 9, 21, h, m) / 1000

type Reply = { status: number; body: unknown; headers?: Record<string, string> }

describe('GET /api/candles', () => {
  let db: Db
  let app: FastifyInstance
  let calls: URL[]
  let reply: (url: URL) => Reply
  const clock = { ms: 0 }

  /** Open times (s) with no bar upstream (exchange halt). */
  let gaps: (t: number) => boolean
  /** 1h fake: Binance semantics, including endTime-only = the last `limit` bars <= endTime. */
  const klines = (url: URL): Reply => {
    const endMs = Number(url.searchParams.get('endTime'))
    const limit = Number(url.searchParams.get('limit'))
    const nowSec = clock.ms / 1000
    const times: number[] = []
    const rawStart = url.searchParams.get('startTime')
    if (rawStart === null) {
      const last = Math.floor(Math.min(endMs / 1000, nowSec) / 3600) * 3600
      for (let t = last; t >= 0 && times.length < limit; t -= 3600) if (!gaps(t)) times.unshift(t)
    } else {
      const start = Number(rawStart) / 1000
      for (let t = start; t * 1000 <= endMs && t <= nowSec && times.length < limit; t += 3600) {
        if (!gaps(t)) times.push(t)
      }
    }
    const rows = times.map((t) => [
      t * 1000,
      '1.5',
      '2',
      '1',
      String(t),
      '3.25',
      (t + 3600) * 1000 - 1,
    ])
    return { status: 200, body: rows }
  }

  beforeEach(async () => {
    db = openDb(':memory:')
    calls = []
    clock.ms = hour(13, 30) * 1000
    reply = klines
    gaps = () => false
    const fakeFetch = (async (input: string | URL | Request) => {
      const url = new URL(String(input))
      calls.push(url)
      const r = reply(url)
      return new Response(JSON.stringify(r.body), { status: r.status, headers: r.headers ?? {} })
    }) as typeof fetch
    app = await buildApp({ db, fetch: fakeFetch, now: () => clock.ms, restBase: 'https://up.test' })
    await app.ready()
  })

  afterEach(async () => {
    await app.close()
    db.close()
  })

  const get = (qs: string) => app.inject({ method: 'GET', url: `/api/candles?${qs}` })

  it('serves candles as JSON numbers and caches closed bars', async () => {
    const res = await get('symbol=btcusdt&interval=1h&limit=3')
    expect(res.statusCode).toBe(200)
    const body = res.json<Candle[]>()
    expect(body).toEqual([
      { time: hour(11), open: 1.5, high: 2, low: 1, close: hour(11), volume: 3.25 },
      { time: hour(12), open: 1.5, high: 2, low: 1, close: hour(12), volume: 3.25 },
      { time: hour(13), open: 1.5, high: 2, low: 1, close: hour(13), volume: 3.25 },
    ])
    expect(calls[0]?.searchParams.get('symbol')).toBe('BTCUSDT')
    expect(calls).toHaveLength(1)
    await get('symbol=BTCUSDT&interval=1h&limit=3')
    expect(calls).toHaveLength(2) // forming bar only
    expect(Number(calls[1]?.searchParams.get('startTime'))).toBe(hour(13) * 1000)
  })

  it('defaults limit to 500', async () => {
    const res = await get('symbol=BTCUSDT&interval=1h')
    expect(res.json<Candle[]>()).toHaveLength(500)
  })

  it('honours endTime in seconds', async () => {
    const res = await get(`symbol=BTCUSDT&interval=1h&limit=2&endTime=${hour(5, 59)}`)
    expect(res.json<Candle[]>().map((c) => c.time)).toEqual([hour(4), hour(5)])
  })

  it.each([
    ['symbol=B&interval=1h', 'bad-symbol'],
    ['symbol=BTC-USD&interval=1h', 'bad-symbol'],
    ['interval=1h', 'bad-symbol'],
    ['symbol=BTCUSDT&interval=2h', 'http'],
    ['symbol=BTCUSDT', 'http'],
    ['symbol=BTCUSDT&interval=1h&limit=0', 'http'],
    ['symbol=BTCUSDT&interval=1h&limit=1001', 'http'],
    ['symbol=BTCUSDT&interval=1h&limit=1.5', 'http'],
    ['symbol=BTCUSDT&interval=1h&endTime=-5', 'http'],
    ['symbol=BTCUSDT&interval=1h&endTime=abc', 'http'],
    ['symbol=BTCUSDT&interval=1h&limit=5&limit=6', 'http'],
  ])('rejects %s with 400 %s and no upstream call', async (qs, kind) => {
    const res = await get(qs)
    expect(res.statusCode).toBe(400)
    expect(res.json()).toMatchObject({ error: kind })
    expect(calls).toHaveLength(0)
  })

  it('maps upstream -1121 to 400 bad-symbol', async () => {
    reply = () => ({ status: 400, body: { code: -1121, msg: 'Invalid symbol.' } })
    const res = await get('symbol=NOPEUSDT&interval=1h&limit=3')
    expect(res.statusCode).toBe(400)
    expect(res.json()).toEqual({ error: 'bad-symbol', message: expect.any(String) })
  })

  it('maps upstream 429 to 429 with Retry-After and retryAfter', async () => {
    reply = () => ({ status: 429, body: {}, headers: { 'Retry-After': '42' } })
    const res = await get('symbol=BTCUSDT&interval=1h&limit=3')
    expect(res.statusCode).toBe(429)
    expect(res.headers['retry-after']).toBe('42')
    expect(res.json()).toMatchObject({ error: 'rate-limited', retryAfter: 42 })
    // Paused: the next request fails without an upstream call.
    const again = await get('symbol=ETHUSDT&interval=1h&limit=3')
    expect(again.statusCode).toBe(429)
    expect(calls).toHaveLength(1)
  })

  it('maps upstream 418 to 503 banned and 451 to 503 region-blocked', async () => {
    reply = () => ({ status: 451, body: {} })
    const blocked = await get('symbol=BTCUSDT&interval=1h&limit=3')
    expect([blocked.statusCode, blocked.json<{ error: string }>().error]).toEqual([
      503,
      'region-blocked',
    ])
    reply = () => ({ status: 418, body: {}, headers: { 'Retry-After': '120' } })
    const banned = await get('symbol=BTCUSDT&interval=1h&limit=3')
    expect(banned.statusCode).toBe(503)
    expect(banned.headers['retry-after']).toBe('120')
    expect(banned.json()).toMatchObject({ error: 'banned', retryAfter: 120 })
  })

  it('maps malformed upstream data to 502 parse', async () => {
    reply = () => ({ status: 200, body: [['x']] })
    const res = await get('symbol=BTCUSDT&interval=1h&limit=3')
    expect(res.statusCode).toBe(502)
    expect(res.json()).toMatchObject({ error: 'parse' })
  })

  it('a gap longer than limit still returns limit bars (loadOlder never sees a false end)', async () => {
    // Halt from 2026-10-01 00:00 to 2026-10-21 00:00: 480 missing 1h bars, > limit.
    const haltStart = Date.UTC(2026, 9, 1) / 1000
    gaps = (t) => t >= haltStart && t < hour(0)
    const res = await get(`symbol=BTCUSDT&interval=1h&limit=100&endTime=${hour(0) - 1}`)
    expect(res.statusCode).toBe(200)
    const body = res.json<Candle[]>()
    expect(body).toHaveLength(100)
    expect(body[99]?.time).toBe(haltStart - 3600)
    expect(body[0]?.time).toBe(haltStart - 100 * 3600)
    const n = calls.length
    const again = await get(`symbol=BTCUSDT&interval=1h&limit=100&endTime=${hour(0) - 1}`)
    expect(again.json()).toEqual(body)
    expect(calls).toHaveLength(n)
  })

  it('a symbol listed after endTime returns [] once, then from cache', async () => {
    gaps = (t) => t < hour(10)
    const res = await get(`symbol=BTCUSDT&interval=1h&limit=5&endTime=${hour(3)}`)
    expect(res.json()).toEqual([])
    const n = calls.length
    await get(`symbol=BTCUSDT&interval=1h&limit=5&endTime=${hour(3)}`)
    await get(`symbol=BTCUSDT&interval=1h&limit=5&endTime=${hour(1)}`)
    expect(calls).toHaveLength(n)
  })

  it.each([
    [429, 'rate-limited'],
    [418, 'banned'],
  ])(
    'upstream %i on the forming bar after a warm cache → 200 with cached closed bars',
    async (status) => {
      const warm = await get('symbol=BTCUSDT&interval=1h&limit=3')
      reply = () => ({ status, body: {}, headers: { 'Retry-After': '30' } })
      const res = await get('symbol=BTCUSDT&interval=1h&limit=3')
      expect(res.statusCode).toBe(200)
      expect(res.json()).toEqual(warm.json<Candle[]>().slice(0, 2))
    },
  )

  it('coalesces two tabs loading the same chart into one upstream fetch', async () => {
    const [a, b] = await Promise.all([
      get('symbol=BTCUSDT&interval=1h&limit=10'),
      get('symbol=BTCUSDT&interval=1h&limit=10'),
    ])
    expect(a.json()).toEqual(b.json())
    expect(calls).toHaveLength(1)
  })
})
