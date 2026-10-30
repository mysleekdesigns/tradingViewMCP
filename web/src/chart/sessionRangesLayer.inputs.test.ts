// Phase 19: the layer derives params, their key and the drawn sessions once per inputs object
// and interval, not per sync (per tick). The derivation helpers are wrapped in spies that call
// the real ones.
import { describe, expect, it, vi } from 'vitest'
import * as sr from '../indicators/sessionRanges'
import type { IndicatorDef, IndicatorId } from '../indicators/types'
import type { IndicatorConfig } from '../state/indicators'
import type { Candle } from '../types'
import { SessionRangesLayer, sessionDrawn } from './sessionRangesLayer'

vi.mock('../indicators/sessionRanges', async (importOriginal) => {
  const real = await importOriginal<typeof import('../indicators/sessionRanges')>()
  return {
    ...real,
    sessionParamsKey: vi.fn(real.sessionParamsKey),
    alignedSessions: vi.fn(real.alignedSessions),
  }
})

const H1 = 3_600
const H4 = 4 * H1
const T0 = 1_700_006_400 - 6 * H1

function randomWalk(n: number, seed: number, step = H1): Candle[] {
  let s = seed
  const rnd = () => {
    s = (s * 1_103_515_245 + 12_345) % 2_147_483_648
    return s / 2_147_483_648
  }
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

const DEFS: ReadonlyMap<IndicatorId, IndicatorDef> = new Map([['sessions', sr.sessionRangesDef]])
const BASE = {
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
}

function config(inputs: Record<string, number>, visible = true): IndicatorConfig {
  return { uid: 'a', type: 'sessions', inputs, source: 'close', colors: {}, visible }
}

function syncLive(
  layer: SessionRangesLayer,
  cfg: IndicatorConfig,
  bars: readonly Candle[],
  interval = H1,
) {
  layer.sync([cfg], bars, bars.length >= 2 ? bars[bars.length - 2].time : null, interval, DEFS)
}

describe('SessionRangesLayer: inputs derived once per inputs object and interval', () => {
  it('forming-bar ticks and closed bars with the same config derive nothing again', () => {
    const bars = randomWalk(400, 3)
    const layer = new SessionRangesLayer()
    const cfg = config({ ...BASE })
    syncLive(layer, cfg, bars.slice(0, 200))
    const key = vi.mocked(sr.sessionParamsKey)
    const aligned = vi.mocked(sr.alignedSessions)
    key.mockClear()
    aligned.mockClear()
    const view = layer.primitiveOf('a')?.view()
    for (let i = 0; i < 20; i++) {
      const forming = bars[199]
      const tick = { ...forming, close: forming.close + (i % 2 === 0 ? 0.5 : -0.5) }
      syncLive(layer, cfg, [...bars.slice(0, 199), tick])
      expect(layer.primitiveOf('a')?.view()).toBe(view)
    }
    for (let n = 201; n <= 400; n++) syncLive(layer, cfg, bars.slice(0, n))
    expect(key).not.toHaveBeenCalled()
    expect(aligned).not.toHaveBeenCalled()
    expect(layer.statsOf('a')).toEqual({ recomputes: 1, pushed: 399, count: 399 })
    expect(layer.resultOf('a')).toEqual(
      sr.computeSessionRanges(bars.slice(0, 399), sr.SESSION_RANGES_DEFAULTS),
    )
  })

  it('a new interval with the same inputs object re-derives the drawn sessions', () => {
    const cfg = config({ ...BASE })
    const layer = new SessionRangesLayer()
    syncLive(layer, cfg, randomWalk(200, 4), H1)
    expect(layer.primitiveOf('a')?.view()?.show).toEqual(sessionDrawn(cfg.inputs, H1))
    syncLive(layer, cfg, randomWalk(200, 4, H4), H4)
    expect(layer.primitiveOf('a')?.view()?.show).toEqual(sessionDrawn(cfg.inputs, H4))
    expect(sessionDrawn(cfg.inputs, H4)).not.toEqual(sessionDrawn(cfg.inputs, H1))
  })

  it('a new inputs object is re-derived: equal values keep the engine, new hours recompute', () => {
    const bars = randomWalk(300, 5)
    const layer = new SessionRangesLayer()
    syncLive(layer, config({ ...BASE }), bars)
    const view = layer.primitiveOf('a')?.view()
    const key = vi.mocked(sr.sessionParamsKey)
    key.mockClear()
    syncLive(layer, config({ ...BASE }), bars)
    expect(key).toHaveBeenCalledTimes(1)
    expect(layer.statsOf('a')?.recomputes).toBe(1)
    expect(layer.primitiveOf('a')?.view()).toBe(view)
    syncLive(layer, config({ ...BASE, showLondon: 0 }), bars)
    expect(layer.statsOf('a')?.recomputes).toBe(1)
    expect(layer.primitiveOf('a')?.view()?.show.london).toBe(false)
    const hours = { ...BASE, asiaStart: 1, asiaEnd: 9 }
    syncLive(layer, config(hours), bars)
    expect(layer.statsOf('a')?.recomputes).toBe(2)
    expect(layer.resultOf('a')).toEqual(
      sr.computeSessionRanges(bars.slice(0, -1), sr.sessionParams(hours)),
    )
  })
})
