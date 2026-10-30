// Phase 19: IndicatorRunner.prepend (older history put in front) against a full recompute,
// bit for bit, for every registered indicator. Finite windows (SMA, Volume MA, Bollinger,
// Stochastic) splice after their first bar-time re-sum in the old bars, session VWAP and
// anchored VWAP at a state reset, line-less indicators have nothing to splice, and EMA / RSI /
// MACD (infinite memory) always reset.
import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { computeIndicator, defaultInputs } from './compute'
import { LegacyRollingWindow, prng } from './primitives.testkit'
import { INDICATORS, getIndicatorDef } from './registry'
import { IndicatorRunner } from './runner'
import type { IndicatorDef, IndicatorId, IndicatorInputs } from './types'

function liveBar(time: number, close: number, rand: () => number): Candle {
  const open = close + (rand() - 0.5)
  return {
    time,
    open,
    high: Math.max(open, close) + rand(),
    low: Math.min(open, close) - rand(),
    close,
    volume: rand() < 0.1 ? 0 : 1 + rand() * 10,
  }
}

/** n bars of `step` seconds ending just before `end`, with some zero-volume bars. */
function barsBefore(end: number, n: number, rand: () => number, step = 3600, p0 = 100): Candle[] {
  const out: Candle[] = []
  let price = p0
  for (let i = 0; i < n; i++) out.push(liveBar(end - (n - i) * step, (price += rand() - 0.5), rand))
  return out
}

const T0 = 1_700_006_400 + 400 * 3600

const SPLICES: ReadonlySet<IndicatorId> = new Set([
  'sma',
  'volma',
  'bb',
  'stoch',
  'vwap',
  'avwap',
  'vprofile',
  'mstructure',
  'sessions',
])

/** Inputs per case: the defaults plus long or unusual windows. */
function cases(def: IndicatorDef): IndicatorInputs[] {
  const base = defaultInputs(def)
  switch (def.id) {
    case 'sma':
    case 'volma':
      return [base, { period: 1 }, { period: 150 }]
    case 'bb':
      return [base, { period: 1, mult: 2 }, { period: 150, mult: 2.5 }]
    case 'stoch':
      return [base, { kLength: 40, kSmooth: 5, dSmooth: 7 }]
    case 'avwap':
      // Not set; inside the old history; older than the old history.
      return [base, { anchor: T0 + 30 * 3600 }, { anchor: T0 - 50 * 3600 }]
    case 'ema':
    case 'rsi':
      return [base, { period: 3 }]
    default:
      return [base]
  }
}

describe.each(INDICATORS)('IndicatorRunner.prepend ($id)', (def) => {
  const source = def.source ?? 'close'

  for (const inputs of cases(def)) {
    const label = JSON.stringify(inputs)

    it(`prepend-then-splice equals a reset on the full history ${label}`, () => {
      for (const [count, seed] of [
        [1, 1],
        [37, 2],
        [300, 3],
      ] as const) {
        const rand = prng(seed)
        const old = barsBefore(T0 + 300 * 3600, 300, rand)
        const head = barsBefore(T0, count, rand, 3600, 90)
        const r = new IndicatorRunner(def, inputs, source)
        r.reset(old)
        const next = [...head, ...old]
        const res = r.prepend(next, count)
        const expected = computeIndicator(def, next, inputs, source)
        expect(res.output).toEqual(expected)
        expect(r.output).toBe(res.output)
        if (!SPLICES.has(def.id)) expect(res.kind).toBe('reset')
      }
    })

    it(`a prepend with a ticked last bar and appended bars, then live ticks ${label}`, () => {
      const rand = prng(41)
      const old = barsBefore(T0 + 200 * 3600, 200, rand)
      const r = new IndicatorRunner(def, inputs, source)
      r.reset(old)
      const last = old[old.length - 1]
      const tail = [liveBar(last.time, last.close + 1, rand), liveBar(last.time + 3600, 99, rand)]
      let candles = [...barsBefore(T0, 500, rand), ...old.slice(0, -1), ...tail]
      r.prepend(candles, 500)
      let expected = computeIndicator(def, candles, inputs, source)
      expect(r.output).toEqual(expected)
      for (let step = 0; step < 60; step++) {
        const end = candles[candles.length - 1]
        if (rand() < 0.6) candles = [...candles.slice(0, -1), liveBar(end.time, end.close, rand)]
        else candles = [...candles, liveBar(end.time + 3600, end.close + rand() - 0.5, rand)]
        r.update(candles, candles.length - 1)
        expected = computeIndicator(def, candles, inputs, source)
        expect(r.output).toEqual(expected)
      }
    })

    it(`repeated history pages keep matching a full recompute ${label}`, () => {
      const rand = prng(43)
      let candles = barsBefore(T0 + 100 * 3600, 100, rand)
      const r = new IndicatorRunner(def, inputs, source)
      r.reset(candles)
      for (let page = 0; page < 5; page++) {
        const count = 20 + Math.floor(rand() * 120)
        candles = [...barsBefore(candles[0].time, count, rand), ...candles]
        r.prepend(candles, count)
        const expected = computeIndicator(def, candles, inputs, source)
        expect(r.output).toEqual(expected)
      }
    })

    it(`no look-ahead through a splice ${label}`, () => {
      const rand = prng(47)
      const old = barsBefore(T0 + 300 * 3600, 300, rand)
      const head = barsBefore(T0, 200, rand)
      const next = [...head, ...old]
      for (const t of [50, 199, 200, 260, 480]) {
        // Change every bar after t in both the old history and the prepended one.
        const f = (c: Candle, i: number): Candle =>
          i <= t ? c : { ...c, close: c.close * 1.7, high: c.high * 1.8, volume: c.volume * 3 }
        const a = new IndicatorRunner(def, inputs, source)
        a.reset(old)
        const outA = a.prepend(next, 200).output
        const changed = next.map(f)
        const b = new IndicatorRunner(def, inputs, source)
        b.reset(changed.slice(200))
        const outB = b.prepend(changed, 200).output
        const cut = next[t].time
        for (const line of def.lines) {
          expect(outB[line.key].filter((p) => p.time <= cut)).toEqual(
            outA[line.key].filter((p) => p.time <= cut),
          )
        }
      }
    })
  }
})

describe('IndicatorRunner.prepend splices only where it is exact or bounded', () => {
  const rand = prng(51)
  const old = barsBefore(T0 + 400 * 3600, 400, rand)
  const head = barsBefore(T0, 100, rand)
  const next = [...head, ...old]

  it.each([
    ['sma', { period: 20 }, 'splice'],
    ['volma', { period: 20 }, 'splice'],
    ['bb', { period: 20, mult: 2 }, 'splice'],
    ['stoch', { kLength: 14, kSmooth: 1, dSmooth: 3 }, 'splice'],
    ['vwap', {}, 'splice'],
    ['avwap', { anchor: 0 }, 'splice'],
    ['avwap', { anchor: T0 + 30 * 3600 }, 'splice'],
    ['vprofile', defaultInputs(getIndicatorDef('vprofile') as IndicatorDef), 'splice'],
    ['mstructure', defaultInputs(getIndicatorDef('mstructure') as IndicatorDef), 'splice'],
    ['sessions', defaultInputs(getIndicatorDef('sessions') as IndicatorDef), 'splice'],
    ['ema', { period: 20 }, 'reset'],
    ['rsi', { period: 14 }, 'reset'],
    ['macd', { fast: 12, slow: 26, signal: 9 }, 'reset'],
  ] as const)('%s %o -> %s', (id, inputs, kind) => {
    const def = getIndicatorDef(id) as IndicatorDef
    const r = new IndicatorRunner(def, inputs, def.source ?? 'close')
    r.reset(old)
    expect(r.prepend(next, 100).kind).toBe(kind)
  })

  it('anchored VWAP with the anchor older than the old history resets (values appear)', () => {
    const def = getIndicatorDef('avwap') as IndicatorDef
    const inputs = { anchor: T0 - 50 * 3600 }
    const r = new IndicatorRunner(def, inputs, 'hlc3')
    r.reset(old)
    expect(r.output.avwap).toEqual([])
    const res = r.prepend(next, 100)
    expect(res.kind).toBe('reset')
    expect(res.output.avwap.length).toBeGreaterThan(0)
    expect(res.output).toEqual(computeIndicator(def, next, inputs, 'hlc3'))
  })

  it('a window longer than the old history resets', () => {
    const def = getIndicatorDef('sma') as IndicatorDef
    const r = new IndicatorRunner(def, { period: 450 }, 'close')
    r.reset(old)
    const res = r.prepend(next, 100)
    expect(res.kind).toBe('reset')
    expect(res.output).toEqual(computeIndicator(def, next, { period: 450 }, 'close'))
  })

  it('VWAP on 1m bars splices at the first UTC midnight of the old history, bit for bit', () => {
    const def = getIndicatorDef('vwap') as IndicatorDef
    const r1 = prng(52)
    const end = 1_700_006_400 + 3 * 86_400 + 600 * 60
    const oldMin = barsBefore(end, 3_000, r1, 60)
    const all = [...barsBefore(oldMin[0].time, 1_000, r1, 60), ...oldMin]
    const r = new IndicatorRunner(def, {}, 'close')
    r.reset(oldMin)
    const res = r.prepend(all, 1_000)
    expect(res.kind).toBe('splice')
    expect(res.output).toEqual(computeIndicator(def, all, {}, 'close'))
  })

  it('falls back to a reset when the state does not line up', () => {
    const def = getIndicatorDef('sma') as IndicatorDef
    const inputs = { period: 20 }
    const fresh = (c: readonly Candle[]) => computeIndicator(def, c, inputs, 'close')
    const r = new IndicatorRunner(def, inputs, 'close')
    // No reset yet.
    expect(r.prepend(next, 100).kind).toBe('reset')
    // Wrong count (old first bar not at `count`), zero, negative, fractional, too large.
    for (const count of [99, 101, 0, -1, 2.5, next.length]) {
      r.reset(old)
      const res = r.prepend(next, count)
      expect(res.kind).toBe('reset')
      expect(res.output).toEqual(fresh(next))
    }
    // The old history changed under the runner (a different bar at the checkpoint).
    r.reset(old)
    const edited = next.map((c, i) => (i === next.length - 2 ? { ...c, time: c.time + 1 } : c))
    expect(r.prepend(edited, 100).kind).toBe('reset')
    // A one-bar old history has no checkpoint bar to keep.
    r.reset(old.slice(0, 1))
    expect(r.prepend([...head, old[0]], 100).kind).toBe('reset')
  })
})

describe('a 1000-bar prepend on a 150k history', () => {
  /** Minute bars around 30k with 0.2% moves. */
  function minutes(n: number, t0: number, seed: number): Candle[] {
    const rand = prng(seed)
    const out: Candle[] = []
    let p = 30_000
    for (let i = 0; i < n; i++) {
      const open = p
      p *= 1 + (rand() - 0.5) * 0.004
      const high = Math.max(open, p) * 1.0005
      const low = Math.min(open, p) * 0.9995
      out.push({ time: t0 + i * 60, open, high, low, close: p, volume: 1 + rand() * 9 })
    }
    return out
  }
  const all = minutes(151_000, 1_600_000_000, 61)
  const old = all.slice(1_000)

  it.each([
    ['sma', { period: 500 }],
    ['bb', { period: 500, mult: 2 }],
    ['volma', { period: 20 }],
    ['stoch', { kLength: 14, kSmooth: 3, dSmooth: 3 }],
    ['vwap', {}],
  ] as const)(
    '%s %o splices instead of resetting, and matches the full recompute',
    (id, inputs) => {
      const def = getIndicatorDef(id) as IndicatorDef
      const source = def.source ?? 'close'
      const r = new IndicatorRunner(def, inputs, source)
      r.reset(old)
      const res = r.prepend(all, 1_000)
      expect(res.kind).toBe('splice')
      const expected = computeIndicator(def, all, inputs, source)
      expect(res.output).toEqual(expected)
    },
  )
})

describe('IndicatorRunner.prepend with gaps in the bar times', () => {
  /** Hourly bars with random outages (1 to 40 missing bars), so bar-time re-sums are irregular. */
  function gappy(n: number, end: number, rand: () => number): Candle[] {
    const times: number[] = []
    let t = end
    for (let i = 0; i < n; i++) {
      t -= 3600 * (rand() < 0.05 ? 1 + Math.floor(rand() * 40) : 1)
      times.push(t)
    }
    times.reverse()
    let p = 100
    return times.map((time) => liveBar(time, (p += rand() - 0.5), rand))
  }

  it.each(INDICATORS.map((d) => [d.id, d] as const))('%s matches a full recompute', (_id, def) => {
    const rand = prng(71)
    const source = def.source ?? 'close'
    for (const inputs of cases(def)) {
      const old = gappy(1_500, T0 + 3_000 * 3600, rand)
      const all = [...gappy(700, old[0].time, rand), ...old]
      const r = new IndicatorRunner(def, inputs, source)
      r.reset(old)
      r.prepend(all, 700)
      expect(r.output).toEqual(computeIndicator(def, all, inputs, source))
    }
  })
})

describe('IndicatorRunner.prepend through a running-sum mean guard re-sum', () => {
  // A huge print (close, high and volume 1.23e9) in thin, cheap bars trips the mean guard when
  // it leaves a window, at a bar that depends only on where the spike is. With the spike in the
  // spliced head only the new run re-sums there; with it in the old bars both runs do, from
  // different states. Either way the splice must equal a reset on the full history.
  const SPIKE = 1.23e9

  /** n hourly bars ending before `end`: closes near `p0`, volume 0.000123 .. 0.001123. */
  function thin(end: number, n: number, rand: () => number, p0: number): Candle[] {
    const out: Candle[] = []
    let p = p0
    for (let i = 0; i < n; i++) {
      const open = p
      p *= 1 + (rand() - 0.5) * 0.004
      out.push({
        time: end - (n - i) * 3600,
        open,
        high: Math.max(open, p) * 1.001,
        low: Math.min(open, p) * 0.999,
        close: p,
        volume: 0.000123 + rand() * 0.001,
      })
    }
    return out
  }

  function spiked(c: Candle): Candle {
    return { ...c, close: SPIKE, high: SPIKE, volume: SPIKE }
  }

  /** Close 60k crashing to 1e-4 over `steps` bars from index `at` (volume crashes with it). */
  function crashed(bars: Candle[], at: number, steps: number): Candle[] {
    const f = Math.pow(1e-4 / 60_000, 1 / steps)
    let scale = 60_000 / bars[0].close
    return bars.map((c, i) => {
      if (i >= at && i < at + steps) scale *= f
      return {
        ...c,
        open: c.open * scale,
        high: c.high * scale,
        low: c.low * scale,
        close: c.close * scale,
        volume: c.volume * scale * 1e6,
      }
    })
  }

  const COUNT = 120
  const OLD = 400
  const rand = prng(91)
  const old0 = thin(T0 + OLD * 3600, OLD, rand, 100)
  const head0 = thin(T0, COUNT, rand, 100)
  const plain = [...head0, ...old0]

  // Indices into the full history [head, old]: in the head (early, last head bar, the bar whose
  // exit can coincide with the splice point), and in the old bars (before and after the splice
  // point, and the last, still-forming bar).
  const SPIKES = [5, 60, COUNT - 1, COUNT - 20, COUNT + 3, COUNT + 25, COUNT + 250, COUNT + OLD - 1]

  const RUNS: [IndicatorId, IndicatorInputs][] = [
    ['sma', { period: 20 }],
    ['sma', { period: 2 }],
    ['sma', { period: 150 }],
    ['volma', { period: 20 }],
    ['volma', { period: 3 }],
    ['volma', { period: 150 }],
    ['bb', { period: 20, mult: 2 }],
    ['bb', { period: 150, mult: 2.5 }],
    ['stoch', { kLength: 14, kSmooth: 3, dSmooth: 3 }],
    ['stoch', { kLength: 40, kSmooth: 5, dSmooth: 7 }],
  ]

  function check(def: IndicatorDef, inputs: IndicatorInputs, next: Candle[]): void {
    const source = def.source ?? 'close'
    const r = new IndicatorRunner(def, inputs, source)
    r.reset(next.slice(COUNT))
    const res = r.prepend(next, COUNT)
    expect(res.kind).toBe('splice')
    expect(res.output).toEqual(computeIndicator(def, next, inputs, source))
    // Then live ticks through the spike's own window.
    let candles = next
    const tickRand = prng(next.length)
    for (let step = 0; step < 30; step++) {
      const end = candles[candles.length - 1]
      const bar = liveBar(end.time + 3600, end.close, tickRand)
      const fresh = step === 3 ? spiked(bar) : { ...bar, volume: 0.000123 + tickRand() * 0.001 }
      candles = [...candles, fresh]
      r.update(candles, candles.length - 1)
      expect(r.output).toEqual(computeIndicator(def, candles, inputs, source))
    }
  }

  it.each(RUNS)('%s %o: a spike inside or outside the head splices bit for bit', (id, inputs) => {
    const def = getIndicatorDef(id) as IndicatorDef
    for (const at of SPIKES)
      check(
        def,
        inputs,
        plain.map((c, i) => (i === at ? spiked(c) : c)),
      )
  })

  it.each(RUNS)('%s %o: a 60k -> 1e-4 crash inside or outside the head', (id, inputs) => {
    const def = getIndicatorDef(id) as IndicatorDef
    for (const [at, steps] of [
      [30, 1],
      [COUNT - 2, 3],
      [COUNT + 10, 1],
      [COUNT + 200, 8],
    ] as const) {
      check(def, inputs, crashed(plain, at, steps))
    }
  })

  it('the guard fires: Volume MA after the spike leaves keeps ~1e-10 of the fresh mean', () => {
    // Not vacuous: without the guard this mean keeps only ~4 significant digits.
    const def = getIndicatorDef('volma') as IndicatorDef
    const next = plain.map((c, i) => (i === COUNT - 1 ? spiked(c) : c))
    const r = new IndicatorRunner(def, { period: 20 }, 'close')
    r.reset(next.slice(COUNT))
    const out = r.prepend(next, COUNT).output.ma
    const legacy = new LegacyRollingWindow(20)
    let checked = 0
    for (const c of next) {
      legacy.push(c.volume)
      if (!legacy.full) continue
      const p = out.find((q) => q.time === c.time)
      expect(p).toBeDefined()
      if (legacy.values.every((v) => v < 1)) {
        expect(Math.abs((p as { value: number }).value - legacy.mean())).toBeLessThanOrEqual(
          1e-10 * legacy.mean(),
        )
        checked++
      }
    }
    expect(checked).toBeGreaterThan(400)
  })
})
