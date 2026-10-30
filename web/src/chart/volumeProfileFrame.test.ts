// Phase 19 wave 2: the volume profile's per-frame path. The geometry is cached behind a view
// signature, culled before any row is placed, and drawn without per-frame dash arrays; none of
// it may change what is drawn.
import { describe, expect, it, vi } from 'vitest'
import type { VolumeProfile } from '../indicators/types'
import { computeVolumeProfile } from '../indicators/volumeProfile'
import type { Candle } from '../types'
import {
  OUTSIDE_VALUE_AREA_ALPHA,
  VALUE_AREA_ALPHA,
  VolumeProfilePrimitive,
  bitmapSpan,
  drawProfile,
  profileGeometry,
  profileOffscreen,
  profileStats,
  isValueAreaRow,
  valueAreaDash,
  volumeProfileColorsFor,
  type ProfileGeometry,
  type VolumeProfileColors,
} from './volumeProfileLayer'
import { barSpacingOf } from './viewSignature'

const T0 = 1_700_000_000
const STEP = 3600

function rng(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function bars(n: number, r: () => number): Candle[] {
  const out: Candle[] = []
  let prev = 100
  for (let i = 0; i < n; i++) {
    const open = prev
    const close = Math.max(1, open * (1 + (r() - 0.5) * 0.04))
    out.push({
      time: T0 + i * STEP,
      open,
      high: Math.max(open, close) * (1 + r() * 0.01),
      low: Math.min(open, close) * (1 - r() * 0.01),
      close,
      volume: r() * 1000,
    })
    prev = close
  }
  return out
}

const CANDLES = bars(300, rng(42))
const TIMES = CANDLES.map((c) => c.time)
const DARK = volumeProfileColorsFor({ text: '#b2b5be', mutedText: '#787b86' })
const LIGHT = volumeProfileColorsFor({ text: '#131722', mutedText: '#787b86' })

function profile(from: number, to: number, rows: number, va = 70): VolumeProfile {
  const p = computeVolumeProfile(CANDLES, TIMES[from], to === 0 ? 0 : TIMES[to], rows, va)
  if (p === null) throw new Error('no profile')
  return p
}

const PROFILES = [profile(40, 120, 24), profile(10, 290, 100, 68), profile(200, 0, 400)]

interface View {
  rightOffset: number
  bs: number
  width: number
  a: number
  b: number
  log: boolean
}

const HOME: View = { rightOffset: 0, bs: 6, width: 400, a: 1100, b: 10, log: false }

/**
 * A chart whose time scale places logical l as LWC does (`width - (base + rightOffset - l +
 * 0.5) * barSpacing - 1`) and whose price scale is linear or logarithmic. Counts the calls the
 * frame path makes.
 */
function fakeChart(initial: View = HOME) {
  const state = { view: { ...initial } }
  const counts = { price: 0, logical: 0 }
  const options = vi.fn(() => ({ barSpacing: state.view.bs }))
  const ts = {
    timeToIndex: (t: number, nearest?: boolean) => {
      let lo = 0
      let hi = TIMES.length - 1
      while (lo < hi) {
        const mid = (lo + hi) >> 1
        if (TIMES[mid] < t) lo = mid + 1
        else hi = mid
      }
      if (TIMES[lo] === t) return lo
      if (!nearest) return null
      if (lo > 0 && Math.abs(TIMES[lo - 1] - t) <= Math.abs(TIMES[lo] - t)) return lo - 1
      return lo
    },
    logicalToCoordinate: (l: number) => {
      counts.logical++
      const v = state.view
      return v.width - (TIMES.length - 1 + v.rightOffset - l + 0.5) * v.bs - 1
    },
    width: () => state.view.width,
    options,
  }
  const chart = { timeScale: () => ts }
  const series = {
    priceToCoordinate: (p: number) => {
      counts.price++
      const v = state.view
      return v.log ? v.a - v.b * 30 * Math.log(p) : v.a - v.b * p
    },
  }
  return { state, counts, options, ts, chart, series }
}

type Fake = ReturnType<typeof fakeChart>

function primitiveOn(fake: Fake, p: VolumeProfile | null, colors = DARK): VolumeProfilePrimitive {
  const prim = new VolumeProfilePrimitive('vp', colors)
  prim.attached({
    chart: fake.chart,
    series: fake.series,
    requestUpdate: () => {},
  } as never)
  prim.setProfile(p)
  return prim
}

/** The full geometry for the fake's current view, built without the primitive or its cache. */
function fullGeometry(fake: Fake, p: VolumeProfile): ProfileGeometry | null {
  const ts = fake.ts
  return profileGeometry(p, {
    xOfTime: (t) => {
      const i = ts.timeToIndex(t, true)
      return i === null ? null : ts.logicalToCoordinate(i)
    },
    yOfPrice: (price) => fake.series.priceToCoordinate(price),
    barSpacing: barSpacingOf(ts as never),
  })
}

function randomView(r: () => number): View {
  return {
    rightOffset: (r() - 0.5) * 700,
    bs: 0.5 + r() * 12,
    width: 200 + Math.floor(r() * 600),
    a: 800 + r() * 600,
    b: 0.2 + r() * 15,
    log: r() < 0.3,
  }
}

describe('volume profile geometry cache', () => {
  it('cached geometry equals a fresh build across random view, profile, colour and visibility changes', () => {
    const r = rng(19)
    const fake = fakeChart()
    const prim = primitiveOn(fake, PROFILES[0])
    let current: VolumeProfile = PROFILES[0]
    let visible = true
    let hits = 0
    for (let step = 0; step < 600; step++) {
      const v = fake.state.view
      const op = Math.floor(r() * 9)
      if (op === 0) fake.state.view = { ...v, rightOffset: v.rightOffset + (r() - 0.5) * 80 }
      else if (op === 1) fake.state.view = { ...v, bs: Math.max(0.5, v.bs * (0.6 + r() * 0.8)) }
      else if (op === 2) fake.state.view = { ...v, width: 200 + Math.floor(r() * 600) }
      else if (op === 3) fake.state.view = { ...v, a: v.a + (r() - 0.5) * 200, b: v.b * 1.3 }
      else if (op === 4) fake.state.view = { ...v, log: !v.log }
      else if (op === 5) {
        current = PROFILES[Math.floor(r() * PROFILES.length)]
        prim.setProfile(current)
      } else if (op === 6) prim.setColors(r() < 0.5 ? LIGHT : DARK)
      else if (op === 7) {
        visible = r() < 0.7
        prim.setVisible(visible)
      }
      // op 8: nothing changed.
      const builds = prim.buildCount()
      prim.updateAllViews()
      if (prim.buildCount() === builds) hits++
      const fresh = primitiveOn(fakeChart(fake.state.view), current)
      fresh.setVisible(visible)
      fresh.updateAllViews()
      expect(prim.geometry(), `step ${step}`).toEqual(fresh.geometry())
      // And the cache-free reference: full geometry, then the old off-screen check.
      const full = visible ? fullGeometry(fake, current) : null
      const expected = full !== null && profileOffscreen(full, fake.state.view.width) ? null : full
      expect(prim.geometry(), `step ${step}`).toEqual(expected)
    }
    expect(hits).toBeGreaterThan(30)
  })

  it('an unchanged view returns the same geometry object without placing any row', () => {
    const fake = fakeChart()
    const prim = primitiveOn(fake, PROFILES[2])
    prim.updateAllViews()
    const first = prim.geometry()
    expect(first).not.toBeNull()
    expect(prim.buildCount()).toBe(1)
    const before = fake.counts.price
    for (let i = 0; i < 10; i++) prim.updateAllViews()
    expect(prim.geometry()).toBe(first)
    expect(prim.buildCount()).toBe(1)
    // Two reference prices for the signature and the POC per frame; never the 400 rows.
    expect(fake.counts.price - before).toBe(30)
    // Any of scroll, zoom, resize or a price-scale move rebuilds.
    for (const change of [
      { rightOffset: 3 },
      { bs: 7 },
      { width: 401 },
      { a: 1101 },
      { b: 11 },
      { log: true },
    ]) {
      fake.state.view = { ...fake.state.view, ...change }
      const builds = prim.buildCount()
      prim.updateAllViews()
      expect(prim.buildCount()).toBe(builds + 1)
    }
  })

  it('setProfile, setColors, setVisible and re-attaching each invalidate the cached geometry', () => {
    const fake = fakeChart()
    const prim = primitiveOn(fake, PROFILES[0])
    prim.updateAllViews()
    const steps: (() => void)[] = [
      () => prim.setProfile(PROFILES[1]),
      () => prim.setColors(LIGHT),
      () => {
        prim.setVisible(false)
        prim.updateAllViews()
        expect(prim.geometry()).toBeNull()
        prim.setVisible(true)
      },
      () => {
        prim.detached()
        expect(prim.geometry()).toBeNull()
        prim.attached({ chart: fake.chart, series: fake.series, requestUpdate: () => {} } as never)
      },
    ]
    for (const change of steps) {
      const builds = prim.buildCount()
      change()
      prim.updateAllViews()
      expect(prim.buildCount()).toBe(builds + 1)
      expect(prim.geometry()).not.toBeNull()
    }
  })

  it('reads bar spacing from logical coordinates, never from timeScale().options()', () => {
    const r = rng(5)
    const fake = fakeChart()
    const prim = primitiveOn(fake, PROFILES[1])
    for (let i = 0; i < 50; i++) {
      fake.state.view = randomView(r)
      prim.updateAllViews()
      prim.updateAllViews()
      prim.autoscaleInfo(0 as never, 299 as never)
      prim.hitTest(100, 100)
    }
    expect(fake.options).not.toHaveBeenCalled()
  })

  it('caches the largest row volume and value-area flags per profile', () => {
    for (const p of PROFILES) {
      const stats = profileStats(p)
      expect(stats.maxVolume).toBe(Math.max(...p.rows.map((row) => row.volume)))
      expect(stats.inValueArea).toEqual(p.rows.map((row) => isValueAreaRow(row, p)))
    }
  })
})

describe('volume profile off-screen culling', () => {
  it('culls exactly what profileOffscreen culls, over random views', () => {
    const r = rng(77)
    const fake = fakeChart()
    let culled = 0
    let drawn = 0
    for (let i = 0; i < 400; i++) {
      const p = PROFILES[i % PROFILES.length]
      fake.state.view = randomView(r)
      const prim = primitiveOn(fake, p)
      prim.updateAllViews()
      const full = fullGeometry(fake, p) as ProfileGeometry
      expect(full).not.toBeNull()
      const off = profileOffscreen(full, fake.state.view.width)
      expect(prim.geometry() === null).toBe(off)
      if (off) culled++
      else drawn++
    }
    expect(culled).toBeGreaterThan(20)
    expect(drawn).toBeGreaterThan(20)
  })

  it('an off-screen profile places no row and draws nothing', () => {
    for (const rightOffset of [-900, 900]) {
      const fake = fakeChart({ ...HOME, rightOffset })
      const prim = primitiveOn(fake, PROFILES[2])
      const before = fake.counts.price
      prim.updateAllViews()
      expect(prim.geometry()).toBeNull()
      expect(prim.buildCount()).toBe(1)
      // Only the signature's two reference prices and the POC: no row edges.
      expect(fake.counts.price - before).toBe(3)
      const full = fullGeometry(fake, PROFILES[2]) as ProfileGeometry
      expect(profileOffscreen(full, HOME.width)).toBe(true)
      const target = { useBitmapCoordinateSpace: vi.fn() }
      for (const view of prim.paneViews()) view.renderer()?.draw(target as never)
      expect(target.useBitmapCoordinateSpace).not.toHaveBeenCalled()
    }
  })
})

/* ------------------------------------------------------------------------------------------ */
/* Drawing: same canvas output as the pre-Phase-19 renderer                                    */
/* ------------------------------------------------------------------------------------------ */

type BitmapScope = Parameters<typeof drawProfile>[0]

/** The renderer as it was before Phase 19 wave 2, verbatim (the reference for pixel identity). */
function referenceDraw(scope: BitmapScope, geom: ProfileGeometry, colors: VolumeProfileColors) {
  const hLine = (x0: number, x1: number, y: number, width: number) => {
    const c = scope.context
    const yb = Math.round(y * scope.verticalPixelRatio) + (width % 2 ? 0.5 : 0)
    c.beginPath()
    c.moveTo(Math.round(x0 * scope.horizontalPixelRatio), yb)
    c.lineTo(Math.round(x1 * scope.horizontalPixelRatio), yb)
    c.stroke()
  }
  const ctx = scope.context
  const hr = scope.horizontalPixelRatio
  const vr = scope.verticalPixelRatio
  ctx.save()
  const box = bitmapSpan(geom.x0, geom.x1, hr)
  const boxY = bitmapSpan(geom.top, geom.bottom, vr)
  ctx.globalAlpha = 0.04
  ctx.fillStyle = colors.text
  ctx.fillRect(box.position, boxY.position, box.length, boxY.length)
  const outline = Math.max(1, Math.floor(hr))
  ctx.globalAlpha = 0.32
  ctx.strokeStyle = colors.text
  ctx.lineWidth = outline
  ctx.setLineDash([])
  const o = outline % 2 ? 0.5 : 0
  ctx.strokeRect(box.position + o, boxY.position + o, box.length - 1, boxY.length - 1)
  for (const row of geom.rows) {
    const len = row.upLength + row.downLength
    if (!(len > 0)) continue
    const ys = bitmapSpan(row.top, row.bottom, vr, true)
    const left = Math.round(geom.barX * hr)
    const upEnd = Math.round((geom.barX + row.upLength) * hr)
    const end = Math.max(left + 1, Math.round((geom.barX + len) * hr))
    ctx.globalAlpha = row.inValueArea ? VALUE_AREA_ALPHA : OUTSIDE_VALUE_AREA_ALPHA
    if (upEnd > left) {
      ctx.fillStyle = colors.up
      ctx.fillRect(left, ys.position, upEnd - left, ys.length)
    }
    const downStart = Math.max(left, upEnd)
    if (end > downStart) {
      ctx.fillStyle = colors.down
      ctx.fillRect(downStart, ys.position, end - downStart, ys.length)
    }
  }
  const lineWidth = Math.max(1, Math.floor(vr))
  ctx.lineWidth = lineWidth
  ctx.globalAlpha = 0.75
  ctx.strokeStyle = colors.valueArea
  ctx.setLineDash([Math.max(1, Math.round(2 * hr)), Math.max(2, Math.round(3 * hr))])
  for (const y of [geom.vahY, geom.valY]) {
    if (y !== null) hLine(geom.x0, geom.x1, y, lineWidth)
  }
  ctx.setLineDash([])
  if (geom.pocY !== null) {
    ctx.globalAlpha = 1
    ctx.strokeStyle = colors.poc
    hLine(geom.x0, geom.x1, geom.pocY, lineWidth)
  }
  ctx.restore()
}

/**
 * A canvas that records every drawing call with the full state in effect at that call (alpha,
 * fill, stroke, line width, dash). Two renderers with equal records paint the same pixels in
 * the same order, however many redundant state sets they make.
 */
function recordingScope(ratio: number) {
  const ops: string[] = []
  const dashArgs: number[][] = []
  const st = { alpha: 1, fill: '', stroke: '', lw: 1, dash: '[]' }
  const at = () => `a=${st.alpha} f=${st.fill} s=${st.stroke} lw=${st.lw} d=${st.dash}`
  const ctx = {
    set globalAlpha(v: number) {
      st.alpha = v
    },
    get globalAlpha() {
      return st.alpha
    },
    set fillStyle(v: string) {
      st.fill = v
    },
    set strokeStyle(v: string) {
      st.stroke = v
    },
    set lineWidth(v: number) {
      st.lw = v
    },
    setLineDash(d: number[]) {
      dashArgs.push(d)
      st.dash = JSON.stringify(d)
    },
    save: () => ops.push('save'),
    restore: () => ops.push('restore'),
    beginPath: () => ops.push('beginPath'),
    moveTo: (x: number, y: number) => ops.push(`moveTo ${x},${y}`),
    lineTo: (x: number, y: number) => ops.push(`lineTo ${x},${y}`),
    stroke: () => ops.push(`stroke ${at()}`),
    strokeRect: (x: number, y: number, w: number, h: number) =>
      ops.push(`strokeRect ${x},${y},${w},${h} ${at()}`),
    fillRect: (x: number, y: number, w: number, h: number) =>
      ops.push(`fillRect ${x},${y},${w},${h} ${at()}`),
  }
  const scope = {
    context: ctx,
    horizontalPixelRatio: ratio,
    verticalPixelRatio: ratio,
  } as unknown as BitmapScope
  return { scope, ops, dashArgs }
}

describe('drawProfile', () => {
  it('issues the same canvas calls as the pre-cache renderer, light and dark, at every ratio', () => {
    const r = rng(3)
    let compared = 0
    for (let i = 0; i < 120; i++) {
      const fake = fakeChart(i < 3 ? HOME : randomView(r))
      // Include thin rows (sub-pixel) so overlapping fills are covered too.
      if (i % 4 === 0) fake.state.view = { ...fake.state.view, b: 0.05 }
      const geom = fullGeometry(fake, PROFILES[i % PROFILES.length])
      if (geom === null) continue
      for (const colors of [DARK, LIGHT])
        for (const ratio of [1, 1.25, 1.5, 2, 3]) {
          const now = recordingScope(ratio)
          const ref = recordingScope(ratio)
          drawProfile(now.scope, geom, colors)
          drawProfile(now.scope, geom, colors) // a repeat draw uses the cached bitmap rows
          referenceDraw(ref.scope, geom, colors)
          referenceDraw(ref.scope, geom, colors)
          expect(now.ops).toEqual(ref.ops)
          expect(now.dashArgs).toEqual(ref.dashArgs)
          compared++
        }
    }
    expect(compared).toBe(1200)
  })

  it('a repeat draw allocates no dash arrays', () => {
    const fake = fakeChart()
    const geom = fullGeometry(fake, PROFILES[0]) as ProfileGeometry
    const a = recordingScope(2)
    const b = recordingScope(2)
    drawProfile(a.scope, geom, DARK)
    drawProfile(b.scope, geom, LIGHT)
    expect(a.dashArgs).toHaveLength(3)
    for (let i = 0; i < 3; i++) expect(b.dashArgs[i]).toBe(a.dashArgs[i])
    expect(a.dashArgs[1]).toEqual([4, 6])
    // Another ratio gets its own pattern; returning gives the right values again.
    expect(valueAreaDash(1)).toEqual([2, 3])
    expect(valueAreaDash(1.5)).toEqual([3, 5])
    expect(valueAreaDash(2)).toEqual([4, 6])
    expect(valueAreaDash(2)).toBe(valueAreaDash(2))
  })
})
