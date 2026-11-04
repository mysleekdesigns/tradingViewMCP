import { HistogramSeries, type IChartApi } from 'lightweight-charts'
import { describe, expect, it } from 'vitest'
import type { RegimePoint, RegimeState } from '../data/labTypes'
import type { Candle } from '../types'
import {
  MAX_REGIME_BARS,
  REGIME_ALPHA,
  REGIME_LABELS,
  REGIME_PROB_FLOOR,
  REGIME_RIBBON_PRICE_SCALE_ID,
  REGIME_SIDEWAYS_ALPHA,
  RegimeRibbon,
  closedBarsForRegime,
  regimeColorsFor,
  regimeProbStrength,
  regimeQueryKey,
  regimeRequestBars,
  regimeRibbonData,
  regimeRibbonScaleMargins,
  regimeRun,
  regimeRunLabel,
  regimeRunThrough,
  regimeSourceBars,
  type RegimeColors,
} from './regimeRibbon'
import { paletteFor, withHexAlpha } from './theme'

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

describe('HMM ribbon opacity', () => {
  const palette = paletteFor({
    theme: 'dark',
    upColor: '#00ff00',
    downColor: '#ff0000',
    timezone: 'UTC',
  })
  const colors = regimeColorsFor(palette)
  const point = (state: RegimeState, prob: number | null): RegimePoint => ({
    time: 100,
    state,
    prob,
  })
  const colorAt = (state: RegimeState, prob: number, n = 2): string | undefined => {
    const [p] = regimeRibbonData([point(state, prob)], colors, undefined, n)
    return 'color' in p ? p.color : undefined
  }

  it('is full at certainty and at the floor at chance, for two and three states', () => {
    expect(regimeProbStrength(1, 2)).toBe(1)
    expect(regimeProbStrength(0.5, 2)).toBe(REGIME_PROB_FLOOR)
    expect(regimeProbStrength(1, 3)).toBe(1)
    expect(regimeProbStrength(1 / 3, 3)).toBe(REGIME_PROB_FLOOR)
  })

  it('never drops below the floor, also below chance, and is linear in between', () => {
    expect(regimeProbStrength(0.1, 2)).toBe(REGIME_PROB_FLOOR)
    expect(regimeProbStrength(0, 3)).toBe(REGIME_PROB_FLOOR)
    expect(regimeProbStrength(0.75, 2)).toBeCloseTo(0.5, 12)
    expect(regimeProbStrength(2 / 3, 3)).toBeCloseTo(0.5, 12)
  })

  it('a certain bar matches the Markov strength', () => {
    expect(colorAt(1, 1)).toBe(colors.bull)
    expect(colorAt(-1, 1)).toBe(colors.bear)
    expect(colorAt(0, 1, 3)).toBe(colors.sideways)
  })

  it('a coin-flip bar is faint but visible', () => {
    expect(colorAt(1, 0.5)).toBe(withHexAlpha('#00ff00', REGIME_ALPHA * REGIME_PROB_FLOOR))
    expect(colorAt(-1, 0.5)).toBe(withHexAlpha('#ff0000', REGIME_ALPHA * REGIME_PROB_FLOOR))
    expect(colorAt(0, 1 / 3, 3)).toBe(
      withHexAlpha(palette.mutedText, REGIME_SIDEWAYS_ALPHA * REGIME_PROB_FLOOR),
    )
    expect(colorAt(1, 0.5)).toBe('#00ff0023')
  })

  it('a null or missing prob is whitespace on the HMM source', () => {
    const data = regimeRibbonData(
      [point(1, null), { time: 200, state: 1 }, { time: 300, state: null, prob: null }],
      colors,
      undefined,
      2,
    )
    expect(data).toEqual([{ time: 100 }, { time: 200 }, { time: 300 }])
  })

  it('names the two HMM states Bear and Bull', () => {
    expect(REGIME_LABELS[-1]).toBe('Bear')
    expect(REGIME_LABELS[1]).toBe('Bull')
  })

  it('leaves Markov colours as they were, whatever a point carries', () => {
    const markov = regimeRibbonData([point(1, 0.51), point(-1, null)], colors)
    expect(markov).toEqual([
      { time: 100, value: 1, color: '#00ff008c' },
      { time: 100, value: 1, color: '#ff00008c' },
    ])
    expect(regimeRibbonData([{ time: 1, state: 0 }], colors, undefined, null)).toEqual([
      { time: 1, value: 1, color: '#787b8659' },
    ])
  })

  it('perturbation: changing later points leaves earlier ribbon data identical', () => {
    const states: RegimeState[] = [-1, 0, 1]
    const base: RegimePoint[] = Array.from({ length: 40 }, (_, i) => ({
      time: 100 * (i + 1),
      state: i < 5 ? null : states[i % 3],
      prob: i < 5 ? null : 0.4 + (i % 7) / 10,
    }))
    const cut = 25
    const changed: RegimePoint[] = base.map((p, i) =>
      i <= cut ? p : { ...p, state: states[(i + 1) % 3], prob: 0.99 },
    )
    const withExtra = [...changed, { time: 100_000, state: 1, prob: 1 } as RegimePoint]
    const a = regimeRibbonData(base, colors, undefined, 3)
    const b = regimeRibbonData(changed, colors, undefined, 3)
    const c = regimeRibbonData(withExtra, colors, undefined, 3)
    expect(b.slice(0, cut + 1)).toEqual(a.slice(0, cut + 1))
    expect(c.slice(0, cut + 1)).toEqual(a.slice(0, cut + 1))
    expect(b.slice(cut + 1)).not.toEqual(a.slice(cut + 1))
    expect(regimeRun(base.slice(0, cut + 1))).toEqual(regimeRun(changed.slice(0, cut + 1)))
  })

  it('bars in regime works on HMM points', () => {
    const run = regimeRun([
      { time: 1, state: null, prob: null },
      { time: 2, state: -1, prob: 0.7 },
      { time: 3, state: 1, prob: 0.6 },
      { time: 4, state: 1, prob: 0.9 },
    ])
    expect(run).toEqual({ state: 1, bars: 2 })
    expect(run && regimeRunLabel(run)).toBe('Bull · 2 bars')
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

  it('includes the model, so one source never answers for the other', () => {
    const markov = regimeQueryKey({ ...base, candles })
    expect(regimeQueryKey({ ...base, candles, model: 'markov' })).toBe(markov)
    expect(regimeQueryKey({ ...base, candles, model: 'hmm' })).not.toBe(markov)
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

describe('regimeRun', () => {
  /** Points at times 100, 200, ... with the given states. */
  const series = (states: readonly (RegimeState | null)[]): RegimePoint[] =>
    states.map((state, i) => ({ time: 100 * (i + 1), state }))

  it('gives the newest state and the length of the run ending on it', () => {
    expect(regimeRun(series([1, 1, 0, -1, -1, -1]))).toEqual({ state: -1, bars: 3 })
    expect(regimeRun(series([0, 1]))).toEqual({ state: 1, bars: 1 })
    expect(regimeRun(series([1, 1, 1, 1]))).toEqual({ state: 1, bars: 4 })
    expect(regimeRun(series([1, -1, 0]))).toEqual({ state: 0, bars: 1 })
  })

  it('is null with no labelled bar', () => {
    expect(regimeRun([])).toBeNull()
    expect(regimeRun(series([null, null]))).toBeNull()
  })

  it('stops at a null: warm-up bars and gaps are never counted across', () => {
    expect(regimeRun(series([null, null, 1, 1]))).toEqual({ state: 1, bars: 2 })
    expect(regimeRun(series([1, 1, 1, null, 1]))).toEqual({ state: 1, bars: 1 })
  })

  it('ends on the newest labelled bar and does not count a trailing null', () => {
    expect(regimeRun(series([0, 1, 1, null]))).toEqual({ state: 1, bars: 2 })
  })

  // Perturbation test (.claude/rules/no-lookahead): what happens after bar t cannot reach t.
  it('never back-dates: points after bar t leave the run at t identical', () => {
    // A fixed pseudo-random label path (seeded LCG), warm-up and gaps included.
    let seed = 20_261_104
    const next = () => {
      seed = (seed * 1_103_515_245 + 12_345) % 2_147_483_648
      return seed / 2_147_483_648
    }
    const pick = (): RegimeState | null => {
      const u = next()
      return u < 0.1 ? null : u < 0.4 ? -1 : u < 0.7 ? 0 : 1
    }
    const points = series([null, null, ...Array.from({ length: 200 }, pick)])
    for (let t = 0; t < points.length - 1; t++) {
      const before = regimeRun(points.slice(0, t + 1))
      const changed = points.map((p, i): RegimePoint => {
        if (i <= t) return p
        return { time: p.time, state: p.state === 1 ? -1 : 1 }
      })
      const appended = [...points, { time: 1e9, state: 1 as const }]
      expect(regimeRun(changed.slice(0, t + 1))).toEqual(before)
      expect(regimeRun(appended.slice(0, t + 1))).toEqual(before)
    }
  })
})

describe('regimeRunThrough', () => {
  const candles = makeCandles(4, 100, 100) // times 100, 200, 300, 400
  const points: RegimePoint[] = [
    { time: 100, state: 1 },
    { time: 200, state: 1 },
    { time: 300, state: 1 },
    { time: 400, state: -1 },
  ]

  it('reads the newest closed bar, not the forming one', () => {
    expect(regimeRunThrough(points, candles, true)).toEqual({ state: 1, bars: 3 })
    expect(regimeRunThrough(points, candles, false)).toEqual({ state: -1, bars: 1 })
  })

  it('ignores labels past a replay cursor while the previous answer is still held', () => {
    const replay = regimeSourceBars(candles, candles.slice(0, 2))
    expect(regimeRunThrough(points, replay.candles, replay.formingLast)).toEqual({
      state: 1,
      bars: 2,
    })
  })

  it('is null until a bar has closed', () => {
    expect(regimeRunThrough(points, candles.slice(0, 1), true)).toBeNull()
    expect(regimeRunThrough(points, [], false)).toBeNull()
  })
})

describe('regimeRunLabel', () => {
  it('names the state and the bars', () => {
    expect(regimeRunLabel({ state: 1, bars: 14 })).toBe('Bull · 14 bars')
    expect(regimeRunLabel({ state: 0, bars: 2 })).toBe('Sideways · 2 bars')
    expect(regimeRunLabel({ state: -1, bars: 1 })).toBe('Bear · 1 bar')
  })
})
