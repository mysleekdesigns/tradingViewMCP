import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { computeIndicator, defaultInputs } from './compute'
import {
  D1,
  EXPECTED_AS_OF_DEFAULTS,
  EXPECTED_ASIA_WRAP,
  EXPECTED_DEFAULTS,
  EXPECTED_OPENING_3,
  FIXTURE_CANDLES,
  h1,
  h2,
} from './sessionRanges.fixture'
import {
  SESSION_KEYS,
  SESSION_RANGES_DEFAULTS,
  SESSION_SHOW_KEYS,
  SessionRangesEngine,
  alignedSessions,
  computeSessionRanges,
  sessionAligned,
  sessionDurationHours,
  sessionParams,
  sessionParamsKey,
  sessionRangesDef,
  type SessionRangesParams,
  type SessionWindow,
} from './sessionRanges'
import type { SessionBox, SessionKey, SessionRanges } from './types'

const DAY = 86_400
const HOUR = 3_600
const DEFAULTS: SessionRangesParams = {
  asia: { ...SESSION_RANGES_DEFAULTS.asia },
  london: { ...SESSION_RANGES_DEFAULTS.london },
  newyork: { ...SESSION_RANGES_DEFAULTS.newyork },
  openingHours: SESSION_RANGES_DEFAULTS.openingHours,
}

function withParams(over: Partial<SessionRangesParams>): SessionRangesParams {
  return { ...DEFAULTS, ...over }
}

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

/** Hour-aligned (and 4h-aligned) start that is not a UTC midnight. */
const T0 = 1_700_006_400

/**
 * Seeded random walk on a 0.5 tick grid (so equal highs/lows are common), one bar every `step`
 * seconds, with about `gapRate` of the bars dropped (gaps are never filled).
 */
function randomWalk(n: number, seed: number, step: number, gapRate = 0.03): Candle[] {
  const rnd = mulberry32(seed)
  const q = (v: number) => Math.round(v * 2) / 2
  const out: Candle[] = []
  let prev = 100
  for (let i = 0; i < n; i++) {
    const open = prev
    const close = Math.max(0.5, q(open + (rnd() - 0.5) * 6))
    const high = q(Math.max(open, close) + rnd() * 2)
    const low = Math.max(0, q(Math.min(open, close) - rnd() * 2))
    prev = close
    if (rnd() < gapRate) continue
    out.push({ time: T0 + i * step, open, high, low, close, volume: 1 + rnd() })
  }
  return out
}

/**
 * A new series with the same times: bars 0..t are value-equal COPIES (different objects), every
 * bar after t gets fresh OHLC with extreme highs and lows (valid candles: low <= open, close <=
 * high), so any read of a later bar would show up in a box.
 */
function perturbAfter(bars: readonly Candle[], t: number, seed: number): Candle[] {
  const rnd = mulberry32(seed)
  return bars.map((b, i) => {
    if (i <= t) return { ...b }
    const open = Math.round(50 + rnd() * 100)
    const close = Math.round(50 + rnd() * 100)
    const r = rnd()
    const high = r < 0.3 ? 1e9 : Math.max(open, close) + Math.round(rnd() * 40)
    const low = r > 0.7 ? 1e-6 : Math.min(open, close) - Math.round(rnd() * 40)
    return { time: b.time, open, high, low, close, volume: rnd() * 5 }
  })
}

/** The engine's result after each push (index i = after bars 0..i). */
function snapshots(bars: readonly Candle[], p: SessionRangesParams): SessionRanges[] {
  const engine = new SessionRangesEngine(p)
  return bars.map((c) => {
    engine.push(c)
    return engine.result()
  })
}

/**
 * Independent brute-force reference: for every bar and session, try the instances starting on
 * the bar's UTC day and the day before, keep the latest one that contains the bar, group, then
 * take max/min. No shared code with the engine.
 */
function reference(bars: readonly Candle[], p: SessionRangesParams): SessionRanges {
  const windows: [SessionKey, SessionWindow][] = [
    ['asia', p.asia],
    ['london', p.london],
    ['newyork', p.newyork],
    ['opening', { startHour: 0, endHour: p.openingHours }],
  ]
  const groups = new Map<string, { key: SessionKey; start: number; end: number; bars: Candle[] }>()
  for (const [key, w] of windows) {
    let hours = (w.endHour - w.startHour + 24) % 24
    if (hours === 0) hours = 24
    for (const b of bars) {
      const day = Math.floor(b.time / DAY) * DAY
      let found: number | null = null
      for (const s of [day - DAY + w.startHour * HOUR, day + w.startHour * HOUR]) {
        if (s <= b.time && b.time < s + hours * HOUR) found = s
      }
      if (found === null) continue
      const id = `${key}@${found}`
      let g = groups.get(id)
      if (g === undefined) {
        g = { key, start: found, end: found + hours * HOUR, bars: [] }
        groups.set(id, g)
      }
      g.bars.push(b)
    }
  }
  const boxes: SessionBox[] = [...groups.values()].map((g) => ({
    session: g.key,
    start: g.start,
    end: g.end,
    high: Math.max(...g.bars.map((b) => b.high)),
    low: Math.min(...g.bars.map((b) => b.low)),
    firstTime: g.bars[0].time,
    lastTime: g.bars[g.bars.length - 1].time,
    bars: g.bars.length,
  }))
  boxes.sort(
    (a, b) =>
      a.start - b.start || SESSION_KEYS.indexOf(a.session) - SESSION_KEYS.indexOf(b.session),
  )
  return { boxes }
}

/** Every box restricted to bars with open time <= t, from the full bar list (brute force). */
function asOf(bars: readonly Candle[], t: number, p: SessionRangesParams): SessionRanges {
  return reference(
    bars.filter((b) => b.time <= t),
    p,
  )
}

describe('session ranges: hand-built fixture across a UTC midnight', () => {
  it('fixture is 1h bars from day 1 20:00 to day 2 17:00 with the 03:00 bar missing', () => {
    expect(D1 % DAY).toBe(0)
    expect(FIXTURE_CANDLES[0].time).toBe(h1(20))
    expect(FIXTURE_CANDLES[FIXTURE_CANDLES.length - 1].time).toBe(h2(17))
    expect(FIXTURE_CANDLES.map((c) => c.time)).not.toContain(h2(3))
    expect(FIXTURE_CANDLES.length).toBe(21)
  })

  it('defaults: Asia, London, New York and the opening range match the hand working', () => {
    expect(computeSessionRanges(FIXTURE_CANDLES, DEFAULTS).boxes).toEqual(EXPECTED_DEFAULTS)
  })

  it('a wrapping custom window (Asia 22 -> 06) spans the UTC midnight', () => {
    const r = computeSessionRanges(
      FIXTURE_CANDLES,
      withParams({ asia: { startHour: 22, endHour: 6 } }),
    )
    expect(r.boxes).toEqual(EXPECTED_ASIA_WRAP)
  })

  it('non-default opening range N = 3', () => {
    const r = computeSessionRanges(FIXTURE_CANDLES, withParams({ openingHours: 3 }))
    expect(r.boxes).toEqual(EXPECTED_OPENING_3)
  })

  it('start == end is a 24h window from the start hour (Asia 08 -> 08)', () => {
    // Instance [h1(8), h2(8)) holds bars 0..10 (h1(20)..h2(7)): 11 bars.
    //   highs 102,104,105,106,107,103,107,106.5,105.5,99,100 -> 107
    //   lows  99,100,102,103,101,98,97,104,96,95,94 -> 94
    // Instance [h2(8), h2(8) + 1 day) holds bars 11..20 (h2(8)..h2(17)): 10 bars.
    //   highs 108,109,110,109.5,105,103,96,111,112,113 -> 113
    //   lows  98,106,107,103,101,93,92,94,109,108 -> 92
    const r = computeSessionRanges(
      FIXTURE_CANDLES,
      withParams({ asia: { startHour: 8, endHour: 8 } }),
    )
    const asia = r.boxes.filter((b) => b.session === 'asia')
    expect(asia).toEqual([
      {
        session: 'asia',
        start: h1(8),
        end: h2(8),
        high: 107,
        low: 94,
        firstTime: h1(20),
        lastTime: h2(7),
        bars: 11,
      },
      {
        session: 'asia',
        start: h2(8),
        end: h2(8) + DAY,
        high: 113,
        low: 92,
        firstTime: h2(8),
        lastTime: h2(17),
        bars: 10,
      },
    ])
  })

  it('as-of-t boxes (closed bars 0..t only) match the fixture snapshots, across the gap and overlaps', () => {
    expect(EXPECTED_AS_OF_DEFAULTS.map((x) => x.lastIndex)).toEqual([3, 6, 7, 15, 17])
    const engine = snapshots(FIXTURE_CANDLES, DEFAULTS)
    for (const { lastIndex, boxes } of EXPECTED_AS_OF_DEFAULTS) {
      expect(computeSessionRanges(FIXTURE_CANDLES.slice(0, lastIndex + 1), DEFAULTS).boxes).toEqual(
        boxes,
      )
      expect(engine[lastIndex].boxes).toEqual(boxes)
      expect(reference(FIXTURE_CANDLES.slice(0, lastIndex + 1), DEFAULTS).boxes).toEqual(boxes)
    }
  })

  it('the brute-force reference agrees with the hand working (sanity check of the test oracle)', () => {
    expect(reference(FIXTURE_CANDLES, DEFAULTS).boxes).toEqual(EXPECTED_DEFAULTS)
    expect(
      reference(FIXTURE_CANDLES, withParams({ asia: { startHour: 22, endHour: 6 } })).boxes,
    ).toEqual(EXPECTED_ASIA_WRAP)
    expect(reference(FIXTURE_CANDLES, withParams({ openingHours: 3 })).boxes).toEqual(
      EXPECTED_OPENING_3,
    )
  })

  it('no bars, no boxes', () => {
    expect(computeSessionRanges([], DEFAULTS)).toEqual({ boxes: [] })
  })
})

describe('session ranges: membership by open time', () => {
  it('on 3h bars a bar straddling a session end counts for the session it opens in', () => {
    // 3h bars on D1 at 00, 03, ..., 21. Asia [0,8): 00, 03, 06 (06 runs to 09 but opens in Asia).
    // London [7,16): 09, 12, 15 (06 opens before 07: not London). NY [13,22): 15, 18, 21.
    // Opening [0,1): 00. high = 10 + i, low = i for bar i.
    const bars: Candle[] = Array.from({ length: 8 }, (_, i) => ({
      time: D1 + i * 3 * HOUR,
      open: 5 + i,
      high: 10 + i,
      low: i,
      close: 5 + i,
      volume: 1,
    }))
    const r = computeSessionRanges(bars, DEFAULTS)
    const pick = (k: SessionKey) => r.boxes.filter((b) => b.session === k)
    expect(pick('asia')).toEqual([
      {
        session: 'asia',
        start: D1,
        end: D1 + 8 * HOUR,
        high: 12,
        low: 0,
        firstTime: D1,
        lastTime: D1 + 6 * HOUR,
        bars: 3,
      },
    ])
    expect(pick('london')).toEqual([
      {
        session: 'london',
        start: D1 + 7 * HOUR,
        end: D1 + 16 * HOUR,
        high: 15,
        low: 3,
        firstTime: D1 + 9 * HOUR,
        lastTime: D1 + 15 * HOUR,
        bars: 3,
      },
    ])
    expect(pick('newyork')).toEqual([
      {
        session: 'newyork',
        start: D1 + 13 * HOUR,
        end: D1 + 22 * HOUR,
        high: 17,
        low: 5,
        firstTime: D1 + 15 * HOUR,
        lastTime: D1 + 21 * HOUR,
        bars: 3,
      },
    ])
    expect(pick('opening')).toEqual([
      {
        session: 'opening',
        start: D1,
        end: D1 + HOUR,
        high: 10,
        low: 0,
        firstTime: D1,
        lastTime: D1,
        bars: 1,
      },
    ])
  })

  it('matches the brute-force reference on random walks (1h, 15m, 4h; default and custom windows)', () => {
    const custom = withParams({
      asia: { startHour: 22, endHour: 6 },
      london: { startHour: 0, endHour: 24 },
      newyork: { startHour: 9, endHour: 9 },
      openingHours: 5,
    })
    for (const [step, n] of [
      [HOUR, 3000],
      [900, 4000],
      [4 * HOUR, 1500],
    ] as const) {
      for (const p of [DEFAULTS, custom]) {
        const bars = randomWalk(n, step + n, step)
        expect(computeSessionRanges(bars, p)).toEqual(reference(bars, p))
      }
    }
  })

  it('boxes are ascending by start, ties in the order asia, london, newyork, opening', () => {
    // Every session starting at 00:00 forces equal starts.
    const p = withParams({
      asia: { startHour: 0, endHour: 8 },
      london: { startHour: 0, endHour: 16 },
      newyork: { startHour: 0, endHour: 22 },
    })
    const r = computeSessionRanges(randomWalk(500, 9, HOUR), p)
    for (let i = 1; i < r.boxes.length; i++) {
      const a = r.boxes[i - 1]
      const b = r.boxes[i]
      expect(a.start <= b.start).toBe(true)
      if (a.start === b.start) {
        expect(SESSION_KEYS.indexOf(a.session)).toBeLessThan(SESSION_KEYS.indexOf(b.session))
      }
    }
    expect(r.boxes.slice(0, 4).map((b) => b.session)).toEqual([
      'asia',
      'london',
      'newyork',
      'opening',
    ])
  })
})

describe('session ranges: no look-ahead', () => {
  it('perturbing every bar after t (new objects, extreme values) leaves every box as of t and every finished box unchanged (1h, 15m, 4h)', () => {
    for (const [step, n] of [
      [HOUR, 600],
      [900, 800],
      [4 * HOUR, 300],
    ] as const) {
      const bars = randomWalk(n, 41 + step, step)
      const original = snapshots(bars, DEFAULTS)
      const full = original[original.length - 1]
      let changedSomewhere = 0
      for (let t = 0; t < bars.length - 1; t += 7) {
        const perturbed = perturbAfter(bars, t, 1000 + t)
        expect(perturbed[t]).not.toBe(bars[t])
        // 1. One engine over the whole perturbed series: its result after exactly t + 1 pushes
        //    equals the original series' result after t + 1 pushes, and the brute-force boxes of
        //    bars <= t.
        const engine = new SessionRangesEngine(DEFAULTS)
        for (let i = 0; i <= t; i++) engine.push(perturbed[i])
        const asOfT = engine.result()
        expect(asOfT).toEqual(original[t])
        expect(asOfT).toEqual(asOf(bars, bars[t].time, DEFAULTS))
        for (let i = t + 1; i < perturbed.length; i++) engine.push(perturbed[i])
        const perturbedFull = engine.result()
        // The perturbation is not vacuous: the full results differ somewhere after t.
        if (JSON.stringify(perturbedFull) !== JSON.stringify(full)) changedSomewhere++
        // 2. Every box whose instance ended by bar t+1's open is identical in the full original
        //    and the full perturbed computation (incremental and one-shot).
        const cut = bars[t + 1].time
        const finished = (r: SessionRanges) => r.boxes.filter((b) => b.end <= cut)
        expect(finished(perturbedFull)).toEqual(finished(full))
        expect(finished(computeSessionRanges(perturbed, DEFAULTS))).toEqual(finished(full))
        // 3. Every box of the full perturbed result that took no bar after t equals the original
        //    box of the same instance as of t.
        for (const b of perturbedFull.boxes.filter((x) => x.lastTime <= bars[t].time)) {
          const o = original[t].boxes.find((x) => x.session === b.session && x.start === b.start)
          expect(o).toEqual(b)
        }
      }
      expect(changedSomewhere).toBeGreaterThan(0)
    }
  })

  it('the closed-only result ignores the forming bar; pushing it would stretch the open New York box', () => {
    // The layer passes closed bars only (never the forming bar). A "forming" bar with extreme
    // values, pushed or not, cannot touch any box it is not a member of, and the closed-only
    // result is what the overlay draws.
    const closed = [...FIXTURE_CANDLES]
    const forming: Candle = {
      time: h2(18),
      open: 112,
      high: 1_000_000,
      low: 0.0001,
      close: 50,
      volume: 1,
    }
    const closedOnly = computeSessionRanges(closed, DEFAULTS)
    expect(closedOnly.boxes).toEqual(EXPECTED_DEFAULTS)
    expect(sessionRangesDef.sessions?.(closed, defaultInputs(sessionRangesDef))).toEqual(closedOnly)

    const withForming = computeSessionRanges([...closed, forming], DEFAULTS)
    // h2(18) is only in NY d2; every other box is identical with or without it.
    const others = (r: SessionRanges) =>
      r.boxes.filter((b) => !(b.start <= forming.time && forming.time < b.end))
    expect(others(withForming)).toEqual(others(closedOnly))
    expect(others(closedOnly).length).toBe(EXPECTED_DEFAULTS.length - 1)
    // This is why it must never be passed: it would stretch the open NY box.
    expect(withForming.boxes[4].high).toBe(1_000_000)
    expect(closedOnly.boxes[4].high).toBe(113)
    expect(closedOnly.boxes[4].low).toBe(92)
  })

  it('a bar reaches a box once, with the values it is pushed with; it cannot be revised in place', () => {
    // Closed through h2(11) (bar 14). Bar 15 (h2(12)) is in London d2 only.
    const closed = FIXTURE_CANDLES.slice(0, 15)
    const final = FIXTURE_CANDLES[15]
    const engine = new SessionRangesEngine(DEFAULTS)
    for (const c of closed) engine.push(c)
    const before = engine.result()
    const londonOf = (r: SessionRanges) => r.boxes.find((b) => b.session === 'london')
    expect(londonOf(before)).toMatchObject({ high: 110, low: 94, bars: 5 })
    for (const [high, low] of [
      [1e3, final.low],
      [final.high, 1e-3],
      [1e9, 1e-9],
    ]) {
      // An engine that pushed a forming tick would carry the tick's extremes into London d2 ...
      const ticked = new SessionRangesEngine(DEFAULTS)
      for (const c of closed) ticked.push(c)
      ticked.push({ ...final, high, low })
      expect(londonOf(ticked.result())).toMatchObject({ high: Math.max(110, high) })
      expect(londonOf(ticked.result())?.low).toBe(Math.min(94, low))
      // ... and could never take the final values afterwards (no revision of a pushed bar).
      expect(() => ticked.push(final)).toThrow(RangeError)
      expect(londonOf(ticked.result())?.high).toBe(Math.max(110, high))
    }
    // The closed-only engine was never touched by any of that, and takes the final values once.
    expect(engine.result()).toEqual(before)
    engine.push(final)
    expect(engine.result()).toEqual(computeSessionRanges(FIXTURE_CANDLES.slice(0, 16), DEFAULTS))
    expect(engine.result()).toEqual({ boxes: EXPECTED_AS_OF_DEFAULTS[3].boxes })
    // The engine has no update-in-place API a forming bar could use.
    expect(Object.getOwnPropertyNames(SessionRangesEngine.prototype).sort()).toEqual(
      ['constructor', 'count', 'push', 'result'].sort(),
    )
  })
})

describe('session ranges: alignment with the bar length (display rule)', () => {
  const H = HOUR
  const shown = (p: SessionRangesParams, l: number) =>
    SESSION_KEYS.filter((k) => alignedSessions(p, l)[k])

  it('sessionDurationHours: end > start same day, end <= start wraps, start == end is 24h', () => {
    expect(sessionDurationHours({ startHour: 0, endHour: 8 })).toBe(8)
    expect(sessionDurationHours({ startHour: 7, endHour: 16 })).toBe(9)
    expect(sessionDurationHours({ startHour: 22, endHour: 6 })).toBe(8)
    expect(sessionDurationHours({ startHour: 8, endHour: 8 })).toBe(24)
    expect(sessionDurationHours({ startHour: 0, endHour: 24 })).toBe(24)
  })

  it('sessionAligned: the day, the start and the length must all be whole bars', () => {
    expect(sessionAligned(0, 8, 4 * H)).toBe(true)
    expect(sessionAligned(7, 9, 4 * H)).toBe(false) // start 7 is not a multiple of 4
    expect(sessionAligned(8, 6, 4 * H)).toBe(false) // length 6 is not a multiple of 4
    expect(sessionAligned(0, 1, 4 * H)).toBe(false)
    expect(sessionAligned(0, 4, 4 * H)).toBe(true)
    expect(sessionAligned(0, 5, 5 * H)).toBe(false) // 5h bars do not tile the UTC day
    expect(sessionAligned(13, 9, 900)).toBe(true)
    expect(sessionAligned(0, 24, 86_400)).toBe(true)
    for (const bad of [0, -3600, 1.5, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(sessionAligned(0, 8, bad)).toBe(false)
    }
  })

  it('defaults: 1h and sub-hour show all four; 2h and 4h Asia only; 12h and 5h none', () => {
    const all = ['asia', 'london', 'newyork', 'opening']
    for (const l of [1, 5, 60, 180, 300, 900, 1800, H]) expect(shown(DEFAULTS, l)).toEqual(all)
    expect(shown(DEFAULTS, 2 * H)).toEqual(['asia'])
    expect(shown(DEFAULTS, 4 * H)).toEqual(['asia'])
    expect(shown(DEFAULTS, 3 * H)).toEqual([]) // Asia 8h is not 3h bars
    expect(shown(DEFAULTS, 6 * H)).toEqual([])
    expect(shown(DEFAULTS, 12 * H)).toEqual([])
    expect(shown(DEFAULTS, 5 * H)).toEqual([])
    expect(shown(DEFAULTS, 7 * H)).toEqual([])
    expect(shown(DEFAULTS, 23 * H)).toEqual([])
    expect(shown(DEFAULTS, 7)).toEqual([]) // 7s bars do not tile the day
  })

  it('custom hours: OR N = 4 on 4h is shown; London 08-16 and NY 12-20 on 4h are shown', () => {
    expect(shown(withParams({ openingHours: 4 }), 4 * H)).toEqual(['asia', 'opening'])
    expect(shown(withParams({ openingHours: 2 }), 2 * H)).toEqual(['asia', 'opening'])
    expect(shown(withParams({ openingHours: 12 }), 12 * H)).toEqual(['opening'])
    expect(
      shown(
        withParams({
          london: { startHour: 8, endHour: 16 },
          newyork: { startHour: 12, endHour: 20 },
        }),
        4 * H,
      ),
    ).toEqual(['asia', 'london', 'newyork'])
    // A wrapping 20 -> 04 window is 8h from 20:00: whole 4h bars.
    expect(shown(withParams({ asia: { startHour: 20, endHour: 4 } }), 4 * H)).toEqual(['asia'])
    // Raw params are normalized first (N = 0.6 -> 1, hidden on 4h).
    expect(alignedSessions(withParams({ openingHours: 0.6 }), 4 * H).opening).toBe(false)
  })

  it('the compute is interval-agnostic: alignment never changes the boxes', () => {
    const bars = randomWalk(600, 17, 4 * H)
    expect(computeSessionRanges(bars, DEFAULTS)).toEqual(reference(bars, DEFAULTS))
    expect(computeSessionRanges(bars, DEFAULTS).boxes.some((b) => b.session === 'london')).toBe(
      true,
    )
  })
})

describe('session ranges: incremental engine', () => {
  it('pushing one bar at a time equals the full computation at every prefix (fixture)', () => {
    for (const p of [
      DEFAULTS,
      withParams({ asia: { startHour: 22, endHour: 6 }, openingHours: 3 }),
    ]) {
      const engine = new SessionRangesEngine(p)
      for (let i = 0; i < FIXTURE_CANDLES.length; i++) {
        engine.push(FIXTURE_CANDLES[i])
        expect(engine.count).toBe(i + 1)
        expect(engine.result()).toEqual(computeSessionRanges(FIXTURE_CANDLES.slice(0, i + 1), p))
      }
    }
  })

  it('every prefix on a 1h random walk; checkpoints on 1h, 15m and 4h walks of thousands of bars', () => {
    const small = randomWalk(700, 5, HOUR)
    const e1 = new SessionRangesEngine(DEFAULTS)
    small.forEach((c, i) => {
      e1.push(c)
      expect(e1.result()).toEqual(computeSessionRanges(small.slice(0, i + 1), DEFAULTS))
    })
    for (const [step, n] of [
      [HOUR, 5000],
      [900, 6000],
      [4 * HOUR, 3000],
    ] as const) {
      const bars = randomWalk(n, 77 + step, step)
      const engine = new SessionRangesEngine(DEFAULTS)
      bars.forEach((c, i) => {
        engine.push(c)
        if (i % 97 === 0 || i === bars.length - 1) {
          expect(engine.result()).toEqual(computeSessionRanges(bars.slice(0, i + 1), DEFAULTS))
        }
      })
    }
  })

  it('result() is a fresh array; returned boxes are never mutated; finished boxes keep identity', () => {
    const bars = randomWalk(400, 13, HOUR)
    const engine = new SessionRangesEngine(DEFAULTS)
    const snapshots: { r: SessionRanges; copy: SessionBox[]; at: number }[] = []
    for (const c of bars) {
      engine.push(c)
      const r = engine.result()
      snapshots.push({ r, copy: r.boxes.map((b) => ({ ...b })), at: c.time })
    }
    const last = engine.result()
    expect(last.boxes).not.toBe(snapshots[snapshots.length - 1].r.boxes)
    for (const s of snapshots) {
      // Nothing handed out earlier changed.
      expect(s.r.boxes).toEqual(s.copy)
      // A box that could take no more bars (next bar opens at or after its end) is the same object.
      for (const b of s.r.boxes) {
        const later = last.boxes.find((x) => x.session === b.session && x.start === b.start)
        expect(later).toBeDefined()
        if (later !== undefined && later.bars === b.bars) expect(later).toBe(b)
      }
    }
  })

  it('rejects a bar that is not strictly after the previous one', () => {
    const engine = new SessionRangesEngine(DEFAULTS)
    engine.push(FIXTURE_CANDLES[0])
    expect(() => engine.push(FIXTURE_CANDLES[0])).toThrow(RangeError)
    expect(() => engine.push({ ...FIXTURE_CANDLES[0], time: FIXTURE_CANDLES[0].time - 1 })).toThrow(
      RangeError,
    )
    expect(engine.count).toBe(1)
  })

  it('100k bars compute in well under a second', () => {
    const bars = randomWalk(100_000, 3, 60, 0)
    const t0 = performance.now()
    const r = computeSessionRanges(bars, DEFAULTS)
    const ms = performance.now() - t0
    expect(r.boxes.length).toBeGreaterThan(0)
    expect(ms).toBeLessThan(400)
  })
})

describe('session ranges: params and definition', () => {
  it('defaults are the PLAN values: Asia 0-8, London 7-16, New York 13-22, N = 1', () => {
    expect(SESSION_RANGES_DEFAULTS).toEqual({
      asia: { startHour: 0, endHour: 8 },
      london: { startHour: 7, endHour: 16 },
      newyork: { startHour: 13, endHour: 22 },
      openingHours: 1,
    })
    expect(sessionParams({})).toEqual(SESSION_RANGES_DEFAULTS)
    expect(sessionParams(defaultInputs(sessionRangesDef))).toEqual(SESSION_RANGES_DEFAULTS)
    expect(SESSION_KEYS).toEqual(['asia', 'london', 'newyork', 'opening'])
  })

  it('clamps, rounds and falls back to defaults', () => {
    expect(
      sessionParams({
        asiaStart: -3,
        asiaEnd: 0,
        londonStart: 30,
        londonEnd: 99,
        nyStart: 12.6,
        nyEnd: Number.NaN,
        orHours: 0,
      }),
    ).toEqual({
      asia: { startHour: 0, endHour: 1 },
      london: { startHour: 23, endHour: 24 },
      newyork: { startHour: 13, endHour: 22 },
      openingHours: 1,
    })
    expect(sessionParams({ orHours: 50 }).openingHours).toBe(12)
    expect(sessionParams({ orHours: 2.4 }).openingHours).toBe(2)
    expect(sessionParams({ nyStart: Number.POSITIVE_INFINITY }).newyork.startHour).toBe(13)
  })

  it('the engine normalizes raw params the same way', () => {
    const raw: SessionRangesParams = {
      asia: { startHour: -5, endHour: 8.4 },
      london: { startHour: 7, endHour: 16 },
      newyork: { startHour: Number.NaN, endHour: 22 },
      openingHours: 0.6,
    }
    expect(computeSessionRanges(FIXTURE_CANDLES, raw).boxes).toEqual(EXPECTED_DEFAULTS)
  })

  it('sessionParamsKey changes with hours and N, never with the show toggles', () => {
    const base = defaultInputs(sessionRangesDef)
    const key = sessionParamsKey(sessionParams(base))
    for (const k of SESSION_SHOW_KEYS) {
      expect(sessionParamsKey(sessionParams({ ...base, [k]: 0 }))).toBe(key)
    }
    for (const k of [
      'asiaStart',
      'asiaEnd',
      'londonStart',
      'londonEnd',
      'nyStart',
      'nyEnd',
      'orHours',
    ]) {
      expect(sessionParamsKey(sessionParams({ ...base, [k]: base[k] + 1 }))).not.toBe(key)
    }
    // Swapping two windows' hours is a different key.
    const swapped = sessionParams({
      ...base,
      asiaStart: 7,
      asiaEnd: 16,
      londonStart: 0,
      londonEnd: 8,
    })
    expect(sessionParamsKey(swapped)).not.toBe(key)
  })

  it('show toggles never change the computation', () => {
    const base = defaultInputs(sessionRangesDef)
    const off = { ...base, showAsia: 0, showLondon: 0, showNewYork: 0, showOpening: 0 }
    expect(sessionRangesDef.sessions?.(FIXTURE_CANDLES, off)).toEqual(
      sessionRangesDef.sessions?.(FIXTURE_CANDLES, base),
    )
  })

  it('registry definition: event-style overlay with ordered inputs and toggles', () => {
    const def = sessionRangesDef
    expect(def.id).toBe('sessions')
    expect(def.name).toBe('Session Ranges')
    expect(def.shortName).toBe('SR')
    expect(def.placement).toBe('overlay')
    expect(def.source).toBeNull()
    expect(def.lines).toEqual([])
    expect(def.guides).toEqual([])
    expect(def.profile).toBeUndefined()
    expect(def.structure).toBeUndefined()
    expect(def.inputs.map((i) => [i.key, i.default, i.min, i.max])).toEqual([
      ['asiaStart', 0, 0, 23],
      ['asiaEnd', 8, 1, 24],
      ['londonStart', 7, 0, 23],
      ['londonEnd', 16, 1, 24],
      ['nyStart', 13, 0, 23],
      ['nyEnd', 22, 1, 24],
      ['orHours', 1, 1, 12],
      ['showAsia', 1, 0, 1],
      ['showLondon', 1, 0, 1],
      ['showNewYork', 1, 0, 1],
      ['showOpening', 1, 0, 1],
    ])
    for (const i of def.inputs) {
      expect(i.step).toBe(1)
      expect(i.integer).toBe(true)
    }
    expect(def.inputs.filter((i) => i.kind === 'toggle').map((i) => i.key)).toEqual([
      ...SESSION_SHOW_KEYS,
    ])
    expect(def.inputs.find((i) => i.key === 'asiaStart')?.label).toBe('Asia start (UTC h)')
    expect(def.inputs.find((i) => i.key === 'orHours')?.label).toBe('Opening range (h)')
    // The stepper returns nothing; the per-bar compute yields no lines.
    const stepper = def.createStepper(defaultInputs(def), 'close')
    expect(stepper.next(FIXTURE_CANDLES[0])).toEqual({})
    expect(stepper.clone().next(FIXTURE_CANDLES[1])).toEqual({})
    expect(computeIndicator(def, FIXTURE_CANDLES, defaultInputs(def))).toEqual({})
    expect(def.sessions?.(FIXTURE_CANDLES, defaultInputs(def))?.boxes).toEqual(EXPECTED_DEFAULTS)
  })
})
