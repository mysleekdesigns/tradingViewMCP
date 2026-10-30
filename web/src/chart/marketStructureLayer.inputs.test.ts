// Phase 19: the layer derives params, their key and the toggles once per inputs object, not
// per sync (per tick). The derivation helpers are wrapped in spies that call the real ones.
import { describe, expect, it, vi } from 'vitest'
import * as ms from '../indicators/marketStructure'
import type { IndicatorConfig } from '../state/indicators'
import type { Candle } from '../types'
import { MarketStructureLayer } from './marketStructureLayer'

vi.mock('../indicators/marketStructure', async (importOriginal) => {
  const real = await importOriginal<typeof import('../indicators/marketStructure')>()
  return {
    ...real,
    structureParams: vi.fn(real.structureParams),
    structureParamsKey: vi.fn(real.structureParamsKey),
  }
})

const T0 = 1_700_006_400
const H4 = 14_400

function randomWalk(n: number, seed: number): Candle[] {
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
    out.push({ time: T0 + i * H4, open, high, low, close, volume: 1 + rnd() })
    prev = close
  }
  return out
}

function config(inputs: Record<string, number>, visible = true): IndicatorConfig {
  return { uid: 'a', type: 'mstructure', inputs, source: 'close', colors: {}, visible }
}

const BASE = {
  left: 2,
  right: 2,
  maxZones: 3,
  showSwings: 1,
  showBos: 1,
  showChoch: 1,
  showZones: 1,
}

/** Live feed: bars plotted, the last one forming. */
function syncLive(layer: MarketStructureLayer, cfg: IndicatorConfig, bars: readonly Candle[]) {
  layer.sync([cfg], bars, bars.length >= 2 ? bars[bars.length - 2].time : null)
}

describe('MarketStructureLayer: inputs derived once per inputs object', () => {
  it('forming-bar ticks and closed bars with the same config derive nothing again', () => {
    const params = vi.mocked(ms.structureParams)
    const key = vi.mocked(ms.structureParamsKey)
    params.mockClear()
    key.mockClear()
    const bars = randomWalk(300, 3)
    const layer = new MarketStructureLayer()
    const cfg = config({ ...BASE })
    syncLive(layer, cfg, bars.slice(0, 200))
    expect(params).toHaveBeenCalledTimes(1)
    expect(key).toHaveBeenCalledTimes(1)
    const view = layer.primitiveOf('a')?.view()
    for (let i = 0; i < 20; i++) {
      const forming = bars[199]
      const tick = { ...forming, close: forming.close + (i % 2 === 0 ? 0.5 : -0.5) }
      syncLive(layer, cfg, [...bars.slice(0, 199), tick])
      expect(layer.primitiveOf('a')?.view()).toBe(view)
    }
    for (let n = 201; n <= 300; n++) syncLive(layer, cfg, bars.slice(0, n))
    expect(params).toHaveBeenCalledTimes(1)
    expect(key).toHaveBeenCalledTimes(1)
    expect(layer.statsOf('a')).toEqual({ recomputes: 1, pushed: 299, count: 299 })
    expect(layer.resultOf('a')).toEqual(
      ms.computeMarketStructure(bars.slice(0, 299), { left: 2, right: 2, maxZones: 3 }),
    )
  })

  it('a new inputs object is re-derived: equal values keep the engine, new params recompute', () => {
    const params = vi.mocked(ms.structureParams)
    params.mockClear()
    const bars = randomWalk(300, 4)
    const layer = new MarketStructureLayer()
    syncLive(layer, config({ ...BASE }), bars)
    const view = layer.primitiveOf('a')?.view()
    // Same values in a fresh object (e.g. a colour edit): re-derived, nothing else changes.
    syncLive(layer, config({ ...BASE }), bars)
    expect(params).toHaveBeenCalledTimes(2)
    expect(layer.statsOf('a')?.recomputes).toBe(1)
    expect(layer.primitiveOf('a')?.view()).toBe(view)
    // A toggle: same engine, new view with the toggle off.
    syncLive(layer, config({ ...BASE, showZones: 0 }), bars)
    expect(layer.statsOf('a')?.recomputes).toBe(1)
    expect(layer.primitiveOf('a')?.view()?.show.zones).toBe(false)
    // A params change: recomputed with the new params.
    syncLive(layer, config({ ...BASE, left: 4, showZones: 0 }), bars)
    expect(layer.statsOf('a')?.recomputes).toBe(2)
    expect(layer.resultOf('a')).toEqual(
      ms.computeMarketStructure(bars.slice(0, -1), { left: 4, right: 2, maxZones: 3 }),
    )
  })

  it('a hidden instance derives nothing; shown again with new inputs it uses them', () => {
    const params = vi.mocked(ms.structureParams)
    const bars = randomWalk(200, 5)
    const layer = new MarketStructureLayer()
    syncLive(layer, config({ ...BASE }), bars)
    params.mockClear()
    syncLive(layer, config({ ...BASE, right: 3 }, false), bars)
    expect(params).not.toHaveBeenCalled()
    syncLive(layer, config({ ...BASE, right: 3 }), bars)
    expect(params).toHaveBeenCalledTimes(1)
    expect(layer.resultOf('a')).toEqual(
      ms.computeMarketStructure(bars.slice(0, -1), { left: 2, right: 3, maxZones: 3 }),
    )
  })
})
