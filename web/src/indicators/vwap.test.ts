import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { computeIndicator } from './compute'
import { vwapDef } from './vwap'
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

function stepAll(
  def: IndicatorDef,
  inputs: IndicatorInputs,
  candles: readonly Candle[],
): IndicatorValues[] {
  const s = def.createStepper(inputs, def.source ?? 'close')
  return candles.map((c) => s.next(c))
}

const DAY = 86400
/** 2023-11-15 00:00:00 UTC (a UTC midnight). */
const MIDNIGHT = 1_700_006_400

function bar(time: number, high: number, low: number, close: number, volume: number): Candle {
  return { time, open: close, high, low, close, volume }
}

/** Naive reference: for each bar, scan back over the same UTC day and sum hlc3*vol / vol. */
function refVwap(candles: readonly Candle[]): (number | undefined)[] {
  return candles.map((c, i) => {
    const day = Math.floor(c.time / DAY)
    let pv = 0
    let v = 0
    for (let j = i; j >= 0 && Math.floor(candles[j].time / DAY) === day; j--) {
      pv += ((candles[j].high + candles[j].low + candles[j].close) / 3) * candles[j].volume
      v += candles[j].volume
    }
    return v > 0 ? pv / v : undefined
  })
}

describe('vwapDef', () => {
  it('matches hand-calculated values', () => {
    // hlc3: (12+8+10)/3=10 vol 1 -> 10; (22+18+20)/3=20 vol 3 -> (10+60)/4=17.5;
    // hlc3 30 vol 0 -> still 17.5; next UTC day hlc3 40 vol 2 -> 40 (reset)
    const candles = [
      bar(MIDNIGHT, 12, 8, 10, 1),
      bar(MIDNIGHT + 3600, 22, 18, 20, 3),
      bar(MIDNIGHT + 7200, 32, 28, 30, 0),
      bar(MIDNIGHT + DAY, 42, 38, 40, 2),
    ]
    const out = computeIndicator(vwapDef, candles, {})
    expect(out.vwap.map((p) => p.value)).toEqual([10, 17.5, 17.5, 40])
  })

  it('matches a brute-force reference on 300 random 1h candles', () => {
    // Tolerance 1e-9 relative: the reference sums the session backwards, the stepper forwards.
    const candles = randomCandles(300, 41)
    const out = computeIndicator(vwapDef, candles, {})
    const ref = refVwap(candles)
    const expected = ref.flatMap((v, i) =>
      v === undefined ? [] : [{ time: candles[i].time, value: v }],
    )
    expect(out.vwap.length).toBe(expected.length)
    out.vwap.forEach((p, i) => {
      expect(p.time).toBe(expected[i].time)
      expect(Math.abs(p.value - expected[i].value)).toBeLessThanOrEqual(1e-9 * expected[i].value)
    })
  })

  it('resets at UTC midnight on 1h candles', () => {
    // 23:00 and 00:00 bars on either side of a midnight boundary.
    const candles = [
      bar(MIDNIGHT - 2 * 3600, 102, 98, 100, 5),
      bar(MIDNIGHT - 3600, 202, 198, 200, 5),
      bar(MIDNIGHT, 52, 48, 50, 7),
      bar(MIDNIGHT + 3600, 62, 58, 60, 7),
    ]
    const out = computeIndicator(vwapDef, candles, {})
    // Day 1: 100, then (500+1000)/10=150. Day 2 starts fresh: 50, then (350+420)/14=55.
    expect(out.vwap.map((p) => p.value)).toEqual([100, 150, 50, 55])
  })

  it('on daily bars every bar is its own session (vwap = hlc3)', () => {
    const candles = randomCandles(30, 42, DAY)
    const out = computeIndicator(vwapDef, candles, {})
    out.vwap.forEach((p, i) => {
      const c = candles[i]
      expect(p.value).toBeCloseTo((c.high + c.low + c.close) / 3, 9)
    })
  })

  it('emits nothing for a zero-volume session and never NaN', () => {
    const candles = [
      bar(MIDNIGHT, 12, 8, 10, 0),
      bar(MIDNIGHT + 3600, 12, 8, 10, 0),
      bar(MIDNIGHT + DAY, 42, 38, 40, 0),
      bar(MIDNIGHT + DAY + 3600, 42, 38, 40, 4),
    ]
    const s = vwapDef.createStepper({}, 'close')
    const values = candles.map((c) => s.next(c))
    expect(values.slice(0, 3)).toEqual([{}, {}, {}])
    expect(values[3]).toEqual({ vwap: 40 })
  })

  it('first output is at the first bar with volume', () => {
    const candles = randomCandles(50, 43).map((c, i) => (i < 4 ? { ...c, volume: 0 } : c))
    const out = computeIndicator(vwapDef, candles, {})
    expect(out.vwap[0].time).toBe(candles[4].time)
  })

  it('has no look-ahead: changing bars after t leaves outputs at <= t identical', () => {
    const candles = randomCandles(300, 44)
    for (const t of [0, 23, 24, 100, 250, 298]) {
      const a = computeIndicator(vwapDef, candles, {})
      const b = computeIndicator(vwapDef, perturbAfter(candles, t, 9 + t), {})
      const cut = candles[t].time
      expect(b.vwap.filter((p) => p.time <= cut)).toEqual(a.vwap.filter((p) => p.time <= cut))
    }
  })

  it('clone replays identically and independently (including across a reset)', () => {
    const candles = randomCandles(120, 45)
    const full = stepAll(vwapDef, {}, candles)
    for (const k of [5, 23, 60]) {
      const a = vwapDef.createStepper({}, 'close')
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
