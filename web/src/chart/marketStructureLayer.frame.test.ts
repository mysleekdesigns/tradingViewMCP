// Phase 19 wave 2: the market-structure primitive's frame path. The geometry cache must return
// exactly what a fresh build would (and skip the build when the view did not move), and the
// draw must paint exactly what the pre-cache renderer painted, without new dash arrays per draw.

import { describe, expect, it } from 'vitest'
import { computeMarketStructure, type MarketStructureParams } from '../indicators/marketStructure'
import type { IndicatorConfig } from '../state/indicators'
import type { Candle } from '../types'
import {
  bitmapScope,
  canvasRecording,
  fakeChartHost,
  fakePriceToCoordinate,
  fakeTimeToIndex,
  fakeVisibleRange,
  type FakeView,
} from './layerFrame.testkit'
import {
  DEFAULT_MARKET_STRUCTURE_COLORS,
  MarketStructureLayer,
  MarketStructurePrimitive,
  StructureSpans,
  drawStructure,
  marketStructureColorsFor,
  structureGeometry,
  type MarketStructureColors,
  type StructureCoords,
  type StructureGeometry,
  type SwingLineState,
} from './marketStructureLayer'
import { paletteFor } from './theme'

/* ------------------------------------------------------------------------------------------ */
/* Helpers                                                                                     */
/* ------------------------------------------------------------------------------------------ */

function mulberry32(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let x = a
    x = Math.imul(x ^ (x >>> 15), x | 1)
    x ^= x + Math.imul(x ^ (x >>> 7), x | 61)
    return ((x ^ (x >>> 14)) >>> 0) / 4294967296
  }
}

const T0 = 1_700_006_400
const H4 = 14_400

function randomWalk(n: number, seed: number): Candle[] {
  const rnd = mulberry32(seed)
  const q = (v: number) => Math.round(v * 2) / 2
  const out: Candle[] = []
  let prev = 100
  for (let i = 0; i < n; i++) {
    const open = prev
    const close = Math.max(0.5, q(open + (rnd() - 0.5) * 6))
    const high = q(Math.max(open, close) + rnd() * 2)
    const low = Math.max(0.5, q(Math.min(open, close) - rnd() * 2))
    out.push({ time: T0 + i * H4, open, high, low, close, volume: 1 })
    prev = close
  }
  return out
}

const SMALL: MarketStructureParams = { left: 2, right: 2, maxZones: 3 }

function config(uid: string, extra: Record<string, number> = {}, visible = true): IndicatorConfig {
  return {
    uid,
    type: 'mstructure',
    inputs: {
      left: SMALL.left,
      right: SMALL.right,
      maxZones: SMALL.maxZones,
      showSwings: 1,
      showBos: 1,
      showChoch: 1,
      showZones: 1,
      ...extra,
    },
    source: 'close',
    colors: {},
    visible,
  }
}

const DARK = DEFAULT_MARKET_STRUCTURE_COLORS
const LIGHT = marketStructureColorsFor(
  paletteFor({ theme: 'light', upColor: '#089981', downColor: '#f23645', timezone: 'UTC' }),
)

/** Coordinates read straight from the fake view, as the pre-cache primitive built them. */
function freshCoords(v: FakeView): StructureCoords {
  return {
    visibleRange: () => fakeVisibleRange(v),
    xOfTime: (t) => {
      const i = fakeTimeToIndex(v, t, true)
      return i === null ? null : v.x0 + i * v.spacing
    },
    yOfPrice: (p) => fakePriceToCoordinate(v, p),
    width: v.width,
  }
}

function freshGeometry(p: MarketStructurePrimitive, v: FakeView): StructureGeometry | null {
  const view = p.view()
  return p.isVisible() && view !== null ? structureGeometry(view, freshCoords(v)) : null
}

/* ------------------------------------------------------------------------------------------ */
/* (a) Geometry cache                                                                          */
/* ------------------------------------------------------------------------------------------ */

describe('MarketStructurePrimitive: geometry cache', () => {
  function setup(seed: number, x0 = -3000) {
    const bars = randomWalk(700, seed)
    const view: FakeView = {
      x0,
      spacing: 8,
      width: 800,
      offset: 900,
      scale: 4,
      log: false,
      times: [],
    }
    const { host } = fakeChartHost(view)
    const layer = new MarketStructureLayer()
    layer.attach(host)
    return { bars, view, layer }
  }

  it('an unchanged view (drawing drag, crosshair move) returns the cached geometry, no rebuild', () => {
    const { bars, view, layer } = setup(5)
    const plotted = bars.slice(0, 500)
    view.times = plotted.map((b) => b.time)
    layer.sync([config('a')], plotted, plotted[plotted.length - 2].time)
    const p = layer.primitiveOf('a') as MarketStructurePrimitive
    p.updateAllViews()
    const geom = p.geometry()
    expect(geom?.lines.length).toBeGreaterThan(0)
    expect(p.buildCount()).toBe(1)
    for (let i = 0; i < 100; i++) p.updateAllViews()
    expect(p.buildCount()).toBe(1)
    expect(p.geometry()).toBe(geom)
    expect(p.geometry()).toEqual(freshGeometry(p, view))
  })

  it('scroll, zoom, resize, price scale, data, colours, toggles and visibility each rebuild', () => {
    const { bars, view, layer } = setup(6)
    let plotted = bars.slice(0, 500)
    view.times = plotted.map((b) => b.time)
    const sync = (cfg: IndicatorConfig) =>
      layer.sync([cfg], plotted, plotted[plotted.length - 2].time)
    sync(config('a'))
    const p = layer.primitiveOf('a') as MarketStructurePrimitive
    p.updateAllViews()
    const changes: (() => void)[] = [
      () => (view.x0 -= 8),
      () => (view.spacing = 9),
      () => (view.width = 640),
      () => (view.scale = 4.5),
      () => (view.offset = 950),
      () => (view.log = true),
      () => {
        plotted = bars.slice(0, 501)
        view.times = plotted.map((b) => b.time)
        sync(config('a'))
      },
      () => layer.setColors(LIGHT),
      () => sync(config('a', { showZones: 0 })),
      () => sync(config('a', { showZones: 0 }, false)),
      () => sync(config('a')),
    ]
    for (const change of changes) {
      const before = p.buildCount()
      change()
      p.updateAllViews()
      if (p.isVisible()) expect(p.buildCount()).toBe(before + 1)
      expect(p.geometry()).toEqual(freshGeometry(p, view))
    }
  })

  it('equals a fresh build across random sequences of view and data changes', () => {
    for (const seed of [1, 2, 3]) {
      const rnd = mulberry32(100 + seed)
      // 300 bars of 8 px, the last one at the right edge of the 800 px pane.
      const { bars, view, layer } = setup(seed, 800 - 300 * 8)
      let n = 300
      let plotted = bars.slice(0, n)
      let inputs: Record<string, number> = {}
      let visible = true
      const sync = () => {
        view.times = plotted.map((b) => b.time)
        layer.sync([config('a', inputs, visible)], plotted, plotted[plotted.length - 2].time)
      }
      sync()
      const p = layer.primitiveOf('a') as MarketStructurePrimitive
      let hits = 0
      for (let step = 0; step < 400; step++) {
        const k = Math.floor(rnd() * 12)
        if (k === 0) view.x0 += Math.round((rnd() - 0.5) * 20) * view.spacing
        else if (k === 1) view.x0 += (rnd() - 0.5) * 7
        else if (k === 2) {
          const s = Math.max(1, view.spacing * (0.7 + rnd() * 0.6))
          view.x0 = view.width - (view.width - view.x0) * (s / view.spacing)
          view.spacing = s
        } else if (k === 3) view.width = 500 + Math.floor(rnd() * 600)
        else if (k === 4) {
          view.scale *= 0.8 + rnd() * 0.4
          view.offset += (rnd() - 0.5) * 100
        } else if (k === 5) view.log = !view.log
        else if (k === 6 && n < bars.length) {
          // A new bar: the forming one closes; the view follows the right edge.
          n += 1
          plotted = bars.slice(0, n)
          view.x0 -= view.spacing
          sync()
        } else if (k === 7) {
          // A forming-bar tick: the last bar is a new object, nothing closed.
          const f = plotted[plotted.length - 1]
          plotted = [...plotted.slice(0, -1), { ...f, close: f.close + 0.5 }]
          sync()
        } else if (k === 8) layer.setColors(rnd() < 0.5 ? LIGHT : DARK)
        else if (k === 9) {
          inputs = {
            showSwings: rnd() < 0.7 ? 1 : 0,
            showZones: rnd() < 0.7 ? 1 : 0,
          }
          sync()
        } else if (k === 10) {
          visible = rnd() < 0.8
          sync()
        }
        // k === 11 (and any no-op): a crosshair move or a drawing drag, nothing moved.
        const before = p.buildCount()
        p.updateAllViews()
        expect(p.geometry()).toEqual(freshGeometry(p, view))
        if (p.buildCount() === before) hits += 1
        // A second update in the same frame never rebuilds.
        const g = p.geometry()
        const built = p.buildCount()
        p.updateAllViews()
        expect(p.buildCount()).toBe(built)
        expect(p.geometry()).toBe(g)
      }
      expect(hits).toBeGreaterThan(0)
    }
  })

  it('detach drops the geometry; re-attach rebuilds', () => {
    const { bars, view, layer } = setup(9)
    const plotted = bars.slice(0, 400)
    view.times = plotted.map((b) => b.time)
    layer.sync([config('a')], plotted, plotted[plotted.length - 2].time)
    const p = layer.primitiveOf('a') as MarketStructurePrimitive
    p.updateAllViews()
    layer.detach(false)
    p.updateAllViews()
    expect(p.geometry()).toBeNull()
    const other = fakeChartHost(view)
    layer.attach(other.host)
    const before = p.buildCount()
    p.updateAllViews()
    expect(p.buildCount()).toBe(before + 1)
    expect(p.geometry()).toEqual(freshGeometry(p, view))
  })
})

/* ------------------------------------------------------------------------------------------ */
/* (b) Draw output, (c) no dash allocation                                                     */
/* ------------------------------------------------------------------------------------------ */

/**
 * The renderer as it was before the frame-path change (verbatim, apart from the constants
 * inlined): every recorded paint operation of the current renderer must match it.
 */
function referenceDrawStructure(
  ctx: CanvasRenderingContext2D,
  hr: number,
  vr: number,
  geom: StructureGeometry,
  colors: MarketStructureColors,
): void {
  const LINE_ALPHA: Record<SwingLineState, number> = {
    active: 0.9,
    broken: 0.75,
    superseded: 0.35,
  }
  const setDash = (state: SwingLineState, ratio: number) => {
    if (state === 'active') ctx.setLineDash([Math.round(4 * ratio), Math.round(3 * ratio)])
    else if (state === 'superseded') ctx.setLineDash([Math.round(1 * ratio), Math.round(3 * ratio)])
    else ctx.setLineDash([])
  }
  const fillScale = colors.light ? 0.8 : 1
  const lineWidth = Math.max(1, Math.floor(vr))
  const half = lineWidth % 2 ? 0.5 : 0
  ctx.save()
  for (const z of geom.zones) {
    const color = z.side === 'support' ? colors.up : colors.down
    const left = Math.round(z.x0 * hr)
    const right = Math.max(left + 1, Math.round(z.x1 * hr))
    const top = Math.round(z.top * vr)
    const bottom = Math.max(top + 1, Math.round(z.bottom * vr))
    ctx.fillStyle = color
    const split =
      z.retestX === null ? right : Math.min(right, Math.max(left, Math.round(z.retestX * hr)))
    if (split > left) {
      ctx.globalAlpha = 0.1 * fillScale
      ctx.fillRect(left, top, split - left, bottom - top)
    }
    if (right > split) {
      ctx.globalAlpha = 0.2 * fillScale
      ctx.fillRect(split, top, right - split, bottom - top)
    }
    ctx.globalAlpha = 0.55
    ctx.strokeStyle = color
    ctx.lineWidth = lineWidth
    ctx.setLineDash(z.active ? [Math.round(4 * hr), Math.round(3 * hr)] : [])
    ctx.beginPath()
    ctx.moveTo(left, top + half)
    ctx.lineTo(right, top + half)
    ctx.moveTo(left, bottom - lineWidth + half)
    ctx.lineTo(right, bottom - lineWidth + half)
    ctx.stroke()
  }
  ctx.lineWidth = lineWidth
  for (const l of geom.lines) {
    const color = l.broke === 'up' ? colors.up : l.broke === 'down' ? colors.down : colors.neutral
    ctx.globalAlpha = LINE_ALPHA[l.state]
    ctx.strokeStyle = color
    setDash(l.state, hr)
    const y = Math.round(l.y * vr) + half
    ctx.beginPath()
    ctx.moveTo(Math.round(l.x0 * hr), y)
    ctx.lineTo(Math.max(Math.round(l.x0 * hr) + 1, Math.round(l.x1 * hr)), y)
    ctx.stroke()
  }
  ctx.setLineDash([])
  ctx.globalAlpha = 0.9
  ctx.fillStyle = colors.neutral
  const r = 2.25 * Math.max(hr, vr)
  for (const d of geom.dots) {
    ctx.beginPath()
    ctx.arc(d.x * hr, d.y * vr, r, 0, 2 * Math.PI)
    ctx.fill()
  }
  ctx.restore()
}

/** Every line state, both zone sides, overlapping same- and mixed-colour shapes, both dot kinds. */
const HAND: StructureGeometry = {
  zones: [
    { x0: 10, x1: 100, top: 50, bottom: 60, side: 'support', retestX: 40, active: true },
    { x0: 20, x1: 80, top: 55, bottom: 70, side: 'support', retestX: null, active: true },
    { x0: 20, x1: 80, top: 70, bottom: 70, side: 'resistance', retestX: null, active: false },
    { x0: 30, x1: 802, top: 20, bottom: 35, side: 'resistance', retestX: 900, active: true },
  ],
  lines: [
    { x0: 5, x1: 90, y: 30, state: 'broken', broke: 'up' },
    { x0: 5, x1: 90, y: 30.4, state: 'broken', broke: 'down' },
    { x0: 15, x1: 802, y: 40, state: 'active', broke: null },
    { x0: 15, x1: 60, y: 45.6, state: 'superseded', broke: null },
    { x0: 25, x1: 802, y: 48, state: 'active', broke: null },
  ],
  dots: [
    { x: 3, y: 30, kind: 'high' },
    { x: 3.5, y: 30.2, kind: 'low' },
  ],
}

/** Geometry built by the real engine and geometry function over several views. */
function engineGeometries(): StructureGeometry[] {
  const bars = randomWalk(1200, 21)
  const out: StructureGeometry[] = []
  for (const params of [SMALL, { left: 5, right: 5, maxZones: 10 }]) {
    const r = computeMarketStructure(bars, params)
    const spans = new StructureSpans()
    spans.update(r)
    for (const [a, b, px] of [
      [0, 200, 10],
      [500, 700, 4],
      [1000, 1199, 4],
    ]) {
      const from = bars[a].time
      const to = bars[b].time
      for (const zones of [true, false]) {
        const g = structureGeometry(
          { result: r, spans, show: { swings: true, bos: true, choch: true, zones }, version: 1 },
          {
            visibleRange: () => ({ from, to }),
            xOfTime: (t) => ((t - from) / H4) * px + 0.37,
            yOfPrice: (p) => 900 - 4.3 * p,
            width: 800,
          },
        )
        if (g !== null) out.push(g)
      }
    }
  }
  return out
}

const RATIOS: [number, number][] = [
  [1, 1],
  [1.25, 1.25],
  [1.5, 2],
  [2, 2],
  [3, 3],
]

describe('drawStructure: frame path', () => {
  it('paints exactly what the pre-cache renderer painted (light and dark, every pixel ratio)', () => {
    const geoms = [HAND, ...engineGeometries()]
    expect(geoms.some((g) => g.zones.length > 0 && g.lines.length > 0 && g.dots.length > 0)).toBe(
      true,
    )
    for (const geom of geoms)
      for (const colors of [DARK, LIGHT])
        for (const [hr, vr] of RATIOS) {
          const now = canvasRecording()
          drawStructure(bitmapScope(now.ctx, hr, vr) as never, geom, colors)
          const ref = canvasRecording()
          referenceDrawStructure(ref.ctx, hr, vr, geom, colors)
          expect(now.ops).toEqual(ref.ops)
          // The context is left as it was found.
          expect(now.finalState()).toEqual(now.initialState())
        }
  })

  it('a repeat draw allocates no dash arrays (the same arrays are passed again)', () => {
    const first = canvasRecording()
    drawStructure(bitmapScope(first.ctx, 2) as never, HAND, DARK)
    const again = canvasRecording()
    drawStructure(bitmapScope(again.ctx, 2) as never, HAND, DARK)
    expect(again.dashArgs.length).toBeGreaterThan(0)
    expect(again.dashArgs.length).toBe(first.dashArgs.length)
    const seen = new Set(first.dashArgs)
    for (const d of again.dashArgs) expect(seen.has(d)).toBe(true)
    // Three patterns in all: dashed (running zones, active levels), dotted, solid.
    expect(new Set(again.dashArgs).size).toBeLessThanOrEqual(3)
    expect(new Set(again.dashArgs.map((d) => d.join(',')))).toEqual(new Set(['8,6', '2,6', '']))
  })

  it('consecutive shapes of one colour set the style once', () => {
    const rec = canvasRecording()
    const lines = Array.from({ length: 10 }, (_, i) => ({
      x0: 0,
      x1: 50,
      y: 10 + i * 5,
      state: 'broken' as const,
      broke: 'up' as const,
    }))
    drawStructure(bitmapScope(rec.ctx, 1) as never, { zones: [], lines, dots: [] }, DARK)
    expect(rec.ops.filter((o) => o.op === 'stroke')).toHaveLength(10)
    expect(rec.writes.get('strokeStyle')).toBe(1)
    expect(rec.writes.get('setLineDash')).toBe(1)
  })
})
