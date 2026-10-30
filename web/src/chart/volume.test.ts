import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { toCandlestickData, toVolumeData, volumeColorsOf } from './volume'

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

describe('volumeColorsOf (one colours object per palette)', () => {
  it('returns the same object for the same palette and the palette volume colours', () => {
    const p = { volumeUp: 'U', volumeDown: 'D' }
    const a = volumeColorsOf(p)
    expect(a).toEqual({ up: 'U', down: 'D' })
    expect(volumeColorsOf(p)).toBe(a)
    expect(Object.isFrozen(a)).toBe(true)
  })

  it('a different palette object gets its own colours', () => {
    const a = volumeColorsOf({ volumeUp: 'U', volumeDown: 'D' })
    const b = volumeColorsOf({ volumeUp: 'U2', volumeDown: 'D2' })
    expect(b).toEqual({ up: 'U2', down: 'D2' })
    expect(b).not.toBe(a)
  })

  it('re-derives when a palette object was edited in place (never stale colours)', () => {
    const p = { volumeUp: 'U', volumeDown: 'D' }
    volumeColorsOf(p)
    p.volumeDown = 'X'
    expect(volumeColorsOf(p)).toEqual({ up: 'U', down: 'X' })
  })

  it('draws the same volume points as a fresh palette per point', () => {
    const p = { volumeUp: 'U', volumeDown: 'D' }
    expect(toVolumeData(candles, volumeColorsOf(p))).toEqual(
      toVolumeData(candles, { up: p.volumeUp, down: p.volumeDown }),
    )
  })
})
