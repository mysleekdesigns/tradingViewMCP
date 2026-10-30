import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Candle } from '../types'
import {
  aggregateCandles,
  bucketStart,
  buildCatalog,
  COINBASE_EARLIEST_S,
  CoinbaseProvider,
  pageEndParam,
  parseCoinbaseCandle,
  parseProduct,
  parseStats,
} from './coinbase'
import { MarketDataError } from './provider'
import { COINBASE_VENUE } from './venue'

/** 2026-10-25T00:00:00Z, a Sunday. */
const T = 1_792_886_400
const H = 3600
const D = 86_400

/** /products entries in the shape observed live (2026-10-25), trimmed to the fields we read. */
const product = (id: string, base: string, quote: string, extra: Record<string, unknown> = {}) => ({
  id,
  base_currency: base,
  quote_currency: quote,
  quote_increment: '0.01',
  base_increment: '0.00000001',
  display_name: id,
  status: 'online',
  trading_disabled: false,
  cancel_only: false,
  ...extra,
})
const PRODUCTS = [
  product('BTC-USD', 'BTC', 'USD'),
  product('ETH-USD', 'ETH', 'USD', { quote_increment: '0.001' }),
  product('BTC-USDT', 'BTC', 'USDT'),
  product('SOL-USD', 'SOL', 'USD'),
  product('ADA-USD', 'ADA', 'USD'),
  product('OLD-USD', 'OLD', 'USD', { status: 'delisted', trading_disabled: true }),
]

/** Synthetic candle value for time t: hours since T + 1000. */
const f = (t: number) => (t - T) / H + 1000
/** Coinbase row `[time, low, high, open, close, volume]` for t. */
const row = (t: number) => [t, f(t) - 1, f(t) + 2, f(t), f(t) + 1, 1]

function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers })
}

interface Routes {
  products?: () => Response
  /** Override for candle requests; default serves synthetic rows newest first. */
  candles?: (url: URL) => Response
  omit?: ReadonlySet<number>
  stats?: (id: string) => Response
  allStats?: () => Response
}

function fakeFetch(routes: Routes = {}) {
  const urls: string[] = []
  const fn = vi.fn((input: RequestInfo | URL) => {
    const u = new URL(String(input))
    urls.push(u.toString())
    if (u.pathname === '/products') return Promise.resolve(routes.products?.() ?? json(PRODUCTS))
    if (u.pathname === '/products/stats') {
      return Promise.resolve(routes.allStats?.() ?? json({}))
    }
    const stats = /^\/products\/([^/]+)\/stats$/.exec(u.pathname)
    if (stats) return Promise.resolve(routes.stats?.(decodeURIComponent(stats[1])) ?? json({}))
    if (/\/candles$/.test(u.pathname)) {
      if (routes.candles) return Promise.resolve(routes.candles(u))
      const g = Number(u.searchParams.get('granularity'))
      const start = Date.parse(u.searchParams.get('start') ?? '') / 1000
      const end = Date.parse(u.searchParams.get('end') ?? '') / 1000
      const rows: unknown[] = []
      // Like Coinbase: aligned start, inclusive end (an unaligned end includes its bucket).
      for (let t = start; t <= end; t += g) if (!routes.omit?.has(t)) rows.unshift(row(t))
      return Promise.resolve(json(rows))
    }
    return Promise.resolve(json({ message: 'NotFound' }, 404))
  })
  return { fn: fn as unknown as typeof fetch, urls, calls: fn }
}

/** Default provider clock: 14:00:30 UTC on T. */
const NOW = T + 14 * H + 30

function provider(routes: Routes = {}, nowS = NOW) {
  const fetch = fakeFetch(routes)
  const p = new CoinbaseProvider({
    fetchFn: fetch.fn,
    minRequestIntervalMs: 0,
    now: () => nowS * 1000,
  })
  return { p, ...fetch }
}

const candleUrls = (urls: string[]) =>
  urls
    .map((u) => new URL(u))
    .filter((u) => u.pathname.endsWith('/candles'))
    .map((u) => ({
      path: u.pathname,
      granularity: u.searchParams.get('granularity'),
      start: u.searchParams.get('start'),
      end: u.searchParams.get('end'),
    }))

const iso = (t: number) => new Date(t * 1000).toISOString().replace('.000Z', 'Z')

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('parseCoinbaseCandle', () => {
  it('maps [time, low, high, open, close, volume] (low/high before open)', () => {
    // A real BTC-USD 1h row (2026-10-25).
    expect(
      parseCoinbaseCandle([1792936800, 80179.66, 80311.8, 80266.06, 80266.2, 37.42966619]),
    ).toEqual({
      time: 1792936800,
      open: 80266.06,
      high: 80311.8,
      low: 80179.66,
      close: 80266.2,
      volume: 37.42966619,
    })
  })
  it('rejects short or non-numeric rows with a parse error', () => {
    expect(() => parseCoinbaseCandle([1, 2, 3])).toThrow(MarketDataError)
    expect(() => parseCoinbaseCandle([1, 2, 3, 'x', 5, 6])).toThrow(/open/)
    expect(() => parseCoinbaseCandle({})).toThrow(MarketDataError)
  })
})

describe('bucketStart', () => {
  it('buckets 4h on the UTC 00/04/08/... grid', () => {
    expect(bucketStart(T + 5 * H + 1800, '4h')).toBe(T + 4 * H)
    expect(bucketStart(T + 4 * H, '4h')).toBe(T + 4 * H)
    expect(bucketStart(T + 3 * H + 3599, '4h')).toBe(T)
  })
  it('buckets 1w on Monday 00:00 UTC, like Binance', () => {
    // 2024-01-01 was a Monday (Binance 1w open 1704067200000 ms).
    expect(bucketStart(1_704_067_200 + 3 * D + 5, '1w')).toBe(1_704_067_200)
    expect(bucketStart(1_704_067_200 - 1, '1w')).toBe(1_704_067_200 - 7 * D)
    const monday = bucketStart(T, '1w') // T is a Sunday
    expect(monday).toBe(T - 6 * D)
    expect(new Date(monday * 1000).getUTCDay()).toBe(1)
  })
  it('leaves native granularities on their own grid', () => {
    expect(bucketStart(T + 61, '1m')).toBe(T + 60)
    expect(bucketStart(T + 899, '15m')).toBe(T)
    expect(bucketStart(T + D - 1, '1d')).toBe(T)
  })
})

describe('aggregateCandles', () => {
  const c = (time: number, o: number, h: number, l: number, cl: number, v: number): Candle => ({
    time,
    open: o,
    high: h,
    low: l,
    close: cl,
    volume: v,
  })

  it('builds 4h bars from 1h: first open, max high, min low, last close, summed volume', () => {
    const hours = [
      c(T, 10, 12, 9, 11, 1),
      c(T + H, 11, 15, 10, 14, 2),
      c(T + 2 * H, 14, 14, 8, 9, 3),
      c(T + 3 * H, 9, 10, 9, 10, 4),
      c(T + 4 * H, 10, 11, 10, 11, 5),
    ]
    expect(aggregateCandles(hours, '4h')).toEqual([
      c(T, 10, 15, 8, 10, 10),
      c(T + 4 * H, 10, 11, 10, 11, 5),
    ])
  })

  it('builds 1w bars from 1d on Monday boundaries', () => {
    const monday = T - 6 * D
    const days = Array.from({ length: 9 }, (_, i) =>
      c(monday - 2 * D + i * D, 100 + i, 110 + i, 90 + i, 101 + i, 1),
    )
    // Sat+Sun of the prior week, then Mon..Sun of this week.
    expect(aggregateCandles(days, '1w')).toEqual([
      c(monday - 7 * D, 100, 111, 90, 102, 2),
      c(monday, 102, 118, 92, 109, 7),
    ])
  })

  it('does not forward-fill: missing hours are skipped and empty buckets omitted', () => {
    const hours = [
      c(T + H, 11, 15, 10, 14, 2),
      c(T + 3 * H, 9, 10, 9, 10, 4),
      c(T + 8 * H, 1, 1, 1, 1, 1),
    ]
    // Bucket T has no first hour (no trades then): it opens at the first traded hour.
    // Bucket T+4h has no components at all: no bar.
    expect(aggregateCandles(hours, '4h')).toEqual([
      c(T, 11, 15, 9, 10, 6),
      c(T + 8 * H, 1, 1, 1, 1, 1),
    ])
  })

  it('drops a bucket that starts before the requested window (it could be truncated)', () => {
    const hours = [c(T + 3 * H, 9, 10, 9, 10, 4), c(T + 4 * H, 10, 11, 10, 11, 5)]
    expect(aggregateCandles(hours, '4h', T + 4 * H)).toEqual([c(T + 4 * H, 10, 11, 10, 11, 5)])
  })
})

describe('products and symbols', () => {
  it('exposes BTC-USD as BTCUSD with base/quote, TRADING status and quote_increment tick', () => {
    expect(parseProduct(product('BTC-USD', 'BTC', 'USD'))).toEqual({
      productId: 'BTC-USD',
      info: {
        symbol: 'BTCUSD',
        baseAsset: 'BTC',
        quoteAsset: 'USD',
        status: 'TRADING',
        tickSize: 0.01,
      },
    })
  })
  it('maps delisted and disabled products to non-TRADING statuses', () => {
    expect(parseProduct(PRODUCTS[5])?.info.status).toBe('DELISTED')
    expect(
      parseProduct(product('X-USD', 'X1', 'USD', { trading_disabled: true }))?.info.status,
    ).toBe('HALT')
    expect(parseProduct(product('X-USD', 'X1', 'USD', { cancel_only: true }))?.info.status).toBe(
      'HALT',
    )
  })
  it('skips malformed products and symbols outside the app format', () => {
    expect(parseProduct({ id: 'BTC-USD' })).toBeNull()
    expect(parseProduct(product('BTC-USD', 'BTC', 'USD', { quote_increment: '0' }))).toBeNull()
    expect(parseProduct(product('B.T-USD', 'B.T', 'USD'))).toBeNull()
    expect(parseProduct(null)).toBeNull()
  })
  it('builds both maps from base/quote, never by splitting the id', () => {
    const cat = buildCatalog([product('WEIRD_ID', 'BTC', 'EUR'), 'junk'])
    expect(cat.symbols.map((s) => s.symbol)).toEqual(['BTCEUR'])
    expect(cat.toProduct.get('BTCEUR')).toBe('WEIRD_ID')
    expect(cat.toSymbol.get('WEIRD_ID')).toBe('BTCEUR')
  })
  it('resolves a dash-collision to the one TRADING product, else leaves it out', () => {
    const a = product('AB-CUSD', 'AB', 'CUSD', { status: 'delisted' })
    const b = product('ABC-USD', 'ABC', 'USD')
    const one = buildCatalog([a, b])
    expect(one.toProduct.get('ABCUSD')).toBe('ABC-USD')
    expect(one.symbols).toHaveLength(1)
    const both = buildCatalog([product('AB-CUSD', 'AB', 'CUSD'), b])
    expect(both.toProduct.has('ABCUSD')).toBe(false)
    expect(both.symbols).toEqual([])
  })
  it('getSymbols fetches /products once and caches it', async () => {
    const { p, urls } = provider()
    const [a, b] = await Promise.all([p.getSymbols(), p.getSymbols()])
    expect(a).toBe(b)
    expect(a.map((s) => s.symbol)).toEqual([
      'BTCUSD',
      'ETHUSD',
      'BTCUSDT',
      'SOLUSD',
      'ADAUSD',
      'OLDUSD',
    ])
    expect(a[1].tickSize).toBe(0.001)
    expect(urls).toEqual(['https://api.exchange.coinbase.com/products'])
  })
  it('does not cache a failed product fetch', async () => {
    let n = 0
    const { p } = provider({
      products: () => (++n === 1 ? json({ message: 'oops' }, 500) : json(PRODUCTS)),
    })
    await expect(p.getSymbols()).rejects.toMatchObject({ kind: 'http', status: 500 })
    await expect(p.getSymbols()).resolves.toHaveLength(6)
  })
  it('getVenue reports Coinbase', async () => {
    await expect(provider().p.getVenue()).resolves.toEqual(COINBASE_VENUE)
  })
})

describe('pageEndParam', () => {
  it('recent pages get an unaligned end inside the same bucket (CDN cache miss)', () => {
    const e = pageEndParam(T + 14 * H, H, NOW)
    expect(e).toBeGreaterThan(T + 14 * H)
    expect(e).toBeLessThan(T + 15 * H)
    expect(pageEndParam(T + 14 * H, H, NOW + 1)).not.toBe(e)
    const m = pageEndParam(T, 60, T + 61)
    expect(m % 60).not.toBe(0)
    expect(Math.floor(m / 60) * 60).toBe(T)
  })
  it('history older than the cache lifetime keeps the aligned, cacheable end', () => {
    expect(pageEndParam(T, H, T + H + 301)).toBe(T)
    expect(pageEndParam(T, 60, T + 60 + 300)).not.toBe(T)
  })
})

describe('CoinbaseProvider.getCandles', () => {
  it('requests the mapped product with aligned ISO start/end and returns ascending bars', async () => {
    const { p, urls } = provider()
    const bars = await p.getCandles('btcusd', '1h', 3)
    expect(candleUrls(urls)).toEqual([
      {
        path: '/products/BTC-USD/candles',
        granularity: '3600',
        start: iso(T + 12 * H),
        end: iso(pageEndParam(T + 14 * H, H, NOW)),
      },
    ])
    expect(bars.map((b) => b.time)).toEqual([T + 12 * H, T + 13 * H, T + 14 * H])
    expect(bars[0]).toEqual({
      time: T + 12 * H,
      open: 1012,
      high: 1014,
      low: 1011,
      close: 1013,
      volume: 1,
    })
  })

  it('sorts newest-first rows ascending and removes duplicate times', async () => {
    const { p } = provider({
      candles: () => json([row(T + 14 * H), row(T + 13 * H), row(T + 14 * H), row(T + 12 * H)]),
    })
    const bars = await p.getCandles('BTCUSD', '1h', 3)
    expect(bars.map((b) => b.time)).toEqual([T + 12 * H, T + 13 * H, T + 14 * H])
  })

  it('paginates in pages of at most 300 rows up to endTime', async () => {
    const { p, urls } = provider()
    const end = T + 10 * H
    const bars = await p.getCandles('BTCUSD', '1m', 500, end)
    const first = end - 499 * 60
    expect(
      candleUrls(urls).map(({ start, end: e, granularity }) => [granularity, start, e]),
    ).toEqual([
      ['60', iso(first), iso(first + 299 * 60)],
      ['60', iso(first + 300 * 60), iso(end)],
    ])
    expect(bars).toHaveLength(500)
    expect(bars[0].time).toBe(first)
    expect(bars[499].time).toBe(end)
    expect(bars.every((b, i) => i === 0 || b.time - bars[i - 1].time === 60)).toBe(true)
  })

  it('treats endTime like Binance: the bar containing endTime is the last one', async () => {
    const { p } = provider()
    const bars = await p.getCandles('BTCUSD', '1h', 2, T + 5 * H + 1234)
    expect(bars.map((b) => b.time)).toEqual([T + 4 * H, T + 5 * H])
  })

  it('returns fewer bars when Coinbase omits empty slots (no forward fill)', async () => {
    const { p } = provider({ omit: new Set([T + 13 * H]) })
    const bars = await p.getCandles('BTCUSD', '1h', 3)
    expect(bars.map((b) => b.time)).toEqual([T + 12 * H, T + 14 * H])
  })

  it('aggregates 4h from bucket-aligned 1h rows; the newest bucket is the forming one', async () => {
    const { p, urls } = provider()
    const bars = await p.getCandles('BTCUSD', '4h', 2)
    expect(candleUrls(urls)).toEqual([
      {
        path: '/products/BTC-USD/candles',
        granularity: '3600',
        start: iso(T + 8 * H),
        end: iso(pageEndParam(T + 14 * H, H, NOW)),
      },
    ])
    expect(bars).toEqual([
      { time: T + 8 * H, open: 1008, high: 1013, low: 1007, close: 1012, volume: 4 },
      { time: T + 12 * H, open: 1012, high: 1016, low: 1011, close: 1015, volume: 3 },
    ])
  })

  it('aggregates 1w from 1d rows starting on Monday', async () => {
    const { p, urls } = provider()
    const bars = await p.getCandles('BTCUSD', '1w', 2)
    const monday = T - 6 * D
    expect(candleUrls(urls)).toEqual([
      {
        path: '/products/BTC-USD/candles',
        granularity: '86400',
        start: iso(monday - 7 * D),
        end: iso(pageEndParam(T, D, NOW)),
      },
    ])
    expect(bars.map((b) => [b.time, b.open, b.close, b.volume])).toEqual([
      [monday - 7 * D, f(monday - 7 * D), f(monday - D) + 1, 7],
      [monday, f(monday), f(T) + 1, 7],
    ])
  })

  it('a 4h history page never starts with a truncated bucket', async () => {
    // endTime mid-bucket: the window still covers whole buckets.
    const { p, urls } = provider()
    const bars = await p.getCandles('BTCUSD', '4h', 3, T + 9 * H + 5)
    expect(candleUrls(urls)[0].start).toBe(iso(T))
    expect(candleUrls(urls)[0].end).toBe(iso(T + 11 * H))
    expect(bars.map((b) => [b.time, b.volume])).toEqual([
      [T, 4],
      [T + 4 * H, 4],
      [T + 8 * H, 4],
    ])
  })
})

describe('CoinbaseProvider.getCandles on illiquid pairs (empty windows)', () => {
  /** Candle route: synthetic rows only for times in [first, last] (Coinbase omits the rest). */
  const sparse =
    (last: number, first = -Infinity) =>
    (u: URL): Response => {
      const g = Number(u.searchParams.get('granularity'))
      const start = Date.parse(u.searchParams.get('start') ?? '') / 1000
      const end = Date.parse(u.searchParams.get('end') ?? '') / 1000
      const rows: unknown[] = []
      for (let t = start; t <= end; t += g) if (t <= last && t >= first) rows.unshift(row(t))
      return json(rows)
    }

  it('an empty older window steps back to the newest traded bars instead of []', async () => {
    const lastTrade = T - 40 * D + 5 * H
    const { p, urls } = provider({ candles: sparse(lastTrade) })
    const bars = await p.getCandles('BTCUSD', '1h', 3, T + 5 * H)
    expect(bars.map((b) => b.time)).toEqual([lastTrade - 2 * H, lastTrade - H, lastTrade])
    const reqs = candleUrls(urls).map((r) => [r.granularity, r.start])
    expect(reqs).toEqual([
      ['3600', iso(T + 3 * H)], // the requested window: empty
      ['3600', iso(T)], // rest of the UTC day: empty
      ['86400', iso(T - 300 * D)], // daily probe: newest traded day is T - 40d
      ['3600', iso(T - 40 * D)], // that day, hourly
    ])
  })

  it('1m: pages to the day start, probes, then pages inside the traded day', async () => {
    const lastTrade = T - 2 * D + 100 * 60
    const { p, urls } = provider({ candles: sparse(lastTrade) })
    const bars = await p.getCandles('BTCUSD', '1m', 3, T + 14 * H)
    expect(bars.map((b) => b.time)).toEqual([lastTrade - 120, lastTrade - 60, lastTrade])
    const reqs = candleUrls(urls)
    expect(reqs.length).toBeLessThanOrEqual(1 + 12)
    // Every request stays within 300 rows.
    for (const r of reqs) {
      const n = (Date.parse(r.end ?? '') - Date.parse(r.start ?? '')) / 1000 / Number(r.granularity)
      expect(n).toBeLessThan(301)
    }
  })

  it('4h aggregates from whole buckets while stepping back', async () => {
    const lastTrade = T - 10 * D + 9 * H
    const { p } = provider({ candles: sparse(lastTrade) })
    const bars = await p.getCandles('BTCUSD', '4h', 2, T + 13 * H)
    expect(bars.map((b) => [b.time, b.volume])).toEqual([
      [T - 10 * D + 4 * H, 4],
      [T - 10 * D + 8 * H, 2],
    ])
  })

  it('1d pages back 300 days per request', async () => {
    const lastTrade = T - 400 * D
    const { p, urls } = provider({ candles: sparse(lastTrade) })
    const bars = await p.getCandles('BTCUSD', '1d', 2, T)
    expect(bars.map((b) => b.time)).toEqual([lastTrade - D, lastTrade])
    expect(candleUrls(urls).map((r) => r.start)).toEqual([
      iso(T - D),
      iso(T - D - 300 * D),
      iso(T - D - 600 * D),
    ])
  })

  it('no older data at all: [] after a bounded number of requests', async () => {
    const { p, urls } = provider({ candles: () => json([]) })
    await expect(p.getCandles('BTCUSD', '1h', 3, T)).resolves.toEqual([])
    expect(candleUrls(urls).length).toBeLessThanOrEqual(1 + 12)
  })

  it('stops once it passes Coinbase’s earliest data', async () => {
    const { p, urls } = provider({ candles: () => json([]) })
    await expect(p.getCandles('BTCUSD', '1d', 3, COINBASE_EARLIEST_S + 10 * D)).resolves.toEqual([])
    expect(candleUrls(urls)).toHaveLength(2)
  })

  it('a non-empty window is returned without stepping back', async () => {
    const { p, urls } = provider({ candles: sparse(T + 5 * H) })
    const bars = await p.getCandles('BTCUSD', '1h', 3, T + 5 * H)
    expect(bars).toHaveLength(3)
    expect(candleUrls(urls)).toHaveLength(1)
  })
})

describe('CoinbaseProvider errors', () => {
  it('an unknown symbol is bad-symbol without a candle request', async () => {
    const { p, urls } = provider()
    await expect(p.getCandles('NOPEUSD', '1h')).rejects.toMatchObject({ kind: 'bad-symbol' })
    expect(candleUrls(urls)).toEqual([])
  })
  it('404 from the candles endpoint is bad-symbol', async () => {
    const { p } = provider({ candles: () => json({ message: 'NotFound' }, 404) })
    await expect(p.getCandles('BTCUSD', '1h')).rejects.toMatchObject({
      kind: 'bad-symbol',
      status: 404,
    })
  })
  it('429 is rate-limited with Retry-After seconds', async () => {
    const { p } = provider({
      candles: () => json({ message: 'slow down' }, 429, { 'Retry-After': '3' }),
    })
    await expect(p.getCandles('BTCUSD', '1h')).rejects.toMatchObject({
      kind: 'rate-limited',
      status: 429,
      retryAfter: 3,
    })
  })
  it('418 is banned and 451 is region-blocked', async () => {
    await expect(
      provider({ candles: () => json({}, 418) }).p.getCandles('BTCUSD', '1h'),
    ).rejects.toMatchObject({ kind: 'banned' })
    await expect(
      provider({ candles: () => json({}, 451) }).p.getCandles('BTCUSD', '1h'),
    ).rejects.toMatchObject({ kind: 'region-blocked' })
  })
  it('other statuses are http with the Coinbase message', async () => {
    const { p } = provider({ candles: () => json({ message: 'Unsupported granularity' }, 400) })
    await expect(p.getCandles('BTCUSD', '1h')).rejects.toMatchObject({
      kind: 'http',
      status: 400,
      message: expect.stringContaining('Unsupported granularity') as unknown,
    })
  })
  it('a non-array body or malformed row is a parse error', async () => {
    await expect(
      provider({ candles: () => json({ nope: 1 }) }).p.getCandles('BTCUSD', '1h'),
    ).rejects.toMatchObject({ kind: 'parse' })
    await expect(
      provider({ candles: () => json([[1, 2]]) }).p.getCandles('BTCUSD', '1h'),
    ).rejects.toMatchObject({ kind: 'parse' })
  })
  it('a transport failure is network; a caller abort is aborted', async () => {
    const failing = new CoinbaseProvider({
      fetchFn: () => Promise.reject(new TypeError('Failed to fetch')),
      minRequestIntervalMs: 0,
    })
    await expect(failing.getSymbols()).rejects.toMatchObject({ kind: 'network' })
    const ctrl = new AbortController()
    ctrl.abort()
    await expect(
      provider().p.getCandles('BTCUSD', '1h', 10, undefined, { signal: ctrl.signal }),
    ).rejects.toMatchObject({
      kind: 'aborted',
    })
  })
})

describe('CoinbaseProvider rate limiting', () => {
  it('spaces every REST request by minRequestIntervalMs (8/s default)', async () => {
    vi.useFakeTimers()
    vi.setSystemTime((T + 14 * H) * 1000)
    const fetch = fakeFetch()
    const p = new CoinbaseProvider({ fetchFn: fetch.fn, minRequestIntervalMs: 125 })
    const done = p.getCandles('BTCUSD', '1m', 500) // /products + 2 candle pages
    await vi.advanceTimersByTimeAsync(0)
    expect(fetch.calls).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(124)
    expect(fetch.calls).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(fetch.calls).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(125)
    expect(fetch.calls).toHaveBeenCalledTimes(3)
    await expect(done).resolves.toHaveLength(500)
  })

  it('pauses the throttle after a 429', async () => {
    vi.useFakeTimers()
    vi.setSystemTime((T + 14 * H) * 1000)
    let n = 0
    const fetch = fakeFetch({
      candles: () => (++n === 1 ? json({}, 429, { 'Retry-After': '2' }) : json([row(T + 14 * H)])),
    })
    const p = new CoinbaseProvider({ fetchFn: fetch.fn, minRequestIntervalMs: 0 })
    await expect(p.getCandles('BTCUSD', '1h', 2)).rejects.toMatchObject({ kind: 'rate-limited' })
    const next = p.getCandles('BTCUSD', '1h', 2)
    await vi.advanceTimersByTimeAsync(1_999)
    expect(fetch.calls).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(1)
    expect(fetch.calls).toHaveBeenCalledTimes(3)
    await expect(next).resolves.toHaveLength(1)
  })
})

describe('CoinbaseProvider.getTickers', () => {
  // GET /products/BTC-USD/stats as observed live (2026-10-25).
  const BTC_STATS = {
    open: '80100.26',
    high: '80925',
    low: '79107',
    last: '80262.54',
    volume: '2725.55486336',
    volume_30day: '146097.35114321',
    rfq_volume_24hour: '89.108401',
  }

  it('maps 24h stats to a Ticker (quote volume approximated as volume * last)', () => {
    const t = parseStats(BTC_STATS, 'BTCUSD', 1_792_939_243)
    expect(t).toMatchObject({
      symbol: 'BTCUSD',
      last: 80262.54,
      open: 80100.26,
      high: 80925,
      low: 79107,
      volume: 2725.55486336,
      time: 1_792_939_243,
    })
    expect(t?.quoteVolume).toBeCloseTo(2725.55486336 * 80262.54, 4)
    expect(t?.changePct).toBeCloseTo(((80262.54 - 80100.26) / 80100.26) * 100, 10)
    expect(parseStats({ ...BTC_STATS, last: '' }, 'BTCUSD', 0)).toBeNull()
  })

  it('few symbols: one /stats request each, input order, unknown and 404 omitted', async () => {
    const { p, urls } = provider({
      stats: (id) =>
        id === 'ETH-USD'
          ? json({ message: 'NotFound' }, 404)
          : json({ ...BTC_STATS, last: id === 'SOL-USD' ? '150' : '80262.54' }),
    })
    const out = await p.getTickers(['solusd', 'NOPEUSD', 'BTCUSD', 'ETHUSD', 'BTCUSD', 'bad-sym'])
    expect(out.map((t) => [t.symbol, t.last])).toEqual([
      ['SOLUSD', 150],
      ['BTCUSD', 80262.54],
    ])
    expect(out[0].time).toBe(T + 14 * H + 30)
    expect(urls.filter((u) => u.includes('/stats'))).toEqual([
      'https://api.exchange.coinbase.com/products/SOL-USD/stats',
      'https://api.exchange.coinbase.com/products/BTC-USD/stats',
      'https://api.exchange.coinbase.com/products/ETH-USD/stats',
    ])
  })

  it('many symbols: one bulk /products/stats request', async () => {
    const entry = (last: string) => ({
      stats_24hour: { ...BTC_STATS, last },
      stats_30day: { volume: '1' },
    })
    const { p, urls } = provider({
      allStats: () =>
        json({
          'BTC-USD': entry('1'),
          'ETH-USD': entry('2'),
          'BTC-USDT': entry('3'),
          'SOL-USD': entry('4'),
          'ADA-USD': { stats_30day: {} },
        }),
    })
    const out = await p.getTickers(['ADAUSD', 'SOLUSD', 'BTCUSDT', 'ETHUSD', 'BTCUSD'])
    expect(out.map((t) => [t.symbol, t.last])).toEqual([
      ['SOLUSD', 4],
      ['BTCUSDT', 3],
      ['ETHUSD', 2],
      ['BTCUSD', 1],
    ])
    expect(urls.filter((u) => u.includes('stats'))).toEqual([
      'https://api.exchange.coinbase.com/products/stats',
    ])
  })

  it('an empty list resolves to [] without a request', async () => {
    const { p, calls } = provider()
    await expect(p.getTickers([])).resolves.toEqual([])
    expect(calls).not.toHaveBeenCalled()
  })
})

describe('CoinbaseProvider parallel candle paging (Phase 19)', () => {
  type Iv = Parameters<CoinbaseProvider['getCandles']>[1]
  const IVS: readonly Iv[] = ['1m', '5m', '15m', '1h', '4h', '1d', '1w']
  /** Copy of the private INTERVAL_SPEC (granularity fetched, bucket length). */
  const SPEC: Readonly<Record<Iv, { g: number; bucket: number }>> = {
    '1m': { g: 60, bucket: 60 },
    '5m': { g: 300, bucket: 300 },
    '15m': { g: 900, bucket: 900 },
    '1h': { g: H, bucket: H },
    '4h': { g: H, bucket: 4 * H },
    '1d': { g: D, bucket: D },
    '1w': { g: D, bucket: 7 * D },
  }
  const BASE = 'https://api.exchange.coinbase.com'
  const NOW_MS = NOW * 1000

  /** Deterministic 32-bit hash for synthetic gaps and latencies. */
  const hash = (n: number) => {
    let x = (n ^ 0x9e3779b9) >>> 0
    x = Math.imul(x ^ (x >>> 16), 0x85ebca6b) >>> 0
    x = Math.imul(x ^ (x >>> 13), 0xc2b2ae35) >>> 0
    return (x ^ (x >>> 16)) >>> 0
  }

  type Mode = 'dense' | 'sparse' | 'stale'
  /**
   * Candle rows for a request, newest first. 'sparse' omits ~1 in 5 buckets; 'stale' has no
   * rows newer than 40 days before NOW (forces the step-back path). Every page also returns
   * a row one slot before its start with page-specific values, so assembling pages in a
   * different order than the sequential loop would change which duplicate wins.
   */
  const serveRows = (u: URL, mode: Mode): unknown[] => {
    const g = Number(u.searchParams.get('granularity'))
    const start = Date.parse(u.searchParams.get('start') ?? '') / 1000
    const end = Date.parse(u.searchParams.get('end') ?? '') / 1000
    const has = (t: number) =>
      mode === 'dense' ||
      (mode === 'sparse' && hash(t) % 5 !== 0) ||
      (mode === 'stale' && t <= NOW - 40 * D)
    const rows: unknown[] = []
    if (has(start - g)) rows.push([start - g, 1, 9, 2, 3, 1000 + (start % 997)])
    for (let t = start; t <= end; t += g) if (has(t)) rows.unshift(row(t))
    return rows
  }

  /** The pre-Phase-19 sequential getCandles (fetchBars + stepBack), copied as the oracle. */
  function sequentialOracle(
    serve: (u: URL) => unknown[],
    interval: Iv,
    limit: number,
    endTime: number | undefined,
    nowS: number,
  ): { bars: Candle[]; urls: string[] } {
    const urls: string[] = []
    const pid = 'BTC-USD'
    const fetchRows = (g: number, start: number, pageEnd: number): Candle[] => {
      const params = new URLSearchParams({
        granularity: String(g),
        start: iso(start),
        end: iso(pageEndParam(pageEnd, g, nowS)),
      })
      const u = new URL(`${BASE}/products/${encodeURIComponent(pid)}/candles?${params.toString()}`)
      urls.push(u.toString())
      return serve(u).map(parseCoinbaseCandle)
    }
    const dedupe = (sorted: Candle[]) => {
      const out: Candle[] = []
      for (const c of sorted) {
        if (out.length > 0 && out[out.length - 1].time === c.time) out[out.length - 1] = c
        else out.push(c)
      }
      return out
    }
    const fetchBars = (firstBucket: number, lastBucket: number): Candle[] => {
      const spec = SPEC[interval]
      const g = spec.g
      const compEnd = Math.min(lastBucket + spec.bucket - g, Math.floor(nowS / g) * g)
      const rows: Candle[] = []
      for (let start = firstBucket; start <= compEnd; start += 300 * g) {
        const pageEnd = Math.min(start + 299 * g, compEnd)
        for (const r of fetchRows(g, start, pageEnd)) rows.push(r)
      }
      rows.sort((a, b) => a.time - b.time)
      const components = dedupe(rows).filter((c) => c.time >= firstBucket && c.time <= compEnd)
      return spec.bucket === g ? components : aggregateCandles(components, interval, firstBucket)
    }
    const stepBack = (before: number): Candle[] => {
      const spec = SPEC[interval]
      const g = spec.g
      const perPage = Math.max(1, Math.floor((300 * g) / spec.bucket))
      let cursor = before
      let floor = g < D ? Math.floor(cursor / D) * D : -Infinity
      for (let n = 0; n < 12 && cursor > COINBASE_EARLIEST_S; n++) {
        if (cursor > floor) {
          const first = Math.max(floor, cursor - perPage * spec.bucket)
          const bars = fetchBars(first, cursor - spec.bucket)
          if (bars.length > 0) return bars
          cursor = first
          continue
        }
        const probeStart = cursor - 300 * D
        const probeEnd = cursor - D
        const days = fetchRows(D, probeStart, probeEnd).filter(
          (d) => d.time >= probeStart && d.time <= probeEnd,
        )
        if (days.length === 0) {
          cursor = probeStart
          floor = probeStart
          continue
        }
        const newest = days.reduce((t, d) => Math.max(t, d.time), -Infinity)
        cursor = newest + D
        floor = newest
      }
      return []
    }
    const lim = Math.min(1000, Math.max(1, Math.floor(limit)))
    const end = endTime === undefined ? nowS : Math.min(Math.floor(endTime), nowS)
    const lastBucket = bucketStart(end, interval)
    const firstBucket = lastBucket - (lim - 1) * SPEC[interval].bucket
    const bars = fetchBars(firstBucket, lastBucket)
    if (bars.length > 0) return { bars: bars.slice(-lim), urls }
    return { bars: stepBack(firstBucket).slice(-lim), urls }
  }

  interface Sent {
    url: URL
    at: number
    signal: AbortSignal | undefined
  }

  /**
   * A fetch with per-request latency on fake timers. `respond` builds the Response (default:
   * serveRows in `mode`); a request aborted before its response rejects like fetch does.
   */
  function timedFetch(
    latency: (u: URL, n: number) => number,
    mode: Mode = 'dense',
    respond?: (u: URL, n: number) => Response | null,
  ) {
    const sent: Sent[] = []
    const fn = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = new URL(String(input))
      const signal = init?.signal ?? undefined
      const n = sent.filter((s) => s.url.pathname.endsWith('/candles')).length + 1
      sent.push({ url, at: Date.now(), signal })
      return new Promise<Response>((resolve, reject) => {
        const id = setTimeout(
          () => {
            if (url.pathname === '/products') return resolve(json(PRODUCTS))
            resolve(respond?.(url, n) ?? json(serveRows(url, mode)))
          },
          latency(url, n),
        )
        signal?.addEventListener('abort', () => {
          clearTimeout(id)
          reject(new DOMException('The operation was aborted.', 'AbortError'))
        })
      })
    }
    const candleSent = () => sent.filter((s) => s.url.pathname.endsWith('/candles'))
    return { fn: fn as typeof fetch, sent, candleSent }
  }

  /** Settle a promise into a value or the error, so a rejection is never unhandled. */
  const outcome = <V>(p: Promise<V>) =>
    p.then(
      (value) => ({ ok: true as const, value }),
      (error: unknown) => ({ ok: false as const, error }),
    )

  it('assembles exactly the candles (and requests) of the sequential implementation', async () => {
    vi.useFakeTimers()
    const limits = [1, 7, 300, 301, 500, 1000]
    const ends = [undefined, NOW - 37 * D + 1234, NOW - 400 * D + 5]
    const modes: Mode[] = ['dense', 'sparse', 'stale']
    let cases = 0
    let multiPage = 0
    for (const interval of IVS) {
      for (const limit of limits) {
        for (const endTime of ends) {
          for (const mode of modes) {
            vi.setSystemTime(NOW_MS)
            const oracle = sequentialOracle(
              (u) => serveRows(u, mode),
              interval,
              limit,
              endTime,
              NOW,
            )
            const fetch = timedFetch(
              (u) => 20 + (hash(u.toString().length * 31 + cases) % 380),
              mode,
            )
            const p = new CoinbaseProvider({ fetchFn: fetch.fn, minRequestIntervalMs: 125 })
            const done = outcome(p.getCandles('BTCUSD', interval, limit, endTime))
            await vi.runAllTimersAsync()
            const res = await done
            const label = `${interval} limit=${limit} end=${String(endTime)} ${mode}`
            expect(res.ok, label).toBe(true)
            if (res.ok) expect(res.value, label).toEqual(oracle.bars)
            expect(
              fetch.candleSent().map((s) => s.url.toString()),
              label,
            ).toEqual(oracle.urls)
            // Every request start (catalog included) is >= 125 ms after the previous one.
            for (let i = 1; i < fetch.sent.length; i++) {
              expect(fetch.sent[i].at - fetch.sent[i - 1].at, label).toBeGreaterThanOrEqual(125)
            }
            cases++
            if (oracle.urls.length > 1) multiPage++
          }
        }
      }
    }
    expect(cases).toBe(IVS.length * limits.length * ends.length * modes.length)
    expect(multiPage).toBeGreaterThan(50)
  })

  it.each([
    ['4h', 7],
    ['1w', 12],
    ['1m', 2],
  ] as const)(
    '%s x 500 (%i pages): starts every 125 ms and finishes after (pages - 1) * 125 ms + one round trip',
    async (interval, pages) => {
      vi.useFakeTimers()
      vi.setSystemTime(NOW_MS)
      const L = 300
      const fetch = timedFetch(() => L)
      const p = new CoinbaseProvider({ fetchFn: fetch.fn, minRequestIntervalMs: 125 })
      const t0 = Date.now()
      let doneAt = -1
      const done = outcome(p.getCandles('BTCUSD', interval, 500)).then((r) => {
        doneAt = Date.now()
        return r
      })
      await vi.runAllTimersAsync()
      const res = await done
      expect(res.ok).toBe(true)
      if (res.ok) expect(res.value).toHaveLength(500)
      const starts = fetch.candleSent().map((s) => s.at - t0)
      expect(starts).toHaveLength(pages)
      // The catalog round trip first, then one page start per 125 ms slot.
      expect(starts).toEqual(Array.from({ length: pages }, (_, i) => L + i * 125))
      // Sequential took catalog + pages * round trip (the 125 ms slot is shorter than L).
      expect(doneAt - t0).toBe(L + (pages - 1) * 125 + L)
      expect(doneAt - t0).toBeLessThan(L + pages * L)
    },
  )

  it('the default throttle still spaces parallel pages 125 ms apart', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW_MS)
    const fetch = timedFetch(() => 0)
    const p = new CoinbaseProvider({ fetchFn: fetch.fn })
    const done = outcome(p.getCandles('BTCUSD', '1w', 1000))
    await vi.runAllTimersAsync()
    expect((await done).ok).toBe(true)
    const at = fetch.sent.map((s) => s.at)
    expect(at.length).toBe(1 + 24)
    for (let i = 1; i < at.length; i++) expect(at[i] - at[i - 1]).toBeGreaterThanOrEqual(125)
  })

  it('a caller abort mid-load aborts every page, starts none after, and frees the unused slot', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW_MS)
    const L = 300
    const fetch = timedFetch(() => L)
    const p = new CoinbaseProvider({ fetchFn: fetch.fn, minRequestIntervalMs: 125 })
    const ctrl = new AbortController()
    const t0 = Date.now()
    const done = outcome(p.getCandles('BTCUSD', '4h', 500, undefined, { signal: ctrl.signal }))
    await vi.advanceTimersByTimeAsync(L + 2 * 125 + 10) // pages 1-3 sent
    expect(fetch.candleSent()).toHaveLength(3)
    ctrl.abort()
    const res = await done
    expect(res.ok).toBe(false)
    if (!res.ok) expect(res.error).toMatchObject({ kind: 'aborted' })
    expect(fetch.candleSent().every((s) => s.signal?.aborted === true)).toBe(true)
    await vi.advanceTimersByTimeAsync(5_000)
    expect(fetch.candleSent()).toHaveLength(3)

    // The next load starts one slot after the last request actually sent (page 3), as the
    // sequential loop allowed; the cancelled page 4 slot is given back.
    const lastStart = fetch.candleSent()[2].at
    vi.setSystemTime(lastStart + 10)
    const next = outcome(p.getCandles('BTCUSD', '1h', 2))
    await vi.runAllTimersAsync()
    expect((await next).ok).toBe(true)
    expect(fetch.candleSent()[3].at - lastStart).toBe(125)
    expect(lastStart - t0).toBe(L + 250)
  })

  it('a 429 on page 2: rate-limited with Retry-After, later pages aborted, none started after', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW_MS)
    const L = 300
    const fetch = timedFetch(
      () => L,
      'dense',
      (_u, n) => (n === 2 ? json({ message: 'slow down' }, 429, { 'Retry-After': '2' }) : null),
    )
    const p = new CoinbaseProvider({ fetchFn: fetch.fn, minRequestIntervalMs: 125 })
    const t0 = Date.now()
    const done = outcome(p.getCandles('BTCUSD', '4h', 500))
    await vi.runAllTimersAsync()
    const res = await done
    expect(res.ok).toBe(false)
    if (!res.ok) {
      expect(res.error).toBeInstanceOf(MarketDataError)
      expect(res.error).toMatchObject({ kind: 'rate-limited', status: 429, retryAfter: 2 })
    }
    const rateLimitedAt = t0 + L + 125 + L // page 2 sent at L + 125, answered L later
    const sent = fetch.candleSent()
    expect(sent.every((s) => s.at <= rateLimitedAt)).toBe(true)
    expect(sent.map((s) => s.at - t0)).toEqual([L, L + 125, L + 250, L + 375])
    // Pages sent after page 2 were cancelled; pages 1 and 2 completed.
    expect(sent.map((s) => s.signal?.aborted === true)).toEqual([false, false, true, true])

    // The throttle stays paused for Retry-After from the 429.
    const next = outcome(p.getCandles('BTCUSD', '1h', 2))
    await vi.runAllTimersAsync()
    expect((await next).ok).toBe(true)
    expect(fetch.candleSent()[4].at).toBe(rateLimitedAt + 2_000)
  })

  it('the earliest failing page’s error wins, as in the sequential loop', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW_MS)
    // Page 2 answers slowly with a 500; page 3 fails fast with a 400 before page 2 returns.
    const fetch = timedFetch(
      (u, n) => (u.pathname === '/products' ? 100 : n === 2 ? 900 : n === 3 ? 10 : 300),
      'dense',
      (_u, n) =>
        n === 2
          ? json({ message: 'page two' }, 500)
          : n === 3
            ? json({ message: 'page three' }, 400)
            : null,
    )
    const p = new CoinbaseProvider({ fetchFn: fetch.fn, minRequestIntervalMs: 125 })
    const done = outcome(p.getCandles('BTCUSD', '4h', 500))
    await vi.runAllTimersAsync()
    const res = await done
    expect(res.ok).toBe(false)
    if (!res.ok) {
      expect(res.error).toMatchObject({ kind: 'http', status: 500 })
      expect(String((res.error as Error).message)).toContain('page two')
    }
    // Page 3 failed at its start + 10 ms, before page 4's slot: no fourth page.
    expect(fetch.candleSent()).toHaveLength(3)
  })

  it('a later page failing after the earlier ones succeed is the error', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW_MS)
    const fetch = timedFetch(
      () => 300,
      'dense',
      (_u, n) => (n === 5 ? json({ message: 'boom' }, 503) : null),
    )
    const p = new CoinbaseProvider({ fetchFn: fetch.fn, minRequestIntervalMs: 125 })
    const done = outcome(p.getCandles('BTCUSD', '4h', 500))
    await vi.runAllTimersAsync()
    const res = await done
    expect(res.ok).toBe(false)
    if (!res.ok) expect(res.error).toMatchObject({ kind: 'http', status: 503 })
    const sent = fetch.candleSent()
    // Page 5 answered at its start + 300: pages 6 and 7 had started and were aborted.
    expect(sent).toHaveLength(7)
    expect(sent.map((s) => s.signal?.aborted === true)).toEqual([
      false,
      false,
      false,
      false,
      false,
      true,
      true,
    ])
  })
})
