import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { heikinAshi, heikinAshiBar, heikinAshiFrom } from './heikinAshi'

const H = 3600
const c = (i: number, open: number, high: number, low: number, close: number): Candle => ({
  time: i * H,
  open,
  high,
  low,
  close,
  volume: 1,
})

// Hand-calculated:
// 1: close=(10+12+9+11)/4=10.5   open=(10+11)/2=10.5            high=12  low=9
// 2: close=(11+14+10+13)/4=12    open=(10.5+10.5)/2=10.5        high=14  low=10
// 3: close=(13+13+8+9)/4=10.75   open=(10.5+12)/2=11.25         high=13  low=8
// 4: close=(9+10+7+8)/4=8.5      open=(11.25+10.75)/2=11        high=max(10,11,8.5)=11  low=7
const candles: Candle[] = [
  c(0, 10, 12, 9, 11),
  c(1, 11, 14, 10, 13),
  c(2, 13, 13, 8, 9),
  c(3, 9, 10, 7, 8),
]

describe('heikinAshi', () => {
  it('matches hand-calculated values', () => {
    expect(heikinAshi(candles)).toEqual([
      { time: 0, open: 10.5, high: 12, low: 9, close: 10.5 },
      { time: H, open: 10.5, high: 14, low: 10, close: 12 },
      { time: 2 * H, open: 11.25, high: 13, low: 8, close: 10.75 },
      { time: 3 * H, open: 11, high: 11, low: 7, close: 8.5 },
    ])
  })

  it('returns nothing for no candles', () => {
    expect(heikinAshi([])).toEqual([])
  })

  it('high/low always envelope the HA open and close', () => {
    for (const b of heikinAshi(candles)) {
      expect(b.high).toBeGreaterThanOrEqual(Math.max(b.open, b.close))
      expect(b.low).toBeLessThanOrEqual(Math.min(b.open, b.close))
    }
  })

  it('a live update of the last candle changes only the last HA bar', () => {
    const before = heikinAshi(candles)
    const ticked = [...candles.slice(0, 3), c(3, 9, 12, 7, 11.5)]
    const after = heikinAshi(ticked)
    expect(after.slice(0, 3)).toEqual(before.slice(0, 3))
    expect(after[3]).not.toEqual(before[3])
    expect(after[3]).toEqual(heikinAshiBar(ticked[3], before[2]))
  })

  it('heikinAshiFrom recomputes only the tail and agrees with a full recompute', () => {
    const before = heikinAshi(candles)
    const ticked = [...candles.slice(0, 3), c(3, 9, 12, 7, 11.5), c(4, 11.5, 13, 11, 12)]
    const tail = heikinAshiFrom(before, ticked, 3)
    expect(tail).toEqual(heikinAshi(ticked))
    // Prefix bars are reused, not recomputed.
    expect(tail[0]).toBe(before[0])
    expect(tail[2]).toBe(before[2])
  })

  it('is causal: perturbing a future candle never changes earlier HA bars', () => {
    const base = heikinAshi(candles)
    for (let t = 0; t < candles.length; t++) {
      const perturbed = candles.map((x, i) =>
        i > t
          ? { ...x, open: x.open * 3, high: x.high * 3, low: x.low * 0.1, close: x.close * 2 }
          : x,
      )
      expect(heikinAshi(perturbed).slice(0, t + 1)).toEqual(base.slice(0, t + 1))
    }
  })
})
