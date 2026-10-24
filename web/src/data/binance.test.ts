import { describe, expect, it, vi } from 'vitest'
import { BinanceProvider, parseKline } from './binance'
import { BINANCE_US_REST } from './venue'
import { MarketDataError } from './provider'

// Inline fixture: two real-shaped Binance kline rows (strings for prices, ms times),
// deliberately out of order to check sorting.
const ROW_A = [
  1700000000000,
  '37000.10',
  '37100.50',
  '36950.00',
  '37050.25',
  '123.456',
  1700003599999,
  '4567890.12',
  1000,
  '60.1',
  '2222222.2',
  '0',
]
const ROW_B = [
  1700003600000,
  '37050.25',
  '37200.00',
  '37000.00',
  '37150.75',
  '98.7',
  1700007199999,
  '3666666.6',
  900,
  '50.0',
  '1850000.0',
  '0',
]

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

async function expectKind(p: Promise<unknown>, kind: string, status?: number) {
  const err = await p.then(
    () => {
      throw new Error('expected rejection')
    },
    (e: unknown) => e,
  )
  expect(err).toBeInstanceOf(MarketDataError)
  expect((err as MarketDataError).kind).toBe(kind)
  if (status !== undefined) expect((err as MarketDataError).status).toBe(status)
}

describe('parseKline', () => {
  it('converts string prices to numbers and ms to UNIX seconds', () => {
    expect(parseKline(ROW_A)).toEqual({
      time: 1700000000,
      open: 37000.1,
      high: 37100.5,
      low: 36950,
      close: 37050.25,
      volume: 123.456,
    })
  })

  it('floors non-whole-second open times', () => {
    expect(parseKline([1700000000999, '1', '1', '1', '1', '1']).time).toBe(1700000000)
  })

  it.each([
    ['not an array', { t: 1 }],
    ['too short', [1700000000000, '1', '2', '3', '4']],
    ['non-numeric price', [1700000000000, 'abc', '2', '0.5', '1', '10']],
    ['empty string', [1700000000000, '', '2', '0.5', '1', '10']],
    ['null field', [1700000000000, '1', null, '0.5', '1', '10']],
    ['infinite volume', [1700000000000, '1', '2', '0.5', '1', 'Infinity']],
    ['NaN time', ['nope', '1', '2', '0.5', '1', '10']],
  ])('rejects malformed row: %s', (_label, row) => {
    expect(() => parseKline(row)).toThrow(MarketDataError)
    try {
      parseKline(row)
    } catch (e) {
      expect((e as MarketDataError).kind).toBe('parse')
    }
  })
})

describe('BinanceProvider.getCandles', () => {
  it('can still be pointed at Binance.US', async () => {
    const fetchFn = fakeFetch(async () => jsonResponse([ROW_A]))
    await new BinanceProvider({ fetchFn, baseUrl: BINANCE_US_REST }).getCandles('BTCUSDT', '1h')
    expect(new URL(String(fetchFn.mock.calls[0][0])).origin).toBe('https://api.binance.us')
  })

  it('builds the klines URL against the vision host by default with an uppercased symbol', async () => {
    const fetchFn = fakeFetch(async () => jsonResponse([ROW_A]))
    await new BinanceProvider({ fetchFn }).getCandles('btcusdt', '1h')
    const url = new URL(String(fetchFn.mock.calls[0][0]))
    expect(url.origin).toBe('https://data-api.binance.vision')
    expect(url.pathname).toBe('/api/v3/klines')
    expect(url.searchParams.get('symbol')).toBe('BTCUSDT')
    expect(url.searchParams.get('interval')).toBe('1h')
    expect(url.searchParams.get('limit')).toBe('500')
    expect(url.searchParams.has('endTime')).toBe(false)
  })

  it('honours a custom baseUrl', async () => {
    const fetchFn = fakeFetch(async () => jsonResponse([]))
    await new BinanceProvider({ baseUrl: 'http://localhost:9999/', fetchFn }).getCandles(
      'ETHUSDT',
      '4h',
    )
    expect(
      String(fetchFn.mock.calls[0][0]).startsWith('http://localhost:9999/api/v3/klines?'),
    ).toBe(true)
  })

  it.each([
    [5000, '1000'],
    [1000, '1000'],
    [0, '1'],
    [-3, '1'],
    [250, '250'],
  ])('clamps limit %d to %s', async (limit, expected) => {
    const fetchFn = fakeFetch(async () => jsonResponse([]))
    await new BinanceProvider({ fetchFn }).getCandles('BTCUSDT', '1m', limit)
    expect(new URL(String(fetchFn.mock.calls[0][0])).searchParams.get('limit')).toBe(expected)
  })

  it('converts endTime from seconds to milliseconds', async () => {
    const fetchFn = fakeFetch(async () => jsonResponse([]))
    await new BinanceProvider({ fetchFn }).getCandles('BTCUSDT', '1d', 100, 1700000000)
    expect(new URL(String(fetchFn.mock.calls[0][0])).searchParams.get('endTime')).toBe(
      '1700000000000',
    )
  })

  it('returns parsed candles sorted ascending, keeping the last (forming) row', async () => {
    const fetchFn = fakeFetch(async () => jsonResponse([ROW_B, ROW_A]))
    const candles = await new BinanceProvider({ fetchFn }).getCandles('BTCUSDT', '1h')
    expect(candles.map((c) => c.time)).toEqual([1700000000, 1700003600])
    expect(candles[1].close).toBe(37150.75)
  })

  it('maps a rejected fetch to a network error', async () => {
    const fetchFn = fakeFetch(async () => {
      throw new TypeError('Failed to fetch')
    })
    await expectKind(new BinanceProvider({ fetchFn }).getCandles('BTCUSDT', '1h'), 'network')
  })

  it('maps HTTP 400 code -1121 to bad-symbol', async () => {
    const fetchFn = fakeFetch(async () =>
      jsonResponse({ code: -1121, msg: 'Invalid symbol.' }, 400),
    )
    await expectKind(
      new BinanceProvider({ fetchFn }).getCandles('NOPEUSDT', '1h'),
      'bad-symbol',
      400,
    )
  })

  it('maps other HTTP 400 codes to http', async () => {
    const fetchFn = fakeFetch(async () =>
      jsonResponse({ code: -1120, msg: 'Invalid interval.' }, 400),
    )
    await expectKind(new BinanceProvider({ fetchFn }).getCandles('BTCUSDT', '1h'), 'http', 400)
  })

  it('maps HTTP 500 to http with status', async () => {
    const fetchFn = fakeFetch(async () => new Response('oops', { status: 500 }))
    await expectKind(new BinanceProvider({ fetchFn }).getCandles('BTCUSDT', '1h'), 'http', 500)
  })

  it('maps HTTP 429 to rate-limited with Retry-After seconds', async () => {
    const fetchFn = fakeFetch(
      async () => new Response('', { status: 429, headers: { 'Retry-After': '30' } }),
    )
    const err = await new BinanceProvider({ fetchFn }).getCandles('BTCUSDT', '1h').catch((e) => e)
    expect(err).toBeInstanceOf(MarketDataError)
    expect(err.kind).toBe('rate-limited')
    expect(err.retryAfter).toBe(30)
  })

  it('maps HTTP 418 to banned', async () => {
    const fetchFn = fakeFetch(
      async () => new Response('', { status: 418, headers: { 'Retry-After': '120' } }),
    )
    await expectKind(new BinanceProvider({ fetchFn }).getCandles('BTCUSDT', '1h'), 'banned', 418)
  })

  it('maps HTTP 451 to region-blocked', async () => {
    const fetchFn = fakeFetch(async () => new Response('', { status: 451 }))
    await expectKind(
      new BinanceProvider({ fetchFn }).getCandles('BTCUSDT', '1h'),
      'region-blocked',
      451,
    )
  })

  it('maps a request that exceeds the timeout to network', async () => {
    // A fetch that never settles until its signal aborts.
    const fetchFn = vi.fn(
      (_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(init.signal?.reason))
        }),
    ) as unknown as typeof fetch
    await expectKind(
      new BinanceProvider({ fetchFn, timeoutMs: 20 }).getCandles('BTCUSDT', '1h'),
      'network',
    )
  })

  it('maps a caller abort to aborted', async () => {
    const fetchFn = vi.fn(
      (_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(init.signal?.reason))
        }),
    ) as unknown as typeof fetch
    const controller = new AbortController()
    const p = new BinanceProvider({ fetchFn }).getCandles('BTCUSDT', '1h', 500, undefined, {
      signal: controller.signal,
    })
    controller.abort()
    await expectKind(p, 'aborted')
  })

  it('drops duplicate open times, keeping the later row', async () => {
    const dup = [...ROW_A]
    dup[4] = '99999.00'
    const fetchFn = fakeFetch(async () => jsonResponse([ROW_A, ROW_B, dup]))
    const candles = await new BinanceProvider({ fetchFn }).getCandles('BTCUSDT', '1h')
    expect(candles.map((c) => c.time)).toEqual([1700000000, 1700003600])
    expect(candles[0].close).toBe(99999)
  })

  it('maps invalid JSON to parse', async () => {
    const fetchFn = fakeFetch(async () => new Response('<html>not json', { status: 200 }))
    await expectKind(new BinanceProvider({ fetchFn }).getCandles('BTCUSDT', '1h'), 'parse')
  })

  it('maps a non-array body to parse', async () => {
    const fetchFn = fakeFetch(async () => jsonResponse({ rows: [] }))
    await expectKind(new BinanceProvider({ fetchFn }).getCandles('BTCUSDT', '1h'), 'parse')
  })

  it('maps a malformed row inside the array to parse', async () => {
    const fetchFn = fakeFetch(async () => jsonResponse([ROW_A, [1, 'x']]))
    await expectKind(new BinanceProvider({ fetchFn }).getCandles('BTCUSDT', '1h'), 'parse')
  })
})

describe('BinanceProvider error mapping is shared across endpoints', () => {
  it('getTickers maps HTTP 429 to rate-limited with Retry-After', async () => {
    const fetchFn = fakeFetch(
      async () => new Response('', { status: 429, headers: { 'Retry-After': '7' } }),
    )
    const err = await new BinanceProvider({ fetchFn }).getTickers(['BTCUSDT']).catch((e) => e)
    expect(err).toBeInstanceOf(MarketDataError)
    expect(err.kind).toBe('rate-limited')
    expect(err.retryAfter).toBe(7)
  })

  it('getSymbols maps HTTP 451 to region-blocked', async () => {
    const fetchFn = fakeFetch(async () => new Response('', { status: 451 }))
    await expectKind(new BinanceProvider({ fetchFn }).getSymbols(), 'region-blocked', 451)
  })
})
