import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { computeIndicator, defaultInputs } from './compute'
import { macdDef } from './macd'
import type { IndicatorValues } from './types'

/** Seeded PRNG so the random candles are the same on every run. */
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

const T0 = 1_700_000_000

function randomCandles(n: number, seed: number, start = 0, startPrice = 100): Candle[] {
  const rand = mulberry32(seed)
  const out: Candle[] = []
  let price = startPrice
  for (let i = 0; i < n; i++) {
    const open = price
    const close = Math.max(1, open * (1 + (rand() - 0.5) * 0.04))
    const high = Math.max(open, close) * (1 + rand() * 0.01)
    const low = Math.min(open, close) * (1 - rand() * 0.01)
    out.push({ time: T0 + (start + i) * 60, open, high, low, close, volume: 1 + rand() * 10 })
    price = close
  }
  return out
}

function fromCloses(closes: number[]): Candle[] {
  return closes.map((c, i) => ({
    time: T0 + i * 60,
    open: c,
    high: c,
    low: c,
    close: c,
    volume: 1,
  }))
}

/** Brute-force SMA-seeded EMA over an array that may start with undefined (warm-up) entries. */
function naiveEma(xs: (number | undefined)[], period: number): (number | undefined)[] {
  const out: (number | undefined)[] = xs.map(() => undefined)
  const first = xs.findIndex((v) => v !== undefined)
  if (first < 0) return out
  const alpha = 2 / (period + 1)
  const seedIdx = first + period - 1
  if (seedIdx >= xs.length) return out
  let s = 0
  for (let j = first; j <= seedIdx; j++) s += xs[j] as number
  let prev = s / period
  out[seedIdx] = prev
  for (let i = seedIdx + 1; i < xs.length; i++) {
    prev = alpha * (xs[i] as number) + (1 - alpha) * prev
    out[i] = prev
  }
  return out
}

function points(candles: Candle[], xs: (number | undefined)[]): { time: number; value: number }[] {
  return xs.flatMap((v, i) => (v === undefined ? [] : [{ time: candles[i].time, value: v }]))
}

describe('macdDef', () => {
  it('matches a hand-calculated fast 2 / slow 3 / signal 2 example', () => {
    // closes 1, 2, 3, 5, 4, 6, 8, 7
    // EMA2 (alpha 2/3): idx1 1.5, idx2 2.5, idx3 25/6, idx4 73/18, idx5 289/54, idx6 1153/162,
    //   idx7 3421/486
    // EMA3 (alpha 1/2): idx2 2, idx3 3.5, idx4 3.75, idx5 4.875, idx6 6.4375, idx7 6.71875
    // macd: idx2 1/2, idx3 2/3, idx4 11/36, idx5 103/216, idx6 881/1296, idx7 2491/7776
    // signal (EMA2 of macd, seed (1/2 + 2/3)/2): idx3 7/12, idx4 43/108, idx5 73/162,
    //   idx6 391/648, idx7 4837/11664
    const candles = fromCloses([1, 2, 3, 5, 4, 6, 8, 7])
    const out = computeIndicator(macdDef, candles, { fast: 2, slow: 3, signal: 2 })
    const macd = [1 / 2, 2 / 3, 11 / 36, 103 / 216, 881 / 1296, 2491 / 7776]
    const signal = [7 / 12, 43 / 108, 73 / 162, 391 / 648, 4837 / 11664]
    expect(out.macd.map((p) => p.time)).toEqual(candles.slice(2).map((c) => c.time))
    expect(out.signal.map((p) => p.time)).toEqual(candles.slice(3).map((c) => c.time))
    out.macd.forEach((p, i) => expect(p.value).toBeCloseTo(macd[i], 12))
    out.signal.forEach((p, i) => expect(p.value).toBeCloseTo(signal[i], 12))
    out.hist.forEach((p, i) => expect(p.value).toBeCloseTo(macd[i + 1] - signal[i], 12))
  })

  it('matches a brute-force reference over 300 random candles', () => {
    // Tolerance 1e-9: the reference updates with alpha*x + (1-alpha)*prev rather than
    // prev + alpha*(x-prev), so rounding differs in the last few bits.
    const candles = randomCandles(300, 42)
    const closes = candles.map((c) => c.close)
    for (const [fast, slow, sig] of [
      [12, 26, 9],
      [3, 10, 4],
      [20, 8, 5],
    ]) {
      const f = naiveEma(closes, fast)
      const s = naiveEma(closes, slow)
      const m = closes.map((_, i) => {
        const a = f[i]
        const b = s[i]
        return a === undefined || b === undefined ? undefined : a - b
      })
      const sg = naiveEma(m, sig)
      const h = m.map((v, i) => {
        const g = sg[i]
        return v === undefined || g === undefined ? undefined : v - g
      })
      const out = computeIndicator(macdDef, candles, { fast, slow, signal: sig })
      for (const [key, ref] of [
        ['macd', m],
        ['signal', sg],
        ['hist', h],
      ] as const) {
        const expected = points(candles, ref)
        expect(out[key].length).toBe(expected.length)
        out[key].forEach((p, i) => {
          expect(p.time).toBe(expected[i].time)
          expect(Math.abs(p.value - expected[i].value)).toBeLessThan(1e-9)
        })
      }
    }
  })

  it('macd starts at max(fast, slow) - 1 and signal/hist signal - 1 bars later', () => {
    const candles = randomCandles(100, 7)
    for (const [fast, slow, sig] of [
      [12, 26, 9],
      [30, 10, 5],
      [1, 1, 1],
    ]) {
      const out = computeIndicator(macdDef, candles, { fast, slow, signal: sig })
      const m0 = Math.max(fast, slow) - 1
      expect(out.macd[0].time).toBe(candles[m0].time)
      expect(out.signal[0].time).toBe(candles[m0 + sig - 1].time)
      expect(out.hist[0].time).toBe(candles[m0 + sig - 1].time)
    }
    const def = computeIndicator(macdDef, candles, defaultInputs(macdDef))
    expect(def.macd[0].time).toBe(candles[25].time)
    expect(def.signal[0].time).toBe(candles[33].time)
  })

  it('hist equals macd - signal exactly at every bar', () => {
    const stepper = macdDef.createStepper({ fast: 12, slow: 26, signal: 9 }, 'close')
    let seen = 0
    for (const c of randomCandles(300, 3)) {
      const v = stepper.next(c)
      if (v.signal !== undefined) {
        seen++
        expect(v.macd).toBeDefined()
        expect(v.hist).toBe((v.macd as number) - v.signal)
      } else {
        expect(v.hist).toBeUndefined()
      }
    }
    expect(seen).toBe(300 - 33)
  })

  it('has no look-ahead: changing bars after t leaves outputs up to t bit-identical', () => {
    const candles = randomCandles(200, 11)
    const inputs = { fast: 12, slow: 26, signal: 9 }
    const full = computeIndicator(macdDef, candles, inputs)
    for (const t of [25, 33, 100, 198]) {
      const perturbed = [...candles.slice(0, t + 1), ...randomCandles(199 - t, 999 + t, t + 1, 50)]
      const out = computeIndicator(macdDef, perturbed, inputs)
      const cut = candles[t].time
      for (const key of ['macd', 'signal', 'hist']) {
        expect(out[key].filter((p) => p.time <= cut)).toStrictEqual(
          full[key].filter((p) => p.time <= cut),
        )
      }
    }
  })

  it('clone replays identically and is independent of the original', () => {
    const candles = randomCandles(120, 5)
    const other = randomCandles(60, 77, 30, 80)
    const inputs = { fast: 12, slow: 26, signal: 9 }
    // k = 30 is inside the signal warm-up, so the clone must copy the EMA seed buffers too.
    const a = macdDef.createStepper(inputs, 'close')
    for (const c of candles.slice(0, 30)) a.next(c)
    const b = a.clone()
    const outA: IndicatorValues[] = []
    const outB: IndicatorValues[] = []
    for (const c of candles.slice(30)) {
      outA.push(a.next(c))
      outB.push(b.next(c))
    }
    expect(outB).toStrictEqual(outA)

    const orig = macdDef.createStepper(inputs, 'close')
    for (const c of candles.slice(0, 30)) orig.next(c)
    const diverged = orig.clone()
    for (const c of other) diverged.next(c)
    const after = candles.slice(30).map((c) => orig.next(c))
    expect(after).toStrictEqual(outA)
  })
})
