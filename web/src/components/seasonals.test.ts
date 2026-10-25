import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { MarketDataError, type MarketDataProvider } from '../data/provider'
import type { Candle, Interval } from '../types'
import {
  DAY_SEC,
  DEFAULT_MAX_BARS,
  MULTIPLE_COMPARISONS_NOTE,
  PAGE_SIZE,
  SMALL_N_MONTH,
  SMALL_N_WEEKDAY,
  computeSeasonals,
  deviation,
  historyTruncated,
  incompleteReason,
  isNotable,
  loadDailyHistory,
  nextTabIndex,
  noiseHalfWidth,
  noneComparable,
  pooledMean,
  returnStats,
  tQuantile975,
  weekdayBar,
  weekdayIndex,
  weekdayScale,
  type MonthCell,
  type ReturnStats,
  type SeasonalsResult,
} from './seasonals'
import {
  HeatLegend,
  MonthlyTable,
  NoiseLegend,
  SeasonalsView,
  SummaryMeta,
  WeekdayTable,
} from './SeasonalsView'

const utc = (y: number, m: number, d: number, h = 0) => Date.UTC(y, m, d, h) / 1000

function bar(time: number, close: number): Candle {
  return { time, open: close, high: close, low: close, close, volume: 1 }
}

/** One daily bar per UTC day from `start`, closes taken from `closes` in order. */
function series(start: number, closes: readonly number[]): Candle[] {
  return closes.map((c, i) => bar(start + i * DAY_SEC, c))
}

/** Every day from `from` to `to` inclusive, close given by `closeOf(time)`. */
function days(from: number, to: number, closeOf: (t: number) => number): Candle[] {
  const out: Candle[] = []
  for (let t = from; t <= to; t += DAY_SEC) out.push(bar(t, closeOf(t)))
  return out
}

function cell(r: SeasonalsResult, year: number, month: number): MonthCell | undefined {
  return r.monthly.find((c) => c.year === year && c.month === month)
}

// Floating-point tolerance for ratios of small integers: only rounding error is possible.
const EPS = 12

describe('returnStats', () => {
  it('uses the middle value for odd n', () => {
    const s = returnStats([0.03, -0.01, 0.02])
    expect(s.n).toBe(3)
    expect(s.median).toBeCloseTo(0.02, EPS)
    expect(s.mean).toBeCloseTo(0.04 / 3, EPS)
    expect(s.positivePct).toBeCloseTo((2 / 3) * 100, EPS)
  })

  it('averages the middle two for even n', () => {
    const s = returnStats([0.04, -0.02, 0.01, 0.02])
    expect(s.median).toBeCloseTo(0.015, EPS)
    expect(s.positivePct).toBe(75)
  })

  it('counts zero as not positive', () => {
    expect(returnStats([0, 0.01]).positivePct).toBe(50)
  })

  it('is all null for an empty sample', () => {
    expect(returnStats([])).toEqual({
      n: 0,
      mean: null,
      median: null,
      positivePct: null,
      sd: null,
      se: null,
    })
  })

  it('has no sd or se for a single observation', () => {
    const s = returnStats([0.05])
    expect(s.mean).toBeCloseTo(0.05, EPS)
    expect(s.sd).toBeNull()
    expect(s.se).toBeNull()
  })

  it('uses the n - 1 sample sd for n = 2', () => {
    // [0.01, 0.03]: mean 0.02, squared deviations 1e-4 + 1e-4, / (n - 1) = 2e-4.
    const s = returnStats([0.01, 0.03])
    expect(s.sd).toBeCloseTo(Math.sqrt(2e-4), EPS)
    expect(s.se).toBeCloseTo(Math.sqrt(2e-4) / Math.sqrt(2), EPS) // = 0.01
    expect(s.se).toBeCloseTo(0.01, EPS)
  })

  it('computes sd and se for a known sample', () => {
    // 2, 4, 4, 4, 5, 5, 7, 9 (as %): mean 5, sum of squared deviations 32, sample var 32/7.
    const s = returnStats([0.02, 0.04, 0.04, 0.04, 0.05, 0.05, 0.07, 0.09])
    expect(s.mean).toBeCloseTo(0.05, EPS)
    expect(s.sd).toBeCloseTo(Math.sqrt(32 / 7) / 100, EPS)
    expect(s.se).toBeCloseTo(Math.sqrt(32 / 7) / 100 / Math.sqrt(8), EPS)
  })

  it('has zero sd when every value is equal', () => {
    const s = returnStats([0.01, 0.01, 0.01])
    expect(s.sd).toBeCloseTo(0, EPS)
    expect(s.se).toBeCloseTo(0, EPS)
  })
})

const stats = (mean: number | null, se: number | null, n = 10): ReturnStats => ({
  n,
  mean,
  median: mean,
  positivePct: null,
  sd: se === null ? null : se * Math.sqrt(n),
  se,
})

describe('tQuantile975 (upper 0.975 Student-t quantile)', () => {
  // Published two-sided 95% t critical values; 3 decimals is the precision they are quoted at.
  it.each([
    [1, 12.706],
    [2, 4.303],
    [5, 2.571],
    [10, 2.228],
    [29, 2.045],
    [100, 1.984],
  ])('t(0.975, %i) = %f', (df, want) => {
    expect(tQuantile975(df)).toBeCloseTo(want, 3)
  })

  it('tends to the normal 1.960 as df grows', () => {
    expect(tQuantile975(Infinity)).toBeCloseTo(1.96, 3)
    expect(tQuantile975(1e6)).toBeCloseTo(1.96, 3)
  })

  it('is continuous and decreasing across the table / expansion boundary', () => {
    // t(0.975, 31) = 2.039513 (scipy.stats.t.ppf); the expansion is within 1e-7 above df 30.
    expect(tQuantile975(31)).toBeCloseTo(2.039513, 5)
    expect(tQuantile975(30)).toBeGreaterThan(tQuantile975(31))
    for (let df = 1; df < 200; df++) {
      expect(tQuantile975(df + 1)).toBeLessThan(tQuantile975(df))
    }
  })

  it('floors a fractional df and is NaN below 1', () => {
    expect(tQuantile975(5.7)).toBe(tQuantile975(5))
    expect(tQuantile975(0)).toBeNaN()
    expect(tQuantile975(Number.NaN)).toBeNaN()
  })
})

describe('noiseHalfWidth and pooledMean', () => {
  it('uses t(0.975, n - 1) x SE, not 2 x SE', () => {
    // [0.10, 0.20]: mean 15%, SE 5%, df 1 -> half-width 12.706 x 5% = 63.5%.
    expect(noiseHalfWidth(returnStats([0.1, 0.2]))).toBeCloseTo(12.7062047 * 0.05, 6)
    expect(noiseHalfWidth(stats(0.01, 0.001, 101))).toBeCloseTo(tQuantile975(100) * 0.001, EPS)
  })

  it('has no half-width without an SE', () => {
    expect(noiseHalfWidth(returnStats([0.5]))).toBeNull()
    expect(noiseHalfWidth(returnStats([]))).toBeNull()
  })

  it('weights each cell by its n and skips empty cells', () => {
    // (2 x 10% + 3 x 0%) / 5 = 4%.
    expect(pooledMean([stats(0.1, 0.01, 2), stats(0, 0.01, 3), stats(null, null, 0)])).toBeCloseTo(
      0.04,
      EPS,
    )
    expect(pooledMean([returnStats([]), returnStats([])])).toBeNull()
  })

  it('equals the mean of the pooled raw returns', () => {
    const a = [0.1, -0.05, 0.2]
    const b = [0.03, 0.01]
    const all = returnStats([...a, ...b]).mean
    expect(pooledMean([returnStats(a), returnStats(b)])).toBeCloseTo(all!, EPS)
  })
})

describe('isNotable (deviation from the pooled mean beyond the 95% t band)', () => {
  it('does not tint [0.10, 0.20]: mean 15%, SE 5%, but its 95% band is ±63.5%', () => {
    const s = returnStats([0.1, 0.2])
    // The old rule (|mean| > 2 SE) would have tinted it: 15% > 10%.
    expect(Math.abs(s.mean!)).toBeGreaterThan(2 * s.se!)
    expect(isNotable(s, 0, 0)).toBe(false) // even with the small-n rule off
    expect(isNotable(s, 0, SMALL_N_MONTH)).toBe(false)
  })

  it('is never true below the small-n threshold, however extreme the mean', () => {
    for (let n = 0; n < SMALL_N_MONTH; n++) {
      expect(isNotable(stats(0.9, 0.001, n), 0, SMALL_N_MONTH)).toBe(false)
    }
    expect(isNotable(stats(0.9, 0.001, SMALL_N_MONTH), 0, SMALL_N_MONTH)).toBe(true)
    expect(isNotable(stats(0.05, 0.0001, SMALL_N_WEEKDAY - 1), 0, SMALL_N_WEEKDAY)).toBe(false)
    expect(isNotable(stats(0.05, 0.0001, SMALL_N_WEEKDAY), 0, SMALL_N_WEEKDAY)).toBe(true)
  })

  it('tints nothing in a bull window where every month has the same mean', () => {
    // Every month +5% with SE 0.5% (10 SE from zero), n = 20: pure drift, no seasonality.
    const months = Array.from({ length: 12 }, () => stats(0.05, 0.005, 20))
    const ref = pooledMean(months)
    expect(ref).toBeCloseTo(0.05, EPS)
    for (const m of months) expect(isNotable(m, ref, SMALL_N_MONTH)).toBe(false)
  })

  it('tints a clear outlier at large n, on either side of the average', () => {
    // n = 30, SE 3%: band = 2.045 x 3% = 6.1%; +20% is 18 points above a +2% average.
    expect(isNotable(stats(0.2, 0.03, 30), 0.02, SMALL_N_MONTH)).toBe(true)
    expect(isNotable(stats(-0.16, 0.03, 30), 0.02, SMALL_N_MONTH)).toBe(true)
  })

  it('treats a deviation inside the band as noise, including one exactly on its edge', () => {
    const s = stats(0.02, 0.03, 30)
    const edge = 0.02 + noiseHalfWidth(s)!
    expect(isNotable(stats(edge, 0.03, 30), 0.02, SMALL_N_MONTH)).toBe(false)
    expect(isNotable(stats(0.06, 0.03, 30), 0.02, SMALL_N_MONTH)).toBe(false) // 4 points < 6.1
  })

  it('needs an SE and a reference', () => {
    expect(isNotable(returnStats([]), 0, 0)).toBe(false)
    expect(isNotable(returnStats([0.5]), 0, 0)).toBe(false)
    expect(isNotable(stats(0.5, 0.001, 50), null, 10)).toBe(false)
  })

  it('agrees with returnStats on real samples', () => {
    // Ten months tightly around +10% against a 0% average: stands out.
    const tight = returnStats([0.1, 0.11, 0.09, 0.1, 0.12, 0.1, 0.09, 0.11, 0.1, 0.1])
    expect(isNotable(tight, 0, SMALL_N_MONTH)).toBe(true)
    // Wide spread around a small positive mean: noise.
    const wide = returnStats([0.3, -0.25, 0.2, -0.15, 0.1, 0.25, -0.2, 0.15, -0.1, 0.05])
    expect(isNotable(wide, 0, SMALL_N_MONTH)).toBe(false)
  })
})

describe('deviation (direction and size against the pooled mean)', () => {
  it('calls a positive month below a higher average "below", by the gap', () => {
    // Average +6%; Sep +0.5%, SE 1.5%, n = 10: |0.5 - 6| = 5.5 > 2.262 x 1.5 = 3.39.
    const sep = stats(0.005, 0.015, 10)
    expect(isNotable(sep, 0.06, SMALL_N_MONTH)).toBe(true)
    const d = deviation(sep, 0.06, SMALL_N_MONTH)
    expect(d?.sign).toBe('below')
    expect(d?.size).toBeCloseTo(0.055, EPS)
  })

  it('calls a negative month above a lower average "above"', () => {
    const d = deviation(stats(-0.005, 0.015, 10), -0.06, SMALL_N_MONTH)
    expect(d?.sign).toBe('above')
    expect(d?.size).toBeCloseTo(0.055, EPS)
  })

  it('is null for a cell within noise, a small sample, or no reference', () => {
    expect(deviation(stats(0.05, 0.03, 30), 0.02, SMALL_N_MONTH)).toBeNull()
    expect(deviation(stats(0.005, 0.015, 9), 0.06, SMALL_N_MONTH)).toBeNull()
    expect(deviation(stats(0.005, 0.015, 10), null, SMALL_N_MONTH)).toBeNull()
    expect(deviation(returnStats([]), 0, 0)).toBeNull()
  })
})

describe('noneComparable', () => {
  it('is true only when every cell is below the small-sample threshold', () => {
    // About nine complete Septembers since 2017-08: below 10 in every column.
    const nine = Array.from({ length: 12 }, () => stats(0.02, 0.05, 9))
    expect(noneComparable(nine, SMALL_N_MONTH)).toBe(true)
    expect(noneComparable([...nine.slice(1), stats(0.02, 0.05, 10)], SMALL_N_MONTH)).toBe(false)
    expect(noneComparable([], SMALL_N_MONTH)).toBe(true)
  })
})

describe('weekday bar scale', () => {
  it('scales against the largest of |mean| and |average| + 95% half-width', () => {
    const rows = [stats(0.002, 0.0016, 500), stats(-0.001, 0.0015, 500), stats(0.0005, 0.0018, 500)]
    const ref = 0.0004
    const hw = tQuantile975(499) * 0.0018
    expect(weekdayScale(rows, ref)).toBeCloseTo(ref + hw, EPS)
    const bar = weekdayBar(rows[0], weekdayScale(rows, ref), ref)
    // The mean ends inside its own band: noise, and it is not drawn at full width.
    expect(50 + bar.fill).toBeLessThan(bar.bandStart + bar.bandWidth)
    expect(bar.fill).toBeLessThan(50)
  })

  it('centres the 95% band on the average', () => {
    // mean 0.4%, SE 0.1%, n = 100, average 0.1%, scale 1%.
    const s = stats(0.004, 0.001, 100)
    const hw = tQuantile975(99) * 0.001
    const bar = weekdayBar(s, 0.01, 0.001)
    expect(bar.fill).toBeCloseTo(20, EPS)
    expect(bar.ref).toBeCloseTo(55, EPS)
    expect(bar.bandStart).toBeCloseTo(50 + ((0.001 - hw) / 0.01) * 50, EPS)
    expect(bar.bandWidth).toBeCloseTo(((2 * hw) / 0.01) * 50, EPS)
    expect(bar.bandStart + bar.bandWidth / 2).toBeCloseTo(bar.ref!, EPS)
  })

  it('draws no band without an SE or an average and never divides by zero', () => {
    expect(weekdayScale([returnStats([])], null)).toBeGreaterThan(0)
    expect(weekdayBar(returnStats([0.01]), 0.02, 0)).toEqual({
      fill: 25,
      bandStart: 0,
      bandWidth: 0,
      ref: 50,
    })
    expect(weekdayBar(returnStats([]), 0.02, null)).toEqual({
      fill: 0,
      bandStart: 0,
      bandWidth: 0,
      ref: null,
    })
  })
})

describe('summary constants', () => {
  it('flags calendar months with fewer than 10 and weekdays with fewer than 30 samples', () => {
    expect(SMALL_N_MONTH).toBe(10)
    expect(SMALL_N_WEEKDAY).toBe(30)
  })

  it('carries a quantitative multiple-comparisons caption', () => {
    expect(MULTIPLE_COMPARISONS_NOTE).toBe(
      '12 months and 7 weekdays are compared at once: in pure noise, at least one month still stands out about 40% of the time (a weekday about 20%). Treat a lone highlight as chance.',
    )
  })
})

describe('nextTabIndex', () => {
  it('wraps arrows and jumps with Home/End', () => {
    expect(nextTabIndex(0, 2, 'ArrowRight')).toBe(1)
    expect(nextTabIndex(1, 2, 'ArrowRight')).toBe(0)
    expect(nextTabIndex(0, 2, 'ArrowLeft')).toBe(1)
    expect(nextTabIndex(0, 2, 'ArrowDown')).toBe(1)
    expect(nextTabIndex(1, 2, 'ArrowUp')).toBe(0)
    expect(nextTabIndex(1, 2, 'Home')).toBe(0)
    expect(nextTabIndex(0, 2, 'End')).toBe(1)
  })

  it('ignores other keys, including Escape and Tab (left to useModalKeys)', () => {
    expect(nextTabIndex(0, 2, 'Escape')).toBeNull()
    expect(nextTabIndex(0, 2, 'Tab')).toBeNull()
    expect(nextTabIndex(0, 2, 'a')).toBeNull()
    expect(nextTabIndex(0, 0, 'ArrowRight')).toBeNull()
  })
})

describe('weekdayIndex', () => {
  it('maps 2024-01-01 (a Monday) to 0 and 2024-01-07 (a Sunday) to 6', () => {
    expect(weekdayIndex(utc(2024, 0, 1))).toBe(0)
    expect(weekdayIndex(utc(2024, 0, 3))).toBe(2)
    expect(weekdayIndex(utc(2024, 0, 7))).toBe(6)
  })
})

describe('computeSeasonals', () => {
  it('returns empty tables for no input', () => {
    const r = computeSeasonals([], utc(2024, 0, 10))
    expect(r.years).toEqual([])
    expect(r.monthly).toEqual([])
    expect(r.byMonth).toHaveLength(12)
    expect(r.byWeekday).toHaveLength(7)
    for (const s of [...r.byMonth, ...r.byWeekday]) expect(s.n).toBe(0)
    expect(r.byMonth[0].mean).toBeNull()
    expect(r.firstTime).toBeNull()
    expect(r.lastTime).toBeNull()
    expect(r.excludedDays).toBe(0)
    expect(r.barCount).toBe(0)
  })

  it('counts closed unique bars in barCount (forming bar and duplicates excluded)', () => {
    const bars = series(utc(2024, 0, 1), [100, 110, 121, 130])
    const r = computeSeasonals([...bars, bars[1]], utc(2024, 0, 4, 6))
    expect(r.barCount).toBe(3)
  })

  it('drops the forming bar and keeps a bar that closed exactly at nowSec', () => {
    const bars = series(utc(2024, 0, 1), [100, 110, 121])
    // Jan 3 opened at 00:00 and is still forming at 12:00.
    const forming = computeSeasonals(bars, utc(2024, 0, 3, 12))
    expect(forming.lastTime).toBe(utc(2024, 0, 2))
    expect(forming.byWeekday[2].n).toBe(0) // Wednesday Jan 3 not used
    // At 00:00 on Jan 4 the Jan 3 bar has closed.
    const closed = computeSeasonals(bars, utc(2024, 0, 4))
    expect(closed.lastTime).toBe(utc(2024, 0, 3))
    expect(closed.byWeekday[2].n).toBe(1)
    expect(closed.byWeekday[2].mean).toBeCloseTo(0.1, EPS)
  })

  it('computes daily close-to-close returns on the UTC weekday of the bar', () => {
    // Mon Jan 1 .. Mon Jan 8 2024.
    const bars = series(utc(2024, 0, 1), [100, 110, 99, 99, 198, 99, 99, 104.94])
    const r = computeSeasonals(bars, utc(2024, 0, 9))
    expect(r.firstTime).toBe(utc(2024, 0, 1))
    expect(r.lastTime).toBe(utc(2024, 0, 8))
    const [mon, tue, wed, thu, fri, sat, sun] = r.byWeekday
    expect(tue.mean).toBeCloseTo(0.1, EPS)
    expect(wed.mean).toBeCloseTo(-0.1, EPS)
    expect(thu.mean).toBeCloseTo(0, EPS)
    expect(thu.positivePct).toBe(0)
    expect(fri.mean).toBeCloseTo(1, EPS)
    expect(sat.mean).toBeCloseTo(-0.5, EPS)
    expect(sun.mean).toBeCloseTo(0, EPS)
    // Monday Jan 1 has no predecessor; only Monday Jan 8 counts.
    expect(mon.n).toBe(1)
    expect(mon.mean).toBeCloseTo(0.06, EPS)
    expect(r.excludedDays).toBe(0)
  })

  it('skips and counts a return across a gap instead of forward-filling', () => {
    // Jan 1, 2, (3 missing), 4, 5.
    const bars = [
      bar(utc(2024, 0, 1), 100),
      bar(utc(2024, 0, 2), 110),
      bar(utc(2024, 0, 4), 220),
      bar(utc(2024, 0, 5), 242),
    ]
    const r = computeSeasonals(bars, utc(2024, 0, 6))
    expect(r.excludedDays).toBe(1)
    const total = r.byWeekday.reduce((s, x) => s + x.n, 0)
    expect(total).toBe(2)
    expect(r.byWeekday[3].n).toBe(0) // Thursday Jan 4 (after the gap) skipped
    expect(r.byWeekday[4].mean).toBeCloseTo(0.1, EPS) // Friday Jan 5
  })

  it('dedupes repeated bars by time', () => {
    const bars = series(utc(2024, 0, 1), [100, 110])
    const r = computeSeasonals([...bars, bars[1]], utc(2024, 0, 3))
    expect(r.byWeekday[1].n).toBe(1)
    expect(r.excludedDays).toBe(0)
  })

  describe('monthly table', () => {
    // Full Jan..Mar 2024 (leap year), part of April. Month-end closes: Jan 100, Feb 110,
    // Mar 99; Apr 1..5 ends at 108.9. Other days 50 so only month-ends drive the returns.
    const monthEnd: Record<number, number> = {
      [utc(2024, 0, 31)]: 100,
      [utc(2024, 1, 29)]: 110,
      [utc(2024, 2, 31)]: 99,
      [utc(2024, 3, 5)]: 108.9,
    }
    const full = days(utc(2024, 0, 1), utc(2024, 3, 5), (t) => monthEnd[t] ?? 50)
    const now = utc(2024, 3, 6, 6) // Apr 5 closed, Apr 6 forming (not present anyway)

    it('computes last-close-to-last-close returns', () => {
      const r = computeSeasonals(full, now)
      expect(r.years).toEqual([2024])
      expect(r.monthly.map((c) => c.month)).toEqual([0, 1, 2, 3])
      expect(cell(r, 2024, 1)?.ret).toBeCloseTo(0.1, EPS)
      expect(cell(r, 2024, 2)?.ret).toBeCloseTo(-0.1, EPS)
      expect(cell(r, 2024, 3)?.ret).toBeCloseTo(0.1, EPS) // month-to-date
    })

    it('marks the first month and the partial current month incomplete', () => {
      const r = computeSeasonals(full, now)
      expect(cell(r, 2024, 0)).toEqual({ year: 2024, month: 0, ret: null, complete: false })
      expect(cell(r, 2024, 1)?.complete).toBe(true)
      expect(cell(r, 2024, 2)?.complete).toBe(true)
      expect(cell(r, 2024, 3)?.complete).toBe(false)
      expect(r.byMonth[0].n).toBe(0)
      expect(r.byMonth[1]).toMatchObject({ n: 1, positivePct: 100 })
      expect(r.byMonth[1].mean).toBeCloseTo(0.1, EPS)
      expect(r.byMonth[2].median).toBeCloseTo(-0.1, EPS)
      expect(r.byMonth[3].n).toBe(0)
    })

    it('explains why a cell is incomplete', () => {
      const r = computeSeasonals(full, now)
      expect(incompleteReason(cell(r, 2024, 0)!, r)).toMatch(/First month/)
      expect(incompleteReason(cell(r, 2024, 1)!, r)).toBeNull()
      expect(incompleteReason(cell(r, 2024, 3)!, r)).toMatch(/Partial month.*Apr 5/)
    })

    it('marks a month with a missing day incomplete', () => {
      const gap = full.filter((c) => c.time !== utc(2024, 2, 15))
      const r = computeSeasonals(gap, now)
      const mar = cell(r, 2024, 2)!
      expect(mar.complete).toBe(false)
      expect(mar.ret).toBeCloseTo(-0.1, EPS)
      expect(r.byMonth[2].n).toBe(0)
      expect(incompleteReason(mar, r)).toMatch(/gap/)
      expect(r.excludedDays).toBe(1)
    })

    it('marks a month incomplete when the previous month-end is missing', () => {
      const gap = full.filter((c) => c.time !== utc(2024, 1, 29))
      const r = computeSeasonals(gap, now)
      expect(cell(r, 2024, 1)?.complete).toBe(false)
      expect(cell(r, 2024, 2)?.complete).toBe(false) // March itself is full
      expect(cell(r, 2024, 2)?.ret).toBeCloseTo(99 / 50 - 1, EPS) // from Feb 28's close
    })

    it('aggregates the same calendar month across years', () => {
      // Dec 2022 .. Jan 2024: two complete Januaries (2023, 2024). Month-end closes double in
      // Jan 2023 and halve in Jan 2024.
      const ends: Record<number, number> = {
        [utc(2022, 11, 31)]: 100,
        [utc(2023, 0, 31)]: 200,
        [utc(2023, 11, 31)]: 400,
        [utc(2024, 0, 31)]: 200,
      }
      const bars = days(utc(2022, 11, 1), utc(2024, 0, 31), (t) => ends[t] ?? 300)
      const r = computeSeasonals(bars, utc(2024, 1, 1))
      expect(r.years).toEqual([2022, 2023, 2024])
      expect(r.byMonth[0].n).toBe(2)
      expect(r.byMonth[0].mean).toBeCloseTo((1 + -0.5) / 2, EPS)
      expect(r.byMonth[0].median).toBeCloseTo(0.25, EPS)
      expect(r.byMonth[0].positivePct).toBe(50)
      expect(r.byMonth[11].n).toBe(1) // Dec 2023; Dec 2022 is the first month
    })
  })

  it('does not look ahead: future and forming bars cannot change the result', () => {
    const now = utc(2024, 2, 10)
    let x = 1000
    const past = days(utc(2023, 10, 1), utc(2024, 2, 9), () => {
      x = x * 1.013 - 7 // deterministic, non-trivial path
      return x
    })
    const base = computeSeasonals(past, now)
    expect(base.lastTime).toBe(utc(2024, 2, 9))

    const forming = bar(utc(2024, 2, 10), 1)
    const future = days(utc(2024, 2, 11), utc(2024, 5, 30), (t) => (t % 7) + 1)
    const withFuture = computeSeasonals([...past, forming, ...future], now)
    expect(withFuture).toEqual(base)

    const changed = [bar(utc(2024, 2, 10), 99_999), ...future.map((c) => bar(c.time, c.close * 50))]
    expect(computeSeasonals([...past, ...changed], now)).toEqual(base)
  })
})

/** Fake provider over a fixed daily dataset, honouring `limit`, `endTime`, and a row cap. */
function fakeProvider(
  data: readonly Candle[],
  opts: { cap?: number; ignoreEndTime?: boolean; overlap?: boolean } = {},
) {
  const calls: { symbol: string; interval: Interval; limit?: number; endTime?: number }[] = []
  const provider: MarketDataProvider = {
    getCandles(symbol, interval, limit, endTime, o) {
      calls.push({ symbol, interval, limit, endTime })
      if (o?.signal?.aborted) return Promise.reject(new MarketDataError('aborted', 'cancelled'))
      const n = Math.min(limit ?? 500, opts.cap ?? Infinity)
      // `overlap` returns one extra already-seen row after endTime (a sloppy provider).
      const end =
        endTime === undefined || opts.ignoreEndTime
          ? Infinity
          : endTime + (opts.overlap ? DAY_SEC : 0)
      const eligible = data.filter((c) => c.time <= end)
      return Promise.resolve(eligible.slice(Math.max(0, eligible.length - n)))
    },
    subscribeCandles: () => () => {},
    getSymbols: () => Promise.resolve([]),
    getTickers: () => Promise.resolve([]),
    subscribeTickers: () => () => {},
  }
  return { provider, calls }
}

const history = (n: number) =>
  series(
    utc(2015, 0, 1),
    Array.from({ length: n }, (_, i) => i + 1),
  )

function expectAscendingUnique(rows: readonly Candle[]) {
  for (let i = 1; i < rows.length; i++) expect(rows[i].time).toBeGreaterThan(rows[i - 1].time)
}

describe('loadDailyHistory', () => {
  it('pages backwards with endTime = oldest - 1 until a page is empty', async () => {
    const data = history(2500)
    const { provider, calls } = fakeProvider(data)
    const rows = await loadDailyHistory(provider, 'BTCUSDT', {})
    expect(rows).toEqual(data)
    expect(calls.map((c) => c.endTime)).toEqual([
      undefined,
      data[1500].time - 1,
      data[500].time - 1,
      data[0].time - 1,
    ])
    for (const c of calls) {
      expect(c).toMatchObject({ symbol: 'BTCUSDT', interval: '1d', limit: PAGE_SIZE })
    }
  })

  it('keeps going when the provider returns fewer rows than requested', async () => {
    const data = history(1000)
    const { provider, calls } = fakeProvider(data, { cap: 300 })
    const rows = await loadDailyHistory(provider, 'ETHUSDT', {})
    expect(rows).toEqual(data)
    expect(calls).toHaveLength(5) // 300, 300, 300, 100, empty
  })

  it('stops at maxBars and keeps the newest bars', async () => {
    const data = history(5000)
    const { provider, calls } = fakeProvider(data)
    const rows = await loadDailyHistory(provider, 'BTCUSDT', { maxBars: 1500 })
    expect(rows).toEqual(data.slice(3500))
    expect(calls).toHaveLength(2)
  })

  it('caps at 3650 bars by default', async () => {
    const { provider, calls } = fakeProvider(history(5000))
    const rows = await loadDailyHistory(provider, 'BTCUSDT', {})
    expect(rows).toHaveLength(3650)
    expect(calls).toHaveLength(4)
  })

  it('dedupes rows that overlap between pages', async () => {
    const data = history(2200)
    const { provider } = fakeProvider(data, { overlap: true })
    const rows = await loadDailyHistory(provider, 'BTCUSDT', {})
    expect(rows).toEqual(data)
    expectAscendingUnique(rows)
  })

  it('stops when a page adds nothing new', async () => {
    const data = history(1500)
    const { provider, calls } = fakeProvider(data, { ignoreEndTime: true })
    const rows = await loadDailyHistory(provider, 'BTCUSDT', {})
    expect(rows).toEqual(data.slice(500))
    expect(calls).toHaveLength(2)
  })

  it('with nowSec, drops the forming row so maxBars counts closed bars', async () => {
    const data = history(5000)
    const last = data[data.length - 1].time
    // `last` opened 12 h ago and is still forming.
    const nowSec = last + DAY_SEC / 2
    const { provider } = fakeProvider(data)
    const rows = await loadDailyHistory(provider, 'BTCUSDT', { nowSec })
    expect(rows).toHaveLength(DEFAULT_MAX_BARS)
    expect(rows[rows.length - 1].time).toBe(last - DAY_SEC)
    expect(rows).toEqual(data.slice(5000 - 1 - DEFAULT_MAX_BARS, 4999))
    expect(historyTruncated(rows.length)).toBe(true)
    expect(computeSeasonals(rows, nowSec).barCount).toBe(DEFAULT_MAX_BARS)
  })

  it('does not call a full history truncated just because of the forming row', async () => {
    // Exactly 3650 rows on the venue, the newest still forming: 3649 closed bars, all of them.
    const data = history(DEFAULT_MAX_BARS)
    const nowSec = data[data.length - 1].time + 60
    const { provider } = fakeProvider(data)
    const rows = await loadDailyHistory(provider, 'BTCUSDT', { nowSec })
    expect(rows).toEqual(data.slice(0, DEFAULT_MAX_BARS - 1))
    expect(historyTruncated(rows.length)).toBe(false)
    expect(computeSeasonals(rows, nowSec).barCount).toBe(DEFAULT_MAX_BARS - 1)
  })

  it('historyTruncated compares the closed row count with the cap', () => {
    expect(historyTruncated(DEFAULT_MAX_BARS - 1)).toBe(false)
    expect(historyTruncated(DEFAULT_MAX_BARS)).toBe(true)
    expect(historyTruncated(10, 10)).toBe(true)
    expect(historyTruncated(9, 10)).toBe(false)
  })

  it('returns [] when the first page is empty', async () => {
    const { provider, calls } = fakeProvider([])
    await expect(loadDailyHistory(provider, 'NEWUSDT', {})).resolves.toEqual([])
    expect(calls).toHaveLength(1)
  })

  it('rejects with kind aborted when the signal is already aborted', async () => {
    const { provider, calls } = fakeProvider(history(10))
    const ac = new AbortController()
    ac.abort()
    await expect(loadDailyHistory(provider, 'BTCUSDT', { signal: ac.signal })).rejects.toSatisfy(
      (e: unknown) => e instanceof MarketDataError && e.kind === 'aborted',
    )
    expect(calls).toHaveLength(0)
  })

  it('stops paging when aborted between pages', async () => {
    const data = history(3000)
    const ac = new AbortController()
    const inner = fakeProvider(data)
    let n = 0
    const provider: MarketDataProvider = {
      ...inner.provider,
      getCandles: (...args) => {
        n++
        const p = inner.provider.getCandles(...args)
        if (n === 1) ac.abort()
        return p
      },
    }
    await expect(loadDailyHistory(provider, 'BTCUSDT', { signal: ac.signal })).rejects.toSatisfy(
      (e: unknown) => e instanceof MarketDataError && e.kind === 'aborted',
    )
    expect(n).toBe(1)
  })
})

describe('SeasonalsView markup', () => {
  it('renders a labelled modal dialog with tabs, a loading state and the disclaimer', () => {
    const { provider } = fakeProvider(history(10))
    const html = renderToStaticMarkup(
      createElement(SeasonalsView, {
        symbol: 'BTCUSDT',
        provider,
        venueName: 'Binance',
        onClose: () => {},
      }),
    )
    expect(html).toContain('role="dialog"')
    expect(html).toContain('aria-modal="true"')
    expect(html).toMatch(/<h2 id="([^"]+)"[^>]*>Seasonals · BTCUSDT<\/h2>/)
    const titleId = /<h2 id="([^"]+)"/.exec(html)?.[1]
    expect(html).toContain(`aria-labelledby="${titleId}"`)
    expect(html).toMatch(/role="tab"[^>]*aria-selected="true"[^>]*>Monthly</)
    expect(html).toMatch(/role="tab"[^>]*aria-selected="false"[^>]*>Weekday</)
    expect(html).toContain('Loading daily history')
    expect(html).toContain('UTC days')
    expect(html).toContain('Historical, not forward-looking. Small samples; not a trading signal.')
    expect(html).toContain(MULTIPLE_COMPARISONS_NOTE)
  })
})

/** A result with hand-set byMonth stats (no grid cells), for summary-row markup tests. */
function resultWithMonthStats(byMonth: ReturnStats[]): SeasonalsResult {
  return {
    years: [],
    monthly: [],
    byMonth,
    byWeekday: Array.from({ length: 7 }, () => returnStats([])),
    firstTime: null,
    lastTime: null,
    excludedDays: 0,
    barCount: 0,
  }
}

/** The <td> cells of the tfoot row whose header is `label`. */
function footRow(html: string, label: string): string[] {
  const rows = html.split('<tr').filter((r) => r.includes(`>${label}</th>`))
  expect(rows).toHaveLength(1)
  return rows[0].split('<td').slice(1)
}

describe('summary-row markup', () => {
  // Jan: +30% (SE 3%, n = 12), a clear outlier. Oct: −25% (SE 2%) but n = 8, a small sample.
  // Others: +2% (SE 5%, n = 12). All months: (3.6 + 2.4 − 2) / 140 = +2.857%.
  const months = Array.from({ length: 12 }, (_, i) =>
    i === 0 ? stats(0.3, 0.03, 12) : i === 9 ? stats(-0.25, 0.02, 8) : stats(0.02, 0.05, 12),
  )
  const html = renderToStaticMarkup(
    createElement(MonthlyTable, { result: resultWithMonthStats(months) }),
  )

  it('tints only a month that stands out from the all-month average', () => {
    const mean = footRow(html, 'Mean')
    expect(mean).toHaveLength(12)
    expect(mean[0]).toContain('is-up')
    expect(mean[0]).toContain('--heat')
    expect(mean[0]).not.toContain('seasonals-cell--noise')
    expect(mean[0]).toContain(
      'Stands out: 27.1 percentage points above the all-month average (beyond the 95% t band).',
    )
    expect(mean[0]).toContain('(above the all-month average)')
    expect(mean[1]).toContain('seasonals-cell--noise')
    expect(mean[1]).not.toContain('--heat')
    expect(mean[1]).toContain('(within noise)')
    expect(mean[1]).toContain('Within noise of the all-month average (inside the 95% t band).')
  })

  it('never tints a small-sample month and says why', () => {
    const mean = footRow(html, 'Mean')
    expect(mean[9]).toContain('seasonals-cell--noise')
    expect(mean[9]).not.toContain('is-down')
    expect(mean[9]).not.toContain('--heat')
    expect(mean[9]).toContain('(small sample, not compared)')
    expect(mean[9]).toContain('Small sample (fewer than 10): not compared, untinted.')
  })

  it('names the reference and the 95% band in the tooltip and the caption', () => {
    const mean = footRow(html, 'Mean')
    const hw = (tQuantile975(11) * 0.03 * 100).toFixed(1)
    expect(mean[0]).toContain(`vs. the all-month average +2.9%: 95% band ±${hw}%`)
    expect(html).toContain('All months: mean +2.9%')
    expect(html).not.toMatch(/significant/i)
    expect(html).not.toContain('2 SE')
  })

  it('shows a ± SE row', () => {
    const se = footRow(html, '± SE')
    expect(se[0]).toContain('±3.0%')
    expect(se[9]).toContain('±2.0%')
    expect(se[1]).toContain('±5.0%')
  })

  it('flags n below 10 with text and a tooltip, not colour only', () => {
    const n = footRow(html, 'n')
    expect(n[0]).not.toContain('seasonals-n--small') // n = 12
    expect(n[0]).not.toContain('small sample')
    expect(n[9]).toContain('seasonals-n--small') // n = 8
    expect(n[9]).toContain('(small sample)')
    expect(n[9]).toContain('title="Oct: 8 complete months. Small sample (fewer than 10).')
  })

  it('tints nothing in an all-positive bull window with equal months', () => {
    const bull = Array.from({ length: 12 }, () => stats(0.05, 0.005, 20))
    const out = renderToStaticMarkup(
      createElement(MonthlyTable, { result: resultWithMonthStats(bull) }),
    )
    for (const c of footRow(out, 'Mean')) {
      expect(c).not.toContain('--heat')
      expect(c).not.toContain('is-up')
      expect(c).toContain('seasonals-cell--noise')
    }
  })

  it('colours a positive month below a higher average as below, shaded by the gap', () => {
    // Sep +0.5% (SE 1.5%, n = 10); the other months +6.5% (SE 5%, n = 10, within noise).
    // All months: (110 x 6.5 + 10 x 0.5) / 120 = +6.0%. Sep is 5.5 points below it.
    const below = Array.from({ length: 12 }, (_, i) =>
      i === 8 ? stats(0.005, 0.015, 10) : stats(0.065, 0.05, 10),
    )
    const out = renderToStaticMarkup(
      createElement(MonthlyTable, { result: resultWithMonthStats(below) }),
    )
    const mean = footRow(out, 'Mean')
    expect(mean[8]).toContain('is-down')
    expect(mean[8]).not.toContain('is-up')
    // Heat from |0.5 - 6| = 5.5 points: 8 + 62 x 0.055 / 0.25 = 21.6%, not the raw +0.5%.
    expect(mean[8]).toContain('--heat:21.6%')
    expect(mean[8]).toContain('+0.5%')
    expect(mean[8]).toContain(
      'Stands out: 5.5 percentage points below the all-month average (beyond the 95% t band).',
    )
    expect(mean[8]).toContain('(below the all-month average)')
    for (const [i, c] of mean.entries()) {
      if (i === 8) continue
      expect(c).toContain('seasonals-cell--noise')
      expect(c).not.toContain('is-up')
      expect(c).not.toContain('is-down')
      expect(c).not.toContain('--heat')
    }
    expect(out).not.toContain('no month is compared')
  })

  it('says so when every month has fewer than 10 complete years', () => {
    const nine = Array.from({ length: 12 }, (_, i) => stats(i === 0 ? 0.3 : 0.02, 0.01, 9))
    const out = renderToStaticMarkup(
      createElement(MonthlyTable, { result: resultWithMonthStats(nine) }),
    )
    expect(out).toContain('Fewer than 10 complete years per month: no month is compared.')
    for (const c of footRow(out, 'Mean')) {
      expect(c).not.toContain('is-up')
      expect(c).not.toContain('--heat')
    }
    expect(html).not.toContain('no month is compared')
  })

  it('keeps raw-return colouring on year x month cells', () => {
    // Jan 2021 +10% in a month whose column mean is far higher: the grid cell still shows
    // its own return (green), since grid cells are not a comparison with the average.
    const r: SeasonalsResult = {
      ...resultWithMonthStats(months),
      years: [2021],
      monthly: [{ year: 2021, month: 0, ret: 0.1, complete: true }],
    }
    const out = renderToStaticMarkup(createElement(MonthlyTable, { result: r }))
    const row = out.split('<tbody>')[1].split('<tr').slice(1)[0]
    const jan = row.split('<td')[1]
    expect(jan).toContain('is-up')
    // 8 + 62 x 0.10 / 0.25 = 32.8%.
    expect(jan).toContain('--heat:32.8%')
  })

  it('explains both colour meanings in the legend', () => {
    const legend = renderToStaticMarkup(createElement(HeatLegend))
    expect(legend).toContain('Year cells: return')
    expect(legend).toContain(
      'Mean row: above / below the all-month average, beyond its 95% band (shade = distance from it)',
    )
    expect(legend).toContain('Mean within the band, or n &lt; 10: untinted')
    const week = renderToStaticMarkup(createElement(NoiseLegend))
    expect(week).toContain('Mean above / below the all-day average, beyond its band')
  })

  it('shows the pooled mean beside the tabs', () => {
    const r = resultWithMonthStats(months)
    const monthly = renderToStaticMarkup(createElement(SummaryMeta, { tab: 'monthly', result: r }))
    expect(monthly).toContain('All months: mean <span class="seasonals__num">+2.9%</span>')
    expect(monthly).toContain('140</span> complete months')
    const weekday = renderToStaticMarkup(
      createElement(SummaryMeta, {
        tab: 'weekday',
        result: { ...r, byWeekday: [stats(0.01, 0.001, 3), stats(0, 0.001, 1)] },
      }),
    )
    expect(weekday).toContain('All days: mean <span class="seasonals__num">+0.75%</span>')
  })
})

describe('weekday markup', () => {
  // Monday: mean 1% with SE 0.1% (stands out). Tue..Sat: 0.2% with SE 0.16%, n = 500 (noise).
  // Sunday: 5% with SE 0.16% but n = 20 (small sample, never tinted).
  // All days: (5 + 5 + 1) / 3020 = +0.364%.
  const rows = Array.from({ length: 7 }, (_, i) =>
    i === 0
      ? stats(0.01, 0.001, 500)
      : i === 6
        ? stats(0.05, 0.0016, 20)
        : stats(0.002, 0.0016, 500),
  )
  const html = renderToStaticMarkup(createElement(WeekdayTable, { stats: rows }))
  const body = html.split('<tbody>')[1].split('<tr').slice(1)

  it('shows ± SE, a 95% band and the average marker on every bar', () => {
    expect(body).toHaveLength(7)
    for (const r of body) {
      expect(r).toContain('seasonals-bar__band')
      expect(r).toContain('seasonals-bar__ref')
    }
    expect(body[0]).toContain('±0.10%')
    expect(body[1]).toContain('±0.16%')
    expect(html).toContain('Mean daily return, 95% band around the average')
    expect(html).toContain('All days: mean +0.36%')
    expect(html).not.toContain('2 SE')
    expect(html).not.toMatch(/significant/i)
  })

  it('fades bars and means inside the band and keeps a standout strong', () => {
    expect(body[0]).not.toContain('seasonals-bar__fill--noise')
    expect(body[0]).not.toContain('seasonals-week__num--noise')
    expect(body[0]).toContain(
      'Stands out: 0.64 percentage points above the all-day average (beyond the 95% t band).',
    )
    expect(body[0]).toContain('is-up')
    expect(body[0]).toContain('seasonals-bar__fill--above')
    expect(body[1]).toContain('seasonals-bar__fill--noise')
    expect(body[1]).toContain('seasonals-week__num--noise')
    expect(body[1]).toContain('(within noise)')
    expect(body[1]).toContain('vs. the all-day average +0.36%')
    // Noise stays neutral: no direction colour on the number or the bar.
    const meanCell = body[1].split('<td')[1]
    expect(meanCell).not.toContain('is-up')
    expect(meanCell).not.toContain('is-down')
    expect(body[1]).not.toContain('seasonals-bar__fill--above')
    expect(body[1]).not.toContain('seasonals-bar__fill--below')
  })

  it('colours a positive weekday below a higher average as below', () => {
    // Average +0.6% per day; Mon +0.05% (SE 0.1%, n = 500) is 0.55 points below it.
    const below = Array.from({ length: 7 }, (_, i) =>
      i === 0 ? stats(0.0005, 0.001, 500) : stats(0.0069167, 0.004, 500),
    )
    const out = renderToStaticMarkup(createElement(WeekdayTable, { stats: below }))
    const rows = out.split('<tbody>')[1].split('<tr').slice(1)
    const mon = rows[0]
    const meanCell = mon.split('<td')[1]
    expect(meanCell).toContain('is-down')
    expect(meanCell).not.toContain('seasonals-week__num--noise')
    expect(meanCell).toContain('+0.05%')
    expect(meanCell).toContain('below the all-day average')
    // The bar sits right of zero (positive mean) but is coloured as below the average.
    expect(mon).toContain('seasonals-bar__fill is-up seasonals-bar__fill--below')
    for (const r of rows.slice(1)) expect(r).toContain('seasonals-bar__fill--noise')
  })

  it('says so when every weekday has fewer than 30 returns', () => {
    const few = Array.from({ length: 7 }, () => stats(0.01, 0.001, 20))
    const out = renderToStaticMarkup(createElement(WeekdayTable, { stats: few }))
    expect(out).toContain('Fewer than 30 daily returns per weekday: no weekday is compared.')
    expect(html).not.toContain('no weekday is compared')
  })

  it('never tints a small weekday sample, however far its mean is from the average', () => {
    expect(body[6]).toContain('(small sample)')
    expect(body[6]).toContain('seasonals-bar__fill--noise')
    expect(body[6]).toContain('seasonals-week__num--noise')
    expect(body[6]).toContain('Small sample (fewer than 30): not compared, untinted.')
    expect(body[1]).not.toContain('(small sample)')
  })
})
