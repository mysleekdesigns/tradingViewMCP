import type { ISeriesPrimitive, Logical, Time } from 'lightweight-charts'
import { describe, expect, it, vi } from 'vitest'
import type { IndicatorDef, IndicatorId, IndicatorInputs, VolumeProfile } from '../indicators/types'
import { computeVolumeProfile, volumeProfileDef } from '../indicators/volumeProfile'
import type { IndicatorConfig } from '../state/indicators'
import type { Candle } from '../types'
import {
  DEFAULT_VOLUME_PROFILE_COLORS,
  MIN_PROFILE_BAR_PX,
  OUTSIDE_VALUE_AREA_ALPHA,
  VALUE_AREA_ALPHA,
  VolumeProfileLayer,
  VolumeProfilePrimitive,
  bitmapSpan,
  drawProfile,
  hitProfile,
  inputsKey,
  isValueAreaRow,
  nextProfileCache,
  profileGeometry,
  profileMaxBarLength,
  profileOffscreen,
  rangeOf,
  volumeProfileOpenOps,
  type OpenProfileOps,
  type ProfileCache,
  rowBarLengths,
  volumeProfileColorsFor,
  type ProfileCoords,
  type ProfileGeometry,
} from './volumeProfileLayer'

const T0 = 1_700_000_000
const STEP = 3600

function makeCandles(n: number, start = T0): Candle[] {
  return Array.from({ length: n }, (_, i) => ({
    time: start + i * STEP,
    open: 100 + i,
    high: 102 + i,
    low: 99 + i,
    close: 101 + i,
    volume: 10 + i,
  }))
}

/** Three rows 100..103, the middle one the POC and the value area [101, 103]. */
const PROFILE: VolumeProfile = {
  from: T0 + 2 * STEP,
  to: T0 + 6 * STEP,
  rows: [
    { low: 100, high: 101, volume: 10, upVolume: 10 },
    { low: 101, high: 102, volume: 40, upVolume: 10 },
    { low: 102, high: 103, volume: 20, upVolume: 0 },
  ],
  poc: 101.5,
  vah: 103,
  val: 101,
  totalVolume: 70,
}

/** Bar i at x = 10 i; price p at y = 1100 - 10 p (price 100 -> y 100, 103 -> y 70). */
function coordsFor(times: readonly number[], barSpacing = 10): ProfileCoords {
  return {
    xOfTime: (t) => {
      const i = times.indexOf(t)
      return i < 0 ? null : i * barSpacing
    },
    yOfPrice: (p) => 1100 - 10 * p,
    barSpacing,
  }
}

const TIMES = makeCandles(40).map((c) => c.time)

describe('volumeProfileColorsFor', () => {
  it('uses TradingView blue/amber rows, a red POC and the theme text for the outline', () => {
    expect(volumeProfileColorsFor({ text: '#111111', mutedText: '#222222' })).toEqual({
      up: '#2962ff',
      down: '#fbc02d',
      poc: '#f23645',
      valueArea: '#222222',
      text: '#111111',
    })
  })
})

describe('row scaling', () => {
  it('maps the largest row to the max length and splits up/down by volume share', () => {
    expect(rowBarLengths({ volume: 40, upVolume: 10 }, 40, 120)).toEqual({ up: 30, down: 90 })
    expect(rowBarLengths({ volume: 20, upVolume: 20 }, 40, 120)).toEqual({ up: 60, down: 0 })
    expect(rowBarLengths({ volume: 0, upVolume: 0 }, 40, 120)).toEqual({ up: 0, down: 0 })
    expect(rowBarLengths({ volume: 5, upVolume: 1 }, 0, 120)).toEqual({ up: 0, down: 0 })
  })

  it('caps the longest row at 30% of the range width, never below the minimum', () => {
    expect(profileMaxBarLength(1000)).toBe(300)
    expect(profileMaxBarLength(50)).toBe(MIN_PROFILE_BAR_PX)
    expect(profileMaxBarLength(-5)).toBe(MIN_PROFILE_BAR_PX)
  })
})

describe('isValueAreaRow', () => {
  it('is true for rows within [val, vah] only', () => {
    expect(PROFILE.rows.map((r) => isValueAreaRow(r, PROFILE))).toEqual([false, true, true])
  })

  it('tolerates float rounding at the edges', () => {
    expect(isValueAreaRow({ low: 0.1 + 0.2, high: 0.4 }, { val: 0.3, vah: 0.4 })).toBe(true)
  })
})

describe('profileGeometry', () => {
  it('spans half a bar either side of from/to and maps rows to price coordinates', () => {
    const g = profileGeometry(PROFILE, coordsFor(TIMES))
    expect(g).not.toBeNull()
    if (g === null) return
    // from = bar 2 (x 20), to = bar 6 (x 60), spacing 10.
    expect(g.x0).toBe(15)
    expect(g.x1).toBe(65)
    expect(g.barX).toBe(15)
    expect(g.maxBarLength).toBe(MIN_PROFILE_BAR_PX)
    expect(g.top).toBe(1100 - 1030)
    expect(g.bottom).toBe(1100 - 1000)
    expect(g.rows.map((r) => [r.top, r.bottom])).toEqual([
      [90, 100],
      [80, 90],
      [70, 80],
    ])
    const max = MIN_PROFILE_BAR_PX
    expect(g.rows[1].upLength + g.rows[1].downLength).toBe(max)
    expect(g.rows[0].upLength).toBe(max / 4)
    expect(g.rows[2].downLength).toBe(max / 2)
    expect(g.rows.map((r) => r.inValueArea)).toEqual([false, true, true])
    expect(g.pocY).toBe(1100 - 1015)
    expect(g.vahY).toBe(70)
    expect(g.valY).toBe(90)
  })

  it('places a range that is partly or wholly off-screen, and culls it only when wholly off', () => {
    // x positions of an off-screen range are negative; still a geometry.
    const shifted = coordsFor(TIMES)
    const offLeft: ProfileCoords = { ...shifted, xOfTime: (t) => (shifted.xOfTime(t) ?? 0) - 500 }
    const g = profileGeometry(PROFILE, offLeft)
    expect(g?.x0).toBe(-485)
    expect(g && profileOffscreen(g, 800)).toBe(true)
    const partly: ProfileCoords = { ...shifted, xOfTime: (t) => (shifted.xOfTime(t) ?? 0) - 40 }
    const p = profileGeometry(PROFILE, partly)
    expect(p && profileOffscreen(p, 800)).toBe(false)
    const right: ProfileCoords = { ...shifted, xOfTime: (t) => (shifted.xOfTime(t) ?? 0) + 900 }
    const r = profileGeometry(PROFILE, right)
    expect(r && profileOffscreen(r, 800)).toBe(true)
  })

  it('is null when the chart cannot place the range or a price', () => {
    expect(profileGeometry(PROFILE, { ...coordsFor(TIMES), xOfTime: () => null })).toBeNull()
    expect(profileGeometry(PROFILE, { ...coordsFor(TIMES), yOfPrice: () => null })).toBeNull()
    expect(profileGeometry({ ...PROFILE, rows: [] }, coordsFor(TIMES))).toBeNull()
  })
})

describe('hitProfile', () => {
  const g = profileGeometry(PROFILE, coordsFor(TIMES)) as ProfileGeometry

  it('hits a row bar, not the empty part of the row', () => {
    // Row 1 (y 80..90) is the full max length (48 px from x 15).
    expect(hitProfile(g, 20, 85)).toBe(true)
    expect(hitProfile(g, 15 + MIN_PROFILE_BAR_PX, 85)).toBe(true)
    // Row 0 (y 90..100) is a quarter as long (12 px).
    expect(hitProfile(g, 15 + 12, 95)).toBe(true)
    expect(hitProfile(g, 15 + 30, 95)).toBe(false)
  })

  it('hits the POC line across the range and misses outside the body', () => {
    expect(hitProfile(g, 64, 85 + 2)).toBe(true)
    expect(hitProfile(g, 10, 85)).toBe(false)
    expect(hitProfile(g, 30, 50)).toBe(false)
    expect(hitProfile(g, 30, 120)).toBe(false)
  })
})

describe('bitmapSpan', () => {
  it('rounds both edges so adjacent rows share an edge', () => {
    const a = bitmapSpan(10.2, 20.4, 2)
    const b = bitmapSpan(20.4, 30.6, 2)
    expect(a).toEqual({ position: 20, length: 21 })
    expect(b.position).toBe(a.position + a.length)
  })

  it('leaves a 1 px gap on spans of 3+ px and is never empty', () => {
    expect(bitmapSpan(0, 10, 1, true)).toEqual({ position: 0, length: 9 })
    expect(bitmapSpan(0, 2, 1, true)).toEqual({ position: 0, length: 2 })
    expect(bitmapSpan(5, 5.1, 1, true)).toEqual({ position: 5, length: 1 })
    expect(bitmapSpan(20, 10, 1)).toEqual({ position: 10, length: 10 })
  })
})

/** A def whose profile() always returns PROFILE, with a call counter. */
function countingDef() {
  const profile = vi.fn<(c: readonly Candle[], i: IndicatorInputs) => VolumeProfile | null>(
    () => PROFILE,
  )
  return { def: { profile }, profile }
}

describe('recompute caching', () => {
  const inputs: IndicatorInputs = {
    from: T0 + 5 * STEP,
    to: T0 + 10 * STEP,
    rows: 24,
    valueArea: 70,
  }

  it('keys inputs independent of key order', () => {
    expect(inputsKey({ b: 2, a: 1 })).toBe(inputsKey({ a: 1, b: 2 }))
    expect(inputsKey({ a: 1 })).not.toBe(inputsKey({ a: 2 }))
  })

  it('reuses the profile for the same array, and computes once at first', () => {
    const { def, profile } = countingDef()
    const candles = makeCandles(30)
    const a = nextProfileCache(null, def, inputs, candles)
    expect(a.profile).toBe(PROFILE)
    expect(nextProfileCache(a, def, inputs, candles)).toBe(a)
    expect(profile).toHaveBeenCalledTimes(1)
  })

  it('ignores ticks after the range and older history prepended before it', () => {
    const { def, profile } = countingDef()
    const candles = makeCandles(30)
    const a = nextProfileCache(null, def, inputs, candles)
    const ticked = [...candles.slice(0, -1), { ...candles[29], close: 999, volume: 999 }]
    const b = nextProfileCache(a, def, inputs, ticked)
    expect(b.profile).toBe(a.profile)
    expect(b.candles).toBe(ticked)
    const prepended = [...makeCandles(5, T0 - 5 * STEP), ...candles]
    nextProfileCache(b, def, inputs, prepended)
    expect(profile).toHaveBeenCalledTimes(1)
  })

  it('recomputes when a bar in range, the inputs, or the bar set in range change', () => {
    const { def, profile } = countingDef()
    const candles = makeCandles(30)
    let c = nextProfileCache(null, def, inputs, candles)
    const edited = candles.map((x, i) => (i === 7 ? { ...x, volume: x.volume + 1 } : x))
    c = nextProfileCache(c, def, inputs, edited)
    expect(profile).toHaveBeenCalledTimes(2)
    c = nextProfileCache(c, def, { ...inputs, rows: 12 }, edited)
    expect(profile).toHaveBeenCalledTimes(3)
    nextProfileCache(c, def, { ...inputs, rows: 12 }, edited.slice(0, 8))
    expect(profile).toHaveBeenCalledTimes(4)
  })

  it('a range open to the latest bar recomputes on every tick of that bar', () => {
    const { def, profile } = countingDef()
    const open = { ...inputs, to: 0 }
    const candles = makeCandles(30)
    const a = nextProfileCache(null, def, open, candles)
    const ticked = [...candles.slice(0, -1), { ...candles[29], volume: candles[29].volume + 5 }]
    nextProfileCache(a, def, open, ticked)
    expect(profile).toHaveBeenCalledTimes(2)
  })

  it('has no profile without candles and does not call the def', () => {
    const { def, profile } = countingDef()
    expect(nextProfileCache(null, def, inputs, null).profile).toBeNull()
    expect(nextProfileCache(null, def, inputs, []).profile).toBeNull()
    expect(profile).not.toHaveBeenCalled()
  })

  it('rangeOf covers only the bars in [from, to]', () => {
    const candles = makeCandles(30)
    expect(rangeOf(candles, { from: T0 + 100 * STEP, to: 0 }).count).toBe(0)
    expect(rangeOf(candles, { from: T0 + 5 * STEP, to: T0 + 4 * STEP }).count).toBe(0)
    expect(rangeOf(candles, inputs)).toEqual({ start: 5, count: 6 })
    expect(rangeOf(candles, { ...inputs, to: 0 })).toEqual({ start: 5, count: 25 })
    // A `from` between bars starts at the next bar; `to` between bars ends at the one before.
    expect(rangeOf(candles, { from: T0 + 5 * STEP + 1, to: T0 + 10 * STEP - 1 })).toEqual({
      start: 6,
      count: 4,
    })
  })

  // Reviewer's inputs: 10 bars H101 L99 C100 V10, range over all of them.
  function flatBars(): Candle[] {
    return Array.from({ length: 10 }, (_, i) => ({
      time: T0 + i * STEP,
      open: 100,
      high: 101,
      low: 99,
      close: 100,
      volume: 10,
    }))
  }
  const whole: IndicatorInputs = { from: T0, to: T0 + 9 * STEP, rows: 24, valueArea: 70 }

  it('recomputes when a mid-range high changes (new object, same volume / close sums)', () => {
    const bars = flatBars()
    const a = nextProfileCache(null, volumeProfileDef, whole, bars)
    const edited = bars.map((c, i) => (i === 5 ? { ...c, high: 150 } : c))
    const b = nextProfileCache(a, volumeProfileDef, whole, edited)
    expect(b.profile).not.toBe(a.profile)
    expect(b.profile?.rows.at(-1)?.high).toBe(150)
    expect(b.profile).toEqual(computeVolumeProfile(edited, T0, T0 + 9 * STEP, 24, 70))
  })

  it('recomputes on offsetting volume edits (bar 3 +5, bar 6 -5: same total)', () => {
    const bars = flatBars()
    const { def, profile } = countingDef()
    const a = nextProfileCache(null, def, whole, bars)
    const edited = bars.map((c, i) =>
      i === 3 ? { ...c, volume: 15 } : i === 6 ? { ...c, volume: 5 } : c,
    )
    nextProfileCache(a, def, whole, edited)
    expect(profile).toHaveBeenCalledTimes(2)
    // With the real def the profile is the full recompute of the edited bars.
    const real = nextProfileCache(
      nextProfileCache(null, volumeProfileDef, whole, bars),
      volumeProfileDef,
      whole,
      edited,
    )
    expect(real.profile).toEqual(computeVolumeProfile(edited, T0, T0 + 9 * STEP, 24, 70))
  })

  it('a new array with the same bar objects in range reuses the profile', () => {
    const { def, profile } = countingDef()
    const bars = flatBars()
    const a = nextProfileCache(null, def, whole, bars)
    const copy = [...bars]
    const b = nextProfileCache(a, def, whole, copy)
    expect(b.profile).toBe(a.profile)
    expect(b.candles).toBe(copy)
    expect(profile).toHaveBeenCalledTimes(1)
  })

  it('recomputes when older history loads up to an unloaded `from` without touching the range', () => {
    // `from` between bars, before the first loaded bar: no profile (anchor not loaded).
    const bars = makeCandles(20, T0 + STEP)
    const between: IndicatorInputs = { from: T0 + 30, to: 0, rows: 24, valueArea: 70 }
    const a = nextProfileCache(null, volumeProfileDef, between, bars)
    expect(a.profile).toBeNull()
    // The bar at T0 (< from) loads: the range is unchanged but now known to start at T0 + STEP.
    const grown = [...makeCandles(1, T0), ...bars]
    const b = nextProfileCache(a, volumeProfileDef, between, grown)
    expect(b.profile).not.toBeNull()
    expect(b.profile).toEqual(computeVolumeProfile(grown, T0 + 30, 0, 24, 70))
    expect(b.profile?.from).toBe(T0 + STEP)
  })
})

/* ------------------------------------------------------------------------------------------ */
/* Open-ended range: incremental tick path                                                     */
/* ------------------------------------------------------------------------------------------ */

describe('open-ended range: incremental ticks', () => {
  function mulberry32(seed: number): () => number {
    let a = seed >>> 0
    return () => {
      a = (a + 0x6d2b79f5) >>> 0
      let t = a
      t = Math.imul(t ^ (t >>> 15), t | 1)
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296
    }
  }

  function randomBars(n: number, rnd: () => number): Candle[] {
    const out: Candle[] = []
    let prev = 100
    for (let i = 0; i < n; i++) {
      const open = prev
      const close = Math.max(1, open * (1 + (rnd() - 0.5) * 0.04))
      const high = Math.max(open, close) * (1 + rnd() * 0.01)
      const low = Math.min(open, close) * (1 - rnd() * 0.01)
      out.push({ time: T0 + i * STEP, open, high, low, close, volume: rnd() * 1000 })
      prev = close
    }
    return out
  }

  /** Real ops wrapped with call counters. */
  function countingOps() {
    const calls = { base: 0, fits: 0, extend: 0 }
    const ops: OpenProfileOps = {
      base: (...a) => {
        calls.base++
        return volumeProfileOpenOps.base(...a)
      },
      fits: (...a) => {
        calls.fits++
        return volumeProfileOpenOps.fits(...a)
      },
      extend: (...a) => {
        calls.extend++
        return volumeProfileOpenOps.extend(...a)
      },
    }
    return { ops, calls }
  }

  /** A live tick on the last bar: new object, close moves, high/low widen as needed. */
  function tick(bars: readonly Candle[], rnd: () => number, widen = false): Candle[] {
    const last = bars[bars.length - 1]
    const close = last.close * (1 + (rnd() - 0.5) * (widen ? 0.2 : 0.002))
    const next: Candle = {
      ...last,
      close,
      high: Math.max(last.high, close),
      low: Math.min(last.low, close),
      volume: last.volume + rnd() * 10,
    }
    return [...bars.slice(0, -1), next]
  }

  it('a plain tick inside the span takes the incremental path, not the full one', () => {
    const rnd = mulberry32(11)
    const bars = randomBars(500, rnd)
    // Last bar in the middle of the closed bars' span, so small ticks never move the grid.
    const closed = bars.slice(50, 499)
    const mid = (Math.min(...closed.map((c) => c.low)) + Math.max(...closed.map((c) => c.high))) / 2
    bars[499] = { ...bars[499], open: mid, close: mid, high: mid * 1.0001, low: mid * 0.9999 }
    const open: IndicatorInputs = { from: bars[50].time, to: 0, rows: 24, valueArea: 70 }
    const def = {
      profile: vi.fn((c: readonly Candle[], i: IndicatorInputs) =>
        computeVolumeProfile(c, i.from, i.to, i.rows, i.valueArea),
      ),
    }
    const { ops, calls } = countingOps()
    let cache = nextProfileCache(null, def, open, bars, ops)
    expect(calls.base).toBe(1)
    let current = bars
    for (let i = 0; i < 25; i++) {
      current = tick(current, rnd)
      cache = nextProfileCache(cache, def, open, current, ops)
      expect(cache.profile).toEqual(computeVolumeProfile(current, open.from, 0, 24, 70))
    }
    // One build at first; every tick only extended the cached closed-bar accumulation.
    expect(calls.base).toBe(1)
    expect(calls.extend).toBe(26)
    expect(def.profile).not.toHaveBeenCalled()
  })

  it('the registry volume profile uses the incremental path by default; other defs do not', () => {
    const base = vi.spyOn(volumeProfileOpenOps, 'base')
    const extend = vi.spyOn(volumeProfileOpenOps, 'extend')
    try {
      const bars = makeCandles(30)
      const open: IndicatorInputs = { from: T0 + 5 * STEP, to: 0, rows: 24, valueArea: 70 }
      nextProfileCache(null, volumeProfileDef, open, bars)
      expect(base).toHaveBeenCalledTimes(1)
      expect(extend).toHaveBeenCalledTimes(1)
      const { def } = countingDef()
      nextProfileCache(null, def, open, bars)
      expect(base).toHaveBeenCalledTimes(1)
    } finally {
      base.mockRestore()
      extend.mockRestore()
    }
  })

  it('rebuilds when the forming bar leaves the span, a closed bar changes, or a bar is added', () => {
    const rnd = mulberry32(12)
    const bars = randomBars(200, rnd)
    const open: IndicatorInputs = { from: bars[10].time, to: 0, rows: 24, valueArea: 70 }
    const { ops, calls } = countingOps()
    let cache: ProfileCache = nextProfileCache(null, volumeProfileDef, open, bars, ops)
    // Forming bar's high far above every closed high: the row grid changes.
    const high = [...bars.slice(0, -1), { ...bars[199], high: 1e6 }]
    cache = nextProfileCache(cache, volumeProfileDef, open, high, ops)
    expect(calls.base).toBe(2)
    expect(cache.profile).toEqual(computeVolumeProfile(high, open.from, 0, 24, 70))
    // A closed bar in range changed (new object) together with a tick.
    const closedEdit = tick(
      high.map((c, i) => (i === 100 ? { ...c, volume: c.volume + 1 } : c)),
      rnd,
    )
    cache = nextProfileCache(cache, volumeProfileDef, open, closedEdit, ops)
    expect(calls.base).toBe(3)
    expect(cache.profile).toEqual(computeVolumeProfile(closedEdit, open.from, 0, 24, 70))
    // A new bar opens.
    const last = closedEdit[199]
    const appended = [...closedEdit, { ...last, time: last.time + STEP, open: last.close }]
    cache = nextProfileCache(cache, volumeProfileDef, open, appended, ops)
    expect(calls.base).toBe(4)
    expect(cache.profile).toEqual(computeVolumeProfile(appended, open.from, 0, 24, 70))
  })

  it('property: incremental equals a full recompute bit for bit over random tick sequences', () => {
    // Exact equality (toEqual compares numbers with Object.is): both paths add the same bar
    // shares in the same order on the same grid, so no tolerance is needed.
    let incremental = 0
    for (let seed = 1; seed <= 12; seed++) {
      const rnd = mulberry32(100 + seed)
      let bars = randomBars(60 + Math.floor(rnd() * 200), rnd)
      const rows = 6 + Math.floor(rnd() * 60)
      const valueArea = 50 + Math.floor(rnd() * 46)
      const from = bars[Math.floor(rnd() * 40)].time
      const open: IndicatorInputs = { from, to: 0, rows, valueArea }
      let cache: ProfileCache | null = null
      const { ops, calls } = countingOps()
      for (let step = 0; step < 120; step++) {
        const r = rnd()
        if (r < 0.75) bars = tick(bars, rnd, rnd() < 0.15)
        else if (r < 0.85) {
          const last = bars[bars.length - 1]
          const p = last.close
          bars = [
            ...bars,
            { time: last.time + STEP, open: p, high: p, low: p, close: p, volume: 0 },
          ]
        } else if (r < 0.92) {
          // A zero / non-finite volume tick and a single-price forming bar.
          const last = bars[bars.length - 1]
          bars = [...bars.slice(0, -1), { ...last, volume: r < 0.88 ? 0 : Number.NaN }]
        } else {
          // A closed bar in range rewritten (gap fill / correction).
          const k = bars.length - 2 - Math.floor(rnd() * 5)
          bars = bars.map((c, i) => (i === k ? { ...c, volume: c.volume * 2 } : c))
        }
        cache = nextProfileCache(cache, volumeProfileDef, open, bars, ops)
        expect(cache.profile).toEqual(computeVolumeProfile(bars, from, 0, rows, valueArea))
      }
      // Both paths were exercised: most ticks reused the closed-bar accumulation.
      expect(calls.base).toBeGreaterThan(1)
      expect(calls.extend - calls.base).toBeGreaterThan(40)
      incremental += calls.extend - calls.base
    }
    expect(incremental).toBeGreaterThan(600)
  })
})

/* ------------------------------------------------------------------------------------------ */
/* Primitive + layer with fakes                                                                */
/* ------------------------------------------------------------------------------------------ */

function fakeChart(times: readonly number[], width = 800, barSpacing = 10) {
  return {
    timeScale: () => ({
      timeToIndex: (t: number, nearest?: boolean) => {
        const i = times.indexOf(t)
        if (i >= 0) return i
        if (!nearest) return null
        let best = 0
        for (let k = 1; k < times.length; k++)
          if (Math.abs(times[k] - t) < Math.abs(times[best] - t)) best = k
        return best
      },
      logicalToCoordinate: (l: number) => l * barSpacing,
      width: () => width,
      options: () => ({ barSpacing }),
    }),
  }
}

const fakeMainSeries = { priceToCoordinate: (p: number) => 1100 - 10 * p }

function fakeHost() {
  const attached: ISeriesPrimitive<Time>[] = []
  let redraws = 0
  const host = {
    attachPrimitive(p: ISeriesPrimitive<Time>) {
      attached.push(p)
      p.attached?.({
        chart: fakeChart(TIMES),
        series: fakeMainSeries,
        requestUpdate: () => redraws++,
      } as unknown as Parameters<NonNullable<ISeriesPrimitive<Time>['attached']>>[0])
    },
    detachPrimitive(p: ISeriesPrimitive<Time>) {
      const i = attached.indexOf(p)
      if (i >= 0) attached.splice(i, 1)
      p.detached?.()
    },
  }
  return { host, attached, redraws: () => redraws }
}

const vpDef = {
  id: 'vprofile',
  name: 'Fake VP',
  shortName: 'VP',
  description: 'test',
  placement: 'overlay',
  inputs: [],
  source: null,
  lines: [],
  guides: [],
  createStepper: () => ({
    next: () => ({}),
    clone() {
      return this
    },
  }),
  profile: vi.fn((_c: readonly Candle[], i: IndicatorInputs) => (i.from > 0 ? PROFILE : null)),
} as unknown as IndicatorDef

const smaDef = { ...vpDef, id: 'sma', profile: undefined } as unknown as IndicatorDef

const DEFS: ReadonlyMap<IndicatorId, IndicatorDef> = new Map([
  ['vprofile', vpDef],
  ['sma', smaDef],
])

function config(
  uid: string,
  type: IndicatorId,
  visible = true,
  from = PROFILE.from,
): IndicatorConfig {
  return {
    uid,
    type,
    inputs: { from, to: 0, rows: 3, valueArea: 70 },
    source: 'close',
    colors: {},
    visible,
  }
}

describe('VolumeProfileLayer', () => {
  const candles = makeCandles(40)

  it('adds one primitive per range indicator and removes it with the config', () => {
    const layer = new VolumeProfileLayer()
    const { host, attached } = fakeHost()
    layer.attach(host)
    layer.sync(
      [config('a', 'vprofile'), config('s', 'sma'), config('b', 'vprofile')],
      candles,
      DEFS,
    )
    expect(layer.uids()).toEqual(['a', 'b'])
    expect(attached).toHaveLength(2)
    layer.sync([config('b', 'vprofile')], candles, DEFS)
    expect(layer.uids()).toEqual(['b'])
    expect(attached).toEqual([layer.primitiveOf('b')])
  })

  it('draws visible instances and nothing for hidden ones', () => {
    const layer = new VolumeProfileLayer()
    const { host } = fakeHost()
    layer.attach(host)
    layer.sync([config('a', 'vprofile'), config('h', 'vprofile', false)], candles, DEFS)
    const a = layer.primitiveOf('a') as VolumeProfilePrimitive
    const h = layer.primitiveOf('h') as VolumeProfilePrimitive
    a.updateAllViews()
    h.updateAllViews()
    expect(a.geometry()).not.toBeNull()
    expect(h.geometry()).toBeNull()
    expect(h.hitTest(20, 85)).toBeNull()
    expect(h.autoscaleInfo(0 as Logical, 100 as Logical)).toBeNull()
    // Showing it again draws it.
    layer.sync([config('a', 'vprofile'), config('h', 'vprofile', true)], candles, DEFS)
    h.updateAllViews()
    expect(h.geometry()).not.toBeNull()
  })

  it('draws nothing for an unset range (the def returns null)', () => {
    const layer = new VolumeProfileLayer()
    const { host } = fakeHost()
    layer.attach(host)
    layer.sync([config('u', 'vprofile', true, 0)], candles, DEFS)
    const u = layer.primitiveOf('u') as VolumeProfilePrimitive
    u.updateAllViews()
    expect(u.getProfile()).toBeNull()
    expect(u.geometry()).toBeNull()
  })

  it('reports hits as indicator:<uid> and includes the profile in autoscale when in view', () => {
    const layer = new VolumeProfileLayer()
    const { host } = fakeHost()
    layer.attach(host)
    layer.sync([config('a', 'vprofile')], candles, DEFS)
    const a = layer.primitiveOf('a') as VolumeProfilePrimitive
    a.updateAllViews()
    expect(a.hitTest(20, 85)).toMatchObject({ externalId: 'indicator:a', zOrder: 'normal' })
    expect(a.hitTest(700, 10)).toBeNull()
    expect(a.autoscaleInfo(0 as Logical, 30 as Logical)).toEqual({
      priceRange: { minValue: 100, maxValue: 103 },
    })
    // Visible logical range after the profile's last bar (6).
    expect(a.autoscaleInfo(7 as Logical, 30 as Logical)).toBeNull()
  })

  it('re-attaches primitives to a new series and keeps profiles cached', () => {
    const profile = vpDef.profile as unknown as ReturnType<typeof vi.fn>
    const layer = new VolumeProfileLayer()
    const first = fakeHost()
    layer.attach(first.host)
    layer.sync([config('a', 'vprofile')], candles, DEFS)
    const calls = profile.mock.calls.length
    const second = fakeHost()
    layer.attach(second.host)
    expect(first.attached).toHaveLength(0)
    expect(second.attached).toEqual([layer.primitiveOf('a')])
    layer.sync([config('a', 'vprofile')], candles, DEFS)
    expect(profile.mock.calls.length).toBe(calls)
    // A removed series: references dropped, nothing called on it.
    layer.detach(false)
    expect(second.attached).toHaveLength(1)
    expect(layer.primitiveOf('a')?.geometry()).toBeNull()
  })

  it('creates primitives before a series exists and attaches them later', () => {
    const layer = new VolumeProfileLayer()
    layer.sync([config('a', 'vprofile')], candles, DEFS)
    const { host, attached } = fakeHost()
    layer.attach(host)
    expect(attached).toEqual([layer.primitiveOf('a')])
  })

  it('recolours every primitive and redraws only on a real change', () => {
    const layer = new VolumeProfileLayer()
    const fake = fakeHost()
    layer.attach(fake.host)
    layer.sync([config('a', 'vprofile')], candles, DEFS)
    const before = fake.redraws()
    layer.setColors({ ...DEFAULT_VOLUME_PROFILE_COLORS })
    expect(fake.redraws()).toBe(before)
    layer.setColors({ ...DEFAULT_VOLUME_PROFILE_COLORS, poc: '#00ff00' })
    expect(fake.redraws()).toBe(before + 1)
  })
})

describe('drawProfile', () => {
  function recordingScope(ratio: number) {
    const fills: { style: string; alpha: number; rect: number[] }[] = []
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
      strokeRect: vi.fn(),
      fillRect(x: number, y: number, w: number, h: number) {
        fills.push({ style: String(this.fillStyle), alpha: this.globalAlpha, rect: [x, y, w, h] })
      },
    }
    const scope = {
      context: ctx,
      horizontalPixelRatio: ratio,
      verticalPixelRatio: ratio,
    } as unknown as Parameters<typeof drawProfile>[0]
    return { scope, fills }
  }

  it('fills integer bitmap rects, up then down, brighter inside the value area', () => {
    const g = profileGeometry(PROFILE, coordsFor(TIMES)) as ProfileGeometry
    const { scope, fills } = recordingScope(2)
    drawProfile(scope, g, DEFAULT_VOLUME_PROFILE_COLORS)
    for (const f of fills) for (const v of f.rect) expect(Number.isInteger(v)).toBe(true)
    const rows = fills.slice(1)
    // Row 0: all up, outside VA. Row 1: up + down, VA. Row 2: all down, VA.
    expect(rows.map((f) => [f.style, f.alpha])).toEqual([
      ['#2962ff', OUTSIDE_VALUE_AREA_ALPHA],
      ['#2962ff', VALUE_AREA_ALPHA],
      ['#fbc02d', VALUE_AREA_ALPHA],
      ['#fbc02d', VALUE_AREA_ALPHA],
    ])
    // Row 1 starts at the range's left edge and spans the max length.
    const [up, down] = [rows[1].rect, rows[2].rect]
    expect(up[0]).toBe(30)
    expect(up[2] + down[2]).toBe(MIN_PROFILE_BAR_PX * 2)
    expect(down[0]).toBe(up[0] + up[2])
    // 1 px separator: a 20 px row is drawn 19 px tall.
    expect(up[3]).toBe(19)
  })
})
