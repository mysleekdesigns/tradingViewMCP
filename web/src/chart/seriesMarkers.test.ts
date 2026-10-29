import type { UTCTimestamp } from 'lightweight-charts'
import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { BacktestOverlay, type BacktestChartData } from './backtestOverlay'
import { SeriesMarkersMux, type ChartMarker, type MarkersPrimitive } from './seriesMarkers'

interface FakeSeries {
  name: string
}

interface FakePrimitive extends MarkersPrimitive {
  series: FakeSeries
  current: ChartMarker[]
  sets: number
  detached: boolean
}

/** A fake `createSeriesMarkers`: records every primitive it creates. */
function fakeFactory() {
  const created: FakePrimitive[] = []
  const factory = (series: FakeSeries, markers: ChartMarker[]): FakePrimitive => {
    const p: FakePrimitive = {
      series,
      current: markers,
      sets: 0,
      detached: false,
      setMarkers(m) {
        if (this.detached) throw new Error('setMarkers on a detached primitive')
        this.current = m
        this.sets += 1
      },
      detach() {
        if (this.detached) throw new Error('detached twice')
        this.detached = true
      },
    }
    created.push(p)
    return p
  }
  /** Primitives not detached yet. */
  const live = () => created.filter((p) => !p.detached)
  return { factory, created, live }
}

function m(time: number, text: string): ChartMarker {
  return {
    time: time as UTCTimestamp,
    position: 'aboveBar',
    shape: 'circle',
    color: '#000000',
    text,
  }
}

const texts = (list: readonly ChartMarker[]) => list.map((x) => `${x.time}:${x.text}`)

describe('SeriesMarkersMux', () => {
  it('creates no primitive while every channel is empty or no series is attached', () => {
    const f = fakeFactory()
    const mux = new SeriesMarkersMux(f.factory, ['backtest', 'structure'])
    mux.set('structure', [m(1, 'a')])
    expect(f.created).toHaveLength(0)
    mux.clear('structure')
    mux.attach({ name: 's1' })
    expect(f.created).toHaveLength(0)
    expect(mux.hasPrimitive()).toBe(false)
  })

  it('creates the primitive lazily with the merged markers', () => {
    const f = fakeFactory()
    const mux = new SeriesMarkersMux(f.factory)
    const s1 = { name: 's1' }
    mux.attach(s1)
    mux.channel('structure').setMarkers([m(5, 'x')])
    expect(f.created).toHaveLength(1)
    expect(f.created[0].series).toBe(s1)
    expect(texts(f.created[0].current)).toEqual(['5:x'])
  })

  it('merges channels sorted by time; equal times keep channel order, then list order', () => {
    const f = fakeFactory()
    const mux = new SeriesMarkersMux(f.factory, ['backtest', 'structure'])
    mux.attach({ name: 's1' })
    // Set structure first: the constructor order still puts backtest first on ties.
    mux.set('structure', [m(2, 's2a'), m(2, 's2b'), m(9, 's9')])
    mux.set('backtest', [m(1, 'b1'), m(2, 'b2'), m(7, 'b7')])
    expect(texts(mux.markers())).toEqual(['1:b1', '2:b2', '2:s2a', '2:s2b', '7:b7', '9:s9'])
    expect(texts(f.live()[0].current)).toEqual(texts(mux.markers()))
    // An unsorted channel is sorted too (stably).
    mux.set('other', [m(8, 'o8'), m(2, 'o2')])
    expect(texts(mux.markers())).toEqual([
      '1:b1',
      '2:b2',
      '2:s2a',
      '2:s2b',
      '2:o2',
      '7:b7',
      '8:o8',
      '9:s9',
    ])
  })

  it('a handle detach clears only its channel; the primitive is emptied, not re-created', () => {
    const f = fakeFactory()
    const mux = new SeriesMarkersMux(f.factory, ['backtest', 'structure'])
    mux.attach({ name: 's1' })
    const backtest = mux.channel('backtest')
    const structure = mux.channel('structure')
    backtest.setMarkers([m(1, 'b')])
    structure.setMarkers([m(2, 's')])
    backtest.detach()
    expect(texts(f.live()[0].current)).toEqual(['2:s'])
    expect(f.created).toHaveLength(1)
    structure.detach()
    // Every channel empty: the same primitive stays on the series, with no markers.
    expect(f.live()).toHaveLength(1)
    expect(f.live()[0].current).toEqual([])
    expect(mux.hasPrimitive()).toBe(true)
    structure.setMarkers([m(3, 's3')])
    expect(f.created).toHaveLength(1)
    expect(texts(f.live()[0].current)).toEqual(['3:s3'])
  })

  it('toggling empty / non-empty many times creates the primitive once and never detaches it', () => {
    const f = fakeFactory()
    const mux = new SeriesMarkersMux(f.factory, ['backtest', 'structure'])
    mux.attach({ name: 's1' })
    const structure = mux.channel('structure')
    for (let i = 0; i < 25; i++) {
      structure.setMarkers([m(i, `s${i}`)])
      expect(texts(f.live()[0].current)).toEqual([`${i}:s${i}`])
      structure.detach()
      expect(f.live()[0].current).toEqual([])
    }
    expect(f.created).toHaveLength(1)
    expect(f.created[0].detached).toBe(false)
    // Only a series removal or switch detaches it.
    mux.detach(true)
    expect(f.created[0].detached).toBe(true)
  })

  it('channel() starts the channel empty, like a fresh createSeriesMarkers', () => {
    const f = fakeFactory()
    const mux = new SeriesMarkersMux(f.factory)
    mux.attach({ name: 's1' })
    mux.channel('backtest').setMarkers([m(1, 'old')])
    mux.channel('structure').setMarkers([m(2, 's')])
    mux.channel('backtest')
    expect(texts(mux.markers())).toEqual(['2:s'])
  })

  it('setting the same array again does not touch the primitive', () => {
    const f = fakeFactory()
    const mux = new SeriesMarkersMux(f.factory)
    mux.attach({ name: 's1' })
    const list = [m(1, 'a')]
    mux.set('structure', list)
    const sets = f.created[0].sets
    mux.set('structure', list)
    mux.clear('backtest')
    expect(f.created[0].sets).toBe(sets)
    expect(f.created).toHaveLength(1)
  })

  it('attach to a new series (alive) moves the markers there', () => {
    const f = fakeFactory()
    const mux = new SeriesMarkersMux(f.factory)
    const s1 = { name: 's1' }
    const s2 = { name: 's2' }
    mux.attach(s1)
    mux.set('structure', [m(1, 'a')])
    mux.detach(true)
    expect(f.created[0].detached).toBe(true)
    mux.attach(s2)
    expect(f.live()).toHaveLength(1)
    expect(f.live()[0].series).toBe(s2)
    expect(texts(f.live()[0].current)).toEqual(['1:a'])
    // attach without a detach also moves them.
    const s3 = { name: 's3' }
    mux.attach(s3)
    expect(f.live().map((p) => p.series)).toEqual([s3])
  })

  it('detach(false) never touches the dead primitive; changes while detached apply on attach', () => {
    const f = fakeFactory()
    const mux = new SeriesMarkersMux(f.factory)
    mux.attach({ name: 's1' })
    mux.set('structure', [m(1, 'a')])
    const dead = f.created[0]
    mux.detach(false)
    mux.set('structure', [m(4, 'b')])
    mux.set('backtest', [m(3, 'c')])
    expect(dead.detached).toBe(false)
    expect(dead.sets).toBe(0)
    mux.attach({ name: 's2' })
    expect(f.created).toHaveLength(2)
    expect(texts(f.created[1].current)).toEqual(['3:c', '4:b'])
  })

  it('hands the primitive a copy (later merges never mutate what LWC holds)', () => {
    const f = fakeFactory()
    const mux = new SeriesMarkersMux(f.factory)
    mux.attach({ name: 's1' })
    mux.set('a', [m(1, 'a')])
    const first = f.created[0].current
    mux.set('b', [m(2, 'b')])
    expect(texts(first)).toEqual(['1:a'])
    expect(f.created[0].current).not.toBe(mux.markers())
  })
})

/* ---------------------------------------------------------------------------------------- */
/* BacktestOverlay through the mux, with the exact call order Chart.tsx uses                  */
/* ---------------------------------------------------------------------------------------- */

const T0 = 1_700_000_000
const STEP = 3600

function bars(n: number): Candle[] {
  return Array.from({ length: n }, (_, i) => ({
    time: T0 + i * STEP,
    open: 1,
    high: 2,
    low: 0.5,
    close: 1.5,
    volume: 1,
  }))
}

function report(key: string, times: number[]): BacktestChartData {
  return { key, markers: times.map((t) => m(t, `L${t}`)), equity: [] }
}

/** Chart.tsx's main-series effect: setup, and the cleanup with `alive`. */
function chartWiring() {
  const f = fakeFactory()
  const mux = new SeriesMarkersMux<FakeSeries>(f.factory, ['backtest', 'structure'])
  const overlay = new BacktestOverlay<FakeSeries>(() => mux.channel('backtest'))
  const structure = mux.channel('structure')
  const attach = (series: FakeSeries) => {
    mux.attach(series)
    overlay.attachSeries(series)
  }
  const detach = (alive: boolean) => {
    overlay.detachSeries(alive)
    mux.detach(alive)
    mux.clear('backtest')
  }
  return { f, mux, overlay, structure, attach, detach }
}

describe('BacktestOverlay through SeriesMarkersMux', () => {
  it('shows trade markers together with structure markers', () => {
    const w = chartWiring()
    w.attach({ name: 's1' })
    w.overlay.setBars(bars(10))
    w.overlay.setData(report('r1', [T0 + 2 * STEP, T0 + 5 * STEP]))
    w.structure.setMarkers([m(T0 + 2 * STEP, 'BOS'), m(T0 + 8 * STEP, 'ChoCh')])
    expect(w.f.live()).toHaveLength(1)
    expect(texts(w.f.live()[0].current)).toEqual([
      `${T0 + 2 * STEP}:L${T0 + 2 * STEP}`,
      `${T0 + 2 * STEP}:BOS`,
      `${T0 + 5 * STEP}:L${T0 + 5 * STEP}`,
      `${T0 + 8 * STEP}:ChoCh`,
    ])
    expect(w.overlay.markerCount()).toBe(2)
  })

  it('clearing the backtest keeps the structure markers (and vice versa)', () => {
    const w = chartWiring()
    w.attach({ name: 's1' })
    w.overlay.setBars(bars(10))
    w.overlay.setData(report('r1', [T0 + STEP]))
    w.structure.setMarkers([m(T0 + 3 * STEP, 'BOS')])
    w.overlay.setData(null)
    expect(texts(w.f.live()[0].current)).toEqual([`${T0 + 3 * STEP}:BOS`])
    w.overlay.setData(report('r2', [T0 + 4 * STEP]))
    w.structure.detach()
    expect(texts(w.f.live()[0].current)).toEqual([`${T0 + 4 * STEP}:L${T0 + 4 * STEP}`])
    w.overlay.setData(null)
    // Both empty: the one primitive stays, emptied.
    expect(w.f.live()).toHaveLength(1)
    expect(w.f.live()[0].current).toEqual([])
    expect(w.f.created).toHaveLength(1)
  })

  it('chart-type switch (alive): both channels move to the new series, the old primitive goes', () => {
    const w = chartWiring()
    const s1 = { name: 's1' }
    const s2 = { name: 's2' }
    w.attach(s1)
    w.overlay.setBars(bars(10))
    w.overlay.setData(report('r1', [T0 + STEP]))
    w.structure.setMarkers([m(T0 + 3 * STEP, 'BOS')])
    w.detach(true)
    expect(w.f.live()).toHaveLength(0)
    w.attach(s2)
    const live = w.f.live()
    expect(live).toHaveLength(1)
    expect(live[0].series).toBe(s2)
    expect(texts(live[0].current)).toEqual([`${T0 + STEP}:L${T0 + STEP}`, `${T0 + 3 * STEP}:BOS`])
  })

  it('chart removed (alive false): nothing is called on the dead series; no stale trades after', () => {
    const w = chartWiring()
    const s1 = { name: 's1' }
    w.attach(s1)
    w.overlay.setBars(bars(10))
    w.overlay.setData(report('r1', [T0 + STEP]))
    w.structure.setMarkers([m(T0 + 3 * STEP, 'BOS')])
    const dead = w.f.created[w.f.created.length - 1]
    const deadSets = dead.sets
    w.detach(false)
    // StrictMode-style remount: the backtest effect's cleanup clears the report while detached.
    w.overlay.setData(null)
    expect(dead.detached).toBe(false)
    expect(dead.sets).toBe(deadSets)
    w.attach({ name: 's2' })
    const live = w.f.created.filter((p) => p !== dead && !p.detached)
    expect(live).toHaveLength(1)
    expect(texts(live[0].current)).toEqual([`${T0 + 3 * STEP}:BOS`])
    // The report comes back: its markers join the structure markers again.
    w.overlay.setData(report('r1', [T0 + STEP]))
    expect(texts(live[0].current)).toEqual([`${T0 + STEP}:L${T0 + STEP}`, `${T0 + 3 * STEP}:BOS`])
  })

  it('chart removed with the report kept: the remount re-sets the same trade markers once', () => {
    const w = chartWiring()
    w.attach({ name: 's1' })
    w.overlay.setBars(bars(10))
    w.overlay.setData(report('r1', [T0 + STEP, T0 + 2 * STEP]))
    w.detach(false)
    w.attach({ name: 's2' })
    const live = w.f.live().filter((p) => p.series.name === 's2')
    expect(live).toHaveLength(1)
    expect(texts(live[0].current)).toEqual([
      `${T0 + STEP}:L${T0 + STEP}`,
      `${T0 + 2 * STEP}:L${T0 + 2 * STEP}`,
    ])
  })
})
