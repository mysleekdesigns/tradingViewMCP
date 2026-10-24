import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { computeIndicator, defaultInputs } from './compute'
import { stochDef } from './stochastic'
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

function hlc(rows: [number, number, number][]): Candle[] {
  return rows.map(([high, low, close], i) => ({
    time: T0 + i * 60,
    open: close,
    high,
    low,
    close,
    volume: 1,
  }))
}

/** Brute-force stochastic with plain loops (flat window -> 50, as the indicator documents). */
function naiveStoch(
  candles: Candle[],
  kLength: number,
  kSmooth: number,
  dSmooth: number,
): { k: (number | undefined)[]; d: (number | undefined)[] } {
  const n = candles.length
  const raw: (number | undefined)[] = new Array<number | undefined>(n).fill(undefined)
  for (let i = kLength - 1; i < n; i++) {
    let hh = -Infinity
    let ll = Infinity
    for (let j = i - kLength + 1; j <= i; j++) {
      if (candles[j].high > hh) hh = candles[j].high
      if (candles[j].low < ll) ll = candles[j].low
    }
    raw[i] = hh === ll ? 50 : (100 * (candles[i].close - ll)) / (hh - ll)
  }
  const sma = (xs: (number | undefined)[], p: number): (number | undefined)[] =>
    xs.map((_, i) => {
      if (i - p + 1 < 0) return undefined
      let s = 0
      for (let j = i - p + 1; j <= i; j++) {
        const v = xs[j]
        if (v === undefined) return undefined
        s += v
      }
      return s / p
    })
  const k = sma(raw, kSmooth)
  return { k, d: sma(k, dSmooth) }
}

describe('stochDef', () => {
  it('matches a hand-calculated 3/1/2 example', () => {
    // (high, low, close): (10,8,9) (12,9,11) (11,7,8) (13,10,12) (14,11,14)
    // idx2: HH 12, LL 7 -> 100*(8-7)/5 = 20
    // idx3: HH 13, LL 7 -> 100*(12-7)/6 = 250/3; %D = (20 + 250/3)/2 = 155/3
    // idx4: HH 14, LL 7 -> 100*(14-7)/7 = 100; %D = (250/3 + 100)/2 = 275/3
    const candles = hlc([
      [10, 8, 9],
      [12, 9, 11],
      [11, 7, 8],
      [13, 10, 12],
      [14, 11, 14],
    ])
    const out = computeIndicator(stochDef, candles, { kLength: 3, kSmooth: 1, dSmooth: 2 })
    expect(out.k.map((p) => p.time)).toEqual(candles.slice(2).map((c) => c.time))
    expect(out.d.map((p) => p.time)).toEqual(candles.slice(3).map((c) => c.time))
    const k = [20, 250 / 3, 100]
    const d = [155 / 3, 275 / 3]
    out.k.forEach((p, i) => expect(p.value).toBeCloseTo(k[i], 12))
    out.d.forEach((p, i) => expect(p.value).toBeCloseTo(d[i], 12))
  })

  it('matches a brute-force reference over 300 random candles', () => {
    // Tolerance 1e-9: both sides sum the same values in the same order, so they should agree
    // to the last bit; the slack only guards against harmless reordering.
    const candles = randomCandles(300, 42)
    for (const [kLength, kSmooth, dSmooth] of [
      [14, 1, 3],
      [14, 3, 3],
      [5, 2, 7],
      [1, 1, 1],
    ]) {
      const ref = naiveStoch(candles, kLength, kSmooth, dSmooth)
      const out = computeIndicator(stochDef, candles, { kLength, kSmooth, dSmooth })
      for (const key of ['k', 'd'] as const) {
        const expected = ref[key].flatMap((v, i) =>
          v === undefined ? [] : [{ time: candles[i].time, value: v }],
        )
        expect(out[key].length).toBe(expected.length)
        out[key].forEach((p, i) => {
          expect(p.time).toBe(expected[i].time)
          expect(Math.abs(p.value - expected[i].value)).toBeLessThan(1e-9)
        })
      }
    }
  })

  it('%K starts at kLength + kSmooth - 2 and %D dSmooth - 1 bars later', () => {
    const candles = randomCandles(100, 7)
    for (const [kLength, kSmooth, dSmooth] of [
      [14, 3, 3],
      [5, 1, 1],
      [10, 4, 6],
    ]) {
      const out = computeIndicator(stochDef, candles, { kLength, kSmooth, dSmooth })
      const k0 = kLength - 1 + kSmooth - 1
      expect(out.k[0].time).toBe(candles[k0].time)
      expect(out.d[0].time).toBe(candles[k0 + dSmooth - 1].time)
    }
    const def = computeIndicator(stochDef, candles, defaultInputs(stochDef))
    expect(def.k[0].time).toBe(candles[13].time)
    expect(def.d[0].time).toBe(candles[15].time)
  })

  it('always stays within [0, 100]', () => {
    for (const seed of [1, 2, 3]) {
      const out = computeIndicator(stochDef, randomCandles(500, seed), {
        kLength: 5,
        kSmooth: 2,
        dSmooth: 3,
      })
      for (const key of ['k', 'd']) {
        for (const p of out[key]) {
          expect(p.value).toBeGreaterThanOrEqual(0)
          expect(p.value).toBeLessThanOrEqual(100)
        }
      }
    }
  })

  it('is 50 when the window range is flat', () => {
    const candles = hlc(Array.from({ length: 20 }, (): [number, number, number] => [5, 5, 5]))
    const out = computeIndicator(stochDef, candles, { kLength: 14, kSmooth: 3, dSmooth: 3 })
    expect(out.k.length).toBeGreaterThan(0)
    for (const p of [...out.k, ...out.d]) expect(p.value).toBe(50)
  })

  it('has no look-ahead: changing bars after t leaves outputs up to t bit-identical', () => {
    const candles = randomCandles(200, 11)
    const inputs = { kLength: 14, kSmooth: 3, dSmooth: 3 }
    const full = computeIndicator(stochDef, candles, inputs)
    for (const t of [15, 17, 100, 198]) {
      const perturbed = [...candles.slice(0, t + 1), ...randomCandles(199 - t, 999 + t, t + 1, 50)]
      const out = computeIndicator(stochDef, perturbed, inputs)
      const cut = candles[t].time
      for (const key of ['k', 'd']) {
        expect(out[key].filter((p) => p.time <= cut)).toStrictEqual(
          full[key].filter((p) => p.time <= cut),
        )
      }
    }
  })

  it('clone replays identically and is independent of the original', () => {
    const candles = randomCandles(120, 5)
    const other = randomCandles(60, 77, 16, 80)
    const inputs = { kLength: 14, kSmooth: 3, dSmooth: 3 }
    // k = 16 is inside the %D warm-up, so the clone must copy the partial windows.
    const a = stochDef.createStepper(inputs, 'close')
    for (const c of candles.slice(0, 16)) a.next(c)
    const b = a.clone()
    const outA: IndicatorValues[] = []
    const outB: IndicatorValues[] = []
    for (const c of candles.slice(16)) {
      outA.push(a.next(c))
      outB.push(b.next(c))
    }
    expect(outB).toStrictEqual(outA)

    const orig = stochDef.createStepper(inputs, 'close')
    for (const c of candles.slice(0, 16)) orig.next(c)
    const diverged = orig.clone()
    for (const c of other) diverged.next(c)
    const after = candles.slice(16).map((c) => orig.next(c))
    expect(after).toStrictEqual(outA)
  })
})
