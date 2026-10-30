// Phase 19 wave 2: the session-ranges primitive's frame path. The geometry cache must return
// exactly what a fresh build would (and skip the build when the view did not move), and the
// draw must paint exactly what the pre-cache renderer painted, without new dash arrays per draw.

import { describe, expect, it } from 'vitest'
import { SESSION_RANGES_DEFAULTS, computeSessionRanges } from '../indicators/sessionRanges'
import type { IndicatorDef, IndicatorId } from '../indicators/types'
import { sessionRangesDef } from '../indicators/sessionRanges'
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
  DEFAULT_SESSION_RANGES_COLORS,
  LABEL_FONT_PX,
  SessionRangesLayer,
  SessionRangesPrimitive,
  drawSessions,
  sessionGeometry,
  sessionRangesColorsFor,
  type SessionBoxGeometry,
  type SessionCoords,
  type SessionRangesColors,
} from './sessionRangesLayer'
import { fontFamily, paletteFor } from './theme'

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

const H1 = 3_600
const M15 = 900
/** 2023-11-14 18:00 UTC, so every series crosses a UTC midnight. */
const T0 = 1_700_006_400 - 6 * H1

function randomWalk(n: number, seed: number, step: number): Candle[] {
  const rnd = mulberry32(seed)
  const out: Candle[] = []
  let prev = 100
  for (let i = 0; i < n; i++) {
    const open = prev
    const close = Math.max(1, open + (rnd() - 0.5) * 4)
    const high = Math.max(open, close) + rnd() * 2
    const low = Math.max(0.5, Math.min(open, close) - rnd() * 2)
    out.push({ time: T0 + i * step, open, high, low, close, volume: 1 })
    prev = close
  }
  return out
}

const DEFS: ReadonlyMap<IndicatorId, IndicatorDef> = new Map([['sessions', sessionRangesDef]])

function config(uid: string, extra: Record<string, number> = {}, visible = true): IndicatorConfig {
  return {
    uid,
    type: 'sessions',
    inputs: {
      asiaStart: 0,
      asiaEnd: 8,
      londonStart: 7,
      londonEnd: 16,
      nyStart: 13,
      nyEnd: 22,
      orHours: 1,
      showAsia: 1,
      showLondon: 1,
      showNewYork: 1,
      showOpening: 1,
      ...extra,
    },
    source: 'close',
    colors: {},
    visible,
  }
}

const DARK = DEFAULT_SESSION_RANGES_COLORS
const LIGHT = sessionRangesColorsFor(
  paletteFor({ theme: 'light', upColor: '#089981', downColor: '#f23645', timezone: 'UTC' }),
)

/** Coordinates read straight from the fake view, as the pre-cache primitive built them. */
function freshCoords(v: FakeView): SessionCoords {
  return {
    visibleRange: () => fakeVisibleRange(v),
    xOfTime: (t) => {
      const i = fakeTimeToIndex(v, t, false)
      return i === null ? null : v.x0 + i * v.spacing
    },
    yOfPrice: (p) => fakePriceToCoordinate(v, p),
    // As the chart reports it: the distance between logical 0 and 1.
    barSpacing: Math.abs(v.x0 + 1 * v.spacing - (v.x0 + 0 * v.spacing)),
    width: v.width,
  }
}

function freshGeometry(p: SessionRangesPrimitive, v: FakeView): SessionBoxGeometry[] | null {
  const view = p.view()
  return p.isVisible() && view !== null ? sessionGeometry(view, freshCoords(v)) : null
}

/* ------------------------------------------------------------------------------------------ */
/* (a) Geometry cache                                                                          */
/* ------------------------------------------------------------------------------------------ */

describe('SessionRangesPrimitive: geometry cache', () => {
  function setup(seed: number, n: number, step = H1) {
    const bars = randomWalk(n, seed, step)
    const view: FakeView = {
      x0: 0,
      spacing: 8,
      width: 800,
      offset: 900,
      scale: 4,
      log: false,
      times: [],
    }
    const { host } = fakeChartHost(view)
    const layer = new SessionRangesLayer()
    layer.attach(host)
    return { bars, view, layer }
  }

  it('an unchanged view (drawing drag, crosshair move) returns the cached geometry, no rebuild', () => {
    const { bars, view, layer } = setup(5, 300)
    view.times = bars.map((b) => b.time)
    view.x0 = 800 - bars.length * 8
    layer.sync([config('a')], bars, bars[bars.length - 2].time, H1, DEFS)
    const p = layer.primitiveOf('a') as SessionRangesPrimitive
    p.updateAllViews()
    const geom = p.geometry()
    expect(geom?.length).toBeGreaterThan(0)
    expect(p.buildCount()).toBe(1)
    for (let i = 0; i < 100; i++) p.updateAllViews()
    expect(p.buildCount()).toBe(1)
    expect(p.geometry()).toBe(geom)
    expect(p.geometry()).toEqual(freshGeometry(p, view))
  })

  it('scroll, zoom, resize, price scale, data, colours, toggles and visibility each rebuild', () => {
    const { bars, view, layer } = setup(6, 400)
    let plotted = bars.slice(0, 300)
    view.times = plotted.map((b) => b.time)
    view.x0 = 800 - plotted.length * 8
    const sync = (cfg: IndicatorConfig) =>
      layer.sync([cfg], plotted, plotted[plotted.length - 2].time, H1, DEFS)
    sync(config('a'))
    const p = layer.primitiveOf('a') as SessionRangesPrimitive
    p.updateAllViews()
    const changes: (() => void)[] = [
      () => (view.x0 -= 8),
      () => (view.x0 += 3.5),
      () => (view.spacing = 9),
      () => (view.width = 640),
      () => (view.scale = 4.5),
      () => (view.offset = 950),
      () => (view.log = true),
      () => {
        plotted = bars.slice(0, 301)
        view.times = plotted.map((b) => b.time)
        sync(config('a'))
      },
      () => layer.setColors(LIGHT),
      () => sync(config('a', { showLondon: 0 })),
      () => sync(config('a', { showLondon: 0 }, false)),
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
    for (const [seed, step] of [
      [1, H1],
      [2, M15],
      [3, H1],
    ]) {
      const rnd = mulberry32(200 + seed)
      const { bars, view, layer } = setup(seed, 700, step)
      let n = 300
      let plotted = bars.slice(0, n)
      let inputs: Record<string, number> = {}
      let visible = true
      let interval = step
      view.x0 = 800 - n * view.spacing
      const sync = () => {
        view.times = plotted.map((b) => b.time)
        layer.sync(
          [config('a', inputs, visible)],
          plotted,
          plotted[plotted.length - 2].time,
          interval,
          DEFS,
        )
      }
      sync()
      const p = layer.primitiveOf('a') as SessionRangesPrimitive
      let hits = 0
      for (let s = 0; s < 400; s++) {
        const k = Math.floor(rnd() * 13)
        if (k === 0) view.x0 += Math.round((rnd() - 0.5) * 20) * view.spacing
        else if (k === 1) view.x0 += (rnd() - 0.5) * 7
        else if (k === 2) {
          const sp = Math.max(1, view.spacing * (0.7 + rnd() * 0.6))
          view.x0 = view.width - (view.width - view.x0) * (sp / view.spacing)
          view.spacing = sp
        } else if (k === 3) view.width = 500 + Math.floor(rnd() * 600)
        else if (k === 4) {
          view.scale *= 0.8 + rnd() * 0.4
          view.offset += (rnd() - 0.5) * 100
        } else if (k === 5) view.log = !view.log
        else if (k === 6 && n < bars.length) {
          n += 1
          plotted = bars.slice(0, n)
          view.x0 -= view.spacing
          sync()
        } else if (k === 7) {
          const f = plotted[plotted.length - 1]
          plotted = [...plotted.slice(0, -1), { ...f, high: f.high + 3, low: f.low - 3 }]
          sync()
        } else if (k === 8) layer.setColors(rnd() < 0.5 ? LIGHT : DARK)
        else if (k === 9) {
          inputs = {
            showAsia: rnd() < 0.7 ? 1 : 0,
            showLondon: rnd() < 0.7 ? 1 : 0,
            showOpening: rnd() < 0.7 ? 1 : 0,
          }
          sync()
        } else if (k === 10) {
          visible = rnd() < 0.8
          sync()
        } else if (k === 11) {
          // Interval switch to 1d and back (nothing drawn on 1d).
          interval = interval === step ? 86_400 : step
          sync()
        }
        // k === 12 (and any no-op): a crosshair move or a drawing drag, nothing moved.
        const before = p.buildCount()
        p.updateAllViews()
        expect(p.geometry()).toEqual(freshGeometry(p, view))
        if (p.buildCount() === before) hits += 1
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
    const { bars, view, layer } = setup(9, 200)
    view.times = bars.map((b) => b.time)
    view.x0 = 800 - bars.length * 8
    layer.sync([config('a')], bars, bars[bars.length - 2].time, H1, DEFS)
    const p = layer.primitiveOf('a') as SessionRangesPrimitive
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
function referenceDrawSessions(
  ctx: CanvasRenderingContext2D,
  hr: number,
  vr: number,
  geom: readonly SessionBoxGeometry[],
  colors: SessionRangesColors,
): void {
  if (geom.length === 0) return
  const lineWidth = Math.max(1, Math.floor(hr))
  const half = lineWidth % 2 ? 0.5 : 0
  const fillAlpha = 0.08 * (colors.light ? 0.8 : 1)
  ctx.save()
  ctx.lineWidth = lineWidth
  for (const g of geom) {
    const color = colors[g.session]
    const left = Math.round(g.x0 * hr)
    const right = Math.max(left + 1, Math.round(g.x1 * hr))
    const top = Math.round(g.top * vr)
    const bottom = Math.max(top + 1, Math.round(g.bottom * vr))
    ctx.globalAlpha = fillAlpha
    ctx.fillStyle = color
    ctx.fillRect(left, top, right - left, bottom - top)
    ctx.globalAlpha = 0.55
    ctx.strokeStyle = color
    ctx.setLineDash(g.session === 'opening' ? [Math.round(4 * hr), Math.round(3 * hr)] : [])
    ctx.strokeRect(
      left + half,
      top + half,
      Math.max(0, right - left - lineWidth),
      Math.max(0, bottom - top - lineWidth),
    )
  }
  ctx.setLineDash([])
  ctx.globalAlpha = 0.9
  ctx.font = `${Math.round(LABEL_FONT_PX * vr)}px ${fontFamily}`
  ctx.textAlign = 'left'
  for (const g of geom) {
    const l = g.label
    if (l === null) continue
    ctx.fillStyle = colors[g.session]
    ctx.textBaseline = l.baseline
    ctx.fillText(l.text, Math.round(l.x * hr), Math.round(l.y * vr))
  }
  ctx.restore()
}

/** Geometry built by the real engine and geometry function over several views and toggles. */
function engineGeometries(): SessionBoxGeometry[][] {
  const out: SessionBoxGeometry[][] = []
  for (const step of [H1, M15]) {
    const bars = randomWalk(800, 11, step)
    const r = computeSessionRanges(bars, SESSION_RANGES_DEFAULTS)
    for (const [a, b, px] of [
      [0, 100, 8],
      [300, 500, 3],
      [700, 799, 12],
    ]) {
      const from = bars[a].time
      const to = bars[b].time
      for (const show of [
        { asia: true, london: true, newyork: true, opening: true },
        { asia: true, london: false, newyork: true, opening: false },
      ]) {
        const g = sessionGeometry(
          { boxes: r.boxes, show, version: 1 },
          {
            visibleRange: () => ({ from, to }),
            xOfTime: (t) => ((t - from) / step) * px + 0.41,
            yOfPrice: (p) => 900 - 4.3 * p,
            barSpacing: px,
            width: 800,
          },
        )
        if (g !== null) out.push(g)
      }
    }
  }
  return out
}

/** Two adjacent same-session boxes, the opening range, labels with both baselines. */
const HAND: SessionBoxGeometry[] = [
  {
    session: 'asia',
    x0: 10,
    x1: 100,
    top: 50,
    bottom: 60,
    label: { text: 'Asia', x: 14, y: 53, baseline: 'top' },
  },
  {
    session: 'asia',
    x0: 90,
    x1: 200,
    top: 40,
    bottom: 65,
    label: { text: 'Asia', x: 94, y: 43, baseline: 'top' },
  },
  { session: 'opening', x0: 10, x1: 20, top: 55, bottom: 55, label: null },
  {
    session: 'opening',
    x0: 90,
    x1: 140,
    top: 45,
    bottom: 47,
    label: { text: 'OR', x: 94, y: 49, baseline: 'top' },
  },
  {
    session: 'london',
    x0: 60,
    x1: 160,
    top: 30,
    bottom: 34,
    label: { text: 'London', x: 64, y: 28, baseline: 'bottom' },
  },
]

const RATIOS: [number, number][] = [
  [1, 1],
  [1.25, 1.25],
  [1.5, 2],
  [2, 2],
  [3, 3],
]

describe('drawSessions: frame path', () => {
  it('paints exactly what the pre-cache renderer painted (light and dark, every pixel ratio)', () => {
    const geoms = [HAND, ...engineGeometries()]
    expect(geoms.some((g) => g.some((b) => b.session === 'opening' && b.label !== null))).toBe(true)
    for (const geom of geoms)
      for (const colors of [DARK, LIGHT])
        for (const [hr, vr] of RATIOS) {
          const now = canvasRecording()
          drawSessions(bitmapScope(now.ctx, hr, vr) as never, geom, colors)
          const ref = canvasRecording()
          referenceDrawSessions(ref.ctx, hr, vr, geom, colors)
          expect(now.ops).toEqual(ref.ops)
          expect(now.finalState()).toEqual(now.initialState())
        }
  })

  it('a repeat draw allocates no dash arrays (the same arrays are passed again)', () => {
    const first = canvasRecording()
    drawSessions(bitmapScope(first.ctx, 2) as never, HAND, DARK)
    const again = canvasRecording()
    drawSessions(bitmapScope(again.ctx, 2) as never, HAND, DARK)
    expect(again.dashArgs.length).toBeGreaterThan(0)
    expect(again.dashArgs.length).toBe(first.dashArgs.length)
    const seen = new Set(first.dashArgs)
    for (const d of again.dashArgs) expect(seen.has(d)).toBe(true)
    expect(new Set(again.dashArgs.map((d) => d.join(',')))).toEqual(new Set(['8,6', '']))
  })

  it('consecutive boxes of one session set the colour and dash once', () => {
    const rec = canvasRecording()
    const boxes: SessionBoxGeometry[] = Array.from({ length: 6 }, (_, i) => ({
      session: 'london',
      x0: i * 30,
      x1: i * 30 + 25,
      top: 10,
      bottom: 40,
      label: null,
    }))
    drawSessions(bitmapScope(rec.ctx, 1) as never, boxes, DARK)
    expect(rec.ops.filter((o) => o.op === 'strokeRect')).toHaveLength(6)
    expect(rec.writes.get('strokeStyle')).toBe(1)
    expect(rec.writes.get('fillStyle')).toBe(1)
    expect(rec.writes.get('setLineDash')).toBe(1)
  })
})
