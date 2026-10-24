import { describe, expect, it } from 'vitest'
import { backoffDelay, parseKlineMessage } from './binanceStream'

function klineMsg(overrides: Record<string, unknown> = {}, top: Record<string, unknown> = {}) {
  return {
    e: 'kline',
    E: 1700000012345,
    s: 'BTCUSDT',
    k: {
      t: 1700000040000,
      T: 1700000099999,
      s: 'BTCUSDT',
      i: '1m',
      o: '37000.10',
      c: '37050.25',
      h: '37100.50',
      l: '36950.00',
      v: '12.5',
      n: 42,
      x: false,
      q: '463000.1',
      ...overrides,
    },
    ...top,
  }
}

describe('parseKlineMessage', () => {
  it('parses a forming kline from a JSON string, ms -> s, strings -> numbers', () => {
    const out = parseKlineMessage(JSON.stringify(klineMsg()))
    expect(out).toEqual({
      candle: {
        time: 1700000040,
        open: 37000.1,
        high: 37100.5,
        low: 36950,
        close: 37050.25,
        volume: 12.5,
      },
      closed: false,
    })
  })

  it('marks closed only when k.x === true', () => {
    expect(parseKlineMessage(klineMsg({ x: true }))?.closed).toBe(true)
    expect(parseKlineMessage(klineMsg({ x: 'true' }))?.closed).toBe(false)
    expect(parseKlineMessage(klineMsg({ x: 1 }))?.closed).toBe(false)
  })

  it('floors a non-whole-second open time', () => {
    expect(parseKlineMessage(klineMsg({ t: 1700000040999 }))?.candle.time).toBe(1700000040)
  })

  it('unwraps the combined-stream envelope', () => {
    const out = parseKlineMessage({ stream: 'btcusdt@kline_1m', data: klineMsg() })
    expect(out?.candle.close).toBe(37050.25)
  })

  it.each([
    ['invalid JSON', '{not json'],
    ['null', null],
    ['number', 42],
    ['array', [1, 2]],
    ['non-kline event', { e: 'trade', s: 'BTCUSDT', p: '1' }],
    ['missing k', { e: 'kline', s: 'BTCUSDT' }],
    ['empty price', klineMsg({ o: '' })],
    ['NaN price', klineMsg({ c: 'abc' })],
    ['Infinity volume', klineMsg({ v: 'Infinity' })],
    ['missing high', klineMsg({ h: undefined })],
    ['object time', klineMsg({ t: {} })],
  ])('returns null for %s', (_name, data) => {
    expect(parseKlineMessage(data)).toBeNull()
  })

  it('filters by expected symbol and interval', () => {
    const expected = { symbol: 'btcusdt', interval: '1m' }
    expect(parseKlineMessage(klineMsg(), expected)).not.toBeNull()
    expect(parseKlineMessage(klineMsg({ s: 'ETHUSDT' }), expected)).toBeNull()
    expect(parseKlineMessage(klineMsg({ i: '5m' }), expected)).toBeNull()
    expect(parseKlineMessage(klineMsg({ s: undefined }), expected)).toBeNull()
  })
})

describe('backoffDelay', () => {
  it('grows exponentially from 1 s at the top of the jitter range', () => {
    const max = () => 1
    expect([0, 1, 2, 3, 4].map((a) => backoffDelay(a, max))).toEqual([
      1000, 2000, 4000, 8000, 16000,
    ])
  })

  it('caps at 30 s, even for huge attempts', () => {
    expect(backoffDelay(5, () => 1)).toBe(30000)
    expect(backoffDelay(1000, () => 1)).toBe(30000)
    expect(backoffDelay(Number.MAX_SAFE_INTEGER, () => 0)).toBe(15000)
  })

  it('jitters within [base/2, base]', () => {
    for (let a = 0; a < 8; a++) {
      const base = Math.min(30000, 1000 * 2 ** a)
      expect(backoffDelay(a, () => 0)).toBe(base / 2)
      for (let i = 0; i < 50; i++) {
        const d = backoffDelay(a)
        expect(d).toBeGreaterThanOrEqual(base / 2)
        expect(d).toBeLessThanOrEqual(base)
      }
    }
  })

  it('treats negative / NaN attempts as 0 and clamps bad rand output', () => {
    expect(backoffDelay(-3, () => 1)).toBe(1000)
    expect(backoffDelay(Number.NaN, () => 1)).toBe(1000)
    expect(backoffDelay(0, () => 5)).toBe(1000)
    expect(backoffDelay(0, () => -5)).toBe(500)
  })
})
