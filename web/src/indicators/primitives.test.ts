import { describe, expect, it } from 'vitest'
import { Ema, RollingWindow, Sma, rma } from './primitives'
import { LegacyRollingWindow, U, prng } from './primitives.testkit'
import { priceOf } from './sources'

describe('RollingWindow', () => {
  it('keeps the last N values and computes population stdev', () => {
    const w = new RollingWindow(3)
    for (const v of [1, 2, 3, 4]) w.push(v)
    expect(w.values).toEqual([2, 3, 4])
    expect(w.mean()).toBe(3)
    // Population stdev of 2,3,4 = sqrt(2/3).
    expect(w.stdev()).toBeCloseTo(Math.sqrt(2 / 3), 12)
    expect(w.max()).toBe(4)
    expect(w.min()).toBe(2)
  })

  it('clone is independent', () => {
    const a = new RollingWindow(2)
    a.push(1)
    const b = a.clone()
    b.push(5)
    expect(a.values).toEqual([1])
    expect(b.values).toEqual([1, 5])
  })
})

describe('Sma', () => {
  it('warms up for period - 1 inputs', () => {
    const s = new Sma(3)
    expect([1, 2, 3, 4, 5].map((v) => s.next(v))).toEqual([undefined, undefined, 2, 3, 4])
  })
})

describe('Ema', () => {
  it('seeds with the SMA, then smooths with alpha 2/(n+1)', () => {
    const e = new Ema(3)
    const out = [2, 4, 6, 8, 10].map((v) => e.next(v))
    // Seed = (2+4+6)/3 = 4; alpha = 0.5: 4 + 0.5*(8-4) = 6; 6 + 0.5*(10-6) = 8.
    expect(out).toEqual([undefined, undefined, 4, 6, 8])
  })

  it('rma uses alpha 1/n', () => {
    const r = rma(2)
    // Seed (1+3)/2 = 2; then 2 + 0.5*(5-2) = 3.5.
    expect([1, 3, 5].map((v) => r.next(v))).toEqual([undefined, 2, 3.5])
  })

  it('clone replays identically', () => {
    const e = new Ema(2)
    e.next(1)
    e.next(2)
    const c = e.clone()
    expect(c.next(10)).toBe(e.next(10))
  })
})

describe('priceOf', () => {
  it('derives composite sources', () => {
    const c = { time: 0, open: 1, high: 4, low: 2, close: 3, volume: 0 }
    expect(priceOf(c, 'hl2')).toBe(3)
    expect(priceOf(c, 'hlc3')).toBe(3)
    expect(priceOf(c, 'ohlc4')).toBe(2.5)
  })
})

// --- Phase 19: ring buffer with running sums (see primitives.drift.test.ts for long series) ---

/** Same number, telling -0 from +0 and NaN from NaN as equal (Object.is). */
function same(a: number, b: number): boolean {
  return Object.is(a, b)
}

describe('RollingWindow ring buffer matches the fresh-sum window', () => {
  it('keeps the same values oldest first through many wraps', () => {
    const w = new RollingWindow(7)
    const old = new LegacyRollingWindow(7)
    const rand = prng(12)
    for (let i = 0; i < 500; i++) {
      const x = rand() * 100
      w.push(x)
      old.push(x)
      expect(w.values).toEqual(old.values)
      expect(w.full).toBe(old.full)
    }
  })

  it('partial windows (warm-up) use the old arithmetic exactly', () => {
    const w = new RollingWindow(50)
    const old = new LegacyRollingWindow(50)
    const rand = prng(13)
    for (let i = 0; i < 49; i++) {
      const x = 1000 + rand()
      w.push(x)
      old.push(x)
      expect(same(w.mean(), old.mean())).toBe(true)
      expect(same(w.stdev(), old.stdev())).toBe(true)
    }
  })

  it("track 'none' and 'sum' keep the old stdev; every track has the old max/min", () => {
    const rand = prng(14)
    for (const track of ['none', 'sum', 'squares'] as const) {
      const w = new RollingWindow(9, { track })
      const old = new LegacyRollingWindow(9)
      for (let i = 0; i < 300; i++) {
        const x = Math.round(rand() * 20) - 10
        w.push(x)
        old.push(x)
        if (track !== 'squares') expect(same(w.stdev(), old.stdev())).toBe(true)
        if (track === 'none') expect(same(w.mean(), old.mean())).toBe(true)
        expect(same(w.max(), old.max())).toBe(true)
        expect(same(w.min(), old.min())).toBe(true)
      }
    }
  })

  it('max/min follow Math.max/Math.min with ties, -0/+0, infinities and NaN', () => {
    const pool = [0, -0, 1, -1, 2, 2, Infinity, -Infinity, NaN, 0.5, -0]
    const rand = prng(15)
    for (const size of [1, 2, 3, 5, 8]) {
      const w = new RollingWindow(size)
      const old = new LegacyRollingWindow(size)
      for (let i = 0; i < 2_000; i++) {
        const x = pool[Math.floor(rand() * pool.length)]
        w.push(x)
        old.push(x)
        // Lazily built deques (first call after some pushes) and kept ones agree too.
        if (i < 50 && i % 7 !== 0) continue
        expect(same(w.max(), old.max())).toBe(true)
        expect(same(w.min(), old.min())).toBe(true)
      }
    }
  })

  it('non-finite values give the old non-finite results and leave no trace', () => {
    const rand = prng(16)
    const specials = [NaN, Infinity, -Infinity]
    const w = new RollingWindow(4)
    const old = new LegacyRollingWindow(4)
    for (let i = 0; i < 3_000; i++) {
      const x = rand() < 0.05 ? specials[Math.floor(rand() * 3)] : 50 + rand()
      w.push(x)
      old.push(x)
      if (!old.full) continue
      const m = old.mean()
      const s = old.stdev()
      if (!Number.isFinite(m)) expect(same(w.mean(), m)).toBe(true)
      else expect(Math.abs(w.mean() - m)).toBeLessThanOrEqual(1e-12 * 51)
      if (!Number.isFinite(s)) expect(same(w.stdev(), s)).toBe(true)
      else expect(Math.abs(w.stdev() - s)).toBeLessThanOrEqual(1e-12 * 51)
    }
  })

  it('a clone taken anywhere replays bit-identically, independent of the original', () => {
    const rand = prng(17)
    const xs = Array.from({ length: 3_000 }, () => 100 + rand() * 5)
    for (const k of [3, 19, 20, 21, 777, 2_000]) {
      const a = new RollingWindow(20)
      for (const x of xs.slice(0, k)) a.push(x)
      a.max()
      const b = a.clone()
      for (const x of xs.slice(k)) {
        a.push(x)
        b.push(x)
        expect(same(a.mean(), b.mean())).toBe(true)
        expect(same(a.stdev(), b.stdev())).toBe(true)
        expect(same(a.max(), b.max())).toBe(true)
        expect(same(a.min(), b.min())).toBe(true)
      }
      b.push(1e9)
      expect(a.max()).toBeLessThan(1e9)
    }
  })

  it('Sma uses a running sum and matches the fresh mean within the bound', () => {
    const s = new Sma(30)
    const old = new LegacyRollingWindow(30)
    const rand = prng(18)
    for (let i = 0; i < 5_000; i++) {
      const x = 20_000 * (1 + rand() * 0.1)
      const v = s.next(x)
      old.push(x)
      if (!old.full) expect(v).toBeUndefined()
      else expect(Math.abs((v as number) - old.mean())).toBeLessThanOrEqual(214 * U * 22_000)
    }
  })
})
