import type { ISeriesPrimitive, Time } from 'lightweight-charts'
import { describe, expect, it, vi } from 'vitest'
import {
  SESSION_RANGES_DEFAULTS,
  computeSessionRanges,
  sessionRangesDef,
} from '../indicators/sessionRanges'
import type { IndicatorDef, IndicatorId, SessionBox, SessionRanges } from '../indicators/types'
import type { IndicatorConfig } from '../state/indicators'
import type { Candle } from '../types'
import {
  DEFAULT_SESSION_RANGES_COLORS,
  SESSION_LABELS,
  SessionRangesLayer,
  SessionRangesPrimitive,
  drawSessions,
  labelMinWidth,
  sessionDrawn,
  sessionGeometry,
  sessionRangesColorsFor,
  sessionShow,
  sessionsHiddenByAlignment,
  type SessionCoords,
  type SessionShow,
  type SessionView,
} from './sessionRangesLayer'
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

const H1 = 3_600
const DAY = 86_400
/** 2023-11-15 00:00 UTC. */
const D0 = 1_700_006_400
/** Start at 18:00 UTC the day before, so every series crosses a UTC midnight. */
const T0 = D0 - 6 * H1

function randomWalk(n: number, seed: number, step = H1): Candle[] {
  const rnd = mulberry32(seed)
  const out: Candle[] = []
  let prev = 100
  for (let i = 0; i < n; i++) {
    const open = prev
    const close = Math.max(1, open + (rnd() - 0.5) * 4)
    const high = Math.max(open, close) + rnd() * 2
    const low = Math.max(0.5, Math.min(open, close) - rnd() * 2)
    out.push({ time: T0 + i * step, open, high, low, close, volume: 1 + rnd() })
    prev = close
  }
  return out
}

const DEFS: ReadonlyMap<IndicatorId, IndicatorDef> = new Map([['sessions', sessionRangesDef]])
const ALL_ON: SessionShow = { asia: true, london: true, newyork: true, opening: true }

function config(
  uid: string,
  extra: Partial<Record<string, number>> = {},
  visible = true,
): IndicatorConfig {
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
    } as Record<string, number>,
    source: 'close',
    colors: {},
    visible,
  }
}

/** closedThrough of a live feed (the last plotted bar is forming) or of replay (all closed). */
function through(bars: readonly Candle[], lastClosed: boolean): number | null {
  const i = lastClosed ? bars.length - 1 : bars.length - 2
  return i >= 0 ? bars[i].time : null
}

function syncBars(
  layer: SessionRangesLayer,
  configs: readonly IndicatorConfig[],
  bars: readonly Candle[],
  lastClosed = false,
  interval = H1,
): void {
  layer.sync(configs, bars, through(bars, lastClosed), interval, DEFS)
}

const PX_PER_BAR = 10

/** Bars `PX_PER_BAR` apart from `from`; y = 1000 - 5 * price; an 800 px pane. */
function coordsFor(from: number, to: number, step = H1): SessionCoords {
  return {
    visibleRange: () => ({ from, to }),
    xOfTime: (t) => ((t - from) / step) * PX_PER_BAR,
    yOfPrice: (p) => 1000 - 5 * p,
    barSpacing: PX_PER_BAR,
    width: 800,
  }
}

function box(partial: Partial<SessionBox> & Pick<SessionBox, 'session' | 'start'>): SessionBox {
  return {
    end: partial.start + 8 * H1,
    high: 110,
    low: 100,
    firstTime: partial.start,
    lastTime: partial.start,
    bars: 1,
    ...partial,
  }
}

function fakeHost(times: readonly number[]) {
  const attached: ISeriesPrimitive<Time>[] = []
  let redraws = 0
  const chart = {
    timeScale: () => ({
      getVisibleRange: () => ({ from: times[0], to: times[times.length - 1] }),
      timeToCoordinate: (t: number) => {
        const i = times.indexOf(t)
        return i >= 0 ? i * PX_PER_BAR : null
      },
      logicalToCoordinate: (l: number) => l * PX_PER_BAR,
      width: () => 800,
    }),
  }
  const series = { priceToCoordinate: (p: number) => 1000 - 5 * p }
  const host = {
    attachPrimitive(p: ISeriesPrimitive<Time>) {
      attached.push(p)
      p.attached?.({ chart, series, requestUpdate: () => redraws++ } as unknown as Parameters<
        NonNullable<ISeriesPrimitive<Time>['attached']>
      >[0])
    },
    detachPrimitive(p: ISeriesPrimitive<Time>) {
      const i = attached.indexOf(p)
      if (i >= 0) attached.splice(i, 1)
      p.detached?.()
    },
  }
  return { host, attached, redraws: () => redraws }
}

/* ------------------------------------------------------------------------------------------ */
/* Geometry                                                                                    */
/* ------------------------------------------------------------------------------------------ */

describe('sessionGeometry', () => {
  const from = D0
  const to = D0 + 30 * H1
  const coords = coordsFor(from, to)
  const x = (t: number) => ((t - from) / H1) * PX_PER_BAR
  const asia = box({ session: 'asia', start: D0, firstTime: D0, lastTime: D0 + 7 * H1 })
  const opening = box({ session: 'opening', start: D0, end: D0 + H1, high: 104, low: 101 })
  const london = box({
    session: 'london',
    start: D0 + 7 * H1,
    end: D0 + 16 * H1,
    firstTime: D0 + 7 * H1,
    lastTime: D0 + 15 * H1,
    high: 120,
    low: 98,
  })
  const view = (boxes: SessionBox[], show = ALL_ON): SessionView => ({ boxes, show, version: 1 })

  it('known values: bar left edge to bar right edge, high to low, label in the corner', () => {
    const g = sessionGeometry(view([asia, opening, london]), coords)
    expect(g).toEqual([
      {
        session: 'asia',
        x0: -2, // x(D0) - 5 = -5, clamped 2 px outside the pane
        x1: x(D0 + 7 * H1) + 5,
        top: 1000 - 5 * 110,
        bottom: 1000 - 5 * 100,
        label: { text: 'Asia', x: 2, y: 450 + 3, baseline: 'top' },
      },
      {
        session: 'opening',
        x0: -2,
        x1: 5,
        top: 1000 - 5 * 104,
        bottom: 1000 - 5 * 101,
        // One bar (7 px after clamping) is too narrow for "OR".
        label: null,
      },
      {
        session: 'london',
        x0: x(D0 + 7 * H1) - 5,
        x1: x(D0 + 15 * H1) + 5,
        top: 1000 - 5 * 120,
        bottom: 1000 - 5 * 98,
        label: { text: 'London', x: x(D0 + 7 * H1) - 5 + 4, y: 400 + 3, baseline: 'top' },
      },
    ])
  })

  it('a short box puts its label above it; the opening range labels its bottom-left corner', () => {
    const flat = box({ ...london, high: 100.5, low: 100 }) // 2.5 px tall
    const wideOr = box({
      ...opening,
      start: D0 + DAY,
      firstTime: D0 + DAY,
      lastTime: D0 + DAY + 3 * H1,
    })
    const g = sessionGeometry(view([flat, wideOr]), coordsFor(D0, D0 + 40 * H1)) ?? []
    expect(g[0].label).toEqual({
      text: 'London',
      x: x(D0 + 7 * H1) - 1,
      y: 497.5 - 2,
      baseline: 'bottom',
    })
    // 15 px tall: below the 18 px needed inside, so under the box.
    expect(g[1].label).toEqual({
      text: 'OR',
      x: x(D0 + DAY) - 5 + 4,
      y: 1000 - 5 * 101 + 2,
      baseline: 'top',
    })
    const tallOr = box({ ...wideOr, high: 110, low: 100 })
    const g2 = sessionGeometry(view([tallOr]), coordsFor(D0, D0 + 40 * H1)) ?? []
    expect(g2[0].label).toEqual({ text: 'OR', x: x(D0 + DAY) - 1, y: 500 - 3, baseline: 'bottom' })
    expect(labelMinWidth(SESSION_LABELS.newyork)).toBeGreaterThan(labelMinWidth('OR'))
  })

  it('culls to the visible range by start (boxes up to 24h before from still count)', () => {
    const old = box({ session: 'asia', start: D0 - 3 * DAY, lastTime: D0 - 3 * DAY + 7 * H1 })
    // Started the day before `from`, last bar inside the view.
    const spanning = box({
      session: 'newyork',
      start: from + 10 * H1 - DAY,
      firstTime: from + 10 * H1 - DAY,
      lastTime: from + 2 * H1,
    })
    // Started the day before, ended before `from`.
    const endedBefore = box({
      session: 'london',
      start: from - 20 * H1,
      firstTime: from - 20 * H1,
      lastTime: from - H1,
    })
    const after = box({
      session: 'asia',
      start: to + H1,
      firstTime: to + H1,
      lastTime: to + 5 * H1,
    })
    const inView = box({ session: 'asia', start: from + 24 * H1, lastTime: from + 26 * H1 })
    const boxes = [old, spanning, endedBefore, asia, inView, after].sort(
      (a, b) => a.start - b.start,
    )
    const g = sessionGeometry(view(boxes), coords) ?? []
    expect(g.map((b) => b.session)).toEqual(['newyork', 'asia', 'asia'])
    expect(g[0].x1).toBe(x(from + 2 * H1) + 5)
    expect(g[2].x0).toBe(x(from + 24 * H1) - 5)
  })

  it('toggles hide sessions; nothing in view or no range -> empty / null', () => {
    const g = sessionGeometry(
      view([asia, opening, london], { ...ALL_ON, asia: false, opening: false }),
      coords,
    )
    expect(g?.map((b) => b.session)).toEqual(['london'])
    expect(sessionGeometry(view([asia]), { ...coords, visibleRange: () => null })).toBeNull()
    expect(sessionGeometry(view([asia]), coordsFor(D0 + 5 * DAY, D0 + 6 * DAY))).toEqual([])
    // A bar without a coordinate (not plotted) is skipped, never drawn to NaN.
    expect(sessionGeometry(view([asia]), { ...coords, xOfTime: () => null })).toEqual([])
  })

  it('sessionShow reads the 1/0 toggles; a missing key is on', () => {
    expect(sessionShow({ showAsia: 0, showLondon: 1, showNewYork: 0 })).toEqual({
      asia: false,
      london: true,
      newyork: false,
      opening: true,
    })
  })
})

/* ------------------------------------------------------------------------------------------ */
/* Drawing                                                                                     */
/* ------------------------------------------------------------------------------------------ */

describe('drawSessions', () => {
  function recordingScope(ratio: number) {
    const ctx = {
      globalAlpha: 1,
      fillStyle: '',
      strokeStyle: '',
      lineWidth: 1,
      font: '',
      textAlign: '',
      textBaseline: '',
      save: vi.fn(),
      restore: vi.fn(),
      setLineDash: vi.fn(),
      fills: [] as { style: string; alpha: number; rect: number[] }[],
      strokes: [] as { style: string; rect: number[] }[],
      texts: [] as { text: string; at: number[]; baseline: string; style: string }[],
      fillRect(x: number, y: number, w: number, h: number) {
        this.fills.push({
          style: String(this.fillStyle),
          alpha: this.globalAlpha,
          rect: [x, y, w, h],
        })
      },
      strokeRect(x: number, y: number, w: number, h: number) {
        this.strokes.push({ style: String(this.strokeStyle), rect: [x, y, w, h] })
      },
      fillText(text: string, x: number, y: number) {
        this.texts.push({
          text,
          at: [x, y],
          baseline: this.textBaseline,
          style: String(this.fillStyle),
        })
      },
    }
    const scope = {
      context: ctx,
      horizontalPixelRatio: ratio,
      verticalPixelRatio: ratio,
    } as unknown as Parameters<typeof drawSessions>[0]
    return { scope, ctx }
  }
  const C = DEFAULT_SESSION_RANGES_COLORS

  it('fills and borders each box in device pixels, dashes the opening range, labels on top', () => {
    const { scope, ctx } = recordingScope(2)
    drawSessions(
      scope,
      [
        {
          session: 'asia',
          x0: 10,
          x1: 100,
          top: 50,
          bottom: 60,
          label: { text: 'Asia', x: 14, y: 53, baseline: 'top' },
        },
        { session: 'opening', x0: 10, x1: 20, top: 55, bottom: 55, label: null },
      ],
      C,
    )
    expect(ctx.save).toHaveBeenCalledTimes(1)
    expect(ctx.restore).toHaveBeenCalledTimes(1)
    expect(ctx.fills.map((f) => [f.style, f.rect])).toEqual([
      [C.asia, [20, 100, 180, 20]],
      // A zero-height box still gets one device pixel.
      [C.opening, [20, 110, 20, 1]],
    ])
    for (const f of ctx.fills) expect(f.alpha).toBeLessThan(0.2)
    expect(ctx.strokes.map((s) => s.style)).toEqual([C.asia, C.opening])
    for (const s of [...ctx.fills, ...ctx.strokes])
      for (const v of s.rect) expect(Number.isFinite(v)).toBe(true)
    expect(ctx.setLineDash).toHaveBeenNthCalledWith(1, [])
    expect(ctx.setLineDash).toHaveBeenNthCalledWith(2, [8, 6])
    expect(ctx.texts).toEqual([{ text: 'Asia', at: [28, 106], baseline: 'top', style: C.asia }])
    expect(ctx.font).toMatch(/^20px /)
  })

  it('nothing to draw touches nothing', () => {
    const { scope, ctx } = recordingScope(1)
    drawSessions(scope, [], C)
    expect(ctx.save).not.toHaveBeenCalled()
  })
})

/* ------------------------------------------------------------------------------------------ */
/* Colours                                                                                     */
/* ------------------------------------------------------------------------------------------ */

describe('sessionRangesColorsFor', () => {
  const dark = sessionRangesColorsFor(
    paletteFor({ theme: 'dark', upColor: '#089981', downColor: '#f23645', timezone: 'UTC' }),
  )
  const light = sessionRangesColorsFor(
    paletteFor({ theme: 'light', upColor: '#089981', downColor: '#f23645', timezone: 'UTC' }),
  )

  it('dark and light differ, and every session has its own hue', () => {
    expect(dark.light).toBe(false)
    expect(light.light).toBe(true)
    for (const k of ['asia', 'london', 'newyork', 'opening'] as const) {
      expect(dark[k]).not.toBe(light[k])
      expect(dark[k]).toMatch(/^#[0-9a-f]{6}$/)
    }
    expect(new Set([dark.asia, dark.london, dark.newyork, dark.opening]).size).toBe(4)
    expect(new Set([light.asia, light.london, light.newyork, light.opening]).size).toBe(4)
    expect(dark).toEqual(DEFAULT_SESSION_RANGES_COLORS)
  })

  it('setColors reaches existing primitives; the same colours are a no-op', () => {
    const bars = randomWalk(60, 1)
    const h = fakeHost(bars.map((b) => b.time))
    const layer = new SessionRangesLayer(dark)
    layer.attach(h.host)
    syncBars(layer, [config('a')], bars)
    const p = layer.primitiveOf('a') as SessionRangesPrimitive
    expect(p.colors()).toEqual(dark)
    const redraws = h.redraws()
    layer.setColors({ ...dark })
    expect(h.redraws()).toBe(redraws)
    layer.setColors(light)
    expect(p.colors()).toEqual(light)
    expect(h.redraws()).toBe(redraws + 1)
  })
})

/* ------------------------------------------------------------------------------------------ */
/* Layer: closed bars only, incremental                                                        */
/* ------------------------------------------------------------------------------------------ */

const P = SESSION_RANGES_DEFAULTS

describe('SessionRangesLayer: incremental vs full', () => {
  it('bar-by-bar live sync equals a full recompute of the closed bars at every step', () => {
    const bars = randomWalk(24 * 6, 7)
    const layer = new SessionRangesLayer()
    const cfg = [config('a')]
    for (let i = 1; i <= bars.length; i++) {
      syncBars(layer, cfg, bars.slice(0, i))
      expect(layer.resultOf('a')).toEqual(computeSessionRanges(bars.slice(0, i - 1), P))
    }
    expect(layer.statsOf('a')).toEqual({
      recomputes: 1,
      pushed: bars.length - 1,
      count: bars.length - 1,
    })
  })

  it('the incremental layer draws exactly what a fresh layer synced once on the full set draws', () => {
    const bars = randomWalk(24 * 5 + 7, 8)
    const times = bars.map((b) => b.time)
    const a = fakeHost(times)
    const b = fakeHost(times)
    const inc = new SessionRangesLayer()
    inc.attach(a.host)
    for (let i = 1; i <= bars.length; i++) syncBars(inc, [config('x')], bars.slice(0, i))
    const once = new SessionRangesLayer()
    once.attach(b.host)
    syncBars(once, [config('x')], bars)
    const pi = inc.primitiveOf('x') as SessionRangesPrimitive
    const po = once.primitiveOf('x') as SessionRangesPrimitive
    pi.updateAllViews()
    po.updateAllViews()
    expect(pi.geometry()?.length).toBeGreaterThan(10)
    expect(pi.geometry()).toEqual(po.geometry())
    expect(inc.resultOf('x')).toEqual(once.resultOf('x'))
    // Every session kind and a box across the UTC midnight exist in the fixture.
    const sessions = new Set(inc.resultOf('x')?.boxes.map((x) => x.session))
    expect([...sessions].sort()).toEqual(['asia', 'london', 'newyork', 'opening'])
  })

  it('replay (every bar closed) and several bars at once also match', () => {
    const bars = randomWalk(200, 11)
    const layer = new SessionRangesLayer()
    for (const i of [1, 2, 30, 31, 90, 200]) {
      syncBars(layer, [config('a')], bars.slice(0, i), true)
      expect(layer.resultOf('a')).toEqual(computeSessionRanges(bars.slice(0, i), P))
    }
    expect(layer.statsOf('a')).toEqual({ recomputes: 1, pushed: 200, count: 200 })
  })

  it('custom session hours are used (and a change recomputes)', () => {
    const bars = randomWalk(100, 12)
    const layer = new SessionRangesLayer()
    syncBars(layer, [config('a')], bars)
    const custom = { asiaStart: 22, asiaEnd: 6, orHours: 3 }
    syncBars(layer, [config('a', custom)], bars)
    expect(layer.statsOf('a')?.recomputes).toBe(2)
    expect(layer.resultOf('a')).toEqual(
      computeSessionRanges(bars.slice(0, -1), {
        ...P,
        asia: { startHour: 22, endHour: 6 },
        openingHours: 3,
      }),
    )
  })
})

describe('SessionRangesLayer: the forming bar never moves a box', () => {
  it('ticks on the forming bar (extreme highs / lows) push nothing and change no box or view', () => {
    // End inside the London and New York overlap, so the forming bar is in two open sessions.
    const bars = randomWalk(6 + 24 + 15, 5)
    const h = fakeHost(bars.map((b) => b.time))
    const layer = new SessionRangesLayer()
    layer.attach(h.host)
    const cfg = [config('a')]
    syncBars(layer, cfg, bars)
    const p = layer.primitiveOf('a') as SessionRangesPrimitive
    const view = p.view()
    const snapshot = structuredClone(layer.resultOf('a')) as SessionRanges
    const stats = layer.statsOf('a')
    const redraws = h.redraws()
    const forming = bars[bars.length - 1]
    const open = snapshot.boxes.filter((b) => forming.time < b.end && forming.time >= b.start)
    expect(open.length).toBeGreaterThan(0)
    for (const [high, low] of [
      [1e6, forming.low],
      [forming.high, 0.0001],
      [1e6, 0.0001],
      [forming.high, forming.low],
    ]) {
      const ticked = [...bars.slice(0, -1), { ...forming, high, low, close: (high + low) / 2 }]
      syncBars(layer, cfg, ticked)
      expect(layer.resultOf('a')).toEqual(snapshot)
      expect(p.view()).toBe(view)
      expect(layer.statsOf('a')).toEqual(stats)
      expect(h.redraws()).toBe(redraws)
    }
    // ... and the geometry never reaches the tick's extremes.
    p.updateAllViews()
    for (const g of p.geometry() ?? []) {
      expect(g.top).toBeGreaterThan(1000 - 5 * 1e5)
      expect(g.bottom).toBeLessThan(1000)
    }
  })

  it('a new forming bar (previous one closed) is one push', () => {
    const bars = randomWalk(80, 6)
    const layer = new SessionRangesLayer()
    syncBars(layer, [config('a')], bars.slice(0, 79))
    syncBars(layer, [config('a')], bars)
    expect(layer.statsOf('a')).toEqual({ recomputes: 1, pushed: 79, count: 79 })
  })

  it('perturbing the bars after closedThrough in the synced array changes no result (incremental or fresh layer)', () => {
    const bars = randomWalk(24 * 4 + 5, 14)
    const cut = 24 * 3 + 2 // last closed index: inside a session, so a later bar would join a box
    const closedThrough = bars[cut].time
    const layer = new SessionRangesLayer()
    layer.sync([config('a')], bars, closedThrough, H1, DEFS)
    const before = structuredClone(layer.resultOf('a')) as SessionRanges
    const stats = layer.statsOf('a')
    expect(before).toEqual(computeSessionRanges(bars.slice(0, cut + 1), P))
    const rnd = mulberry32(99)
    for (let k = 0; k < 5; k++) {
      // Same objects through closedThrough, new extreme bars after it.
      const perturbed = bars.map((b, i) =>
        i <= cut
          ? b
          : {
              ...b,
              high: rnd() < 0.5 ? 1e9 : b.high + 1000,
              low: rnd() < 0.5 ? 1e-9 : 0.001,
              close: b.close + 1,
            },
      )
      layer.sync([config('a')], perturbed, closedThrough, H1, DEFS)
      expect(layer.resultOf('a')).toEqual(before)
      expect(layer.statsOf('a')).toEqual(stats)
      // A fresh layer synced once on the perturbed array gives the same boxes.
      const fresh = new SessionRangesLayer()
      fresh.sync([config('a')], perturbed, closedThrough, H1, DEFS)
      expect(fresh.resultOf('a')).toEqual(before)
    }
    // Control: had those bars been counted as closed, the boxes would move.
    const all = new SessionRangesLayer()
    const wild = bars.map((b, i) => (i <= cut ? b : { ...b, high: 1e9 }))
    all.sync([config('a')], wild, wild[wild.length - 1].time, H1, DEFS)
    expect(all.resultOf('a')).not.toEqual(before)
  })

  it('closedThrough null pushes nothing; a time between bars counts the earlier ones', () => {
    const bars = randomWalk(100, 13)
    const layer = new SessionRangesLayer()
    layer.sync([config('a')], bars, null, H1, DEFS)
    expect(layer.statsOf('a')?.count).toBe(0)
    expect(layer.resultOf('a')).toEqual({ boxes: [] })
    layer.sync([config('a')], bars, bars[49].time + 1, H1, DEFS)
    expect(layer.resultOf('a')).toEqual(computeSessionRanges(bars.slice(0, 50), P))
  })
})

describe('SessionRangesLayer: restarts', () => {
  it('closedThrough moving back starts over and matches the shorter full recompute', () => {
    const bars = randomWalk(150, 21)
    const layer = new SessionRangesLayer()
    syncBars(layer, [config('a')], bars, true)
    layer.sync([config('a')], bars, bars[99].time, H1, DEFS)
    expect(layer.statsOf('a')).toEqual({ recomputes: 2, pushed: 250, count: 100 })
    expect(layer.resultOf('a')).toEqual(computeSessionRanges(bars.slice(0, 100), P))
  })

  it('a pushed bar revised in place, or older history prepended, starts over', () => {
    const bars = randomWalk(150, 22)
    const layer = new SessionRangesLayer()
    syncBars(layer, [config('a')], bars)
    const revised = bars.slice()
    revised[40] = { ...bars[40], high: bars[40].high + 50 }
    syncBars(layer, [config('a')], revised)
    expect(layer.statsOf('a')?.recomputes).toBe(2)
    expect(layer.resultOf('a')).toEqual(computeSessionRanges(revised.slice(0, -1), P))
    const older = randomWalk(30, 23).map((c) => ({ ...c, time: c.time - 30 * H1 }))
    const merged = [...older, ...revised]
    syncBars(layer, [config('a')], merged)
    expect(layer.statsOf('a')?.recomputes).toBe(3)
    expect(layer.resultOf('a')).toEqual(computeSessionRanges(merged.slice(0, -1), P))
  })

  it('show toggles change the drawing only: same boxes, no recompute, no push', () => {
    const bars = randomWalk(100, 24)
    const h = fakeHost(bars.map((b) => b.time))
    const layer = new SessionRangesLayer()
    layer.attach(h.host)
    syncBars(layer, [config('a')], bars)
    const p = layer.primitiveOf('a') as SessionRangesPrimitive
    const before = p.view() as SessionView
    const stats = layer.statsOf('a')
    syncBars(layer, [config('a', { showLondon: 0, showOpening: 0 })], bars)
    const after = p.view() as SessionView
    expect(after).not.toBe(before)
    expect(after.boxes).toBe(before.boxes)
    expect(layer.statsOf('a')).toEqual(stats)
    p.updateAllViews()
    const shown = new Set(p.geometry()?.map((g) => g.session))
    expect([...shown].sort()).toEqual(['asia', 'newyork'])
    // Everything off draws nothing; back on draws everything again, still no recompute.
    syncBars(
      layer,
      [config('a', { showAsia: 0, showLondon: 0, showNewYork: 0, showOpening: 0 })],
      bars,
    )
    p.updateAllViews()
    expect(p.geometry()).toEqual([])
    syncBars(layer, [config('a')], bars)
    p.updateAllViews()
    expect(new Set(p.geometry()?.map((g) => g.session)).size).toBe(4)
    expect(layer.statsOf('a')).toEqual(stats)
  })
})

describe('SessionRangesLayer: intervals, visibility, attach', () => {
  it('draws nothing (and computes nothing) on 1d and above; back below 1d recomputes', () => {
    const bars = randomWalk(60, 31)
    const h = fakeHost(bars.map((b) => b.time))
    const layer = new SessionRangesLayer()
    layer.attach(h.host)
    for (const interval of [DAY, 3 * DAY, 7 * DAY]) {
      syncBars(layer, [config('a')], bars, false, interval)
      const p = layer.primitiveOf('a') as SessionRangesPrimitive
      p.updateAllViews()
      expect(p.view()).toBeNull()
      expect(p.geometry()).toBeNull()
      expect(layer.statsOf('a')?.count).toBe(0)
      expect(layer.resultOf('a')).toBeNull()
    }
    syncBars(layer, [config('a')], bars, false, 4 * H1)
    expect(layer.resultOf('a')).toEqual(computeSessionRanges(bars.slice(0, -1), P))
    expect(layer.statsOf('a')?.recomputes).toBe(1)
  })

  it('draws at zOrder bottom (under candles and drawings), never feeds autoscale or hit tests', () => {
    const p = new SessionRangesPrimitive('a', DEFAULT_SESSION_RANGES_COLORS)
    expect(p.paneViews()[0].zOrder?.()).toBe('bottom')
    expect('autoscaleInfo' in p).toBe(false)
    expect('hitTest' in p).toBe(false)
  })

  it('hiding an instance drops its engine, boxes and bars reference; showing it again recomputes', () => {
    const bars = randomWalk(100, 43)
    const h = fakeHost(bars.map((b) => b.time))
    const layer = new SessionRangesLayer()
    layer.attach(h.host)
    syncBars(layer, [config('a')], bars)
    const p = layer.primitiveOf('a') as SessionRangesPrimitive
    expect(p.view()).not.toBeNull()
    expect(layer.statsOf('a')).toMatchObject({ recomputes: 1, count: 99 })
    // Hidden: nothing kept (no engine, no boxes, no view), and later bars are not pushed.
    syncBars(layer, [config('a', {}, false)], bars)
    expect(p.isVisible()).toBe(false)
    expect(p.view()).toBeNull()
    expect(layer.resultOf('a')).toBeNull()
    expect(layer.statsOf('a')).toMatchObject({ recomputes: 1, count: 0 })
    const other = randomWalk(120, 44) // e.g. another symbol while hidden
    syncBars(layer, [config('a', {}, false)], other)
    expect(layer.statsOf('a')).toMatchObject({ recomputes: 1, count: 0, pushed: 99 })
    // Shown again: one fresh recompute over the current bars.
    syncBars(layer, [config('a')], other)
    expect(p.isVisible()).toBe(true)
    expect(layer.statsOf('a')).toEqual({ recomputes: 2, pushed: 99 + 119, count: 119 })
    expect(layer.resultOf('a')).toEqual(computeSessionRanges(other.slice(0, -1), P))
    expect(p.view()?.boxes).toEqual(layer.resultOf('a')?.boxes)
  })

  it('one primitive per instance; hidden draws nothing; moves series; removed detaches', () => {
    const bars = randomWalk(60, 41)
    const times = bars.map((b) => b.time)
    const a = fakeHost(times)
    const b = fakeHost(times)
    const layer = new SessionRangesLayer()
    layer.attach(a.host)
    // A non-session config gets no primitive.
    const ema: IndicatorConfig = { ...config('e'), type: 'ema' }
    syncBars(layer, [config('x'), config('y', {}, false), ema], bars)
    expect(a.attached).toHaveLength(2)
    expect(layer.uids()).toEqual(['x', 'y'])
    const px = layer.primitiveOf('x') as SessionRangesPrimitive
    const py = layer.primitiveOf('y') as SessionRangesPrimitive
    px.updateAllViews()
    py.updateAllViews()
    expect(px.geometry()?.length).toBeGreaterThan(0)
    expect(py.geometry()).toBeNull()
    expect(layer.statsOf('y')?.count).toBe(0) // hidden: not advanced
    // Chart-type switch: detach (alive) then attach to the new series.
    layer.detach(true)
    expect(a.attached).toHaveLength(0)
    layer.attach(b.host)
    expect(b.attached).toEqual([px, py])
    px.updateAllViews()
    expect(px.geometry()?.length).toBeGreaterThan(0)
    // Chart removed: references dropped, nothing drawn.
    layer.detach(false)
    px.updateAllViews()
    expect(px.geometry()).toBeNull()
    // Removing an instance from the configs takes its primitive off.
    layer.attach(a.host)
    syncBars(layer, [config('y', {}, false)], bars)
    expect(a.attached).toEqual([py])
    expect(layer.uids()).toEqual(['y'])
    syncBars(layer, [], bars)
    expect(a.attached).toEqual([])
    expect(layer.uids()).toEqual([])
  })

  it('geometry in the real primitive uses bar coordinates plus half the bar spacing', () => {
    const bars = randomWalk(30, 42)
    const h = fakeHost(bars.map((b) => b.time))
    const layer = new SessionRangesLayer()
    layer.attach(h.host)
    syncBars(layer, [config('x')], bars)
    const p = layer.primitiveOf('x') as SessionRangesPrimitive
    p.updateAllViews()
    const r = layer.resultOf('x') as SessionRanges
    const asia = r.boxes.find((b) => b.session === 'asia') as SessionBox
    const g = p.geometry()?.find((x) => x.session === 'asia')
    const idx = (t: number) => bars.findIndex((c) => c.time === t)
    expect(g?.x0).toBe(idx(asia.firstTime) * PX_PER_BAR - PX_PER_BAR / 2)
    expect(g?.x1).toBe(idx(asia.lastTime) * PX_PER_BAR + PX_PER_BAR / 2)
    expect(g?.top).toBe(1000 - 5 * asia.high)
    expect(g?.bottom).toBe(1000 - 5 * asia.low)
  })
})

describe('SessionRangesLayer: sessions not aligned with the bar length are not drawn', () => {
  const H4 = 4 * H1

  it('sessionDrawn = toggled on AND aligned; sessionsHiddenByAlignment lists the toggled-on rest', () => {
    const on = config('a').inputs
    expect(sessionDrawn(on, H1)).toEqual(ALL_ON)
    expect(sessionDrawn(on, 900)).toEqual(ALL_ON)
    expect(sessionDrawn(on, H4)).toEqual({
      asia: true,
      london: false,
      newyork: false,
      opening: false,
    })
    expect(sessionDrawn(on, 2 * H1)).toEqual({
      asia: true,
      london: false,
      newyork: false,
      opening: false,
    })
    expect(sessionDrawn(on, 12 * H1)).toEqual({
      asia: false,
      london: false,
      newyork: false,
      opening: false,
    })
    expect(sessionDrawn({ ...on, orHours: 4 }, H4).opening).toBe(true)
    expect(sessionDrawn({ ...on, showAsia: 0 }, H4).asia).toBe(false)
    expect(sessionsHiddenByAlignment(on, H1)).toEqual([])
    expect(sessionsHiddenByAlignment(on, H4)).toEqual(['london', 'newyork', 'opening'])
    expect(sessionsHiddenByAlignment({ ...on, showLondon: 0, showOpening: 0 }, H4)).toEqual([
      'newyork',
    ])
    expect(sessionsHiddenByAlignment(on, 12 * H1)).toEqual(['asia', 'london', 'newyork', 'opening'])
    // 1d and above: the layer draws nothing at all, which the hint covers; no alignment list.
    expect(sessionsHiddenByAlignment(on, DAY)).toEqual([])
  })

  it('on 4h only Asia is drawn (OR with N = 4 too); the boxes are still computed for all, no recompute', () => {
    // Real 4h bars open on the epoch 4h grid (T0 is 18:00; +2h = 20:00).
    const bars = randomWalk(6 * 8 + 3, 51, H4).map((c) => ({ ...c, time: c.time + 2 * H1 }))
    expect(bars[0].time % H4).toBe(0)
    const times = bars.map((b) => b.time)
    const h = fakeHost(times)
    const layer = new SessionRangesLayer()
    layer.attach(h.host)
    syncBars(layer, [config('a')], bars, false, H4)
    const p = layer.primitiveOf('a') as SessionRangesPrimitive
    p.updateAllViews()
    expect(new Set(p.geometry()?.map((g) => g.session))).toEqual(new Set(['asia']))
    // The compute is unchanged: every session still has boxes.
    const r = layer.resultOf('a') as SessionRanges
    expect(r).toEqual(computeSessionRanges(bars.slice(0, -1), P))
    expect(new Set(r.boxes.map((b) => b.session)).size).toBe(4)
    const stats = layer.statsOf('a')
    // OR N = 4 lines up with 4h bars; it is a param change, so it recomputes once.
    syncBars(layer, [config('a', { orHours: 4 })], bars, false, H4)
    p.updateAllViews()
    expect(new Set(p.geometry()?.map((g) => g.session))).toEqual(new Set(['asia', 'opening']))
    expect(layer.statsOf('a')?.recomputes).toBe((stats?.recomputes ?? 0) + 1)
  })

  it('the bar length alone changes what is drawn, never the boxes (display rule, no push)', () => {
    const bars = randomWalk(24 * 3, 52)
    const h = fakeHost(bars.map((b) => b.time))
    const layer = new SessionRangesLayer()
    layer.attach(h.host)
    syncBars(layer, [config('a')], bars, false, H1)
    const p = layer.primitiveOf('a') as SessionRangesPrimitive
    const v1 = p.view() as SessionView
    const stats = layer.statsOf('a')
    expect(v1.show).toEqual(ALL_ON)
    syncBars(layer, [config('a')], bars, false, 2 * H1)
    const v2 = p.view() as SessionView
    expect(v2).not.toBe(v1)
    expect(v2.boxes).toBe(v1.boxes)
    expect(v2.show).toEqual({ asia: true, london: false, newyork: false, opening: false })
    expect(layer.statsOf('a')).toEqual(stats)
    p.updateAllViews()
    expect(new Set(p.geometry()?.map((g) => g.session))).toEqual(new Set(['asia']))
    // 15m (sub-hour) and 1h draw all four again; the same show is the same view object.
    syncBars(layer, [config('a')], bars, false, 900)
    const v3 = p.view() as SessionView
    expect(v3.show).toEqual(ALL_ON)
    syncBars(layer, [config('a')], bars, false, H1)
    expect(p.view()).toBe(v3)
    expect(layer.statsOf('a')).toEqual(stats)
  })
})
