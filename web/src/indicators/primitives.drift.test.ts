// Phase 19: the ring-buffer RollingWindow against the pre-Phase-19 window (fresh sums per call)
// on long series. The bounds asserted here are the ones documented on RollingWindow:
//   mean:  |new - old| <= (12 N + 24) u M   (bar-time re-sums, at most 2N pushes apart)
//   stdev: |new - old| <= 1.01 VARIANCE_REL_TOL sd + (2 N + 4) u M + N u sd
// with u = 2^-53, N = window size, M = max |x| over the series (looser than the last 3N values).
// Each series runs three ways: unstamped pushes (re-sum every N), evenly spaced bar times, and
// bar times with random outages (irregular re-sums, the 2N cap).
import { describe, expect, it } from 'vitest'
import { MEAN_REL_TOL, RollingWindow, VARIANCE_REL_TOL, firstResum, resumesAt } from './primitives'
import { LegacyRollingWindow, U, prng } from './primitives.testkit'

/**
 * Geometric random walk around `scale` with the hard cases for running sums spliced in: a 50%
 * jump into a long exactly-flat stretch, a micro-volatility stretch (stablecoin-like, relative
 * noise 1e-9), a strong trend, and a crash back down.
 */
function hardWalk(n: number, scale: number, seed: number): number[] {
  const rand = prng(seed)
  const out: number[] = []
  let x = scale
  for (let i = 0; i < n; i++) {
    const f = i / n
    if (f >= 0.4 && f < 0.42) {
      if (Math.abs(f - 0.4) < 1 / n) x *= 1.5
      // exactly flat: x unchanged
    } else if (f >= 0.6 && f < 0.64) {
      out.push(x * (1 + (rand() - 0.5) * 2e-9))
      continue
    } else if (f >= 0.8 && f < 0.82) {
      x *= 1.003
    } else if (f >= 0.82 && f < 0.83) {
      x *= 0.99
    } else {
      x *= 1 + (rand() - 0.5) * 0.004
    }
    out.push(x)
  }
  return out
}

interface Drift {
  meanRatio: number
  sdRatio: number
}

/** Bar times: evenly spaced minutes, or minutes with random outages of up to 3N bars. */
function barTimes(n: number, size: number, gaps: boolean, seed: number): number[] {
  const rand = prng(seed)
  const out: number[] = []
  let t = 1_600_000_000
  for (let i = 0; i < n; i++) {
    t += 60 * (gaps && rand() < 0.01 ? 1 + Math.floor(rand() * 3 * size) : 1)
    out.push(t)
  }
  return out
}

/** Push `xs` through both windows; return the worst |diff| / bound for mean and stdev. */
function compare(xs: readonly number[], size: number): Drift {
  let meanRatio = 0
  let sdRatio = 0
  for (const mode of ['unstamped', 'even', 'gaps'] as const) {
    const times = mode === 'unstamped' ? null : barTimes(xs.length, size, mode === 'gaps', size)
    const d = compareRun(xs, size, times)
    meanRatio = Math.max(meanRatio, d.meanRatio)
    sdRatio = Math.max(sdRatio, d.sdRatio)
  }
  return { meanRatio, sdRatio }
}

function compareRun(xs: readonly number[], size: number, times: number[] | null): Drift {
  const fresh = new RollingWindow(size)
  const sma = new RollingWindow(size, { track: 'sum' })
  const old = new LegacyRollingWindow(size)
  let m = 0
  for (const x of xs) m = Math.max(m, Math.abs(x))
  let meanRatio = 0
  let sdRatio = 0
  for (let i = 0; i < xs.length; i++) {
    const x = xs[i]
    const t = times ? times[i] : undefined
    fresh.push(x, t)
    sma.push(x, t)
    old.push(x)
    if (!old.full) continue
    const oldMean = old.mean()
    const meanBound = (12 * size + 24) * U * m
    meanRatio = Math.max(
      meanRatio,
      Math.abs(fresh.mean() - oldMean) / meanBound,
      Math.abs(sma.mean() - oldMean) / meanBound,
    )
    const sd = fresh.stdev()
    const sdBound = 1.01 * VARIANCE_REL_TOL * sd + (2 * size + 4) * U * m + size * U * sd
    sdRatio = Math.max(sdRatio, Math.abs(sd - old.stdev()) / sdBound)
  }
  return { meanRatio, sdRatio }
}

describe('RollingWindow drift against the fresh-sum window', () => {
  // 150k bars (the long-history case) with the longest period the inputs allow.
  for (const scale of [1e-8, 1e5]) {
    it(`150k-bar walk at price scale ${scale}, period 500, stays inside the bounds`, () => {
      const d = compare(hardWalk(150_000, scale, 17), 500)
      expect(d.meanRatio).toBeLessThanOrEqual(1)
      expect(d.sdRatio).toBeLessThanOrEqual(1)
    })
  }

  for (const scale of [1, 1e12]) {
    for (const size of [2, 20, 200]) {
      it(`40k-bar walk at price scale ${scale}, period ${size}, stays inside the bounds`, () => {
        const d = compare(hardWalk(40_000, scale, 31 + size), size)
        expect(d.meanRatio).toBeLessThanOrEqual(1)
        expect(d.sdRatio).toBeLessThanOrEqual(1)
      })
    }
  }

  it('period 1 is the value itself with zero deviation', () => {
    const w = new RollingWindow(1)
    for (const x of hardWalk(2_000, 3, 5)) {
      w.push(x)
      expect(w.mean()).toBe(x)
      expect(w.stdev()).toBe(0)
    }
  })

  it('an exactly flat window after a jump has (near) zero width, not drift', () => {
    // A move of 50% then 1000 identical values: the old two-pass stdev is ~0; without the
    // variance guard the running sum of squares would leave about sqrt(N u) x the move.
    const w = new RollingWindow(500)
    const rand = prng(3)
    for (let i = 0; i < 5_000; i++) w.push(100 + rand() * 10)
    for (let i = 0; i < 1_000; i++) w.push(150)
    expect(w.mean()).toBeCloseTo(150, 10)
    expect(w.stdev()).toBeLessThanOrEqual(500 * U * 150)
  })

  it('integer windows stay exact in the mean after many re-sums', () => {
    // Small integers: every running-sum step is exact, and k is the plain mean at each re-sum.
    const w = new RollingWindow(4, { track: 'sum' })
    const old = new LegacyRollingWindow(4)
    const rand = prng(8)
    for (let i = 0; i < 10_000; i++) {
      const x = Math.floor(rand() * 1000) * 4
      w.push(x)
      old.push(x)
      if (old.full) expect(w.mean()).toBe(old.mean())
    }
  })
})

/**
 * Worst |new mean - old mean| / (|old mean| x the documented relative bound) over every full
 * window, for both tracks that keep a running sum ('sum' for SMA, 'squares' for Bollinger), in
 * the three push modes. The bound for non-negative windows (prices, volumes, %K):
 *   |new - old| <= (1.01 MEAN_REL_TOL + (1.01 N + 3) u) |mean|
 * (the running-sum guard, plus the old fresh left-to-right sum's own error and two roundings).
 */
function relativeMeanRatio(xs: readonly number[], size: number): number {
  let worst = 0
  for (const mode of ['unstamped', 'even', 'gaps'] as const) {
    const times = mode === 'unstamped' ? null : barTimes(xs.length, size, mode === 'gaps', size)
    const sum = new RollingWindow(size, { track: 'sum' })
    const squares = new RollingWindow(size)
    const old = new LegacyRollingWindow(size)
    for (let i = 0; i < xs.length; i++) {
      const t = times ? times[i] : undefined
      sum.push(xs[i], t)
      squares.push(xs[i], t)
      old.push(xs[i])
      if (!old.full) continue
      const ref = old.mean()
      const bound = 1.01 * (1.01 * MEAN_REL_TOL + (1.01 * size + 3) * U) * Math.abs(ref)
      for (const w of [sum, squares]) {
        const diff = Math.abs(w.mean() - ref)
        // A zero mean (all-zero window) must come out exactly zero.
        worst = Math.max(worst, bound === 0 ? (diff === 0 ? 0 : Infinity) : diff / bound)
      }
    }
  }
  return worst
}

/** Thin volume (0.000123 .. 0.001123) with one 1.23e9 print at `at`. */
function volumeSpike(n: number, at: number, seed: number): number[] {
  const rand = prng(seed)
  return Array.from({ length: n }, (_, i) => (i === at ? 1.23e9 : 0.000123 + rand() * 0.001))
}

/** A 60k walk that crashes to 1e-4 (in `steps` bars) and keeps walking there. */
function crash(n: number, at: number, steps: number, seed: number): number[] {
  const rand = prng(seed)
  const out: number[] = []
  let x = 60_000
  const f = Math.pow(1e-4 / 60_000, 1 / steps)
  for (let i = 0; i < n; i++) {
    x = i >= at && i < at + steps ? x * f : x * (1 + (rand() - 0.5) * 0.004)
    out.push(x)
  }
  return out
}

describe('running-sum mean guard: large values leaving the window', () => {
  for (const size of [2, 3, 20, 200, 500]) {
    it(`a 1.23e9 volume spike in thin volume, period ${size}, agrees to ~1e-10 of the mean`, () => {
      const n = 6 * size + 50
      // The spike at several phases relative to the re-sum schedule.
      for (const at of [size + 1, 2 * size + 7, 3 * size - 1]) {
        expect(relativeMeanRatio(volumeSpike(n, at, at), size)).toBeLessThanOrEqual(1)
      }
    })

    it(`a 60k -> 1e-4 crash, period ${size}, agrees to ~1e-10 of the mean`, () => {
      const n = 8 * size + 50
      for (const steps of [1, 3, Math.max(1, Math.floor(size / 2))]) {
        expect(relativeMeanRatio(crash(n, 2 * size + 5, steps, steps), size)).toBeLessThanOrEqual(1)
      }
    })
  }

  it('repeated spikes and zero-volume stretches stay inside the relative bound', () => {
    const rand = prng(77)
    const xs = Array.from({ length: 20_000 }, (_, i) => {
      if (i % 997 === 0) return 1e8 * (1 + rand())
      if (Math.floor(i / 300) % 7 === 3) return 0
      return 0.001 * rand()
    })
    for (const size of [5, 20, 100]) expect(relativeMeanRatio(xs, size)).toBeLessThanOrEqual(1)
  })

  it('the existing max|x| bound and the stdev bound still hold through a spike and a crash', () => {
    for (const size of [20, 200]) {
      for (const xs of [volumeSpike(4_000, 1_000, 1), crash(4_000, 1_000, 2, 2)]) {
        const d = compare(xs, size)
        expect(d.meanRatio).toBeLessThanOrEqual(1)
        expect(d.sdRatio).toBeLessThanOrEqual(1)
      }
    }
  })

  it('a clone taken before the spike leaves takes the same guard decisions', () => {
    const xs = volumeSpike(400, 50, 9)
    const a = new RollingWindow(20, { track: 'sum' })
    for (let i = 0; i < 55; i++) a.push(xs[i], 1_600_000_000 + i * 60)
    const b = a.clone()
    for (let i = 55; i < xs.length; i++) {
      a.push(xs[i], 1_600_000_000 + i * 60)
      b.push(xs[i], 1_600_000_000 + i * 60)
      expect(Object.is(a.mean(), b.mean())).toBe(true)
    }
  })
})

describe('bar-time re-sums', () => {
  it('fire exactly once every N evenly spaced bars, whatever the phase', () => {
    for (const size of [2, 3, 20, 500]) {
      for (const offset of [0, 7, 59, 1234]) {
        let hits = 0
        const t0 = 1_600_000_000 + offset * 60
        for (let i = 1; i <= size * 40; i++) {
          if (resumesAt(t0 + (i - 1) * 60, t0 + i * 60, size)) hits++
        }
        expect(hits).toBe(40)
      }
    }
  })

  it('size 1 re-sums on every bar; a non-increasing time never triggers one', () => {
    expect(resumesAt(100, 160, 1)).toBe(true)
    expect(resumesAt(160, 160, 20)).toBe(false)
    expect(resumesAt(160, 100, 20)).toBe(false)
  })

  it('a window fed the same bars from a later start reaches the same state at a re-sum', () => {
    const rand = prng(9)
    const n = 3_000
    const xs = Array.from({ length: n }, () => 1e4 * (1 + rand() * 0.01))
    const times = barTimes(n, 50, true, 4)
    const bars = times.map((time) => ({ time }))
    const a = new RollingWindow(50)
    const b = new RollingWindow(50)
    const start = 333
    for (let i = 0; i < start; i++) a.push(xs[i], times[i])
    const sync = firstResum(bars, start + 50, 50) as number
    expect(sync).not.toBeNull()
    for (let i = start; i < n; i++) {
      a.push(xs[i], times[i])
      b.push(xs[i], times[i])
      if (i >= sync) {
        expect(Object.is(a.mean(), b.mean())).toBe(true)
        expect(Object.is(a.stdev(), b.stdev())).toBe(true)
      }
    }
  })
})
