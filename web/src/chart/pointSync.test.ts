// Phase 19 (C5): the regime ribbon and the derivatives panes push a grown tail with
// series.update() and everything else with setData(). A fake series with LWC's update()
// semantics records the calls; the data it ends up showing must equal a plain setData of the
// new points in every case.

import type { IChartApi } from 'lightweight-charts'
import { describe, expect, it } from 'vitest'
import type { DerivativesPoint, RegimePoint, RegimeState } from '../data/labTypes'
import {
  DerivativesPanes,
  fundingHistogramData,
  openInterestLineData,
  type DerivativesChart,
  type DerivativesColors,
  type DerivativesHistogram,
  type DerivativesLine,
} from './derivativesPanes'
import {
  MAX_TAIL_UPDATES,
  RegimeRibbon,
  planPointSync,
  regimeRibbonData,
  syncPoints,
  type RegimeColors,
} from './regimeRibbon'

type P = { time: number; value?: number; color?: string }

/** A series with LWC's data semantics: update() replaces the last point or appends a newer one. */
class RecordingSeries {
  data: P[] = []
  calls: ('setData' | 'update')[] = []
  readonly options: Record<string, unknown>
  constructor(options: Record<string, unknown> = {}) {
    this.options = options
  }
  setData(d: P[]) {
    this.calls.push('setData')
    this.data = d.slice()
  }
  update(p: P) {
    this.calls.push('update')
    const last = this.data[this.data.length - 1]
    if (last !== undefined && p.time < last.time) throw new Error('Cannot update oldest data')
    if (last !== undefined && p.time === last.time) this.data[this.data.length - 1] = p
    else this.data.push(p)
  }
  applyOptions(o: Record<string, unknown>) {
    Object.assign(this.options, o)
  }
  priceScale() {
    return { applyOptions: () => {} }
  }
  createPriceLine() {
    return { applyOptions: () => {} }
  }
  getPane() {
    return { paneIndex: () => 1, getStretchFactor: () => 1, setStretchFactor: () => {} }
  }
}

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

const pt = (time: number, value?: number, color?: string): P =>
  value === undefined ? { time } : color === undefined ? { time, value } : { time, value, color }

describe('planPointSync', () => {
  const base = [pt(1, 1, 'g'), pt(2), pt(3, 1, 'r')]

  it('sets the first data, a shrink, and any change before the last point', () => {
    expect(planPointSync(null, base)).toEqual({ kind: 'set' })
    expect(planPointSync([], base)).toEqual({ kind: 'set' })
    expect(planPointSync(base, base.slice(0, 2))).toEqual({ kind: 'set' })
    expect(planPointSync(base, [pt(1, 1, 'x'), pt(2), pt(3, 1, 'r')])).toEqual({ kind: 'set' })
    expect(planPointSync(base, [pt(1, 1, 'g'), pt(2, 1, 'g'), pt(3, 1, 'r')])).toEqual({
      kind: 'set',
    })
  })

  it('does nothing when the points are unchanged (by value, not identity)', () => {
    expect(
      planPointSync(
        base,
        base.map((p) => ({ ...p })),
      ),
    ).toEqual({ kind: 'none' })
  })

  it('updates a rewritten last point and up to MAX_TAIL_UPDATES appended points', () => {
    expect(planPointSync(base, [...base.slice(0, 2), pt(3, 1, 'g')])).toEqual({
      kind: 'update',
      from: 2,
    })
    expect(planPointSync(base, [...base, pt(4, 1, 'g')])).toEqual({ kind: 'update', from: 3 })
    expect(planPointSync(base, [...base.slice(0, 2), pt(3), pt(4)])).toEqual({
      kind: 'update',
      from: 2,
    })
    const many = [...base, ...Array.from({ length: MAX_TAIL_UPDATES + 1 }, (_, i) => pt(10 + i))]
    expect(planPointSync(base, many)).toEqual({ kind: 'set' })
  })

  it('sets when the last point moved to another time or the tail is not ascending', () => {
    expect(planPointSync(base, [...base.slice(0, 2), pt(4, 1, 'r')])).toEqual({ kind: 'set' })
    expect(planPointSync(base, [...base, pt(3, 2)])).toEqual({ kind: 'set' })
  })
})

describe('syncPoints', () => {
  it('always leaves the series showing exactly the new points (random edits)', () => {
    const r = rng(19)
    for (let trial = 0; trial < 2000; trial++) {
      const n = Math.floor(r() * 8)
      const prev: P[] = []
      let t = 0
      for (let i = 0; i < n; i++) {
        t += 1 + Math.floor(r() * 2)
        prev.push(r() < 0.3 ? pt(t) : pt(t, Math.floor(r() * 2), r() < 0.5 ? 'g' : 'r'))
      }
      const next = prev.map((p) => ({ ...p }))
      const edit = r()
      if (edit < 0.25 && next.length > 0) next[next.length - 1] = pt(next[next.length - 1].time, 7)
      else if (edit < 0.5) for (let k = Math.floor(r() * 4); k > 0; k--) next.push(pt(++t + 1, 1))
      else if (edit < 0.6 && next.length > 0) next.splice(Math.floor(r() * next.length), 1)
      else if (edit < 0.7 && next.length > 1) next[0] = pt(next[0].time, 9)
      const series = new RecordingSeries()
      series.setData(prev)
      series.calls = []
      syncPoints(series, prev, next)
      expect(series.data).toEqual(next)
      const plan = planPointSync(prev, next)
      if (plan.kind === 'none') expect(series.calls).toEqual([])
      if (plan.kind === 'set') expect(series.calls).toEqual(['setData'])
      if (plan.kind === 'update') {
        expect(series.calls.every((c) => c === 'update')).toBe(true)
        expect(series.calls.length).toBe(next.length - plan.from)
      }
    }
  })
})

/* ------------------------------------- regime ribbon ------------------------------------- */

const RC: RegimeColors = { bull: 'G', bear: 'R', sideways: 'S' }

function ribbonSetup() {
  const added: RecordingSeries[] = []
  const chart = {
    addSeries: (_d: unknown, options: Record<string, unknown>) => {
      const s = new RecordingSeries(options)
      added.push(s)
      return s
    },
    removeSeries: () => {},
  }
  const ribbon = new RegimeRibbon(chart as unknown as IChartApi, RC)
  return { ribbon, series: added[0] }
}

describe('RegimeRibbon sync', () => {
  const H = 3600
  const points = (n: number): RegimePoint[] =>
    Array.from({ length: n }, (_, i) => ({
      time: i * H,
      state: i < 2 ? null : (((i % 3) - 1) as RegimeState),
    }))
  const plottedOf = (n: number) => new Set(Array.from({ length: n }, (_, i) => i * H))

  it('a bar close: the new bar alone costs nothing, its label is one update()', () => {
    const { ribbon, series } = ribbonSetup()
    ribbon.setPoints(points(10), plottedOf(10))
    expect(series.calls).toEqual(['setData'])
    // The bar closes: a new plotted bar, no label for it yet -> the points are unchanged.
    ribbon.setPoints(points(10), plottedOf(11))
    expect(series.calls).toEqual(['setData'])
    // The lab labels it: one more point -> update().
    ribbon.setPoints(points(11), plottedOf(11))
    expect(series.calls).toEqual(['setData', 'update'])
    expect(series.data).toEqual(regimeRibbonData(points(11), RC, plottedOf(11)))
  })

  it('a prepend, a recolour or a cleared strip is one setData with the same result', () => {
    const { ribbon, series } = ribbonSetup()
    ribbon.setPoints(points(10), plottedOf(10))
    const prepended = points(14).map((p) => ({ ...p, time: p.time - 4 * H }))
    const plotted = new Set(prepended.map((p) => p.time))
    ribbon.setPoints(prepended, plotted)
    expect(series.calls).toEqual(['setData', 'setData'])
    expect(series.data).toEqual(regimeRibbonData(prepended, RC, plotted))
    const next = { bull: 'g2', bear: 'r2', sideways: 's2' }
    ribbon.setColors(next)
    expect(series.calls).toEqual(['setData', 'setData', 'setData'])
    expect(series.data).toEqual(regimeRibbonData(prepended, next, plotted))
    ribbon.setPoints(null)
    expect(series.calls.at(-1)).toBe('setData')
    expect(series.data).toEqual([])
  })
})

/* ----------------------------------- derivatives panes ----------------------------------- */

const DC: DerivativesColors = { positive: 'P', negative: 'N', line: 'L', zero: 'Z' }

function derivativesSetup() {
  const series: RecordingSeries[] = []
  let panes = 1
  const chart: DerivativesChart = {
    addHistogram: () => {
      const s = new RecordingSeries()
      series.push(s)
      panes++
      return s as unknown as DerivativesHistogram
    },
    addLine: () => {
      const s = new RecordingSeries()
      series.push(s)
      panes++
      return s as unknown as DerivativesLine
    },
    removeSeries: () => {
      panes--
    },
    paneCount: () => panes,
  }
  const derivatives = new DerivativesPanes(DC)
  derivatives.attachChart(chart)
  return { derivatives, series }
}

describe('DerivativesPanes sync', () => {
  const H = 3600
  const funding = (n: number): DerivativesPoint[] =>
    Array.from({ length: n }, (_, i) => ({ time: i * H, value: i === 3 ? null : (i % 2) - 0.5 }))
  const oi = (n: number): DerivativesPoint[] =>
    Array.from({ length: n }, (_, i) => ({ time: i * H, value: i === 4 ? null : 1e9 + i }))
  const plottedOf = (n: number) => new Set(Array.from({ length: n }, (_, i) => i * H))

  it('a bar close with no new point costs no call; a new point is one update() per pane', () => {
    const { derivatives, series } = derivativesSetup()
    derivatives.setData({ funding: funding(10), openInterest: oi(10) }, plottedOf(10))
    const [f, o] = series
    expect([f.calls, o.calls]).toEqual([['setData'], ['setData']])
    derivatives.setData({ funding: funding(10), openInterest: oi(10) }, plottedOf(11))
    expect([f.calls, o.calls]).toEqual([['setData'], ['setData']])
    const grown = { funding: funding(11), openInterest: oi(11) }
    derivatives.setData(grown, plottedOf(11))
    expect([f.calls, o.calls]).toEqual([
      ['setData', 'update'],
      ['setData', 'update'],
    ])
    expect(f.data).toEqual(fundingHistogramData(grown.funding, DC, plottedOf(11)))
    expect(o.data).toEqual(openInterestLineData(grown.openInterest, plottedOf(11)))
  })

  it('a longer history or a recolour is one setData with the same result', () => {
    const { derivatives, series } = derivativesSetup()
    derivatives.setData({ funding: funding(10), openInterest: oi(10) }, plottedOf(10))
    const [f, o] = series
    const longer = { funding: funding(20), openInterest: oi(20) }
    derivatives.setData(longer, plottedOf(20))
    expect([f.calls, o.calls]).toEqual([
      ['setData', 'setData'],
      ['setData', 'setData'],
    ])
    expect(f.data).toEqual(fundingHistogramData(longer.funding, DC, plottedOf(20)))
    expect(o.data).toEqual(openInterestLineData(longer.openInterest, plottedOf(20)))
    const next = { positive: 'p2', negative: 'n2', line: 'l2', zero: 'z2' }
    derivatives.setColors(next)
    expect(f.calls.at(-1)).toBe('setData')
    expect(f.data).toEqual(fundingHistogramData(longer.funding, next, plottedOf(20)))
  })
})
