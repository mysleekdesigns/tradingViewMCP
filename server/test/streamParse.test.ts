import { describe, expect, it } from 'vitest'
import {
  changePct,
  decodeMessage,
  parseKlineMessage,
  parseMiniTickerMessage,
} from '../src/upstream/streamParse.ts'

// Real-shaped Binance kline push (prices/volumes as strings, times in ms).
const kline = {
  e: 'kline',
  E: 1_700_000_123_456,
  s: 'BTCUSDT',
  k: {
    t: 1_700_000_100_000,
    T: 1_700_000_159_999,
    s: 'BTCUSDT',
    i: '1m',
    f: 100,
    L: 200,
    o: '37000.10',
    c: '37010.50',
    h: '37020.00',
    l: '36990.00',
    v: '12.345',
    n: 101,
    x: false,
    q: '456789.12',
    V: '6.0',
    Q: '222222.2',
    B: '0',
  },
}

const miniTickers = [
  {
    e: '24hrMiniTicker',
    E: 1_700_000_200_999,
    s: 'BTCUSDT',
    c: '110.0',
    o: '100.0',
    h: '120.0',
    l: '90.0',
    v: '5.5',
    q: '600.5',
  },
  {
    e: '24hrMiniTicker',
    E: 1_700_000_201_000,
    s: 'ZEROUSDT',
    c: '1',
    o: '0',
    h: '1',
    l: '0',
    v: '1',
    q: '1',
  },
]

describe('parseKlineMessage', () => {
  it('parses a forming kline into a numeric candle in UNIX seconds', () => {
    expect(parseKlineMessage(JSON.stringify(kline), { symbol: 'BTCUSDT', interval: '1m' })).toEqual(
      {
        candle: {
          time: 1_700_000_100,
          open: 37000.1,
          high: 37020,
          low: 36990,
          close: 37010.5,
          volume: 12.345,
        },
        closed: false,
      },
    )
  })

  it('reports closed only when k.x === true', () => {
    const closed = { ...kline, k: { ...kline.k, x: true } }
    expect(parseKlineMessage(JSON.stringify(closed))?.closed).toBe(true)
    const truthy = { ...kline, k: { ...kline.k, x: 'true' } }
    expect(parseKlineMessage(JSON.stringify(truthy))?.closed).toBe(false)
  })

  it('filters by symbol and interval', () => {
    const raw = JSON.stringify(kline)
    expect(parseKlineMessage(raw, { symbol: 'ETHUSDT', interval: '1m' })).toBeNull()
    expect(parseKlineMessage(raw, { symbol: 'BTCUSDT', interval: '5m' })).toBeNull()
    expect(parseKlineMessage(raw, { symbol: 'btcusdt', interval: '1m' })).not.toBeNull()
  })

  it('unwraps the combined-stream envelope and decodes binary frames', () => {
    const wrapped = JSON.stringify({ stream: 'btcusdt@kline_1m', data: kline })
    expect(parseKlineMessage(wrapped)?.candle.time).toBe(1_700_000_100)
    const bytes = new TextEncoder().encode(JSON.stringify(kline))
    expect(parseKlineMessage(bytes)?.candle.close).toBe(37010.5)
    expect(parseKlineMessage(bytes.buffer)?.candle.close).toBe(37010.5)
  })

  it('ignores malformed messages without throwing', () => {
    expect(parseKlineMessage('not json')).toBeNull()
    expect(parseKlineMessage('null')).toBeNull()
    expect(parseKlineMessage(JSON.stringify({ e: 'trade' }))).toBeNull()
    expect(parseKlineMessage(JSON.stringify({ e: 'kline', k: 'x' }))).toBeNull()
    expect(parseKlineMessage(JSON.stringify({ ...kline, k: { ...kline.k, o: 'abc' } }))).toBeNull()
    expect(parseKlineMessage(JSON.stringify({ ...kline, k: { ...kline.k, v: '' } }))).toBeNull()
    expect(parseKlineMessage(JSON.stringify({ ...kline, k: { ...kline.k, t: null } }))).toBeNull()
  })
})

describe('parseMiniTickerMessage', () => {
  it('parses an array push with changePct and seconds', () => {
    const out = parseMiniTickerMessage(JSON.stringify(miniTickers))
    expect(out).toEqual([
      {
        symbol: 'BTCUSDT',
        last: 110,
        open: 100,
        high: 120,
        low: 90,
        volume: 5.5,
        quoteVolume: 600.5,
        changePct: 10,
        time: 1_700_000_200,
      },
      {
        symbol: 'ZEROUSDT',
        last: 1,
        open: 0,
        high: 1,
        low: 0,
        volume: 1,
        quoteVolume: 1,
        changePct: 0,
        time: 1_700_000_201,
      },
    ])
  })

  it('skips malformed elements and returns [] for non-arrays', () => {
    const mixed = [miniTickers[0], { e: '24hrMiniTicker', s: 'BAD', c: 'x' }, 7, null]
    expect(parseMiniTickerMessage(JSON.stringify(mixed)).map((t) => t.symbol)).toEqual(['BTCUSDT'])
    expect(parseMiniTickerMessage('{}')).toEqual([])
    expect(parseMiniTickerMessage('garbage')).toEqual([])
    expect(
      parseMiniTickerMessage(JSON.stringify({ stream: '!miniTicker@arr', data: miniTickers })),
    ).toHaveLength(2)
  })
})

describe('helpers', () => {
  it('changePct', () => {
    expect(changePct(110, 100)).toBeCloseTo(10)
    expect(changePct(90, 100)).toBeCloseTo(-10)
    expect(changePct(5, 0)).toBe(0)
  })

  it('decodeMessage passes parsed values through and returns undefined on bad JSON', () => {
    const obj = { a: 1 }
    expect(decodeMessage(obj)).toBe(obj)
    expect(decodeMessage('{')).toBeUndefined()
  })
})
