import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { computeIndicator, defaultInputs } from './compute'
import { volumeMaDef } from './volumeMa'
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

function vols(values: number[]): Candle[] {
  return closes(values.map(() => 100)).map((c, i) => ({ ...c, volume: values[i] }))
}

/** Naive reference: mean of each full slice of volumes. */
function refSma(xs: number[], n: number): (number | undefined)[] {
  return xs.map((_, i) => {
    if (i < n - 1) return undefined
    return xs.slice(i - n + 1, i + 1).reduce((a, b) => a + b, 0) / n
  })
}

describe('volumeMaDef', () => {
  it('matches hand-calculated values', () => {
    // period 2 over volumes 10,20,30,0: (10+20)/2=15, (20+30)/2=25, (30+0)/2=15
    const out = computeIndicator(volumeMaDef, vols([10, 20, 30, 0]), { period: 2 })
    expect(out.ma.map((p) => p.value)).toEqual([15, 25, 15])
  })

  it('reads volume, not price', () => {
    const candles = randomCandles(10, 30)
    const out = computeIndicator(volumeMaDef, candles, { period: 1 })
    expect(out.ma.map((p) => p.value)).toEqual(candles.map((c) => c.volume))
  })

  it('matches a brute-force reference on 300 random candles', () => {
    // Tolerance 1e-9 relative (plus 1e-9 absolute for tiny volumes): summation order may differ.
    const candles = randomCandles(300, 31)
    for (const period of [1, 5, 20, 50]) {
      const out = computeIndicator(volumeMaDef, candles, { period })
      const ref = refSma(
        candles.map((c) => c.volume),
        period,
      )
      const expected = ref.flatMap((v, i) =>
        v === undefined ? [] : [{ time: candles[i].time, value: v }],
      )
      expect(out.ma.length).toBe(expected.length)
      out.ma.forEach((p, i) => {
        expect(p.time).toBe(expected[i].time)
        expect(Math.abs(p.value - expected[i].value)).toBeLessThanOrEqual(
          1e-9 * expected[i].value + 1e-9,
        )
      })
    }
  })

  it('first output is at candles[period-1].time', () => {
    const candles = randomCandles(100, 32)
    const out = computeIndicator(volumeMaDef, candles, defaultInputs(volumeMaDef))
    expect(out.ma[0].time).toBe(candles[19].time)
  })

  it('has no look-ahead: changing bars after t leaves outputs at <= t identical', () => {
    const candles = randomCandles(300, 33)
    for (const t of [0, 19, 20, 100, 250, 298]) {
      const a = computeIndicator(volumeMaDef, candles, { period: 20 })
      const b = computeIndicator(volumeMaDef, perturbAfter(candles, t, 5 + t), { period: 20 })
      const cut = candles[t].time
      expect(b.ma.filter((p) => p.time <= cut)).toEqual(a.ma.filter((p) => p.time <= cut))
    }
  })

  it('clone replays identically and independently', () => {
    const candles = randomCandles(120, 34)
    const inputs = { period: 14 }
    const full = stepAll(volumeMaDef, inputs, candles)
    for (const k of [5, 13, 60]) {
      const a = volumeMaDef.createStepper(inputs, 'close')
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
