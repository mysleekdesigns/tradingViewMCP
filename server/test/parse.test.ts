import { describe, expect, it } from 'vitest'
import {
  changePct,
  parseErrorBody,
  parseExchangeInfo,
  parseKline,
  parseKlines,
  parseRetryAfter,
  parseSymbolInfo,
  parseTicker24hr,
  parseTickers24hr,
} from '../src/upstream/parse.ts'
import { ApiError } from '../src/types.ts'

// Shape copied from a live api.binance.us BTCUSDT 1w response.
const ROW = [
  1792368000000,
  '82106.90000000',
  '84895.75000000',
  '79237.41000000',
  '80535.39000000',
  '378.64507000',
  1792972799999,
  '31221437.80113350',
  36849,
  '175.45844000',
  '14465013.49942650',
  '0',
]

function expectParse(fn: () => unknown): void {
  try {
    fn()
  } catch (e) {
    expect(e).toBeInstanceOf(ApiError)
    expect((e as ApiError).kind).toBe('parse')
    expect((e as ApiError).status).toBe(502)
    return
  }
  throw new Error('expected a parse error')
}

describe('klines', () => {
  it('parses string prices and ms open time into numbers and seconds', () => {
    expect(parseKline(ROW)).toEqual({
      time: 1792368000,
      open: 82106.9,
      high: 84895.75,
      low: 79237.41,
      close: 80535.39,
      volume: 378.64507,
    })
  })

  it('rejects short rows, non-numeric and empty fields', () => {
    expectParse(() => parseKline([1, '2', '3']))
    expectParse(() => parseKline([1, 'x', '1', '1', '1', '1']))
    expectParse(() => parseKline([1, '', '1', '1', '1', '1']))
    expectParse(() => parseKline({}))
    expectParse(() => parseKlines({ code: 0 }))
  })

  it('sorts ascending and dedupes by time (later row wins)', () => {
    const a = [120000, '1', '1', '1', '1', '1']
    const b = [60000, '2', '2', '2', '2', '2']
    const a2 = [120000, '3', '3', '3', '3', '3']
    expect(parseKlines([a, b, a2]).map((c) => [c.time, c.close])).toEqual([
      [60, 2],
      [120, 3],
    ])
    expect(parseKlines([])).toEqual([])
  })
})

describe('exchangeInfo', () => {
  const sym = {
    symbol: 'BTCUSDT',
    status: 'TRADING',
    baseAsset: 'BTC',
    quoteAsset: 'USDT',
    filters: [
      { filterType: 'PRICE_FILTER', minPrice: '0.01', tickSize: '0.01000000' },
      { filterType: 'LOT_SIZE', stepSize: '0.00001' },
    ],
  }

  it('maps symbols with tickSize from PRICE_FILTER and reads the weight limit', () => {
    const info = parseExchangeInfo({
      rateLimits: [
        { rateLimitType: 'REQUEST_WEIGHT', interval: 'MINUTE', intervalNum: 1, limit: 6000 },
        { rateLimitType: 'ORDERS', interval: 'SECOND', intervalNum: 10, limit: 100 },
      ],
      symbols: [sym, { symbol: 'BAD' }, { ...sym, symbol: 'X', filters: [] }],
    })
    expect(info.symbols).toEqual([
      {
        symbol: 'BTCUSDT',
        baseAsset: 'BTC',
        quoteAsset: 'USDT',
        status: 'TRADING',
        tickSize: 0.01,
      },
    ])
    expect(info.weightLimit1m).toBe(6000)
  })

  it('rejects bodies without a symbols array', () => {
    expectParse(() => parseExchangeInfo([]))
    expectParse(() => parseExchangeInfo({ symbols: 'no' }))
    expect(parseExchangeInfo({ symbols: [] }).weightLimit1m).toBeUndefined()
  })

  it('rejects negative tickSize', () => {
    expect(
      parseSymbolInfo({ ...sym, filters: [{ filterType: 'PRICE_FILTER', tickSize: '-1' }] }),
    ).toBeNull()
  })
})

describe('24hr tickers', () => {
  const raw = {
    symbol: 'BTCUSDT',
    lastPrice: '110.0',
    openPrice: '100.0',
    highPrice: '120',
    lowPrice: '90',
    volume: '5',
    quoteVolume: '500',
    closeTime: 1792972799999,
  }

  it('maps a ticker with changePct and closeTime in seconds', () => {
    expect(parseTicker24hr(raw)).toEqual({
      symbol: 'BTCUSDT',
      last: 110,
      open: 100,
      high: 120,
      low: 90,
      volume: 5,
      quoteVolume: 500,
      changePct: 10,
      time: 1792972799,
    })
  })

  it('changePct is 0 when open is 0', () => {
    expect(changePct(5, 0)).toBe(0)
    expect(parseTicker24hr({ ...raw, openPrice: '0' })?.changePct).toBe(0)
  })

  it('accepts array or single-object bodies and skips malformed entries', () => {
    expect(parseTickers24hr([raw, { symbol: 'X' }])).toHaveLength(1)
    expect(parseTickers24hr(raw)).toHaveLength(1)
    expectParse(() => parseTickers24hr('nope'))
  })
})

describe('helpers', () => {
  it('parseRetryAfter', () => {
    expect(parseRetryAfter('30')).toBe(30)
    expect(parseRetryAfter(null)).toBeUndefined()
    expect(parseRetryAfter('')).toBeUndefined()
    expect(parseRetryAfter('soon')).toBeUndefined()
  })

  it('parseRetryAfter treats 0 (and fractions) as at least 1 s', () => {
    expect(parseRetryAfter('0')).toBe(1)
    expect(parseRetryAfter('0.2')).toBe(1)
    expect(parseRetryAfter('2.5')).toBe(3)
  })

  it('parseErrorBody', () => {
    expect(parseErrorBody('{"code":-1121,"msg":"Invalid symbol."}')).toEqual({
      code: -1121,
      msg: 'Invalid symbol.',
    })
    expect(parseErrorBody('<html>')).toEqual({})
  })
})
