// Bar-time alignment helpers. All inputs and outputs are UNIX seconds (UTC).
import { INTERVAL_SECONDS, type Interval } from './types.ts'

/**
 * Binance `1w` bars open Monday 00:00 UTC. Epoch 0 (1970-01-01) was a Thursday, so the first
 * Monday is 1970-01-05 = 4 days = 345600 s. Verified against a live BTCUSDT 1w open time
 * (1792368000 % 604800 === 345600). Every other interval divides a UTC day and aligns to 0.
 */
export const WEEK_OFFSET_SEC = 345_600

function offsetFor(interval: Interval): number {
  return interval === '1w' ? WEEK_OFFSET_SEC : 0
}

/** Open time of the bar of `interval` that contains `t` (UNIX s). */
export function alignOpenTime(t: number, interval: Interval): number {
  const len = INTERVAL_SECONDS[interval]
  const off = offsetFor(interval)
  return Math.floor((Math.floor(t) - off) / len) * len + off
}

/** Open time of the bar that is still forming at `nowSec`. */
export function formingOpenTime(nowSec: number, interval: Interval): number {
  return alignOpenTime(nowSec, interval)
}

/** Open time of the newest bar that is closed at `nowSec` (`time + intervalSec <= nowSec`). */
export function lastClosedOpenTime(nowSec: number, interval: Interval): number {
  return formingOpenTime(nowSec, interval) - INTERVAL_SECONDS[interval]
}

/** Milliseconds (Binance) → UNIX seconds. */
export function msToSec(ms: number): number {
  return Math.floor(ms / 1000)
}
