import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { computeIndicator, defaultInputs } from './compute'
import {
  VOLUME_PROFILE_HINDSIGHT,
  computeVolumeProfile,
  extendOpenProfile,
  openProfileBase,
  openProfileFits,
  volumeProfileDef,
} from './volumeProfile'
import type { VolumeProfile } from './types'

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

const T0 = 1_700_006_400
const H = 3600

function randomCandles(n: number, seed: number): Candle[] {
  const rnd = mulberry32(seed)
  const out: Candle[] = []
  let prev = 100
  for (let i = 0; i < n; i++) {
    const open = prev
    const close = Math.max(1, open * (1 + (rnd() - 0.5) * 0.04))
    const high = Math.max(open, close) * (1 + rnd() * 0.01)
    const low = Math.min(open, close) * (1 - rnd() * 0.01)
    out.push({ time: T0 + i * H, open, high, low, close, volume: rnd() * 1000 })
    prev = close
  }
  return out
}

/** Copy of `candles` with every bar after index t changed (prices and volume). */
function perturbAfter(candles: readonly Candle[], t: number, seed: number): Candle[] {
  const rnd = mulberry32(seed)
  return candles.map((c, i) => {
    if (i <= t) return c
    const f = 0.5 + rnd() * 2
    return {
      ...c,
      open: c.open * f,
      high: c.high * f * 1.01,
      low: c.low * f * 0.99,
      close: c.close * f,
      volume: rnd() * 5000,
    }
  })
}

function bar(i: number, open: number, high: number, low: number, close: number, volume: number) {
  return { time: T0 + i * H, open, high, low, close, volume }
}

function inputs(from: number, to = 0, rows = 24, valueArea = 70) {
  return { from, to, rows, valueArea }
}

function profileOf(candles: readonly Candle[], from: number, to = 0, rows = 24, va = 70) {
  const p = volumeProfileDef.profile?.(candles, inputs(from, to, rows, va))
  if (p === undefined) throw new Error('volumeProfileDef has no profile()')
  return p
}

function rowSum(p: VolumeProfile): number {
  return p.rows.reduce((s, r) => s + r.volume, 0)
}

describe('volumeProfileDef', () => {
  it('is a range indicator: no lines, a stepper that returns {}, time inputs defaulting to 0', () => {
    expect(volumeProfileDef.id).toBe('vprofile')
    expect(volumeProfileDef.lines).toEqual([])
    expect(volumeProfileDef.source).toBeNull()
    const byKey = Object.fromEntries(volumeProfileDef.inputs.map((i) => [i.key, i]))
    expect(byKey.from).toMatchObject({ kind: 'time', default: 0 })
    expect(byKey.to).toMatchObject({ kind: 'time', default: 0 })
    expect(byKey.rows).toMatchObject({ default: 24, min: 6, max: 200, integer: true })
    expect(byKey.valueArea).toMatchObject({ default: 70, min: 50, max: 95, integer: true })
    const s = volumeProfileDef.createStepper(defaultInputs(volumeProfileDef), 'close')
    const candles = randomCandles(10, 1)
    expect(candles.map((c) => s.next(c))).toEqual(candles.map(() => ({})))
    expect(s.clone().next(candles[0])).toEqual({})
    expect(computeIndicator(volumeProfileDef, candles, defaultInputs(volumeProfileDef))).toEqual({})
  })

  it('matches a hand-computed 3-bar profile (bucket split, POC, value area)', () => {
    // Range [0, 4], 4 rows of height 1: [0,1) [1,2) [2,3) [3,4].
    // Bar A: 0..4, up, vol 8 -> 2 per row (up).
    // Bar B: 2..4, down, vol 4 -> rows 2 and 3 get 2 each.
    // Bar C: high == low == 1, up, vol 2 -> all in row 1 (its lower edge).
    // Volumes [2, 4, 4, 4], up [2, 4, 2, 2], total 14. POC: lowest of the tied rows = row 1,
    // middle 1.5. VA 70% (target 9.8): row1 4 -> above row2 (4) beats below row0 (2): 8 ->
    // above row3 (4) beats row0: 12 >= 9.8. VAH 4, VAL 1.
    const candles = [bar(0, 1, 4, 0, 3, 8), bar(1, 4, 4, 2, 2, 4), bar(2, 1, 1, 1, 1, 2)]
    const p = profileOf(candles, T0, 0, 4, 70)
    expect(p).not.toBeNull()
    if (p === null) return
    expect(p.rows).toEqual([
      { low: 0, high: 1, volume: 2, upVolume: 2 },
      { low: 1, high: 2, volume: 4, upVolume: 4 },
      { low: 2, high: 3, volume: 4, upVolume: 2 },
      { low: 3, high: 4, volume: 4, upVolume: 2 },
    ])
    expect(p.poc).toBe(1.5)
    expect(p.vah).toBe(4)
    expect(p.val).toBe(1)
    expect(p.totalVolume).toBe(14)
    expect(p.from).toBe(T0)
    expect(p.to).toBe(T0 + 2 * H)
    // VA 50% (target 7): row1 4 + row2 4 = 8 >= 7 -> VAH 3, VAL 1.
    const p50 = profileOf(candles, T0, 0, 4, 50)
    expect(p50?.vah).toBe(3)
    expect(p50?.val).toBe(1)
  })

  it('value area: ties go above, the top row includes its upper edge, then it expands below', () => {
    // Range [0, 5], 5 rows of height 1. Single-price bars: 0 (vol 3) -> row0, 1.5 (1) -> row1,
    // 2.5 (5) -> row2, 3.5 (1) -> row3, 5 (3) -> row4 (top edge is inclusive). Total 13.
    const candles = [
      bar(0, 0, 0, 0, 0, 3),
      bar(1, 1.5, 1.5, 1.5, 1.5, 1),
      bar(2, 2.5, 2.5, 2.5, 2.5, 5),
      bar(3, 3.5, 3.5, 3.5, 3.5, 1),
      bar(4, 5, 5, 5, 5, 3),
    ]
    const p50 = computeVolumeProfile(candles, T0, 0, 5, 50)
    expect(p50?.rows.map((r) => r.volume)).toEqual([3, 1, 5, 1, 3])
    expect(p50?.poc).toBe(2.5)
    // Target 6.5: row2 5 -> tie row3 (1) vs row1 (1) goes above: 6 -> row4 (3) beats row1 (1): 9.
    expect(p50?.vah).toBe(5)
    expect(p50?.val).toBe(2)
    // Target 9.1: ... 9 -> nothing above, so row1: 10 -> VAL 1.
    const p70 = computeVolumeProfile(candles, T0, 0, 5, 70)
    expect(p70?.vah).toBe(5)
    expect(p70?.val).toBe(1)
  })

  it('a single-price range puts all volume in the middle row and the POC on that price', () => {
    const candles = [bar(0, 50, 50, 50, 50, 2), bar(1, 50, 50, 50, 50, 3)]
    for (const rows of [6, 7, 24]) {
      const p = computeVolumeProfile(candles, T0, 0, rows, 70)
      expect(p).not.toBeNull()
      if (p === null) return
      const mid = Math.floor(rows / 2)
      expect(p.rows).toHaveLength(rows)
      expect(p.rows.map((r) => r.volume)).toEqual(p.rows.map((_, i) => (i === mid ? 5 : 0)))
      expect(p.poc).toBe(50)
      expect(p.rows[mid].low).toBeLessThan(50)
      expect(p.rows[mid].high).toBeGreaterThan(50)
      expect(p.val).toBe(p.rows[mid].low)
      expect(p.vah).toBe(p.rows[mid].high)
      expect(p.vah - p.val).toBeLessThan(1e-3)
      // Rows are contiguous with equal heights.
      for (let i = 1; i < rows; i++) expect(p.rows[i].low).toBe(p.rows[i - 1].high)
    }
  })

  it('returns null when from is 0, no bar is in range, or the range has no volume', () => {
    const candles = randomCandles(20, 2)
    expect(profileOf(candles, 0)).toBeNull()
    expect(profileOf(candles, candles[19].time + 1)).toBeNull()
    expect(profileOf(candles, candles[5].time + 1, candles[5].time + 2)).toBeNull()
    expect(profileOf([], T0)).toBeNull()
    const zeroVol = candles.map((c) => ({ ...c, volume: 0 }))
    expect(profileOf(zeroVol, T0)).toBeNull()
  })

  it('conserves volume: rows sum to the total within 1e-9 relative, upVolume <= volume', () => {
    const candles = randomCandles(500, 3)
    for (const rows of [6, 24, 77, 200]) {
      const p = profileOf(candles, candles[10].time, candles[400].time, rows)
      expect(p).not.toBeNull()
      if (p === null) return
      const expected = candles.slice(10, 401).reduce((s, c) => s + c.volume, 0)
      expect(Math.abs(p.totalVolume - expected)).toBeLessThanOrEqual(1e-9 * expected)
      expect(Math.abs(rowSum(p) - p.totalVolume)).toBeLessThanOrEqual(1e-9 * p.totalVolume)
      const upExpected = candles
        .slice(10, 401)
        .filter((c) => c.close >= c.open)
        .reduce((s, c) => s + c.volume, 0)
      const upSum = p.rows.reduce((s, r) => s + r.upVolume, 0)
      expect(Math.abs(upSum - upExpected)).toBeLessThanOrEqual(1e-9 * p.totalVolume)
      for (const r of p.rows) expect(r.upVolume).toBeLessThanOrEqual(r.volume + 1e-9 * r.volume)
      // Rows span [lowest low, highest high] with equal heights.
      const lows = candles.slice(10, 401).map((c) => c.low)
      const highs = candles.slice(10, 401).map((c) => c.high)
      expect(p.rows[0].low).toBe(Math.min(...lows))
      expect(p.rows[rows - 1].high).toBe(Math.max(...highs))
      // POC row has the max volume and the value area holds at least 70% of it.
      const maxVol = Math.max(...p.rows.map((r) => r.volume))
      const pocRow = p.rows.find((r) => r.volume === maxVol)
      expect(p.poc).toBe(((pocRow?.low ?? NaN) + (pocRow?.high ?? NaN)) / 2)
      const inVa = p.rows
        .filter((r) => r.low >= p.val && r.high <= p.vah)
        .reduce((s, r) => s + r.volume, 0)
      expect(inVa).toBeGreaterThanOrEqual(0.7 * p.totalVolume * (1 - 1e-12))
      expect(p.val).toBeLessThanOrEqual(p.poc)
      expect(p.vah).toBeGreaterThanOrEqual(p.poc)
    }
  })

  it('ignores bars before from', () => {
    const candles = randomCandles(100, 4)
    const from = candles[30].time
    const a = profileOf(candles, from, candles[80].time)
    // Wild bars before `from` (huge volume, far-away prices) change nothing.
    const changed = candles.map((c, i) =>
      i < 30 ? { ...c, high: c.high * 10, low: c.low / 10, volume: 1e9 } : c,
    )
    expect(profileOf(changed, from, candles[80].time)).toEqual(a)
    expect(a?.from).toBe(from)
    expect(a?.to).toBe(candles[80].time)
    // A `from` between bars starts at the next bar.
    expect(profileOf(candles, from - 1, candles[80].time)).toEqual(a)
  })

  it('has no look-ahead: bars after `to` changed or appended leave the profile identical', () => {
    const candles = randomCandles(300, 5)
    const from = candles[20].time
    for (const t of [20, 21, 150, 298]) {
      const to = candles[t].time
      const a = profileOf(candles, from, to)
      expect(profileOf(perturbAfter(candles, t, 7 + t), from, to)).toEqual(a)
      const appended = [...candles, { ...candles[299], time: candles[299].time + H, volume: 1e6 }]
      expect(profileOf(appended, from, to)).toEqual(a)
    }
  })

  it('with to = 0 the profile over candles[0..k] never depends on bars after k', () => {
    const candles = randomCandles(300, 6)
    const from = candles[10].time
    for (const k of [10, 11, 100, 298]) {
      const prefix = candles.slice(0, k + 1)
      const a = profileOf(prefix, from, 0)
      // Same as the bounded range ending at bar k computed on a series whose later bars changed.
      expect(profileOf(perturbAfter(candles, k, 3 + k), from, candles[k].time)).toEqual(a)
      expect(a?.to).toBe(candles[k].time)
      // A perturbed prefix copy (bars <= k identical) gives the bit-identical result.
      expect(profileOf(perturbAfter(candles, k, 99).slice(0, k + 1), from, 0)).toEqual(a)
    }
  })
  it('returns null when `from` is before the first loaded bar (anchor bar not loaded)', () => {
    const candles = randomCandles(50, 8)
    // From one bar before the history, or between the missing bar and the first loaded one.
    expect(profileOf(candles, candles[0].time - H)).toBeNull()
    expect(profileOf(candles, candles[0].time - 1, candles[20].time)).toBeNull()
    // `from` on the first loaded bar is fine and starts there.
    expect(profileOf(candles, candles[0].time)?.from).toBe(candles[0].time)
    // Once older history is loaded the same `from` gives a profile starting at the next bar.
    const older = { ...candles[0], time: candles[0].time - H }
    const grown = [older, ...candles]
    expect(profileOf(grown, candles[0].time - 1)?.from).toBe(candles[0].time)
  })

  it('a single-price range at a tiny price keeps every row above 0 and the POC on the price', () => {
    const p = 2e-7
    const prof = computeVolumeProfile([bar(0, p, p, p, p, 5)], T0, 0, 24, 70)
    expect(prof).not.toBeNull()
    if (prof === null) return
    for (const r of prof.rows) expect(r.low).toBeGreaterThan(0)
    expect(prof.poc).toBe(p)
    expect(prof.rows[12].volume).toBe(5)
    expect(prof.rows[12].low).toBeLessThan(p)
    expect(prof.rows[12].high).toBeGreaterThan(p)
    // Price 0 still gets a band of height 1e-6.
    const zero = computeVolumeProfile([bar(0, 0, 0, 0, 0, 5)], T0, 0, 24, 70)
    expect(zero?.poc).toBe(0)
    expect((zero?.vah ?? 0) - (zero?.val ?? 0)).toBeGreaterThan(0)
  })

  it('a single-price bar exactly on a row edge goes to the row above it', () => {
    // lo 78.2, hi 89.7, 45 rows: 80.5 is the low edge of row 9 in exact arithmetic, and
    // floor((80.5 - 78.2) / h) rounds to 8.
    const candles = [bar(0, 78.2, 89.7, 78.2, 89.7, 0), bar(1, 80.5, 80.5, 80.5, 80.5, 7)]
    const prof = computeVolumeProfile(candles, T0, 0, 45, 70)
    expect(prof).not.toBeNull()
    if (prof === null) return
    const h = (89.7 - 78.2) / 45
    expect(Math.floor((80.5 - 78.2) / h)).toBe(8)
    const row = prof.rows.findIndex((r) => r.volume > 0)
    expect(prof.rows[row].volume).toBe(7)
    expect(prof.rows[row].low).toBeLessThanOrEqual(80.5)
    expect(80.5).toBeLessThan(prof.rows[row].high)
    expect(row).toBe(9)
    // The top edge stays inclusive.
    const top = computeVolumeProfile(
      [bar(0, 78.2, 89.7, 78.2, 89.7, 0), bar(1, 89.7, 89.7, 89.7, 89.7, 3)],
      T0,
      0,
      45,
      70,
    )
    expect(top?.rows[44].volume).toBe(3)
  })

  it('treats missing, non-finite and negative volume as 0', () => {
    const candles = randomCandles(30, 9)
    const a = profileOf(candles, candles[0].time)
    const dirty = [
      ...candles,
      { ...candles[29], time: candles[29].time + H, volume: Number.NaN },
      { ...candles[29], time: candles[29].time + 2 * H, volume: -5 },
      { ...candles[29], time: candles[29].time + 3 * H, volume: Number.POSITIVE_INFINITY },
    ]
    const b = profileOf(dirty, candles[0].time)
    expect(b?.totalVolume).toBe(a?.totalVolume)
    expect(b?.rows.map((r) => r.volume)).toEqual(a?.rows.map((r) => r.volume))
  })

  it('open-ended base + forming bar equals the full computation bit for bit', () => {
    const candles = randomCandles(400, 10)
    for (const start of [0, 37, 398, 399]) {
      for (const rows of [6, 24, 101]) {
        const last = candles.length - 1
        const base = openProfileBase(candles, start, last, rows)
        expect(openProfileFits(base, candles[last])).toBe(true)
        expect(extendOpenProfile(base, candles[last], 70)).toEqual(
          computeVolumeProfile(candles, candles[start].time, 0, rows, 70),
        )
      }
    }
    // A forming bar beyond the span no longer fits the grid.
    const base = openProfileBase(candles, 10, 399, 24)
    expect(openProfileFits(base, { ...candles[399], high: 1e9 })).toBe(false)
    expect(openProfileFits(base, { ...candles[399], low: -1 })).toBe(false)
  })

  it('states the hindsight caveat in its description', () => {
    expect(volumeProfileDef.description).toContain(VOLUME_PROFILE_HINDSIGHT)
    expect(VOLUME_PROFILE_HINDSIGHT).toMatch(/hindsight, not a signal/)
  })
})
