// Phase 19 (C5): the backtest overlay takes the chart's shared BarTimes (one times array and one
// Set per bar set) instead of building its own; markers and equity are the same either way.

import type { LineData, SeriesMarker, UTCTimestamp } from 'lightweight-charts'
import { describe, expect, it } from 'vitest'
import { BarTimes, BarTimesMemo } from '../data/candleStore'
import type { Candle } from '../types'
import {
  BacktestOverlay,
  type BacktestChartData,
  type MarkersHandle,
  type OverlayChart,
  type OverlayLineSeries,
} from './backtestOverlay'

class Line implements OverlayLineSeries {
  data: LineData<UTCTimestamp>[] = []
  setData(d: LineData<UTCTimestamp>[]) {
    this.data = d
  }
  getPane() {
    return { paneIndex: () => 1, getStretchFactor: () => 1, setStretchFactor: () => {} }
  }
}

class Chart implements OverlayChart<Line> {
  lines: Line[] = []
  addLine() {
    const l = new Line()
    this.lines.push(l)
    return l
  }
  removeSeries(l: Line) {
    this.lines = this.lines.filter((x) => x !== l)
  }
  paneCount() {
    return 1 + this.lines.length
  }
}

class Markers implements MarkersHandle {
  markers: SeriesMarker<UTCTimestamp>[] = []
  setMarkers(m: SeriesMarker<UTCTimestamp>[]) {
    this.markers = m
  }
  detach() {}
}

const bars = (times: number[]): Candle[] =>
  times.map((time) => ({ time, open: 1, high: 1, low: 1, close: 1, volume: 1 }))

const marker = (time: number): SeriesMarker<UTCTimestamp> => ({
  time: time as UTCTimestamp,
  position: 'belowBar',
  shape: 'arrowUp',
  color: '#000',
})

const DATA: BacktestChartData = {
  key: 'k',
  markers: [marker(100), marker(250), marker(400), marker(900)],
  equity: [
    {
      id: 'strategy',
      title: 'Strategy',
      color: '#00f',
      dashed: false,
      width: 2,
      points: [100, 200, 300, 400, 500].map((time, i) => ({ time, value: 1 + i / 10 })),
    },
    {
      id: 'buy_and_hold',
      title: 'Buy & hold',
      color: '#888',
      dashed: false,
      width: 1,
      points: [100, 300, 500].map((time, i) => ({ time, value: 1 - i / 10 })),
    },
  ],
}

function setup() {
  const markers: Markers[] = []
  const overlay = new BacktestOverlay<object>(() => {
    const m = new Markers()
    markers.push(m)
    return m
  })
  const chart = new Chart()
  overlay.attachChart(chart)
  overlay.attachSeries({})
  overlay.setData(DATA)
  return { overlay, chart, markers }
}

const snapshot = (s: ReturnType<typeof setup>) => ({
  markers: s.markers.at(-1)?.markers ?? [],
  lines: s.chart.lines.map((l) => l.data),
  count: s.overlay.markerCount(),
})

describe('BacktestOverlay with shared BarTimes', () => {
  it('draws the same markers and equity as with its own times, through a bar-set history', () => {
    const history = [
      [],
      [100, 200, 300],
      [100, 200, 300], // a live tick: same key
      [100, 200, 300, 400],
      [0, 100, 200, 300, 400], // prepend
      [100, 300, 500], // new window
      [],
    ]
    const own = setup()
    const shared = setup()
    const memo = new BarTimesMemo()
    for (const times of history) {
      const b = bars(times)
      own.overlay.setBars(b)
      shared.overlay.setBars(b, memo.get(b))
      expect(snapshot(shared)).toEqual(snapshot(own))
    }
  })

  it('reads the shared Set and times instead of building its own', () => {
    const { overlay } = setup()
    const b = bars([100, 200, 300, 400])
    const times = new BarTimes(b)
    const set = times.set
    const arr = times.times
    overlay.setBars(b, times)
    // Same object, read by the overlay: nothing new was built for it.
    expect(times.set).toBe(set)
    expect(times.times).toBe(arr)
    // Same key again (a forming-bar tick): ignored.
    overlay.setBars(bars([100, 200, 300, 400]), new BarTimes(bars([100, 200, 300, 400])))
    expect(overlay.markerCount()).toBe(3)
  })

  it('without backtest data, a bar set builds no Set or times array at all', () => {
    const overlay = new BacktestOverlay<object>(() => new Markers())
    overlay.attachChart(new Chart())
    overlay.attachSeries({})
    let reads = 0
    const b = bars([100, 200, 300])
    const counted = new Proxy(b, {
      get(target, prop, receiver) {
        if (typeof prop === 'string' && /^\d+$/.test(prop)) reads++
        return Reflect.get(target, prop, receiver) as unknown
      },
    })
    overlay.setBars(counted, new BarTimes(counted))
    expect(reads).toBeLessThanOrEqual(2) // the key's first and last bar only
  })
})
