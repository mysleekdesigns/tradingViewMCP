import type { IChartApi, ISeriesApi, SeriesType } from 'lightweight-charts'
import { describe, expect, it } from 'vitest'
import { ChartCoordinateAdapter } from './chartAdapter'
import { DrawingController } from './controller'
import {
  HIT_TOLERANCE,
  hitTestBody,
  hitTestHandles,
  projectDrawing,
  type CoordinateAdapter,
  type HitPart,
  type Point,
} from './geometry'
import { DrawingsPrimitive } from './primitives'
import { ShapeCache } from './shapeCache'
import { STEP, barTime, fakeAdapter, makeBars, movableAdapter } from './testAdapter'
import { TimeIndex } from './timeIndex'
import { DEFAULT_DRAWING_STYLE, DRAWING_KINDS, type Drawing } from './types'

/** Deterministic PRNG (mulberry32) so failures reproduce. */
function rng(seed: number) {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

/** The hit test as it was before the frame cache: every drawing re-projected on every call. */
function referenceHit(
  controller: DrawingController,
  adapter: CoordinateAdapter,
  p: Point,
  previewId: string | null = null,
): { id: string; part: HitPart; distance: number } | null {
  const { drawings } = controller.view()
  const width = adapter.paneWidth()
  const selectedId = controller.selectedId
  if (selectedId) {
    const sel = drawings.find((d) => d.id === selectedId)
    const shape = sel && projectDrawing(sel, adapter)
    const h = shape ? hitTestHandles(shape, p, width) : null
    if (sel && h) return { id: sel.id, part: h, distance: h.distance }
  }
  let best: { id: string; part: HitPart; distance: number } | null = null
  for (let i = drawings.length - 1; i >= 0; i--) {
    const d = drawings[i]
    if (d.id === previewId) continue
    const shape = projectDrawing(d, adapter)
    if (!shape) continue
    const distance = hitTestBody(shape, p, width, HIT_TOLERANCE)
    if (distance !== null && (best === null || distance < best.distance)) {
      best = { id: d.id, part: { part: 'body' }, distance }
    }
  }
  return best
}

function randomDrawings(r: () => number, count: number): Drawing[] {
  const out: Drawing[] = []
  for (let i = 0; i < count; i++) {
    const kind = DRAWING_KINDS[Math.floor(r() * DRAWING_KINDS.length)]
    // Off-bar times (seconds inside a bar) and times past either end of the data, too.
    const anchor = () => ({
      time: barTime(Math.floor(r() * 140) - 20) + (r() < 0.25 ? Math.floor(r() * STEP) : 0),
      price: 20 + r() * 170,
    })
    const anchors = kind === 'hline' || kind === 'hray' ? [anchor()] : [anchor(), anchor()]
    out.push({ id: `d${i}`, symbol: 'BTCUSDT', kind, anchors, style: DEFAULT_DRAWING_STYLE })
  }
  return out
}

/** Pointer positions: uniform over the pane plus points near each drawing's features. */
function probePoints(
  r: () => number,
  drawings: readonly Drawing[],
  adapter: CoordinateAdapter,
): Point[] {
  const pts: Point[] = []
  const w = adapter.paneWidth()
  for (let k = 0; k < 40; k++) pts.push({ x: r() * w, y: r() * 420 - 10 })
  for (const d of drawings) {
    const s = projectDrawing(d, adapter)
    if (!s) continue
    const near = (x: number, y: number) =>
      pts.push({ x: x + (r() - 0.5) * 16, y: y + (r() - 0.5) * 16 })
    if (s.kind === 'hline') near(r() * w, s.y)
    else if (s.kind === 'hray') near(s.x + r() * 50, s.y)
    else {
      near(s.x1, s.y1)
      near(s.x2, s.y2)
      near((s.x1 + s.x2) / 2, (s.y1 + s.y2) / 2)
      if (s.kind === 'fib') for (const lv of s.levels) near((s.x1 + s.x2) / 2, lv.y)
    }
  }
  return pts
}

describe('hit test against the frame cache', () => {
  it('equals the re-projecting hit test over random drawings, views, bars and pointers', () => {
    const outcomes = { miss: 0, body: 0, handle: 0 }
    for (let seed = 1; seed <= 25; seed++) {
      const r = rng(seed)
      const m = movableAdapter()
      const drawings = randomDrawings(r, 3 + Math.floor(r() * 12))
      const controller = new DrawingController(m.adapter)
      controller.setDrawings(drawings)
      const primitive = new DrawingsPrimitive(controller, m.adapter)
      let checks = 0
      for (let step = 0; step < 30; step++) {
        const action = r()
        if (action < 0.15) m.view.offset += (r() - 0.5) * 30
        else if (action < 0.27) m.view.spacing = 2 + r() * 20
        else if (action < 0.37) {
          m.view.scale = 0.5 + r() * 4
          m.view.base = 150 + r() * 100
        } else if (action < 0.42) m.view.width = 400 + Math.floor(r() * 600)
        else if (action < 0.47) {
          // New bars: a gap of random size somewhere, or a shorter/longer history.
          const n = 60 + Math.floor(r() * 80)
          const gapAt = Math.floor(r() * n)
          const gap = Math.floor(r() * 5)
          m.setBars(makeBars(n).map((b, i) => ({ time: i < gapAt ? b.time : b.time + gap * STEP })))
        } else if (action < 0.57) {
          controller.setSelected(r() < 0.2 ? null : drawings[Math.floor(r() * drawings.length)].id)
        }
        // Sometimes paint a frame first (the usual case), sometimes hit-test a view the frame
        // has not seen yet (a hit test right after scroll/zoom, before LWC's next redraw).
        if (r() < 0.6) primitive.updateAllViews()
        for (const p of probePoints(r, drawings, m.adapter)) {
          const hit = controller.hitTest(p)
          expect(hit, `seed ${seed} step ${step}`).toEqual(referenceHit(controller, m.adapter, p))
          outcomes[hit === null ? 'miss' : hit.part.part]++
          checks++
        }
      }
      expect(checks).toBeGreaterThan(500)
    }
    // The probes exercise every outcome, not just misses.
    expect(outcomes.body).toBeGreaterThan(1000)
    expect(outcomes.handle).toBeGreaterThan(100)
    expect(outcomes.miss).toBeGreaterThan(1000)
  })

  it('equals it during a drag (the dragged drawing changes every move) and while creating', () => {
    for (let seed = 101; seed <= 115; seed++) {
      const r = rng(seed)
      const m = movableAdapter()
      const drawings = randomDrawings(r, 8)
      let n = 0
      const controller = new DrawingController(m.adapter, {}, { newId: () => `new-${++n}` })
      controller.setDrawings(drawings)
      const primitive = new DrawingsPrimitive(controller, m.adapter)
      primitive.updateAllViews()

      // Find a body or handle to grab.
      const start = probePoints(r, drawings, m.adapter).find((p) => controller.hitTest(p) !== null)
      if (!start) continue
      expect(controller.pointerDown(start)).toBe(true)
      for (let k = 1; k <= 8; k++) {
        controller.pointerMove({ x: start.x + 9 * k, y: start.y + (r() - 0.5) * 40 })
        if (k % 2 === 0) primitive.updateAllViews()
        if (k === 5) m.view.offset += 3
        for (const p of probePoints(r, controller.view().drawings, m.adapter)) {
          expect(controller.hitTest(p), `seed ${seed} move ${k}`).toEqual(
            referenceHit(controller, m.adapter, p),
          )
        }
      }
      controller.pointerUp({ x: start.x + 80, y: start.y })
      for (const p of probePoints(r, controller.view().drawings, m.adapter)) {
        expect(controller.hitTest(p)).toEqual(referenceHit(controller, m.adapter, p))
      }

      // Creating: the preview follows the pointer and is never hit itself.
      controller.setTool('rectangle')
      controller.pointerDown({ x: 100, y: 100 })
      for (let k = 1; k <= 4; k++) {
        controller.pointerMove({ x: 100 + 20 * k, y: 100 + 15 * k })
        primitive.updateAllViews()
        for (const p of probePoints(r, controller.view().drawings, m.adapter)) {
          expect(controller.hitTest(p)).toEqual(referenceHit(controller, m.adapter, p, `new-${n}`))
        }
      }
    }
  })

  it('a mouse move over an unchanged view re-projects nothing', () => {
    const m = movableAdapter()
    const calls = { timeToLogical: 0, priceToY: 0 }
    const counted: CoordinateAdapter = {
      ...m.adapter,
      timeToLogical: (t) => (calls.timeToLogical++, m.adapter.timeToLogical(t)),
      priceToY: (p) => (calls.priceToY++, m.adapter.priceToY(p)),
    }
    const controller = new DrawingController(counted)
    controller.setDrawings(randomDrawings(rng(7), 12))
    controller.setSelected('d3')
    const primitive = new DrawingsPrimitive(controller, counted)
    primitive.updateAllViews()
    calls.timeToLogical = 0
    calls.priceToY = 0
    for (let k = 0; k < 50; k++) controller.hitTest({ x: k * 13, y: k * 7 })
    expect(calls.timeToLogical).toBe(0)
    // Only the two reference prices of the view fingerprint, per move.
    expect(calls.priceToY).toBe(2 * 50)
  })

  it('the selected drawing is projected once per hit test, not twice', () => {
    const base = fakeAdapter()
    let projections = 0
    const counted: CoordinateAdapter = {
      ...base,
      viewVersion: undefined,
      timeToLogical: (t) => (projections++, base.timeToLogical(t)),
    }
    const controller = new DrawingController(counted)
    const trend: Drawing = {
      id: 't',
      symbol: 'BTCUSDT',
      kind: 'trendline',
      anchors: [
        { time: barTime(1), price: 100 },
        { time: barTime(5), price: 110 },
      ],
      style: DEFAULT_DRAWING_STYLE,
    }
    controller.setDrawings([trend])
    controller.setSelected('t')
    controller.hitTest({ x: 500, y: 5 })
    // Two anchors, one projection (the old hit test projected the selection twice: 4).
    expect(projections).toBe(2)
  })
})

describe('ShapeCache', () => {
  const drawings: Drawing[] = randomDrawings(rng(3), 6)

  it('returns the same frame while the drawings and the view are unchanged', () => {
    const m = movableAdapter()
    const cache = new ShapeCache(m.adapter)
    const a = cache.frame(drawings)
    expect(cache.frame(drawings)).toBe(a)
    m.view.offset = 2
    const b = cache.frame(drawings)
    expect(b).not.toBe(a)
    expect(b.shapes).toEqual(drawings.map((d) => projectDrawing(d, m.adapter)))
  })

  it('re-projects only a drawing whose anchors changed', () => {
    const m = movableAdapter()
    const cache = new ShapeCache(m.adapter)
    const a = cache.frame(drawings)
    const moved = drawings.map((d, i) =>
      i === 2 ? { ...d, anchors: d.anchors.map((x) => ({ ...x, price: x.price + 1 })) } : d,
    )
    const b = cache.frame(moved)
    b.shapes.forEach((s, i) =>
      i === 2 ? expect(s).not.toBe(a.shapes[i]) : expect(s).toBe(a.shapes[i]),
    )
    expect(b.shapes[2]).toEqual(projectDrawing(moved[2], m.adapter))
  })

  it('an anchor edited in place (same object) is still re-projected', () => {
    const m = movableAdapter()
    const cache = new ShapeCache(m.adapter)
    const own = drawings.map((d) => ({ ...d, anchors: d.anchors.map((x) => ({ ...x })) }))
    cache.frame(own)
    own[1].anchors[0].price += 5
    expect(cache.frame([...own]).shapes[1]).toEqual(projectDrawing(own[1], m.adapter))
  })

  it('new bars invalidate through viewVersion even when the view did not move', () => {
    const m = movableAdapter()
    const cache = new ShapeCache(m.adapter)
    const offBar: Drawing = {
      ...drawings[0],
      kind: 'hray',
      anchors: [{ time: barTime(30) + 20, price: 90 }],
    }
    const before = cache.frame([offBar]).shapes[0]
    m.setBars(makeBars(100).map((b, i) => ({ time: i < 30 ? b.time : b.time + 2 * STEP })))
    const after = cache.frame([offBar]).shapes[0]
    expect(after).toEqual(projectDrawing(offBar, m.adapter))
    expect(after).not.toEqual(before)
  })

  it('an adapter without viewVersion is never cached', () => {
    const base = fakeAdapter()
    let calls = 0
    const adapter: CoordinateAdapter = {
      ...base,
      viewVersion: undefined,
      timeToLogical: (t) => (calls++, base.timeToLogical(t)),
    }
    const cache = new ShapeCache(adapter)
    cache.frame(drawings)
    const first = calls
    cache.frame(drawings)
    expect(calls).toBe(2 * first)
  })
})

describe('viewVersion', () => {
  it('TimeIndex: increases on setBars, setStep and an in-place length change', () => {
    const bars = makeBars(5)
    const index = new TimeIndex(bars, STEP)
    const v0 = index.version()
    expect(index.version()).toBe(v0)
    index.setBars(bars)
    const v1 = index.version()
    expect(v1).toBeGreaterThan(v0)
    index.setStep(300)
    const v2 = index.version()
    expect(v2).toBeGreaterThan(v1)
    bars.push({ time: bars[4].time + STEP })
    expect(index.version()).toBeGreaterThan(v2)
  })

  it('ChartCoordinateAdapter: increases on every chart/series bind and index change', () => {
    const adapter = new ChartCoordinateAdapter()
    const seen = [adapter.viewVersion()]
    const next = () => {
      const v = adapter.viewVersion()
      expect(v).toBeGreaterThan(seen[seen.length - 1])
      seen.push(v)
    }
    const chart = {} as IChartApi
    const series = {} as ISeriesApi<SeriesType>
    adapter.setChart(chart)
    next()
    adapter.setSeries(series)
    next()
    adapter.index.setBars(makeBars(3))
    next()
    adapter.index.setStep(900)
    next()
    adapter.setSeries(null, series)
    next()
    adapter.setChart(null, chart)
    next()
    expect(adapter.viewVersion()).toBe(seen[seen.length - 1])
  })
})
