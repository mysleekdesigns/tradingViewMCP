import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { toCandlestickData, toVolumeData } from './volume'

const candles: Candle[] = [
  { time: 1_700_000_000, open: 10, high: 12, low: 9, close: 11, volume: 100 }, // up
  { time: 1_700_003_600, open: 11, high: 11, low: 8, close: 9, volume: 250 }, // down
  { time: 1_700_007_200, open: 9, high: 10, low: 8, close: 9, volume: 0 }, // doji -> up
]
const palette = { up: 'UP', down: 'DOWN' }

describe('toVolumeData', () => {
  it('maps volume and colours bars by direction (doji counts as up)', () => {
    expect(toVolumeData(candles, palette)).toEqual([
      { time: 1_700_000_000, value: 100, color: 'UP' },
      { time: 1_700_003_600, value: 250, color: 'DOWN' },
      { time: 1_700_007_200, value: 0, color: 'UP' },
    ])
  })
  it('returns an empty array for no candles', () => {
    expect(toVolumeData([], palette)).toEqual([])
  })
})

describe('toCandlestickData', () => {
  it('keeps UNIX-seconds time and OHLC, drops volume', () => {
    expect(toCandlestickData(candles.slice(0, 1))).toEqual([
      { time: 1_700_000_000, open: 10, high: 12, low: 9, close: 11 },
    ])
  })
})
