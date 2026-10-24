import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { computeIndicator, defaultInputs } from './compute'
import { Ema, Sma } from './primitives'
import { INDICATORS } from './registry'
import { IndicatorRunner } from './runner'
import { priceOf } from './sources'
import type { IndicatorDef, IndicatorStepper, IndicatorValues, PriceSource } from './types'

/** Fake SMA-like def: one line, warm-up = period. */
const fakeSma: IndicatorDef = {
  id: 'sma',
  name: 'Fake SMA',
  shortName: 'FSMA',
  description: 'test',
  placement: 'overlay',
  inputs: [
    { key: 'period', label: 'Length', default: 3, min: 1, max: 500, step: 1, integer: true },
  ],
  source: 'close',
  lines: [{ key: 'value', label: 'SMA', kind: 'line', color: '#2962ff', lineWidth: 2 }],
  guides: [],
  createStepper(inputs, source) {
    const make = (sma: Sma): IndicatorStepper => ({
      next(c: Candle): IndicatorValues {
        const v = sma.next(priceOf(c, source))
        return v === undefined ? {} : { value: v }
      },
      clone: () => make(sma.clone()),
    })
    return make(new Sma(inputs.period))
  },
}

/** Fake 2-line def with different warm-ups: fast EMA(2) and slow SMA(5) of the difference. */
const fakeTwoLine: IndicatorDef = {
  id: 'macd',
  name: 'Fake Two Line',
  shortName: 'F2',
  description: 'test',
  placement: 'pane',
  inputs: [
    { key: 'fast', label: 'Fast', default: 2, min: 1, max: 100, step: 1, integer: true },
    { key: 'slow', label: 'Slow', default: 5, min: 1, max: 100, step: 1, integer: true },
  ],
  source: 'close',
  lines: [
    { key: 'fast', label: 'Fast', kind: 'line', color: '#2962ff', lineWidth: 1 },
    { key: 'slow', label: 'Slow', kind: 'histogram', color: '#f7a21b', lineWidth: 1 },
  ],
  guides: [],
  createStepper(inputs, source: PriceSource) {
    const make = (ema: Ema, sma: Sma): IndicatorStepper => ({
      next(c: Candle): IndicatorValues {
        const f = ema.next(priceOf(c, source))
        const out: IndicatorValues = {}
        if (f !== undefined) {
          out.fast = f
          const s = sma.next(c.close - f)
          if (s !== undefined) out.slow = s
        }
        return out
      },
      clone: () => make(ema.clone(), sma.clone()),
    })
    return make(new Ema(inputs.fast), new Sma(inputs.slow))
  },
}

const DEFS = [fakeSma, fakeTwoLine]
const INPUTS: Record<string, Record<string, number>> = {
  sma: { period: 3 },
  macd: { fast: 2, slow: 5 },
}

/** Mulberry32: tiny seeded PRNG so the random test is reproducible. */
function prng(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function bar(time: number, close: number): Candle {
  return { time, open: close - 1, high: close + 2, low: close - 2, close, volume: 10 }
}

function series(n: number, rand = prng(1)): Candle[] {
  const out: Candle[] = []
  let p = 100
  for (let i = 0; i < n; i++) {
    p += rand() * 4 - 2
    out.push(bar(1_700_000_000 + i * 60, p))
  }
  return out
}

function fresh(def: IndicatorDef, candles: readonly Candle[]) {
  return computeIndicator(def, candles, INPUTS[def.id], 'close')
}

describe.each(DEFS)('IndicatorRunner ($name)', (def) => {
  const inputs = INPUTS[def.id]

  it('reset equals computeIndicator', () => {
    const candles = series(40)
    const r = new IndicatorRunner(def, inputs, 'close')
    expect(r.reset(candles)).toEqual(fresh(def, candles))
    expect(r.output).toEqual(fresh(def, candles))
  })

  it('a tick on the last bar equals a reset on the same candles', () => {
    const candles = series(30)
    const r = new IndicatorRunner(def, inputs, 'close')
    r.reset(candles)
    const ticked = [...candles.slice(0, -1), bar(candles[29].time, candles[29].close + 5)]
    const res = r.update(ticked, 29)
    expect(res.kind).toBe('update')
    expect(r.output).toEqual(fresh(def, ticked))
  })

  it('appending 1..5 bars equals a full recompute', () => {
    for (let k = 1; k <= 5; k++) {
      const all = series(35 + k)
      const r = new IndicatorRunner(def, inputs, 'close')
      r.reset(all.slice(0, 35))
      const res = r.update(all, 35)
      expect(res.kind).toBe('update')
      expect(r.output).toEqual(fresh(def, all))
    }
  })

  it('200 random ticks and appends always equal a fresh recompute', () => {
    const rand = prng(42)
    let candles = series(3, rand)
    const r = new IndicatorRunner(def, inputs, 'close')
    r.reset(candles)
    for (let step = 0; step < 200; step++) {
      const last = candles[candles.length - 1]
      let from: number
      if (rand() < 0.6) {
        // Tick: the forming bar's close moves.
        candles = [...candles.slice(0, -1), bar(last.time, last.close + rand() * 6 - 3)]
        from = candles.length - 1
      } else {
        // Close the bar (maybe with a final tick) and append 1..3 new ones.
        const k = 1 + Math.floor(rand() * 3)
        candles = [...candles.slice(0, -1), bar(last.time, last.close + rand() - 0.5)]
        from = candles.length - 1
        for (let i = 0; i < k; i++) {
          const t = candles[candles.length - 1].time + 60
          candles = [...candles, bar(t, 100 + rand() * 10)]
        }
      }
      const res = r.update(candles, from)
      expect(res.kind).toBe('update')
      expect(r.output).toEqual(fresh(def, candles))
    }
  })

  it('update returns only points for bars after the checkpoint', () => {
    const candles = series(30)
    const r = new IndicatorRunner(def, inputs, 'close')
    r.reset(candles)
    const next = [...candles, bar(candles[29].time + 60, 120), bar(candles[29].time + 120, 121)]
    const res = r.update(next, 30)
    expect(res.kind).toBe('update')
    // Checkpoint was at bar 28 (length - 2), so bars 29, 30, 31 are replayed.
    const minTime = candles[29].time
    const full = fresh(def, next)
    for (const line of def.lines) {
      expect(res.output[line.key].every((p) => p.time >= minTime)).toBe(true)
      expect(res.output[line.key]).toEqual(full[line.key].filter((p) => p.time >= minTime))
    }
  })

  it('nothing changed returns an empty update', () => {
    const candles = series(20)
    const r = new IndicatorRunner(def, inputs, 'close')
    r.reset(candles)
    const res = r.update(candles, 20)
    expect(res.kind).toBe('update')
    for (const line of def.lines) expect(res.output[line.key]).toEqual([])
    expect(r.output).toEqual(fresh(def, candles))
  })

  it('falls back to set when history changed', () => {
    const candles = series(30)
    const r = new IndicatorRunner(def, inputs, 'close')
    r.reset(candles)

    // from before the checkpoint.
    const edited = candles.map((c, i) => (i === 10 ? bar(c.time, c.close + 50) : c))
    let res = r.update(edited, 10)
    expect(res.kind).toBe('set')
    expect(res.output).toEqual(fresh(def, edited))

    // Prepended history: checkpoint bar time no longer matches.
    const older = series(5, prng(9)).map((c, i) => ({ ...c, time: candles[0].time - (5 - i) * 60 }))
    const prepended = [...older, ...edited]
    res = r.update(prepended, prepended.length - 1)
    expect(res.kind).toBe('set')
    expect(res.output).toEqual(fresh(def, prepended))

    // from past the end, and fewer candles than before.
    expect(r.update(prepended, prepended.length + 1).kind).toBe('set')
    const shorter = prepended.slice(0, -3)
    res = r.update(shorter, shorter.length)
    expect(res.kind).toBe('set')
    expect(res.output).toEqual(fresh(def, shorter))
  })

  it('falls back to set when no reset happened yet, and handles empty candles', () => {
    const candles = series(10)
    const r = new IndicatorRunner(def, inputs, 'close')
    const res = r.update(candles, 9)
    expect(res.kind).toBe('set')
    expect(res.output).toEqual(fresh(def, candles))
    const empty = r.update([], 0)
    expect(empty.kind).toBe('set')
    for (const line of def.lines) expect(empty.output[line.key]).toEqual([])
  })

  it('grows from a single bar through warm-up incrementally', () => {
    const all = series(20)
    const r = new IndicatorRunner(def, inputs, 'close')
    r.reset(all.slice(0, 1))
    for (let n = 2; n <= all.length; n++) {
      const res = r.update(all.slice(0, n), n - 1)
      expect(res.kind).toBe('update')
      expect(r.output).toEqual(fresh(def, all.slice(0, n)))
    }
  })

  it('two ticks on the same bar never corrupt the checkpoint', () => {
    const candles = series(25)
    const r = new IndicatorRunner(def, inputs, 'close')
    r.reset(candles)
    const t = candles[24].time
    const a = [...candles.slice(0, -1), bar(t, 150)]
    const b = [...candles.slice(0, -1), bar(t, 90)]
    r.update(a, 24)
    expect(r.output).toEqual(fresh(def, a))
    const res = r.update(b, 24)
    expect(res.kind).toBe('update')
    expect(r.output).toEqual(fresh(def, b))
    // The emitted points are exactly the last bar's values from a fresh computation.
    const full = fresh(def, b)
    for (const line of def.lines) {
      expect(res.output[line.key]).toEqual(full[line.key].filter((p) => p.time === t))
    }
  })

  it('valuesAt returns values at a hovered time and {} for unknown times', () => {
    const candles = series(30)
    const r = new IndicatorRunner(def, inputs, 'close')
    r.reset(candles)
    const full = fresh(def, candles)
    const t = candles[20].time
    const expected: IndicatorValues = {}
    for (const line of def.lines) {
      const p = full[line.key].find((x) => x.time === t)
      if (p) expected[line.key] = p.value
    }
    expect(Object.keys(expected).length).toBe(def.lines.length)
    expect(r.valuesAt(t)).toEqual(expected)
    expect(r.valuesAt(t + 1)).toEqual({})
    expect(r.valuesAt(0)).toEqual({})
    expect(r.valuesAt(candles[0].time)).toEqual({}) // still warming up
  })
})

describe('IndicatorRunner deletion fallback', () => {
  it('returns set when a replayed bar loses a value it had before', () => {
    // Emits a value only when close > 100; a tick that drops the close removes the point.
    const gated: IndicatorDef = {
      ...fakeSma,
      createStepper() {
        const make = (): IndicatorStepper => ({
          next: (c) => (c.close > 100 ? { value: c.close } : {}),
          clone: make,
        })
        return make()
      },
    }
    const candles = [bar(60, 101), bar(120, 102), bar(180, 103)]
    const r = new IndicatorRunner(gated, {}, 'close')
    r.reset(candles)
    const ticked = [candles[0], candles[1], bar(180, 99)]
    const res = r.update(ticked, 2)
    expect(res.kind).toBe('set')
    expect(res.output).toEqual(computeIndicator(gated, ticked, {}, 'close'))
    expect(r.output).toEqual(computeIndicator(gated, ticked, {}, 'close'))
  })
})

describe('IndicatorRunner appended bars with a stale from', () => {
  it('from === candles.length after an append still emits the new bar', () => {
    const candles = series(60)
    const runner = new IndicatorRunner(fakeSma, { period: 3 }, 'close')
    runner.reset(candles.slice(0, 59))
    const res = runner.update(candles, 60)
    expect(res.kind).toBe('update')
    expect(res.output.value.at(-1)?.time).toBe(candles[59].time)
    expect(runner.output).toEqual(fresh(fakeSma, candles))
  })
})

/** Hourly OHLCV bars over several UTC days, with some zero-volume bars (VWAP edge cases). */
function liveBars(n: number, rand: () => number): Candle[] {
  const out: Candle[] = []
  let price = 100
  for (let i = 0; i < n; i++)
    out.push(liveBar(1_700_006_400 + i * 3600, (price += rand() - 0.5), rand))
  return out
}

function liveBar(time: number, close: number, rand: () => number): Candle {
  const open = close + (rand() - 0.5)
  return {
    time,
    open,
    high: Math.max(open, close) + rand(),
    low: Math.min(open, close) - rand(),
    close,
    volume: rand() < 0.1 ? 0 : 1 + rand() * 10,
  }
}

describe.each(INDICATORS)('IndicatorRunner with real indicator $id', (def) => {
  it('random live ticks and appends always equal a fresh recompute', () => {
    const rand = prng(7)
    const inputs = defaultInputs(def)
    const source = def.source ?? 'close'
    const candles = liveBars(80, rand)
    const runner = new IndicatorRunner(def, inputs, source)
    runner.reset(candles)
    for (let step = 0; step < 300; step++) {
      const last = candles[candles.length - 1]
      let from: number
      if (rand() < 0.7) {
        // Tick the forming bar.
        candles[candles.length - 1] = liveBar(last.time, last.close + (rand() - 0.5), rand)
        from = candles.length - 1
      } else {
        candles.push(liveBar(last.time + 3600, last.close + (rand() - 0.5), rand))
        from = candles.length - 1
      }
      runner.update(candles, from)
      expect(runner.output).toEqual(computeIndicator(def, candles, inputs, source))
    }
  })
})
