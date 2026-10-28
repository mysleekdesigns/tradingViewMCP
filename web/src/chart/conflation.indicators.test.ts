// Phase 14 data conflation, indicator side. Conflation is a Lightweight Charts time-scale option
// that only changes how bars are drawn; this file proves the part we own: on a history long
// enough to switch conflation on (and precompute it), the indicator manager still computes on
// every bar and hands setData every bar of every indicator line, equal to computeIndicator, and
// live ticks / appends touch only the tail. It does NOT prove anything about LWC's rendering: the
// fake chart below records data and ignores time-scale options, so whether LWC draws conflated
// bars correctly is a browser check. Synthetic 1m candles (seeded LCG, no network), 120,000
// bars: past the 100k of the Phase 14 check and past every conflation threshold.
import { HistogramSeries, type IChartApi } from 'lightweight-charts'
import { describe, expect, it } from 'vitest'
import { computeIndicator } from '../indicators/compute'
import { INDICATOR_BY_ID } from '../indicators/registry'
import { IndicatorRunner } from '../indicators/runner'
import type {
  IndicatorDef,
  IndicatorId,
  IndicatorInputs,
  IndicatorOutput,
} from '../indicators/types'
import { createIndicatorConfig, type IndicatorConfig } from '../state/indicators'
import type { Candle } from '../types'
import { conflationChartOptions, conflationOptions } from './conflation'
import { IndicatorSeriesManager } from './indicatorSeries'
import { planSeriesSync } from './seriesSync'

const N = 120_000
const STEP = 60
const START = 1_700_000_040 - (1_700_000_040 % 86_400) // a UTC midnight, so VWAP sessions are whole days

/** Seeded random walk (LCG, seed 14): high > low on every bar, volume > 0. */
function makeCandles(n: number, start = START, seed = 14): Candle[] {
  let state = seed >>> 0
  const rand = () => {
    state = (Math.imul(state, 1_664_525) + 1_013_904_223) >>> 0
    return state / 2 ** 32
  }
  const out: Candle[] = []
  let close = 30_000
  for (let i = 0; i < n; i++) {
    const open = close
    close = Math.max(1, open * (1 + (rand() - 0.5) * 0.004))
    const high = Math.max(open, close) * (1 + rand() * 0.001) + 0.01
    const low = Math.min(open, close) * (1 - rand() * 0.001) - 0.01
    out.push({ time: start + i * STEP, open, high, low, close, volume: 1 + rand() * 50 })
  }
  return out
}

function def(id: IndicatorId): IndicatorDef {
  const d = INDICATOR_BY_ID.get(id)
  if (!d) throw new Error(`no def ${id}`)
  return d
}

/**
 * Bars before each line's first value at default inputs, worked out by hand from the
 * definitions (SMA-seeded smoothers, TradingView conventions):
 * SMA/EMA/BB/Vol MA 20 -> first value at bar 19; RSI 14 needs 14 price changes -> bar 14;
 * MACD 12/26/9: line at bar 25 (slow EMA), signal = EMA 9 of the line -> bar 25 + 8 = 33,
 * histogram = line - signal -> bar 33; Stoch 14/1/3: %K at bar 13, %D = SMA 3 of %K -> bar 15;
 * session VWAP has a value on every bar with volume; anchored VWAP (anchor = bar AVWAP_AT) starts
 * at its anchor bar (every bar has volume). AVWAP's entry is relative to the anchor's index in
 * the history used (see `warmup`).
 */
const WARMUP: Partial<Record<IndicatorId, Record<string, number>>> = {
  sma: { sma: 19 },
  ema: { ema: 19 },
  bb: { basis: 19, upper: 19, lower: 19 },
  vwap: { vwap: 0 },
  avwap: { avwap: 0 },
  volma: { ma: 19 },
  rsi: { rsi: 14 },
  macd: { macd: 25, signal: 33, hist: 33 },
  stoch: { k: 13, d: 15 },
}
const IDS = Object.keys(WARMUP) as IndicatorId[]

const candles = makeCandles(N)

/** Anchored VWAP's anchor: the open time of bar AVWAP_AT of the base history. */
const AVWAP_AT = 1_000
const AVWAP_ANCHOR = candles[AVWAP_AT].time

/** Bars before the first value of `id:key` in `bars` (AVWAP: the anchor bar's index). */
function warmup(id: IndicatorId, key: string, bars: readonly Candle[] = candles): number {
  const warm = WARMUP[id]?.[key]
  if (warm === undefined) throw new Error(`no warm-up for ${id}:${key}`)
  if (id !== 'avwap') return warm
  const at = bars.findIndex((c) => c.time === AVWAP_ANCHOR)
  if (at < 0) throw new Error('anchor bar not in history')
  return at + warm
}

/** Inputs every test uses: defaults, plus the anchor for anchored VWAP. */
function inputsFor(d: IndicatorDef, cfgs: readonly IndicatorConfig[] = configs()): IndicatorInputs {
  const c = cfgs.find((x) => x.type === d.id)
  if (!c) throw new Error(`no config for ${d.id}`)
  return c.inputs
}

/* ------------------------------- fake chart (records data) ------------------------------- */

class FakeSeries {
  readonly kind: 'Line' | 'Histogram'
  data: { time: number; value: number }[] = []
  setDataCalls = 0
  updates: { time: number; value: number }[] = []
  constructor(kind: 'Line' | 'Histogram') {
    this.kind = kind
  }
  setData(data: { time: number; value: number }[]) {
    this.setDataCalls++
    this.data = data
  }
  update(p: { time: number; value: number }) {
    this.updates.push(p)
    // In place: the manager hands setData a fresh array each time.
    const last = this.data.at(-1)
    if (last && last.time === p.time) this.data[this.data.length - 1] = p
    else this.data.push(p)
  }
  applyOptions() {}
  priceScale() {
    return { applyOptions: () => {} }
  }
  createPriceLine() {
    return { applyOptions: () => {} }
  }
}

class FakePane {
  series: FakeSeries[] = []
  stretch = 1
  getSeries() {
    return this.series
  }
  setStretchFactor(f: number) {
    this.stretch = f
  }
  getStretchFactor() {
    return this.stretch
  }
}

/** Minimal IChartApi that also keeps the time scale options applied to it. */
class FakeChart {
  panesList: FakePane[] = [new FakePane()]
  timeScaleOptions: Record<string, unknown> = { enableConflation: false }
  applyOptions(o: { timeScale?: Record<string, unknown> }) {
    if (o.timeScale) Object.assign(this.timeScaleOptions, o.timeScale)
  }
  addSeries(definition: unknown, _options: unknown, paneIndex = 0) {
    const s = new FakeSeries(definition === HistogramSeries ? 'Histogram' : 'Line')
    const i = Math.min(paneIndex, this.panesList.length)
    if (i === this.panesList.length) this.panesList.push(new FakePane())
    this.panesList[i].series.push(s)
    return s
  }
  removeSeries(s: FakeSeries) {
    for (const p of this.panesList) p.series = p.series.filter((x) => x !== s)
  }
  panes() {
    return this.panesList
  }
  removePane(i: number) {
    this.panesList.splice(i, 1)
  }
}

function configs(): IndicatorConfig[] {
  const out: IndicatorConfig[] = []
  for (const id of IDS) {
    const c = createIndicatorConfig(def(id), out, `${id}-1`)
    out.push(id === 'avwap' ? { ...c, inputs: { ...c.inputs, anchor: AVWAP_ANCHOR } } : c)
  }
  return out
}

/**
 * What every series of every indicator holds, keyed `${uid}:${line}`: each fake series is mapped
 * back to its line through the order the manager builds them in.
 */
function shown(
  chart: FakeChart,
  cfgs: readonly IndicatorConfig[],
  manager: IndicatorSeriesManager,
) {
  const byLine = new Map<string, { time: number; value: number }[]>()
  const all = chart.panesList.flatMap((p) => p.series)
  // Series were created per indicator in config order, histograms first, within their pane.
  const byPane = new Map<number, FakeSeries[]>()
  chart.panesList.forEach((p, i) => byPane.set(i, [...p.series]))
  for (const c of cfgs) {
    const d = def(c.type)
    const pane = byPane.get(manager.paneIndexOf(c.uid)) ?? []
    const ordered = [
      ...d.lines.filter((l) => l.kind === 'histogram'),
      ...d.lines.filter((l) => l.kind !== 'histogram'),
    ]
    for (const line of ordered) {
      const s = pane.shift()
      if (!s) throw new Error(`no series for ${c.uid}:${line.key}`)
      byLine.set(`${c.uid}:${line.key}`, s.data)
    }
  }
  expect(byLine.size).toBe(all.length)
  return { byLine }
}

/** Manager on a fake chart with the conflation options applied (the fake only records them). */
function setup(bars: readonly Candle[] = candles) {
  const chart = new FakeChart()
  chart.applyOptions(conflationChartOptions(bars.length, '1m'))
  const manager = new IndicatorSeriesManager()
  manager.attach(chart as unknown as IChartApi)
  manager.applyPlan(planSeriesSync(null, bars), bars)
  const cfgs = configs()
  manager.sync(cfgs, bars)
  return { chart, manager, cfgs }
}

type Point = { time: number; value: number; color?: string }

/**
 * Index of the first point where `a` and `b` differ (time, value with Object.is, colour), or
 * -1 when they are identical. A plain loop: toEqual on 120k-element arrays is slow.
 */
function firstDiff(a: readonly Point[], b: readonly Point[]): number {
  const n = Math.min(a.length, b.length)
  for (let i = 0; i < n; i++) {
    const x = a[i]
    const y = b[i]
    if (x.time !== y.time || !Object.is(x.value, y.value) || x.color !== y.color) return i
  }
  return a.length === b.length ? -1 : n
}

/** `shown` points against the independent full computation: same times and exact values. */
function firstDiffFromReference(shown: readonly Point[], out: IndicatorOutput, key: string) {
  const ref = out[key] ?? []
  return firstDiff(
    shown.map((p) => ({ time: p.time, value: p.value })),
    ref.map((p) => ({ time: p.time, value: p.value })),
  )
}

/* ------------------------------------------ tests ------------------------------------------ */

describe('conflated-length history: indicators compute and are handed every bar', () => {
  it('the test history is long enough to switch conflation and precompute on', () => {
    expect(conflationOptions(N, '1m')).toMatchObject({
      enableConflation: true,
      precomputeConflationOnInit: true,
    })
  })

  it('the runner yields one value per bar after warm-up, at every candle time', () => {
    for (const id of IDS) {
      const d = def(id)
      const runner = new IndicatorRunner(d, inputsFor(d), d.source ?? 'close')
      const out = runner.reset(candles)
      // Independent path: the plain stepper loop gives exactly the same numbers.
      const reference = computeIndicator(d, candles, inputsFor(d))
      for (const line of d.lines) {
        const warm = warmup(id, line.key)
        const points = out[line.key]
        expect(points.length, `${id}:${line.key}`).toBe(N - warm)
        expect(firstDiff(points, reference[line.key]), `${id}:${line.key}`).toBe(-1)
        // No merged or skipped bars: point i sits on candle i + warm-up.
        let gaps = 0
        for (let i = 0; i < points.length; i++) {
          if (points[i].time !== candles[i + warm].time) gaps++
        }
        expect(gaps, `${id}:${line.key}`).toBe(0)
      }
    }
  })

  it('setData receives every bar of every indicator line, equal to computeIndicator', () => {
    const { chart, manager, cfgs } = setup()
    expect(chart.timeScaleOptions.enableConflation).toBe(true)
    const view = shown(chart, cfgs, manager)
    expect(view.byLine.size).toBe(IDS.reduce((s, id) => s + def(id).lines.length, 0))
    for (const c of cfgs) {
      const d = def(c.type)
      const reference = computeIndicator(d, candles, c.inputs)
      for (const line of d.lines) {
        const key = `${c.uid}:${line.key}`
        const data = view.byLine.get(key) ?? []
        expect(data.length, key).toBe(N - warmup(c.type, line.key))
        expect(firstDiffFromReference(data, reference, line.key), key).toBe(-1)
      }
    }
    // Every series got its data in one setData call.
    for (const s of chart.panesList.flatMap((p) => p.series)) expect(s.setDataCalls).toBe(1)
  })

  it('live ticks and appended bars update only the tail and match a full recompute', () => {
    const { chart, manager, cfgs } = setup()
    const last = candles[N - 1]
    const ticked = [
      ...candles.slice(0, -1),
      { ...last, close: last.close * 1.01, high: last.high * 1.01 },
    ]
    manager.applyPlan(planSeriesSync(candles, ticked), ticked)
    const next = { ...last, time: last.time + STEP, open: last.close, volume: 7 }
    const appended = [...ticked, next]
    manager.applyPlan(planSeriesSync(ticked, appended), appended)

    const view = shown(chart, cfgs, manager)
    for (const c of cfgs) {
      const d = def(c.type)
      const reference = computeIndicator(d, appended, c.inputs)
      for (const line of d.lines) {
        const data = view.byLine.get(`${c.uid}:${line.key}`) ?? []
        expect(data.length).toBe(N + 1 - warmup(c.type, line.key, appended))
        expect(firstDiffFromReference(data, reference, line.key), `${c.uid}:${line.key}`).toBe(-1)
      }
    }
    // Tail only, never a full setData per tick: the tick replays the last bar (1 update), the
    // append replays from the checkpoint before it, i.e. the old last bar and the new one (2).
    for (const s of chart.panesList.flatMap((p) => p.series)) {
      expect(s.setDataCalls).toBe(1)
      expect(s.updates.length).toBe(3)
    }
  })

  it('scroll-back paging hands the whole longer history to setData', () => {
    const { chart, manager, cfgs } = setup()
    const older = makeCandles(1_000, START - 1_000 * STEP, 99)
    const grown = [...older, ...candles]
    expect(planSeriesSync(candles, grown).kind).toBe('prepend')
    chart.applyOptions(conflationChartOptions(grown.length, '1m'))
    manager.applyPlan(planSeriesSync(candles, grown), grown)
    const view = shown(chart, cfgs, manager)
    for (const c of cfgs) {
      const d = def(c.type)
      const reference = computeIndicator(d, grown, c.inputs)
      for (const line of d.lines) {
        const data = view.byLine.get(`${c.uid}:${line.key}`) ?? []
        expect(data.length).toBe(grown.length - warmup(c.type, line.key, grown))
        expect(firstDiffFromReference(data, reference, line.key), `${c.uid}:${line.key}`).toBe(-1)
      }
    }
  })
})
