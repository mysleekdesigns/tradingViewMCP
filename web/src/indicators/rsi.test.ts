import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { computeIndicator, defaultInputs } from './compute'
import { rsiDef } from './rsi'
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

/** Brute-force Wilder RSI with plain loops: SMA seed over the first `period` changes, then RMA. */
function naiveRsi(closes: number[], period: number): (number | undefined)[] {
  const out: (number | undefined)[] = closes.map(() => undefined)
  let avgGain = 0
  let avgLoss = 0
  for (let i = 1; i < closes.length; i++) {
    const ch = closes[i] - closes[i - 1]
    const g = ch > 0 ? ch : 0
    const l = ch < 0 ? -ch : 0
    if (i < period) continue
    if (i === period) {
      let sg = 0
      let sl = 0
      for (let j = 1; j <= period; j++) {
        const c = closes[j] - closes[j - 1]
        sg += c > 0 ? c : 0
        sl += c < 0 ? -c : 0
      }
      avgGain = sg / period
      avgLoss = sl / period
    } else {
      avgGain = (avgGain * (period - 1) + g) / period
      avgLoss = (avgLoss * (period - 1) + l) / period
    }
    out[i] = avgLoss === 0 ? 100 : avgGain === 0 ? 0 : 100 - 100 / (1 + avgGain / avgLoss)
  }
  return out
}

describe('rsiDef', () => {
  it('matches a hand-calculated period-2 example', () => {
    // closes 10, 11, 13, 12, 15 -> changes +1, +2, -1, +3; gains 1,2,0,3; losses 0,0,1,0.
    // idx2: avgGain = (1+2)/2 = 1.5, avgLoss = 0 -> 100.
    // idx3: avgGain = 1.5 + 0.5*(0-1.5) = 0.75, avgLoss = 0 + 0.5*(1-0) = 0.5, RS 1.5 -> 60.
    // idx4: avgGain = 0.75 + 0.5*(3-0.75) = 1.875, avgLoss = 0.25, RS 7.5 -> 100 - 100/8.5.
    const out = computeIndicator(rsiDef, fromCloses([10, 11, 13, 12, 15]), { period: 2 })
    expect(out.rsi.map((p) => p.time)).toEqual([T0 + 120, T0 + 180, T0 + 240])
    expect(out.rsi[0].value).toBe(100)
    expect(out.rsi[1].value).toBeCloseTo(60, 12)
    expect(out.rsi[2].value).toBeCloseTo(100 - 100 / 8.5, 12)
  })

  it('matches a brute-force reference over 300 random candles', () => {
    // Tolerance 1e-9: the reference uses (prev*(n-1)+x)/n instead of prev + (x-prev)/n,
    // so rounding differs in the last few bits.
    const candles = randomCandles(300, 42)
    for (const period of [2, 5, 14, 30]) {
      const ref = naiveRsi(
        candles.map((c) => c.close),
        period,
      )
      const out = computeIndicator(rsiDef, candles, { period })
      const expected = ref.flatMap((v, i) =>
        v === undefined ? [] : [{ time: candles[i].time, value: v }],
      )
      expect(out.rsi.length).toBe(expected.length)
      out.rsi.forEach((p, i) => {
        expect(p.time).toBe(expected[i].time)
        expect(Math.abs(p.value - expected[i].value)).toBeLessThan(1e-9)
      })
    }
  })

  it('first value is at bar index `period`', () => {
    const candles = randomCandles(100, 7)
    for (const period of [1, 3, 14]) {
      const out = computeIndicator(rsiDef, candles, { period })
      expect(out.rsi[0].time).toBe(candles[period].time)
    }
    const def = computeIndicator(rsiDef, candles, defaultInputs(rsiDef))
    expect(def.rsi[0].time).toBe(candles[14].time)
  })

  it('is 100 on monotonically rising closes', () => {
    const closes = Array.from({ length: 40 }, (_, i) => 100 + i * 1.5)
    const out = computeIndicator(rsiDef, fromCloses(closes), { period: 14 })
    expect(out.rsi.length).toBe(40 - 14)
    for (const p of out.rsi) expect(p.value).toBe(100)
  })

  it('is 0 on monotonically falling closes', () => {
    const closes = Array.from({ length: 40 }, (_, i) => 200 - i * 1.5)
    const out = computeIndicator(rsiDef, fromCloses(closes), { period: 14 })
    expect(out.rsi.length).toBe(40 - 14)
    for (const p of out.rsi) expect(p.value).toBe(0)
  })

  it('always stays within [0, 100]', () => {
    for (const seed of [1, 2, 3]) {
      const out = computeIndicator(rsiDef, randomCandles(500, seed), { period: 3 })
      for (const p of out.rsi) {
        expect(p.value).toBeGreaterThanOrEqual(0)
        expect(p.value).toBeLessThanOrEqual(100)
      }
    }
  })

  it('has no look-ahead: changing bars after t leaves outputs up to t bit-identical', () => {
    const candles = randomCandles(200, 11)
    const full = computeIndicator(rsiDef, candles, { period: 14 })
    for (const t of [14, 50, 120, 198]) {
      const perturbed = [...candles.slice(0, t + 1), ...randomCandles(199 - t, 999 + t, t + 1, 50)]
      const out = computeIndicator(rsiDef, perturbed, { period: 14 })
      const cut = candles[t].time
      expect(out.rsi.filter((p) => p.time <= cut)).toStrictEqual(
        full.rsi.filter((p) => p.time <= cut),
      )
    }
  })

  it('clone replays identically and is independent of the original', () => {
    const candles = randomCandles(120, 5)
    const other = randomCandles(60, 77, 60, 80)
    const a = rsiDef.createStepper({ period: 14 }, 'close')
    for (const c of candles.slice(0, 60)) a.next(c)
    const b = a.clone()
    const outA: IndicatorValues[] = []
    const outB: IndicatorValues[] = []
    for (const c of candles.slice(60)) {
      outA.push(a.next(c))
      outB.push(b.next(c))
    }
    expect(outB).toStrictEqual(outA)

    const orig = rsiDef.createStepper({ period: 14 }, 'close')
    for (const c of candles.slice(0, 60)) orig.next(c)
    const diverged = orig.clone()
    for (const c of other) diverged.next(c)
    const after = candles.slice(60).map((c) => orig.next(c))
    expect(after).toStrictEqual(outA)
  })
})
