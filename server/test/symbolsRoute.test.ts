import type { FastifyInstance } from 'fastify'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { buildApp } from '../src/app.ts'
import { openDb, type Db } from '../src/db/index.ts'
import type { SymbolInfo, Ticker } from '../src/types.ts'

const SYMBOL = (symbol: string, base: string) => ({
  symbol,
  status: 'TRADING',
  baseAsset: base,
  quoteAsset: 'USDT',
  filters: [{ filterType: 'PRICE_FILTER', tickSize: '0.01000000' }],
})

const EXCHANGE_INFO = {
  rateLimits: [
    { rateLimitType: 'REQUEST_WEIGHT', interval: 'MINUTE', intervalNum: 1, limit: 6000 },
  ],
  symbols: [SYMBOL('BTCUSDT', 'BTC'), SYMBOL('ETHUSDT', 'ETH')],
}

const TICKER = (symbol: string, last: string, open: string) => ({
  symbol,
  lastPrice: last,
  openPrice: open,
  highPrice: '200',
  lowPrice: '50',
  volume: '7',
  quoteVolume: '700',
  closeTime: 1792972799999,
})

const DEFAULT_LISTED = ['BTCUSDT', 'ETHUSDT']
const TICKERS: Record<string, ReturnType<typeof TICKER>> = {
  BTCUSDT: TICKER('BTCUSDT', '110', '100'),
  ETHUSDT: TICKER('ETHUSDT', '90', '0'),
}

describe('symbols and tickers routes', () => {
  let db: Db
  let app: FastifyInstance
  let calls: URL[]
  let down: boolean
  /** Symbols the fake upstream accepts in ticker/24hr. */
  let listed: Set<string>
  /** Extra exchangeInfo entries for a test. */
  let extraSymbols: ReturnType<typeof SYMBOL>[]
  const clock = { ms: Date.UTC(2026, 9, 21, 12) }

  beforeEach(async () => {
    db = openDb(':memory:')
    calls = []
    down = false
    listed = new Set(DEFAULT_LISTED)
    extraSymbols = []
    clock.ms = Date.UTC(2026, 9, 21, 12)
    const fakeFetch = (async (input: string | URL | Request) => {
      const url = new URL(String(input))
      calls.push(url)
      if (down) throw new TypeError('fetch failed')
      if (url.pathname === '/api/v3/exchangeInfo') {
        return Response.json({
          ...EXCHANGE_INFO,
          symbols: [...EXCHANGE_INFO.symbols, ...extraSymbols],
        })
      }
      if (url.pathname === '/api/v3/ticker/24hr') {
        const list = JSON.parse(url.searchParams.get('symbols') ?? '[]') as string[]
        if (list.some((s) => !listed.has(s))) {
          return Response.json({ code: -1121, msg: 'Invalid symbol.' }, { status: 400 })
        }
        return Response.json(list.map((s) => TICKERS[s] ?? TICKER(s, '1', '1')))
      }
      return new Response('nope', { status: 404 })
    }) as typeof fetch
    app = await buildApp({ db, fetch: fakeFetch, now: () => clock.ms, restBase: 'https://up.test' })
    await app.ready()
  })

  afterEach(async () => {
    await app.close()
    db.close()
  })

  const get = (url: string) => app.inject({ method: 'GET', url })
  const exchangeInfoCalls = () => calls.filter((u) => u.pathname === '/api/v3/exchangeInfo').length

  describe('GET /api/symbols', () => {
    it('maps exchangeInfo and caches it in kv_cache', async () => {
      const res = await get('/api/symbols')
      expect(res.statusCode).toBe(200)
      expect(res.headers['content-type']).toMatch(/application\/json/)
      expect(res.json<SymbolInfo[]>()).toEqual([
        {
          symbol: 'BTCUSDT',
          baseAsset: 'BTC',
          quoteAsset: 'USDT',
          status: 'TRADING',
          tickSize: 0.01,
        },
        {
          symbol: 'ETHUSDT',
          baseAsset: 'ETH',
          quoteAsset: 'USDT',
          status: 'TRADING',
          tickSize: 0.01,
        },
      ])
      const row = db.prepare("SELECT updated_at FROM kv_cache WHERE key = 'symbols:v1'").get() as {
        updated_at: number
      }
      expect(row.updated_at).toBe(clock.ms / 1000)
    })

    it('serves from cache within the 1 h TTL and refreshes after it', async () => {
      await get('/api/symbols')
      clock.ms += 59 * 60_000
      await get('/api/symbols')
      expect(exchangeInfoCalls()).toBe(1)
      clock.ms += 60_000
      await get('/api/symbols')
      expect(exchangeInfoCalls()).toBe(2)
    })

    it('serves the stale copy when a refresh fails', async () => {
      const first = await get('/api/symbols')
      clock.ms += 2 * 3600_000
      down = true
      const res = await get('/api/symbols')
      expect(res.statusCode).toBe(200)
      expect(res.json()).toEqual(first.json())
    })

    it('fails with network 502 when there is no copy at all', async () => {
      down = true
      const res = await get('/api/symbols')
      expect(res.statusCode).toBe(502)
      expect(res.json()).toMatchObject({ error: 'network' })
    })

    it('coalesces concurrent refreshes', async () => {
      await Promise.all([get('/api/symbols'), get('/api/symbols'), get('/api/symbols')])
      expect(exchangeInfoCalls()).toBe(1)
    })
  })

  describe('GET /api/tickers', () => {
    it('empty or missing list → [] without an upstream call', async () => {
      for (const url of ['/api/tickers', '/api/tickers?symbols=', '/api/tickers?symbols=,,']) {
        const res = await get(url)
        expect(res.statusCode).toBe(200)
        expect(res.json()).toEqual([])
      }
      expect(calls).toHaveLength(0)
    })

    it('normalises, dedupes, maps, and keeps input order', async () => {
      const res = await get('/api/tickers?symbols=ethusdt,%20BTCUSDT%20,ETHUSDT')
      expect(res.statusCode).toBe(200)
      const body = res.json<Ticker[]>()
      expect(body.map((t) => t.symbol)).toEqual(['ETHUSDT', 'BTCUSDT'])
      expect(body[1]).toEqual({
        symbol: 'BTCUSDT',
        last: 110,
        open: 100,
        high: 200,
        low: 50,
        volume: 7,
        quoteVolume: 700,
        changePct: 10,
        time: 1792972799,
      })
      expect(body[0]?.changePct).toBe(0) // open 0
      expect(calls).toHaveLength(1)
      expect(calls[0]?.searchParams.get('symbols')).toBe('["ETHUSDT","BTCUSDT"]')
    })

    it('omits unknown symbols using the cached symbol list', async () => {
      const res = await get('/api/tickers?symbols=BTCUSDT,NOPEUSDT,ETHUSDT')
      expect(res.statusCode).toBe(200)
      expect(res.json<Ticker[]>().map((t) => t.symbol)).toEqual(['BTCUSDT', 'ETHUSDT'])
      const tickerCalls = calls.filter((u) => u.pathname === '/api/v3/ticker/24hr')
      expect(tickerCalls.map((u) => u.searchParams.get('symbols'))).toEqual([
        '["BTCUSDT","NOPEUSDT","ETHUSDT"]',
        '["BTCUSDT","ETHUSDT"]',
      ])
    })

    const tickerCalls = () =>
      calls
        .filter((u) => u.pathname === '/api/v3/ticker/24hr')
        .map((u) => u.searchParams.get('symbols'))

    it('drops symbols that are not TRADING in the stored list before the batch call', async () => {
      extraSymbols = [{ ...SYMBOL('HALTUSDT', 'HALT'), status: 'BREAK' }]
      await get('/api/symbols')
      calls = []
      const res = await get('/api/tickers?symbols=HALTUSDT,BTCUSDT,NOPEUSDT')
      expect(res.statusCode).toBe(200)
      expect(res.json<Ticker[]>().map((t) => t.symbol)).toEqual(['BTCUSDT'])
      expect(tickerCalls()).toEqual(['["BTCUSDT"]'])
    })

    it('remembers symbols rejected one by one for 10 min and skips them', async () => {
      // GHOSTUSDT is TRADING in exchangeInfo but upstream rejects it with -1121.
      extraSymbols = [SYMBOL('GHOSTUSDT', 'GHOST')]
      const first = await get('/api/tickers?symbols=BTCUSDT,GHOSTUSDT')
      expect(first.json<Ticker[]>().map((t) => t.symbol)).toEqual(['BTCUSDT'])
      expect(tickerCalls()).toEqual([
        '["BTCUSDT","GHOSTUSDT"]',
        '["BTCUSDT","GHOSTUSDT"]',
        '["BTCUSDT"]',
        '["GHOSTUSDT"]',
      ])
      calls = []
      clock.ms += 9 * 60_000
      const second = await get('/api/tickers?symbols=BTCUSDT,GHOSTUSDT')
      expect(second.json<Ticker[]>().map((t) => t.symbol)).toEqual(['BTCUSDT'])
      expect(tickerCalls()).toEqual(['["BTCUSDT"]'])
      // After the TTL the symbol is asked about again.
      calls = []
      clock.ms += 2 * 60_000
      await get('/api/tickers?symbols=BTCUSDT,GHOSTUSDT')
      expect(tickerCalls()[0]).toBe('["BTCUSDT","GHOSTUSDT"]')
    })

    it('caps the single-symbol fallback at 20 upstream calls', async () => {
      const names = Array.from({ length: 30 }, (_, i) => `S${i}USDT`)
      extraSymbols = names.map((n) => SYMBOL(n, n.slice(0, -4)))
      for (const n of names.slice(0, 29)) listed.add(n)
      // S29USDT is TRADING in exchangeInfo but rejected upstream, so batches fail.
      const res = await get(`/api/tickers?symbols=S29USDT,${names.slice(0, 29).join(',')}`)
      expect(res.statusCode).toBe(200)
      const singles = tickerCalls().filter((q) => q !== null && JSON.parse(q).length === 1)
      expect(singles).toHaveLength(20)
      expect(singles[0]).toBe('["S29USDT"]')
      expect(res.json<Ticker[]>()).toHaveLength(19)
    })

    it('only unknown symbols → []', async () => {
      const res = await get('/api/tickers?symbols=NOPEUSDT')
      expect(res.json()).toEqual([])
    })

    it('drops invalid syntax locally', async () => {
      const res = await get('/api/tickers?symbols=BTC-USD,x')
      expect(res.json()).toEqual([])
      expect(calls).toHaveLength(0)
    })

    it('rejects more than 200 symbols', async () => {
      const many = Array.from({ length: 201 }, (_, i) => `S${i}USDT`).join(',')
      const res = await get(`/api/tickers?symbols=${many}`)
      expect(res.statusCode).toBe(400)
      expect(res.json()).toMatchObject({ error: 'http' })
    })

    it('shares the rate limiter with candles (one pause for both)', async () => {
      // A 418 on tickers bans candles too.
      let banCalls = 0
      const banApp = await buildApp({
        db: openDb(':memory:'),
        fetch: (async () => {
          banCalls++
          return new Response('{}', { status: 418, headers: { 'Retry-After': '60' } })
        }) as typeof fetch,
        now: () => clock.ms,
      })
      const first = await banApp.inject({ method: 'GET', url: '/api/tickers?symbols=BTCUSDT' })
      expect(first.statusCode).toBe(503)
      const second = await banApp.inject({
        method: 'GET',
        url: '/api/candles?symbol=BTCUSDT&interval=1h&limit=2',
      })
      expect(second.statusCode).toBe(503)
      expect(second.json()).toMatchObject({ error: 'banned' })
      expect(banCalls).toBe(1)
      await banApp.close()
    })
  })
})
