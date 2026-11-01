import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { toLinePoint } from './chartTypes'
import {
  toCandlestickData,
  toCandlestickPoint,
  toCloseLinePoint,
  toLineData,
  toTime,
  toVolumeData,
  toVolumePoint,
} from './points'
import {
  toCandlestickPoint as seriesSyncCandle,
  toVolumePoint as seriesSyncVolume,
} from './seriesSync'
import { toCandlestickData as volumeCandles, toVolumeData as volumeData } from './volume'

const c = (time: number, open: number, close: number, volume = 5): Candle => ({
  time,
  open,
  high: Math.max(open, close) + 1,
  low: Math.min(open, close) - 1,
  close,
  volume,
})
const candles = [c(60, 10, 12), c(120, 12, 11, 7), c(180, 11, 11, 0)]
const palette = { up: 'U', down: 'D' }

describe('points: one candle → chart point mapper set', () => {
  it('toTime keeps the number unchanged', () => {
    expect(toTime(1_700_000_000)).toBe(1_700_000_000)
    expect(Object.is(toTime(-0), -0)).toBe(true)
    expect(toTime(Number.NaN)).toBeNaN()
  })

  it('candlestick points carry OHLC only, in time/open/high/low/close key order', () => {
    const p = toCandlestickPoint(candles[0])
    expect(p).toStrictEqual({ time: 60, open: 10, high: 13, low: 9, close: 12 })
    expect(Object.keys(p)).toEqual(['time', 'open', 'high', 'low', 'close'])
    // A Heikin-Ashi bar (or any bar with extra fields) maps the same way.
    const ha = { ...candles[1], extra: 1 }
    expect(toCandlestickPoint(ha)).toStrictEqual({
      time: 120,
      open: 12,
      high: 13,
      low: 10,
      close: 11,
    })
    expect(toCandlestickData(candles)).toStrictEqual(candles.map(toCandlestickPoint))
  })

  it('close line points and generic value points', () => {
    expect(toCloseLinePoint(candles[1])).toStrictEqual({ time: 120, value: 11 })
    expect(Object.keys(toCloseLinePoint(candles[1]))).toEqual(['time', 'value'])
    expect(toLineData({ time: 60, value: 1.5 })).toStrictEqual({ time: 60, value: 1.5 })
    // Extra fields on the source point are not copied.
    const src = { time: 60, value: 2, label: 'x' }
    expect(toLineData(src)).toStrictEqual({ time: 60, value: 2 })
  })

  it('volume points: up on close >= open (doji up), down otherwise, time/value/color order', () => {
    expect(toVolumeData(candles, palette)).toStrictEqual([
      { time: 60, value: 5, color: 'U' },
      { time: 120, value: 7, color: 'D' },
      { time: 180, value: 0, color: 'U' },
    ])
    expect(Object.keys(toVolumePoint(candles[0], palette))).toEqual(['time', 'value', 'color'])
    expect(toVolumeData([], palette)).toEqual([])
  })

  it('the old module names re-export the same functions', () => {
    expect(seriesSyncCandle).toBe(toCandlestickPoint)
    expect(seriesSyncVolume).toBe(toVolumePoint)
    expect(volumeCandles).toBe(toCandlestickData)
    expect(volumeData).toBe(toVolumeData)
    expect(toLinePoint).toBe(toCloseLinePoint)
  })
})
