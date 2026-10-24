import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import {
  buildLegend,
  changePercent,
  formatChange,
  formatPrice,
  formatVolume,
  selectLegendIndex,
} from './legend.ts'

const candle = (time: number, open: number, close: number, volume = 1): Candle => ({
  time,
  open,
  high: Math.max(open, close) + 1,
  low: Math.min(open, close) - 1,
  close,
  volume,
})

describe('formatPrice', () => {
  it('uses 2 decimals at or above 1', () => {
    expect(formatPrice(67123.456)).toBe('67123.46')
    expect(formatPrice(1)).toBe('1.00')
  })
  it('uses 4 decimals between 0.01 and 1', () => {
    expect(formatPrice(0.123456)).toBe('0.1235')
  })
  it('uses 8 decimals below 0.01', () => {
    expect(formatPrice(0.0000123)).toBe('0.00001230')
  })
  it('handles zero and non-finite', () => {
    expect(formatPrice(0)).toBe('0.00')
    expect(formatPrice(Number.NaN)).toBe('—')
  })
})

describe('formatVolume', () => {
  it('abbreviates with K/M/B/T', () => {
    expect(formatVolume(999.5)).toBe('999.50')
    expect(formatVolume(1234)).toBe('1.23K')
    expect(formatVolume(4_560_000)).toBe('4.56M')
    expect(formatVolume(7_890_000_000)).toBe('7.89B')
    expect(formatVolume(2e12)).toBe('2.00T')
  })
  it('promotes to the next unit when rounding reaches 1000', () => {
    expect(formatVolume(999.999)).toBe('1.00K')
    expect(formatVolume(999_999)).toBe('1.00M')
  })
  it('handles zero and non-finite', () => {
    expect(formatVolume(0)).toBe('0.00')
    expect(formatVolume(Number.POSITIVE_INFINITY)).toBe('—')
  })
})

describe('change %', () => {
  it('computes percent change against a base', () => {
    expect(changePercent(110, 100)).toBeCloseTo(10)
    expect(changePercent(95, 100)).toBeCloseTo(-5)
  })
  it('returns null for a zero or missing base', () => {
    expect(changePercent(1, 0)).toBeNull()
    expect(changePercent(1, undefined)).toBeNull()
  })
  it('formats with an explicit sign', () => {
    expect(formatChange(1.234)).toBe('+1.23%')
    expect(formatChange(-0.5)).toBe('-0.50%')
    expect(formatChange(0)).toBe('0.00%')
    expect(formatChange(-0.001)).toBe('0.00%')
    expect(formatChange(null)).toBe('—')
  })
})

describe('selectLegendIndex', () => {
  const candles = [candle(100, 1, 2), candle(200, 2, 3), candle(300, 3, 1)]
  it('selects the hovered bar by time', () => {
    expect(selectLegendIndex(candles, 100)).toBe(0)
    expect(selectLegendIndex(candles, 200)).toBe(1)
    expect(selectLegendIndex(candles, 300)).toBe(2)
  })
  it('falls back to the last candle when the cursor leaves', () => {
    expect(selectLegendIndex(candles, undefined)).toBe(2)
  })
  it('falls back to the last candle for an unknown time', () => {
    expect(selectLegendIndex(candles, 150)).toBe(2)
    expect(selectLegendIndex(candles, 9999)).toBe(2)
  })
  it('returns -1 for no candles', () => {
    expect(selectLegendIndex([], 100)).toBe(-1)
    expect(selectLegendIndex([])).toBe(-1)
  })
})

describe('buildLegend', () => {
  const candles = [candle(100, 100, 100, 500), candle(200, 100, 110, 1500), candle(300, 110, 99)]
  it('measures change against the previous close', () => {
    const v = buildLegend(candles, 1)
    expect(v).toMatchObject({
      time: 200,
      open: '100.00',
      high: '111.00',
      low: '99.00',
      close: '110.00',
      volume: '1.50K',
      change: '+10.00%',
      direction: 'up',
    })
    expect(buildLegend(candles, 2)).toMatchObject({ change: '-10.00%', direction: 'down' })
  })
  it('uses the bar open for the first candle', () => {
    expect(buildLegend(candles, 0)).toMatchObject({ change: '0.00%', direction: 'flat' })
  })
  it('returns null for an out-of-range index', () => {
    expect(buildLegend(candles, -1)).toBeNull()
    expect(buildLegend(candles, 3)).toBeNull()
    expect(buildLegend([], 0)).toBeNull()
  })
  it('combined with selection, shows the last candle when nothing is hovered', () => {
    expect(buildLegend(candles, selectLegendIndex(candles))?.time).toBe(300)
  })
})
