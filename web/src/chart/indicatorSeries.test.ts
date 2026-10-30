import { HistogramSeries, type IChartApi } from 'lightweight-charts'
import { describe, expect, it } from 'vitest'
import { computeIndicator } from '../indicators/compute'
import { INDICATOR_BY_ID } from '../indicators/registry'
import type { IndicatorDef, IndicatorId } from '../indicators/types'
import { createIndicatorConfig, type IndicatorConfig } from '../state/indicators'
import type { Candle } from '../types'
import { VOLUME_PROFILE_HINDSIGHT } from '../indicators/volumeProfile'
import {
  ANCHOR_NOT_LOADED_NOTE,
  ANCHOR_NOT_SET_NOTE,
  anchorNote,
  assignPanes,
  buildLegendRow,
  diffIndicators,
  formatIndicatorValue,
  formatInputNumber,
  formatTimeInput,
  histogramColor,
  indicatorPriceFormat,
  indicatorTitle,
  IndicatorSeriesManager,
  MARKET_STRUCTURE_HINT,
  ownsPane,
  SESSION_RANGES_HINT,
  paneGuideValues,
  sessionAlignmentNote,
  paneIndicatorUids,
} from './indicatorSeries'
import { planSeriesSync } from './seriesSync'

function def(id: IndicatorId): IndicatorDef {
  const d = INDICATOR_BY_ID.get(id)
  if (!d) throw new Error(`no def ${id}`)
  return d
}

function cfg(id: IndicatorId, uid: string, existing: readonly IndicatorConfig[] = []) {
  return createIndicatorConfig(def(id), existing, uid)
}

function makeCandles(n: number, start = 1_700_000_000, step = 3600): Candle[] {
  const out: Candle[] = []
  for (let i = 0; i < n; i++) {
    const close = 100 + 10 * Math.sin(i / 5) + i * 0.1
    out.push({
      time: start + i * step,
      open: close - 0.5,
      high: close + 1,
      low: close - 1.5,
      close,
      volume: 1000 + (i % 7) * 100,
    })
  }
  return out
}

/* ---------------------------------- pure helpers ---------------------------------- */

describe('diffIndicators', () => {
  const ema = cfg('ema', 'ema-a')
  const rsi = cfg('rsi', 'rsi-a')
  const macd = cfg('macd', 'macd-a')

  it('adds and removes by uid', () => {
    const d = diffIndicators([ema], [ema, rsi])
    expect(d.create).toEqual(['rsi-a'])
    expect(d.remove).toEqual([])
    expect(d.rebuildPanes).toBe(true)
    const r = diffIndicators([ema, rsi], [rsi])
    expect(r.remove).toEqual(['ema-a'])
    expect(r.create).toEqual([])
    expect(r.rebuildPanes).toBe(false)
  })

  it('no-op for identical lists', () => {
    const d = diffIndicators([ema, rsi], [ema, rsi])
    expect(d).toEqual({
      create: [],
      remove: [],
      recompute: [],
      recolor: [],
      visibility: [],
      rebuildPanes: false,
    })
  })

  it('reordering pane indicators rebuilds panes; reordering overlays does not', () => {
    expect(diffIndicators([rsi, macd], [macd, rsi]).rebuildPanes).toBe(true)
    const sma = cfg('sma', 'sma-a')
    expect(diffIndicators([ema, sma, rsi], [sma, ema, rsi]).rebuildPanes).toBe(false)
  })

  it('inputs or source change → recompute only', () => {
    const d = diffIndicators([ema], [{ ...ema, inputs: { period: 50 } }])
    expect(d.recompute).toEqual(['ema-a'])
    expect(d.recolor).toEqual([])
    const s = diffIndicators([ema], [{ ...ema, source: 'hl2' }])
    expect(s.recompute).toEqual(['ema-a'])
  })

  it('colour change → recolor only (case-insensitive)', () => {
    const d = diffIndicators([ema], [{ ...ema, colors: { ema: '#00ff00' } }])
    expect(d.recolor).toEqual(['ema-a'])
    expect(d.recompute).toEqual([])
    const same = diffIndicators(
      [{ ...ema, colors: { ema: '#abcdef' } }],
      [{ ...ema, colors: { ema: '#ABCDEF' } }],
    )
    expect(same.recolor).toEqual([])
  })

  it('visibility flip', () => {
    const d = diffIndicators([rsi], [{ ...rsi, visible: false }])
    expect(d.visibility).toEqual(['rsi-a'])
    expect(d.rebuildPanes).toBe(false)
  })

  it('type change under the same uid is remove + create', () => {
    const d = diffIndicators([ema], [{ ...rsi, uid: 'ema-a' }])
    expect(d.remove).toEqual(['ema-a'])
    expect(d.create).toEqual(['ema-a'])
    expect(d.rebuildPanes).toBe(true)
  })
})

describe('pane assignment', () => {
  it('overlays and volume on pane 0, pane indicators 1..n in order', () => {
    const items = [
      cfg('rsi', 'r'),
      cfg('ema', 'e'),
      cfg('volma', 'v'),
      cfg('macd', 'm'),
      cfg('stoch', 's'),
    ]
    expect(Object.fromEntries(assignPanes(items))).toEqual({ r: 1, e: 0, v: 0, m: 2, s: 3 })
    expect(paneIndicatorUids(items)).toEqual(['r', 'm', 's'])
  })
})

describe('histogramColor', () => {
  it('TradingView four-colour scheme', () => {
    expect(histogramColor(2, 1)).toBe('#26a69a')
    expect(histogramColor(2, 2)).toBe('#26a69a')
    expect(histogramColor(1, 2)).toBe('#b2dfdb')
    expect(histogramColor(-2, -1)).toBe('#ff5252')
    expect(histogramColor(-1, -2)).toBe('#ffcdd2')
    expect(histogramColor(0, 1)).toBe('#b2dfdb')
  })
  it('first bar counts as rising', () => {
    expect(histogramColor(1, undefined)).toBe('#26a69a')
    expect(histogramColor(-1, undefined)).toBe('#ffcdd2')
  })
  it('custom colour replaces strong positive, faded = 50% alpha', () => {
    expect(histogramColor(2, 1, '#2962ff')).toBe('#2962ff')
    expect(histogramColor(1, 2, '#2962ff')).toBe('#2962ff80')
    expect(histogramColor(-2, -1, '#2962ff')).toBe('#ff5252')
  })
})

describe('legend text', () => {
  it('formats input numbers without trailing zeros', () => {
    expect(formatInputNumber(2)).toBe('2')
    expect(formatInputNumber(2.0)).toBe('2')
    expect(formatInputNumber(0.5)).toBe('0.5')
    expect(formatInputNumber(0.1 + 0.2)).toBe('0.3')
  })

  it('titles', () => {
    expect(indicatorTitle(def('ema'), cfg('ema', 'a'))).toBe('EMA 20 close')
    expect(indicatorTitle(def('bb'), cfg('bb', 'a'))).toBe('BB 20 2 close')
    expect(indicatorTitle(def('bb'), { inputs: { period: 10, mult: 0.5 }, source: 'hl2' })).toBe(
      'BB 10 0.5 hl2',
    )
    expect(indicatorTitle(def('macd'), cfg('macd', 'a'))).toBe('MACD 12 26 9 close')
    expect(indicatorTitle(def('vwap'), cfg('vwap', 'a'))).toBe('VWAP')
    expect(indicatorTitle(def('volma'), cfg('volma', 'a'))).toBe('Vol MA 20')
    expect(indicatorTitle(def('stoch'), cfg('stoch', 'a'))).toBe('Stoch 14 1 3')
    expect(indicatorTitle(def('rsi'), cfg('rsi', 'a'))).toBe('RSI 14 close')
  })

  it('titles show set anchor times as compact UTC and leave out unset ones', () => {
    const anchored = { anchor: 1_700_000_040 }
    expect(formatTimeInput(1_700_000_040)).toBe('2023-11-14 22:14')
    expect(indicatorTitle(def('avwap'), { inputs: anchored, source: 'hlc3' })).toBe(
      'AVWAP 2023-11-14 22:14 hlc3',
    )
    expect(indicatorTitle(def('avwap'), { inputs: { anchor: 0 }, source: 'hlc3' })).toBe(
      'AVWAP hlc3',
    )
    expect(indicatorTitle(def('avwap'), { inputs: {}, source: 'close' })).toBe('AVWAP close')
    const range = { from: 1_700_000_040, to: 0, rows: 24, valueArea: 70 }
    expect(indicatorTitle(def('vprofile'), { inputs: range, source: 'close' })).toBe(
      'VP 2023-11-14 22:14 24 70',
    )
    const bounded = { ...range, to: 1_700_086_440 }
    expect(indicatorTitle(def('vprofile'), { inputs: bounded, source: 'close' })).toBe(
      'VP 2023-11-14 22:14 2023-11-15 22:14 24 70',
    )
    // Two AVWAPs on different anchors are told apart.
    expect(
      indicatorTitle(def('avwap'), { inputs: { anchor: 1_700_003_640 }, source: 'hlc3' }),
    ).not.toBe(indicatorTitle(def('avwap'), { inputs: anchored, source: 'hlc3' }))
  })

  it('anchor note: not set, before the loaded history, or none', () => {
    const first = 1_700_000_000
    const avwap = (anchor: number) => ({ inputs: { anchor } })
    expect(anchorNote(def('avwap'), avwap(0), first)).toBe(ANCHOR_NOT_SET_NOTE)
    expect(anchorNote(def('avwap'), { inputs: {} }, first)).toBe(ANCHOR_NOT_SET_NOTE)
    expect(anchorNote(def('avwap'), avwap(first - 1), first)).toBe(ANCHOR_NOT_LOADED_NOTE)
    expect(anchorNote(def('avwap'), avwap(first), first)).toBeUndefined()
    expect(anchorNote(def('avwap'), avwap(first + 3600), first)).toBeUndefined()
    // No bars yet: only "not set" can be told.
    expect(anchorNote(def('avwap'), avwap(first - 1), undefined)).toBeUndefined()
    // VP: `from` is the anchor; `to` (second time input) is not checked.
    const vp = (from: number) => ({ inputs: { from, to: 0, rows: 24, valueArea: 70 } })
    expect(anchorNote(def('vprofile'), vp(0), first)).toBe(ANCHOR_NOT_SET_NOTE)
    expect(anchorNote(def('vprofile'), vp(first - 60), first)).toBe(ANCHOR_NOT_LOADED_NOTE)
    expect(anchorNote(def('vprofile'), vp(first + 60), first)).toBeUndefined()
    // Indicators without a time input never get a note.
    expect(anchorNote(def('ema'), cfg('ema', 'e'), first)).toBeUndefined()
  })

  it('legend rows carry the anchor note and the range-profile hindsight hint', () => {
    const first = 1_700_000_000
    const avwap = { ...cfg('avwap', 'a'), inputs: { anchor: first - 3600 } }
    const row = buildLegendRow(def('avwap'), avwap, 0, {}, {}, first)
    expect(row.note).toBe(ANCHOR_NOT_LOADED_NOTE)
    expect(row.hint).toBeUndefined()
    expect(buildLegendRow(def('avwap'), cfg('avwap', 'b'), 0, {}).note).toBe(ANCHOR_NOT_SET_NOTE)
    const set = { ...cfg('avwap', 'c'), inputs: { anchor: first } }
    expect(buildLegendRow(def('avwap'), set, 0, { avwap: 1 }, {}, first)).not.toHaveProperty('note')
    const vp = buildLegendRow(def('vprofile'), cfg('vprofile', 'v'), 0, {}, {}, first)
    expect(vp.note).toBe(ANCHOR_NOT_SET_NOTE)
    expect(vp.hint).toBe(VOLUME_PROFILE_HINDSIGHT)
    const ema = buildLegendRow(def('ema'), cfg('ema', 'e'), 0, {}, {}, first)
    expect(ema).not.toHaveProperty('note')
    expect(ema).not.toHaveProperty('hint')
  })

  it('the manager passes the first loaded bar to the anchor note', () => {
    const candles = makeCandles(50)
    const chart = new FakeChart()
    const manager = new IndicatorSeriesManager()
    manager.attach(chart as unknown as IChartApi)
    manager.applyPlan(planSeriesSync(null, candles), candles)
    const before = { ...cfg('avwap', 'a'), inputs: { anchor: candles[0].time - 3600 } }
    const loaded = { ...cfg('avwap', 'b'), inputs: { anchor: candles[10].time } }
    manager.sync([before, loaded], candles)
    const [a, b] = manager.legendRows(undefined)
    expect(a.note).toBe(ANCHOR_NOT_LOADED_NOTE)
    expect(a.values[0].text).toBe('—')
    expect(b.note).toBeUndefined()
    expect(b.values[0].text).not.toBe('—')
  })

  it('values per placement', () => {
    expect(formatIndicatorValue(1234.5, 'volume')).toBe('1.23K')
    expect(formatIndicatorValue(1234.5, 'overlay')).toBe('1234.50')
    expect(formatIndicatorValue(0.00123, 'pane')).toBe('0.00123000')
    expect(formatIndicatorValue(undefined, 'pane')).toBe('—')
  })

  it('legend row colours each value by its line, histogram by its bar', () => {
    const macd = {
      ...cfg('macd', 'm'),
      colors: { hist: '#26a69a', macd: '#111111', signal: '#222222' },
    }
    const row = buildLegendRow(def('macd'), macd, 2, { hist: -1, macd: 0.5 }, { hist: -2 })
    expect(row.title).toBe('MACD 12 26 9 close')
    expect(row.pane).toBe(2)
    expect(row.values).toEqual([
      { key: 'hist', label: 'Histogram', text: '-1.00', color: '#ffcdd2' },
      { key: 'macd', label: 'MACD', text: '0.5000', color: '#111111' },
      { key: 'signal', label: 'Signal', text: '—', color: '#222222' },
    ])
  })
})

describe('guides and price format', () => {
  it('guide values', () => {
    expect(paneGuideValues(def('rsi'))).toEqual([70, 30])
    expect(paneGuideValues(def('stoch'))).toEqual([80, 20])
    expect(paneGuideValues(def('macd'))).toEqual([0])
    expect(paneGuideValues(def('ema'))).toEqual([])
  })
  it('price format', () => {
    expect(indicatorPriceFormat(def('volma'), 100)).toEqual({ type: 'volume' })
    expect(indicatorPriceFormat(def('ema'), 0.001)).toBeUndefined()
    expect(indicatorPriceFormat(def('rsi'), 0.001)).toBeUndefined()
    expect(indicatorPriceFormat(def('macd'), 0.001)).toEqual({
      type: 'price',
      precision: 8,
      minMove: 1e-8,
    })
    expect(indicatorPriceFormat(def('macd'), 50_000)?.type).toBe('price')
  })
})

/* ---------------------------------- manager with a fake chart ---------------------------------- */

interface Call {
  method: string
  arg?: unknown
}

class FakeSeries {
  readonly kind: 'Line' | 'Histogram'
  options: Record<string, unknown>
  calls: Call[] = []
  priceLines: unknown[] = []
  scaleOptions: unknown[] = []
  constructor(kind: 'Line' | 'Histogram', options: Record<string, unknown>) {
    this.kind = kind
    this.options = { ...options }
  }
  setData(data: unknown[]) {
    this.calls.push({ method: 'setData', arg: data })
  }
  update(point: unknown) {
    this.calls.push({ method: 'update', arg: point })
  }
  applyOptions(o: Record<string, unknown>) {
    this.calls.push({ method: 'applyOptions', arg: o })
    Object.assign(this.options, o)
  }
  priceScale() {
    return { applyOptions: (o: unknown) => this.scaleOptions.push(o) }
  }
  createPriceLine(o: unknown) {
    this.priceLines.push(o)
    // An IPriceLine handle: applyOptions merges into the recorded options.
    return { applyOptions: (patch: object) => Object.assign(o as object, patch) }
  }
  count(method: string) {
    return this.calls.filter((c) => c.method === method).length
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

/** Minimal IChartApi. Like LWC with preserveEmptyPane, it never drops empty panes by itself. */
class FakeChart {
  panesList: FakePane[] = [new FakePane()]
  removed: FakeSeries[] = []
  removedPanes: number[] = []
  addSeries(definition: unknown, options: Record<string, unknown>, paneIndex = 0) {
    const s = new FakeSeries(definition === HistogramSeries ? 'Histogram' : 'Line', options)
    const i = Math.min(paneIndex, this.panesList.length)
    if (i === this.panesList.length) this.panesList.push(new FakePane())
    this.panesList[i].series.push(s)
    return s
  }
  removeSeries(s: FakeSeries) {
    this.removed.push(s)
    for (const p of this.panesList) p.series = p.series.filter((x) => x !== s)
  }
  panes() {
    return this.panesList
  }
  removePane(i: number) {
    this.removedPanes.push(i)
    this.panesList.splice(i, 1)
  }
  all() {
    return this.panesList.flatMap((p) => p.series)
  }
}

/** The points a series shows after its setData/update calls (update replaces or appends). */
function shownPoints(s: FakeSeries): unknown[] {
  let shown: { time: number }[] = []
  for (const c of s.calls) {
    if (c.method === 'setData') shown = [...(c.arg as { time: number }[])]
    else if (c.method === 'update') {
      const p = c.arg as { time: number }
      if (shown.length > 0 && shown[shown.length - 1].time === p.time) shown[shown.length - 1] = p
      else shown.push(p)
    }
  }
  return shown
}

function setup(configs: IndicatorConfig[], candles: readonly Candle[]) {
  const chart = new FakeChart()
  const manager = new IndicatorSeriesManager()
  manager.attach(chart as unknown as IChartApi)
  manager.applyPlan(planSeriesSync(null, candles), candles)
  manager.sync(configs, candles)
  return { chart, manager }
}

describe('IndicatorSeriesManager', () => {
  const candles = makeCandles(80)

  it('builds overlays on pane 0 and one pane per pane indicator, in order', () => {
    const ema = cfg('ema', 'e')
    const rsi = cfg('rsi', 'r')
    const macd = cfg('macd', 'm')
    const vol = cfg('volma', 'v')
    const { chart, manager } = setup([ema, rsi, vol, macd], candles)
    expect(chart.panesList).toHaveLength(3)
    // pane 0: 1 EMA + 1 Vol MA
    expect(chart.panesList[0].series).toHaveLength(2)
    expect(chart.panesList[0].series[1].options.priceScaleId).toBe('')
    expect(chart.panesList[0].series[0].options.priceScaleId).toBe('right')
    // pane 1 = RSI with 70/30 guides and a fixed 0..100 scale
    const [rsiSeries] = chart.panesList[1].series
    expect(rsiSeries.priceLines.map((p) => (p as { price: number }).price)).toEqual([70, 30])
    const provider = rsiSeries.options.autoscaleInfoProvider as () => unknown
    expect(provider()).toEqual({ priceRange: { minValue: 0, maxValue: 100 } })
    // pane 2 = MACD: histogram first (drawn below), then lines, zero line on the histogram
    const macdSeries = chart.panesList[2].series
    expect(macdSeries.map((s) => s.kind)).toEqual(['Histogram', 'Line', 'Line'])
    expect(macdSeries[0].priceLines.map((p) => (p as { price: number }).price)).toEqual([0])
    // series options
    expect(rsiSeries.options).toMatchObject({
      lastValueVisible: true,
      priceLineVisible: false,
      lineWidth: 2,
      color: '#7e57c2',
    })
    // pane stretch: price pane dominant
    expect(chart.panesList[0].stretch).toBeGreaterThan(chart.panesList[1].stretch)
    expect(manager.paneIndexOf('m')).toBe(2)
    expect(manager.paneIndexOf('e')).toBe(0)
  })

  it('initial data equals a full computation on the real candles', () => {
    const { chart } = setup([cfg('ema', 'e')], candles)
    const ema = chart.panesList[0].series[0]
    const last = ema.calls.filter((c) => c.method === 'setData').at(-1)?.arg as {
      time: number
      value: number
    }[]
    const expected = computeIndicator(def('ema'), candles, { period: 20 }, 'close').ema
    expect(last.map((p) => p.value)).toEqual(expected.map((p) => p.value))
  })

  it('a live tick uses series.update, never setData', () => {
    const { chart, manager } = setup([cfg('ema', 'e'), cfg('macd', 'm')], candles)
    for (const s of chart.all()) s.calls = []
    const ticked = candles.slice()
    const lastBar = ticked[ticked.length - 1]
    ticked[ticked.length - 1] = { ...lastBar, close: lastBar.close + 3 }
    const plan = planSeriesSync(candles, ticked)
    expect(plan.kind).toBe('update')
    manager.applyPlan(plan, ticked)
    for (const s of chart.all()) {
      expect(s.count('setData')).toBe(0)
      expect(s.count('update')).toBe(1)
    }
    // updated value matches a full recompute
    const ema = chart.panesList[0].series[0]
    const full = computeIndicator(def('ema'), ticked, { period: 20 }, 'close').ema
    expect(ema.calls[0].arg).toEqual({
      time: full.at(-1)?.time,
      value: full.at(-1)?.value,
    })
    // a new bar appends via update too
    const appended = [...ticked, { ...lastBar, time: lastBar.time + 3600 }]
    for (const s of chart.all()) s.calls = []
    manager.applyPlan(planSeriesSync(ticked, appended), appended)
    for (const s of chart.all()) expect(s.count('setData')).toBe(0)
  })

  it('bar replay: shrink then regrow leaves every series equal to a from-scratch computation', () => {
    const configs = [cfg('ema', 'e'), cfg('rsi', 'r'), cfg('macd', 'm')]
    const { chart, manager } = setup(configs, candles)
    let prev: readonly Candle[] = candles
    // Replay starts at bar 50, steps back to 45, then forward bar by bar to 70.
    const cursors = [50, 49, 48, 47, 46, 45, ...Array.from({ length: 25 }, (_, i) => 46 + i)]
    for (const k of cursors) {
      const next = candles.slice(0, k)
      manager.applyPlan(planSeriesSync(prev, next), next)
      prev = next
    }
    // Rebuild what each series shows from its setData/update calls and compare it with the
    // series a fresh chart would show for the same 70 bars.
    const fresh = setup(configs, prev).chart.all()
    const replayed = chart.all()
    expect(replayed).toHaveLength(fresh.length)
    replayed.forEach((s, i) => {
      expect(shownPoints(s)).toEqual(shownPoints(fresh[i]))
      expect(shownPoints(s).length).toBeGreaterThan(0)
    })
  })

  it('bar replay: a step back uses setData, a step forward only update', () => {
    const { chart, manager } = setup([cfg('ema', 'e'), cfg('macd', 'm')], candles)
    const at50 = candles.slice(0, 50)
    manager.applyPlan(planSeriesSync(candles, at50), at50)
    const at49 = candles.slice(0, 49)
    for (const s of chart.all()) s.calls = []
    manager.applyPlan(planSeriesSync(at50, at49), at49)
    for (const s of chart.all()) {
      expect(s.count('setData')).toBe(1)
      expect(s.count('update')).toBe(0)
    }
    for (const s of chart.all()) s.calls = []
    manager.applyPlan(planSeriesSync(at49, at50), at50)
    for (const s of chart.all()) {
      expect(s.count('setData')).toBe(0)
      expect(s.count('update')).toBeGreaterThan(0)
    }
  })

  it('history prepend recomputes with setData', () => {
    const { chart, manager } = setup([cfg('ema', 'e')], candles)
    const older = makeCandles(10, candles[0].time - 10 * 3600)
    const next = [...older, ...candles]
    const plan = planSeriesSync(candles, next)
    expect(plan.kind).toBe('prepend')
    chart.all()[0].calls = []
    manager.applyPlan(plan, next)
    expect(chart.all()[0].count('setData')).toBe(1)
  })

  it('removing the only pane indicator removes its pane', () => {
    const ema = cfg('ema', 'e')
    const rsi = cfg('rsi', 'r')
    const { chart, manager } = setup([ema, rsi], candles)
    expect(chart.panesList).toHaveLength(2)
    manager.sync([ema], candles)
    expect(chart.panesList).toHaveLength(1)
    expect(chart.removedPanes).toEqual([1])
    expect(chart.removed).toHaveLength(1)
  })

  it('removing the first of two pane indicators keeps the other one, now in pane 1', () => {
    const rsi = cfg('rsi', 'r')
    const macd = cfg('macd', 'm')
    const { chart, manager } = setup([rsi, macd], candles)
    manager.sync([macd], candles)
    expect(chart.panesList).toHaveLength(2)
    expect(chart.panesList[1].series.map((s) => s.kind)).toEqual(['Histogram', 'Line', 'Line'])
    expect(manager.paneIndexOf('m')).toBe(1)
    expect(manager.paneIndexOf('r')).toBe(-1)
  })

  it('colour change re-colours without recomputing the lines', () => {
    const ema = cfg('ema', 'e')
    const { chart, manager } = setup([ema], candles)
    const s = chart.all()[0]
    s.calls = []
    manager.sync([{ ...ema, colors: { ema: '#00ff00' } }], candles)
    expect(s.count('setData')).toBe(0)
    // The crosshair dot follows the new colour too.
    expect(s.calls).toEqual([
      {
        method: 'applyOptions',
        arg: { color: '#00ff00', crosshairMarkerBackgroundColor: '#00ff00' },
      },
    ])
  })

  it('adding a pane indicator keeps the size the user gave existing panes', () => {
    const rsi = cfg('rsi', 'r')
    const { chart, manager } = setup([rsi], candles)
    // User drags the separators: price pane smaller, RSI pane taller.
    chart.panesList[0].stretch = 0.7
    chart.panesList[1].stretch = 0.6
    manager.sync([rsi, cfg('stoch', 's')], candles)
    expect(chart.panesList.map((p) => p.stretch)).toEqual([0.7, 0.6, 0.3])
  })

  it('MACD histogram colour change re-colours the bars', () => {
    const macd = cfg('macd', 'm')
    const { chart, manager } = setup([macd], candles)
    const [hist, line] = chart.panesList[1].series
    hist.calls = []
    line.calls = []
    manager.sync([{ ...macd, colors: { ...macd.colors, hist: '#2962ff' } }], candles)
    expect(hist.count('setData')).toBe(1)
    expect(line.count('setData')).toBe(0)
    const data = hist.calls.find((c) => c.method === 'setData')?.arg as { color: string }[]
    expect(data.some((p) => p.color === '#2962ff')).toBe(true)
  })

  it('inputs change recomputes into the same series', () => {
    const ema = cfg('ema', 'e')
    const { chart, manager } = setup([ema], candles)
    const s = chart.all()[0]
    s.calls = []
    manager.sync([{ ...ema, inputs: { period: 5 } }], candles)
    expect(chart.all()).toEqual([s])
    const data = s.calls.find((c) => c.method === 'setData')?.arg as unknown[]
    expect(data).toHaveLength(candles.length - 4)
  })

  it('hide keeps the series and pane, toggles visible', () => {
    const rsi = cfg('rsi', 'r')
    const { chart, manager } = setup([rsi], candles)
    manager.sync([{ ...rsi, visible: false }], candles)
    expect(chart.panesList).toHaveLength(2)
    expect(chart.all()[0].options.visible).toBe(false)
    expect(manager.legendRows(undefined)[0].visible).toBe(false)
  })

  it('legend rows: values at hovered bar or last bar, in config order', () => {
    const ema = cfg('ema', 'e')
    const rsi = cfg('rsi', 'r')
    const { manager } = setup([rsi, ema], candles)
    const rows = manager.legendRows(undefined)
    expect(rows.map((r) => r.uid)).toEqual(['r', 'e'])
    const full = computeIndicator(def('ema'), candles, { period: 20 }, 'close').ema
    expect(rows[1].values[0].text).toBe(full.at(-1)?.value.toFixed(2))
    const hovered = manager.legendRows(candles[40].time)
    const at40 = full.find((p) => p.time === candles[40].time)
    expect(hovered[1].values[0].text).toBe(at40?.value.toFixed(2))
    // warm-up bar has no value
    expect(manager.legendRows(candles[0].time)[1].values[0].text).toBe('—')
  })

  it('notifies subscribers; paneVersion only bumps on pane changes', () => {
    const ema = cfg('ema', 'e')
    const { manager } = setup([ema], candles)
    let n = 0
    manager.subscribe(() => n++)
    const s0 = manager.getSnapshot()
    const ticked = [...candles.slice(0, -1), { ...candles[candles.length - 1], close: 1 }]
    manager.applyPlan(planSeriesSync(candles, ticked), ticked)
    const s1 = manager.getSnapshot()
    expect(n).toBe(1)
    expect(s1.version).toBe(s0.version + 1)
    expect(s1.paneVersion).toBe(s0.paneVersion)
    manager.sync([ema, cfg('rsi', 'r')], ticked)
    expect(manager.getSnapshot().paneVersion).toBe(s1.paneVersion + 1)
  })

  it('detach drops every reference; re-attach rebuilds from scratch (StrictMode)', () => {
    const ema = cfg('ema', 'e')
    const rsi = cfg('rsi', 'r')
    const { manager } = setup([ema, rsi], candles)
    manager.detach()
    expect(manager.legendRows(undefined)).toEqual([])
    expect(manager.paneIndexOf('r')).toBe(-1)
    const chart2 = new FakeChart()
    manager.attach(chart2 as unknown as IChartApi)
    manager.applyPlan(planSeriesSync(null, candles), candles)
    manager.sync([ema, rsi], candles)
    expect(chart2.panesList).toHaveLength(2)
    expect(chart2.all()).toHaveLength(2)
  })

  it('indicators added before any candles fill in on the first set', () => {
    const chart = new FakeChart()
    const manager = new IndicatorSeriesManager()
    manager.attach(chart as unknown as IChartApi)
    manager.sync([cfg('ema', 'e')], null)
    manager.applyPlan(planSeriesSync(null, candles), candles)
    const s = chart.all()[0]
    const data = s.calls.filter((c) => c.method === 'setData').at(-1)?.arg as unknown[]
    expect(data).toHaveLength(candles.length - 19)
  })

  it('setPalette recolours marker borders and guides in place, and new series use it', () => {
    const { chart, manager } = setup([cfg('rsi', 'r'), cfg('ema', 'e')], candles)
    const before = chart.all().length
    manager.setPalette({ background: '#ffffff', mutedText: '#6a6d78' })
    // Nothing is rebuilt.
    expect(chart.all()).toHaveLength(before)
    expect(chart.removed).toHaveLength(0)
    const [rsiSeries] = chart.panesList[1].series
    expect(rsiSeries.options.crosshairMarkerBorderColor).toBe('#ffffff')
    expect(rsiSeries.priceLines).toEqual([
      expect.objectContaining({ price: 70, color: '#6a6d78' }),
      expect.objectContaining({ price: 30, color: '#6a6d78' }),
    ])
    // User line colours stay as chosen.
    expect(rsiSeries.options.color).toBe('#7e57c2')
    // A series added later picks up the palette.
    manager.sync([cfg('rsi', 'r'), cfg('ema', 'e'), cfg('macd', 'm')], candles)
    const macd = chart.panesList[2].series
    expect(macd[0].priceLines[0]).toMatchObject({ price: 0, color: '#6a6d78' })
    expect(macd[1].options.crosshairMarkerBorderColor).toBe('#ffffff')
  })
})

/* ---------------------------------- hover highlight ---------------------------------- */

describe('IndicatorSeriesManager highlight (hit-test hover)', () => {
  const candles = makeCandles(80)

  function widths(series: FakeSeries[]) {
    return series.map((s) => (s.kind === 'Line' ? s.options.lineWidth : s.kind))
  }

  it('maps each indicator series to its uid; other series map to null', () => {
    const { chart, manager } = setup([cfg('ema', 'e'), cfg('macd', 'm')], candles)
    const [ema] = chart.panesList[0].series
    const [hist, macdLine, signal] = chart.panesList[1].series
    expect(manager.uidOfSeries(ema as never)).toBe('e')
    for (const s of [hist, macdLine, signal]) expect(manager.uidOfSeries(s as never)).toBe('m')
    expect(manager.uidOfSeries(new FakeSeries('Line', {}) as never)).toBeNull()
  })

  it('widens the lines of the highlighted indicator by 1 and restores the exact widths', () => {
    const { chart, manager } = setup([cfg('ema', 'e'), cfg('macd', 'm')], candles)
    const macd = chart.panesList[1].series
    const before = widths(macd)
    const ema = chart.panesList[0].series[0]
    const emaWidth = ema.options.lineWidth
    manager.setHighlighted('m')
    expect(manager.highlightedUid()).toBe('m')
    // Histogram bars unchanged; MACD and signal lines +1.
    expect(widths(macd)).toEqual(before.map((w) => (typeof w === 'number' ? w + 1 : w)))
    expect(macd[0].calls.some((c) => c.method === 'applyOptions')).toBe(false)
    expect(ema.options.lineWidth).toBe(emaWidth)
    manager.setHighlighted(null)
    expect(widths(macd)).toEqual(before)
    expect(manager.highlightedUid()).toBeNull()
  })

  it('moving the highlight restores the previous indicator', () => {
    const { chart, manager } = setup([cfg('ema', 'e'), cfg('rsi', 'r')], candles)
    const ema = chart.panesList[0].series[0]
    const rsi = chart.panesList[1].series[0]
    const emaW = ema.options.lineWidth as number
    const rsiW = rsi.options.lineWidth as number
    manager.setHighlighted('e')
    manager.setHighlighted('r')
    expect(ema.options.lineWidth).toBe(emaW)
    expect(rsi.options.lineWidth).toBe(rsiW + 1)
  })

  it('is idempotent: repeating the same uid changes nothing and does not notify', () => {
    const { chart, manager } = setup([cfg('rsi', 'r')], candles)
    const rsi = chart.panesList[1].series[0]
    let n = 0
    manager.subscribe(() => n++)
    manager.setHighlighted('r')
    rsi.calls = []
    manager.setHighlighted('r')
    expect(rsi.calls).toEqual([])
    expect(n).toBe(1)
    manager.setHighlighted(null)
    manager.setHighlighted(null)
    expect(n).toBe(2)
  })

  it('an unknown uid is a no-op', () => {
    const { chart, manager } = setup([cfg('rsi', 'r')], candles)
    manager.setHighlighted('r')
    const rsi = chart.panesList[1].series[0]
    rsi.calls = []
    manager.setHighlighted('nope')
    expect(manager.highlightedUid()).toBe('r')
    expect(rsi.calls).toEqual([])
  })

  it('survives a pane rebuild: re-created series come back highlighted, then restore', () => {
    const rsi = cfg('rsi', 'r')
    const stoch = cfg('stoch', 's')
    const { chart, manager } = setup([rsi, stoch], candles)
    const base = widths(chart.panesList[2].series)
    manager.setHighlighted('s')
    // Removing RSI rebuilds every pane indicator; Stoch is re-created in pane 1.
    manager.sync([stoch], candles)
    const rebuilt = chart.panesList[1].series
    expect(manager.uidOfSeries(rebuilt[0] as never)).toBe('s')
    expect(widths(rebuilt)).toEqual(base.map((w) => (w as number) + 1))
    manager.setHighlighted(null)
    expect(widths(rebuilt)).toEqual(base)
  })

  it('survives colour, input and palette changes and live updates', () => {
    const ema = cfg('ema', 'e')
    const { chart, manager } = setup([ema], candles)
    const s = chart.all()[0]
    const w = s.options.lineWidth as number
    manager.setHighlighted('e')
    manager.sync([{ ...ema, colors: { ema: '#00ff00' } }], candles)
    manager.sync([{ ...ema, colors: { ema: '#00ff00' }, inputs: { period: 5 } }], candles)
    manager.setPalette({ background: '#ffffff', mutedText: '#6a6d78' })
    const ticked = [...candles.slice(0, -1), { ...candles[candles.length - 1], close: 1 }]
    manager.applyPlan(planSeriesSync(candles, ticked), ticked)
    expect(s.options.lineWidth).toBe(w + 1)
    expect(s.options.color).toBe('#00ff00')
    manager.setHighlighted(null)
    expect(s.options.lineWidth).toBe(w)
  })

  it('removing the highlighted indicator clears the highlight; detach clears it too', () => {
    const ema = cfg('ema', 'e')
    const rsi = cfg('rsi', 'r')
    const { manager } = setup([ema, rsi], candles)
    manager.setHighlighted('r')
    manager.sync([ema], candles)
    expect(manager.highlightedUid()).toBeNull()
    manager.setHighlighted('e')
    manager.detach()
    expect(manager.highlightedUid()).toBeNull()
  })
})

/* ---------------------------------- defs without lines ---------------------------------- */

describe('IndicatorSeriesManager with a lineless (range) indicator', () => {
  const candles = makeCandles(80)
  // Like the fixed-range volume profile: no per-bar lines, drawn by a primitive.
  function lineless(placement: IndicatorDef['placement']): IndicatorDef {
    return {
      id: 'vprofile',
      name: 'Fake Range Profile',
      shortName: 'FRP',
      description: 'test def without lines',
      placement,
      inputs: [
        { key: 'rows', label: 'Rows', default: 24, min: 4, max: 200, step: 1, integer: true },
      ],
      source: null,
      lines: [],
      guides: [],
      createStepper() {
        const stepper = { next: () => ({}), clone: () => stepper }
        return stepper
      },
      profile: () => null,
    }
  }
  function defsWith(extra: IndicatorDef): ReadonlyMap<IndicatorId, IndicatorDef> {
    return new Map([...INDICATOR_BY_ID, [extra.id, extra]])
  }
  const fakeCfg: IndicatorConfig = {
    ...cfg('ema', 'p'),
    type: 'vprofile',
    inputs: { rows: 24 },
    colors: {},
  }

  function setupWith(extra: IndicatorDef, configs: IndicatorConfig[]) {
    const chart = new FakeChart()
    const manager = new IndicatorSeriesManager(defsWith(extra))
    manager.attach(chart as unknown as IChartApi)
    manager.applyPlan(planSeriesSync(null, candles), candles)
    manager.sync(configs, candles)
    return { chart, manager }
  }

  it('creates no series and still yields a legend row with a title and no values', () => {
    const { chart, manager } = setupWith(lineless('overlay'), [fakeCfg, cfg('ema', 'e')])
    expect(chart.all()).toHaveLength(1) // the EMA only
    const rows = manager.legendRows(undefined)
    expect(rows.map((r) => r.uid)).toEqual(['p', 'e'])
    expect(rows[0]).toMatchObject({ title: 'FRP 24', pane: 0, visible: true, values: [] })
    expect(manager.legendRows(candles[10].time)[0].values).toEqual([])
  })

  it('later syncs, ticks, hide and highlight never build series for it', () => {
    const { chart, manager } = setupWith(lineless('overlay'), [fakeCfg])
    manager.sync([{ ...fakeCfg, visible: false }], candles)
    manager.sync([{ ...fakeCfg, inputs: { rows: 30 } }], candles)
    const ticked = [...candles.slice(0, -1), { ...candles[candles.length - 1], close: 1 }]
    manager.applyPlan(planSeriesSync(candles, ticked), ticked)
    manager.setHighlighted('p')
    expect(chart.all()).toHaveLength(0)
    expect(manager.highlightedUid()).toBe('p')
    expect(manager.legendRows(undefined)[0].title).toBe('FRP 30')
  })

  it('a lineless pane def takes no pane, so pane indicators keep panes 1..n', () => {
    const def = lineless('pane')
    expect(ownsPane(def)).toBe(false)
    expect(ownsPane(INDICATOR_BY_ID.get('rsi'))).toBe(true)
    const { chart, manager } = setupWith(def, [fakeCfg, cfg('rsi', 'r')])
    expect(chart.panesList).toHaveLength(2)
    expect(manager.paneIndexOf('r')).toBe(1)
    expect(manager.paneIndexOf('p')).toBe(0)
    expect(paneIndicatorUids([fakeCfg, cfg('rsi', 'r')], defsWith(def))).toEqual(['r'])
  })
})

/* ---------------------------------- event (structure) defs ---------------------------------- */

describe('IndicatorSeriesManager with an event (market structure) indicator', () => {
  const candles = makeCandles(80)
  const toggle = (key: string, label: string) => ({
    key,
    label,
    default: 1,
    min: 0,
    max: 1,
    step: 1,
    integer: true,
    kind: 'toggle' as const,
  })
  // Like market structure: numeric inputs, then toggles; no lines, drawn by markers + a primitive.
  function structureDef(placement: IndicatorDef['placement']): IndicatorDef {
    return {
      id: 'mstructure',
      name: 'Fake Market Structure',
      shortName: 'MS',
      description: 'test event def',
      placement,
      inputs: [
        { key: 'left', label: 'Left bars', default: 5, min: 1, max: 50, step: 1, integer: true },
        { key: 'right', label: 'Right bars', default: 5, min: 1, max: 50, step: 1, integer: true },
        toggle('showSwings', 'Swings'),
        toggle('showBos', 'BOS'),
      ],
      source: null,
      lines: [],
      guides: [],
      createStepper() {
        const stepper = { next: () => ({}), clone: () => stepper }
        return stepper
      },
      structure: () => ({ swings: [], events: [], zones: [], state: 'none' }),
    }
  }
  const msCfg: IndicatorConfig = {
    ...cfg('ema', 'm'),
    type: 'mstructure',
    inputs: { left: 5, right: 3, showSwings: 1, showBos: 0 },
    colors: {},
  }
  function setupWith(extra: IndicatorDef, configs: IndicatorConfig[]) {
    const chart = new FakeChart()
    const manager = new IndicatorSeriesManager(new Map([...INDICATOR_BY_ID, [extra.id, extra]]))
    manager.attach(chart as unknown as IChartApi)
    manager.applyPlan(planSeriesSync(null, candles), candles)
    manager.sync(configs, candles)
    return { chart, manager }
  }

  it('the legend row carries the visual-only hint; other indicators do not', () => {
    const row = buildLegendRow(structureDef('overlay'), msCfg, 0, {})
    expect(row.hint).toBe(MARKET_STRUCTURE_HINT)
    expect(row.values).toEqual([])
    expect(row).not.toHaveProperty('note')
    expect(MARKET_STRUCTURE_HINT).toMatch(/no edge claim/)
    expect(buildLegendRow(def('ema'), cfg('ema', 'e'), 0, {})).not.toHaveProperty('hint')
    expect(buildLegendRow(def('vprofile'), cfg('vprofile', 'v'), 0, {}).hint).toBe(
      VOLUME_PROFILE_HINDSIGHT,
    )
  })

  it('the title lists the numeric inputs but not the on/off toggles', () => {
    expect(indicatorTitle(structureDef('overlay'), msCfg)).toBe('MS 5 3')
  })

  it('creates no series or pane, on sync, ticks, toggle edits and hide', () => {
    const { chart, manager } = setupWith(structureDef('overlay'), [msCfg, cfg('rsi', 'r')])
    expect(chart.all()).toHaveLength(1) // the RSI only
    expect(chart.panesList).toHaveLength(2) // main + RSI
    expect(manager.paneIndexOf('m')).toBe(0)
    expect(manager.paneIndexOf('r')).toBe(1)
    manager.sync([{ ...msCfg, inputs: { ...msCfg.inputs, showBos: 1 } }], candles)
    manager.sync([{ ...msCfg, visible: false }], candles)
    const ticked = [...candles.slice(0, -1), { ...candles[candles.length - 1], close: 1 }]
    manager.applyPlan(planSeriesSync(candles, ticked), ticked)
    expect(chart.all()).toHaveLength(0)
    const rows = manager.legendRows(undefined)
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({ uid: 'm', pane: 0, values: [], hint: MARKET_STRUCTURE_HINT })
  })

  it('a pane-placed event def would not take a pane either', () => {
    expect(ownsPane(structureDef('pane'))).toBe(false)
  })
})

describe('session indicators (session ranges)', () => {
  const candles = makeCandles(60)
  const srCfg: IndicatorConfig = {
    ...cfg('sessions', 's'),
    inputs: { ...cfg('sessions', 's').inputs, showAsia: 0 },
  }

  it('the legend row carries the visual-only hint and no values', () => {
    const row = buildLegendRow(def('sessions'), srCfg, 0, {})
    expect(row.hint).toBe(SESSION_RANGES_HINT)
    expect(row.values).toEqual([])
    expect(row).not.toHaveProperty('note')
    expect(SESSION_RANGES_HINT).toMatch(/no edge claim/)
    expect(SESSION_RANGES_HINT).toMatch(/closed bars/)
    // A finished box on history is the whole session's range: hindsight, stated in the hint.
    expect(SESSION_RANGES_HINT).toMatch(
      /whole session's range, known only once its last bar closed/,
    )
  })

  it('the legend row notes toggled-on sessions hidden because their hours miss the bar length', () => {
    const on = cfg('sessions', 'n')
    const H = 3_600
    // 1h and sub-hour: everything lines up, no note.
    for (const l of [60, 900, H]) {
      expect(buildLegendRow(def('sessions'), on, 0, {}, {}, undefined, l)).not.toHaveProperty(
        'note',
      )
    }
    // 4h (defaults): London 07, New York 13 and the 1h opening range are hidden.
    expect(buildLegendRow(def('sessions'), on, 0, {}, {}, undefined, 4 * H).note).toBe(
      "London, New York and opening range hidden: hours don't line up with the bar length",
    )
    // Toggled-off sessions are not listed (Asia is off in srCfg but aligned anyway).
    const someOff = { ...on, inputs: { ...on.inputs, showLondon: 0, showOpening: 0 } }
    expect(sessionAlignmentNote(def('sessions'), someOff, 4 * H)).toBe(
      "New York hidden: hours don't line up with the bar length",
    )
    expect(sessionAlignmentNote(def('sessions'), on, 12 * H)).toBe(
      "Asia, London, New York and opening range hidden: hours don't line up with the bar length",
    )
    // OR N = 4 lines up with 4h bars.
    const or4 = { ...on, inputs: { ...on.inputs, orHours: 4 } }
    expect(sessionAlignmentNote(def('sessions'), or4, 4 * H)).toBe(
      "London and New York hidden: hours don't line up with the bar length",
    )
    // 1d and above (covered by the hint), unknown interval, other indicators: no note.
    expect(sessionAlignmentNote(def('sessions'), on, 86_400)).toBeUndefined()
    expect(sessionAlignmentNote(def('sessions'), on, undefined)).toBeUndefined()
    expect(sessionAlignmentNote(def('ema'), cfg('ema', 'e'), 4 * H)).toBeUndefined()
    expect(buildLegendRow(def('sessions'), on, 0, {})).not.toHaveProperty('note')
  })

  it('the title lists the session hours and N but not the show toggles', () => {
    expect(indicatorTitle(def('sessions'), srCfg)).toBe('SR 0 8 7 16 13 22 1')
  })

  it('creates no series and takes no pane, on sync, toggle edits, hide and ticks', () => {
    expect(ownsPane(def('sessions'))).toBe(false)
    const chart = new FakeChart()
    const manager = new IndicatorSeriesManager()
    manager.attach(chart as unknown as IChartApi)
    manager.applyPlan(planSeriesSync(null, candles), candles)
    manager.sync([srCfg], candles)
    manager.sync([{ ...srCfg, inputs: { ...srCfg.inputs, showLondon: 0 } }], candles)
    manager.sync([{ ...srCfg, visible: false }], candles)
    const ticked = [...candles.slice(0, -1), { ...candles[candles.length - 1], close: 1 }]
    manager.applyPlan(planSeriesSync(candles, ticked), ticked)
    expect(chart.all()).toHaveLength(0)
    expect(chart.panesList).toHaveLength(1)
    expect(manager.legendRows(undefined)[0]).toMatchObject({
      uid: 's',
      pane: 0,
      values: [],
      hint: SESSION_RANGES_HINT,
    })
    // The manager passes the bar length through to the alignment note.
    expect(manager.legendRows(undefined, 3_600)[0]).not.toHaveProperty('note')
    expect(manager.legendRows(undefined, 4 * 3_600)[0].note).toBe(
      "London, New York and opening range hidden: hours don't line up with the bar length",
    )
  })
})
