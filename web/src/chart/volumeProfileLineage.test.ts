// Phase 19: the volume profile layer takes the chart's shared BarLineage (like market structure
// and sessions). With it a live tick checks the in-range bars without reading them; the
// profiles must be exactly those of a layer without it.
import { describe, expect, it, vi } from 'vitest'
import { applyLiveCandle, prependOlder } from '../data/candleStore'
import type { IndicatorDef, IndicatorId, IndicatorInputs } from '../indicators/types'
import { computeVolumeProfile, volumeProfileDef } from '../indicators/volumeProfile'
import type { IndicatorConfig } from '../state/indicators'
import type { Candle } from '../types'
import { planSeriesSync } from './seriesSync'
import { BarLineage, planSamePrefix } from './structureBars'
import { VolumeProfileLayer, nextProfileCache, type PrimitiveHost } from './volumeProfileLayer'

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

function randomBars(n: number, r: () => number, start = T0): Candle[] {
  const out: Candle[] = []
  let prev = 100
  for (let i = 0; i < n; i++) {
    const open = prev
    const close = Math.max(1, open * (1 + (r() - 0.5) * 0.04))
    out.push({
      time: start + i * STEP,
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

const DEFS: ReadonlyMap<IndicatorId, IndicatorDef> = new Map([
  [volumeProfileDef.id, volumeProfileDef],
])

function config(uid: string, from: number, to: number): IndicatorConfig {
  return {
    uid,
    type: volumeProfileDef.id,
    inputs: { from, to, rows: 24, valueArea: 70 },
    source: 'close',
    colors: {},
    visible: true,
  } as IndicatorConfig
}

const host: PrimitiveHost = { attachPrimitive: () => {}, detachPrimitive: () => {} }

describe('volume profile layer with the shared BarLineage', () => {
  for (const seed of [21, 22, 23]) {
    it(`gives the same profiles as without it over random ticks, edits and prepends (seed ${seed})`, () => {
      const r = rng(seed)
      let bars: readonly Candle[] = randomBars(200, r)
      const configs = [
        config('fixed', bars[40].time, bars[90].time),
        config('open', bars[120].time, 0),
        config('early', T0 - 30 * STEP, 0), // anchor before the loaded history
        config('tail', bars[195].time, 0),
      ]
      const lineage = new BarLineage()
      const fast = new VolumeProfileLayer(undefined, lineage)
      const slow = new VolumeProfileLayer()
      fast.attach(host)
      slow.attach(host)
      let applied: readonly Candle[] | null = null
      for (let i = 0; i < 600; i++) {
        const last = bars[bars.length - 1]
        const x = r()
        let next: readonly Candle[]
        if (x < 0.6) {
          const close = last.close * (1 + (r() - 0.5) * (r() < 0.1 ? 0.2 : 0.002))
          next = applyLiveCandle(bars, {
            ...last,
            close,
            high: Math.max(last.high, close),
            low: Math.min(last.low, close),
            volume: last.volume + r() * 10,
          })
        } else if (x < 0.8) {
          next = applyLiveCandle(bars, { ...last, time: last.time + STEP, volume: r() * 100 })
        } else if (x < 0.95) {
          // A late correction of a bar anywhere (inside or outside the ranges).
          const k = Math.floor(r() * bars.length)
          next = applyLiveCandle(bars, { ...bars[k], volume: bars[k].volume + 1 })
        } else {
          const first = bars[0].time
          next = prependOlder(bars, randomBars(10, r, first - 10 * STEP))
        }
        // Like Chart's data effect; sometimes the step is not recorded (falls back to the scan).
        const plan = planSeriesSync(applied, next)
        lineage.record(applied, next, r() < 0.8 ? planSamePrefix(plan, next) : null)
        applied = next
        bars = next
        fast.sync(configs, bars, DEFS)
        slow.sync(configs, bars, DEFS)
        for (const c of configs) {
          expect(fast.primitiveOf(c.uid)?.getProfile()).toEqual(
            slow.primitiveOf(c.uid)?.getProfile(),
          )
        }
      }
      // And both equal a from-scratch computation at the end.
      for (const c of configs) {
        const { from, to } = c.inputs
        const want = computeVolumeProfile(bars, from, to, 24, 70)
        expect(fast.primitiveOf(c.uid)?.getProfile() ?? null).toEqual(want)
      }
    })
  }
})

describe('nextProfileCache identity check with a lineage', () => {
  const bars = randomBars(100, rng(5))
  const inputs: IndicatorInputs = {
    from: bars[10].time,
    to: bars[50].time,
    rows: 24,
    valueArea: 70,
  }

  function countingDef() {
    const profile = vi.fn((c: readonly Candle[], i: IndicatorInputs) =>
      computeVolumeProfile(c, i.from, i.to, i.rows, i.valueArea),
    )
    return { def: { profile }, profile }
  }

  it('trusts the known prefix instead of reading the in-range bars', () => {
    const { def, profile } = countingDef()
    const a = nextProfileCache(null, def, inputs, bars, null)
    // Bar 30 (in range) edited: without a lineage the scan sees it and recomputes ...
    const edited = bars.slice()
    edited[30] = { ...edited[30], volume: 1 }
    nextProfileCache(a, def, inputs, edited, null)
    expect(profile).toHaveBeenCalledTimes(2)
    // ... with a (deliberately false) lineage vouching for 60 leading bars it does not look.
    const liar = { knownSamePrefix: () => 60 }
    const b = nextProfileCache(a, def, inputs, edited, null, liar)
    expect(profile).toHaveBeenCalledTimes(2)
    expect(b.profile).toBe(a.profile)
  })

  it('a known prefix that stops inside the range still reads the rest of it', () => {
    const { def, profile } = countingDef()
    const a = nextProfileCache(null, def, inputs, bars, null)
    const edited = bars.slice()
    edited[30] = { ...edited[30], volume: 1 }
    nextProfileCache(a, def, inputs, edited, null, { knownSamePrefix: () => 30 })
    expect(profile).toHaveBeenCalledTimes(2)
  })

  it('reuses the inputs key while the inputs object is the same (no string per tick)', () => {
    const { def } = countingDef()
    const a = nextProfileCache(null, def, inputs, bars, null)
    expect(a.inputs).toBe(inputs)
    // A planted key proves the string was not rebuilt from the same object ...
    const planted = { ...a, inputsKey: 'planted' }
    expect(nextProfileCache(planted, def, inputs, bars, null)).toBe(planted)
    // ... and a new (equal) inputs object builds it again and still matches.
    const again = nextProfileCache(a, def, { ...inputs }, bars, null)
    expect(again).toBe(a)
  })
})
