import { describe, expect, it } from 'vitest'
import {
  alignOpenTime,
  formingOpenTime,
  lastClosedOpenTime,
  msToSec,
  WEEK_OFFSET_SEC,
} from '../src/time.ts'
import { INTERVAL_SECONDS, INTERVALS } from '../src/types.ts'

// 2026-10-21 13:37:42 UTC (a Wednesday).
const T = Date.UTC(2026, 9, 21, 13, 37, 42) / 1000

describe('alignOpenTime', () => {
  it('aligns intraday intervals to multiples of their length', () => {
    expect(alignOpenTime(T, '1m')).toBe(Date.UTC(2026, 9, 21, 13, 37) / 1000)
    expect(alignOpenTime(T, '5m')).toBe(Date.UTC(2026, 9, 21, 13, 35) / 1000)
    expect(alignOpenTime(T, '15m')).toBe(Date.UTC(2026, 9, 21, 13, 30) / 1000)
    expect(alignOpenTime(T, '1h')).toBe(Date.UTC(2026, 9, 21, 13) / 1000)
    expect(alignOpenTime(T, '4h')).toBe(Date.UTC(2026, 9, 21, 12) / 1000)
  })

  it('aligns 1d to UTC midnight', () => {
    expect(alignOpenTime(T, '1d')).toBe(Date.UTC(2026, 9, 21) / 1000)
  })

  it('aligns 1w to Monday 00:00 UTC', () => {
    const monday = Date.UTC(2026, 9, 19) / 1000
    expect(new Date(monday * 1000).getUTCDay()).toBe(1)
    expect(alignOpenTime(T, '1w')).toBe(monday)
    expect(alignOpenTime(monday, '1w')).toBe(monday)
    expect(alignOpenTime(monday - 1, '1w')).toBe(monday - 604800)
    // Known live BTCUSDT 1w open time from api.binance.us (ms 1792368000000).
    expect(alignOpenTime(1792368000 + 3 * 86400 + 5, '1w')).toBe(1792368000)
    expect(1792368000 % 604800).toBe(WEEK_OFFSET_SEC)
  })

  it('is idempotent and contains t for every interval', () => {
    for (const iv of INTERVALS) {
      const a = alignOpenTime(T, iv)
      expect(alignOpenTime(a, iv)).toBe(a)
      expect(a).toBeLessThanOrEqual(T)
      expect(T).toBeLessThan(a + INTERVAL_SECONDS[iv])
    }
  })

  it('floors fractional seconds', () => {
    expect(alignOpenTime(120.9, '1m')).toBe(120)
  })
})

describe('forming / last closed', () => {
  it('forming bar contains now; last closed is one bar earlier and closed', () => {
    const f = formingOpenTime(T, '1h')
    expect(f).toBe(Date.UTC(2026, 9, 21, 13) / 1000)
    const lc = lastClosedOpenTime(T, '1h')
    expect(lc).toBe(f - 3600)
    expect(lc + 3600).toBeLessThanOrEqual(T)
  })

  it('at an exact boundary the bar that just opened is forming', () => {
    const b = Date.UTC(2026, 9, 21, 14) / 1000
    expect(formingOpenTime(b, '1h')).toBe(b)
    expect(lastClosedOpenTime(b, '1h')).toBe(b - 3600)
  })

  it('msToSec floors', () => {
    expect(msToSec(1792368000999)).toBe(1792368000)
  })
})
