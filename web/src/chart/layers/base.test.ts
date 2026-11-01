// Phase 19 wave 5: the shared layer plumbing (primitive base, culled geometry cache, layer host,
// coordinate adapter, bitmap helpers) that the market-structure, session-ranges and volume
// profile layers now share.

import type {
  IChartApiBase,
  ISeriesApi,
  ISeriesPrimitive,
  ITimeScaleApi,
  SeriesType,
  Time,
} from 'lightweight-charts'
import { describe, expect, it } from 'vitest'
import { canvasRecording } from '../layerFrame.testkit'
import {
  ChartCoords,
  CulledLayerPrimitive,
  LayerHost,
  LayerPrimitive,
  LayerRenderer,
  clampX,
  hLine,
  isTimestampRange,
  type AttachedParam,
  type BitmapScope,
  type PrimitiveHost,
  type RenderTarget,
} from './base'

/* ---------- fakes ---------- */

interface FakeView {
  /** x of logical index 0 and the bar spacing; bars at times 0, 60, 120, ... */
  x0: number
  spacing: number
  width: number
  /** y = yScale * price + yOffset */
  yScale: number
  yOffset: number
  range: { from: number; to: number } | null
  calls: Map<string, number>
}

function fakeView(): FakeView {
  return {
    x0: 10,
    spacing: 8,
    width: 500,
    yScale: -2,
    yOffset: 300,
    range: { from: 0, to: 600 },
    calls: new Map(),
  }
}

function count(v: FakeView, name: string): void {
  v.calls.set(name, (v.calls.get(name) ?? 0) + 1)
}

function fakeTimeScale(v: FakeView): ITimeScaleApi<Time> {
  return {
    logicalToCoordinate: (i: number) => {
      count(v, 'logicalToCoordinate')
      return v.x0 + i * v.spacing
    },
    timeToIndex: (t: number, nearest?: boolean) => {
      count(v, 'timeToIndex')
      const i = t / 60
      if (Number.isInteger(i)) return i
      return nearest ? Math.round(i) : null
    },
    timeToCoordinate: (t: number) => {
      count(v, 'timeToCoordinate')
      const i = t / 60
      return Number.isInteger(i) ? v.x0 + i * v.spacing : null
    },
    getVisibleRange: () => (v.range === null ? null : { ...v.range }),
    width: () => {
      count(v, 'width')
      return v.width
    },
  } as unknown as ITimeScaleApi<Time>
}

function fakeSeries(v: FakeView): ISeriesApi<SeriesType, Time> {
  return {
    priceToCoordinate: (p: number) => v.yScale * p + v.yOffset,
  } as unknown as ISeriesApi<SeriesType, Time>
}

function attach(p: ISeriesPrimitive<Time>, v: FakeView): { redraws: () => number } {
  let redraws = 0
  const ts = fakeTimeScale(v)
  const chart = { timeScale: () => ts } as unknown as IChartApiBase<Time>
  p.attached?.({
    chart,
    series: fakeSeries(v),
    requestUpdate: () => redraws++,
  } as unknown as AttachedParam as never)
  return { redraws: () => redraws }
}

/** A culled primitive whose geometry is the x of each time in view and the y of a price. */
class TestCulled extends CulledLayerPrimitive<
  { times: number[]; price: number },
  { xs: (number | null)[]; y: number | null; spacing: number; width: number },
  { color: string }
> {
  constructor(isEmpty?: (g: { xs: (number | null)[] }) => boolean) {
    super('t', {
      colors: { color: '#2962ff' },
      paint: (scope, geom, colors) => {
        scope.context.fillStyle = colors.color
        for (const x of geom.xs) if (x !== null) scope.context.fillRect(x, geom.y ?? 0, 1, 1)
      },
      isEmpty,
      zOrder: 'bottom',
      snap: 'exact',
    })
  }

  protected build(view: { times: number[]; price: number }, coords: ChartCoords) {
    const r = coords.visibleRange()
    return {
      xs: view.times
        .filter((t) => r !== null && t >= r.from && t <= r.to)
        .map((t) => coords.xOfTime(t)),
      y: coords.yOfPrice(view.price),
      spacing: coords.barSpacing,
      width: coords.width,
    }
  }
}

/* ---------- helpers ---------- */

describe('bitmap helpers', () => {
  it('clampX keeps x within two pixels of the pane', () => {
    expect(clampX(-1e9, 400)).toBe(-2)
    expect(clampX(1e9, 400)).toBe(402)
    expect(clampX(123.5, 400)).toBe(123.5)
  })

  it('hLine draws a crisp line: odd widths on the half pixel, ends rounded', () => {
    const rec = canvasRecording()
    const scope = {
      context: rec.ctx,
      horizontalPixelRatio: 2,
      verticalPixelRatio: 1.5,
    } as unknown as BitmapScope
    hLine(scope, 1.3, 10.6, 10.2, 1)
    hLine(scope, 1.3, 10.6, 10.2, 2)
    const strokes = rec.ops.filter((o) => o.op === 'stroke')
    expect(strokes.map((o) => (o.op === 'stroke' ? o.path : null))).toEqual([
      [
        ['M', 3, 15.5],
        ['L', 21, 15.5],
      ],
      [
        ['M', 3, 15],
        ['L', 21, 15],
      ],
    ])
  })

  it('isTimestampRange accepts only UNIX-second ranges', () => {
    expect(isTimestampRange(null)).toBe(false)
    expect(isTimestampRange({ from: 1 as Time, to: 2 as Time })).toBe(true)
    expect(isTimestampRange({ from: '2024-01-01' as Time, to: 2 as Time })).toBe(false)
  })
})

describe('ChartCoords', () => {
  it("'nearest' snaps to the nearest bar, 'exact' needs a plotted bar", () => {
    const v = fakeView()
    const nearest = new ChartCoords('nearest')
    const exact = new ChartCoords('exact')
    nearest.ts = exact.ts = fakeTimeScale(v)
    expect(nearest.xOfTime(120)).toBe(10 + 2 * 8)
    expect(nearest.xOfTime(130)).toBe(10 + 2 * 8)
    expect(exact.xOfTime(120)).toBe(10 + 2 * 8)
    expect(exact.xOfTime(130)).toBeNull()
  })

  it('without a time scale or series nothing is placed', () => {
    const c = new ChartCoords('nearest')
    expect(c.xOfTime(0)).toBeNull()
    expect(c.yOfPrice(1)).toBeNull()
    expect(c.barSpacing).toBe(0)
  })

  it('yOfPrice works detached from the adapter (passed as a function)', () => {
    const v = fakeView()
    const c = new ChartCoords('exact')
    c.series = fakeSeries(v)
    const y = c.yOfPrice
    expect(y(10)).toBe(280)
  })

  it('barSpacing: the known value as is, else read from the time scale once', () => {
    const v = fakeView()
    const c = new ChartCoords('nearest')
    c.ts = fakeTimeScale(v)
    c.spacing = 5
    expect(c.barSpacing).toBe(5)
    expect(v.calls.get('logicalToCoordinate')).toBeUndefined()
    c.spacing = null
    expect(c.barSpacing).toBe(8)
    expect(c.barSpacing).toBe(8)
    expect(v.calls.get('logicalToCoordinate')).toBe(2)
  })
})

describe('LayerRenderer', () => {
  function target(): { target: RenderTarget; scopes: () => number } {
    let scopes = 0
    const rec = canvasRecording()
    return {
      target: {
        useBitmapCoordinateSpace: (f: (s: BitmapScope) => void) => {
          scopes++
          f({ context: rec.ctx, horizontalPixelRatio: 1, verticalPixelRatio: 1 } as BitmapScope)
        },
      } as unknown as RenderTarget,
      scopes: () => scopes,
    }
  }

  it('draws the geometry with the current colours; nothing without geometry', () => {
    const painted: [number[], string][] = []
    const r = new LayerRenderer<number[], string>('red', (_s, g, c) => painted.push([g, c]))
    const t = target()
    r.draw(t.target)
    expect(t.scopes()).toBe(0)
    r.geom = [1, 2]
    r.colors = 'blue'
    r.draw(t.target)
    expect(painted).toEqual([[[1, 2], 'blue']])
  })

  it('an empty geometry never opens the bitmap scope', () => {
    const r = new LayerRenderer<number[], string>(
      'red',
      () => {},
      (g) => g.length === 0,
    )
    const t = target()
    r.geom = []
    r.draw(t.target)
    expect(t.scopes()).toBe(0)
    r.geom = [1]
    r.draw(t.target)
    expect(t.scopes()).toBe(1)
  })
})

/* ---------- primitives ---------- */

describe('LayerPrimitive / CulledLayerPrimitive', () => {
  it('one pane view with the given z-order and the primitive uid', () => {
    const p = new TestCulled()
    expect(p.uid).toBe('t')
    expect(p.paneViews()).toHaveLength(1)
    expect(p.paneViews()).toBe(p.paneViews())
    expect(p.paneViews()[0].zOrder?.()).toBe('bottom')
    expect(p.colors()).toEqual({ color: '#2962ff' })
  })

  it('setView / setVisible / setColors ask for a redraw only on a change', () => {
    const v = fakeView()
    const p = new TestCulled()
    const a = attach(p, v)
    const view = { times: [0, 60], price: 10 }
    p.setView(view)
    p.setView(view)
    p.setVisible(true)
    expect(a.redraws()).toBe(1)
    p.setVisible(false)
    p.setColors({ color: '#f23645' })
    expect(a.redraws()).toBe(3)
    expect(p.isVisible()).toBe(false)
    expect(p.view()).toBe(view)
  })

  it('builds the visible geometry and reuses it while nothing moved', () => {
    const v = fakeView()
    const p = new TestCulled()
    attach(p, v)
    p.setView({ times: [0, 60, 900], price: 10 })
    p.updateAllViews()
    expect(p.geometry()).toEqual({ xs: [10, 18], y: 280, spacing: 8, width: 500 })
    const first = p.geometry()
    p.updateAllViews()
    expect(p.buildCount()).toBe(1)
    expect(p.geometry()).toBe(first)
    // Scroll (x0), zoom (spacing), resize (width), a price-scale move and a range change rebuild.
    for (const move of [
      () => (v.x0 = 12),
      () => (v.spacing = 9),
      () => (v.width = 400),
      () => (v.yOffset = 310),
      () => (v.range = { from: 0, to: 60 }),
    ]) {
      const before = p.buildCount()
      move()
      p.updateAllViews()
      expect(p.buildCount()).toBe(before + 1)
    }
    expect(p.geometry()).toEqual({ xs: [12, 21], y: 290, spacing: 9, width: 400 })
  })

  it('a data, visibility or colour change rebuilds; hidden or no range draws nothing', () => {
    const v = fakeView()
    const p = new TestCulled()
    attach(p, v)
    p.setView({ times: [0], price: 1 })
    p.updateAllViews()
    p.setColors({ color: '#000000' })
    p.updateAllViews()
    expect(p.buildCount()).toBe(2)
    p.setVisible(false)
    p.updateAllViews()
    expect(p.geometry()).toBeNull()
    p.setVisible(true)
    p.updateAllViews()
    expect(p.buildCount()).toBe(3)
    v.range = null
    p.updateAllViews()
    expect(p.geometry()).toBeNull()
    expect(p.buildCount()).toBe(3)
  })

  it('detach drops the chart and the geometry; a re-attach rebuilds', () => {
    const v = fakeView()
    const p = new TestCulled()
    attach(p, v)
    p.setView({ times: [0], price: 1 })
    p.updateAllViews()
    p.detached()
    expect(p.geometry()).toBeNull()
    p.updateAllViews()
    expect(p.geometry()).toBeNull()
    attach(p, v)
    p.updateAllViews()
    expect(p.buildCount()).toBe(2)
    expect(p.geometry()).not.toBeNull()
  })

  it('without a view signature the width and bar spacing come from the time scale', () => {
    const v = fakeView()
    const p = new TestCulled()
    const ts = fakeTimeScale(v)
    // The signature needs logical 0 placed; this chart cannot place it, but can place bars.
    const noZero = {
      ...ts,
      logicalToCoordinate: (i: number) => (i === 0 ? null : ts.logicalToCoordinate(i as never)),
      timeToCoordinate: ts.timeToCoordinate,
      getVisibleRange: ts.getVisibleRange,
      width: ts.width,
    } as unknown as ITimeScaleApi<Time>
    p.attached({
      chart: { timeScale: () => noZero } as unknown as IChartApiBase<Time>,
      series: fakeSeries(v),
      requestUpdate: () => {},
    } as unknown as AttachedParam)
    p.setView({ times: [60], price: 1 })
    p.updateAllViews()
    expect(p.geometry()?.width).toBe(500)
    expect(p.geometry()?.spacing).toBe(0) // barSpacingOf: no coordinate for logical 0
    // No signature: never cached, so every update rebuilds.
    p.updateAllViews()
    expect(p.buildCount()).toBe(2)
  })
})

class CountingPrimitive extends LayerPrimitive<number, { a: string; b: boolean }> {
  colorSets = 0
  constructor(uid: string, colors: { a: string; b: boolean }) {
    super(uid, { colors, paint: () => {}, zOrder: 'normal', snap: 'nearest' })
  }
  override setColors(colors: { a: string; b: boolean }): void {
    this.colorSets++
    super.setColors(colors)
  }
  updateAllViews(): void {}
}

class TestHost extends LayerHost<
  { a: string; b: boolean },
  CountingPrimitive,
  { primitive: CountingPrimitive }
> {
  constructor() {
    super({ a: 'x', b: false }, ['a', 'b'])
  }
  sync(uids: string[]): void {
    const keep = new Set<string>()
    for (const uid of uids) {
      keep.add(uid)
      if (!this.entries.has(uid))
        this.add(uid, { primitive: new CountingPrimitive(uid, this.colors) })
    }
    this.sweep(keep)
  }
  current(): { a: string; b: boolean } {
    return this.colors
  }
}

function recordingHost(): { host: PrimitiveHost; log: string[] } {
  const log: string[] = []
  return {
    log,
    host: {
      attachPrimitive: (p: ISeriesPrimitive<Time>) => log.push(`+${(p as CountingPrimitive).uid}`),
      detachPrimitive: (p: ISeriesPrimitive<Time>) => log.push(`-${(p as CountingPrimitive).uid}`),
    } as unknown as PrimitiveHost,
  }
}

describe('LayerHost', () => {
  it('adds and attaches new uids, sweeps the rest, keeps creation order', () => {
    const h = new TestHost()
    const s = recordingHost()
    h.attach(s.host)
    h.sync(['a', 'b'])
    h.sync(['b', 'c'])
    expect(h.uids()).toEqual(['b', 'c'])
    expect(s.log).toEqual(['+a', '+b', '+c', '-a'])
    expect(h.primitiveOf('b')?.uid).toBe('b')
    expect(h.primitiveOf('a')).toBeUndefined()
  })

  it('attach moves every primitive; detach(false) only drops the references', () => {
    const h = new TestHost()
    h.sync(['a'])
    const s1 = recordingHost()
    const s2 = recordingHost()
    h.attach(s1.host)
    h.attach(s1.host)
    h.attach(s2.host)
    expect(s1.log).toEqual(['+a', '-a'])
    expect(s2.log).toEqual(['+a'])
    const p = h.primitiveOf('a') as CountingPrimitive
    attach(p, fakeView())
    h.detach(false)
    expect(s2.log).toEqual(['+a'])
    expect(p.geometry()).toBeNull()
    h.detach()
    expect(s2.log).toEqual(['+a'])
  })

  it('setColors fans a copy out only when a compared field changed', () => {
    const h = new TestHost()
    h.sync(['a', 'b'])
    const pa = h.primitiveOf('a') as CountingPrimitive
    h.setColors({ a: 'x', b: false })
    expect(pa.colorSets).toBe(0)
    // A field outside the compared keys is not a change.
    h.setColors({ a: 'x', b: false, extra: 1 } as { a: string; b: boolean })
    expect(pa.colorSets).toBe(0)
    const next = { a: 'y', b: false }
    h.setColors(next)
    expect(pa.colorSets).toBe(1)
    expect(h.primitiveOf('b')?.colorSets).toBe(1)
    expect(h.current()).toEqual(next)
    expect(h.current()).not.toBe(next)
    expect(pa.colors()).toBe(h.current())
    h.setColors({ a: 'y', b: true })
    expect(pa.colorSets).toBe(2)
  })
})
