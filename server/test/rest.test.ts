import { describe, expect, it } from 'vitest'
import { openDb } from '../src/db/index.ts'
import { ApiError } from '../src/types.ts'
import {
  createRestClient,
  restClientFor,
  tickerWeight,
  WEIGHT,
  type RestClientOptions,
} from '../src/upstream/rest.ts'

type Handler = (url: URL, init?: RequestInit) => Response | Promise<Response>

function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', ...headers },
  })
}

function setup(handler: Handler, extra: Partial<RestClientOptions> = {}) {
  // 2026-10-21 12:00:10 UTC: 50 s left in the weight window.
  const clock = { ms: Date.UTC(2026, 9, 21, 12, 0, 10) }
  const calls: URL[] = []
  const sleeps: number[] = []
  const fetchFn = (async (input: string | URL | Request, init?: RequestInit) => {
    const url = new URL(String(input))
    calls.push(url)
    return handler(url, init)
  }) as typeof fetch
  const client = createRestClient({
    fetch: fetchFn,
    now: () => clock.ms,
    restBase: 'https://up.test/',
    sleep: async (ms) => {
      sleeps.push(ms)
      clock.ms += ms
    },
    random: () => 0.5,
    ...extra,
  })
  return { client, calls, sleeps, clock }
}

async function rejection(p: Promise<unknown>): Promise<ApiError> {
  try {
    await p
  } catch (e) {
    expect(e).toBeInstanceOf(ApiError)
    return e as ApiError
  }
  throw new Error('expected rejection')
}

const okKlines: Handler = () => json([[60000, '1', '2', '0.5', '1.5', '10']])

describe('weight accounting', () => {
  it('counts endpoint weights locally', async () => {
    const { client } = setup(okKlines)
    await client.klines({ symbol: 'BTCUSDT', interval: '1m', limit: 1000 })
    await client.klines({ symbol: 'BTCUSDT', interval: '5m', limit: 1000 })
    expect(client.state().usedWeight).toBe(2 * WEIGHT.klines)
    expect(tickerWeight(1)).toBe(2)
    expect(tickerWeight(20)).toBe(2)
    expect(tickerWeight(21)).toBe(40)
    expect(tickerWeight(101)).toBe(80)
  })

  it('resyncs from X-MBX-USED-WEIGHT-1M', async () => {
    const { client } = setup(() => json([], 200, { 'X-MBX-USED-WEIGHT-1M': '1234' }))
    await client.klines({ symbol: 'BTCUSDT', interval: '1m', limit: 1 })
    expect(client.state().usedWeight).toBe(1234)
  })

  it('ignores the header of a request sent in the previous minute', async () => {
    let clockRef: { ms: number } = { ms: 0 }
    const { client, clock } = setup(() => {
      // The response arrives after the minute boundary, reporting the old window's weight.
      clockRef.ms = Date.UTC(2026, 9, 21, 12, 1, 0, 100)
      return json([], 200, { 'X-MBX-USED-WEIGHT-1M': '4000' })
    })
    clockRef = clock
    clock.ms = Date.UTC(2026, 9, 21, 12, 0, 59, 900)
    await client.klines({ symbol: 'BTCUSDT', interval: '1m', limit: 1 })
    const st = client.state()
    expect(st.windowStartMs).toBe(Date.UTC(2026, 9, 21, 12, 1))
    expect(st.usedWeight).toBe(0)
  })

  it('an out-of-order older response cannot lower the count', async () => {
    const releases = new Map<string, () => void>()
    const { client } = setup(async (url) => {
      const limit = url.searchParams.get('limit') ?? ''
      await new Promise<void>((r) => releases.set(limit, r))
      // Request "1" was counted first by the server (lower number) but answers last.
      return json([], 200, { 'X-MBX-USED-WEIGHT-1M': limit === '1' ? '100' : '200' })
    })
    const a = client.klines({ symbol: 'BTCUSDT', interval: '1m', limit: 1 })
    const b = client.klines({ symbol: 'BTCUSDT', interval: '1m', limit: 2 })
    for (let i = 0; i < 20 && releases.size < 2; i++) await new Promise((r) => setTimeout(r, 0))
    releases.get('2')?.()
    await b
    expect(client.state().usedWeight).toBe(200 + WEIGHT.klines) // a is still in flight
    releases.get('1')?.()
    await a
    expect(client.state().usedWeight).toBe(200 + WEIGHT.klines)
  })

  it('resets the counter in a new minute', async () => {
    const { client, clock } = setup(okKlines)
    await client.klines({ symbol: 'BTCUSDT', interval: '1m', limit: 1 })
    clock.ms += 60_000
    expect(client.state().usedWeight).toBe(0)
  })

  it('fails fast with rate-limited when the window is far away', async () => {
    // limit 10 → 80 % budget = 8 → four klines calls.
    const { client, calls } = setup(okKlines, { weightLimit1m: 10 })
    for (let i = 0; i < 4; i++) {
      await client.klines({ symbol: 'BTCUSDT', interval: '1m', limit: i + 1 })
    }
    const err = await rejection(client.klines({ symbol: 'BTCUSDT', interval: '1m', limit: 99 }))
    expect(err.kind).toBe('rate-limited')
    expect(err.status).toBe(429)
    expect(err.retryAfter).toBe(50)
    expect(calls).toHaveLength(4)
  })

  it('waits for the window when it resets within 2 s', async () => {
    const { client, calls, clock, sleeps } = setup(okKlines, { weightLimit1m: 10 })
    for (let i = 0; i < 4; i++) {
      await client.klines({ symbol: 'BTCUSDT', interval: '1m', limit: i + 1 })
    }
    clock.ms = Date.UTC(2026, 9, 21, 12, 0, 59)
    await client.klines({ symbol: 'BTCUSDT', interval: '1m', limit: 99 })
    expect(sleeps).toEqual([1000])
    expect(calls).toHaveLength(5)
  })

  it('takes the weight limit from exchangeInfo', async () => {
    const { client } = setup(() =>
      json({
        rateLimits: [
          { rateLimitType: 'REQUEST_WEIGHT', interval: 'MINUTE', intervalNum: 1, limit: 1200 },
        ],
        symbols: [],
      }),
    )
    await client.exchangeInfo()
    expect(client.state().weightLimit1m).toBe(1200)
    expect(client.state().usedWeight).toBe(WEIGHT.exchangeInfo)
  })
})

describe('error mapping', () => {
  it('429 pauses every call until Retry-After', async () => {
    let status = 429
    const { client, calls, clock } = setup(() =>
      status === 429 ? json({}, 429, { 'Retry-After': '30' }) : json([]),
    )
    const err = await rejection(client.klines({ symbol: 'BTCUSDT', interval: '1m', limit: 1 }))
    expect([err.kind, err.status, err.retryAfter]).toEqual(['rate-limited', 429, 30])
    const again = await rejection(client.getJson('/api/v3/exchangeInfo', {}, 20))
    expect(again.kind).toBe('rate-limited')
    expect(again.retryAfter).toBe(30)
    expect(calls).toHaveLength(1)
    status = 200
    clock.ms += 30_000
    await client.klines({ symbol: 'BTCUSDT', interval: '1m', limit: 1 })
    expect(calls).toHaveLength(2)
  })

  it('418 enters a ban until Retry-After', async () => {
    const { client, calls, clock } = setup(() => json({}, 418, { 'Retry-After': '120' }))
    const err = await rejection(client.klines({ symbol: 'BTCUSDT', interval: '1m', limit: 1 }))
    expect([err.kind, err.status, err.retryAfter]).toEqual(['banned', 503, 120])
    clock.ms += 60_000
    const again = await rejection(client.klines({ symbol: 'ETHUSDT', interval: '1m', limit: 1 }))
    expect([again.kind, again.retryAfter]).toEqual(['banned', 60])
    expect(calls).toHaveLength(1)
  })

  it('451 → region-blocked 503', async () => {
    const { client } = setup(() => json({}, 451))
    const err = await rejection(client.klines({ symbol: 'BTCUSDT', interval: '1m', limit: 1 }))
    expect([err.kind, err.status]).toEqual(['region-blocked', 503])
  })

  it('-1121 → bad-symbol 400, never retried', async () => {
    const { client, calls } = setup(() => json({ code: -1121, msg: 'Invalid symbol.' }, 400))
    const err = await rejection(client.klines({ symbol: 'NOPE', interval: '1m', limit: 1 }))
    expect([err.kind, err.status]).toEqual(['bad-symbol', 400])
    expect(calls).toHaveLength(1)
  })

  it('other 4xx → http 502, never retried', async () => {
    const { client, calls } = setup(() => json({ code: -1100, msg: 'Illegal characters' }, 400))
    const err = await rejection(client.klines({ symbol: 'X', interval: '1m', limit: 1 }))
    expect([err.kind, err.status]).toEqual(['http', 502])
    expect(err.message).toContain('Illegal characters')
    expect(calls).toHaveLength(1)
  })

  it('retries network errors with jittered backoff, then succeeds', async () => {
    let n = 0
    const { client, calls, sleeps } = setup(() => {
      n++
      if (n < 3) throw new TypeError('fetch failed')
      return json([])
    })
    await expect(client.klines({ symbol: 'BTCUSDT', interval: '1m', limit: 1 })).resolves.toEqual(
      [],
    )
    expect(calls).toHaveLength(3)
    expect(sleeps).toEqual([250, 500])
  })

  it('gives up after 2 retries with network 502', async () => {
    const { client, calls } = setup(() => {
      throw new TypeError('fetch failed')
    })
    const err = await rejection(client.klines({ symbol: 'BTCUSDT', interval: '1m', limit: 1 }))
    expect([err.kind, err.status]).toEqual(['network', 502])
    expect(calls).toHaveLength(3)
  })

  it('retries 5xx, then reports http 502', async () => {
    const { client, calls } = setup(() => json({}, 503))
    const err = await rejection(client.klines({ symbol: 'BTCUSDT', interval: '1m', limit: 1 }))
    expect([err.kind, err.status]).toEqual(['http', 502])
    expect(calls).toHaveLength(3)
  })

  it('times out a hung request as network', async () => {
    const { client, calls } = setup(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(new Error('aborted')))
        }),
      { timeoutMs: 5 },
    )
    const err = await rejection(client.klines({ symbol: 'BTCUSDT', interval: '1m', limit: 1 }))
    expect(err.kind).toBe('network')
    expect(err.message).toMatch(/Timed out/)
    expect(calls).toHaveLength(3)
  })

  it('malformed JSON → parse 502; wrong shape → parse 502', async () => {
    const bad = setup(() => new Response('<html>', { status: 200 }))
    const e1 = await rejection(bad.client.klines({ symbol: 'BTCUSDT', interval: '1m', limit: 1 }))
    expect([e1.kind, e1.status]).toEqual(['parse', 502])
    expect(bad.calls).toHaveLength(1)
    const shape = setup(() => json({ not: 'an array' }))
    const e2 = await rejection(shape.client.klines({ symbol: 'BTCUSDT', interval: '1m', limit: 1 }))
    expect(e2.kind).toBe('parse')
  })
})

describe('concurrency', () => {
  it('coalesces identical concurrent GETs', async () => {
    let release: () => void = () => {}
    const gate = new Promise<void>((r) => (release = r))
    const { client, calls } = setup(async () => {
      await gate
      return json([[60000, '1', '1', '1', '1', '1']])
    })
    const q = { symbol: 'BTCUSDT', interval: '1m' as const, limit: 5 }
    const a = client.klines(q)
    const b = client.klines(q)
    const c = client.klines({ ...q, limit: 6 })
    release()
    const [ra, rb] = await Promise.all([a, b, c])
    expect(ra).toEqual(rb)
    expect(calls).toHaveLength(2)
    // Settled requests are not cached: a later identical call goes upstream.
    await client.klines(q)
    expect(calls).toHaveLength(3)
  })

  it('caps in-flight requests at 4', async () => {
    let active = 0
    let peak = 0
    const releases: (() => void)[] = []
    const { client, calls } = setup(async () => {
      active++
      peak = Math.max(peak, active)
      await new Promise<void>((r) => releases.push(r))
      active--
      return json([])
    })
    const ps = Array.from({ length: 7 }, (_, i) =>
      client.klines({ symbol: 'BTCUSDT', interval: '1m', limit: i + 1 }),
    )
    for (let i = 0; i < 20 && calls.length < 7; i++) {
      await new Promise((r) => setTimeout(r, 0))
      expect(client.state().inFlight).toBeLessThanOrEqual(4)
      releases.splice(0).forEach((r) => r())
    }
    await Promise.all(ps)
    expect(peak).toBe(4)
    expect(calls).toHaveLength(7)
    expect(client.state().inFlight).toBe(0)
  })

  it('rejects with rate-limited 429 when the slot queue is full', async () => {
    const releases: (() => void)[] = []
    const { client, calls } = setup(
      async () => {
        await new Promise<void>((r) => releases.push(r))
        return json([])
      },
      { maxInFlight: 1, maxQueue: 2 },
    )
    const ps = [1, 2, 3].map((i) => client.klines({ symbol: 'BTCUSDT', interval: '1m', limit: i }))
    const overflow = client.klines({ symbol: 'BTCUSDT', interval: '1m', limit: 4 })
    const err = await rejection(overflow)
    expect(err.kind).toBe('rate-limited')
    expect(err.status).toBe(429)
    expect(err.retryAfter).toBe(1)
    for (let i = 0; i < 20 && calls.length < 3; i++) {
      await new Promise((r) => setTimeout(r, 0))
      releases.splice(0).forEach((r) => r())
    }
    await Promise.all(ps)
    expect(calls).toHaveLength(3)
    expect(client.state()).toMatchObject({ inFlight: 0, queued: 0 })
  })

  it('rejects a queued request with rate-limited 429 after the queue timeout', async () => {
    let release: () => void = () => {}
    const { client, calls } = setup(
      async () => {
        await new Promise<void>((r) => (release = r))
        return json([])
      },
      { maxInFlight: 1, queueTimeoutMs: 20 },
    )
    const first = client.klines({ symbol: 'BTCUSDT', interval: '1m', limit: 1 })
    const queued = client.klines({ symbol: 'BTCUSDT', interval: '1m', limit: 2 })
    const err = await rejection(queued)
    expect(err.kind).toBe('rate-limited')
    expect(err.status).toBe(429)
    expect(err.retryAfter).toBe(1)
    expect(client.state().queued).toBe(0)
    release()
    await first
    expect(calls).toHaveLength(1)
    expect(client.state().inFlight).toBe(0)
  })

  it('sends tickers as a symbols JSON param and no API key headers', async () => {
    let seenHeaders: RequestInit['headers']
    const { client, calls } = setup((_u, init) => {
      seenHeaders = init?.headers
      return json([])
    })
    await client.tickers24hr(['BTCUSDT', 'ETHUSDT'])
    expect(calls[0]?.pathname).toBe('/api/v3/ticker/24hr')
    expect(calls[0]?.searchParams.get('symbols')).toBe('["BTCUSDT","ETHUSDT"]')
    expect(seenHeaders).toBeUndefined()
    expect(await client.tickers24hr([])).toEqual([])
    expect(calls).toHaveLength(1)
  })
})

describe('restClientFor', () => {
  it('shares one client per app db', () => {
    const db = openDb(':memory:')
    const f = (() => Promise.reject(new Error('x'))) as typeof fetch
    const now = () => 0
    const a = restClientFor({ db, fetch: f, now, restBase: 'https://up.test' })
    const b = restClientFor({ db, fetch: f, now, restBase: 'https://up.test' })
    expect(a).toBe(b)
    const other = restClientFor({
      db: openDb(':memory:'),
      fetch: f,
      now,
      restBase: 'https://up.test',
    })
    expect(other).not.toBe(a)
    db.close()
  })
})
