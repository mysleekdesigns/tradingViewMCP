import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { formatUtc, formatUtcClock, sameQuote, summarizeQuote } from './quote'

const H = 3600

/** Hourly candles with the given closes, starting at 2023-11-14 22:00 UTC. */
function hourly(closes: number[]): Candle[] {
  return closes.map((close, i) => ({
    time: 1_700_000_000 - (1_700_000_000 % H) + i * H,
    open: close,
    high: close,
    low: close,
    close,
    volume: 1,
  }))
}

describe('summarizeQuote', () => {
  it('returns null for no candles', () => {
    expect(summarizeQuote([], '1h')).toBeNull()
  })

  it('measures intraday change against the price 24h earlier', () => {
    // 25 bars: index 0 is exactly 24h before the last bar.
    const closes = Array.from({ length: 25 }, (_, i) => 100 + i)
    closes[24] = 110
    const q = summarizeQuote(hourly(closes), '1h')
    expect(q).toEqual({
      price: '110.00',
      change: '+10.00',
      changePct: '+10.00%',
      direction: 'up',
      basis: '24h',
    })
  })

  it('uses the open of the bar starting exactly 24h before the last bar', () => {
    const candles = hourly(Array.from({ length: 25 }, () => 105))
    candles[0] = { ...candles[0], open: 100, close: 104 }
    candles[24] = { ...candles[24], close: 110 }
    // Base is bar 0's open (the price at last.time - 24h), not its close.
    expect(summarizeQuote(candles, '1h')).toMatchObject({ change: '+10.00', basis: '24h' })
  })

  it('uses the close of the bar before a gap at the 24h mark', () => {
    const candles = hourly(Array.from({ length: 26 }, () => 105))
    candles[0] = { ...candles[0], open: 90, close: 100 }
    candles[25] = { ...candles[25], close: 120 }
    // Drop bar 1, the one starting exactly 24h before the last bar.
    const gapped = [candles[0], ...candles.slice(2)]
    expect(summarizeQuote(gapped, '1h')).toMatchObject({
      change: '+20.00',
      changePct: '+20.00%',
      basis: '24h',
    })
  })

  it('falls back to the previous close when less than 24h is loaded', () => {
    for (const interval of ['1m', '1h'] as const) {
      const q = summarizeQuote(hourly([100, 101]), interval)
      expect(q).toMatchObject({ change: '+1.00', changePct: '+1.00%', basis: 'vs prev close' })
    }
  })

  it('reports unknown change for a single candle', () => {
    const q = summarizeQuote(hourly([100]), '1h')
    expect(q?.change).toBe('—')
    expect(q?.changePct).toBe('—')
    expect(q?.direction).toBe('flat')
  })

  it('formats the change at the price precision, signed after rounding', () => {
    expect(summarizeQuote(hourly([60_000, 60_000.5]), '1d')).toMatchObject({
      change: '+0.50',
      changePct: '0.00%',
      direction: 'up',
    })
    expect(summarizeQuote(hourly([60_000, 60_000.001]), '1d')).toMatchObject({
      change: '0.00',
      direction: 'flat',
    })
    expect(summarizeQuote(hourly([60_000, 59_999.999]), '1d')).toMatchObject({
      change: '0.00',
      direction: 'flat',
    })
  })

  it('measures daily-and-up intervals against the previous close', () => {
    const q = summarizeQuote(hourly([200, 190]), '1d')
    expect(q).toMatchObject({ change: '-10.00', changePct: '-5.00%', direction: 'down' })
    expect(q?.basis).toBe('vs prev close')
  })

  it('treats custom and seconds timeframes under a day as intraday (24h basis)', () => {
    const closes = Array.from({ length: 25 }, (_, i) => 100 + i)
    for (const tf of ['2h', '12h', '30s'] as const) {
      expect(summarizeQuote(hourly(closes), tf)?.basis).toBe('24h')
    }
  })

  it('compares custom day-and-up timeframes against the previous close', () => {
    expect(summarizeQuote(hourly([200, 190]), '3d')?.basis).toBe('vs prev close')
    expect(summarizeQuote(hourly([200, 190]), '2w')?.basis).toBe('vs prev close')
  })
})

describe('UTC formatting', () => {
  it('formats candle time in UTC minutes', () => {
    expect(formatUtc(1_700_000_000)).toBe('2023-11-14 22:13')
  })

  it('formats a clock time in UTC seconds', () => {
    expect(formatUtcClock(new Date(Date.UTC(2026, 9, 24, 14, 8, 31)))).toBe('14:08:31')
  })
})

describe('sameQuote', () => {
  it('is true for the same text and false when any shown field changes', () => {
    const a = summarizeQuote(hourly([100, 101]), '1h')
    const b = summarizeQuote(hourly([100, 101]), '1h')
    expect(a).not.toBe(b)
    expect(sameQuote(a, b)).toBe(true)
    expect(sameQuote(a, summarizeQuote(hourly([100, 102]), '1h'))).toBe(false)
    expect(sameQuote(a, a === null ? null : { ...a, basis: '24h' })).toBe(false)
    expect(sameQuote(null, null)).toBe(true)
    expect(sameQuote(a, null)).toBe(false)
    expect(sameQuote(null, b)).toBe(false)
  })
})
