import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { atrDef, trueRange } from './atr'
import { computeIndicator, defaultInputs } from './compute'
import { getIndicatorDef } from './registry'
import { IndicatorRunner } from './runner'
import type { IndicatorValues } from './types'

const T0 = 1_700_000_000
const STEP = 3_600

function mulberry32(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function randomCandles(n: number, seed: number, start = 0, startPrice = 100): Candle[] {
  const rand = mulberry32(seed)
  const out: Candle[] = []
  let price = startPrice
  for (let i = 0; i < n; i++) {
    // Open away from the previous close, so |high - prevClose| and |low - prevClose| matter.
    const open = price * (1 + (rand() - 0.5) * 0.02)
    const close = Math.max(1, open * (1 + (rand() - 0.5) * 0.04))
    const high = Math.max(open, close) * (1 + rand() * 0.01)
    const low = Math.min(open, close) * (1 - rand() * 0.01)
    out.push({ time: T0 + (start + i) * STEP, open, high, low, close, volume: 1 })
    price = close
  }
  return out
}

function bars(rows: readonly (readonly [number, number, number])[]): Candle[] {
  return rows.map(([high, low, close], i) => ({
    time: T0 + i * STEP,
    open: close,
    high,
    low,
    close,
    volume: 1,
  }))
}

/*
 * Period 3, by hand. TR = max(H - L, |H - prev C|, |L - prev C|).
 *
 * i   H     L     C     TR                                       ATR
 * 0   12    10    11    H - L = 2 (first bar: no previous close)  -
 * 1   13    11    12    max(2, 2, 0) = 2                          -
 * 2   20    18    19    max(2, |20-12|=8, |18-12|=6) = 8          (2 + 2 + 8) / 3 = 4
 * 3   19.5  17.5  18    max(2, 0.5, 1.5) = 2                      4 + (2 - 4)/3 = 10/3
 * 4   18.5  8     9     max(10.5, 0.5, 10) = 10.5                 10/3 + (21/2 - 10/3)/3 = 103/18
 * 5   11    10    10.5  max(1, |11-9|=2, |10-9|=1) = 2            103/18 + (2 - 103/18)/3 = 121/27
 * 6   6     4     5     max(2, |6-10.5|=4.5, |4-10.5|=6.5) = 6.5  121/27 + (13/2 - 121/27)/3 = 835/162
 *
 * Working for the last row: 13/2 = 351/54, 121/27 = 242/54, so (109/54)/3 = 109/162 and
 * 726/162 + 109/162 = 835/162.
 */
const HAND = bars([
  [12, 10, 11],
  [13, 11, 12],
  [20, 18, 19],
  [19.5, 17.5, 18],
  [18.5, 8, 9],
  [11, 10, 10.5],
  [6, 4, 5],
])
const HAND_ATR = [4, 10 / 3, 103 / 18, 121 / 27, 835 / 162]

/** Brute-force Wilder ATR with plain loops: SMA seed over the first `period` TRs, then RMA. */
function naiveAtr(candles: readonly Candle[], period: number): (number | undefined)[] {
  const out: (number | undefined)[] = candles.map(() => undefined)
  const trs = candles.map((c, i) => {
    if (i === 0) return c.high - c.low
    const pc = candles[i - 1].close
    return Math.max(c.high - c.low, Math.abs(c.high - pc), Math.abs(c.low - pc))
  })
  let atr = 0
  for (let i = period - 1; i < candles.length; i++) {
    if (i === period - 1) {
      let s = 0
      for (let j = 0; j < period; j++) s += trs[j]
      atr = s / period
    } else {
      atr = (atr * (period - 1) + trs[i]) / period
    }
    out[i] = atr
  }
  return out
}

describe('trueRange', () => {
  const c: Candle = { time: T0, open: 10, high: 12, low: 9, close: 11, volume: 1 }

  it('is high - low without a previous close', () => {
    expect(trueRange(c, undefined)).toBe(3)
  })

  it('picks whichever of the three ranges is largest', () => {
    expect(trueRange(c, 10)).toBe(3) // high - low
    expect(trueRange(c, 8)).toBe(4) // |12 - 8|
    expect(trueRange(c, 14)).toBe(5) // |9 - 14|
  })
})

describe('atrDef', () => {
  it('is registered as a pane indicator reading whole candles', () => {
    expect(getIndicatorDef('atr')).toBe(atrDef)
    expect(atrDef.placement).toBe('pane')
    expect(atrDef.source).toBeNull()
    expect(atrDef.lines.map((l) => l.key)).toEqual(['atr'])
    expect(defaultInputs(atrDef)).toEqual({ period: 14 })
  })

  it('matches the hand-worked period-3 example: SMA of the first 3 TRs, then Wilder', () => {
    // Tolerance 1e-12: the running form uses alpha = 1/3, inexact in binary, so the last few bits
    // can differ from the exact fractions.
    const out = computeIndicator(atrDef, HAND, { period: 3 })
    expect(out.atr.map((p) => p.time)).toEqual(HAND.slice(2).map((c) => c.time))
    out.atr.forEach((p, i) => {
      expect(Math.abs(p.value - HAND_ATR[i])).toBeLessThan(1e-12)
    })
    // The first value is the plain mean, exact.
    expect(out.atr[0].value).toBe(4)
  })

  it('matches a brute-force reference over 300 random candles', () => {
    // Tolerance 1e-9: the reference uses (prev*(n-1)+tr)/n instead of prev + (tr-prev)/n.
    const candles = randomCandles(300, 42)
    for (const period of [1, 2, 5, 14, 30]) {
      const ref = naiveAtr(candles, period)
      const expected = ref.flatMap((v, i) =>
        v === undefined ? [] : [{ time: candles[i].time, value: v }],
      )
      const out = computeIndicator(atrDef, candles, { period })
      expect(out.atr.length).toBe(expected.length)
      out.atr.forEach((p, i) => {
        expect(p.time).toBe(expected[i].time)
        expect(Math.abs(p.value - expected[i].value)).toBeLessThan(1e-9)
      })
    }
  })

  it('first value is at bar index `period - 1`', () => {
    const candles = randomCandles(100, 7)
    for (const period of [1, 3, 14]) {
      const out = computeIndicator(atrDef, candles, { period })
      expect(out.atr[0].time).toBe(candles[period - 1].time)
    }
    const def = computeIndicator(atrDef, candles, defaultInputs(atrDef))
    expect(def.atr[0].time).toBe(candles[13].time)
  })

  it('gives no point at all with fewer bars than the period', () => {
    expect(computeIndicator(atrDef, randomCandles(13, 3), { period: 14 }).atr).toEqual([])
  })

  it('has no look-ahead: changing bars after t leaves outputs up to t bit-identical', () => {
    const candles = randomCandles(200, 11)
    const full = computeIndicator(atrDef, candles, { period: 14 })
    for (const t of [13, 50, 120, 198]) {
      const later = randomCandles(199 - t, 999 + t, t + 1, 50)
      const out = computeIndicator(atrDef, [...candles.slice(0, t + 1), ...later], { period: 14 })
      const cut = candles[t].time
      expect(out.atr.filter((p) => p.time <= cut)).toStrictEqual(
        full.atr.filter((p) => p.time <= cut),
      )
    }
  })

  it('a live tail recompute through the runner matches a full recompute, bit for bit', () => {
    const candles = randomCandles(120, 21)
    const inputs = { period: 14 }
    const runner = new IndicatorRunner(atrDef, inputs, 'close')
    runner.reset(candles.slice(0, 80))
    for (let n = 80; n < candles.length; n++) {
      // The forming bar ticks twice, then closes as the bar in `candles`.
      const last = candles[n - 1]
      const ticked = { ...last, high: last.high * 1.01, close: last.close * 1.002 }
      const live = [...candles.slice(0, n - 1), ticked]
      runner.update(live, n - 1)
      expect(runner.output).toStrictEqual(computeIndicator(atrDef, live, inputs))
      runner.update(candles.slice(0, n), n - 1)
      runner.update(candles.slice(0, n + 1), n)
      expect(runner.output).toStrictEqual(computeIndicator(atrDef, candles.slice(0, n + 1), inputs))
    }
  })

  it('clone replays identically and is independent of the original', () => {
    const candles = randomCandles(120, 5)
    const other = randomCandles(60, 77, 60, 80)
    const a = atrDef.createStepper({ period: 14 }, 'close')
    for (const c of candles.slice(0, 60)) a.next(c)
    const b = a.clone()
    const diverged = a.clone()
    for (const c of other) diverged.next(c)
    const outA: IndicatorValues[] = []
    const outB: IndicatorValues[] = []
    for (const c of candles.slice(60)) {
      outA.push(a.next(c))
      outB.push(b.next(c))
    }
    expect(outB).toStrictEqual(outA)
  })
})
