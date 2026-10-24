import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { computeIndicator, defaultInputs } from './compute'
import { smaDef } from './sma'
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

/** Naive reference: mean of each full slice. */
function refSma(xs: number[], n: number): (number | undefined)[] {
  return xs.map((_, i) => {
    if (i < n - 1) return undefined
    const w = xs.slice(i - n + 1, i + 1)
    return w.reduce((a, b) => a + b, 0) / n
  })
}

describe('smaDef', () => {
  it('matches hand-calculated values', () => {
    // period 3 over 1..6: (1+2+3)/3=2, (2+3+4)/3=3, 4, 5
    const out = computeIndicator(smaDef, closes([1, 2, 3, 4, 5, 6]), { period: 3 })
    expect(out.sma.map((p) => p.value)).toEqual([2, 3, 4, 5])
  })

  it('matches a brute-force reference on 300 random candles', () => {
    // Tolerance 1e-9 relative: the reference sums slices left to right like the window, but
    // keep a margin for any summation-order difference.
    const candles = randomCandles(300, 1)
    for (const period of [1, 5, 20, 50]) {
      const out = computeIndicator(smaDef, candles, { period })
      const ref = refSma(
        candles.map((c) => c.close),
        period,
      )
      const expected = ref.flatMap((v, i) =>
        v === undefined ? [] : [{ time: candles[i].time, value: v }],
      )
      expect(out.sma.length).toBe(expected.length)
      out.sma.forEach((p, i) => {
        expect(p.time).toBe(expected[i].time)
        expect(Math.abs(p.value - expected[i].value)).toBeLessThanOrEqual(1e-9 * expected[i].value)
      })
    }
  })

  it('uses the selected price source', () => {
    const candles = randomCandles(30, 2)
    const out = computeIndicator(smaDef, candles, { period: 5 }, 'high')
    const ref = refSma(
      candles.map((c) => c.high),
      5,
    )
    expect(out.sma[0].value).toBeCloseTo(ref[4] as number, 9)
  })

  it('first output is at candles[period-1].time', () => {
    const candles = randomCandles(100, 3)
    const out = computeIndicator(smaDef, candles, defaultInputs(smaDef))
    expect(out.sma[0].time).toBe(candles[19].time)
  })

  it('has no look-ahead: changing bars after t leaves outputs at <= t identical', () => {
    const candles = randomCandles(300, 4)
    for (const t of [0, 19, 20, 100, 250, 298]) {
      const a = computeIndicator(smaDef, candles, { period: 20 })
      const b = computeIndicator(smaDef, perturbAfter(candles, t, 99 + t), { period: 20 })
      const cut = candles[t].time
      expect(b.sma.filter((p) => p.time <= cut)).toEqual(a.sma.filter((p) => p.time <= cut))
    }
  })

  it('clone replays identically and independently', () => {
    const candles = randomCandles(120, 5)
    const inputs = { period: 14 }
    const full = stepAll(smaDef, inputs, candles)
    for (const k of [5, 13, 60]) {
      const a = smaDef.createStepper(inputs, 'close')
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
