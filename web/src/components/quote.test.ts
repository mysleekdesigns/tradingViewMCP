import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { formatUtc, formatUtcClock, summarizeQuote } from './quote'

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

  it('measures intraday change against the close 24h earlier', () => {
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

  it('reports unknown change when less than 24h of data is loaded', () => {
    const q = summarizeQuote(hourly([100, 101]), '1h')
    expect(q?.change).toBe('—')
    expect(q?.changePct).toBe('—')
    expect(q?.direction).toBe('flat')
  })

  it('measures daily-and-up intervals against the previous close', () => {
    const q = summarizeQuote(hourly([200, 190]), '1d')
    expect(q).toMatchObject({ change: '-10.00', changePct: '-5.00%', direction: 'down' })
    expect(q?.basis).toBe('vs prev close')
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
