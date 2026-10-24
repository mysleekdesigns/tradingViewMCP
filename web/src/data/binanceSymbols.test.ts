import { describe, expect, it, vi } from 'vitest'
import { BINANCE_US_REST, BinanceProvider } from './binance'
import { parseSymbolInfo } from './binanceTicker'
import { MarketDataError } from './provider'

// Inline fixture shaped like a real Binance.US exchangeInfo response (trimmed).
function symbolEntry(
  symbol: string,
  base: string,
  quote: string,
  status = 'TRADING',
  tick = '0.01',
) {
  return {
    symbol,
    status,
    baseAsset: base,
    baseAssetPrecision: 8,
    quoteAsset: quote,
    quotePrecision: 8,
    orderTypes: ['LIMIT', 'MARKET'],
    filters: [
      { filterType: 'PERCENT_PRICE', multiplierUp: '5', multiplierDown: '0.2', avgPriceMins: 5 },
      { filterType: 'PRICE_FILTER', minPrice: '0.01', maxPrice: '1000000.00', tickSize: tick },
      { filterType: 'LOT_SIZE', minQty: '0.00001', maxQty: '9000', stepSize: '0.00001' },
    ],
  }
}

const EXCHANGE_INFO = {
  timezone: 'UTC',
  serverTime: 1792868675845,
  rateLimits: [],
  exchangeFilters: [],
  symbols: [
    symbolEntry('BTCUSDT', 'BTC', 'USDT'),
    symbolEntry('ETHBTC', 'ETH', 'BTC', 'TRADING', '0.00000100'),
    symbolEntry('OLDUSD', 'OLD', 'USD', 'BREAK', '0.0001'),
    { symbol: 'BROKEN', status: 'TRADING' }, // malformed: skipped
  ],
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function fakeFetch(impl: (url: string) => Promise<Response>) {
  return vi.fn((input: RequestInfo | URL) => impl(String(input))) as unknown as typeof fetch & {
    mock: { calls: unknown[][] }
  }
}

async function rejection(p: Promise<unknown>): Promise<MarketDataError> {
  const err = await p.then(
    () => {
      throw new Error('expected rejection')
    },
    (e: unknown) => e,
  )
  expect(err).toBeInstanceOf(MarketDataError)
  return err as MarketDataError
}

describe('parseSymbolInfo', () => {
  it('extracts fields and tickSize from PRICE_FILTER as a number', () => {
    expect(parseSymbolInfo(symbolEntry('ETHBTC', 'ETH', 'BTC', 'TRADING', '0.00000100'))).toEqual({
      symbol: 'ETHBTC',
      baseAsset: 'ETH',
      quoteAsset: 'BTC',
      status: 'TRADING',
      tickSize: 0.000001,
    })
  })

  it('accepts tickSize 0 (Binance: tick rule disabled)', () => {
    expect(parseSymbolInfo(symbolEntry('X', 'A', 'B', 'TRADING', '0'))?.tickSize).toBe(0)
  })

  it.each([
    ['null', null],
    ['array', []],
    ['missing symbol', { ...symbolEntry('A', 'B', 'C'), symbol: undefined }],
    ['empty baseAsset', symbolEntry('A', '', 'C')],
    ['numeric quoteAsset', { ...symbolEntry('A', 'B', 'C'), quoteAsset: 5 }],
    ['missing status', { ...symbolEntry('A', 'B', 'C'), status: undefined }],
    ['no filters', { ...symbolEntry('A', 'B', 'C'), filters: undefined }],
    ['no PRICE_FILTER', { ...symbolEntry('A', 'B', 'C'), filters: [{ filterType: 'LOT_SIZE' }] }],
    ['non-numeric tickSize', symbolEntry('A', 'B', 'C', 'TRADING', 'abc')],
    ['empty tickSize', symbolEntry('A', 'B', 'C', 'TRADING', '')],
    ['negative tickSize', symbolEntry('A', 'B', 'C', 'TRADING', '-1')],
  ])('returns null for %s', (_label, raw) => {
    expect(parseSymbolInfo(raw)).toBeNull()
  })
})

describe('BinanceProvider.getSymbols', () => {
  it('GETs /api/v3/exchangeInfo on Binance.US and returns all valid symbols unfiltered', async () => {
    const fetchFn = fakeFetch(async () => jsonResponse(EXCHANGE_INFO))
    const symbols = await new BinanceProvider({ fetchFn }).getSymbols()
    expect(String(fetchFn.mock.calls[0][0])).toBe(`${BINANCE_US_REST}/api/v3/exchangeInfo`)
    expect(symbols).toEqual([
      {
        symbol: 'BTCUSDT',
        baseAsset: 'BTC',
        quoteAsset: 'USDT',
        status: 'TRADING',
        tickSize: 0.01,
      },
      { symbol: 'ETHBTC', baseAsset: 'ETH', quoteAsset: 'BTC', status: 'TRADING', tickSize: 1e-6 },
      // Non-TRADING kept: callers filter.
      { symbol: 'OLDUSD', baseAsset: 'OLD', quoteAsset: 'USD', status: 'BREAK', tickSize: 0.0001 },
    ])
  })

  it('memoizes per instance: concurrent and later calls share one request', async () => {
    const fetchFn = fakeFetch(async () => jsonResponse(EXCHANGE_INFO))
    const p = new BinanceProvider({ fetchFn })
    const [a, b] = await Promise.all([p.getSymbols(), p.getSymbols()])
    const c = await p.getSymbols()
    expect(fetchFn).toHaveBeenCalledTimes(1)
    expect(a).toBe(b)
    expect(c).toBe(a)
    // A separate instance has its own cache.
    await new BinanceProvider({ fetchFn }).getSymbols()
    expect(fetchFn).toHaveBeenCalledTimes(2)
  })

  it('does not cache a failed fetch; the next call retries', async () => {
    let n = 0
    const fetchFn = fakeFetch(async () => {
      n += 1
      if (n === 1) throw new TypeError('Failed to fetch')
      return jsonResponse(EXCHANGE_INFO)
    })
    const p = new BinanceProvider({ fetchFn })
    expect((await rejection(p.getSymbols())).kind).toBe('network')
    expect(await p.getSymbols()).toHaveLength(3)
    expect(fetchFn).toHaveBeenCalledTimes(2)
  })

  it.each([
    ['non-object body', [1, 2]],
    ['missing symbols array', { timezone: 'UTC' }],
    ['symbols not an array', { symbols: {} }],
  ])('rejects %s with parse (and does not cache it)', async (_label, body) => {
    const fetchFn = fakeFetch(async () => jsonResponse(body))
    const p = new BinanceProvider({ fetchFn })
    expect((await rejection(p.getSymbols())).kind).toBe('parse')
    await rejection(p.getSymbols())
    expect(fetchFn).toHaveBeenCalledTimes(2)
  })

  it('maps invalid JSON to parse', async () => {
    const fetchFn = fakeFetch(async () => new Response('<html>', { status: 200 }))
    expect((await rejection(new BinanceProvider({ fetchFn }).getSymbols())).kind).toBe('parse')
  })

  it.each([
    [429, 'rate-limited'],
    [418, 'banned'],
    [451, 'region-blocked'],
    [500, 'http'],
  ])('maps HTTP %d to %s', async (status, kind) => {
    const fetchFn = fakeFetch(
      async () => new Response('', { status, headers: { 'Retry-After': '12' } }),
    )
    const err = await rejection(new BinanceProvider({ fetchFn }).getSymbols())
    expect(err.kind).toBe(kind)
    expect(err.status).toBe(status)
    if (status === 429 || status === 418) expect(err.retryAfter).toBe(12)
  })

  it('maps a timeout to network', async () => {
    const fetchFn = vi.fn(
      (_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(init.signal?.reason))
        }),
    ) as unknown as typeof fetch
    const err = await rejection(new BinanceProvider({ fetchFn, timeoutMs: 20 }).getSymbols())
    expect(err.kind).toBe('network')
  })

  it("a caller abort rejects only that caller with 'aborted'; the shared fetch continues", async () => {
    let resolveFetch: (r: Response) => void = () => {}
    const fetchFn = fakeFetch(
      () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve
        }),
    )
    const p = new BinanceProvider({ fetchFn })
    const controller = new AbortController()
    const aborted = p.getSymbols({ signal: controller.signal })
    const other = p.getSymbols()
    controller.abort()
    expect((await rejection(aborted)).kind).toBe('aborted')
    resolveFetch(jsonResponse(EXCHANGE_INFO))
    expect(await other).toHaveLength(3)
    expect(fetchFn).toHaveBeenCalledTimes(1)
  })

  it('rejects immediately with aborted when the signal is already aborted, without a request', async () => {
    const fetchFn = fakeFetch(async () => jsonResponse(EXCHANGE_INFO))
    const controller = new AbortController()
    controller.abort()
    const err = await rejection(
      new BinanceProvider({ fetchFn }).getSymbols({ signal: controller.signal }),
    )
    expect(err.kind).toBe('aborted')
    expect(fetchFn).not.toHaveBeenCalled()
  })
})
