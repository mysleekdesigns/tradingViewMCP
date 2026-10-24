import { describe, expect, it } from 'vitest'
import { Ema, RollingWindow, Sma, rma } from './primitives'
import { priceOf } from './sources'

describe('RollingWindow', () => {
  it('keeps the last N values and computes population stdev', () => {
    const w = new RollingWindow(3)
    for (const v of [1, 2, 3, 4]) w.push(v)
    expect(w.values).toEqual([2, 3, 4])
    expect(w.mean()).toBe(3)
    // Population stdev of 2,3,4 = sqrt(2/3).
    expect(w.stdev()).toBeCloseTo(Math.sqrt(2 / 3), 12)
    expect(w.max()).toBe(4)
    expect(w.min()).toBe(2)
  })

  it('clone is independent', () => {
    const a = new RollingWindow(2)
    a.push(1)
    const b = a.clone()
    b.push(5)
    expect(a.values).toEqual([1])
    expect(b.values).toEqual([1, 5])
  })
})

describe('Sma', () => {
  it('warms up for period - 1 inputs', () => {
    const s = new Sma(3)
    expect([1, 2, 3, 4, 5].map((v) => s.next(v))).toEqual([undefined, undefined, 2, 3, 4])
  })
})

describe('Ema', () => {
  it('seeds with the SMA, then smooths with alpha 2/(n+1)', () => {
    const e = new Ema(3)
    const out = [2, 4, 6, 8, 10].map((v) => e.next(v))
    // Seed = (2+4+6)/3 = 4; alpha = 0.5: 4 + 0.5*(8-4) = 6; 6 + 0.5*(10-6) = 8.
    expect(out).toEqual([undefined, undefined, 4, 6, 8])
  })

  it('rma uses alpha 1/n', () => {
    const r = rma(2)
    // Seed (1+3)/2 = 2; then 2 + 0.5*(5-2) = 3.5.
    expect([1, 3, 5].map((v) => r.next(v))).toEqual([undefined, 2, 3.5])
  })

  it('clone replays identically', () => {
    const e = new Ema(2)
    e.next(1)
    e.next(2)
    const c = e.clone()
    expect(c.next(10)).toBe(e.next(10))
  })
})

describe('priceOf', () => {
  it('derives composite sources', () => {
    const c = { time: 0, open: 1, high: 4, low: 2, close: 3, volume: 0 }
    expect(priceOf(c, 'hl2')).toBe(3)
    expect(priceOf(c, 'hlc3')).toBe(3)
    expect(priceOf(c, 'ohlc4')).toBe(2.5)
  })
})
