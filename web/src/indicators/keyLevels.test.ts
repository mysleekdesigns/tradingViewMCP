import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { defaultInputs } from './compute'
import {
  DAY,
  EXPECTED_ALL_DAYS,
  EXPECTED_ALL_STEPS,
  EXPECTED_DAYS,
  EXPECTED_GAP_DAYS,
  EXPECTED_GAP_STEPS,
  EXPECTED_PW,
  EXPECTED_RANGE_USED,
  EXPECTED_STEPS,
  FIXTURE_BARS,
  FIXTURE_DAILY,
  FIXTURE_PARAMS,
  GAP_DAILY,
  HOUR,
  JAN1,
  jan,
} from './keyLevels.fixture'
import {
  KEY_LEVEL_LABELS,
  KeyLevelsEngine,
  computeKeyLevels,
  keyLevelsDef,
  keyLevelsParams,
  keyLevelsParamsKey,
  type KeyLevelsParams,
} from './keyLevels'
import { getIndicatorDef } from './registry'
import type { KeyLevelDay, KeyLevels, KeyLevelStep } from './types'

/** Rounding of alpha = 1/3 in the Wilder steps; the hand values are exact fractions. */
const TOL = 1e-9

function bar(time: number, high: number, low: number): Candle {
  return { time, open: low, high, low, close: high, volume: 1 }
}

function expectDays(actual: readonly KeyLevelDay[], expected: readonly KeyLevelDay[]): void {
  expect(actual.map((d) => [d.day, d.bias])).toEqual(expected.map((d) => [d.day, d.bias]))
  actual.forEach((d, i) => {
    for (const key of ['atr', 'sma'] as const) {
      const want = expected[i][key]
      if (want === null) expect(d[key]).toBeNull()
      else expect(Math.abs((d[key] as number) - want)).toBeLessThan(TOL)
    }
  })
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

function randomCandle(time: number, rnd: () => number, around: number, spread = 0.04): Candle {
  const open = around * (1 + (rnd() - 0.5) * spread)
  const close = open * (1 + (rnd() - 0.5) * spread)
  const high = Math.max(open, close) * (1 + rnd() * spread)
  const low = Math.min(open, close) * (1 - rnd() * spread)
  return { time, open, high, low, close, volume: 1 }
}

/** Seeded walk of `n` candles every `step` seconds from `start`, about `gapRate` of them dropped. */
function randomWalk(n: number, seed: number, start: number, step: number, gapRate: number) {
  const rnd = mulberry32(seed)
  const out: Candle[] = []
  let price = 100
  for (let i = 0; i < n; i++) {
    const c = randomCandle(start + i * step, rnd, price)
    price = c.close
    if (rnd() >= gapRate) out.push(c)
  }
  return out
}

/** 120 days of daily candles, then 40 days of 1h bars ending inside the last daily candle's day. */
const RANDOM_DAILY = randomWalk(120, 7, JAN1, DAY, 0.03)
const RANDOM_BARS = randomWalk(40 * 24, 8, JAN1 + 80 * DAY, HOUR, 0.02)
const WEEKLY: KeyLevelsParams = { atrPeriod: 14, smaPeriod: 20, showWeekly: true }

/**
 * Value-equal copies at or before `keep`, then fresh extreme candles: any read of a later candle
 * would show up. Each `keep` is a function of the candle so the two series can cut differently.
 */
function perturb(bars: readonly Candle[], keep: (c: Candle) => boolean, seed: number): Candle[] {
  const rnd = mulberry32(seed)
  return bars.map((c) => (keep(c) ? { ...c } : randomCandle(c.time, rnd, 5000, 0.5)))
}

/** Everything in `r` dated at or before t. */
function upTo(r: KeyLevels, t: number): KeyLevels {
  return {
    steps: r.steps.filter((s) => s.start <= t),
    days: r.days.filter((d) => d.day <= t),
    rangeUsed: r.rangeUsed.filter((p) => p.time <= t),
  }
}

/** One day's state by plain loops: the consecutive run of candles ending at D - 86400. */
function naiveDay(
  byTime: ReadonlyMap<number, Candle>,
  day: number,
  atrPeriod: number,
  smaPeriod: number,
): Pick<KeyLevelDay, 'atr' | 'sma'> {
  const run: Candle[] = []
  for (let d = day - DAY; byTime.has(d); d -= DAY) run.unshift(byTime.get(d) as Candle)
  let atr: number | null = null
  if (run.length >= atrPeriod) {
    const trs = run.map((c, i) => {
      if (i === 0) return c.high - c.low
      const pc = run[i - 1].close
      return Math.max(c.high - c.low, Math.abs(c.high - pc), Math.abs(c.low - pc))
    })
    let a = 0
    for (let j = 0; j < atrPeriod; j++) a += trs[j]
    a /= atrPeriod
    for (let j = atrPeriod; j < trs.length; j++) a = (a * (atrPeriod - 1) + trs[j]) / atrPeriod
    atr = a
  }
  let sma: number | null = null
  if (run.length >= smaPeriod) {
    let s = 0
    for (const c of run.slice(-smaPeriod)) s += c.close
    sma = s / smaPeriod
  }
  return { atr, sma }
}

describe('computeKeyLevels: hand-worked fixture', () => {
  const out = computeKeyLevels(FIXTURE_DAILY, FIXTURE_BARS, null, FIXTURE_PARAMS)

  it('gives every step across the UTC midnights and the week boundary', () => {
    expect(out.steps).toEqual(EXPECTED_STEPS)
  })

  it('gives each day its ATR, SMA and bias as of the previous close', () => {
    expectDays(out.days, EXPECTED_DAYS)
  })

  it('gives range used per closed bar, reset at each UTC midnight', () => {
    expect(out.rangeUsed.map((p) => p.time)).toEqual(EXPECTED_RANGE_USED.map((p) => p.time))
    out.rangeUsed.forEach((p, i) => {
      expect(Math.abs(p.value - EXPECTED_RANGE_USED[i].value)).toBeLessThan(TOL)
    })
  })

  it('gives no range used for an oldest day whose start is not loaded', () => {
    // Worked by hand from 12:00: (108 - 102) / 12 = 50%. The whole day may be far wider.
    const firstFrom = (i: number) =>
      computeKeyLevels(FIXTURE_DAILY, FIXTURE_BARS.slice(i), null, FIXTURE_PARAMS)
    expect(firstFrom(0).rangeUsed.some((p) => p.time < jan(8))).toBe(false)
    // Starting at 06:00 on day 8 loses day 8, and day 9 is whole again.
    expect(firstFrom(3).rangeUsed.map((p) => p.time)).toEqual([jan(9, 0), jan(9, 6)])
    // A first bar exactly at 00:00 is a whole day.
    expect(firstFrom(2).rangeUsed.map((p) => p.time)).toEqual(
      EXPECTED_RANGE_USED.map((p) => p.time),
    )
    // Today behind a window of 500 1m bars, read at 22:00: 1 point wide, not 100.
    const today = jan(8)
    const early = { time: today, open: 100, high: 200, low: 100, close: 150, volume: 1 }
    const late = { time: today + 22 * HOUR, open: 150, high: 151, low: 150, close: 150, volume: 1 }
    const whole = computeKeyLevels(FIXTURE_DAILY, [early, late], null, FIXTURE_PARAMS)
    expect(whole.rangeUsed.at(-1)?.value).toBeCloseTo((100 / 14) * 100)
    expect(computeKeyLevels(FIXTURE_DAILY, [late], null, FIXTURE_PARAMS).rangeUsed).toEqual([])
  })

  it('counts a bar for the day it opens in, up to the last second of a Sunday', () => {
    // Worked by hand. ATR for day 7 is 12 (as of Sat), for day 8 is 14 (as of Sun).
    const c = (time: number, high: number, low: number) => ({
      time,
      open: low,
      high,
      low,
      close: low,
      volume: 1,
    })
    const bars = [c(jan(7), 104, 100), c(jan(8) - 1, 110, 102), c(jan(8), 106, 99)]
    const { rangeUsed } = computeKeyLevels(FIXTURE_DAILY, bars, null, FIXTURE_PARAMS)
    expect(rangeUsed.map((p) => p.time)).toEqual(bars.map((b) => b.time))
    expect(rangeUsed[0].value).toBeCloseTo((4 / 12) * 100, 9)
    expect(rangeUsed[1].value).toBeCloseTo((10 / 12) * 100, 9)
    expect(rangeUsed[2].value).toBeCloseTo((7 / 14) * 100, 9)
  })

  it('warms up day by day from the first candle', () => {
    // One bar on jan(1) puts every day from jan(1) to jan(9) in the output.
    const all = computeKeyLevels(
      FIXTURE_DAILY,
      [bar(jan(1, 3), 101, 100)],
      jan(9, 6),
      FIXTURE_PARAMS,
    )
    expect(all.steps).toEqual(EXPECTED_ALL_STEPS)
    expectDays(all.days, EXPECTED_ALL_DAYS)
    // Jan 1 has no ATR yet, so its bar has no range used.
    expect(all.rangeUsed).toEqual([])
  })

  it('shows each bar the high and low of the previous daily candle', () => {
    const dailyByTime = new Map(FIXTURE_DAILY.map((c) => [c.time, c]))
    for (const b of FIXTURE_BARS) {
      const live = out.steps.filter((s) => s.start <= b.time && b.time < s.end)
      const prev = dailyByTime.get(Math.floor(b.time / DAY) * DAY - DAY) as Candle
      expect(live.find((s) => s.key === 'pdh')?.price).toBe(prev.high)
      expect(live.find((s) => s.key === 'pdl')?.price).toBe(prev.low)
    }
  })

  it('takes the bias from a strict comparison: an equal close and SMA give null', () => {
    // Monday 8 Jan closed at 111, the SMA(3) of 117, 105, 111 is 111.
    const tue = out.days[2]
    expect(tue.sma).toBe(111)
    expect(tue.bias).toBeNull()
  })
})

describe('computeKeyLevels: previous week', () => {
  it('reads the whole previous Monday-to-Sunday week from its Monday to the next one', () => {
    const out = computeKeyLevels(FIXTURE_DAILY, FIXTURE_BARS, null, FIXTURE_PARAMS)
    expect(out.steps.filter((s) => s.key === 'pwh' || s.key === 'pwl')).toEqual(EXPECTED_PW)
  })

  it('gives no weekly step when it is off, and changes nothing else', () => {
    const on = computeKeyLevels(FIXTURE_DAILY, FIXTURE_BARS, null, FIXTURE_PARAMS)
    const off = computeKeyLevels(FIXTURE_DAILY, FIXTURE_BARS, null, {
      ...FIXTURE_PARAMS,
      showWeekly: false,
    })
    expect(off.steps).toEqual(on.steps.filter((s) => s.key === 'pdh' || s.key === 'pdl'))
    expect(off.days).toEqual(on.days)
    expect(off.rangeUsed).toEqual(on.rangeUsed)
  })

  it('starts on its Monday and sorts first when the first bar is later that week', () => {
    const out = computeKeyLevels(FIXTURE_DAILY, [bar(jan(9), 101, 100)], null, FIXTURE_PARAMS)
    expect(out.steps).toEqual([
      ...EXPECTED_PW,
      { key: 'pdh', start: jan(9), end: jan(10), price: 118 },
      { key: 'pdl', start: jan(9), end: jan(10), price: 101 },
    ])
  })

  it('gives a week once, after pdh and pdl, when the first bar is on its Monday', () => {
    const out = computeKeyLevels(FIXTURE_DAILY, [bar(jan(8), 101, 100)], null, FIXTURE_PARAMS)
    expect(out.steps).toEqual([
      { key: 'pdh', start: jan(8), end: jan(9), price: 109 },
      { key: 'pdl', start: jan(8), end: jan(9), price: 99 },
      ...EXPECTED_PW,
    ])
  })

  it('keeps starts ascending, and equal starts in the order pdh, pdl, pwh, pwl', () => {
    const { steps } = computeKeyLevels(RANDOM_DAILY, RANDOM_BARS, null, WEEKLY)
    const order = ['pdh', 'pdl', 'pwh', 'pwl']
    expect(steps.some((s) => s.key === 'pwh')).toBe(true)
    const rank = (s: KeyLevelStep) => s.start * 10 + order.indexOf(s.key)
    for (let i = 1; i < steps.length; i++) {
      expect(rank(steps[i])).toBeGreaterThan(rank(steps[i - 1]))
    }
  })

  it('gives no weekly step when any of the 7 days is missing', () => {
    for (const missing of [jan(1), jan(4), jan(7)]) {
      const daily = FIXTURE_DAILY.filter((c) => c.time !== missing)
      const out = computeKeyLevels(daily, FIXTURE_BARS, null, FIXTURE_PARAMS)
      expect(out.steps.filter((s) => s.key === 'pwh' || s.key === 'pwl')).toEqual([])
    }
  })
})

describe('computeKeyLevels: closed data only', () => {
  const closed = FIXTURE_BARS.slice(0, 6) // Through Mon 8 18:00.
  const daily = FIXTURE_DAILY.slice(0, 7) // Through Sun 7: Mon 8 is the newest closed day below.

  it('has the levels of a day before its first bar closes', () => {
    const allDaily = FIXTURE_DAILY
    const out = computeKeyLevels(allDaily, closed, jan(9), FIXTURE_PARAMS)
    expect(out.steps).toEqual(EXPECTED_STEPS)
    expectDays(out.days, EXPECTED_DAYS)
    expect(out.rangeUsed.at(-1)?.time).toBe(jan(8, 18))
  })

  it('reads only the UTC day of `through`, so the forming bar cannot move a level', () => {
    const a = computeKeyLevels(FIXTURE_DAILY, closed, jan(9), FIXTURE_PARAMS)
    const b = computeKeyLevels(FIXTURE_DAILY, closed, jan(9, 23), FIXTURE_PARAMS)
    expect(b).toStrictEqual(a)
  })

  it('a through older than the newest bar still runs to the newest bar', () => {
    const a = computeKeyLevels(FIXTURE_DAILY, FIXTURE_BARS, jan(7), FIXTURE_PARAMS)
    const b = computeKeyLevels(FIXTURE_DAILY, FIXTURE_BARS, null, FIXTURE_PARAMS)
    expect(a).toStrictEqual(b)
  })

  it('the forming 1d candle (open = today) leaves all of today unchanged', () => {
    // At Mon 8 the Jan 8 candle is forming. It may arrive at all, or with any values.
    const today = jan(8)
    const base = computeKeyLevels(daily, closed, today, FIXTURE_PARAMS)
    expect(base.steps.at(-1)?.start).toBe(today)
    const forming: Candle[] = [
      { time: today, open: 1, high: 9999, low: 0.5, close: 8888, volume: 1 },
      { time: today, open: 500, high: 700, low: 400, close: 450, volume: 1 },
      FIXTURE_DAILY[7],
    ]
    for (const c of forming) {
      expect(computeKeyLevels([...daily, c], closed, today, FIXTURE_PARAMS)).toStrictEqual(base)
    }
  })

  it('a daily candle dated later than today changes nothing up to today either', () => {
    const today = jan(7)
    const bars = FIXTURE_BARS.slice(0, 2)
    const base = computeKeyLevels(FIXTURE_DAILY.slice(0, 6), bars, today, FIXTURE_PARAMS)
    const more = computeKeyLevels(FIXTURE_DAILY, bars, today, FIXTURE_PARAMS)
    expect(more).toStrictEqual(base)
  })

  it('gives one day and its levels with no closed bar yet, and nothing with no time at all', () => {
    const none = new KeyLevelsEngine(FIXTURE_DAILY, FIXTURE_PARAMS)
    expect(none.result(null)).toEqual({ steps: [], days: [], rangeUsed: [] })
    const out = none.result(jan(9, 2))
    expect(out.steps).toEqual([
      ...EXPECTED_PW,
      { key: 'pdh', start: jan(9), end: jan(10), price: 118 },
      { key: 'pdl', start: jan(9), end: jan(10), price: 101 },
    ])
    expectDays(out.days, EXPECTED_ALL_DAYS.slice(8))
    expect(out.rangeUsed).toEqual([])
  })

  it('a bar on an earlier day after such a result still starts the days from that bar', () => {
    const engine = new KeyLevelsEngine(FIXTURE_DAILY, FIXTURE_PARAMS)
    engine.result(jan(9, 2))
    for (const b of FIXTURE_BARS) engine.push(b)
    expect(engine.result(null)).toStrictEqual(
      computeKeyLevels(FIXTURE_DAILY, FIXTURE_BARS, null, FIXTURE_PARAMS),
    )
  })
})

describe('computeKeyLevels: gaps', () => {
  const out = computeKeyLevels(GAP_DAILY, [bar(jan(4, 1), 101, 100)], jan(9), FIXTURE_PARAMS)

  it('leaves a missing day with no step, never a forward-filled level', () => {
    expect(out.steps).toEqual(EXPECTED_GAP_STEPS)
    expect(out.steps.some((s) => s.start === jan(5))).toBe(false)
  })

  it('restarts the ATR and SMA warm-ups from the candle after the gap', () => {
    expectDays(out.days, EXPECTED_GAP_DAYS)
  })

  it('gives no range used on a day with no ATR, and resumes when it returns', () => {
    const bars = [bar(jan(5, 12), 110, 100), bar(jan(6, 12), 110, 100), bar(jan(8, 12), 105.5, 100)]
    const r = computeKeyLevels(GAP_DAILY, bars, null, FIXTURE_PARAMS)
    // Mon 8: 5.5 / 11 * 100 = 50.
    expect(r.rangeUsed.map((p) => p.time)).toEqual([jan(8, 12)])
    expect(Math.abs(r.rangeUsed[0].value - 50)).toBeLessThan(TOL)
  })

  it('treats a daily candle that is not on a UTC midnight as absent', () => {
    const shifted = FIXTURE_DAILY.map((c) =>
      c.time === jan(4) ? { ...c, time: c.time + HOUR } : c,
    )
    const r = computeKeyLevels(shifted, [bar(jan(4, 1), 101, 100)], jan(9), FIXTURE_PARAMS)
    expect(r).toStrictEqual(out)
  })

  it('skips a candle that is not on a UTC midnight, without breaking the chain around it', () => {
    const stray: Candle = { ...FIXTURE_DAILY[3], time: jan(4, 3), high: 9999, low: 0.5 }
    const daily = [...FIXTURE_DAILY.slice(0, 4), stray, ...FIXTURE_DAILY.slice(4)]
    const one = [bar(jan(1, 3), 101, 100)]
    expect(computeKeyLevels(daily, one, jan(9), FIXTURE_PARAMS)).toStrictEqual(
      computeKeyLevels(FIXTURE_DAILY, one, jan(9), FIXTURE_PARAMS),
    )
  })

  it('a day past the last daily candle has an entry with nulls and no step', () => {
    const r = computeKeyLevels(FIXTURE_DAILY, FIXTURE_BARS, jan(11), FIXTURE_PARAMS)
    expect(r.days.slice(3)).toEqual([
      { day: jan(10), atr: null, sma: null, bias: null },
      { day: jan(11), atr: null, sma: null, bias: null },
    ])
    expect(r.steps).toEqual(EXPECTED_STEPS)
  })

  it('a day with no intraday bars still has its step', () => {
    const bars = [FIXTURE_BARS[0], FIXTURE_BARS[6]] // Sun 7 and Tue 9, nothing on Mon 8.
    const r = computeKeyLevels(FIXTURE_DAILY, bars, null, FIXTURE_PARAMS)
    expect(r.steps).toEqual(EXPECTED_STEPS)
  })

  it('gives no range used on a zero ATR (flat daily candles), never Infinity', () => {
    const flat: Candle[] = [1, 2, 3].map((n) => ({
      time: jan(n),
      open: 100,
      high: 100,
      low: 100,
      close: 100,
      volume: 1,
    }))
    const r = computeKeyLevels(flat, [bar(jan(4, 1), 101, 100)], null, FIXTURE_PARAMS)
    expect(r.days[0].atr).toBe(0)
    expect(r.rangeUsed).toEqual([])
  })
})

describe('computeKeyLevels: no look-ahead', () => {
  // Every 7th bar lands on each weekday and hour over 5 weeks of dailies running past the cut, so a
  // read of the current week's later days shows up on a Monday or Tuesday cut.
  const cuts = RANDOM_BARS.map((_, i) => i).filter((i) => i % 7 === 1)

  it('changing every candle after t leaves every output up to t bit-identical', () => {
    const full = computeKeyLevels(RANDOM_DAILY, RANDOM_BARS, null, WEEKLY)
    for (const i of cuts) {
      const t = RANDOM_BARS[i].time
      const today = Math.floor(t / DAY) * DAY
      // At t the daily candle of t's own day is still forming, so it may change too.
      const daily = perturb(RANDOM_DAILY, (c) => c.time < today, 100 + i)
      const bars = perturb(RANDOM_BARS, (c) => c.time <= t, 200 + i)
      const out = computeKeyLevels(daily, bars, null, WEEKLY)
      const want = upTo(full, t)
      expect(want.rangeUsed.length).toBeGreaterThan(0)
      expect(upTo(out, t)).toStrictEqual(want)
    }
  })

  it('a run cut at t equals the full run up to t', () => {
    const full = computeKeyLevels(RANDOM_DAILY, RANDOM_BARS, null, WEEKLY)
    for (const i of cuts) {
      const t = RANDOM_BARS[i].time
      const today = Math.floor(t / DAY) * DAY
      const daily = RANDOM_DAILY.filter((c) => c.time < today)
      const cut = computeKeyLevels(daily, RANDOM_BARS.slice(0, i + 1), t, WEEKLY)
      expect(cut).toStrictEqual(upTo(full, t))
    }
  })
})

describe('KeyLevelsEngine', () => {
  it('matches a full recompute after every bar, bit for bit', () => {
    const engine = new KeyLevelsEngine(RANDOM_DAILY, WEEKLY)
    RANDOM_BARS.forEach((b, i) => {
      engine.push(b)
      const full = computeKeyLevels(RANDOM_DAILY, RANDOM_BARS.slice(0, i + 1), b.time, WEEKLY)
      expect(engine.result(b.time)).toStrictEqual(full)
    })
  })

  it('matches a full recompute on the fixture when `through` runs ahead of the closed bars', () => {
    const engine = new KeyLevelsEngine(FIXTURE_DAILY, FIXTURE_PARAMS)
    FIXTURE_BARS.forEach((b, i) => {
      engine.push(b)
      const ahead = b.time + 6 * HOUR // The next bar, forming.
      const full = computeKeyLevels(
        FIXTURE_DAILY,
        FIXTURE_BARS.slice(0, i + 1),
        ahead,
        FIXTURE_PARAMS,
      )
      expect(engine.result(ahead)).toStrictEqual(full)
    })
  })

  it('never mutates what it returned before', () => {
    const engine = new KeyLevelsEngine(FIXTURE_DAILY, FIXTURE_PARAMS)
    for (const b of FIXTURE_BARS.slice(0, 3)) engine.push(b)
    const early = engine.result(null)
    const copy = structuredClone(early)
    for (const b of FIXTURE_BARS.slice(3)) engine.push(b)
    engine.result(jan(11))
    expect(early).toStrictEqual(copy)
  })

  it('answers for the `through` it is given, even an earlier one than the last call', () => {
    const bars = FIXTURE_BARS.slice(0, 2) // Sun 7 only; days 8 and 9 have steps of their own.
    const engine = new KeyLevelsEngine(FIXTURE_DAILY, FIXTURE_PARAMS)
    for (const b of bars) engine.push(b)
    const far = engine.result(jan(9))
    expect(far.days.at(-1)?.day).toBe(jan(9))
    expect(far.steps.at(-1)?.start).toBe(jan(9))
    expect(engine.result(null)).toStrictEqual(
      computeKeyLevels(FIXTURE_DAILY, bars, null, FIXTURE_PARAMS),
    )
  })

  it('throws on a repeated or older bar', () => {
    const engine = new KeyLevelsEngine(FIXTURE_DAILY, FIXTURE_PARAMS)
    engine.push(FIXTURE_BARS[1])
    expect(() => engine.push(FIXTURE_BARS[1])).toThrow(RangeError)
    expect(() => engine.push(FIXTURE_BARS[0])).toThrow(RangeError)
  })
})

describe('computeKeyLevels against a brute-force reference', () => {
  it('matches plain loops at the default periods over random gapped daily candles', () => {
    const params = keyLevelsParams(defaultInputs(keyLevelsDef))
    const byTime = new Map(RANDOM_DAILY.map((c) => [c.time, c]))
    const out = computeKeyLevels(RANDOM_DAILY, RANDOM_BARS, null, params)
    expect(out.days.length).toBeGreaterThan(30)
    expect(out.days.some((d) => d.atr !== null)).toBe(true)
    for (const d of out.days) {
      const want = naiveDay(byTime, d.day, params.atrPeriod, params.smaPeriod)
      for (const key of ['atr', 'sma'] as const) {
        const v = want[key]
        // Tolerance 1e-9: the reference uses (prev*(n-1)+tr)/n and fresh sums.
        if (v === null) expect(d[key]).toBeNull()
        else expect(Math.abs((d[key] as number) - v)).toBeLessThan(TOL)
      }
    }
  })

  it('matches a plain scan of each day for range used', () => {
    const out = computeKeyLevels(RANDOM_DAILY, RANDOM_BARS, null, WEEKLY)
    const atrOf = new Map(out.days.map((d) => [d.day, d.atr]))
    const seen = new Map(out.rangeUsed.map((p) => [p.time, p.value]))
    for (const b of RANDOM_BARS) {
      const day = Math.floor(b.time / DAY) * DAY
      const atr = atrOf.get(day) ?? null
      if (atr === null) {
        expect(seen.has(b.time)).toBe(false)
        continue
      }
      const sofar = RANDOM_BARS.filter((c) => c.time >= day && c.time <= b.time)
      const hi = Math.max(...sofar.map((c) => c.high))
      const lo = Math.min(...sofar.map((c) => c.low))
      expect(seen.get(b.time)).toBe(((hi - lo) / atr) * 100)
    }
  })
})

describe('keyLevelsDef', () => {
  it('is registered as an overlay with no lines, defaults ATR 14, SMA 50, weekly off', () => {
    expect(getIndicatorDef('keylevels')).toBe(keyLevelsDef)
    expect(keyLevelsDef.placement).toBe('overlay')
    expect(keyLevelsDef.source).toBeNull()
    expect(keyLevelsDef.lines).toEqual([])
    expect(defaultInputs(keyLevelsDef)).toEqual({
      atrPeriod: 14,
      smaPeriod: 50,
      showDaily: 1,
      showWeekly: 0,
      showRange: 1,
      showBias: 1,
    })
    expect(keyLevelsParams(defaultInputs(keyLevelsDef))).toEqual({
      atrPeriod: 14,
      smaPeriod: 50,
      showWeekly: false,
    })
  })

  it('has a stepper that returns nothing', () => {
    const stepper = keyLevelsDef.createStepper(defaultInputs(keyLevelsDef), 'close')
    expect(stepper.next(FIXTURE_BARS[0])).toEqual({})
    expect(stepper.clone().next(FIXTURE_BARS[1])).toEqual({})
  })

  it('reads its params from inputs through the def', () => {
    const inputs = { ...defaultInputs(keyLevelsDef), atrPeriod: 3, smaPeriod: 3, showWeekly: 1 }
    const out = keyLevelsDef.keyLevels?.(FIXTURE_DAILY, FIXTURE_BARS, null, inputs)
    expect(out).toStrictEqual(computeKeyLevels(FIXTURE_DAILY, FIXTURE_BARS, null, FIXTURE_PARAMS))
  })

  it('draw-only toggles change neither the output nor the params key', () => {
    const on = { ...defaultInputs(keyLevelsDef), atrPeriod: 3, smaPeriod: 3 }
    const off = { ...on, showDaily: 0, showRange: 0, showBias: 0 }
    expect(keyLevelsParamsKey(keyLevelsParams(off))).toBe(keyLevelsParamsKey(keyLevelsParams(on)))
    expect(keyLevelsDef.keyLevels?.(FIXTURE_DAILY, FIXTURE_BARS, null, off)).toStrictEqual(
      keyLevelsDef.keyLevels?.(FIXTURE_DAILY, FIXTURE_BARS, null, on),
    )
  })

  it('the params key changes with each compute param', () => {
    const base: KeyLevelsParams = { atrPeriod: 14, smaPeriod: 50, showWeekly: false }
    const keys = [
      base,
      { ...base, atrPeriod: 15 },
      { ...base, smaPeriod: 51 },
      { ...base, showWeekly: true },
    ].map(keyLevelsParamsKey)
    expect(new Set(keys).size).toBe(4)
  })

  it('rounds and clamps periods, and falls back to defaults', () => {
    expect(keyLevelsParams({ atrPeriod: 0, smaPeriod: 9999 })).toEqual({
      atrPeriod: 1,
      smaPeriod: 500,
      showWeekly: false,
    })
    expect(keyLevelsParams({ atrPeriod: 2.6, smaPeriod: Number.NaN, showWeekly: 1 })).toEqual({
      atrPeriod: 3,
      smaPeriod: 50,
      showWeekly: true,
    })
    expect(keyLevelsParams({})).toEqual({ atrPeriod: 14, smaPeriod: 50, showWeekly: false })
  })

  it('labels the four levels', () => {
    expect(KEY_LEVEL_LABELS).toEqual({ pdh: 'PDH', pdl: 'PDL', pwh: 'PWH', pwl: 'PWL' })
  })
})
