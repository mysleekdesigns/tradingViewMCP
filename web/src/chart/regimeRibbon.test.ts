import { HistogramSeries, type IChartApi } from 'lightweight-charts'
import { describe, expect, it } from 'vitest'
import type { RegimePoint } from '../data/labTypes'
import type { Candle } from '../types'
import {
  MAX_REGIME_BARS,
  REGIME_RIBBON_PRICE_SCALE_ID,
  RegimeRibbon,
  closedBarsForRegime,
  regimeColorsFor,
  regimeQueryKey,
  regimeRequestBars,
  regimeRibbonData,
  regimeRibbonScaleMargins,
  regimeSourceBars,
  type RegimeColors,
} from './regimeRibbon'
import { paletteFor } from './theme'

const COLORS: RegimeColors = { bull: 'G', bear: 'R', sideways: 'S' }

function makeCandles(n: number, start = 1_700_000_000, step = 3600): Candle[] {
  return Array.from({ length: n }, (_, i) => ({
    time: start + i * step,
    open: 100 + i,
    high: 101 + i,
    low: 99 + i,
    close: 100.5 + i,
    volume: 10,
  }))
}

describe('regimeColorsFor', () => {
  it('uses the candle colours for Bull/Bear and the muted grey for Sideways, translucent', () => {
    const dark = regimeColorsFor(
      paletteFor({ theme: 'dark', upColor: '#00ff00', downColor: '#ff0000', timezone: 'UTC' }),
    )
    expect(dark).toEqual({ bull: '#00ff008c', bear: '#ff00008c', sideways: '#787b8659' })
  })
})

describe('regimeRibbonData', () => {
  const points: RegimePoint[] = [
    { time: 100, state: null },
    { time: 200, state: 1 },
    { time: 300, state: 0 },
    { time: 400, state: -1 },
  ]

  it('draws a constant-height bar coloured by state and whitespace for warm-up bars', () => {
    expect(regimeRibbonData(points, COLORS)).toEqual([
      { time: 100 },
      { time: 200, value: 1, color: 'G' },
      { time: 300, value: 1, color: 'S' },
      { time: 400, value: 1, color: 'R' },
    ])
  })

  it('drops points on bars the chart does not plot', () => {
    expect(regimeRibbonData(points, COLORS, new Set([100, 300, 500]))).toEqual([
      { time: 100 },
      { time: 300, value: 1, color: 'S' },
    ])
  })
})

describe('closedBarsForRegime', () => {
  const candles = makeCandles(5)

  it('drops the forming last bar', () => {
    expect(closedBarsForRegime(candles, true)).toEqual(candles.slice(0, 4))
  })

  it('keeps every bar when the last one has closed', () => {
    expect(closedBarsForRegime(candles, false)).toBe(candles)
  })

  it('returns nothing for a single forming bar', () => {
    expect(closedBarsForRegime(candles.slice(0, 1), true)).toEqual([])
  })
})

describe('regimeRequestBars', () => {
  it('sends the closed bars only, as times and closes', () => {
    const candles = makeCandles(3)
    expect(regimeRequestBars(candles)).toEqual({
      times: [candles[0].time, candles[1].time],
      closes: [100.5, 101.5],
    })
    expect(regimeRequestBars(candles, false).times).toHaveLength(3)
  })

  it('caps the request at the newest MAX_REGIME_BARS closed bars', () => {
    const candles = makeCandles(MAX_REGIME_BARS + 10)
    const req = regimeRequestBars(candles)
    expect(req.times).toHaveLength(MAX_REGIME_BARS)
    expect(req.times.at(-1)).toBe(candles.at(-2)?.time)
    expect(req.times[0]).toBe(candles[9].time)
  })
})

describe('regimeSourceBars', () => {
  // Bar 2 has no trades (volume 0, carries the previous close): the chart does not plot it, but
  // the lab's regime_state feature keeps the row, so the request must too.
  const raw = makeCandles(5).map((c, i) => (i === 2 ? { ...c, volume: 0 } : c))
  const plotted = raw.filter((c) => c.volume > 0)

  it('live: sends every loaded closed bar, no-trade bars included', () => {
    const src = regimeSourceBars(raw, null)
    expect(src).toEqual({ candles: raw, formingLast: true })
    const req = regimeRequestBars(src.candles, src.formingLast)
    // 5 loaded bars, the forming last one dropped: 4 rows, the zero-volume bar among them.
    expect(req.times).toEqual(raw.slice(0, 4).map((c) => c.time))
  })

  it('replay: sends the loaded bars up to the cursor, all closed', () => {
    // Replay shows plotted bars up to raw[3]; the no-trade raw[2] is still sent.
    const visible = plotted.filter((c) => c.time <= raw[3].time)
    const src = regimeSourceBars(raw, visible)
    expect(src).toEqual({ candles: raw.slice(0, 4), formingLast: false })
    expect(regimeRequestBars(src.candles, src.formingLast).times).toHaveLength(4)
    expect(regimeSourceBars(raw, [])).toEqual({ candles: [], formingLast: false })
  })

  it('draws the labels on plotted bars only', () => {
    const points: RegimePoint[] = raw.slice(0, 4).map((c) => ({ time: c.time, state: 1 }))
    const shown = regimeRibbonData(points, COLORS, new Set(plotted.map((c) => c.time)))
    expect(shown.map((p) => p.time)).toEqual([raw[0].time, raw[1].time, raw[3].time])
  })
})

describe('regimeQueryKey', () => {
  const base = { symbol: 'BTCUSDT', timeframe: '4h', windowBars: 20, threshold: 0.05 }
  const candles = makeCandles(50)

  it('ignores live ticks on the forming bar', () => {
    const ticked = [...candles.slice(0, -1), { ...candles[49], close: 999, high: 999 }]
    expect(regimeQueryKey({ ...base, candles: ticked })).toBe(regimeQueryKey({ ...base, candles }))
  })

  it('changes when a new bar opens (the previous one closed)', () => {
    const next = [...candles, makeCandles(1, candles[49].time + 3600)[0]]
    expect(regimeQueryKey({ ...base, candles: next })).not.toBe(
      regimeQueryKey({ ...base, candles }),
    )
  })

  it('changes when older history is prepended', () => {
    const older = [...makeCandles(10, candles[0].time - 36_000), ...candles]
    expect(regimeQueryKey({ ...base, candles: older })).not.toBe(
      regimeQueryKey({ ...base, candles }),
    )
  })

  it('changes when the last closed bar is corrected', () => {
    const fixed = [...candles]
    fixed[48] = { ...fixed[48], close: 1 }
    expect(regimeQueryKey({ ...base, candles: fixed })).not.toBe(
      regimeQueryKey({ ...base, candles }),
    )
  })

  it('changes with symbol, timeframe, and parameters', () => {
    const key = regimeQueryKey({ ...base, candles })
    expect(regimeQueryKey({ ...base, symbol: 'ETHUSDT', candles })).not.toBe(key)
    expect(regimeQueryKey({ ...base, timeframe: '1h', candles })).not.toBe(key)
    expect(regimeQueryKey({ ...base, windowBars: 30, candles })).not.toBe(key)
    expect(regimeQueryKey({ ...base, threshold: 0.1, candles })).not.toBe(key)
  })

  it('counts the last bar when it is not forming', () => {
    expect(regimeQueryKey({ ...base, candles, formingLast: false })).not.toBe(
      regimeQueryKey({ ...base, candles }),
    )
  })

  it('is null when no bar has closed', () => {
    expect(regimeQueryKey({ ...base, candles: candles.slice(0, 1) })).toBeNull()
    expect(regimeQueryKey({ ...base, candles: [] })).toBeNull()
  })
})

/* ---------------------------------- ribbon with a fake chart ---------------------------------- */

class FakeSeries {
  readonly options: Record<string, unknown>
  data: unknown[] = []
  setDataCalls = 0
  scaleOptions: unknown[] = []
  constructor(options: Record<string, unknown>) {
    this.options = options
  }
  setData(data: unknown[]) {
    this.data = data
    this.setDataCalls++
  }
  priceScale() {
    return { applyOptions: (o: unknown) => this.scaleOptions.push(o) }
  }
}

class FakeChart {
  added: { definition: unknown; pane: number | undefined; series: FakeSeries }[] = []
  removed: FakeSeries[] = []
  addSeries(definition: unknown, options: Record<string, unknown>, pane?: number) {
    const series = new FakeSeries(options)
    this.added.push({ definition, pane, series })
    return series
  }
  removeSeries(s: FakeSeries) {
    this.removed.push(s)
  }
}

function setup() {
  const chart = new FakeChart()
  const ribbon = new RegimeRibbon(chart as unknown as IChartApi, COLORS)
  return { chart, ribbon, series: chart.added[0].series }
}

describe('RegimeRibbon', () => {
  it('adds one histogram on pane 0, on its own scale pinned to the bottom, with no labels', () => {
    const { chart, series } = setup()
    expect(chart.added).toHaveLength(1)
    expect(chart.added[0].definition).toBe(HistogramSeries)
    expect(chart.added[0].pane).toBe(0)
    expect(series.options).toMatchObject({
      priceScaleId: REGIME_RIBBON_PRICE_SCALE_ID,
      lastValueVisible: false,
      priceLineVisible: false,
    })
    expect(series.options.priceScaleId).not.toBe('right')
    expect(series.options.priceScaleId).not.toBe('')
    expect(series.scaleOptions).toEqual([{ scaleMargins: regimeRibbonScaleMargins }])
    expect(regimeRibbonScaleMargins).toEqual({ top: 0.97, bottom: 0 })
    const autoscale = series.options.autoscaleInfoProvider as () => unknown
    expect(autoscale()).toEqual({ priceRange: { minValue: 0, maxValue: 1 } })
  })

  it('draws points, re-colours them, and clears on null', () => {
    const { ribbon, series } = setup()
    ribbon.setPoints([
      { time: 1, state: 1 },
      { time: 2, state: null },
    ])
    expect(series.data).toEqual([{ time: 1, value: 1, color: 'G' }, { time: 2 }])
    ribbon.setColors({ bull: 'g2', bear: 'r2', sideways: 's2' })
    expect(series.data).toEqual([{ time: 1, value: 1, color: 'g2' }, { time: 2 }])
    ribbon.setPoints(null)
    expect(series.data).toEqual([])
  })

  it('does not redraw when the colours are unchanged', () => {
    const { ribbon, series } = setup()
    ribbon.setPoints([{ time: 1, state: 0 }])
    ribbon.setColors({ ...COLORS })
    expect(series.setDataCalls).toBe(1)
  })

  it('keeps the plotted-bar filter across a colour change', () => {
    const { ribbon, series } = setup()
    ribbon.setPoints(
      [
        { time: 1, state: 1 },
        { time: 2, state: -1 },
      ],
      new Set([2]),
    )
    ribbon.setColors({ bull: 'x', bear: 'y', sideways: 'z' })
    expect(series.data).toEqual([{ time: 2, value: 1, color: 'y' }])
  })

  it('removes its series once; later calls are no-ops', () => {
    const { chart, ribbon, series } = setup()
    ribbon.remove()
    ribbon.remove()
    ribbon.setPoints([{ time: 1, state: 1 }])
    expect(chart.removed).toEqual([series])
    expect(series.setDataCalls).toBe(0)
  })
})
