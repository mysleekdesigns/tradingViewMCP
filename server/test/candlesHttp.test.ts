import { brotliDecompressSync, gunzipSync } from 'node:zlib'
import type { FastifyInstance } from 'fastify'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { buildApp } from '../src/app.ts'
import { openDb, type Db } from '../src/db/index.ts'
import {
  bodyEtag,
  CACHE_IMMUTABLE,
  CACHE_REVALIDATE,
  ifNoneMatchHits,
} from '../src/routes/candles.ts'
import type { Candle } from '../src/types.ts'

const hour = (h: number, m = 0) => Date.UTC(2026, 9, 21, h, m) / 1000

/** Fake upstream WebSocket (the Binance side): the test opens it and emits pushes. */
class FakeUpstreamWS {
  static instances: FakeUpstreamWS[] = []
  readonly url: string
  onopen: ((ev: unknown) => void) | null = null
  onmessage: ((ev: { data: unknown }) => void) | null = null
  onerror: ((ev: unknown) => void) | null = null
  onclose: ((ev: unknown) => void) | null = null
  constructor(url: string) {
    this.url = url
    FakeUpstreamWS.instances.push(this)
  }
  send(): void {}
  close(): void {}
  emitOpen(): void {
    this.onopen?.({})
  }
  emit(data: unknown): void {
    this.onmessage?.({ data: JSON.stringify(data) })
  }
  emitClose(): void {
    this.onclose?.({})
  }
}

interface TestClient {
  close(): void
  on(event: 'message', cb: (data: Buffer) => void): void
}

/** The kline push for 1h open time `t`, with the values the REST fake returns for it. */
const klinePush = (t: number, x = false) => ({
  e: 'kline',
  E: t * 1000 + 1,
  s: 'BTCUSDT',
  k: { t: t * 1000, s: 'BTCUSDT', i: '1h', o: '1.5', h: '2', l: '1', c: String(t), v: '3.25', x },
})

describe('/api/candles: live forming bar, compression, caching headers', () => {
  let db: Db
  let app: FastifyInstance
  let calls: URL[]
  const clock = { ms: 0 }

  beforeEach(async () => {
    db = openDb(':memory:')
    calls = []
    FakeUpstreamWS.instances = []
    clock.ms = hour(13, 30) * 1000
    // 1h klines, Binance semantics; same values as candlesRoute.test.ts.
    const fakeFetch = (async (input: string | URL | Request) => {
      const url = new URL(String(input))
      calls.push(url)
      const endMs = Number(url.searchParams.get('endTime'))
      const limit = Number(url.searchParams.get('limit'))
      const nowSec = clock.ms / 1000
      const times: number[] = []
      const rawStart = url.searchParams.get('startTime')
      if (rawStart === null) {
        const last = Math.floor(Math.min(endMs / 1000, nowSec) / 3600) * 3600
        for (let t = last; t >= 0 && times.length < limit; t -= 3600) times.unshift(t)
      } else {
        const start = Number(rawStart) / 1000
        for (let t = start; t * 1000 <= endMs && t <= nowSec && times.length < limit; t += 3600) {
          times.push(t)
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
      return new Response(JSON.stringify(rows), { status: 200 })
    }) as typeof fetch
    app = await buildApp({
      db,
      fetch: fakeFetch,
      now: () => clock.ms,
      restBase: 'https://up.test',
      WebSocketImpl: FakeUpstreamWS as unknown as typeof WebSocket,
      wsBase: 'wss://upstream.test',
    })
    await app.ready()
  })

  afterEach(async () => {
    await app.close()
    db.close()
  })

  const get = (qs: string, headers: Record<string, string> = {}) =>
    app.inject({ method: 'GET', url: `/api/candles?${qs}`, headers })

  /** Attach a browser to the BTCUSDT 1h stream so the hub holds the upstream socket. */
  const attachBrowser = async (): Promise<{ client: TestClient; messages: unknown[] }> => {
    const messages: unknown[] = []
    const client = (await app.injectWS(
      '/ws/klines?symbol=BTCUSDT&interval=1h',
      { headers: { host: 'localhost:80', 'accept-encoding': 'gzip, deflate, br' } },
      {
        onInit: (raw) => {
          ;(raw as unknown as TestClient).on('message', (d) =>
            messages.push(JSON.parse(d.toString())),
          )
        },
      },
    )) as unknown as TestClient
    return { client, messages }
  }

  const settle = async (): Promise<void> => {
    for (let i = 0; i < 5; i++) await new Promise<void>((r) => setImmediate(r))
  }

  describe('forming bar from the hub', () => {
    it('a warm-cache request with a live stream makes zero upstream calls, same JSON', async () => {
      const { client } = await attachBrowser()
      const up = FakeUpstreamWS.instances[0]!
      up.emitOpen()
      up.emit(klinePush(hour(13)))
      const first = await get('symbol=BTCUSDT&interval=1h&limit=50')
      expect(calls).toHaveLength(1) // cold: the closed window needs upstream
      const n = calls.length
      clock.ms += 1_000
      up.emit(klinePush(hour(13)))
      const second = await get('symbol=BTCUSDT&interval=1h&limit=50')
      expect(calls).toHaveLength(n)
      expect(second.statusCode).toBe(200)
      expect(second.body).toBe(first.body)
      expect(second.json<Candle[]>().at(-1)).toEqual({
        time: hour(13),
        open: 1.5,
        high: 2,
        low: 1,
        close: hour(13),
        volume: 3.25,
      })
      client.close()
    })

    it('within the release grace period (page reload) the stream still serves the bar', async () => {
      const { client } = await attachBrowser()
      const up = FakeUpstreamWS.instances[0]!
      up.emitOpen()
      await get('symbol=BTCUSDT&interval=1h&limit=10')
      client.close()
      await settle()
      up.emit(klinePush(hour(13)))
      const n = calls.length
      await get('symbol=BTCUSDT&interval=1h&limit=10')
      expect(calls).toHaveLength(n)
    })

    it('falls back to one upstream call once the upstream socket drops', async () => {
      const { client } = await attachBrowser()
      const up = FakeUpstreamWS.instances[0]!
      up.emitOpen()
      up.emit(klinePush(hour(13)))
      const warm = await get('symbol=BTCUSDT&interval=1h&limit=10')
      up.emitClose()
      const n = calls.length
      const res = await get('symbol=BTCUSDT&interval=1h&limit=10')
      expect(calls).toHaveLength(n + 1)
      expect(Number(calls.at(-1)?.searchParams.get('startTime'))).toBe(hour(13) * 1000)
      expect(res.body).toBe(warm.body)
      client.close()
    })

    it('falls back to one upstream call when the last push is stale', async () => {
      const { client } = await attachBrowser()
      const up = FakeUpstreamWS.instances[0]!
      up.emitOpen()
      up.emit(klinePush(hour(13)))
      await get('symbol=BTCUSDT&interval=1h&limit=10')
      clock.ms += 60_000
      const n = calls.length
      await get('symbol=BTCUSDT&interval=1h&limit=10')
      expect(calls).toHaveLength(n + 1)
      client.close()
    })

    it('without any browser stream every warm request makes one upstream call', async () => {
      await get('symbol=BTCUSDT&interval=1h&limit=10')
      await get('symbol=BTCUSDT&interval=1h&limit=10')
      expect(calls).toHaveLength(2)
    })

    it('never stores the live forming bar as closed', async () => {
      const { client } = await attachBrowser()
      const up = FakeUpstreamWS.instances[0]!
      up.emitOpen()
      await get('symbol=BTCUSDT&interval=1h&limit=10')
      up.emit(klinePush(hour(13)))
      await get('symbol=BTCUSDT&interval=1h&limit=10')
      const stored = db.prepare('SELECT MAX(time) AS t FROM candles').get() as { t: number }
      expect(stored.t).toBe(hour(12))
      client.close()
    })
  })

  describe('compression', () => {
    it.each([
      ['gzip', (b: Buffer) => gunzipSync(b)],
      ['br', (b: Buffer) => brotliDecompressSync(b)],
    ])('%s decodes to the identical JSON', async (encoding, decode) => {
      const plain = await get('symbol=BTCUSDT&interval=1h&limit=200&endTime=' + hour(10))
      const packed = await get('symbol=BTCUSDT&interval=1h&limit=200&endTime=' + hour(10), {
        'accept-encoding': encoding,
      })
      expect(packed.statusCode).toBe(200)
      expect(packed.headers['content-encoding']).toBe(encoding)
      expect(String(packed.headers.vary)).toMatch(/accept-encoding/i)
      expect(packed.rawPayload.length).toBeLessThan(plain.rawPayload.length / 3)
      expect(decode(packed.rawPayload).toString('utf8')).toBe(plain.body)
      expect(packed.headers.etag).toBe(plain.headers.etag)
    })

    it('prefers brotli when the browser accepts both', async () => {
      const res = await get('symbol=BTCUSDT&interval=1h&limit=200', {
        'accept-encoding': 'gzip, deflate, br, zstd',
      })
      expect(res.headers['content-encoding']).toBe('br')
    })

    it('sends identity when compression is not accepted', async () => {
      const res = await get('symbol=BTCUSDT&interval=1h&limit=200')
      expect(res.headers['content-encoding']).toBeUndefined()
      expect(res.json<Candle[]>()).toHaveLength(200)
      const identity = await get('symbol=BTCUSDT&interval=1h&limit=200', {
        'accept-encoding': 'identity',
      })
      expect(identity.headers['content-encoding']).toBeUndefined()
    })

    it('leaves bodies under 1 KB uncompressed', async () => {
      const res = await get('symbol=BTCUSDT&interval=1h&limit=1', { 'accept-encoding': 'br' })
      expect(res.headers['content-encoding']).toBeUndefined()
      expect(res.json<Candle[]>()).toHaveLength(1)
    })

    it('error bodies keep their shape and status', async () => {
      const res = await get('symbol=BTCUSDT&interval=2h', { 'accept-encoding': 'gzip' })
      expect(res.statusCode).toBe(400)
      expect(res.json()).toMatchObject({ error: 'http' })
      expect(res.headers['cache-control']).toBeUndefined()
    })

    it('WebSocket routes still deliver plain JSON frames', async () => {
      const { client, messages } = await attachBrowser()
      const up = FakeUpstreamWS.instances[0]!
      up.emitOpen()
      up.emit(klinePush(hour(13)))
      await settle()
      expect(messages).toEqual([
        { type: 'status', status: 'reconnecting' },
        { type: 'status', status: 'live' },
        {
          type: 'kline',
          candle: { time: hour(13), open: 1.5, high: 2, low: 1, close: hour(13), volume: 3.25 },
          closed: false,
        },
      ])
      client.close()
    })
  })

  describe('caching headers', () => {
    const closedNoVenue = `symbol=BTCUSDT&interval=1h&limit=20&endTime=${hour(10) - 1}`
    const closedPage = `${closedNoVenue}&venue=binance`

    it('a closed-history page is immutable with an ETag of its body', async () => {
      const res = await get(closedPage)
      expect(res.statusCode).toBe(200)
      expect(res.headers['cache-control']).toBe(CACHE_IMMUTABLE)
      expect(res.headers['cache-control']).toBe('public, max-age=31536000, immutable')
      expect(res.headers.etag).toBe(bodyEtag(res.body, 'binance'))
      expect(res.headers['content-type']).toBe('application/json; charset=utf-8')
    })

    it('a matching If-None-Match gets 304 with no body and no upstream call', async () => {
      const first = await get(closedPage)
      const n = calls.length
      const etag = String(first.headers.etag)
      const res = await get(closedPage, { 'if-none-match': etag, 'accept-encoding': 'br' })
      expect(res.statusCode).toBe(304)
      expect(res.body).toBe('')
      expect(res.headers.etag).toBe(etag)
      expect(res.headers['cache-control']).toBe(CACHE_IMMUTABLE)
      expect(res.headers['content-encoding']).toBeUndefined()
      expect(calls).toHaveLength(n)
    })

    it('a different If-None-Match gets the full 200', async () => {
      await get(closedPage)
      const res = await get(closedPage, { 'if-none-match': 'W/"something-else"' })
      expect(res.statusCode).toBe(200)
      expect(res.json<Candle[]>()).toHaveLength(20)
    })

    it.each([
      ['no endTime', 'symbol=BTCUSDT&interval=1h&limit=20'],
      ['endTime in the forming bar', `symbol=BTCUSDT&interval=1h&limit=20&endTime=${hour(13, 10)}`],
      ['endTime in the future', `symbol=BTCUSDT&interval=1h&limit=20&endTime=${hour(20)}`],
    ])('%s: no-cache (revalidate), never immutable', async (_name, qs) => {
      const res = await get(qs)
      expect(res.statusCode).toBe(200)
      expect(res.headers['cache-control']).toBe(CACHE_REVALIDATE)
      expect(res.headers['cache-control']).toBe('no-cache')
      expect(res.headers.etag).toBe(bodyEtag(res.body, 'binance'))
    })

    it('an endTime in a closed but unsettled bar is not immutable', async () => {
      clock.ms = (hour(14) + 2) * 1000 // bar 13 closed 2 s ago
      const res = await get(`symbol=BTCUSDT&interval=1h&limit=5&endTime=${hour(13, 30)}`)
      expect(res.json<Candle[]>().at(-1)?.time).toBe(hour(13))
      expect(res.headers['cache-control']).toBe(CACHE_REVALIDATE)
    })

    it('a closed page is immutable only when the URL names the server venue', async () => {
      const named = await get(closedPage)
      expect(named.headers['cache-control']).toBe(CACHE_IMMUTABLE)
      for (const qs of [
        closedNoVenue, // old clients: no venue param
        `${closedNoVenue}&venue=binance-us`, // another venue
        `${closedNoVenue}&venue=coinbase`, // a venue this server does not know
        `${closedNoVenue}&venue=`, // empty
        `${closedNoVenue}&venue=BINANCE`, // not a venue id
        `${closedNoVenue}&venue=binance%20`, // not a venue id
        `${closedNoVenue}&venue=${'a'.repeat(40)}`, // too long
        `${closedNoVenue}&venue=binance&venue=binance`, // repeated
      ]) {
        const res = await get(qs)
        expect(res.statusCode, qs).toBe(200) // never a 400 for the venue
        expect(res.headers['cache-control'], qs).toBe(CACHE_REVALIDATE)
        expect(res.headers.etag, qs).toBe(named.headers.etag)
        expect(res.body, qs).toBe(named.body)
      }
    })

    it('a page reaching the forming bar stays no-cache with a matching venue', async () => {
      const res = await get('symbol=BTCUSDT&interval=1h&limit=20&venue=binance')
      expect(res.headers['cache-control']).toBe(CACHE_REVALIDATE)
      const closed = await get(
        `symbol=BTCUSDT&interval=1h&limit=5&endTime=${hour(13)}&venue=binance`,
      )
      expect(closed.headers['cache-control']).toBe(CACHE_REVALIDATE)
    })

    it('the ETag names the venue: same body, different venue, different tag', () => {
      const body = '[{"time":1}]'
      expect(bodyEtag(body, 'binance')).toMatch(/^W\/"binance:[A-Za-z0-9_-]+"$/)
      expect(bodyEtag(body, 'binance-us')).not.toBe(bodyEtag(body, 'binance'))
      expect(ifNoneMatchHits(bodyEtag(body, 'binance-us'), bodyEtag(body, 'binance'))).toBe(false)
      expect(bodyEtag(body, 'binance')).toBe(bodyEtag(body, 'binance'))
    })

    it('the body is the same JSON Fastify would have serialised', async () => {
      const res = await get(closedPage)
      expect(res.body).toBe(JSON.stringify(res.json()))
    })
  })
})

describe('ifNoneMatchHits', () => {
  const tag = 'W/"abc"'
  it.each<[string | string[] | undefined, boolean]>([
    [undefined, false],
    ['', false],
    ['W/"abc"', true],
    ['"abc"', true],
    ['"x", W/"abc"', true],
    [['"x"', 'W/"abc"'], true],
    ['*', true],
    ['W/"abd"', false],
    ['abc', false],
  ])('%j -> %s', (header, hit) => {
    expect(ifNoneMatchHits(header, tag)).toBe(hit)
  })
})

describe('/api/candles caching across a venue switch', () => {
  /** 1h klines up to the clock, Binance semantics (startTime or endTime-only). */
  const upstream = (nowSec: number) =>
    (async (input: string | URL | Request) => {
      const url = new URL(String(input))
      const endMs = Number(url.searchParams.get('endTime'))
      const limit = Number(url.searchParams.get('limit'))
      const times: number[] = []
      const rawStart = url.searchParams.get('startTime')
      if (rawStart === null) {
        const last = Math.floor(Math.min(endMs / 1000, nowSec) / 3600) * 3600
        for (let t = last; t >= 0 && times.length < limit; t -= 3600) times.unshift(t)
      } else {
        for (
          let t = Number(rawStart) / 1000;
          t * 1000 <= endMs && t <= nowSec && times.length < limit;
          t += 3600
        ) {
          times.push(t)
        }
      }
      const rows = times.map((t) => [t * 1000, '1', '2', '0.5', '1.5', '4', (t + 3600) * 1000 - 1])
      return new Response(JSON.stringify(rows), { status: 200 })
    }) as typeof fetch

  const nowMs = hour(13, 30) * 1000
  const page = `symbol=BTCUSDT&interval=1h&limit=10&endTime=${hour(10) - 1}`
  const opened: { app: FastifyInstance; db: Db }[] = []

  /** One server process on venue `id` (its own empty DB, as after ensureVenue's clear). */
  const serverOn = async (id: 'binance' | 'binance-us', name: string) => {
    const db = openDb(':memory:')
    const app = await buildApp({
      db,
      fetch: upstream(nowMs / 1000),
      now: () => nowMs,
      restBase: 'https://up.test',
      venue: { id, name },
    })
    await app.ready()
    opened.push({ app, db })
    return (qs: string, headers: Record<string, string> = {}) =>
      app.inject({ method: 'GET', url: `/api/candles?${qs}`, headers })
  }

  afterEach(async () => {
    for (const { app, db } of opened.splice(0)) {
      await app.close()
      db.close()
    }
  })

  it('immutable only for the venue the server runs; the ETag differs per venue', async () => {
    const before = await serverOn('binance', 'Binance')
    const after = await serverOn('binance-us', 'Binance.US')
    const a = await before(`${page}&venue=binance`)
    const b = await after(`${page}&venue=binance-us`)
    expect(a.headers['cache-control']).toBe(CACHE_IMMUTABLE)
    expect(b.headers['cache-control']).toBe(CACHE_IMMUTABLE)
    // Same upstream bytes on both venues, yet the tags differ.
    expect(b.body).toBe(a.body)
    expect(b.headers.etag).not.toBe(a.headers.etag)
    // The old venue's URL on the new server: served, but never immutable.
    const stale = await after(`${page}&venue=binance`)
    expect(stale.statusCode).toBe(200)
    expect(stale.headers['cache-control']).toBe(CACHE_REVALIDATE)
    expect(stale.headers.etag).toBe(b.headers.etag)
  })

  it('a revalidation carrying the old venue tag never gets a 304', async () => {
    const before = await serverOn('binance', 'Binance')
    const after = await serverOn('binance-us', 'Binance.US')
    for (const qs of [page, `${page}&venue=binance`, `${page}&venue=binance-us`]) {
      const old = await before(qs)
      expect(old.statusCode).toBe(200)
      const oldTag = String(old.headers.etag)
      const res = await after(qs, { 'if-none-match': oldTag })
      expect(res.statusCode, qs).toBe(200)
      expect(res.body, qs).toBe(old.body)
      // The same server still answers its own tag with 304.
      const own = await after(qs, { 'if-none-match': String(res.headers.etag) })
      expect(own.statusCode, qs).toBe(304)
    }
  })
})
