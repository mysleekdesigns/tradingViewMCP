import { describe, expect, it } from 'vitest'
import { INTERVALS, type Timeframe } from '../types'
import {
  PRESET_TIMEFRAMES,
  baseFor,
  bucketStart,
  isNativeInterval,
  isSecondsTimeframe,
  parseTimeframe,
  timeframeLabel,
  timeframeName,
  timeframeSeconds,
} from './timeframes'

describe('parseTimeframe', () => {
  it('accepts valid input, trims, and lower-cases units', () => {
    expect(parseTimeframe('5s')).toBe('5s')
    expect(parseTimeframe(' 3m ')).toBe('3m')
    expect(parseTimeframe('2H')).toBe('2h')
    expect(parseTimeframe('1D')).toBe('1d')
    expect(parseTimeframe('1W')).toBe('1w')
    expect(parseTimeframe('15S')).toBe('15s')
    expect(parseTimeframe('3 d')).toBe('3d')
  })

  it('reads a bare number as minutes', () => {
    expect(parseTimeframe('15')).toBe('15m')
    expect(parseTimeframe('240')).toBe('4h')
  })

  it('canonicalises whole larger units', () => {
    expect(parseTimeframe('60s')).toBe('1m')
    expect(parseTimeframe('120m')).toBe('2h')
    expect(parseTimeframe('90m')).toBe('90m')
    expect(parseTimeframe('48h')).toBe('2d')
    expect(parseTimeframe('72h')).toBe('3d')
    expect(parseTimeframe('14d')).toBe('2w')
    expect(parseTimeframe('7d')).toBe('1w')
  })

  it('rejects malformed, zero, fractional, negative, and month input', () => {
    for (const s of ['', 'm', '0m', '1.5h', '-1h', '5x', '1M', '3mo', 'h1', '1e3m', '5 5m']) {
      expect(parseTimeframe(s), s).toBeNull()
    }
  })

  it('enforces the limits', () => {
    expect(parseTimeframe('7s')).toBeNull() // seconds must divide 60
    expect(parseTimeframe('45s')).toBeNull()
    expect(parseTimeframe('30s')).toBe('30s')
    expect(parseTimeframe('720m')).toBe('12h')
    expect(parseTimeframe('719m')).toBe('719m')
    expect(parseTimeframe('721m')).toBeNull()
    expect(parseTimeframe('167h')).toBe('167h')
    expect(parseTimeframe('169h')).toBeNull()
    expect(parseTimeframe('365d')).toBe('365d')
    expect(parseTimeframe('366d')).toBeNull()
    expect(parseTimeframe('52w')).toBe('52w')
    expect(parseTimeframe('53w')).toBeNull()
    expect(parseTimeframe('99999999999999999999m')).toBeNull()
  })

  it('round-trips every preset and native interval', () => {
    for (const tf of [...PRESET_TIMEFRAMES, ...INTERVALS]) expect(parseTimeframe(tf)).toBe(tf)
  })
})

describe('timeframe helpers', () => {
  it('timeframeSeconds', () => {
    expect(timeframeSeconds('1s')).toBe(1)
    expect(timeframeSeconds('30s')).toBe(30)
    expect(timeframeSeconds('3m')).toBe(180)
    expect(timeframeSeconds('2h')).toBe(7_200)
    expect(timeframeSeconds('3d')).toBe(259_200)
    expect(timeframeSeconds('1w')).toBe(604_800)
    expect(() => timeframeSeconds('1.5m' as Timeframe)).toThrow()
  })

  it('isNativeInterval / isSecondsTimeframe', () => {
    for (const iv of INTERVALS) expect(isNativeInterval(iv)).toBe(true)
    expect(isNativeInterval('3m')).toBe(false)
    expect(isNativeInterval('1s')).toBe(false)
    expect(isSecondsTimeframe('1s')).toBe(true)
    expect(isSecondsTimeframe('15s')).toBe(true)
    expect(isSecondsTimeframe('1m')).toBe(false)
  })

  it('baseFor picks the longest native interval that tiles the timeframe', () => {
    for (const iv of INTERVALS) expect(baseFor(iv)).toBe(iv)
    expect(baseFor('1s')).toBe('1s')
    expect(baseFor('30s')).toBe('1s')
    expect(baseFor('3m')).toBe('1m')
    expect(baseFor('7m')).toBe('1m')
    expect(baseFor('10m')).toBe('5m')
    expect(baseFor('30m')).toBe('15m')
    expect(baseFor('45m')).toBe('15m')
    expect(baseFor('90m')).toBe('15m')
    expect(baseFor('2h')).toBe('1h')
    expect(baseFor('6h')).toBe('1h')
    expect(baseFor('8h')).toBe('4h')
    expect(baseFor('12h')).toBe('4h')
    expect(baseFor('2d')).toBe('1d')
    expect(baseFor('3d')).toBe('1d')
    expect(baseFor('2w')).toBe('1w')
  })

  it('baseFor bars always tile the bucket grid', () => {
    const custom: Timeframe[] = ['3m', '7m', '90m', '2h', '5h', '12h', '2d', '3d', '5d', '2w', '4w']
    for (const tf of custom) {
      const base = baseFor(tf) as Timeframe
      const len = timeframeSeconds(tf)
      expect(len % timeframeSeconds(base), tf).toBe(0)
      // Every bucket start is also a base bar start.
      for (const t of [0, 1_700_000_123, 1_792_713_600, 1_800_000_000]) {
        const b = bucketStart(t, tf)
        expect(bucketStart(b, base), `${tf} @ ${t}`).toBe(b)
      }
    }
  })

  it('labels and names', () => {
    expect(timeframeLabel('30s')).toBe('30s')
    expect(timeframeLabel('5m')).toBe('5m')
    expect(timeframeLabel('2h')).toBe('2h')
    expect(timeframeLabel('1d')).toBe('1D')
    expect(timeframeLabel('3d')).toBe('3D')
    expect(timeframeLabel('1w')).toBe('1W')
    expect(timeframeName('1s')).toBe('1 second')
    expect(timeframeName('30s')).toBe('30 seconds')
    expect(timeframeName('1m')).toBe('1 minute')
    expect(timeframeName('2h')).toBe('2 hours')
    expect(timeframeName('1d')).toBe('1 day')
    expect(timeframeName('1w')).toBe('1 week')
  })
})

describe('bucketStart', () => {
  it('aligns seconds, minutes, and hours to the epoch', () => {
    expect(bucketStart(1_700_000_007, '5s')).toBe(1_700_000_005)
    expect(bucketStart(1_700_000_005, '5s')).toBe(1_700_000_005)
    expect(bucketStart(1_700_000_000 + 179, '3m') % 180).toBe(0)
    // Binance 2h/6h/12h open times observed live (2026-10-26), mod one day.
    expect(bucketStart(1_793_008_297, '2h') % 7_200).toBe(0)
    expect(bucketStart(1_793_008_297, '6h') % 21_600).toBe(0)
    expect(bucketStart(1_793_008_297, '12h') % 43_200).toBe(0)
  })

  it('anchors weeks on Monday like Binance 1w', () => {
    // 1792368000 is a live BTCUSDT 1w open time (Monday 00:00 UTC).
    expect(bucketStart(1_792_368_000, '1w')).toBe(1_792_368_000)
    expect(bucketStart(1_792_368_000 + 604_799, '1w')).toBe(1_792_368_000)
    expect(bucketStart(1_792_368_000 - 1, '1w')).toBe(1_792_368_000 - 604_800)
    expect(bucketStart(1_792_368_000, '2w') % 604_800).toBe(345_600)
  })

  it('matches Binance native 3d open times', () => {
    // Live BTCUSDT 3d opens (2026-10-26 and 2020): 1792713600 and 1599609600.
    expect(bucketStart(1_792_713_600, '3d')).toBe(1_792_713_600)
    expect(bucketStart(1_792_713_600 + 259_199, '3d')).toBe(1_792_713_600)
    expect(bucketStart(1_792_972_800, '3d')).toBe(1_792_972_800)
    expect(bucketStart(1_599_609_600 + 100_000, '3d')).toBe(1_599_609_600)
  })

  it('days align to midnight UTC', () => {
    expect(bucketStart(1_700_000_000, '1d')).toBe(1_699_920_000)
    expect(bucketStart(1_700_000_000, '2d') % 86_400).toBe(0)
  })
})
