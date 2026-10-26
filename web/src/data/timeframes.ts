// Chart timeframes: the native Intervals plus seconds and custom ones built by aggregation.
// All times are UNIX seconds (UTC).
//
// Canonical form and limits (what parseTimeframe accepts and returns):
//  - seconds: a divisor of 60 below 60 (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30); 60s -> 1m
//  - minutes: 1..720; whole hours become hours (90m stays, 120m -> 2h)
//  - hours:   1..168; whole days become days (48h -> 2d)
//  - days:    1..365; whole weeks become weeks (14d -> 2w)
//  - weeks:   1..52
// With these caps one bucket never needs more than 1000 bars of its base interval (the
// largest ratio is 719m from 1m), so a bucket always fits in one klines page.
//
// Bucket alignment: seconds, minutes and hours are aligned to the UNIX epoch (Binance's 2h, 6h,
// 8h and 12h bars open at multiples of their length). Days and weeks are anchored at Monday
// 1970-01-05 00:00 UTC, like Binance's 1w and 3d bars (3d bars open at 86400 mod 259200;
// verified live 2026-10-26 on BTCUSDT/ETHUSDT/SUIUSDT, including 2020 bars). For 1d and 2d
// that anchor coincides with the epoch.
import { INTERVALS, type Interval, type Timeframe, type TimeframeUnit } from '../types'

/** Quick-pick timeframes, shortest first. */
export const PRESET_TIMEFRAMES: readonly Timeframe[] = [
  '1s',
  '5s',
  '15s',
  '30s',
  '1m',
  '3m',
  '5m',
  '15m',
  '30m',
  '1h',
  '2h',
  '4h',
  '6h',
  '12h',
  '1d',
  '3d',
  '1w',
]

const UNIT_SECONDS: Record<TimeframeUnit, number> = {
  s: 1,
  m: 60,
  h: 3_600,
  d: 86_400,
  w: 604_800,
}

/** Largest count accepted per unit (seconds are further limited to divisors of 60). */
const MAX_COUNT: Record<TimeframeUnit, number> = { s: 60, m: 720, h: 168, d: 365, w: 52 }

/** Monday 1970-01-05 00:00 UTC: the anchor of Binance's 1w (and 3d) bars. */
const MONDAY_ANCHOR_S = 345_600

const UNIT_NAMES: Record<TimeframeUnit, string> = {
  s: 'second',
  m: 'minute',
  h: 'hour',
  d: 'day',
  w: 'week',
}

const TF_PATTERN = /^([1-9]\d*)([smhdw])$/

function split(tf: Timeframe): { count: number; unit: TimeframeUnit } {
  const m = TF_PATTERN.exec(tf)
  if (m === null) throw new Error(`Invalid timeframe: ${String(tf)}`)
  return { count: Number(m[1]), unit: m[2] as TimeframeUnit }
}

/**
 * Parse user input such as '5s', '3m', '2H', '1D', '1W' or ' 15 ' (a bare number means
 * minutes, as in TradingView). Units are case-insensitive except 'M', which is rejected
 * (TradingView's month; months are not supported). Returns the canonical timeframe (see the
 * header comment: 60s -> 1m, 120m -> 2h, 48h -> 2d, 14d -> 2w), or null when the input is not
 * a positive integer count with a unit inside the limits.
 */
export function parseTimeframe(s: string): Timeframe | null {
  const m = /^(\d+)\s*([a-zA-Z]?)$/.exec(s.trim())
  if (m === null) return null
  const count = Number(m[1])
  if (!Number.isSafeInteger(count) || count < 1) return null
  const rawUnit = m[2]
  if (rawUnit === 'M') return null
  const unit = (rawUnit === '' ? 'm' : rawUnit.toLowerCase()) as string
  if (!(unit in UNIT_SECONDS)) return null
  return canonical(count, unit as TimeframeUnit)
}

function canonical(count: number, unit: TimeframeUnit): Timeframe | null {
  if (count > MAX_COUNT[unit]) return null
  switch (unit) {
    case 's':
      if (count === 60) return '1m'
      return 60 % count === 0 ? `${count}s` : null
    case 'm':
      return count % 60 === 0 ? canonical(count / 60, 'h') : `${count}m`
    case 'h':
      return count % 24 === 0 ? canonical(count / 24, 'd') : `${count}h`
    case 'd':
      return count % 7 === 0 ? canonical(count / 7, 'w') : `${count}d`
    case 'w':
      return `${count}w`
  }
}

/** Length of one bar in seconds. Throws on a malformed timeframe. */
export function timeframeSeconds(tf: Timeframe): number {
  const { count, unit } = split(tf)
  return count * UNIT_SECONDS[unit]
}

export function isNativeInterval(tf: Timeframe): tf is Interval {
  return (INTERVALS as readonly string[]).includes(tf)
}

export function isSecondsTimeframe(tf: Timeframe): boolean {
  return split(tf).unit === 's'
}

/** Offset (seconds, in [0, len)) of the bucket grid from the epoch. */
function anchorOffset(tf: Timeframe): number {
  const { unit } = split(tf)
  if (unit !== 'd' && unit !== 'w') return 0
  return MONDAY_ANCHOR_S % timeframeSeconds(tf)
}

/**
 * The native source whose bars tile `tf` exactly with the fewest bars: '1s' for seconds
 * timeframes, otherwise the longest Interval whose length divides `tf` and whose grid lines
 * up with `tf`'s grid. A native Interval maps to itself.
 */
export function baseFor(tf: Timeframe): Interval | '1s' {
  if (isNativeInterval(tf)) return tf
  if (isSecondsTimeframe(tf)) return '1s'
  const len = timeframeSeconds(tf)
  const off = anchorOffset(tf)
  for (let i = INTERVALS.length - 1; i >= 0; i--) {
    const iv = INTERVALS[i]
    const ivLen = timeframeSeconds(iv)
    if (len % ivLen === 0 && (off - anchorOffset(iv)) % ivLen === 0) return iv
  }
  // Every minute-or-longer timeframe is a whole number of 1m bars.
  return '1m'
}

/** Toolbar label: '30s', '5m', '2h', '1D', '3D', '1W' (days and weeks in TradingView capitals). */
export function timeframeLabel(tf: Timeframe): string {
  const { count, unit } = split(tf)
  return unit === 'd' || unit === 'w' ? `${count}${unit.toUpperCase()}` : `${count}${unit}`
}

/** Spoken name: '1 second', '30 seconds', '2 hours', '1 week'. */
export function timeframeName(tf: Timeframe): string {
  const { count, unit } = split(tf)
  return `${count} ${UNIT_NAMES[unit]}${count === 1 ? '' : 's'}`
}

/** Open time (UNIX seconds) of the `tf` bucket that contains `timeSec`. */
export function bucketStart(timeSec: number, tf: Timeframe): number {
  const len = timeframeSeconds(tf)
  const off = anchorOffset(tf)
  return Math.floor((Math.floor(timeSec) - off) / len) * len + off
}
