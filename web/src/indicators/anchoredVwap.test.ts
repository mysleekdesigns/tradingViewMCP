import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { anchoredVwapDef } from './anchoredVwap'
import { computeIndicator, defaultInputs } from './compute'
import { IndicatorRunner } from './runner'
import { priceOf } from './sources'
import type { IndicatorValues, PriceSource } from './types'

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

const T0 = 1_700_006_400
const H = 3600

function randomCandles(n: number, seed: number, step = H): Candle[] {
  const rnd = mulberry32(seed)
  const out: Candle[] = []
  let prev = 100
  for (let i = 0; i < n; i++) {
    const open = prev
    const close = Math.max(1, open * (1 + (rnd() - 0.5) * 0.04))
    const high = Math.max(open, close) * (1 + rnd() * 0.01)
    const low = Math.min(open, close) * (1 - rnd() * 0.01)
    out.push({ time: T0 + i * step, open, high, low, close, volume: rnd() * 1000 })
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

function bar(time: number, high: number, low: number, close: number, volume: number): Candle {
  return { time, open: close, high, low, close, volume }
}

function stepAll(anchor: number, candles: readonly Candle[], source: PriceSource = 'hlc3') {
  const s = anchoredVwapDef.createStepper({ anchor }, source)
  return candles.map((c) => s.next(c))
}

/** Naive reference: sum source*vol / vol over every bar from the anchor bar to i. */
function refAvwap(
  candles: readonly Candle[],
  anchor: number,
  source: PriceSource,
): (number | undefined)[] {
  return candles.map((c, i) => {
    if (anchor <= 0 || c.time < anchor) return undefined
    let pv = 0
    let v = 0
    for (let j = 0; j <= i; j++) {
      if (candles[j].time < anchor) continue
      pv += priceOf(candles[j], source) * candles[j].volume
      v += candles[j].volume
    }
    return v > 0 ? pv / v : undefined
  })
}

describe('anchoredVwapDef', () => {
  it('declares one time input defaulting to 0 (not set) and hlc3 as the default source', () => {
    expect(anchoredVwapDef.id).toBe('avwap')
    expect(anchoredVwapDef.placement).toBe('overlay')
    expect(anchoredVwapDef.source).toBe('hlc3')
    expect(anchoredVwapDef.inputs).toHaveLength(1)
    expect(anchoredVwapDef.inputs[0]).toMatchObject({ key: 'anchor', kind: 'time', default: 0 })
    expect(anchoredVwapDef.lines.map((l) => l.key)).toEqual(['avwap'])
    expect(anchoredVwapDef.lines[0].color.toLowerCase()).not.toBe('#ab47bc')
  })

  it('matches hand-calculated values from the anchor bar onward (never resets)', () => {
    // hlc3 values 10, 20, 30, 40, 50; anchor at bar 1.
    // bar1: 20*3/3 = 20; bar2: (60+30*1)/4 = 22.5; bar3 (next UTC day, no reset):
    // (90+40*0)/4 = 22.5; bar4: (90+50*6)/10 = 39.
    const day = 86400
    const candles = [
      bar(T0, 12, 8, 10, 100),
      bar(T0 + H, 22, 18, 20, 3),
      bar(T0 + 2 * H, 32, 28, 30, 1),
      bar(T0 + day, 42, 38, 40, 0),
      bar(T0 + day + H, 52, 48, 50, 6),
    ]
    const out = computeIndicator(anchoredVwapDef, candles, { anchor: T0 + H })
    expect(out.avwap).toEqual([
      { time: T0 + H, value: 20 },
      { time: T0 + 2 * H, value: 22.5 },
      { time: T0 + day, value: 22.5 },
      { time: T0 + day + H, value: 39 },
    ])
  })

  it('uses the selected price source', () => {
    // close source: closes 10, 20 with vol 1, 3 -> 10, then (10+60)/4 = 17.5
    const candles = [bar(T0, 15, 5, 10, 1), bar(T0 + H, 30, 0, 20, 3)]
    const out = computeIndicator(anchoredVwapDef, candles, { anchor: T0 }, 'close')
    expect(out.avwap.map((p) => p.value)).toEqual([10, 17.5])
    // hl2: 10, 15 -> 10, (10+45)/4 = 13.75
    const hl2 = computeIndicator(anchoredVwapDef, candles, { anchor: T0 }, 'hl2')
    expect(hl2.avwap.map((p) => p.value)).toEqual([10, 13.75])
  })

  it('an anchor between two bars starts at the next bar', () => {
    const candles = [
      bar(T0, 12, 8, 10, 1),
      bar(T0 + H, 22, 18, 20, 2),
      bar(T0 + 2 * H, 32, 28, 30, 2),
    ]
    const out = computeIndicator(anchoredVwapDef, candles, { anchor: T0 + 1 })
    // Starts at bar 1: 20, then (40+60)/4 = 25
    expect(out.avwap).toEqual([
      { time: T0 + H, value: 20 },
      { time: T0 + 2 * H, value: 25 },
    ])
  })

  it('anchor 0 (not set) gives no values; an anchor after the last bar gives none either', () => {
    const candles = randomCandles(50, 1)
    expect(computeIndicator(anchoredVwapDef, candles, { anchor: 0 }).avwap).toEqual([])
    expect(
      computeIndicator(anchoredVwapDef, candles, defaultInputs(anchoredVwapDef)).avwap,
    ).toEqual([])
    const late = candles[49].time + 1
    expect(computeIndicator(anchoredVwapDef, candles, { anchor: late }).avwap).toEqual([])
  })

  it('emits nothing while cumulative volume is 0 and never NaN', () => {
    const candles = [
      bar(T0, 12, 8, 10, 5),
      bar(T0 + H, 22, 18, 20, 0),
      bar(T0 + 2 * H, 32, 28, 30, 0),
      bar(T0 + 3 * H, 42, 38, 40, 2),
    ]
    const values = stepAll(T0 + H, candles)
    expect(values).toEqual([{}, {}, {}, { avwap: 40 }])
  })

  it('matches a brute-force reference on 300 random candles for several anchors and sources', () => {
    // Tolerance 1e-9 relative: both sum forwards, but the reference restarts the sum per bar.
    const candles = randomCandles(300, 2)
    for (const k of [0, 17, 150, 299]) {
      for (const source of ['hlc3', 'close', 'ohlc4'] as const) {
        const anchor = candles[k].time
        const out = computeIndicator(anchoredVwapDef, candles, { anchor }, source)
        const ref = refAvwap(candles, anchor, source)
        const expected = ref.flatMap((v, i) =>
          v === undefined ? [] : [{ time: candles[i].time, value: v }],
        )
        expect(out.avwap.length).toBe(expected.length)
        expect(out.avwap[0].time).toBe(anchor)
        out.avwap.forEach((p, i) => {
          expect(p.time).toBe(expected[i].time)
          expect(Math.abs(p.value - expected[i].value)).toBeLessThanOrEqual(
            1e-9 * expected[i].value,
          )
        })
      }
    }
  })

  it('has no look-ahead: changing bars after t leaves outputs at <= t bit-identical', () => {
    const candles = randomCandles(300, 3)
    for (const k of [0, 40]) {
      const anchor = candles[k].time
      for (const t of [0, 39, 40, 41, 120, 298]) {
        const a = computeIndicator(anchoredVwapDef, candles, { anchor })
        const b = computeIndicator(anchoredVwapDef, perturbAfter(candles, t, 9 + t), { anchor })
        const cut = candles[t].time
        expect(b.avwap.filter((p) => p.time <= cut)).toEqual(a.avwap.filter((p) => p.time <= cut))
      }
    }
  })

  it('clone replays identically and independently', () => {
    const candles = randomCandles(120, 4)
    const anchor = candles[30].time
    const full = stepAll(anchor, candles)
    for (const k of [5, 30, 31, 90]) {
      const a = anchoredVwapDef.createStepper({ anchor }, 'hlc3')
      for (const c of candles.slice(0, k)) a.next(c)
      const b = a.clone()
      const rest = candles.slice(k)
      const fromClone: IndicatorValues[] = rest.map((c) => b.next(c))
      const fromOrig: IndicatorValues[] = rest.map((c) => a.next(c))
      expect(fromClone).toEqual(full.slice(k))
      expect(fromOrig).toEqual(full.slice(k))
    }
  })

  it('tail recompute (IndicatorRunner) equals a full recompute under live ticks and appends', () => {
    const rnd = mulberry32(5)
    const candles = randomCandles(80, 6)
    const inputs = { anchor: candles[60].time }
    const runner = new IndicatorRunner(anchoredVwapDef, inputs, 'hlc3')
    runner.reset(candles)
    for (let step = 0; step < 200; step++) {
      const last = candles[candles.length - 1]
      const close = Math.max(1, last.close * (1 + (rnd() - 0.5) * 0.01))
      const next: Candle = {
        time: last.time,
        open: last.open,
        high: Math.max(last.high, close),
        low: Math.min(last.low, close),
        close,
        volume: last.volume + rnd() * 10,
      }
      if (rnd() < 0.7) {
        candles[candles.length - 1] = next
      } else {
        candles.push({ ...next, time: last.time + H, open: close, high: close, low: close })
      }
      runner.update(candles, candles.length - 1)
      expect(runner.output).toEqual(computeIndicator(anchoredVwapDef, candles, inputs, 'hlc3'))
    }
    expect(runner.output.avwap.length).toBeGreaterThan(20)
  })
  it('anchor before the loaded history: no values at all, not a line from the first bar', () => {
    const candles = randomCandles(100, 7)
    // Anchor one bar (or one second) before the first loaded bar: its bar is not loaded.
    for (const anchor of [candles[0].time - H, candles[0].time - 1]) {
      expect(computeIndicator(anchoredVwapDef, candles, { anchor }).avwap).toEqual([])
    }
    // Anchor exactly on the first loaded bar is fine.
    const onFirst = computeIndicator(anchoredVwapDef, candles, { anchor: candles[0].time })
    expect(onFirst.avwap).toHaveLength(100)
    // Once the anchor bar is loaded (older page prepended), the line starts at it.
    const older = randomCandles(10, 8).map((c, i) => ({
      ...c,
      time: candles[0].time - (10 - i) * H,
    }))
    const grown = [...older, ...candles]
    const anchor = older[4].time
    expect(computeIndicator(anchoredVwapDef, candles, { anchor }).avwap).toEqual([])
    const full = computeIndicator(anchoredVwapDef, grown, { anchor })
    expect(full.avwap[0].time).toBe(anchor)
    expect(full.avwap).toHaveLength(106)
  })

  it('the not-loaded decision is made at the first bar and survives clone()', () => {
    const candles = randomCandles(40, 9)
    const anchor = candles[0].time - 1
    const s = anchoredVwapDef.createStepper({ anchor }, 'hlc3')
    expect(s.next(candles[0])).toEqual({})
    const c = s.clone()
    for (const bar of candles.slice(1)) {
      expect(s.next(bar)).toEqual({})
      expect(c.next(bar)).toEqual({})
    }
    // Runner tail updates replay from clones: still nothing under live ticks.
    const runner = new IndicatorRunner(anchoredVwapDef, { anchor }, 'hlc3')
    runner.reset(candles)
    const ticked = [...candles.slice(0, -1), { ...candles[39], close: 1, volume: 1e6 }]
    runner.update(ticked, 39)
    expect(runner.output.avwap).toEqual([])
  })

  it('missing, non-finite or negative volume counts as 0 and never ends the line', () => {
    const candles = [
      bar(T0, 12, 8, 10, 2),
      bar(T0 + H, 22, 18, 20, Number.NaN),
      bar(T0 + 2 * H, 32, 28, 30, -4),
      bar(T0 + 3 * H, 42, 38, 40, Number.POSITIVE_INFINITY),
      bar(T0 + 4 * H, 52, 48, 50, 2),
    ]
    // hlc3 10 (vol 2) -> 10; the three bad bars add nothing -> 10; then (20 + 100) / 4 = 30.
    expect(stepAll(T0, candles)).toEqual([
      { avwap: 10 },
      { avwap: 10 },
      { avwap: 10 },
      { avwap: 10 },
      { avwap: 30 },
    ])
  })
})
