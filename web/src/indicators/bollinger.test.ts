import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { computeIndicator, defaultInputs } from './compute'
import { bollingerDef } from './bollinger'
import type { IndicatorDef, IndicatorInputs, IndicatorValues } from './types'

// --- local test helpers (seeded, offline) ---
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

function randomCandles(n: number, seed: number, step = 3600): Candle[] {
  const rnd = mulberry32(seed)
  const out: Candle[] = []
  let prev = 100
  for (let i = 0; i < n; i++) {
    const open = prev
    const close = Math.max(1, open * (1 + (rnd() - 0.5) * 0.04))
    const high = Math.max(open, close) * (1 + rnd() * 0.01)
    const low = Math.min(open, close) * (1 - rnd() * 0.01)
    out.push({ time: 1_700_006_400 + i * step, open, high, low, close, volume: rnd() * 1000 })
    prev = close
  }
  return out
}

/** Copy of `candles` with every bar after index t changed (prices and volume). */
function perturbAfter(candles: readonly Candle[], t: number, seed: number): Candle[] {
  const rnd = mulberry32(seed)
  return candles.map((c, i) => {
    if (i <= t) return c
    const f = 0.5 + rnd() * 2
    return {
      ...c,
      open: c.open * f,
      high: c.high * f * 1.01,
      low: c.low * f * 0.99,
      close: c.close * f,
      volume: rnd() * 5000,
    }
  })
}

function closes(values: number[]): Candle[] {
  return values.map((v, i) => ({
    time: 1_700_006_400 + i * 60,
    open: v,
    high: v,
    low: v,
    close: v,
    volume: 1,
  }))
}

function stepAll(
  def: IndicatorDef,
  inputs: IndicatorInputs,
  candles: readonly Candle[],
): IndicatorValues[] {
  const s = def.createStepper(inputs, def.source ?? 'close')
  return candles.map((c) => s.next(c))
}

/** Naive reference from array slices; population stdev (divide by N). */
function refBb(xs: number[], n: number, mult: number) {
  return xs.map((_, i) => {
    if (i < n - 1) return undefined
    const w = xs.slice(i - n + 1, i + 1)
    const m = w.reduce((a, b) => a + b, 0) / n
    const sd = Math.sqrt(w.reduce((a, b) => a + (b - m) * (b - m), 0) / n)
    return { basis: m, upper: m + mult * sd, lower: m - mult * sd }
  })
}

describe('bollingerDef', () => {
  it('matches hand-calculated values', () => {
    // period 3, mult 2 over closes 1,2,3,4,8:
    // [1,2,3]: mean 2, pop sd sqrt(2/3)=0.8164966 -> upper 3.6329932, lower 0.3670068
    // [2,3,4]: mean 3, sd sqrt(2/3)              -> upper 4.6329932, lower 1.3670068
    // [3,4,8]: mean 5, devs -2,-1,3, ss 14, sd sqrt(14/3)=2.1602469 -> upper 9.3204938, lower 0.6795062
    const out = computeIndicator(bollingerDef, closes([1, 2, 3, 4, 8]), { period: 3, mult: 2 })
    expect(out.basis.map((p) => p.value)).toEqual([2, 3, 5])
    const upper = [3.6329932, 4.6329932, 9.3204938]
    const lower = [0.3670068, 1.3670068, 0.6795062]
    out.upper.forEach((p, i) => expect(p.value).toBeCloseTo(upper[i], 6))
    out.lower.forEach((p, i) => expect(p.value).toBeCloseTo(lower[i], 6))
  })

  it('matches a brute-force reference on 300 random candles', () => {
    // Tolerance 1e-9 relative to price: the same slice math, kept loose for summation order.
    const candles = randomCandles(300, 21)
    for (const [period, mult] of [
      [1, 2],
      [5, 1.5],
      [20, 2],
      [50, 3],
    ]) {
      const out = computeIndicator(bollingerDef, candles, { period, mult })
      const ref = refBb(
        candles.map((c) => c.close),
        period,
        mult,
      )
      const expected = ref.flatMap((v, i) =>
        v === undefined ? [] : [{ t: candles[i].time, ...v }],
      )
      for (const key of ['basis', 'upper', 'lower'] as const) {
        expect(out[key].length).toBe(expected.length)
        out[key].forEach((p, i) => {
          expect(p.time).toBe(expected[i].t)
          expect(Math.abs(p.value - expected[i][key])).toBeLessThanOrEqual(1e-9 * expected[i].basis)
        })
      }
    }
  })

  it('first output is at candles[period-1].time for every line', () => {
    const candles = randomCandles(100, 22)
    const out = computeIndicator(bollingerDef, candles, defaultInputs(bollingerDef))
    for (const key of ['basis', 'upper', 'lower']) expect(out[key][0].time).toBe(candles[19].time)
  })

  it('bands are symmetric around the basis', () => {
    const candles = randomCandles(200, 23)
    const out = computeIndicator(bollingerDef, candles, { period: 20, mult: 2 })
    out.basis.forEach((b, i) => {
      const up = out.upper[i].value - b.value
      const down = b.value - out.lower[i].value
      expect(Math.abs(up - down)).toBeLessThanOrEqual(1e-9 * b.value)
    })
  })

  it('band width scales linearly with mult', () => {
    const candles = randomCandles(200, 24)
    const one = computeIndicator(bollingerDef, candles, { period: 20, mult: 1 })
    const three = computeIndicator(bollingerDef, candles, { period: 20, mult: 3 })
    one.basis.forEach((b, i) => {
      expect(three.basis[i].value).toBe(b.value)
      const w1 = one.upper[i].value - one.lower[i].value
      const w3 = three.upper[i].value - three.lower[i].value
      expect(Math.abs(w3 - 3 * w1)).toBeLessThanOrEqual(1e-9 * b.value)
    })
  })

  it('has no look-ahead: changing bars after t leaves outputs at <= t identical', () => {
    const candles = randomCandles(300, 25)
    const inputs = { period: 20, mult: 2 }
    for (const t of [0, 19, 20, 100, 250, 298]) {
      const a = computeIndicator(bollingerDef, candles, inputs)
      const b = computeIndicator(bollingerDef, perturbAfter(candles, t, 3 + t), inputs)
      const cut = candles[t].time
      for (const key of ['basis', 'upper', 'lower']) {
        expect(b[key].filter((p) => p.time <= cut)).toEqual(a[key].filter((p) => p.time <= cut))
      }
    }
  })

  it('clone replays identically and independently', () => {
    const candles = randomCandles(120, 26)
    const inputs = { period: 14, mult: 2 }
    const full = stepAll(bollingerDef, inputs, candles)
    for (const k of [5, 13, 60]) {
      const a = bollingerDef.createStepper(inputs, 'close')
      for (const c of candles.slice(0, k)) a.next(c)
      const b = a.clone()
      const rest = candles.slice(k)
      const fromClone = rest.map((c) => b.next(c))
      const fromOrig = rest.map((c) => a.next(c))
      expect(fromClone).toEqual(full.slice(k))
      expect(fromOrig).toEqual(full.slice(k))
    }
  })
})
