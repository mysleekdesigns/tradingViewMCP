import type {
  LineData,
  LineSeriesPartialOptions,
  SeriesMarker,
  UTCTimestamp,
} from 'lightweight-charts'
import { describe, expect, it } from 'vitest'
import type { BacktestReport, LabMetrics, LabTrade } from '../data/labTypes'
import type { Candle } from '../types'
import {
  BacktestOverlay,
  EQUITY_PANE_STRETCH,
  buildChartData,
  buildTradeMarkers,
  equityValueAt,
  filterToBars,
  formatEquityAxis,
  planFocus,
  sameEquity,
  signedPct,
  tradesOnChart,
  type BacktestChartData,
  type MarkersHandle,
  type OverlayChart,
  type OverlayLineSeries,
} from './backtestOverlay'

const trade = (over: Partial<LabTrade> = {}): LabTrade => ({
  entry_time: 100,
  entry_price: 10,
  exit_time: 300,
  exit_price: 11,
  pnl: 1,
  return_pct: 0.1,
  exit_reason: 'signal',
  ...over,
})

describe('signedPct / formatEquityAxis', () => {
  it('formats signed percentages with a real minus sign', () => {
    expect(signedPct(0.0423)).toBe('+4.2%')
    expect(signedPct(-0.02)).toBe('−2.0%')
    expect(signedPct(0)).toBe('0.0%')
    expect(signedPct(-0.00001)).toBe('0.0%')
  })

  it('labels the equity axis as change since the window start', () => {
    expect(formatEquityAxis(1.234)).toBe('+23.4%')
    expect(formatEquityAxis(0.9)).toBe('−10.0%')
    expect(formatEquityAxis(Number.NaN)).toBe('')
  })
})

describe('planFocus', () => {
  it('applies a range inside the loaded bars unchanged', () => {
    expect(planFocus(200, 400, 100, 1000)).toEqual({ kind: 'apply', from: 200, to: 400 })
  })

  it('clamps a range that overlaps the loaded bars to them', () => {
    expect(planFocus(50, 400, 100, 1000)).toEqual({ kind: 'apply', from: 100, to: 400 })
    expect(planFocus(900, 1500, 100, 1000)).toEqual({ kind: 'apply', from: 900, to: 1000 })
  })

  it('skips a range that ends before the first loaded bar', () => {
    expect(planFocus(10, 99, 100, 1000)).toEqual({ kind: 'before-data' })
  })

  it('skips a range that starts after the last loaded bar', () => {
    expect(planFocus(1001, 2000, 100, 1000)).toEqual({ kind: 'after-data' })
  })

  it('skips when no bars are loaded', () => {
    expect(planFocus(10, 20, null, null)).toEqual({ kind: 'no-data' })
  })
})

describe('buildTradeMarkers', () => {
  it('puts an arrowUp below the entry bar and an arrowDown above the exit bar', () => {
    const [entry, exit] = buildTradeMarkers([trade()])
    expect(entry).toMatchObject({ time: 100, position: 'belowBar', shape: 'arrowUp', text: 'Long' })
    expect(exit).toMatchObject({ time: 300, position: 'aboveBar', shape: 'arrowDown' })
    expect(exit.text).toBe('Exit +10.0%')
  })

  it('labels stop-loss and take-profit exits and colours them by result', () => {
    const m = buildTradeMarkers([
      trade({ exit_reason: 'stop_loss', pnl: -1, return_pct: -0.03 }),
      trade({ entry_time: 400, exit_time: 500, exit_reason: 'take_profit' }),
    ])
    expect(m[1].text).toBe('SL −3.0%')
    expect(m[3].text).toBe('TP +10.0%')
    expect(m[1].color).not.toBe(m[3].color)
  })

  it('has no exit marker for a trade still open at the window end', () => {
    expect(buildTradeMarkers([trade({ exit_time: null, exit_price: null })])).toHaveLength(1)
  })

  it('sorts by time with an exit before an entry on the same bar', () => {
    const m = buildTradeMarkers([
      trade({ entry_time: 300, exit_time: 400 }),
      trade({ entry_time: 100, exit_time: 300 }),
    ])
    expect(m.map((x) => [x.time, x.shape])).toEqual([
      [100, 'arrowUp'],
      [300, 'arrowDown'],
      [300, 'arrowUp'],
      [400, 'arrowDown'],
    ])
  })
})

describe('equity helpers', () => {
  const pts = [
    { time: 10, value: 1 },
    { time: 20, value: 1.1 },
    { time: 30, value: 1.2 },
  ]

  it('keeps only points on loaded bars', () => {
    expect(filterToBars(pts, new Set([20, 30, 40]))).toEqual(pts.slice(1))
  })

  it('reads the value at or before a time, or the last value', () => {
    expect(equityValueAt(pts, 25)).toBe(1.1)
    expect(equityValueAt(pts, 30)).toBe(1.2)
    expect(equityValueAt(pts, 5)).toBeNull()
    expect(equityValueAt(pts)).toBe(1.2)
    expect(equityValueAt([], 10)).toBeNull()
  })

  it('counts trades whose entry bar is loaded', () => {
    const ts = [trade({ entry_time: 50 }), trade({ entry_time: 150 })]
    expect(tradesOnChart(ts, 100)).toBe(1)
    expect(tradesOnChart(ts, null)).toBe(0)
  })
})

const ZERO: LabMetrics = {
  total_return: 0,
  cagr: 0,
  volatility: 0,
  sharpe: 0,
  sortino: 0,
  max_drawdown: 0,
  win_rate: 0,
  profit_factor: 0,
  n_trades: 0,
  exposure: 0,
  turnover: 0,
  bars: 0,
}

function report(volTarget = true): BacktestReport {
  const eq = [
    { time: 100, value: 1 },
    { time: 200, value: 1.05 },
    { time: 300, value: 1.1 },
  ]
  return {
    trial_id: 3,
    run_id: 'ui',
    plan_hash: 'h',
    plan: {},
    symbol: 'BTCUSDT',
    interval: '4h',
    window: { kind: 'design', start: 100, end: 300 },
    costs: { fee_rate: 0.001, slippage_rate: 0.0005, slippage_vol_mult: 0.5 },
    metrics: { net: ZERO, gross: ZERO },
    benchmarks: {
      buy_and_hold: ZERO,
      vol_target: ZERO,
      random_entry: { n_paths: 0, sharpe_median: null, sharpe_p95: null, percentile: null },
    },
    stats: {
      dsr: { dsr: null, psr: null, n_trials: 1, sr0_annual: null },
      pbo: { pbo: null, n_trials: 1, n_splits: 0 },
      sharpe_ci: { low: null, high: null, level: 0.95 },
    },
    walk_forward: [],
    trades: [trade({ entry_time: 100, exit_time: 300 })],
    equity: eq,
    benchmark_equity: { buy_and_hold: eq, vol_target: volTarget ? eq : [] },
    warnings: [],
    disclaimer: 'x',
    elapsed_ms: 1,
  }
}

describe('buildChartData', () => {
  it('draws strategy and buy-and-hold, and vol-target only when the report has it', () => {
    expect(buildChartData(report()).equity.map((l) => l.id)).toEqual([
      'strategy',
      'buy_and_hold',
      'vol_target',
    ])
    expect(buildChartData(report(false)).equity.map((l) => l.id)).toEqual([
      'strategy',
      'buy_and_hold',
    ])
  })

  it('keys the data by run, trial and plan hash', () => {
    expect(buildChartData(report()).key).toBe('ui#3#h')
  })
})

// ---- Overlay manager with a fake chart ----

class FakePane {
  stretch = 1
  readonly chart: FakeChart
  constructor(chart: FakeChart) {
    this.chart = chart
  }
  paneIndex() {
    return this.chart.panes.indexOf(this)
  }
  getStretchFactor() {
    return this.stretch
  }
  setStretchFactor(f: number) {
    this.stretch = f
  }
}

class FakeLine implements OverlayLineSeries {
  data: LineData<UTCTimestamp>[] = []
  readonly pane: FakePane
  readonly options: LineSeriesPartialOptions
  constructor(pane: FakePane, options: LineSeriesPartialOptions) {
    this.pane = pane
    this.options = options
  }
  setData(d: LineData<UTCTimestamp>[]) {
    this.data = d
  }
  getPane() {
    return this.pane
  }
}

/** Panes 0..n; a pane without series is dropped, as LWC does. Pane 0 holds the price series. */
class FakeChart implements OverlayChart<FakeLine> {
  panes: FakePane[] = [new FakePane(this)]
  lines: FakeLine[] = []
  /** Other (indicator) series per pane, so a pane is not empty. */
  others = new Map<FakePane, number>([[this.panes[0], 1]])
  addLine(options: LineSeriesPartialOptions, paneIndex: number) {
    while (this.panes.length <= paneIndex) this.panes.push(new FakePane(this))
    const line = new FakeLine(this.panes[paneIndex], options)
    this.lines.push(line)
    return line
  }
  removeSeries(line: FakeLine) {
    this.lines = this.lines.filter((l) => l !== line)
    this.dropEmpty(line.pane)
  }
  paneCount() {
    return this.panes.length
  }
  addIndicatorPane() {
    const pane = new FakePane(this)
    this.panes.push(pane)
    this.others.set(pane, 1)
    return pane
  }
  private dropEmpty(pane: FakePane) {
    const used = this.lines.some((l) => l.pane === pane) || (this.others.get(pane) ?? 0) > 0
    if (!used && this.panes.length > 1) this.panes = this.panes.filter((p) => p !== pane)
  }
}

class FakeMarkers implements MarkersHandle {
  markers: SeriesMarker<UTCTimestamp>[] = []
  detached = false
  setMarkers(m: SeriesMarker<UTCTimestamp>[]) {
    this.markers = m
  }
  detach() {
    this.detached = true
  }
}

const candles = (times: number[]): Candle[] =>
  times.map((time) => ({ time, open: 1, high: 1, low: 1, close: 1, volume: 1 }))

function setup() {
  const created: FakeMarkers[] = []
  const overlay = new BacktestOverlay<{ name: string }>(() => {
    const m = new FakeMarkers()
    created.push(m)
    return m
  })
  const chart = new FakeChart()
  overlay.attachChart(chart)
  overlay.attachSeries({ name: 'main' })
  return { overlay, chart, created }
}

describe('BacktestOverlay', () => {
  it('adds markers and an equity pane below the price pane once bars are loaded', () => {
    const { overlay, chart, created } = setup()
    const data: BacktestChartData = buildChartData(report())
    overlay.setData(data)
    expect(chart.lines).toHaveLength(0) // no bars yet
    overlay.setBars(candles([100, 200, 300, 400]))
    expect(chart.lines).toHaveLength(3)
    expect(chart.panes).toHaveLength(2)
    expect(overlay.equityPaneIndex()).toBe(1)
    expect(chart.panes[1].stretch).toBe(EQUITY_PANE_STRETCH)
    expect(created).toHaveLength(1)
    expect(created[0].markers.map((m) => m.time)).toEqual([100, 300])
    expect(overlay.markerCount()).toBe(2)
  })

  it('draws only the bars that are loaded on the chart', () => {
    const { overlay, chart, created } = setup()
    overlay.setBars(candles([200, 300]))
    overlay.setData(buildChartData(report()))
    expect(chart.lines[0].data.map((d) => d.time)).toEqual([200, 300])
    expect(created[0].markers.map((m) => m.time)).toEqual([300])
    // Older history loads: the curve and markers widen.
    overlay.setBars(candles([100, 200, 300]))
    expect(chart.lines[0].data.map((d) => d.time)).toEqual([100, 200, 300])
    expect(created[0].markers.map((m) => m.time)).toEqual([100, 300])
  })

  it('shows no equity pane and no markers when the report is outside the loaded bars', () => {
    const { overlay, chart, created } = setup()
    overlay.setBars(candles([1000, 2000]))
    overlay.setData(buildChartData(report()))
    expect(chart.lines).toHaveLength(0)
    expect(chart.panes).toHaveLength(1)
    expect(created).toHaveLength(0)
    expect(overlay.equityPaneIndex()).toBeNull()
  })

  it('clears markers and removes the pane on setData(null)', () => {
    const { overlay, chart, created } = setup()
    overlay.setBars(candles([100, 200, 300]))
    overlay.setData(buildChartData(report()))
    overlay.setData(null)
    expect(chart.lines).toHaveLength(0)
    expect(chart.panes).toHaveLength(1)
    expect(created[0].detached).toBe(true)
    expect(overlay.markerCount()).toBe(0)
  })

  it('moves the equity pane below a newly added indicator pane and keeps its height', () => {
    const { overlay, chart } = setup()
    overlay.setBars(candles([100, 200, 300]))
    overlay.setData(buildChartData(report()))
    chart.panes[1].setStretchFactor(0.6) // user dragged the separator
    overlay.suspendPane()
    expect(chart.panes).toHaveLength(1)
    chart.addIndicatorPane() // indicator manager adds pane 1
    overlay.resumePane()
    expect(overlay.equityPaneIndex()).toBe(2)
    expect(chart.panes[2].stretch).toBe(0.6)
    expect(chart.lines.every((l) => l.pane === chart.panes[2])).toBe(true)
  })

  it('re-attaches markers to a new main series after a chart-type switch', () => {
    const { overlay, created } = setup()
    overlay.setBars(candles([100, 200, 300]))
    overlay.setData(buildChartData(report()))
    overlay.detachSeries(true)
    expect(created[0].detached).toBe(true)
    overlay.attachSeries({ name: 'line' })
    expect(created).toHaveLength(2)
    expect(created[1].markers).toHaveLength(2)
  })

  it('does not touch a removed chart on unmount', () => {
    const { overlay, chart, created } = setup()
    overlay.setBars(candles([100, 200, 300]))
    overlay.setData(buildChartData(report()))
    overlay.detachChart(false)
    overlay.detachSeries(false)
    expect(chart.lines).toHaveLength(3) // left to chart.remove()
    expect(created[0].detached).toBe(false)
    expect(overlay.equityPaneIndex()).toBeNull()
  })

  it('reports legend values at a time and notifies subscribers on pane changes', () => {
    const { overlay } = setup()
    let calls = 0
    const off = overlay.subscribe(() => calls++)
    overlay.setBars(candles([100, 200, 300]))
    overlay.setData(buildChartData(report(false)))
    expect(calls).toBeGreaterThan(0)
    expect(overlay.legend(200).map((l) => l.text)).toEqual(['+5.0%', '+5.0%'])
    expect(overlay.legend().map((l) => l.text)).toEqual(['+10.0%', '+10.0%'])
    off()
  })

  it('ignores live ticks that do not change the bar set', () => {
    const { overlay, chart } = setup()
    overlay.setBars(candles([100, 200, 300]))
    overlay.setData(buildChartData(report()))
    const before = chart.lines[0].data
    overlay.setBars(candles([100, 200, 300]))
    expect(chart.lines[0].data).toBe(before)
  })
})

describe('BacktestOverlay on plotted bars (bars without trades left out)', () => {
  it('moves a marker whose bar had no trades to the next plotted bar instead of dropping it', () => {
    const { overlay, created } = setup()
    overlay.setData(buildChartData(report()))
    // The exit bar (300) had no trades, so it is not plotted.
    overlay.setBars(candles([100, 200, 400]))
    expect(created[0].markers.map((m) => m.time)).toEqual([100, 400])
    expect(overlay.markerCount()).toBe(2)
  })
})

describe('BacktestOverlay: recolouring trade markers', () => {
  it('re-sets only the markers when the colours change, keeping the equity series', () => {
    const { overlay, chart, created } = setup()
    const r = report()
    overlay.setBars(candles([100, 200, 300, 400]))
    overlay.setData(buildChartData(r, { win: '#089981', loss: '#f23645' }))
    const lines = [...chart.lines]
    const stretch = chart.panes[1].stretch
    let removed = 0
    let added = 0
    const removeSeries = chart.removeSeries.bind(chart)
    const addLine = chart.addLine.bind(chart)
    chart.removeSeries = (l) => {
      removed++
      removeSeries(l)
    }
    chart.addLine = (o, p) => {
      added++
      return addLine(o, p)
    }

    // A colour-picker drag: many rebuilds of the same report with new colours.
    for (const win of ['#111111', '#222222', '#2962ff']) {
      overlay.setData(buildChartData(r, { win, loss: '#ff9800' }))
    }

    expect(removed).toBe(0)
    expect(added).toBe(0)
    expect(chart.lines).toEqual(lines)
    expect(chart.lines.every((l, i) => l === lines[i])).toBe(true)
    expect(chart.panes[1].stretch).toBe(stretch)
    expect(created).toHaveLength(1)
    const exit = created[0].markers.find((m) => m.shape === 'arrowDown')
    expect(exit?.color).toBe('#2962ff') // the trade won (pnl 1)
    expect(overlay.markerCount()).toBe(2)
  })

  it('still replaces the equity series for a different report', () => {
    const { overlay, chart } = setup()
    overlay.setBars(candles([100, 200, 300]))
    overlay.setData(buildChartData(report()))
    const first = chart.lines[0]
    overlay.setData(buildChartData({ ...report(), run_id: 'other' }))
    expect(chart.lines).toHaveLength(3)
    expect(chart.lines[0]).not.toBe(first)
  })

  it('compares equity by report key and point arrays', () => {
    const r = report()
    const a = buildChartData(r)
    expect(sameEquity(a, buildChartData(r, { win: '#000000', loss: '#ffffff' }))).toBe(true)
    expect(sameEquity(a, buildChartData({ ...r, equity: [...r.equity] }))).toBe(false)
    expect(sameEquity(a, buildChartData(report(false)))).toBe(false)
  })
})
