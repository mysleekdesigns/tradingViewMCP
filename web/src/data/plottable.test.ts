import { describe, expect, it } from 'vitest'
import { planSeriesSync } from '../chart/seriesSync'
import { computeIndicator, defaultInputs } from '../indicators/compute'
import { INDICATORS } from '../indicators/registry'
import { IndicatorRunner } from '../indicators/runner'
import type { IndicatorDef, IndicatorOutput } from '../indicators/types'
import type { Candle } from '../types'
import { applyLiveCandle } from './candleStore'
import { isTradedBar, plottableCandles } from './plottable'

const M = 60

/** A traded bar (volume > 0). */
function traded(time: number, close: number, volume = 2): Candle {
  return { time, open: close - 0.5, high: close + 1, low: close - 1, close, volume }
}

/** What Binance returns for an interval with no trades: flat at the previous close, volume 0. */
function flat(time: number, price: number): Candle {
  return { time, open: price, high: price, low: price, close: price, volume: 0 }
}

/** Deterministic PRNG (mulberry32) so the random sequences are reproducible. */
function rng(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

/** A thin pair: about `pZero` of the bars had no trade. */
function thinSeries(n: number, pZero: number, seed: number): Candle[] {
  const r = rng(seed)
  const out: Candle[] = []
  let price = 1
  for (let i = 0; i < n; i++) {
    if (i > 0 && r() < pZero) {
      out.push(flat(i * M, price))
      continue
    }
    const open = price
    price = Math.max(0.01, price * (1 + (r() - 0.5) * 0.02))
    out.push({
      time: i * M,
      open,
      high: Math.max(open, price) * (1 + r() * 0.005),
      low: Math.min(open, price) * (1 - r() * 0.005),
      close: price,
      volume: 1 + Math.floor(r() * 100),
    })
  }
  return out
}

describe('isTradedBar', () => {
  it('is true only for a positive volume', () => {
    expect(isTradedBar(traded(0, 1, 0.001))).toBe(true)
    expect(isTradedBar(flat(0, 1))).toBe(false)
    expect(isTradedBar({ ...flat(0, 1), volume: Number.NaN })).toBe(false)
  })
})

describe('plottableCandles', () => {
  it('drops every bar without trades and keeps the traded ones in order', () => {
    const raw = [traded(0, 1), flat(M, 1), flat(2 * M, 1), traded(3 * M, 2), flat(4 * M, 2)]
    const out = plottableCandles(raw)
    expect(out.map((c) => c.time)).toEqual([0, 3 * M])
  })

  it('keeps element identity and never synthesises or forward-fills a bar', () => {
    const raw = thinSeries(300, 0.5, 7)
    const out = plottableCandles(raw)
    const rawSet = new Set(raw)
    expect(out.every((c) => rawSet.has(c))).toBe(true)
    expect(out).toHaveLength(raw.filter((c) => c.volume > 0).length)
    for (let i = 1; i < out.length; i++) expect(out[i].time).toBeGreaterThan(out[i - 1].time)
  })

  it('returns the raw array itself when every bar traded (liquid pairs are unchanged)', () => {
    const raw = [traded(0, 1), traded(M, 2), traded(2 * M, 3)]
    expect(plottableCandles(raw)).toBe(raw)
    expect(plottableCandles(raw, [raw[0]])).toBe(raw)
  })

  it('returns the previous plotted array when the traded bars are the same objects', () => {
    const a = traded(0, 1)
    const b = traded(M, 2)
    const prev = plottableCandles([a, flat(2 * M, 2), b])
    // A tick on the untraded forming bar: new raw array, same traded bars.
    const next = plottableCandles([a, flat(2 * M, 2), b, flat(3 * M, 2)], prev)
    expect(next).toBe(prev)
  })

  it('returns a new array when a traded bar changed', () => {
    const a = traded(0, 1)
    const prev = plottableCandles([a, flat(M, 1), traded(2 * M, 2)])
    const next = plottableCandles([a, flat(M, 1), traded(2 * M, 3)], prev)
    expect(next).not.toBe(prev)
    expect(next[1].close).toBe(3)
  })

  it('is empty when no bar traded', () => {
    expect(plottableCandles([flat(0, 1), flat(M, 1)])).toEqual([])
    expect(plottableCandles([])).toEqual([])
  })
})

describe('live forming bar on the plotted series', () => {
  const a = traded(0, 10)
  const b = traded(M, 11)
  const base = [a, b]

  it('a forming bar without trades is not shown and needs no series work', () => {
    const prev = plottableCandles(base)
    const raw = applyLiveCandle(base, flat(2 * M, 11))
    const next = plottableCandles(raw, prev)
    expect(next).toBe(prev)
    expect(planSeriesSync(prev, next)).toEqual({ kind: 'none' })
  })

  it('appears on its first trade as an appended bar, then ticks as an update', () => {
    let raw = applyLiveCandle(base, flat(2 * M, 11))
    let shown = plottableCandles(raw)
    raw = applyLiveCandle(raw, traded(2 * M, 12, 0.5))
    const first = plottableCandles(raw, shown)
    expect(first.map((c) => c.time)).toEqual([0, M, 2 * M])
    expect(planSeriesSync(shown, first)).toEqual({ kind: 'update', from: 2 })
    shown = first
    raw = applyLiveCandle(raw, traded(2 * M, 13, 1.5))
    const second = plottableCandles(raw, shown)
    expect(second[2].close).toBe(13)
    expect(planSeriesSync(shown, second)).toEqual({ kind: 'update', from: 2 })
  })

  it('a bar that closes without trades never appears', () => {
    const shown = plottableCandles(base)
    let raw = applyLiveCandle(base, flat(2 * M, 11))
    raw = applyLiveCandle(raw, flat(2 * M, 11)) // closed tick, still volume 0
    raw = applyLiveCandle(raw, flat(3 * M, 11)) // next bar opens, also empty
    const next = plottableCandles(raw, shown)
    expect(next).toBe(shown)
    expect(next.some((c) => c.time === 2 * M || c.time === 3 * M)).toBe(false)
  })

  it('a plotted bar updated to volume 0 is removed with a full set (no crash)', () => {
    const shown = plottableCandles(base)
    const raw = applyLiveCandle(base, flat(M, 10))
    const next = plottableCandles(raw, shown)
    expect(next.map((c) => c.time)).toEqual([0])
    expect(planSeriesSync(shown, next)).toEqual({ kind: 'set' })
  })
})

/** What IndicatorSeriesManager.applyPlan does to a runner, without a chart. */
function drive(runner: IndicatorRunner, prev: readonly Candle[] | null, next: readonly Candle[]) {
  const plan = planSeriesSync(prev, next)
  if (plan.kind === 'set' || plan.kind === 'prepend') runner.reset(next)
  else if (plan.kind === 'update') runner.update(next, plan.from)
}

function expectSameOutput(def: IndicatorDef, got: IndicatorOutput, want: IndicatorOutput) {
  for (const line of def.lines) {
    const g = got[line.key]
    const w = want[line.key]
    expect(g.map((p) => p.time)).toEqual(w.map((p) => p.time))
    // Same steps in the same order: bit-identical, so no tolerance.
    expect(g.map((p) => p.value)).toEqual(w.map((p) => p.value))
  }
}

describe('indicators over the plotted series', () => {
  it.each(INDICATORS.map((d) => [d.id, d] as const))(
    '%s: live ticks with bars without trades equal a full recompute over traded bars',
    (_id, def) => {
      const r = rng(42)
      const inputs = defaultInputs(def)
      const runner = new IndicatorRunner(def, inputs, def.source ?? 'close')
      let raw: readonly Candle[] = thinSeries(120, 0.4, 3)
      let shown = plottableCandles(raw)
      drive(runner, null, shown)
      let price = raw[raw.length - 1].close
      let time = raw[raw.length - 1].time
      let forming: Candle | null = null
      for (let step = 0; step < 400; step++) {
        const roll = r()
        if (forming === null || roll < 0.15) {
          // A new bar opens with no trade yet (Binance sends it flat at the last price).
          time += M
          forming = flat(time, price)
        } else if (roll < 0.75) {
          // A trade in the forming bar.
          price = Math.max(0.01, price * (1 + (r() - 0.5) * 0.01))
          forming = {
            time,
            open: forming.open,
            high: Math.max(forming.high, price),
            low: Math.min(forming.low, price),
            close: price,
            volume: forming.volume + 1 + Math.floor(r() * 5),
          }
        }
        // else: a repeated tick with unchanged values (e.g. the closed x: true tick).
        raw = applyLiveCandle(raw, forming)
        const next = plottableCandles(raw, shown)
        drive(runner, shown, next)
        shown = next
      }
      const traded = raw.filter((c) => c.volume > 0)
      expect(shown).toEqual(traded)
      expectSameOutput(def, runner.output, computeIndicator(def, traded, inputs))
    },
  )

  it.each(INDICATORS.map((d) => [d.id, d] as const))(
    '%s: bars without trades have no effect on the values (they are not inputs)',
    (_id, def) => {
      const inputs = defaultInputs(def)
      const raw = thinSeries(200, 0.5, 11)
      const onlyTraded = raw.filter((c) => c.volume > 0)
      expectSameOutput(
        def,
        computeIndicator(def, plottableCandles(raw), inputs),
        computeIndicator(def, onlyTraded, inputs),
      )
    },
  )

  it.each(INDICATORS.map((d) => [d.id, d] as const))(
    '%s: no look-ahead on the plotted series (changing a later bar keeps earlier values)',
    (_id, def) => {
      const inputs = defaultInputs(def)
      const shown = plottableCandles(thinSeries(200, 0.5, 19))
      const k = Math.floor(shown.length * 0.6)
      const perturbed = shown.map((c, i) =>
        i > k ? { ...c, close: c.close * 3, high: c.high * 3, volume: c.volume * 7 } : c,
      )
      const a = computeIndicator(def, shown, inputs)
      const b = computeIndicator(def, perturbed, inputs)
      const cut = shown[k].time
      for (const line of def.lines) {
        const before = (o: IndicatorOutput) => o[line.key].filter((p) => p.time <= cut)
        expect(before(b)).toEqual(before(a))
      }
    },
  )
})
