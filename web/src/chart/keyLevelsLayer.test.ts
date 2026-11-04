import { createElement } from 'react'
import type { ISeriesPrimitive, Time } from 'lightweight-charts'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import {
  computeKeyLevels,
  keyLevelsDef,
  keyLevelsParams,
  type KeyLevelsParams,
} from '../indicators/keyLevels'
import { sessionRangesDef } from '../indicators/sessionRanges'
import type { IndicatorDef, IndicatorId, KeyLevels, KeyLevelStep } from '../indicators/types'
import type { IndicatorConfig } from '../state/indicators'
import type { Candle } from '../types'
import { IndicatorLegendRows } from './IndicatorLegend'
import {
  KEY_LEVELS_FAILED_NOTE,
  KEY_LEVELS_HINT,
  SESSION_DAILY_NOTE,
  buildLegendRow,
  keyLevelsLegendValues,
  type KeyLevelsLegendSource,
} from './indicatorSeries'
import {
  DEFAULT_KEY_LEVELS_COLORS,
  KeyLevelsLayer,
  KeyLevelsPrimitive,
  LABEL_HEIGHT_PX,
  drawKeyLevelLabels,
  drawKeyLevelLines,
  groupThousands,
  keyLevelGeometry,
  keyLevelsAt,
  keyLevelsColorsFor,
  keyLevelsDailyEnabled,
  keyLevelsShow,
  spreadLabels,
  type KeyLevelCoords,
  type KeyLevelLabel,
  type KeyLevelsGeometry,
  type KeyLevelsShow,
  type KeyLevelsView,
} from './keyLevelsLayer'
import type { BitmapScope } from './layers/base'
import { bitmapScope, canvasRecording } from './layerFrame.testkit'
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
/** 2023-11-15 00:00 UTC, a Wednesday. */
const D0 = 1_700_006_400
const PX_PER_BAR = 10

function walk(n: number, start: number, step: number, seed: number): Candle[] {
  const rnd = mulberry32(seed)
  const out: Candle[] = []
  let prev = 100
  for (let i = 0; i < n; i++) {
    const open = prev
    const close = Math.max(1, open + (rnd() - 0.5) * 4)
    const high = Math.max(open, close) + rnd() * 2
    const low = Math.max(0.5, Math.min(open, close) - rnd() * 2)
    out.push({ time: start + i * step, open, high, low, close, volume: 1 })
    prev = close
  }
  return out
}

/** 90 closed days ending the day before D0. */
const DAILY = walk(90, D0 - 90 * DAY, DAY, 3)
/** 1h bars from D0, forming last. */
const hourly = (n: number, seed = 7): Candle[] => walk(n, D0, H1, seed)

const DEFS: ReadonlyMap<IndicatorId, IndicatorDef> = new Map<IndicatorId, IndicatorDef>([
  ['keylevels', keyLevelsDef],
  ['sessions', sessionRangesDef],
])

function config(
  uid: string,
  extra: Partial<Record<string, number>> = {},
  visible = true,
): IndicatorConfig {
  return {
    uid,
    type: 'keylevels',
    inputs: {
      atrPeriod: 14,
      smaPeriod: 50,
      showDaily: 1,
      showWeekly: 0,
      showRange: 1,
      showBias: 1,
      ...extra,
    } as Record<string, number>,
    source: 'close',
    colors: {},
    visible,
  }
}

const P: KeyLevelsParams = { atrPeriod: 14, smaPeriod: 50, showWeekly: false }

/** closedThrough of a live feed: the last plotted bar is forming. */
function liveThrough(bars: readonly Candle[]): number | null {
  return bars.length > 1 ? bars[bars.length - 2].time : null
}

function syncLive(
  layer: KeyLevelsLayer,
  configs: readonly IndicatorConfig[],
  bars: readonly Candle[],
  daily: readonly Candle[] | null = DAILY,
  interval = H1,
): void {
  layer.sync(configs, bars, liveThrough(bars), interval, daily, null, DEFS)
}

/** What a full recompute gives for the live state of `bars`. */
function fullLive(bars: readonly Candle[], params = P): KeyLevels {
  return computeKeyLevels(DAILY, bars.slice(0, -1), bars[bars.length - 1].time, params)
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
      width: () => 2000,
    }),
  }
  const series = {
    priceToCoordinate: (p: number) => 1000 - 5 * p,
    priceFormatter: () => ({ format: (p: number) => p.toFixed(2) }),
  }
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

/** 1h bars are `PX_PER_BAR` apart from `first`; y = 1000 - 5 * price; an 800 px pane. */
function coordsFor(from: number, to: number, first = from): KeyLevelCoords {
  return {
    visibleRange: () => ({ from, to }),
    xOfTime: (t) => ((t - first) / H1) * PX_PER_BAR,
    yOfPrice: (p) => 1000 - 5 * p,
    barSpacing: PX_PER_BAR,
    width: 800,
  }
}

const ON: KeyLevelsShow = { daily: true, weekly: true, range: true, bias: true }

function bar(time: number): Candle {
  return { time, open: 1, high: 1, low: 1, close: 1, volume: 1 }
}

function step(key: KeyLevelStep['key'], start: number, price: number, days = 1): KeyLevelStep {
  return { key, start, end: start + days * DAY, price }
}

function view(
  steps: KeyLevelStep[],
  bars: readonly Candle[],
  show: KeyLevelsShow = ON,
): KeyLevelsView {
  return { steps, bars, show, version: 1 }
}

const fmt = (p: number) => p.toFixed(2)
const scopeOf = (ctx: CanvasRenderingContext2D, ratio = 1) =>
  bitmapScope(ctx, ratio) as unknown as BitmapScope
const barsOver = (from: number, n: number) =>
  Array.from({ length: n }, (_, i) => bar(from + i * H1))

/* ------------------------------------------------------------------------------------------ */
/* Geometry                                                                                    */
/* ------------------------------------------------------------------------------------------ */

describe('keyLevelGeometry', () => {
  const bars = barsOver(D0, 48)
  const coords = coordsFor(D0, D0 + 47 * H1)

  it('known values: a step runs from its first bar left edge to its last bar right edge', () => {
    const g = keyLevelGeometry(
      view([step('pdh', D0, 110), step('pdl', D0, 100), step('pdh', D0 + DAY, 120)], bars),
      coords,
      fmt,
    )
    expect(g?.segments).toEqual([
      { key: 'pdh', x0: -2, x1: 235, y: 450 }, // x(D0) - 5 = -5 clamps 2 px outside the pane
      { key: 'pdl', x0: -2, x1: 235, y: 500 },
      { key: 'pdh', x0: 235, x1: 475, y: 400 },
    ])
  })

  it('labels the latest visible step of each key, right aligned inside the pane', () => {
    const steps = [
      step('pdh', D0, 110),
      step('pdl', D0, 100),
      step('pdh', D0 + DAY, 120),
      step('pdl', D0 + DAY, 100),
    ]
    const g = keyLevelGeometry(view(steps, bars), coords, fmt)
    expect(g?.labels.map((l) => [l.key, l.text, l.y])).toEqual([
      ['pdh', 'PDH 120.00', 400],
      ['pdl', 'PDL 100.00', 500],
    ])
    for (const l of g?.labels ?? []) {
      expect(l.x).toBeLessThan(800)
      expect(l.x - l.width).toBeGreaterThan(0)
    }
  })

  it('a key with no step in view gets no label; scrolled back, the label is the older step', () => {
    const steps = [step('pdh', D0, 110), step('pdh', D0 + DAY, 120)]
    const g = keyLevelGeometry(view(steps, bars), coordsFor(D0, D0 + 10 * H1), fmt)
    expect(g?.labels.map((l) => l.text)).toEqual(['PDH 110.00'])
    expect(keyLevelGeometry(view([], bars), coords, fmt)?.labels).toEqual([])
  })

  it('culls by binary search: only steps in the visible range read a coordinate', () => {
    const steps: KeyLevelStep[] = []
    for (let i = 0; i < 500; i++) steps.push(step('pdh', D0 - 300 * DAY + i * DAY, 100))
    const many = barsOver(D0 - 300 * DAY, 500 * 24)
    const yOfPrice = vi.fn((p: number) => 1000 - 5 * p)
    const c = { ...coordsFor(D0, D0 + DAY - H1, D0 - 300 * DAY), yOfPrice }
    const g = keyLevelGeometry(view(steps, many), c, fmt)
    expect(g?.segments).toHaveLength(1)
    // One segment and one label: the days around the view were never placed.
    expect(yOfPrice).toHaveBeenCalledTimes(2)
  })

  it('a weekly step that started days before the view still draws', () => {
    const monday = D0 - 2 * DAY
    const week = barsOver(monday, 7 * 24)
    const c = coordsFor(D0 + DAY, D0 + 2 * DAY, monday)
    const g = keyLevelGeometry(view([step('pwh', monday, 130, 7)], week), c, fmt)
    expect(g?.segments).toHaveLength(1)
    expect(g?.labels.map((l) => l.text)).toEqual(['PWH 130.00'])
  })

  it('a missing day stays a gap: nothing bridges it', () => {
    // Day 2 has bars but no daily step; day 3 has a step again.
    const three = barsOver(D0, 72)
    const steps = [step('pdh', D0, 110), step('pdh', D0 + 2 * DAY, 110)]
    const g = keyLevelGeometry(view(steps, three), coordsFor(D0, D0 + 71 * H1), fmt)
    expect(g?.segments.map((s) => [s.x0, s.x1])).toEqual([
      [-2, 235],
      [475, 715],
    ])
  })

  it('labels only a step covering the newest visible bar: an older level is not today’s', () => {
    const two = barsOver(D0, 48)
    const yesterdayOnly = [step('pdh', D0, 110), step('pdl', D0, 90)]
    const g = keyLevelGeometry(view(yesterdayOnly, two), coordsFor(D0, D0 + 47 * H1), fmt)
    expect(g?.segments).toHaveLength(2)
    expect(g?.labels).toEqual([])
    // Scrolled back into that day, the same step is the one in force again.
    const back = keyLevelGeometry(view(yesterdayOnly, two), coordsFor(D0, D0 + 20 * H1), fmt)
    expect(back?.labels.map((l) => l.text)).toEqual(['PDH 110.00', 'PDL 90.00'])
  })

  it('a step whose day has no plotted bar draws nothing', () => {
    const noDay2 = [...barsOver(D0, 24), ...barsOver(D0 + 2 * DAY, 24)]
    const steps = [step('pdh', D0, 110), step('pdh', D0 + DAY, 111), step('pdh', D0 + 2 * DAY, 112)]
    const xOfTime = (t: number) => noDay2.findIndex((b) => b.time === t) * PX_PER_BAR
    const c = { ...coordsFor(D0, D0 + 2 * DAY + 23 * H1), xOfTime }
    const g = keyLevelGeometry(view(steps, noDay2), c, fmt)
    expect(g?.segments.map((s) => s.y)).toEqual([1000 - 550, 1000 - 560])
  })

  it('stops at the newest plotted bar: the current day never runs past it', () => {
    const partial = barsOver(D0, 6)
    const g = keyLevelGeometry(
      view([step('pdh', D0, 110)], partial),
      coordsFor(D0, D0 + 5 * H1),
      fmt,
    )
    expect(g?.segments[0].x1).toBe(55)
  })

  it('each toggle hides its pair, and off draws no label either', () => {
    const steps = [
      step('pdh', D0, 110),
      step('pdl', D0, 100),
      step('pwh', D0, 130, 7),
      step('pwl', D0, 90, 7),
    ]
    const firstDay = coordsFor(D0, D0 + 20 * H1)
    const keys = (show: KeyLevelsShow) => {
      const g = keyLevelGeometry(view(steps, bars, show), firstDay, fmt)
      return [g?.segments.map((s) => s.key), g?.labels.map((l) => l.key).sort()]
    }
    expect(keys({ ...ON, weekly: false })).toEqual([
      ['pdh', 'pdl'],
      ['pdh', 'pdl'],
    ])
    expect(keys({ ...ON, daily: false })).toEqual([
      ['pwh', 'pwl'],
      ['pwh', 'pwl'],
    ])
    expect(keys({ ...ON, daily: false, weekly: false })).toEqual([[], []])
  })

  it('no range is null; a bar without a coordinate is skipped, never drawn to NaN', () => {
    const v = view([step('pdh', D0, 110)], bars)
    expect(keyLevelGeometry(v, { ...coords, visibleRange: () => null }, fmt)).toBeNull()
    expect(keyLevelGeometry(v, { ...coords, xOfTime: () => null }, fmt)?.segments).toEqual([])
    expect(keyLevelGeometry(v, { ...coords, yOfPrice: () => null }, fmt)?.labels).toEqual([])
  })
})

describe('label de-collision', () => {
  const label = (key: KeyLevelLabel['key'], y: number): KeyLevelLabel => ({
    key,
    text: key,
    x: 790,
    y,
    width: 60,
  })

  it('close levels move apart in price order; far ones stay on their line', () => {
    const labels = [label('pdl', 104), label('pdh', 100), label('pwl', 300), label('pwh', 109)]
    spreadLabels(labels)
    expect(labels.map((l) => [l.key, l.y])).toEqual([
      ['pdh', 100],
      ['pdl', 100 + LABEL_HEIGHT_PX],
      ['pwh', 100 + 2 * LABEL_HEIGHT_PX],
      ['pwl', 300],
    ])
  })

  it('every pair of drawn labels clears the plate height, from the geometry too', () => {
    const steps = [step('pdh', D0, 101), step('pdl', D0, 100.5), step('pwh', D0, 100, 7)]
    const g = keyLevelGeometry(view(steps, barsOver(D0, 24)), coordsFor(D0, D0 + 23 * H1), fmt)
    const ys = g?.labels.map((l) => l.y) ?? []
    expect(ys).toHaveLength(3)
    for (let i = 1; i < ys.length; i++) {
      expect(ys[i] - ys[i - 1]).toBeGreaterThanOrEqual(LABEL_HEIGHT_PX)
    }
  })
})

describe('groupThousands', () => {
  it('groups the integer part only', () => {
    expect(groupThousands('81196.01')).toBe('81,196.01')
    expect(groupThousands('1234567')).toBe('1,234,567')
    expect(groupThousands('999.12345678')).toBe('999.12345678')
    expect(groupThousands('0.00012345')).toBe('0.00012345')
    expect(groupThousands('-1234.5')).toBe('-1,234.5')
  })
})

/* ------------------------------------------------------------------------------------------ */
/* Drawing                                                                                     */
/* ------------------------------------------------------------------------------------------ */

describe('drawing', () => {
  const C = DEFAULT_KEY_LEVELS_COLORS
  const geom: KeyLevelsGeometry = {
    segments: [
      { key: 'pdh', x0: 10, x1: 100, y: 50 },
      { key: 'pwh', x0: 10, x1: 100, y: 60 },
      { key: 'pdl', x0: 10, x1: 100, y: 70 },
    ],
    labels: [
      { key: 'pdh', text: 'PDH 1.00', x: 796, y: 50, width: 54 },
      { key: 'pwl', text: 'PWL 2.00', x: 796, y: 80, width: 54 },
    ],
  }

  it('one solid path for daily levels, then one dashed path for weekly ones, state restored', () => {
    const rec = canvasRecording()
    drawKeyLevelLines(scopeOf(rec.ctx, 2), geom, C)
    const strokes = rec.ops.flatMap((o) => (o.op === 'stroke' ? [o] : []))
    expect(strokes).toHaveLength(2)
    const [daily, weekly] = strokes
    expect(daily.path).toEqual([
      ['M', 20, 100],
      ['L', 200, 100],
      ['M', 20, 140],
      ['L', 200, 140],
    ])
    expect(daily.state).toMatchObject({ strokeStyle: C.daily, dash: [], lineWidth: 2 })
    expect(weekly.path).toEqual([
      ['M', 20, 120],
      ['L', 200, 120],
    ])
    expect(weekly.state).toMatchObject({ strokeStyle: C.weekly, dash: [10, 8] })
    expect(rec.finalState()).toEqual(rec.initialState())
  })

  it('labels: a plate behind right-aligned text in the level family colour', () => {
    const rec = canvasRecording()
    drawKeyLevelLabels(scopeOf(rec.ctx), geom, C)
    const texts = rec.ops.flatMap((o) => (o.op === 'fillText' ? [o] : []))
    expect(texts.map((t) => [t.text, t.state.fillStyle])).toEqual([
      ['PDH 1.00', C.daily],
      ['PWL 2.00', C.weekly],
    ])
    expect(texts[0].state).toMatchObject({ textAlign: 'right', textBaseline: 'middle' })
    const plates = rec.ops.flatMap((o) => (o.op === 'fillRect' ? [o] : []))
    expect(plates).toHaveLength(2)
    expect(plates[0].state.fillStyle).toBe(C.plate)
    expect(plates[0].rect[0] + plates[0].rect[2]).toBe(796)
    expect(rec.finalState()).toEqual(rec.initialState())
  })

  it('nothing to draw touches nothing', () => {
    const rec = canvasRecording()
    drawKeyLevelLines(scopeOf(rec.ctx), { segments: [], labels: [] }, C)
    drawKeyLevelLabels(scopeOf(rec.ctx), { segments: [], labels: [] }, C)
    expect(rec.ops).toEqual([])
  })
})

/* ------------------------------------------------------------------------------------------ */
/* Colours                                                                                     */
/* ------------------------------------------------------------------------------------------ */

function channel(v: number): number {
  const s = v / 255
  return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
}

/** WCAG relative luminance of '#rrggbb'. */
function wcag(hex: string): number {
  const v = parseInt(hex.slice(1, 7), 16)
  return (
    0.2126 * channel((v >> 16) & 255) + 0.7152 * channel((v >> 8) & 255) + 0.0722 * channel(v & 255)
  )
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [wcag(a), wcag(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

describe('keyLevelsColorsFor', () => {
  const appearance = { upColor: '#089981', downColor: '#f23645', timezone: 'UTC' }
  const palettes = [
    paletteFor({ theme: 'dark', ...appearance }),
    paletteFor({ theme: 'light', ...appearance }),
  ]

  it('both families read on both themes (3:1 is the WCAG bar for graphics)', () => {
    for (const p of palettes) {
      const c = keyLevelsColorsFor(p)
      expect(contrast(c.daily, p.background)).toBeGreaterThanOrEqual(3)
      expect(contrast(c.weekly, p.background)).toBeGreaterThanOrEqual(3)
      expect(c.plate.startsWith(p.background)).toBe(true)
    }
  })

  it('weekly is secondary to daily, and neither is the up or down colour', () => {
    for (const p of palettes) {
      const c = keyLevelsColorsFor(p)
      expect(c.daily).not.toBe(c.weekly)
      for (const v of [c.daily, c.weekly]) expect([p.up, p.down]).not.toContain(v)
    }
    expect(keyLevelsColorsFor(palettes[0])).toEqual(DEFAULT_KEY_LEVELS_COLORS)
  })

  it('setColors reaches both views; the same colours are a no-op', () => {
    const bars = hourly(30)
    const h = fakeHost(bars.map((b) => b.time))
    const dark = keyLevelsColorsFor(palettes[0])
    const light = keyLevelsColorsFor(palettes[1])
    const layer = new KeyLevelsLayer(dark)
    layer.attach(h.host)
    syncLive(layer, [config('a')], bars.slice(0, 5))
    const p = layer.primitiveOf('a') as KeyLevelsPrimitive
    const redraws = h.redraws()
    layer.setColors({ ...dark })
    expect(h.redraws()).toBe(redraws)
    layer.setColors(light)
    expect(p.colors()).toEqual(light)
    expect(h.redraws()).toBe(redraws + 1)
    const rec = canvasRecording()
    p.updateAllViews()
    const target = {
      useBitmapCoordinateSpace: (f: (s: unknown) => void) => f(bitmapScope(rec.ctx, 1)),
    }
    p.paneViews()[1]
      .renderer()
      ?.draw(target as never)
    const text = rec.ops.find((o) => o.op === 'fillText')
    expect(text).toMatchObject({ state: { fillStyle: light.daily } })
  })
})

/* ------------------------------------------------------------------------------------------ */
/* Layer: closed bars only, incremental                                                        */
/* ------------------------------------------------------------------------------------------ */

describe('KeyLevelsLayer: closed bars only', () => {
  it('bar-by-bar live sync equals a full recompute of the closed bars at every step', () => {
    const bars = hourly(24 * 4 + 5)
    const layer = new KeyLevelsLayer()
    for (let i = 1; i <= bars.length; i++) {
      syncLive(layer, [config('a')], bars.slice(0, i))
      expect(layer.resultOf('a')).toEqual(fullLive(bars.slice(0, i)))
    }
    expect(layer.statsOf('a')).toEqual({
      recomputes: 1,
      pushed: bars.length - 1,
      count: bars.length - 1,
    })
  })

  it('a forming tick pushes nothing and changes no level, view, legend or redraw', () => {
    const bars = hourly(40)
    const h = fakeHost(bars.map((b) => b.time))
    const layer = new KeyLevelsLayer()
    layer.attach(h.host)
    const cfg = [config('a')]
    syncLive(layer, cfg, bars)
    const p = layer.primitiveOf('a') as KeyLevelsPrimitive
    const view0 = p.view()
    const snapshot = structuredClone(layer.resultOf('a'))
    const stats = layer.statsOf('a')
    const legendV = layer.legendVersion()
    const redraws = h.redraws()
    const forming = bars[bars.length - 1]
    for (const [high, low] of [
      [1e6, forming.low],
      [forming.high, 0.0001],
      [1e6, 0.0001],
    ]) {
      const ticked = [...bars.slice(0, -1), { ...forming, high, low }]
      syncLive(layer, cfg, ticked)
      expect(layer.resultOf('a')).toEqual(snapshot)
      expect(p.view()).toBe(view0)
      expect(layer.statsOf('a')).toEqual(stats)
      expect(layer.legendVersion()).toBe(legendV)
      expect(h.redraws()).toBe(redraws)
    }
  })

  it('a new forming bar is one push, and the new view reaches the new bar', () => {
    const bars = hourly(30)
    const h = fakeHost(bars.map((b) => b.time))
    const layer = new KeyLevelsLayer()
    layer.attach(h.host)
    syncLive(layer, [config('a')], bars.slice(0, 29))
    const p = layer.primitiveOf('a') as KeyLevelsPrimitive
    const before = p.view()
    syncLive(layer, [config('a')], bars)
    expect(layer.statsOf('a')).toEqual({ recomputes: 1, pushed: 29, count: 29 })
    expect(p.view()).not.toBe(before)
    expect(p.view()?.bars).toBe(bars)
  })

  it('a new plotted bar with no new closed bar still lengthens the line', () => {
    const bars = hourly(30)
    const layer = new KeyLevelsLayer()
    layer.attach(fakeHost(bars.map((b) => b.time)).host)
    const through = bars[20].time
    layer.sync([config('a')], bars.slice(0, 29), through, H1, DAILY, null, DEFS)
    const p = layer.primitiveOf('a') as KeyLevelsPrimitive
    const before = p.view()
    layer.sync([config('a')], bars, through, H1, DAILY, null, DEFS)
    expect(p.view()).not.toBe(before)
    expect(layer.statsOf('a')?.pushed).toBe(21)
  })

  it('no look-ahead: bars after closedThrough never reach any output', () => {
    const bars = hourly(24 * 3)
    const cut = 24 + 9
    const through = bars[cut].time
    const newest = bars[bars.length - 1].time
    const layer = new KeyLevelsLayer()
    layer.sync([config('a')], bars, through, H1, DAILY, null, DEFS)
    const before = structuredClone(layer.resultOf('a'))
    expect(before).toEqual(computeKeyLevels(DAILY, bars.slice(0, cut + 1), newest, P))
    const rnd = mulberry32(99)
    for (let k = 0; k < 5; k++) {
      const wild = bars.map((b, i) =>
        i <= cut ? b : { ...b, high: b.high + 1e6 * rnd(), low: 1e-6, close: 5 },
      )
      layer.sync([config('a')], wild, through, H1, DAILY, null, DEFS)
      expect(layer.resultOf('a')).toEqual(before)
      const fresh = new KeyLevelsLayer()
      fresh.sync([config('a')], wild, through, H1, DAILY, null, DEFS)
      expect(fresh.resultOf('a')).toEqual(before)
    }
    expect(layer.statsOf('a')?.recomputes).toBe(1)
  })

  it('closedThrough null pushes nothing, and the day still has its levels', () => {
    const bars = hourly(5)
    const layer = new KeyLevelsLayer()
    layer.sync([config('a')], bars, null, H1, DAILY, null, DEFS)
    expect(layer.statsOf('a')?.count).toBe(0)
    expect(layer.resultOf('a')?.steps.map((s) => s.key)).toEqual(['pdh', 'pdl'])
    expect(layer.resultOf('a')?.rangeUsed).toEqual([])
  })
})

describe('KeyLevelsLayer: replay', () => {
  const bars = hourly(24 * 4)
  const cursor = 24 * 2 + 5
  /** Replay runs on history: today's closed daily candles reach past the cursor. */
  const daily = walk(96, D0 - 90 * DAY, DAY, 3)
  const params = { ...P, showWeekly: true }

  /** Replay plots closed bars up to the cursor, and `closedThrough` is the cursor bar. */
  function replayAt(
    layer: KeyLevelsLayer,
    i: number,
    candles: readonly Candle[] = daily,
  ): readonly Candle[] {
    const shown = bars.slice(0, i + 1)
    const cfg = [config('a', { showWeekly: 1 })]
    layer.sync(cfg, shown, shown[i].time, H1, candles, null, DEFS)
    return shown
  }

  it('stops at the cursor: no day, step or range-used point is dated after it', () => {
    const layer = new KeyLevelsLayer()
    const shown = replayAt(layer, cursor)
    const at = bars[cursor].time
    const r = layer.resultOf('a') as KeyLevels
    expect(r.days[r.days.length - 1].day).toBe(D0 + 2 * DAY)
    expect(r.rangeUsed[r.rangeUsed.length - 1].time).toBe(at)
    for (const s of r.steps) expect(s.start).toBeLessThanOrEqual(at)
    expect(r).toEqual(computeKeyLevels(daily, shown, at, params))
  })

  it('nothing is drawn right of the cursor bar, though its day runs on', () => {
    const shown = bars.slice(0, cursor + 1)
    const h = fakeHost(shown.map((b) => b.time))
    const layer = new KeyLevelsLayer()
    layer.attach(h.host)
    replayAt(layer, cursor)
    const p = layer.primitiveOf('a') as KeyLevelsPrimitive
    p.updateAllViews()
    const xs = p.geometry()?.segments.map((g) => g.x1) ?? []
    expect(xs.length).toBeGreaterThan(0)
    expect(Math.max(...xs)).toBe(cursor * PX_PER_BAR + PX_PER_BAR / 2)
  })

  it('no look-ahead: daily candles from the cursor day on never reach any output', () => {
    const base = new KeyLevelsLayer()
    replayAt(base, cursor)
    const before = structuredClone(base.resultOf('a'))
    const rnd = mulberry32(5)
    for (let k = 0; k < 5; k++) {
      const wild = daily.map((c) =>
        c.time < D0 + 2 * DAY
          ? c
          : { ...c, high: 1e6 * (1 + rnd()), low: 1e-6, close: 1e5 * (1 + rnd()) },
      )
      const layer = new KeyLevelsLayer()
      replayAt(layer, cursor, wild)
      expect(layer.resultOf('a')).toEqual(before)
    }
    // Control: the candle before the cursor day is read, so changing it does move the levels.
    const moved = daily.map((c) => (c.time === D0 + DAY ? { ...c, high: 1e6 } : c))
    const control = new KeyLevelsLayer()
    replayAt(control, cursor, moved)
    expect(control.resultOf('a')).not.toEqual(before)
  })

  it('a step forward is one push; a step back starts over and matches the shorter state', () => {
    const layer = new KeyLevelsLayer()
    replayAt(layer, cursor)
    replayAt(layer, cursor + 1)
    expect(layer.statsOf('a')).toMatchObject({ recomputes: 1, count: cursor + 2 })
    const shown = replayAt(layer, cursor - 30)
    expect(layer.statsOf('a')).toMatchObject({ recomputes: 2, count: cursor - 29 })
    expect(layer.resultOf('a')).toEqual(
      computeKeyLevels(daily, shown, shown[shown.length - 1].time, params),
    )
  })
})

describe('KeyLevelsLayer: rebuilds', () => {
  const bars = hourly(60)

  it('the same daily reference, params and bars never recompute', () => {
    const layer = new KeyLevelsLayer()
    syncLive(layer, [config('a')], bars)
    syncLive(layer, [config('a')], bars)
    syncLive(layer, [config('a')], bars.slice())
    expect(layer.statsOf('a')?.recomputes).toBe(1)
  })

  it('a new daily array rebuilds and gives the new levels', () => {
    const layer = new KeyLevelsLayer()
    syncLive(layer, [config('a')], bars)
    const newer = walk(90, D0 - 90 * DAY, DAY, 4)
    syncLive(layer, [config('a')], bars, newer)
    expect(layer.statsOf('a')?.recomputes).toBe(2)
    expect(layer.resultOf('a')).toEqual(
      computeKeyLevels(newer, bars.slice(0, -1), bars[bars.length - 1].time, P),
    )
  })

  it('a changed compute param rebuilds; each draw-only toggle does not', () => {
    const layer = new KeyLevelsLayer()
    syncLive(layer, [config('a')], bars)
    syncLive(layer, [config('a', { showRange: 0, showBias: 0, showDaily: 0 })], bars)
    expect(layer.statsOf('a')?.recomputes).toBe(1)
    syncLive(layer, [config('a', { smaPeriod: 20 })], bars)
    expect(layer.statsOf('a')?.recomputes).toBe(2)
    expect(layer.resultOf('a')).toEqual(fullLive(bars, { ...P, smaPeriod: 20 }))
    syncLive(layer, [config('a', { smaPeriod: 20, showWeekly: 1 })], bars)
    expect(layer.statsOf('a')?.recomputes).toBe(3)
    expect(layer.resultOf('a')).toEqual(fullLive(bars, { ...P, smaPeriod: 20, showWeekly: true }))
  })

  it('a pushed bar that is no longer the same object rebuilds', () => {
    const layer = new KeyLevelsLayer()
    syncLive(layer, [config('a')], bars)
    const revised = bars.slice()
    revised[10] = { ...bars[10], high: bars[10].high + 50 }
    syncLive(layer, [config('a')], revised)
    expect(layer.statsOf('a')?.recomputes).toBe(2)
    expect(layer.resultOf('a')).toEqual(fullLive(revised))
  })

  it('older history prepended rebuilds', () => {
    const layer = new KeyLevelsLayer()
    syncLive(layer, [config('a')], bars)
    const merged = [...walk(30, D0 - 30 * H1, H1, 9), ...bars]
    syncLive(layer, [config('a')], merged)
    expect(layer.statsOf('a')?.recomputes).toBe(2)
    expect(layer.resultOf('a')).toEqual(fullLive(merged))
  })

  it('closedThrough moving back rebuilds', () => {
    const layer = new KeyLevelsLayer()
    syncLive(layer, [config('a')], bars)
    layer.sync([config('a')], bars, bars[20].time, H1, DAILY, null, DEFS)
    expect(layer.statsOf('a')).toMatchObject({ recomputes: 2, count: 21 })
  })

  it('the toggles change the drawing only: same steps, no push', () => {
    const h = fakeHost(bars.map((b) => b.time))
    const layer = new KeyLevelsLayer()
    layer.attach(h.host)
    syncLive(layer, [config('a')], bars)
    const p = layer.primitiveOf('a') as KeyLevelsPrimitive
    const before = p.view() as KeyLevelsView
    const stats = layer.statsOf('a')
    syncLive(layer, [config('a', { showDaily: 0 })], bars)
    const after = p.view() as KeyLevelsView
    expect(after).not.toBe(before)
    expect(after.steps).toBe(before.steps)
    expect(layer.statsOf('a')).toEqual(stats)
    p.updateAllViews()
    expect(p.geometry()).toEqual({ segments: [], labels: [] })
    syncLive(layer, [config('a')], bars)
    p.updateAllViews()
    expect(p.geometry()?.segments.length).toBeGreaterThan(0)
  })
})

describe('KeyLevelsLayer: intervals, daily states, visibility, attach', () => {
  const bars = hourly(40)

  it('draws and computes nothing on 1d and above; back below 1d recomputes', () => {
    const h = fakeHost(bars.map((b) => b.time))
    const layer = new KeyLevelsLayer()
    layer.attach(h.host)
    for (const interval of [DAY, 3 * DAY, 7 * DAY]) {
      syncLive(layer, [config('a')], bars, DAILY, interval)
      const p = layer.primitiveOf('a') as KeyLevelsPrimitive
      p.updateAllViews()
      expect(p.view()).toBeNull()
      expect(p.geometry()).toBeNull()
      expect(layer.resultOf('a')).toBeNull()
      expect(layer.statsOf('a')?.count).toBe(0)
      expect(layer.legendAt('a', bars[0].time)).toBeNull()
    }
    syncLive(layer, [config('a')], bars, DAILY, 4 * H1)
    expect(layer.resultOf('a')).toEqual(fullLive(bars))
  })

  it('loads daily candles only for a visible instance below 1d with a known venue', () => {
    const on = [config('a')]
    expect(keyLevelsDailyEnabled(on, H1, false, DEFS)).toBe(true)
    expect(keyLevelsDailyEnabled(on, DAY - 1, false, DEFS)).toBe(true)
    expect(keyLevelsDailyEnabled(on, DAY, false, DEFS)).toBe(false)
    expect(keyLevelsDailyEnabled(on, 7 * DAY, false, DEFS)).toBe(false)
    expect(keyLevelsDailyEnabled(on, H1, true, DEFS)).toBe(false)
    expect(keyLevelsDailyEnabled([config('a', {}, false)], H1, false, DEFS)).toBe(false)
    expect(keyLevelsDailyEnabled([], H1, false, DEFS)).toBe(false)
    const sessions = { ...config('s'), type: 'sessions' as const }
    expect(keyLevelsDailyEnabled([sessions], H1, false, DEFS)).toBe(false)
    expect(keyLevelsDailyEnabled([sessions, ...on], H1, false, DEFS)).toBe(true)
  })

  it('no daily candles yet: nothing drawn; an empty answer gives a day without levels', () => {
    const layer = new KeyLevelsLayer()
    layer.attach(fakeHost(bars.map((b) => b.time)).host)
    syncLive(layer, [config('a')], bars, null)
    const p = layer.primitiveOf('a') as KeyLevelsPrimitive
    expect(p.view()).toBeNull()
    expect(layer.statsOf('a')?.count).toBe(0)
    expect(layer.legendAt('a', bars[0].time)).toBeNull()
    syncLive(layer, [config('a')], bars, [])
    expect(p.view()?.steps).toEqual([])
    expect(layer.legendAt('a', bars[0].time)).toMatchObject({ rangeUsed: null, bias: null })
  })

  it('an error beside good candles keeps drawing; alone it is flagged, and it clears', () => {
    const layer = new KeyLevelsLayer()
    const cfg = [config('a')]
    const firstDay = bars.slice(0, 5)
    layer.sync(cfg, firstDay, liveThrough(firstDay), H1, DAILY, 'Rate limited', DEFS)
    expect(layer.dailyFailed()).toBe(false)
    expect(layer.resultOf('a')).toEqual(fullLive(firstDay))
    const v = layer.legendVersion()
    layer.sync(cfg, firstDay, liveThrough(firstDay), H1, null, 'Rate limited', DEFS)
    expect(layer.dailyFailed()).toBe(true)
    expect(layer.legendVersion()).not.toBe(v)
    layer.sync(cfg, firstDay, liveThrough(firstDay), H1, null, null, DEFS)
    expect(layer.dailyFailed()).toBe(false)
  })

  it('flags an error when the candles stop short of the newest bar’s previous day', () => {
    const layer = new KeyLevelsLayer()
    const cfg = [config('a')]
    // DAILY ends the day before D0, so a bar of D0 + 1 lacks the D0 candle.
    const nextDay = bars.filter((b) => b.time >= D0).slice(0, 26)
    expect(nextDay[nextDay.length - 1].time).toBeGreaterThanOrEqual(D0 + DAY)
    layer.sync(cfg, nextDay, liveThrough(nextDay), H1, DAILY, 'Offline', DEFS)
    expect(layer.dailyFailed()).toBe(true)
    expect(layer.resultOf('a')).toEqual(fullLive(nextDay))
    const v = layer.legendVersion()
    layer.sync(cfg, nextDay, liveThrough(nextDay), H1, DAILY, null, DEFS)
    expect(layer.dailyFailed()).toBe(false)
    expect(layer.legendVersion()).not.toBe(v)
    // No error: a late day is not a failure, and an empty list is treated like a missing day.
    layer.sync(cfg, nextDay, liveThrough(nextDay), H1, [], null, DEFS)
    expect(layer.dailyFailed()).toBe(false)
    layer.sync(cfg, nextDay, liveThrough(nextDay), H1, [], 'Offline', DEFS)
    expect(layer.dailyFailed()).toBe(true)
  })

  it('hiding an instance drops its engine and data; showing it again recomputes', () => {
    const layer = new KeyLevelsLayer()
    layer.attach(fakeHost(bars.map((b) => b.time)).host)
    syncLive(layer, [config('a')], bars)
    const p = layer.primitiveOf('a') as KeyLevelsPrimitive
    syncLive(layer, [config('a', {}, false)], bars)
    expect(p.isVisible()).toBe(false)
    expect(p.view()).toBeNull()
    expect(layer.resultOf('a')).toBeNull()
    expect(layer.statsOf('a')?.count).toBe(0)
    syncLive(layer, [config('a')], bars)
    expect(layer.statsOf('a')?.recomputes).toBe(2)
    expect(layer.resultOf('a')).toEqual(fullLive(bars))
  })

  it('lines at the bottom, labels at normal; never in autoscale or hit tests', () => {
    const p = new KeyLevelsPrimitive('a', DEFAULT_KEY_LEVELS_COLORS)
    expect(p.paneViews().map((v) => v.zOrder?.())).toEqual(['bottom', 'normal'])
    expect(p.paneViews()).toBe(p.paneViews())
    expect('autoscaleInfo' in p).toBe(false)
    expect('hitTest' in p).toBe(false)
  })

  it('the real primitive places lines on bars and labels with the chart price format', () => {
    const big = hourly(20).map((b) => ({ ...b }))
    const daily = DAILY.map((c, i) =>
      i === DAILY.length - 1 ? { ...c, high: 81_196.01, low: 80_000 } : c,
    )
    const h = fakeHost(big.map((b) => b.time))
    const layer = new KeyLevelsLayer()
    layer.attach(h.host)
    syncLive(layer, [config('a')], big, daily)
    const p = layer.primitiveOf('a') as KeyLevelsPrimitive
    p.updateAllViews()
    expect(p.geometry()?.segments[0]).toEqual({
      key: 'pdh',
      x0: -2,
      x1: 195,
      y: 1000 - 5 * 81_196.01,
    })
    expect(p.geometry()?.labels.map((l) => l.text)).toEqual(['PDH 81,196.01', 'PDL 80,000.00'])
  })

  it('an unchanged view builds no new geometry', () => {
    const h = fakeHost(bars.map((b) => b.time))
    const layer = new KeyLevelsLayer()
    layer.attach(h.host)
    syncLive(layer, [config('a')], bars)
    const p = layer.primitiveOf('a') as KeyLevelsPrimitive
    p.updateAllViews()
    const builds = p.buildCount()
    p.updateAllViews()
    p.updateAllViews()
    expect(p.buildCount()).toBe(builds)
  })

  it('cleans up: detaches primitives, drops references, moves series, sweeps removed uids', () => {
    const times = bars.map((b) => b.time)
    const a = fakeHost(times)
    const b = fakeHost(times)
    const layer = new KeyLevelsLayer()
    layer.attach(a.host)
    const ema: IndicatorConfig = { ...config('e'), type: 'sessions' }
    syncLive(layer, [config('x'), config('y', {}, false), ema], bars)
    expect(a.attached).toHaveLength(2)
    expect(layer.uids()).toEqual(['x', 'y'])
    const px = layer.primitiveOf('x') as KeyLevelsPrimitive
    px.updateAllViews()
    expect(px.geometry()?.segments.length).toBeGreaterThan(0)
    layer.detach(true)
    expect(a.attached).toHaveLength(0)
    layer.attach(b.host)
    expect(b.attached).toHaveLength(2)
    px.updateAllViews()
    expect(px.geometry()?.segments.length).toBeGreaterThan(0)
    // Chart removed: references dropped, nothing drawn by either view.
    layer.detach(false)
    px.updateAllViews()
    expect(px.geometry()).toBeNull()
    const rec = canvasRecording()
    const target = {
      useBitmapCoordinateSpace: (f: (s: unknown) => void) => f(bitmapScope(rec.ctx, 1)),
    }
    for (const v of px.paneViews()) v.renderer()?.draw(target as never)
    expect(rec.ops).toEqual([])
    layer.attach(a.host)
    syncLive(layer, [], bars)
    expect(a.attached).toEqual([])
    expect(layer.uids()).toEqual([])
  })
})

/* ------------------------------------------------------------------------------------------ */
/* Legend                                                                                      */
/* ------------------------------------------------------------------------------------------ */

describe('keyLevelsAt', () => {
  const levels: KeyLevels = {
    steps: [],
    days: [
      { day: D0, atr: 10, sma: 90, bias: 'above' },
      { day: D0 + DAY, atr: 10, sma: 90, bias: 'below' },
      { day: D0 + 2 * DAY, atr: null, sma: null, bias: null },
    ],
    rangeUsed: [
      { time: D0, value: 10 },
      { time: D0 + H1, value: 20 },
      { time: D0 + DAY + H1, value: 30 },
    ],
  }

  it('reads the exact closed bar and that bar day bias', () => {
    expect(keyLevelsAt(levels, D0)).toEqual({ rangeUsed: 10, bias: 'above' })
    expect(keyLevelsAt(levels, D0 + H1)).toEqual({ rangeUsed: 20, bias: 'above' })
  })

  it('a bar past the last closed point reads the newest point of its own day', () => {
    expect(keyLevelsAt(levels, D0 + 5 * H1)).toEqual({ rangeUsed: 20, bias: 'above' })
  })

  it('never carries yesterday over: a day with no closed bar yet has no range used', () => {
    expect(keyLevelsAt(levels, D0 + DAY)).toEqual({ rangeUsed: null, bias: 'below' })
    expect(keyLevelsAt(levels, D0 + 2 * DAY + H1)).toEqual({ rangeUsed: null, bias: null })
  })

  it('before and after every entry is unknown', () => {
    expect(keyLevelsAt(levels, D0 - H1)).toEqual({ rangeUsed: null, bias: null })
    expect(keyLevelsAt(levels, D0 + 9 * DAY)).toEqual({ rangeUsed: null, bias: null })
  })
})

describe('key levels legend', () => {
  const on = keyLevelsShow({})
  const legend = { rangeUsed: 61.6, bias: 'above' as const, smaPeriod: 50, show: on }
  const source = (
    value: ReturnType<KeyLevelsLegendSource['legendAt']>,
    failed = false,
  ): KeyLevelsLegendSource => ({ legendAt: () => value, dailyFailed: () => failed })

  it('range used is a whole percent labelled live; the bias uses the current SMA length', () => {
    expect(keyLevelsLegendValues(legend).map((v) => v.text)).toEqual([
      'Range used 62% · live',
      'Above SMA 50',
    ])
    const below = keyLevelsLegendValues({ ...legend, bias: 'below', smaPeriod: 20 })
    expect(below[1].text).toBe('Below SMA 20')
  })

  it('a dash when unknown', () => {
    expect(
      keyLevelsLegendValues({ ...legend, rangeUsed: null, bias: null }).map((v) => v.text),
    ).toEqual(['Range used — · live', 'SMA 50 —'])
  })

  it('each toggle hides its part; loading shows nothing', () => {
    const off = (over: Partial<KeyLevelsShow>) =>
      keyLevelsLegendValues({ ...legend, show: { ...on, ...over } }).map((v) => v.key)
    expect(off({ range: false })).toEqual(['bias'])
    expect(off({ bias: false })).toEqual(['rangeUsed'])
    expect(off({ range: false, bias: false })).toEqual([])
    expect(keyLevelsLegendValues(null)).toEqual([])
  })

  it('the bias is a neutral chip: the same colour either way, not the up or down colour', () => {
    const above = keyLevelsLegendValues(legend)[1]
    const below = keyLevelsLegendValues({ ...legend, bias: 'below' })[1]
    expect(above.chip).toBe(true)
    expect(above.color).toBe(below.color)
    expect(above.color).not.toMatch(/--up|--down/)
  })

  it('the row shows the values, the visual-only hint, and the bar time goes to the source', () => {
    const legendAt = vi.fn(() => legend)
    const row = buildLegendRow(
      keyLevelsDef,
      config('a'),
      0,
      {},
      {},
      undefined,
      H1,
      { legendAt, dailyFailed: () => false },
      D0,
    )
    expect(legendAt).toHaveBeenCalledWith('a', D0)
    expect(row.values.map((v) => v.text)).toEqual(['Range used 62% · live', 'Above SMA 50'])
    expect(row.hint).toBe(KEY_LEVELS_HINT)
    expect(row.note).toBeUndefined()
    expect(row.title).toBe('KL 14 50')
  })

  it('on 1D and above the row says so; a failed load gets a discreet note', () => {
    const daily = buildLegendRow(keyLevelsDef, config('a'), 0, {}, {}, undefined, DAY, source(null))
    expect(daily.note).toBe(SESSION_DAILY_NOTE)
    expect(daily.values).toEqual([])
    const failed = buildLegendRow(
      keyLevelsDef,
      config('a'),
      0,
      {},
      {},
      undefined,
      H1,
      source(null, true),
    )
    expect(failed.note).toBe(KEY_LEVELS_FAILED_NOTE)
    const loading = buildLegendRow(
      keyLevelsDef,
      config('a'),
      0,
      {},
      {},
      undefined,
      H1,
      source(null),
    )
    expect(loading.note).toBeUndefined()
    expect(loading.values).toEqual([])
  })

  it('the legend component renders the text and marks only the bias as a chip', () => {
    const row = buildLegendRow(
      keyLevelsDef,
      config('a'),
      0,
      {},
      {},
      undefined,
      H1,
      source(legend),
      D0,
    )
    const html = renderToStaticMarkup(createElement(IndicatorLegendRows, { rows: [row] }))
    expect(html).toContain('Range used 62% · live')
    expect(html).toContain('Above SMA 50')
    expect(html.match(/ind-legend__value--chip/g)).toHaveLength(1)
    expect(html).toMatch(/ind-legend__value--chip"[^>]*>Above SMA 50/)
  })

  it('end to end: the layer answers the hovered bar and the latest bar from closed bars', () => {
    // All inside one UTC day: `DAILY` has no candle for D0, so D0 + 1 has no ATR.
    const bars = hourly(20)
    const layer = new KeyLevelsLayer()
    syncLive(layer, [config('a')], bars)
    const r = fullLive(bars)
    const hovered = bars[5].time
    expect(r.rangeUsed.length).toBeGreaterThan(5)
    expect(layer.legendAt('a', hovered)?.rangeUsed).toBe(
      r.rangeUsed.find((p) => p.time === hovered)?.value,
    )
    expect(layer.legendAt('a', hovered)?.bias).toBe(r.days[0].bias)
    expect(layer.legendAt('a', hovered)?.smaPeriod).toBe(keyLevelsParams({}).smaPeriod)
    // The forming bar has no point: the legend shows the newest closed one of its day.
    const latest = layer.legendAt('a', bars[bars.length - 1].time)
    expect(latest?.rangeUsed).toBe(r.rangeUsed[r.rangeUsed.length - 1].value)
  })
})
