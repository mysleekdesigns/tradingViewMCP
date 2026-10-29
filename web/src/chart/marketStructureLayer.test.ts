import type { ISeriesPrimitive, Time } from 'lightweight-charts'
import { describe, expect, it, vi } from 'vitest'
import {
  computeMarketStructure,
  marketStructureDef,
  type MarketStructureParams,
} from '../indicators/marketStructure'
import type { IndicatorDef, IndicatorId, MarketStructure } from '../indicators/types'
import type { IndicatorConfig } from '../state/indicators'
import type { Candle } from '../types'
import type { MarkersHandle } from './backtestOverlay'
import {
  DEFAULT_MARKET_STRUCTURE_COLORS,
  IntervalIndex,
  MarketStructureLayer,
  MarketStructurePrimitive,
  RETEST_MARKER_SIZE,
  StructureSpans,
  drawStructure,
  marketStructureColorsFor,
  mergeByTime,
  structureGeometry,
  structureMarkers,
  structureShow,
  type StructureCoords,
  type StructureShow,
  type StructureView,
} from './marketStructureLayer'
import type { ChartMarker } from './seriesMarkers'
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

/** Seeded random walk on a coarse tick grid (ties and closes exactly on a level happen often). */
function randomWalk(n: number, seed: number, tick = 0.5): Candle[] {
  const rnd = mulberry32(seed)
  const q = (v: number) => Math.round(v / tick) * tick
  const out: Candle[] = []
  let prev = 100
  for (let i = 0; i < n; i++) {
    const open = prev
    const close = Math.max(tick, q(open + (rnd() - 0.5) * 6))
    const high = q(Math.max(open, close) + rnd() * 2)
    const low = Math.max(0, q(Math.min(open, close) - rnd() * 2))
    out.push({ time: T0 + i * H4, open, high, low, close, volume: 1 + rnd() })
    prev = close
  }
  return out
}

const SMALL: MarketStructureParams = { left: 2, right: 2, maxZones: 3 }
const ALL_ON: StructureShow = { swings: true, bos: true, choch: true, zones: true }

function config(
  uid: string,
  params: MarketStructureParams = SMALL,
  extra: Partial<Record<string, number>> = {},
  visible = true,
): IndicatorConfig {
  return {
    uid,
    type: 'mstructure',
    inputs: {
      left: params.left,
      right: params.right,
      maxZones: params.maxZones,
      showSwings: 1,
      showBos: 1,
      showChoch: 1,
      showZones: 1,
      ...extra,
    } as Record<string, number>,
    source: 'close',
    colors: {},
    visible,
  }
}

/** A markers handle that records what it was given. */
function recordingHandle() {
  const calls: { kind: 'set' | 'detach'; markers: ChartMarker[] }[] = []
  const handle: MarkersHandle = {
    setMarkers: (markers) => calls.push({ kind: 'set', markers }),
    detach: () => calls.push({ kind: 'detach', markers: [] }),
  }
  return { handle, calls }
}

/**
 * The `closedThrough` a chart passes for plotted `bars`: the last bar when every bar is closed
 * (replay), else the one before it (live: the last bar is forming).
 */
function through(bars: readonly Candle[], lastClosed: boolean): number | null {
  const i = lastClosed ? bars.length - 1 : bars.length - 2
  return i >= 0 ? bars[i].time : null
}

/** `layer.sync` with the closedThrough of a live feed (`lastClosed` false) or of replay. */
function syncBars(
  layer: MarketStructureLayer,
  configs: readonly IndicatorConfig[],
  candles: readonly Candle[],
  lastClosed: boolean,
  defs?: ReadonlyMap<IndicatorId, IndicatorDef>,
): void {
  layer.sync(configs, candles, through(candles, lastClosed), defs)
}

/** Live feed: the first `i` bars plotted, the last of them still forming. */
function live(bars: readonly Candle[], i: number): Candle[] {
  return bars.slice(0, i)
}

/** Expected drawing end of swing i: broken bar, else the next same-kind confirmation, else ∞. */
function expectedSwingEnd(r: MarketStructure, i: number): number {
  const s = r.swings[i]
  if (s.brokenTime !== null) return s.brokenTime
  for (let k = i + 1; k < r.swings.length; k++)
    if (r.swings[k].kind === s.kind) return r.swings[k].confirmTime
  return Number.POSITIVE_INFINITY
}

function expectSpansMatch(spans: StructureSpans, r: MarketStructure): void {
  expect(spans.swings.length).toBe(r.swings.length)
  expect(spans.zones.length).toBe(r.zones.length)
  for (let i = 0; i < r.swings.length; i++) expect(spans.swings.end(i)).toBe(expectedSwingEnd(r, i))
  for (let i = 0; i < r.zones.length; i++)
    expect(spans.zones.end(i)).toBe(r.zones[i].endTime ?? Number.POSITIVE_INFINITY)
}

const COLORS = DEFAULT_MARKET_STRUCTURE_COLORS

/* ------------------------------------------------------------------------------------------ */
/* Markers                                                                                     */
/* ------------------------------------------------------------------------------------------ */

describe('structureMarkers', () => {
  const r = computeMarketStructure(randomWalk(1500, 3), SMALL)

  it('random data exercises BOS, ChoCh and retests in both directions', () => {
    const ms = structureMarkers(r, ALL_ON, COLORS)
    for (const text of ['BOS', 'ChoCh', 'R']) {
      expect(ms.some((m) => m.text === text && m.position === 'belowBar')).toBe(true)
      expect(ms.some((m) => m.text === text && m.position === 'aboveBar')).toBe(true)
    }
  })

  it('bullish events are up arrows below the bar, bearish ones down arrows above, in theme colours', () => {
    const ms = structureMarkers(r, ALL_ON, COLORS).filter((m) => m.text !== 'R')
    expect(ms).toHaveLength(r.events.length)
    ms.forEach((m, k) => {
      const e = r.events[k]
      expect(m.time).toBe(e.time)
      expect(m.text).toBe(e.kind === 'bos' ? 'BOS' : 'ChoCh')
      if (e.direction === 'up') {
        expect([m.position, m.shape, m.color]).toEqual(['belowBar', 'arrowUp', COLORS.up])
      } else {
        expect([m.position, m.shape, m.color]).toEqual(['aboveBar', 'arrowDown', COLORS.down])
      }
    })
  })

  it('retest markers are small circles on the retest bar, support below, resistance above', () => {
    const ms = structureMarkers(r, ALL_ON, COLORS).filter((m) => m.text === 'R')
    const retested = r.zones.filter((z) => z.retestTime !== null)
    expect(ms).toHaveLength(retested.length)
    for (const m of ms) {
      expect(m.shape).toBe('circle')
      expect(m.size).toBe(RETEST_MARKER_SIZE)
      const z = retested.find(
        (x) => x.retestTime === m.time && (x.side === 'support') === (m.position === 'belowBar'),
      )
      expect(z).toBeDefined()
      expect(m.color).toBe(z?.side === 'support' ? COLORS.up : COLORS.down)
    }
  })

  it('is sorted by time', () => {
    const ms = structureMarkers(r, ALL_ON, COLORS)
    for (let i = 1; i < ms.length; i++) expect(ms[i].time).toBeGreaterThanOrEqual(ms[i - 1].time)
  })

  it('toggles filter BOS, ChoCh and retests independently', () => {
    const noBos = structureMarkers(r, { ...ALL_ON, bos: false }, COLORS)
    expect(noBos.some((m) => m.text === 'BOS')).toBe(false)
    expect(noBos.some((m) => m.text === 'ChoCh')).toBe(true)
    const noChoch = structureMarkers(r, { ...ALL_ON, choch: false }, COLORS)
    expect(noChoch.some((m) => m.text === 'ChoCh')).toBe(false)
    expect(noChoch.some((m) => m.text === 'BOS')).toBe(true)
    const noZones = structureMarkers(r, { ...ALL_ON, zones: false }, COLORS)
    expect(noZones.some((m) => m.text === 'R')).toBe(false)
    expect(
      structureMarkers(r, { swings: true, bos: false, choch: false, zones: false }, COLORS),
    ).toEqual([])
  })

  it('mergeByTime is a stable merge', () => {
    const mk = (time: number, text: string): ChartMarker =>
      ({ time, text, position: 'aboveBar', shape: 'circle', color: '#000' }) as ChartMarker
    const out = mergeByTime([
      [mk(1, 'a1'), mk(3, 'a3')],
      [],
      [mk(1, 'b1'), mk(2, 'b2'), mk(3, 'b3')],
    ])
    expect(out.map((m) => m.text)).toEqual(['a1', 'b1', 'b2', 'a3', 'b3'])
  })
})

describe('no marker is earlier than its confirmation bar', () => {
  it('holds for every marker the layer publishes, at every step of a live feed (random data)', () => {
    for (let seed = 1; seed <= 4; seed++) {
      const bars = randomWalk(500, 40 + seed)
      for (const params of [
        SMALL,
        { left: 5, right: 5, maxZones: 10 },
        { left: 1, right: 3, maxZones: 1 },
      ]) {
        const layer = new MarketStructureLayer()
        const cfg = [config('a', params)]
        for (let i = 1; i <= bars.length; i += 7) {
          const candles = live(bars, i)
          syncBars(layer, cfg, candles, false)
          const r = layer.resultOf('a') as MarketStructure
          const formingTime = candles[candles.length - 1].time
          for (const m of layer.markers()) {
            // Only closed bars carry markers: never the forming one.
            expect(m.time).toBeLessThan(formingTime)
            if (m.text === 'R') {
              const z = r.zones.find((x) => x.retestTime === m.time)
              expect(z).toBeDefined()
              expect(m.time).toBeGreaterThan(z?.createdTime ?? Infinity)
            } else {
              const up = m.position === 'belowBar'
              const e = r.events.find((x) => x.time === m.time && (x.direction === 'up') === up)
              expect(e).toBeDefined()
              const s = r.swings.find(
                (x) => x.pivotTime === e?.swingPivotTime && x.kind === (up ? 'high' : 'low'),
              )
              expect(s).toBeDefined()
              // The breaking bar comes after the bar that confirmed the swing.
              expect(m.time).toBeGreaterThan(s?.confirmTime ?? Infinity)
              expect(s?.confirmTime).toBe((s?.pivotTime ?? 0) + params.right * H4)
            }
          }
        }
      }
    }
  })
})

/* ------------------------------------------------------------------------------------------ */
/* Incremental vs full                                                                         */
/* ------------------------------------------------------------------------------------------ */

describe('MarketStructureLayer: live updates', () => {
  it('incremental sync on each closed bar deep-equals a full recompute at every step (live)', () => {
    for (const seed of [7, 8]) {
      const bars = randomWalk(400, seed)
      const layer = new MarketStructureLayer()
      const cfg = [config('a')]
      for (let i = 1; i <= bars.length; i++) {
        syncBars(layer, cfg, live(bars, i), false)
        const full = computeMarketStructure(bars.slice(0, i - 1), SMALL)
        expect(layer.resultOf('a')).toEqual(full)
        expect(layer.markers()).toEqual(structureMarkers(full, ALL_ON, COLORS))
        const view = layer.primitiveOf('a')?.view() as StructureView
        expectSpansMatch(view.spans, full)
      }
      // One full computation at the start, then one push per closed bar.
      expect(layer.statsOf('a')).toEqual({ recomputes: 1, pushed: 399, count: 399 })
    }
  })

  it('replay (every bar closed) also matches the full recompute at every step', () => {
    const bars = randomWalk(300, 11)
    const layer = new MarketStructureLayer()
    const cfg = [config('a')]
    for (let i = 1; i <= bars.length; i++) {
      syncBars(layer, cfg, bars.slice(0, i), true)
      expect(layer.resultOf('a')).toEqual(computeMarketStructure(bars.slice(0, i), SMALL))
    }
    expect(layer.statsOf('a')?.recomputes).toBe(1)
  })

  it('several bars arriving at once are pushed in one sync and still match', () => {
    const bars = randomWalk(500, 12)
    const layer = new MarketStructureLayer()
    const cfg = [config('a')]
    for (const i of [50, 51, 120, 121, 333, 500]) {
      syncBars(layer, cfg, live(bars, i), false)
      expect(layer.resultOf('a')).toEqual(computeMarketStructure(bars.slice(0, i - 1), SMALL))
    }
    expect(layer.statsOf('a')).toEqual({ recomputes: 1, pushed: 499, count: 499 })
  })

  it('a tick on the forming bar changes nothing: same markers, same primitive data, no push', () => {
    const bars = randomWalk(300, 5)
    const out = recordingHandle()
    const layer = new MarketStructureLayer(COLORS, out.handle)
    const cfg = [config('a')]
    syncBars(layer, cfg, bars, false)
    const markers = layer.markers()
    const view = layer.primitiveOf('a')?.view()
    const snapshot = structuredClone(layer.resultOf('a'))
    const stats = layer.statsOf('a')
    const calls = out.calls.length
    const forming = bars[bars.length - 1]
    // Extreme ticks: far above every high and far below every low, and back.
    for (const [high, low, close] of [
      [1e6, forming.low, 1e6],
      [forming.high, 0, 0.01],
      [1e6, 0, 5e5],
      [forming.high, forming.low, forming.close],
    ]) {
      const tick = { ...forming, high, low, close }
      syncBars(layer, cfg, [...bars.slice(0, -1), tick], false)
      expect(layer.markers()).toBe(markers)
      expect(layer.primitiveOf('a')?.view()).toBe(view)
      expect(layer.resultOf('a')).toEqual(snapshot)
      expect(layer.statsOf('a')).toEqual(stats)
      expect(out.calls.length).toBe(calls)
      expect(layer.markers().some((m) => m.time === forming.time)).toBe(false)
    }
  })

  it('a closing bar adds its events (and only once it has closed)', () => {
    const bars = randomWalk(600, 9)
    // A bar k whose own close produces an event.
    const full = computeMarketStructure(bars, SMALL)
    const e = full.events[Math.floor(full.events.length / 2)]
    const k = bars.findIndex((b) => b.time === e.time)
    const layer = new MarketStructureLayer()
    const cfg = [config('a')]
    syncBars(layer, cfg, live(bars, k + 1), false)
    expect(layer.markers().some((m) => m.time === e.time)).toBe(false)
    expect(layer.resultOf('a')?.events.some((x) => x.time === e.time)).toBe(false)
    // The next bar's first tick arrives: bar k is closed now.
    syncBars(layer, cfg, live(bars, k + 2), false)
    expect(layer.markers().some((m) => m.time === e.time && m.text !== 'R')).toBe(true)
    expect(layer.resultOf('a')?.events.some((x) => x.time === e.time)).toBe(true)
  })

  it('a params change recomputes from scratch', () => {
    const bars = randomWalk(400, 13)
    const layer = new MarketStructureLayer()
    syncBars(layer, [config('a')], bars, false)
    const other = { left: 4, right: 3, maxZones: 5 }
    syncBars(layer, [config('a', other)], bars, false)
    expect(layer.statsOf('a')?.recomputes).toBe(2)
    expect(layer.resultOf('a')).toEqual(computeMarketStructure(bars.slice(0, -1), other))
  })

  it('older history, replaced history, a changed closed bar and a step back all recompute', () => {
    const bars = randomWalk(500, 14)
    const layer = new MarketStructureLayer()
    const cfg = [config('a')]
    const check = (candles: readonly Candle[], closed: boolean, recomputes: number) => {
      syncBars(layer, cfg, candles, closed)
      expect(layer.statsOf('a')?.recomputes).toBe(recomputes)
      expect(layer.resultOf('a')).toEqual(
        computeMarketStructure(closed ? candles : candles.slice(0, -1), SMALL),
      )
    }
    check(bars.slice(200), false, 1)
    // Older history prepended.
    check(bars.slice(100), false, 2)
    // A closed bar (the last pushed one) revised.
    const revised = bars.slice(100)
    const j = revised.length - 2
    revised[j] = { ...revised[j], close: revised[j].close + 0.5 }
    check(revised, false, 3)
    // Same times, different bars (e.g. another symbol).
    check(
      randomWalk(400, 99).map((b, i) => ({ ...b, time: bars[100 + i].time })),
      false,
      4,
    )
    // Replay step back: fewer closed bars than pushed.
    check(bars.slice(0, 300), true, 5)
    check(bars.slice(0, 250), true, 6)
    // Nothing plotted.
    check([], true, 7)
    expect(layer.markers()).toEqual([])
  })

  it('toggles re-render only: no recompute, markers and primitive data follow', () => {
    const bars = randomWalk(800, 15)
    const layer = new MarketStructureLayer()
    syncBars(layer, [config('a')], bars, false)
    const stats = layer.statsOf('a')
    const result = layer.resultOf('a')
    syncBars(layer, [config('a', SMALL, { showBos: 0 })], bars, false)
    expect(layer.markers().some((m) => m.text === 'BOS')).toBe(false)
    expect(layer.markers().some((m) => m.text === 'ChoCh')).toBe(true)
    syncBars(layer, [config('a', SMALL, { showChoch: 0, showZones: 0 })], bars, false)
    expect(layer.markers().some((m) => m.text === 'ChoCh' || m.text === 'R')).toBe(false)
    expect(layer.primitiveOf('a')?.view()?.show).toEqual({
      swings: true,
      bos: true,
      choch: false,
      zones: false,
    })
    syncBars(layer, [config('a', SMALL, { showSwings: 0 })], bars, false)
    expect(layer.primitiveOf('a')?.view()?.show.swings).toBe(false)
    expect(layer.statsOf('a')).toEqual(stats)
    expect(layer.resultOf('a')).toBe(result)
  })

  it('a hidden instance draws nothing, gives no markers and is not advanced', () => {
    const bars = randomWalk(400, 16)
    const out = recordingHandle()
    const layer = new MarketStructureLayer(COLORS, out.handle)
    syncBars(layer, [config('a'), config('b', SMALL, {}, false)], bars, false)
    expect(layer.primitiveOf('b')?.isVisible()).toBe(false)
    expect(layer.statsOf('b')?.pushed).toBe(0)
    expect(layer.markers()).toEqual(
      structureMarkers(layer.resultOf('a') as MarketStructure, ALL_ON, COLORS),
    )
    // Hide 'a' too: the channel is cleared.
    syncBars(layer, [config('a', SMALL, {}, false), config('b', SMALL, {}, false)], bars, false)
    expect(layer.markers()).toEqual([])
    expect(out.calls[out.calls.length - 1].kind).toBe('detach')
    // Show 'b': it catches up in one pass.
    syncBars(layer, [config('a', SMALL, {}, false), config('b')], bars, false)
    expect(layer.resultOf('b')).toEqual(computeMarketStructure(bars.slice(0, -1), SMALL))
  })

  it('merges the markers of several visible instances, sorted by time', () => {
    const bars = randomWalk(500, 17)
    const out = recordingHandle()
    const layer = new MarketStructureLayer(COLORS, out.handle)
    const p2 = { left: 3, right: 3, maxZones: 4 }
    syncBars(layer, [config('a'), config('b', p2)], bars, false)
    const closed = bars.slice(0, -1)
    const expected = mergeByTime([
      structureMarkers(computeMarketStructure(closed, SMALL), ALL_ON, COLORS),
      structureMarkers(computeMarketStructure(closed, p2), ALL_ON, COLORS),
    ])
    expect(layer.markers()).toEqual(expected)
    const last = out.calls[out.calls.length - 1]
    expect(last.kind).toBe('set')
    expect(last.markers).toEqual(expected)
    // Removing an instance drops its markers and its primitive.
    syncBars(layer, [config('b', p2)], bars, false)
    expect(layer.uids()).toEqual(['b'])
    expect(layer.markers()).toEqual(
      structureMarkers(computeMarketStructure(closed, p2), ALL_ON, COLORS),
    )
  })

  it('ignores indicators without a structure output', () => {
    const smaDef = {
      ...marketStructureDef,
      id: 'sma',
      structure: undefined,
    } as unknown as IndicatorDef
    const defs: ReadonlyMap<IndicatorId, IndicatorDef> = new Map([
      ['mstructure', marketStructureDef],
      ['sma', smaDef],
    ])
    const layer = new MarketStructureLayer()
    syncBars(layer, [{ ...config('s'), type: 'sma' }, config('a')], randomWalk(100, 1), false, defs)
    expect(layer.uids()).toEqual(['a'])
  })

  it('setColors recolours the markers without recomputing', () => {
    const bars = randomWalk(400, 18)
    const out = recordingHandle()
    const layer = new MarketStructureLayer(COLORS, out.handle)
    syncBars(layer, [config('a')], bars, false)
    const light = marketStructureColorsFor(
      paletteFor({ theme: 'light', upColor: '#2962ff', downColor: '#ff9800', timezone: 'UTC' }),
    )
    expect(light.light).toBe(true)
    expect(COLORS.light).toBe(false)
    layer.setColors(light)
    const colors = new Set(layer.markers().map((m) => m.color))
    expect([...colors].sort()).toEqual(['#2962ff', '#ff9800'])
    expect(out.calls[out.calls.length - 1].markers.map((m) => m.color)).toEqual(
      layer.markers().map((m) => m.color),
    )
    expect(layer.statsOf('a')?.recomputes).toBe(1)
  })

  it('100k bars: the first sync finishes quickly and a later closed bar is cheap', () => {
    const bars = randomWalk(100_001, 42, 0.01)
    const layer = new MarketStructureLayer()
    const cfg = [config('a', { left: 5, right: 5, maxZones: 10 })]
    const t0 = performance.now()
    syncBars(layer, cfg, bars.slice(0, 100_000), false)
    const first = performance.now() - t0
    // Loose bound for slow CI machines; a typical run is a small fraction of it.
    expect(first).toBeLessThan(2000)
    const r = layer.resultOf('a') as MarketStructure
    expect(r.swings.length).toBeGreaterThan(1000)
    const t1 = performance.now()
    syncBars(layer, cfg, bars, false)
    expect(performance.now() - t1).toBeLessThan(100)
    expect(layer.statsOf('a')).toEqual({ recomputes: 1, pushed: 100_000, count: 100_000 })
    // A frame over the last 300 bars touches only what is in view.
    const view = layer.primitiveOf('a')?.view() as StructureView
    const coords = coordsFor(bars[bars.length - 300].time, bars[bars.length - 1].time)
    const t2 = performance.now()
    const g = structureGeometry(view, coords)
    expect(performance.now() - t2).toBeLessThan(50)
    expect(g?.lines.length ?? 0).toBeLessThan(200)
    expect(g?.zones.length ?? 0).toBeLessThan(100)
  })
})

/* ------------------------------------------------------------------------------------------ */
/* Spans and the interval index                                                                */
/* ------------------------------------------------------------------------------------------ */

describe('IntervalIndex', () => {
  it('query returns exactly the intersecting intervals (brute force, with lowered ends)', () => {
    const rnd = mulberry32(3)
    const idx = new IntervalIndex()
    const starts: number[] = []
    const ends: number[] = []
    let s = 0
    for (let i = 0; i < 700; i++) {
      s += Math.floor(rnd() * 5)
      const e = rnd() < 0.1 ? Number.POSITIVE_INFINITY : s + Math.floor(rnd() * 60)
      starts.push(s)
      ends.push(e)
      idx.push(s, e)
    }
    for (let k = 0; k < 200; k++) {
      const i = Math.floor(rnd() * ends.length)
      ends[i] = starts[i] + Math.floor(rnd() * 10)
      idx.setEnd(i, ends[i])
    }
    for (let q = 0; q < 300; q++) {
      const from = Math.floor(rnd() * (s + 50)) - 20
      const to = from + Math.floor(rnd() * 80)
      const expected: number[] = []
      for (let i = 0; i < starts.length; i++)
        if (starts[i] <= to && ends[i] >= from) expected.push(i)
      expect(idx.query(from, to)).toEqual(expected)
    }
  })
})

describe('StructureSpans', () => {
  it('built in one pass from a full result, gives each swing and zone its expected end', () => {
    const bars = randomWalk(1200, 21)
    const full = computeMarketStructure(bars, SMALL)
    const once = new StructureSpans()
    once.update(full)
    expectSpansMatch(once, full)
  })
})

/* ------------------------------------------------------------------------------------------ */
/* Geometry, primitive, drawing                                                                */
/* ------------------------------------------------------------------------------------------ */

const PX_PER_BAR = 10

/** x = bar index * 10 px (from T0), y = 1000 - 5 * price; pane 800 px wide. */
function coordsFor(from: number, to: number, width = 800): StructureCoords {
  return {
    visibleRange: () => ({ from, to }),
    xOfTime: (t) => ((t - from) / H4) * PX_PER_BAR,
    yOfPrice: (p) => 1000 - 5 * p,
    width,
  }
}

function viewOf(r: MarketStructure, show: StructureShow = ALL_ON): StructureView {
  const spans = new StructureSpans()
  spans.update(r)
  return { result: r, spans, show, version: 1 }
}

describe('structureGeometry', () => {
  const bars = randomWalk(1000, 31)
  const r = computeMarketStructure(bars, SMALL)
  const from = bars[400].time
  const to = bars[600].time
  const coords = coordsFor(from, to)
  const x = (t: number) => ((t - from) / H4) * PX_PER_BAR
  /** The geometry keeps far ends 2 px outside the 800 px pane. */
  const cx = (v: number) => Math.min(802, Math.max(-2, v))

  it('swing levels start at the confirmation bar, not the pivot bar, and end where they stop', () => {
    const g = structureGeometry(viewOf(r), coords)
    const expected = r.swings
      .map((s, i) => ({ s, end: expectedSwingEnd(r, i) }))
      .filter(({ s, end }) => s.confirmTime <= to && end >= from)
    expect(g?.lines).toHaveLength(expected.length)
    g?.lines.forEach((l, k) => {
      const { s, end } = expected[k]
      expect(l.x0).toBe(cx(x(s.confirmTime)))
      expect(s.confirmTime).toBe(s.pivotTime + SMALL.right * H4)
      expect(l.x1).toBe(end === Number.POSITIVE_INFINITY ? 802 : cx(x(end)))
      expect(l.y).toBe(1000 - 5 * s.price)
      expect(l.state).toBe(
        s.brokenTime !== null
          ? 'broken'
          : end === Number.POSITIVE_INFINITY
            ? 'active'
            : 'superseded',
      )
    })
    expect(g?.lines.some((l) => l.state === 'superseded')).toBe(true)
    expect(g?.lines.some((l) => l.state === 'broken')).toBe(true)
  })

  it('pivot dots only for confirmed swings with the pivot bar in view', () => {
    const g = structureGeometry(viewOf(r), coords)
    const expected = r.swings.filter((s) => s.pivotTime >= from && s.pivotTime <= to)
    expect(g?.dots.map((d) => [d.x, d.kind])).toEqual(expected.map((s) => [x(s.pivotTime), s.kind]))
    // Live: a pivot whose confirmation bar has not closed is not in the result, so no dot.
    // Bars up to one past a pivot: the pivot bar has closed, its confirmation bar has not.
    const pivot = r.swings[Math.floor(r.swings.length * 0.7)]
    const k = bars.findIndex((b) => b.time === pivot.pivotTime) + 2
    const partial = computeMarketStructure(bars.slice(0, k), SMALL)
    const pivotsNotYet = r.swings.filter(
      (s) => s.confirmTime >= bars[k].time && s.pivotTime < bars[k].time,
    )
    expect(pivotsNotYet.length).toBeGreaterThan(0)
    const g2 = structureGeometry(viewOf(partial), coordsFor(bars[k - 50].time, bars[k - 1].time))
    for (const s of pivotsNotYet) {
      expect(
        g2?.dots.some((d) => d.x === ((s.pivotTime - bars[k - 50].time) / H4) * PX_PER_BAR),
      ).toBe(false)
    }
  })

  it('zones span creation to end (or the right edge), with the retest split', () => {
    const g = structureGeometry(viewOf(r), coords)
    const expected = r.zones.filter((z) => z.createdTime <= to && (z.endTime ?? Infinity) >= from)
    expect(g?.zones).toHaveLength(expected.length)
    g?.zones.forEach((zg, k) => {
      const z = expected[k]
      expect(zg.x0).toBe(cx(x(z.createdTime)))
      expect(zg.x1).toBe(z.endTime === null ? 802 : cx(x(z.endTime)))
      expect(zg.top).toBe(1000 - 5 * z.top)
      expect(zg.bottom).toBe(1000 - 5 * z.bottom)
      expect(zg.side).toBe(z.side)
      expect(zg.retestX).toBe(
        z.retestTime === null ? null : Math.min(802, Math.max(-2, x(z.retestTime))),
      )
      expect(zg.active).toBe(z.endTime === null)
    })
  })

  it('toggles: no swings -> no lines or dots; no zones -> no zones', () => {
    const g = structureGeometry(viewOf(r, { ...ALL_ON, swings: false, zones: false }), coords)
    expect(g).toEqual({ zones: [], lines: [], dots: [] })
  })

  it('no visible range -> nothing', () => {
    expect(structureGeometry(viewOf(r), { ...coords, visibleRange: () => null })).toBeNull()
  })
})

function fakeHost(times: readonly number[]) {
  const attached: ISeriesPrimitive<Time>[] = []
  let redraws = 0
  const chart = {
    timeScale: () => ({
      getVisibleRange: () => ({ from: times[0], to: times[times.length - 1] }),
      timeToIndex: (t: number) => {
        const i = times.indexOf(t)
        return i >= 0 ? i : null
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

describe('MarketStructurePrimitive and layer attach', () => {
  it('draws at zOrder bottom (under candles and drawings) and never feeds autoscale', () => {
    const p = new MarketStructurePrimitive('a', COLORS)
    expect(p.paneViews()[0].zOrder?.()).toBe('bottom')
    expect('autoscaleInfo' in p).toBe(false)
    expect('hitTest' in p).toBe(false)
  })

  it('attaches per instance, moves to a new series, and draws only while visible', () => {
    const bars = randomWalk(200, 41)
    const times = bars.map((b) => b.time)
    const a = fakeHost(times)
    const b = fakeHost(times)
    const layer = new MarketStructureLayer()
    layer.attach(a.host)
    syncBars(layer, [config('x'), config('y', SMALL, {}, false)], bars, false)
    expect(a.attached).toHaveLength(2)
    const px = layer.primitiveOf('x') as MarketStructurePrimitive
    px.updateAllViews()
    expect(px.geometry()?.lines.length).toBeGreaterThan(0)
    const py = layer.primitiveOf('y') as MarketStructurePrimitive
    py.updateAllViews()
    expect(py.geometry()).toBeNull()
    // Chart-type switch: detach (alive) then attach to the new series.
    layer.detach(true)
    expect(a.attached).toHaveLength(0)
    layer.attach(b.host)
    expect(b.attached).toHaveLength(2)
    px.updateAllViews()
    expect(px.geometry()?.lines.length).toBeGreaterThan(0)
    // Chart removed: references dropped, nothing drawn.
    layer.detach(false)
    px.updateAllViews()
    expect(px.geometry()).toBeNull()
    // Removing the instance from the configs takes its primitive off.
    layer.attach(a.host)
    syncBars(layer, [config('y', SMALL, {}, false)], bars, false)
    expect(a.attached).toEqual([py])
  })

  it('a forming-bar tick requests no redraw', () => {
    const bars = randomWalk(200, 43)
    const h = fakeHost(bars.map((b) => b.time))
    const layer = new MarketStructureLayer()
    layer.attach(h.host)
    syncBars(layer, [config('x')], bars, false)
    const redraws = h.redraws()
    const f = bars[bars.length - 1]
    syncBars(
      layer,
      [config('x')],
      [...bars.slice(0, -1), { ...f, close: f.close * 3, high: f.high * 3 }],
      false,
    )
    expect(h.redraws()).toBe(redraws)
  })
})

describe('drawStructure', () => {
  function recordingScope(ratio: number) {
    const ctx = {
      globalAlpha: 1,
      fillStyle: '',
      strokeStyle: '',
      lineWidth: 1,
      save: vi.fn(),
      restore: vi.fn(),
      setLineDash: vi.fn(),
      beginPath: vi.fn(),
      moveTo: vi.fn(),
      lineTo: vi.fn(),
      stroke: vi.fn(),
      arc: vi.fn(),
      fill: vi.fn(),
      fills: [] as { style: string; alpha: number; rect: number[] }[],
      fillRect(x: number, y: number, w: number, h: number) {
        this.fills.push({
          style: String(this.fillStyle),
          alpha: this.globalAlpha,
          rect: [x, y, w, h],
        })
      },
    }
    const scope = {
      context: ctx,
      horizontalPixelRatio: ratio,
      verticalPixelRatio: ratio,
    } as unknown as Parameters<typeof drawStructure>[0]
    return { scope, ctx }
  }

  it('fills zones (stronger from the retest), strokes levels, dots pivots; context restored', () => {
    const { scope, ctx } = recordingScope(2)
    drawStructure(
      scope,
      {
        zones: [
          { x0: 10, x1: 100, top: 50, bottom: 60, side: 'support', retestX: 40, active: true },
          { x0: 20, x1: 80, top: 70, bottom: 70, side: 'resistance', retestX: null, active: false },
        ],
        lines: [{ x0: 5, x1: 90, y: 30, state: 'broken', broke: 'up' }],
        dots: [{ x: 3, y: 30, kind: 'high' }],
      },
      COLORS,
    )
    expect(ctx.save).toHaveBeenCalledTimes(1)
    expect(ctx.restore).toHaveBeenCalledTimes(1)
    expect(ctx.fills.map((f) => [f.style, f.rect])).toEqual([
      [COLORS.up, [20, 100, 60, 20]],
      [COLORS.up, [80, 100, 120, 20]],
      // Zero-height zone still gets one device pixel.
      [COLORS.down, [40, 140, 120, 1]],
    ])
    expect(ctx.fills[1].alpha).toBeGreaterThan(ctx.fills[0].alpha)
    for (const f of ctx.fills) for (const v of f.rect) expect(Number.isInteger(v)).toBe(true)
    expect(ctx.arc).toHaveBeenCalledTimes(1)
  })

  it('structureShow reads the 1/0 toggles', () => {
    expect(structureShow({ showSwings: 1, showBos: 0, showChoch: 1, showZones: 0 })).toEqual({
      swings: true,
      bos: false,
      choch: true,
      zones: false,
    })
  })
})

describe('MarketStructureLayer: closedThrough and revised bars', () => {
  it('pushes only bars at or before closedThrough (null pushes nothing)', () => {
    const bars = randomWalk(300, 51)
    const layer = new MarketStructureLayer()
    const cfg = [config('a')]
    layer.sync(cfg, bars, null)
    expect(layer.statsOf('a')?.count).toBe(0)
    expect(layer.markers()).toEqual([])
    // A time between two plotted bars (e.g. a raw bar without trades) counts the earlier ones.
    layer.sync(cfg, bars, bars[199].time + 1)
    expect(layer.resultOf('a')).toEqual(computeMarketStructure(bars.slice(0, 200), SMALL))
    layer.sync(cfg, bars, bars[249].time)
    expect(layer.resultOf('a')).toEqual(computeMarketStructure(bars.slice(0, 250), SMALL))
    expect(layer.statsOf('a')).toEqual({ recomputes: 1, pushed: 250, count: 250 })
  })

  it('a partial bar before later bars is not used; its final values later push incrementally', () => {
    const final = randomWalk(400, 52)
    // Pick a bar k whose PARTIAL values would make a break event that its final values do not.
    const full = computeMarketStructure(final, SMALL)
    const k = final.findIndex(
      (_, i) =>
        i > 50 &&
        i < 390 &&
        !full.events.some((e) => e.time === final[i].time) &&
        computeMarketStructure(
          [...final.slice(0, i), { ...final[i], close: 1e6, high: 1e6 }],
          SMALL,
        ).events.some((e) => e.time === final[i].time),
    )
    expect(k).toBeGreaterThan(0)
    const partial = { ...final[k], close: 1e6, high: 1e6 }
    const layer = new MarketStructureLayer()
    const cfg = [config('a')]
    // Live: bar k forming.
    layer.sync(cfg, [...final.slice(0, k), partial], final[k - 1].time)
    // The socket dropped; a tick for bar k + 3 is appended right after the partial bar k.
    const dropped = [...final.slice(0, k), partial, final[k + 3]]
    layer.sync(cfg, dropped, final[k - 1].time)
    expect(layer.resultOf('a')).toEqual(computeMarketStructure(final.slice(0, k), SMALL))
    expect(layer.markers().some((m) => m.time === final[k].time)).toBe(false)
    // (Pushing every bar but the last, the old rule, would have drawn an event at bar k.)
    expect(
      computeMarketStructure(dropped.slice(0, -1), SMALL).events.some(
        (e) => e.time === final[k].time,
      ),
    ).toBe(true)
    // The gap fill lands: k .. k + 2 are final now, k + 3 is forming.
    const filled = [...final.slice(0, k), ...final.slice(k, k + 4)]
    layer.sync(cfg, filled, final[k + 2].time)
    expect(layer.resultOf('a')).toEqual(computeMarketStructure(final.slice(0, k + 3), SMALL))
    expect(layer.statsOf('a')).toEqual({ recomputes: 1, pushed: k + 3, count: k + 3 })
  })

  it('a bar revised in place in the middle of the pushed history recomputes', () => {
    const bars = randomWalk(300, 53)
    const layer = new MarketStructureLayer()
    const cfg = [config('a')]
    syncBars(layer, cfg, bars, false)
    const n = bars.length - 1 // pushed
    // Same times, a new object for bars[n - 3] (not the last pushed bar), e.g. a gap-fill merge.
    const revised = bars.slice()
    revised[n - 3] = { ...revised[n - 3], close: revised[n - 3].close + 7, high: 1e6 }
    syncBars(layer, cfg, revised, false)
    expect(layer.statsOf('a')?.recomputes).toBe(2)
    expect(layer.resultOf('a')).toEqual(computeMarketStructure(revised.slice(0, n), SMALL))
    expect(layer.markers()).toEqual(
      structureMarkers(computeMarketStructure(revised.slice(0, n), SMALL), ALL_ON, COLORS),
    )
  })

  it('closedThrough moving back over the same bars array rolls back by recomputing', () => {
    // E.g. a provisional close or a hole cap: the same plotted array, fewer final bars.
    const bars = randomWalk(300, 57)
    const layer = new MarketStructureLayer()
    const cfg = [config('a')]
    layer.sync(cfg, bars, bars[249].time)
    expect(layer.statsOf('a')).toEqual({ recomputes: 1, pushed: 250, count: 250 })
    layer.sync(cfg, bars, bars[199].time)
    // The bars array (and its prefix) is unchanged, but bars 200..249 are no longer final.
    expect(layer.statsOf('a')).toEqual({ recomputes: 2, pushed: 450, count: 200 })
    expect(layer.resultOf('a')).toEqual(computeMarketStructure(bars.slice(0, 200), SMALL))
    expect(layer.markers()).toEqual(
      structureMarkers(computeMarketStructure(bars.slice(0, 200), SMALL), ALL_ON, COLORS),
    )
    // Moving forward again is incremental.
    layer.sync(cfg, bars, bars[259].time)
    expect(layer.statsOf('a')).toEqual({ recomputes: 2, pushed: 510, count: 260 })
    expect(layer.resultOf('a')).toEqual(computeMarketStructure(bars.slice(0, 260), SMALL))
    // null: nothing is final, so the structure is empty.
    layer.sync(cfg, bars, null)
    expect(layer.statsOf('a')?.count).toBe(0)
    expect(layer.markers()).toEqual([])
  })

  it('a new array with the same bar objects does not recompute', () => {
    const bars = randomWalk(300, 54)
    const layer = new MarketStructureLayer()
    const cfg = [config('a')]
    syncBars(layer, cfg, bars, false)
    syncBars(layer, cfg, bars.slice(), false)
    syncBars(layer, cfg, [...bars, randomWalk(301, 54)[300]], false)
    expect(layer.statsOf('a')).toEqual({ recomputes: 1, pushed: 300, count: 300 })
  })

  it('a hidden instance shown again recomputes if a bar it pushed changed meanwhile', () => {
    const bars = randomWalk(300, 55)
    const layer = new MarketStructureLayer()
    syncBars(layer, [config('a')], bars, false)
    syncBars(layer, [config('a', SMALL, {}, false)], bars, false)
    const revised = bars.slice()
    revised[10] = { ...revised[10], low: 0 }
    syncBars(layer, [config('a', SMALL, {}, false)], revised, false)
    syncBars(layer, [config('a')], revised, false)
    expect(layer.statsOf('a')?.recomputes).toBe(2)
    expect(layer.resultOf('a')).toEqual(computeMarketStructure(revised.slice(0, -1), SMALL))
  })

  it('a forming-bar tick with several instances merges and publishes nothing', () => {
    const bars = randomWalk(500, 56)
    const out = recordingHandle()
    const layer = new MarketStructureLayer(COLORS, out.handle)
    const cfg = [config('a'), config('b', { left: 3, right: 3, maxZones: 4 })]
    syncBars(layer, cfg, bars, false)
    const merges = layer.mergeCount()
    const calls = out.calls.length
    const markers = layer.markers()
    expect(merges).toBe(1)
    const f = bars[bars.length - 1]
    for (const close of [f.close + 1, f.close - 1, f.close]) {
      syncBars(layer, cfg, [...bars.slice(0, -1), { ...f, close }], false)
    }
    expect(layer.mergeCount()).toBe(merges)
    expect(out.calls.length).toBe(calls)
    expect(layer.markers()).toBe(markers)
    // A closed bar that adds no marker re-merges at most once per changed list, and only
    // republishes when the merged list changed.
    syncBars(layer, cfg, [...bars, { ...f, time: f.time + H4 }], false)
    expect(layer.mergeCount()).toBeLessThanOrEqual(merges + 1)
  })
})
