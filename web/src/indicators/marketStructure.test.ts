import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { computeIndicator, defaultInputs } from './compute'
import {
  EXPECTED_2_2_MAX10,
  EXPECTED_2_2_MAX2,
  EXPECTED_DEFAULTS,
  FIXTURE_CANDLES,
} from './marketStructure.fixture'
import {
  CASE_BREAKS_BEFORE_NEW_SWINGS,
  CASE_BREAK_TIE_HIGH,
  CASE_BREAK_TIE_LOW,
  CASE_CLOSE_NOT_WICK_HIGH,
  CASE_CLOSE_NOT_WICK_LOW,
  CASE_DROP_WHEN_FULL,
  CASE_EQUAL_HIGHS_APART,
  CASE_INVALIDATION_FREES_SLOT,
  CASE_INVALIDATION_TIE_RESISTANCE,
  CASE_INVALIDATION_TIE_SUPPORT,
  CASE_OUTSIDE_BAR_BOTH,
  CASE_RETEST_TIE_RESISTANCE,
  CASE_RETEST_TIE_SUPPORT,
  CASE_ZERO_HEIGHT_ZONE,
  RULE_CASES,
  rt,
  type RuleCase,
} from './marketStructure.rules.fixture'
import {
  MARKET_STRUCTURE_SHOW_KEYS,
  MarketStructureEngine,
  computeMarketStructure,
  marketStructureDef,
  structureParams,
  structureParamsKey,
  type MarketStructureParams,
} from './marketStructure'
import type { FlipZone, MarketStructure, StructureState } from './types'

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

/**
 * Seeded random walk on a coarse tick grid (prices are multiples of `tick`), so equal highs,
 * equal lows and closes exactly on a level all happen often.
 */
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

/** Replace every bar after index `t` with fresh random OHLC (valid candles, same times). */
function perturbAfter(bars: readonly Candle[], t: number, seed: number): Candle[] {
  const rnd = mulberry32(seed)
  return bars.map((b, i) => {
    if (i <= t) return b
    const open = Math.round(50 + rnd() * 100)
    const close = Math.round(50 + rnd() * 100)
    const high = Math.max(open, close) + Math.round(rnd() * 10)
    const low = Math.min(open, close) - Math.round(rnd() * 10)
    return { time: b.time, open, high, low, close, volume: rnd() * 5 }
  })
}

/** What the full result says was known at the close of the bar with open time `t`. */
function asOf(r: MarketStructure, t: number): MarketStructure {
  const events = r.events.filter((e) => e.time <= t)
  const last = events.length > 0 ? events[events.length - 1] : undefined
  const state: StructureState = last === undefined ? 'none' : last.direction
  return {
    swings: r.swings
      .filter((s) => s.confirmTime <= t)
      .map((s) => ({
        ...s,
        brokenTime: s.brokenTime !== null && s.brokenTime <= t ? s.brokenTime : null,
      })),
    events: events.map((e) => ({ ...e })),
    zones: r.zones
      .filter((z) => z.createdTime <= t)
      .map((z): FlipZone => {
        const ended = z.endTime !== null && z.endTime <= t
        return {
          ...z,
          retestTime: z.retestTime !== null && z.retestTime <= t ? z.retestTime : null,
          endTime: ended ? z.endTime : null,
          endReason: ended ? z.endReason : null,
        }
      }),
    state,
  }
}

const P22_2: MarketStructureParams = { left: 2, right: 2, maxZones: 2 }
const P22_10: MarketStructureParams = { left: 2, right: 2, maxZones: 10 }
const DEFAULTS: MarketStructureParams = { left: 5, right: 5, maxZones: 10 }

describe('market structure fixture (hand-worked known values)', () => {
  it('the fixture candles are valid OHLC bars, ascending in time', () => {
    for (let i = 0; i < FIXTURE_CANDLES.length; i++) {
      const c = FIXTURE_CANDLES[i]
      expect(c.high).toBeGreaterThanOrEqual(Math.max(c.open, c.close))
      expect(c.low).toBeLessThanOrEqual(Math.min(c.open, c.close))
      if (i > 0) expect(c.time).toBeGreaterThan(FIXTURE_CANDLES[i - 1].time)
    }
    expect(FIXTURE_CANDLES).toHaveLength(44)
  })

  it('left 2, right 2, maxZones 2: every swing, event and zone, and the final state', () => {
    const r = computeMarketStructure(FIXTURE_CANDLES, P22_2)
    expect(r.swings).toEqual(EXPECTED_2_2_MAX2.swings)
    expect(r.events).toEqual(EXPECTED_2_2_MAX2.events)
    expect(r.zones).toEqual(EXPECTED_2_2_MAX2.zones)
    expect(r.state).toBe('down')
    expect(r).toEqual(EXPECTED_2_2_MAX2)
  })

  it('left 2, right 2, maxZones 10: nothing is dropped', () => {
    expect(computeMarketStructure(FIXTURE_CANDLES, P22_10)).toEqual(EXPECTED_2_2_MAX10)
  })

  it('defaults (5 / 5 / 10) through the indicator definition', () => {
    const inputs = defaultInputs(marketStructureDef)
    expect(structureParams(inputs)).toEqual(DEFAULTS)
    expect(marketStructureDef.structure?.(FIXTURE_CANDLES, inputs)).toEqual(EXPECTED_DEFAULTS)
  })

  it('covers each named case', () => {
    const r = EXPECTED_2_2_MAX2
    const pivots = (kind: 'high' | 'low') =>
      r.swings.filter((s) => s.kind === kind).map((s) => s.pivotTime)
    const at = (i: number) => FIXTURE_CANDLES[i].time
    // Equal-high pair 9/10 and equal-low pair 5/6: the earlier bar is the pivot (strict-left tie
    // blocks the later one).
    expect(FIXTURE_CANDLES[9].high).toBe(FIXTURE_CANDLES[10].high)
    expect(pivots('high')).toContain(at(9))
    expect(pivots('high')).not.toContain(at(10))
    expect(FIXTURE_CANDLES[5].low).toBe(FIXTURE_CANDLES[6].low)
    expect(pivots('low')).toContain(at(5))
    expect(pivots('low')).not.toContain(at(6))
    // First break from 'none' is a BOS; ChoCh both ways; BOS both ways.
    expect(r.events[0].kind).toBe('bos')
    const kinds = r.events.map((e) => `${e.kind}-${e.direction}`)
    expect(kinds).toEqual(['bos-down', 'choch-up', 'bos-up', 'choch-down', 'bos-down'])
    // Superseded unbroken swing low@28 (8.9): bars 38 and 39 close below it, no event there.
    const low28 = r.swings.find((s) => s.kind === 'low' && s.pivotTime === at(28))
    expect(low28?.brokenTime).toBeNull()
    expect(FIXTURE_CANDLES[38].close).toBeLessThan(8.9)
    expect(FIXTURE_CANDLES[39].close).toBeLessThan(8.9)
    expect(r.events.some((e) => e.time === at(38) || e.time === at(39))).toBe(false)
    // Retest and invalidation on the same bar (Z3), plain invalidation (Z1), dropped (Z2).
    expect(r.zones[2].retestTime).toBe(at(34))
    expect(r.zones[2].endTime).toBe(at(34))
    expect(r.zones[0].retestTime).toBeLessThan(r.zones[0].endTime ?? 0)
    expect(r.zones[1].endReason).toBe('dropped')
  })
})

function run(c: RuleCase): MarketStructure {
  return computeMarketStructure(c.candles, c.params)
}

function eventTimes(r: MarketStructure): number[] {
  return r.events.map((e) => e.time)
}

describe('market structure rule cases (hand-worked, one rule each)', () => {
  it('every case has valid OHLC bars, ascending in time', () => {
    expect(RULE_CASES).toHaveLength(14)
    expect(new Set(RULE_CASES.map((c) => c.name)).size).toBe(RULE_CASES.length)
    for (const c of RULE_CASES) {
      c.candles.forEach((b, i) => {
        expect(b.high).toBeGreaterThanOrEqual(Math.max(b.open, b.close))
        expect(b.low).toBeLessThanOrEqual(Math.min(b.open, b.close))
        expect(b.time).toBe(rt(i))
      })
    }
  })

  for (const c of RULE_CASES) {
    it(`${c.name}: ${c.rule} (full result, and bar-by-bar engine)`, () => {
      expect(run(c)).toEqual(c.expected)
      const engine = new MarketStructureEngine(c.params)
      for (const b of c.candles) engine.push(b)
      expect(structuredClone(engine.result)).toEqual(c.expected)
    })
  }

  it('1. a wick through a swing is not a break; the later close is', () => {
    const hi = CASE_CLOSE_NOT_WICK_HIGH
    expect(hi.candles[3].high).toBeGreaterThan(12)
    expect(hi.candles[3].close).toBeLessThanOrEqual(12)
    expect(eventTimes(run(hi))).toEqual([rt(4)])
    const lo = CASE_CLOSE_NOT_WICK_LOW
    expect(lo.candles[3].low).toBeLessThan(8)
    expect(lo.candles[3].close).toBeGreaterThanOrEqual(8)
    expect(eventTimes(run(lo))).toEqual([rt(4)])
  })

  it('2. a close exactly on the swing price is not a break', () => {
    expect(CASE_BREAK_TIE_HIGH.candles[3].close).toBe(12)
    expect(eventTimes(run(CASE_BREAK_TIE_HIGH))).toEqual([rt(4)])
    expect(CASE_BREAK_TIE_LOW.candles[3].close).toBe(8)
    expect(eventTimes(run(CASE_BREAK_TIE_LOW))).toEqual([rt(4)])
  })

  it('3. touching a zone edge exactly is a retest', () => {
    const s = run(CASE_RETEST_TIE_SUPPORT).zones[0]
    expect(CASE_RETEST_TIE_SUPPORT.candles[5].low).toBe(s.top)
    expect(s.retestTime).toBe(rt(5))
    const r = run(CASE_RETEST_TIE_RESISTANCE).zones[0]
    expect(CASE_RETEST_TIE_RESISTANCE.candles[5].high).toBe(r.bottom)
    expect(r.retestTime).toBe(rt(5))
  })

  it('4. a close exactly on the far edge does not invalidate', () => {
    const s = run(CASE_INVALIDATION_TIE_SUPPORT).zones[0]
    expect(CASE_INVALIDATION_TIE_SUPPORT.candles[4].close).toBe(s.bottom)
    expect(s.endTime).toBe(rt(5))
    const r = run(CASE_INVALIDATION_TIE_RESISTANCE).zones[0]
    expect(CASE_INVALIDATION_TIE_RESISTANCE.candles[4].close).toBe(r.top)
    expect(r.endTime).toBe(rt(5))
  })

  it('5. breaks on bar t use swings confirmed before t, not the one confirmed at t', () => {
    const r = run(CASE_BREAKS_BEFORE_NEW_SWINGS)
    expect(r.events).toHaveLength(1)
    expect(r.events[0].level).toBe(10)
    expect(r.events[0].time).toBe(rt(4))
    expect(r.swings[1].confirmTime).toBe(rt(4))
    expect(r.swings[1].brokenTime).toBeNull()
  })

  it('6. no bar ever carries two events (H below L with both unbroken cannot happen)', () => {
    let events = 0
    for (let seed = 1; seed <= 40; seed++) {
      for (const tick of [0.5, 1]) {
        const bars = randomWalk(1500, 300 + seed, tick)
        for (const [left, right] of [
          [1, 1],
          [2, 1],
          [1, 3],
        ]) {
          const r = computeMarketStructure(bars, { left, right, maxZones: 1 })
          events += r.events.length
          expect(new Set(eventTimes(r)).size).toBe(r.events.length)
          for (const z of r.zones) expect(z.endTime).not.toBe(z.createdTime)
        }
      }
    }
    expect(events).toBeGreaterThan(1000)
  })

  it('7. invalidation runs before zone creation and the maxZones drop', () => {
    const freed = run(CASE_INVALIDATION_FREES_SLOT).zones
    expect(freed[0].endReason).toBe('invalidated')
    expect(freed[0].endTime).toBe(rt(7))
    expect(freed[1].createdTime).toBe(rt(7))
    expect(freed[1].retestTime).toBeNull()
    const full = run(CASE_DROP_WHEN_FULL).zones
    expect(full[0].endReason).toBe('dropped')
    expect(full[0].endTime).toBe(rt(7))
  })

  it('8. an outside bar is both a swing high and a swing low, high recorded first', () => {
    const r = run(CASE_OUTSIDE_BAR_BOTH)
    expect(r.swings.slice(0, 2).map((s) => [s.kind, s.pivotTime])).toEqual([
      ['high', rt(1)],
      ['low', rt(1)],
    ])
    expect(r.zones.map((z) => z.pivotTime)).toEqual([rt(1), rt(1)])
  })

  it('9. a zero-height zone is kept and follows the retest / invalidation rules', () => {
    const z = run(CASE_ZERO_HEIGHT_ZONE).zones
    expect(z).toHaveLength(1)
    expect(z[0].top).toBe(z[0].bottom)
    expect(z[0].retestTime).toBe(rt(4))
    expect(z[0].endTime).toBe(rt(6))
  })

  it('10. equal highs further apart than left bars are both pivots', () => {
    expect(run(CASE_EQUAL_HIGHS_APART).swings.map((s) => [s.price, s.pivotTime])).toEqual([
      [12, rt(1)],
      [12, rt(3)],
    ])
  })
})

describe('MarketStructureEngine', () => {
  it('pushing one bar at a time equals the full computation at every prefix (fixture)', () => {
    for (const params of [P22_2, P22_10, DEFAULTS]) {
      const engine = new MarketStructureEngine(params)
      for (let i = 0; i < FIXTURE_CANDLES.length; i++) {
        engine.push(FIXTURE_CANDLES[i])
        expect(engine.count).toBe(i + 1)
        const full = computeMarketStructure(FIXTURE_CANDLES.slice(0, i + 1), params)
        expect(structuredClone(engine.result)).toEqual(full)
      }
    }
  })

  it('pushing one bar at a time equals the full computation at every prefix (random walk)', () => {
    const bars = randomWalk(400, 7)
    for (const params of [
      { left: 2, right: 2, maxZones: 3 },
      { left: 3, right: 1, maxZones: 1 },
      DEFAULTS,
    ]) {
      const engine = new MarketStructureEngine(params)
      for (let i = 0; i < bars.length; i++) {
        engine.push(bars[i])
        if (i % 7 !== 0 && i !== bars.length - 1) continue
        const full = computeMarketStructure(bars.slice(0, i + 1), params)
        expect(structuredClone(engine.result)).toEqual(full)
      }
    }
  })

  it('the live result object is stable and updated in place', () => {
    const engine = new MarketStructureEngine(P22_2)
    const live = engine.result
    for (const c of FIXTURE_CANDLES) engine.push(c)
    expect(engine.result).toBe(live)
    expect(live).toEqual(EXPECTED_2_2_MAX2)
  })
})

describe('market structure: no look-ahead', () => {
  it('changing every bar after t leaves every output dated <= t unchanged', () => {
    const paramSets: MarketStructureParams[] = [
      { left: 2, right: 2, maxZones: 3 },
      { left: 1, right: 3, maxZones: 2 },
      DEFAULTS,
    ]
    for (const [k, params] of paramSets.entries()) {
      const bars = randomWalk(300, 100 + k)
      const rnd = mulberry32(900 + k)
      for (let trial = 0; trial < 40; trial++) {
        const ti = Math.floor(rnd() * bars.length)
        const t = bars[ti].time
        const prefix = computeMarketStructure(bars.slice(0, ti + 1), params)
        const perturbed = perturbAfter(bars, ti, 5000 + trial)
        const full = computeMarketStructure(bars, params)
        const fullPerturbed = computeMarketStructure(perturbed, params)
        expect(asOf(fullPerturbed, t)).toEqual(prefix)
        expect(asOf(full, t)).toEqual(prefix)
      }
    }
  })

  it('on the fixture: asOf(full, t) equals the prefix result for every t', () => {
    for (const params of [P22_2, DEFAULTS]) {
      const full = computeMarketStructure(FIXTURE_CANDLES, params)
      for (let i = 0; i < FIXTURE_CANDLES.length; i++) {
        const prefix = computeMarketStructure(FIXTURE_CANDLES.slice(0, i + 1), params)
        expect(asOf(full, FIXTURE_CANDLES[i].time)).toEqual(prefix)
      }
    }
  })
})

describe('market structure: no marker before its confirmation bar', () => {
  function checkOrdering(bars: readonly Candle[], params: MarketStructureParams): void {
    const index = new Map(bars.map((b, i) => [b.time, i]))
    const r = computeMarketStructure(bars, params)
    for (const s of r.swings) {
      const pi = index.get(s.pivotTime)
      const ci = index.get(s.confirmTime)
      expect(pi).toBeDefined()
      expect(ci).toBeDefined()
      expect((ci ?? 0) - (pi ?? 0)).toBe(params.right)
      if (s.brokenTime !== null) expect(s.brokenTime).toBeGreaterThan(s.confirmTime)
    }
    for (const e of r.events) {
      const kind = e.direction === 'up' ? 'high' : 'low'
      const s = r.swings.find((x) => x.kind === kind && x.pivotTime === e.swingPivotTime)
      expect(s).toBeDefined()
      expect(e.time).toBeGreaterThan(s?.confirmTime ?? Number.POSITIVE_INFINITY)
      expect(s?.brokenTime).toBe(e.time)
      expect(e.level).toBe(s?.price)
    }
    for (const z of r.zones) {
      expect(z.top).toBeGreaterThanOrEqual(z.bottom)
      expect(r.events.some((e) => e.time === z.createdTime)).toBe(true)
      if (z.retestTime !== null) expect(z.retestTime).toBeGreaterThan(z.createdTime)
      if (z.endTime !== null) {
        // A zone is never ended on its own creation bar: two breaks on one bar cannot happen
        // (see marketStructure.rules.fixture.ts, case 6, and the README).
        expect(z.endTime).toBeGreaterThan(z.createdTime)
        expect(z.endReason).not.toBeNull()
      } else {
        expect(z.endReason).toBeNull()
      }
    }
    const active = r.zones.filter((z) => z.endTime === null).length
    expect(active).toBeLessThanOrEqual(params.maxZones)
    // Sorted by their main time.
    for (let i = 1; i < r.swings.length; i++) {
      expect(r.swings[i].confirmTime).toBeGreaterThanOrEqual(r.swings[i - 1].confirmTime)
    }
    for (let i = 1; i < r.events.length; i++) {
      expect(r.events[i].time).toBeGreaterThanOrEqual(r.events[i - 1].time)
    }
    for (let i = 1; i < r.zones.length; i++) {
      expect(r.zones[i].createdTime).toBeGreaterThanOrEqual(r.zones[i - 1].createdTime)
    }
  }

  it('holds on the fixture and on random walks', () => {
    for (const params of [P22_2, P22_10, DEFAULTS]) checkOrdering(FIXTURE_CANDLES, params)
    for (let seed = 1; seed <= 5; seed++) {
      const bars = randomWalk(2000, seed)
      checkOrdering(bars, { left: 2, right: 2, maxZones: 2 })
      checkOrdering(bars, { left: 3, right: 5, maxZones: 4 })
      checkOrdering(bars, { left: 1, right: 1, maxZones: 1 })
      checkOrdering(bars, DEFAULTS)
    }
  })

  it('random walks exercise every kind of output', () => {
    const r = computeMarketStructure(randomWalk(2000, 3), { left: 2, right: 2, maxZones: 2 })
    const reasons = new Set(r.zones.map((z) => z.endReason))
    expect(reasons.has('invalidated')).toBe(true)
    expect(reasons.has('dropped')).toBe(true)
    expect(r.events.some((e) => e.kind === 'choch')).toBe(true)
    expect(r.events.some((e) => e.kind === 'bos')).toBe(true)
  })
})

describe('market structure definition and params', () => {
  it('declares the inputs in order with their defaults and bounds', () => {
    const d = marketStructureDef
    expect(d.id).toBe('mstructure')
    expect(d.placement).toBe('overlay')
    expect(d.source).toBeNull()
    expect(d.lines).toEqual([])
    expect(d.guides).toEqual([])
    expect(d.inputs.map((i) => i.key)).toEqual([
      'left',
      'right',
      'maxZones',
      ...MARKET_STRUCTURE_SHOW_KEYS,
    ])
    expect(d.inputs.slice(0, 3).map((i) => [i.label, i.default, i.min, i.max])).toEqual([
      ['Left bars', 5, 1, 50],
      ['Right bars', 5, 1, 50],
      ['Max active zones', 10, 1, 50],
    ])
    for (const i of d.inputs.slice(3)) {
      expect(i).toMatchObject({
        kind: 'toggle',
        default: 1,
        min: 0,
        max: 1,
        step: 1,
        integer: true,
      })
    }
    expect(d.inputs.slice(3).map((i) => i.label)).toEqual(['Swings', 'BOS', 'ChoCh', 'Flip zones'])
  })

  it('the stepper returns {} and clones into a fresh stepper; no per-bar lines', () => {
    const s = marketStructureDef.createStepper(defaultInputs(marketStructureDef), 'close')
    expect(s.next(FIXTURE_CANDLES[0])).toEqual({})
    const c = s.clone()
    expect(c).not.toBe(s)
    expect(c.next(FIXTURE_CANDLES[1])).toEqual({})
    expect(
      computeIndicator(marketStructureDef, FIXTURE_CANDLES, defaultInputs(marketStructureDef)),
    ).toEqual({})
  })

  it('the params key ignores the show toggles', () => {
    const base = defaultInputs(marketStructureDef)
    const hidden = { ...base, showSwings: 0, showBos: 0, showChoch: 0, showZones: 0 }
    expect(structureParamsKey(structureParams(hidden))).toBe(
      structureParamsKey(structureParams(base)),
    )
    expect(structureParamsKey(structureParams({ ...base, left: 3 }))).not.toBe(
      structureParamsKey(structureParams(base)),
    )
    expect(structureParamsKey(structureParams({ ...base, maxZones: 4 }))).not.toBe(
      structureParamsKey(structureParams(base)),
    )
  })

  it('an empty or short history gives an empty result', () => {
    expect(computeMarketStructure([], DEFAULTS)).toEqual({
      swings: [],
      events: [],
      zones: [],
      state: 'none',
    })
    expect(computeMarketStructure(FIXTURE_CANDLES.slice(0, 10), DEFAULTS).swings).toEqual([])
  })
})

describe('market structure performance', () => {
  it('computes 100k bars at defaults well under 1.5 s', () => {
    const bars = randomWalk(100_000, 42, 0.01)
    computeMarketStructure(bars.slice(0, 5000), DEFAULTS) // warm up the JIT
    const t0 = performance.now()
    const r = computeMarketStructure(bars, DEFAULTS)
    const ms = performance.now() - t0
    expect(r.swings.length).toBeGreaterThan(0)
    expect(ms).toBeLessThan(1500)
  })
})
