// Time <-> fractional logical index for drawing anchors.
//
// Lightweight Charts positions bars by logical index (0 = oldest loaded bar). Drawing anchors
// are stored as UNIX-seconds times so they survive zooming, scrolling, history prepends and
// interval switches. A time that is not an exact bar open time (drawn on another interval, or
// inside a data gap) maps to a fractional index between its neighbours; a time before the
// first or after the last bar is extrapolated with the interval length. The two directions are
// exact inverses (up to rounding to whole seconds), so shifting an anchor by whole bars and
// mapping back keeps its position relative to the bars around it.

import type { Interval } from '../../types'

/** Bar length of each interval in seconds (used to extrapolate past either end of the data). */
export const INTERVAL_SECONDS: Readonly<Record<Interval, number>> = {
  '1m': 60,
  '5m': 300,
  '15m': 900,
  '1h': 3_600,
  '4h': 14_400,
  '1d': 86_400,
  '1w': 604_800,
}

/** Anything with a bar open time in UNIX seconds (a Candle qualifies). */
export interface TimedBar {
  readonly time: number
}

/** Index of the last bar with time <= t (bars ascending); -1 if t is before the first bar. */
function lastAtOrBefore(bars: readonly TimedBar[], t: number): number {
  let lo = 0
  let hi = bars.length - 1
  let found = -1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (bars[mid].time <= t) {
      found = mid
      lo = mid + 1
    } else {
      hi = mid - 1
    }
  }
  return found
}

/**
 * Fractional logical index of `time` over ascending, unique `bars`. Exact bar times give
 * integers; times between two bars interpolate linearly; times outside the data extrapolate by
 * `stepSeconds` per bar. Null for an empty series or a non-positive step.
 */
export function timeToLogical(
  bars: readonly TimedBar[],
  time: number,
  stepSeconds: number,
): number | null {
  const n = bars.length
  if (n === 0 || !(stepSeconds > 0) || !Number.isFinite(time)) return null
  const first = bars[0].time
  const last = bars[n - 1].time
  if (time <= first) return (time - first) / stepSeconds
  if (time >= last) return n - 1 + (time - last) / stepSeconds
  const i = lastAtOrBefore(bars, time)
  const t0 = bars[i].time
  if (t0 === time) return i
  const t1 = bars[i + 1].time
  return i + (time - t0) / (t1 - t0)
}

/**
 * Inverse of `timeToLogical`: the time (whole UNIX seconds) at a fractional logical index.
 * Integer indices inside the data return that bar's open time; indices past either end are
 * extrapolated bar times. Null for an empty series or a non-positive step.
 */
export function logicalToTime(
  bars: readonly TimedBar[],
  logical: number,
  stepSeconds: number,
): number | null {
  const n = bars.length
  if (n === 0 || !(stepSeconds > 0) || !Number.isFinite(logical)) return null
  const first = bars[0].time
  const last = bars[n - 1].time
  if (logical <= 0) return Math.round(first + logical * stepSeconds)
  if (logical >= n - 1) return Math.round(last + (logical - (n - 1)) * stepSeconds)
  const i = Math.floor(logical)
  const frac = logical - i
  const t0 = bars[i].time
  if (frac === 0) return t0
  const t1 = bars[i + 1].time
  return Math.round(t0 + frac * (t1 - t0))
}

/** Open time of the bar nearest to a fractional logical index (extrapolated outside the data). */
export function snapLogicalToTime(
  bars: readonly TimedBar[],
  logical: number,
  stepSeconds: number,
): number | null {
  if (!Number.isFinite(logical)) return null
  return logicalToTime(bars, Math.round(logical), stepSeconds)
}

/**
 * Mutable holder used by the chart: swapping in a new candles array is O(1) (no copy), so
 * live ticks can update it without rebuilding anything.
 */
export class TimeIndex {
  private bars: readonly TimedBar[]
  private step: number

  constructor(bars: readonly TimedBar[] = [], stepSeconds: number = 60) {
    this.bars = bars
    this.step = stepSeconds
  }

  setBars(bars: readonly TimedBar[]): void {
    this.bars = bars
  }

  setStep(stepSeconds: number): void {
    this.step = stepSeconds
  }

  get length(): number {
    return this.bars.length
  }

  timeToLogical(time: number): number | null {
    return timeToLogical(this.bars, time, this.step)
  }

  logicalToTime(logical: number): number | null {
    return logicalToTime(this.bars, logical, this.step)
  }

  snapLogicalToTime(logical: number): number | null {
    return snapLogicalToTime(this.bars, logical, this.step)
  }
}
