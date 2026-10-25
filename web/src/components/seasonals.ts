// Seasonals: monthly and weekday return tables from closed daily bars (pure computation) plus
// a paginated daily-history loader. Crypto calendar (every UTC day is a trading day), no
// forward fill, no look-ahead: only bars that closed at or before `nowSec` are used.

import { MarketDataError, type MarketDataProvider } from '../data/provider'
import type { Candle } from '../types'

export const DAY_SEC = 86_400
/** Candles per request in `loadDailyHistory`. */
export const PAGE_SIZE = 1000
/** Default cap for `loadDailyHistory`: about ten years of daily bars. */
export const DEFAULT_MAX_BARS = 3650
/** Calendar-month columns with fewer complete months than this are flagged as small samples. */
export const SMALL_N_MONTH = 10
/** Weekday rows with fewer daily returns than this are flagged as small samples. */
export const SMALL_N_WEEKDAY = 30
/**
 * Shown under both tables. The rates are for pure noise under the `isNotable` rule (each cell
 * against the pooled mean with its 95% t band): a Monte Carlo of 20,000 noise tables gives about
 * 0.39-0.40 for 12 months (n = 10-30 each) and about 0.21 for 7 weekdays (n = 30-500 each),
 * with normal and with fat-tailed (t, 3 df) returns.
 */
export const MULTIPLE_COMPARISONS_NOTE =
  '12 months and 7 weekdays are compared at once: in pure noise, at least one month still stands out about 40% of the time (a weekday about 20%). Treat a lone highlight as chance.'

/**
 * Summary of a sample of returns. Returns are fractions (0.012 = 1.2%). `positivePct` is a
 * percentage 0-100 of observations strictly above zero. `sd` is the sample standard deviation
 * (n - 1 denominator) and `se` = sd / sqrt(n), the standard error of the mean; both are null
 * when n < 2. Every other statistic is null when n = 0.
 */
export interface ReturnStats {
  n: number
  mean: number | null
  median: number | null
  positivePct: number | null
  sd: number | null
  se: number | null
}

/**
 * One year x month entry. `ret` = last close of the month / last close of the previous month
 * - 1, null when the previous month has no bars. `complete` is true only when the month has a
 * bar for every UTC calendar day and the previous month's last day is present.
 */
export interface MonthCell {
  year: number
  month: number // 0-11
  ret: number | null
  complete: boolean
}

export interface SeasonalsResult {
  years: number[] // ascending
  monthly: MonthCell[] // year x month grid entries that have data
  byMonth: ReturnStats[] // length 12, Jan..Dec, complete months only
  byWeekday: ReturnStats[] // length 7, Monday..Sunday (UTC), daily close-to-close returns
  firstTime: number | null
  lastTime: number | null // UNIX seconds of the first/last closed bar used
  excludedDays: number // daily returns skipped because of gaps
  barCount: number // closed, unique daily bars used
}

export const MONTH_LABELS = [
  'Jan',
  'Feb',
  'Mar',
  'Apr',
  'May',
  'Jun',
  'Jul',
  'Aug',
  'Sep',
  'Oct',
  'Nov',
  'Dec',
] as const

/** Monday first, matching `byWeekday`. */
export const WEEKDAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'] as const

/** UTC weekday of a UNIX-seconds time, Monday = 0 ... Sunday = 6. */
export function weekdayIndex(timeSec: number): number {
  return (new Date(timeSec * 1000).getUTCDay() + 6) % 7
}

function daysInMonthUtc(year: number, month: number): number {
  return new Date(Date.UTC(year, month + 1, 0)).getUTCDate()
}

/**
 * Mean, median (average of the middle two for even n), % positive, sample sd (n - 1) and the
 * standard error of the mean (sd / sqrt(n)).
 */
export function returnStats(values: readonly number[]): ReturnStats {
  const n = values.length
  if (n === 0) return { n: 0, mean: null, median: null, positivePct: null, sd: null, se: null }
  let sum = 0
  let up = 0
  for (const v of values) {
    sum += v
    if (v > 0) up++
  }
  const mean = sum / n
  const sorted = [...values].sort((a, b) => a - b)
  const mid = n >> 1
  const median = n % 2 === 1 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2
  let sd: number | null = null
  let se: number | null = null
  if (n >= 2) {
    let ss = 0
    for (const v of values) ss += (v - mean) ** 2
    sd = Math.sqrt(ss / (n - 1))
    se = sd / Math.sqrt(n)
  }
  return { n, mean, median, positivePct: (up / n) * 100, sd, se }
}

/** Two-sided 95% (upper 0.975) Student-t quantiles for df = 1..30, to 7 decimals. */
const T975: readonly number[] = [
  12.7062047, 4.3026527, 3.1824463, 2.7764451, 2.5705818, 2.4469119, 2.3646243, 2.3060041,
  2.2621572, 2.2281389, 2.2009852, 2.1788128, 2.1603687, 2.1447867, 2.1314495, 2.1199053, 2.1098156,
  2.100922, 2.0930241, 2.0859634, 2.0796138, 2.0738731, 2.0686576, 2.0638986, 2.0595386, 2.0555294,
  2.0518305, 2.0484071, 2.0452296, 2.0422725,
]

/** Standard normal 0.975 quantile. */
const Z975 = 1.959963984540054

/**
 * Upper 0.975 quantile of Student's t with `df` degrees of freedom (the half-width multiplier of
 * a two-sided 95% interval). df 1-30 come from a table; above 30 the Cornish-Fisher expansion
 * in 1/df (Abramowitz & Stegun 26.7.5, four terms) is used, which is within 1e-7 there and tends
 * to 1.95996 as df grows. A fractional df is floored. NaN for df < 1.
 */
export function tQuantile975(df: number): number {
  if (!(df >= 1)) return Number.NaN
  if (df === Infinity) return Z975
  const v = Math.floor(df)
  if (v <= T975.length) return T975[v - 1]
  const z = Z975
  const z2 = z * z
  const z3 = z2 * z
  const z5 = z3 * z2
  const z7 = z5 * z2
  const z9 = z7 * z2
  return (
    z +
    (z3 + z) / (4 * v) +
    (5 * z5 + 16 * z3 + 3 * z) / (96 * v ** 2) +
    (3 * z7 + 19 * z5 + 17 * z3 - 15 * z) / (384 * v ** 3) +
    (79 * z9 + 776 * z7 + 1482 * z5 - 1920 * z3 - 945 * z) / (92160 * v ** 4)
  )
}

/**
 * Half-width of the 95% t band around a cell's mean: t(0.975, n - 1) x SE. Null when the SE is
 * unknown (n < 2) or not finite.
 */
export function noiseHalfWidth(s: ReturnStats): number | null {
  if (s.se === null || !Number.isFinite(s.se) || s.n < 2) return null
  return tQuantile975(s.n - 1) * s.se
}

/**
 * Mean of every observation in the table (each cell weighted by its n): the pooled mean of all
 * complete months, or of all daily returns. Null when the table is empty.
 */
export function pooledMean(stats: readonly ReturnStats[]): number | null {
  let sum = 0
  let n = 0
  for (const s of stats) {
    if (s.mean === null || !Number.isFinite(s.mean) || s.n <= 0) continue
    sum += s.mean * s.n
    n += s.n
  }
  return n > 0 ? sum / n : null
}

/**
 * True when a cell's mean stands out from the table's pooled mean (`reference`, from
 * `pooledMean`): |mean - reference| > t(0.975, n - 1) x SE. Comparing with the pooled mean
 * asks a seasonal question (does this month differ from a typical month?); comparing with zero
 * would only flag drift, since in a bull window every month leans positive.
 *
 * Never true when n < `minN` (the small-sample threshold: `SMALL_N_MONTH` or `SMALL_N_WEEKDAY`)
 * or when the mean, the SE, or the reference is unknown. For one noise cell this fires about 5%
 * of the time (a little less, since the cell is part of its own reference); 12 months or 7
 * weekdays are compared at once, so at least one noise cell passes far more often (see
 * `MULTIPLE_COMPARISONS_NOTE`). A screen, not a significance test.
 */
export function isNotable(s: ReturnStats, reference: number | null, minN: number): boolean {
  if (s.n < minN || s.n < 2) return false
  if (s.mean === null || !Number.isFinite(s.mean)) return false
  if (reference === null || !Number.isFinite(reference)) return false
  const hw = noiseHalfWidth(s)
  if (hw === null) return false
  return Math.abs(s.mean - reference) > hw
}

/** Direction and size of a notable cell's departure from the table's pooled mean. */
export interface Deviation {
  sign: 'above' | 'below'
  /** |mean - reference|, a fraction (0.055 = 5.5 percentage points). Always > 0. */
  size: number
}

/**
 * How a notable cell differs from the pooled mean (`reference`): 'above' or 'below' it, by
 * |mean - reference|. Null unless `isNotable(s, reference, minN)`, so a cell within noise or a
 * small sample never gets a direction. The tint of a Mean cell follows this, not the sign of
 * the mean: in a +6% average, a +0.5% month that stands out is 'below' (seasonally weak), even
 * though its own mean is positive.
 */
export function deviation(
  s: ReturnStats,
  reference: number | null,
  minN: number,
): Deviation | null {
  if (!isNotable(s, reference, minN) || s.mean === null || reference === null) return null
  const d = s.mean - reference
  return { sign: d > 0 ? 'above' : 'below', size: Math.abs(d) }
}

/**
 * True when no cell can be compared: every cell has fewer than `minN` observations (or the
 * table is empty). The UI says so in words, since then nothing is ever tinted.
 */
export function noneComparable(stats: readonly ReturnStats[], minN: number): boolean {
  return stats.every((s) => s.n < minN || s.n < 2)
}

/**
 * Shared half-width for the weekday bars: the largest of |mean| and |reference| + the 95% t
 * half-width over all rows, so every bar and every band fits and all rows use the same scale.
 * Never zero.
 */
export function weekdayScale(stats: readonly ReturnStats[], reference: number | null): number {
  const ref = reference !== null && Number.isFinite(reference) ? Math.abs(reference) : 0
  let out = ref
  for (const s of stats) {
    if (s.mean !== null && Number.isFinite(s.mean)) out = Math.max(out, Math.abs(s.mean))
    const hw = noiseHalfWidth(s)
    if (hw !== null) out = Math.max(out, ref + hw)
  }
  return out > 0 ? out : 1e-12
}

/**
 * Geometry of one weekday bar, as percentages of the full track width (zero sits at 50%).
 * `fill` is the mean's length from the zero axis. The band is the 95% t band of the row's mean
 * centred on the pooled mean (`reference`): it spans `bandStart`..`bandStart + bandWidth`
 * (both 0 when the SE or the reference is unknown), and `ref` is the pooled mean's position. A
 * mean whose fill ends inside the band is within noise of the average.
 */
export function weekdayBar(
  s: ReturnStats,
  scale: number,
  reference: number | null,
): { fill: number; bandStart: number; bandWidth: number; ref: number | null } {
  const pos = (x: number) => Math.min(Math.max(50 + (x / scale) * 50, 0), 100)
  const fill =
    s.mean === null || !Number.isFinite(s.mean) ? 0 : Math.min((Math.abs(s.mean) / scale) * 50, 50)
  const hasRef = reference !== null && Number.isFinite(reference)
  const ref = hasRef ? pos(reference) : null
  const hw = noiseHalfWidth(s)
  if (!hasRef || hw === null) return { fill, bandStart: 0, bandWidth: 0, ref }
  const lo = pos(reference - hw)
  const hi = pos(reference + hw)
  return { fill, bandStart: lo, bandWidth: hi - lo, ref }
}

function usable(c: Candle): boolean {
  return Number.isFinite(c.time) && Number.isFinite(c.close) && c.close > 0
}

/** Closed daily bars only, ascending, unique by time (a later duplicate wins). */
function closedBars(daily: readonly Candle[], nowSec: number): Candle[] {
  const byTime = new Map<number, Candle>()
  for (const c of daily) {
    if (!usable(c)) continue
    if (c.time + DAY_SEC > nowSec) continue // still forming (or in the future)
    byTime.set(c.time, c)
  }
  return [...byTime.values()].sort((a, b) => a.time - b.time)
}

interface MonthAgg {
  year: number
  month: number
  days: Set<number> // UTC day-of-month numbers with a bar
  lastClose: number
  lastDay: number
}

const monthKey = (year: number, month: number) => year * 12 + month

/**
 * Monthly and weekday return tables from daily candles (`time` = bar open, UNIX seconds UTC).
 * Bars whose day has not ended by `nowSec` are dropped. A daily return needs the previous bar
 * exactly one day earlier; otherwise it is skipped and counted in `excludedDays` (the very
 * first bar has no predecessor and is not counted). Nothing is forward-filled.
 */
export function computeSeasonals(daily: readonly Candle[], nowSec: number): SeasonalsResult {
  const bars = closedBars(daily, nowSec)

  // Daily close-to-close returns by UTC weekday.
  const weekday: number[][] = Array.from({ length: 7 }, () => [])
  let excludedDays = 0
  for (let i = 1; i < bars.length; i++) {
    const prev = bars[i - 1]
    const cur = bars[i]
    if (cur.time - prev.time !== DAY_SEC) {
      excludedDays++
      continue
    }
    weekday[weekdayIndex(cur.time)].push(cur.close / prev.close - 1)
  }

  // Month aggregates (bars are ascending, so the last bar seen in a month is its last close).
  const months = new Map<number, MonthAgg>()
  for (const b of bars) {
    const d = new Date(b.time * 1000)
    const year = d.getUTCFullYear()
    const month = d.getUTCMonth()
    const day = d.getUTCDate()
    const key = monthKey(year, month)
    let m = months.get(key)
    if (!m) {
      m = { year, month, days: new Set(), lastClose: b.close, lastDay: day }
      months.set(key, m)
    }
    m.days.add(day)
    m.lastClose = b.close
    m.lastDay = day
  }

  const keys = [...months.keys()].sort((a, b) => a - b)
  const monthly: MonthCell[] = []
  const perMonth: number[][] = Array.from({ length: 12 }, () => [])
  for (const key of keys) {
    const m = months.get(key)!
    const prev = months.get(key - 1)
    const ret = prev ? m.lastClose / prev.lastClose - 1 : null
    const complete =
      ret !== null &&
      prev !== undefined &&
      m.days.size === daysInMonthUtc(m.year, m.month) &&
      prev.lastDay === daysInMonthUtc(prev.year, prev.month)
    monthly.push({ year: m.year, month: m.month, ret, complete })
    if (complete && ret !== null) perMonth[m.month].push(ret)
  }

  const years = [...new Set(monthly.map((c) => c.year))].sort((a, b) => a - b)
  return {
    years,
    monthly,
    byMonth: perMonth.map(returnStats),
    byWeekday: weekday.map(returnStats),
    firstTime: bars.length > 0 ? bars[0].time : null,
    lastTime: bars.length > 0 ? bars[bars.length - 1].time : null,
    excludedDays,
    barCount: bars.length,
  }
}

/**
 * Why a grid cell is muted, in plain words (null for a complete month). Derived from the
 * cell and the result only, so it needs no extra fields on `MonthCell`.
 */
export function incompleteReason(cell: MonthCell, result: SeasonalsResult): string | null {
  if (cell.complete) return null
  const first = result.monthly[0]
  if (cell.ret === null) {
    if (first && first.year === cell.year && first.month === cell.month) {
      return 'First month of history: no prior month-end close to measure from. Excluded from stats.'
    }
    return 'The previous month has no data. Excluded from stats.'
  }
  if (result.lastTime !== null) {
    const last = new Date(result.lastTime * 1000)
    const lastDay = last.getUTCDate()
    if (
      last.getUTCFullYear() === cell.year &&
      last.getUTCMonth() === cell.month &&
      lastDay < daysInMonthUtc(cell.year, cell.month)
    ) {
      return `Partial month: month-to-date return through ${MONTH_LABELS[cell.month]} ${lastDay} (UTC). Excluded from stats.`
    }
  }
  return 'Missing daily bars in this month or on the previous month-end (exchange gap). Excluded from stats.'
}

function abortedError(symbol: string): MarketDataError {
  return new MarketDataError('aborted', `Daily history for ${symbol} was cancelled`)
}

/**
 * Up to `maxBars` (default 3650) daily candles for `symbol`, ascending, unique by time. Pages
 * backwards from the latest bar with `endTime = oldest.time - 1` until a page is empty, adds
 * nothing new, or `maxBars` is reached (the newest `maxBars` are kept). A short page is not
 * treated as the end, since some providers cap rows below the requested limit. Without
 * `nowSec` the last row may still be forming (`computeSeasonals` drops it). With `nowSec`,
 * rows still forming at `nowSec` (or in the future) are dropped while paging, so `maxBars`
 * counts closed bars only and a result of exactly `maxBars` rows means the cap was hit.
 * Aborting `signal` rejects with kind 'aborted'.
 */
export async function loadDailyHistory(
  provider: MarketDataProvider,
  symbol: string,
  opts: { signal?: AbortSignal; maxBars?: number; nowSec?: number },
): Promise<Candle[]> {
  const { signal, nowSec } = opts
  const maxBars = Math.max(1, Math.floor(opts.maxBars ?? DEFAULT_MAX_BARS))
  const byTime = new Map<number, Candle>()
  let endTime: number | undefined
  while (byTime.size < maxBars) {
    if (signal?.aborted) throw abortedError(symbol)
    const page = await provider.getCandles(symbol, '1d', PAGE_SIZE, endTime, { signal })
    if (signal?.aborted) throw abortedError(symbol)
    if (page.length === 0) break
    let added = 0
    let oldest = Infinity
    for (const c of page) {
      if (c.time < oldest) oldest = c.time
      if (nowSec !== undefined && c.time + DAY_SEC > nowSec) continue // still forming
      if (!byTime.has(c.time)) {
        byTime.set(c.time, c)
        added++
      }
    }
    if (added === 0 || !Number.isFinite(oldest)) break
    const next = oldest - 1
    if (endTime !== undefined && next >= endTime) break // the provider ignored endTime
    endTime = next
  }
  const all = [...byTime.values()].sort((a, b) => a.time - b.time)
  return all.length > maxBars ? all.slice(all.length - maxBars) : all
}

/**
 * Whether a `loadDailyHistory(..., { nowSec })` result was cut at the cap: `closedRows` closed
 * bars came back and the cap is `maxBars`. Pass the loader's row count, not a count that
 * includes a forming row.
 */
export function historyTruncated(closedRows: number, maxBars: number = DEFAULT_MAX_BARS): boolean {
  return closedRows >= maxBars
}

/**
 * Roving-focus target for a tablist key press: the index of the tab to select, or null when the
 * key is not a tab-navigation key. Arrows wrap; Home/End jump to the ends.
 */
export function nextTabIndex(current: number, count: number, key: string): number | null {
  if (count <= 0) return null
  switch (key) {
    case 'ArrowRight':
    case 'ArrowDown':
      return (current + 1) % count
    case 'ArrowLeft':
    case 'ArrowUp':
      return (current - 1 + count) % count
    case 'Home':
      return 0
    case 'End':
      return count - 1
    default:
      return null
  }
}
