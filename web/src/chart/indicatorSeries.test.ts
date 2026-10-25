import { HistogramSeries, type IChartApi } from 'lightweight-charts'
import { describe, expect, it } from 'vitest'
import { computeIndicator } from '../indicators/compute'
import { INDICATOR_BY_ID } from '../indicators/registry'
import type { IndicatorDef, IndicatorId } from '../indicators/types'
import { createIndicatorConfig, type IndicatorConfig } from '../state/indicators'
import type { Candle } from '../types'
import {
  assignPanes,
  buildLegendRow,
  diffIndicators,
  formatIndicatorValue,
  formatInputNumber,
  histogramColor,
  indicatorPriceFormat,
  indicatorTitle,
  IndicatorSeriesManager,
  paneGuideValues,
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

function setup(configs: IndicatorConfig[], candles: Candle[]) {
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
