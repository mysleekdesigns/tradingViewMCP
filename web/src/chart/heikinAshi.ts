import type { Candle } from '../types'

/** One Heikin-Ashi bar. `time` is the source candle's open time in UNIX seconds. */
export interface HeikinAshiBar {
  time: number
  open: number
  high: number
  low: number
  close: number
}

/**
 * HA bar for `c` given the previous HA bar (undefined for the first bar).
 *
 * close = (o + h + l + c) / 4
 * open  = (prevHA.open + prevHA.close) / 2, seeded with (o + c) / 2 on the first bar
 * high  = max(h, open, close), low = min(l, open, close)
 *
 * Bar i depends only on candles 0..i, so a change to the last candle changes only the last HA bar.
 */
export function heikinAshiBar(c: Candle, prev: HeikinAshiBar | undefined): HeikinAshiBar {
  const close = (c.open + c.high + c.low + c.close) / 4
  const open = prev === undefined ? (c.open + c.close) / 2 : (prev.open + prev.close) / 2
  return {
    time: c.time,
    open,
    high: Math.max(c.high, open, close),
    low: Math.min(c.low, open, close),
    close,
  }
}

/** Heikin-Ashi bars for the whole series. */
export function heikinAshi(candles: readonly Candle[]): HeikinAshiBar[] {
  return heikinAshiFrom([], candles, 0)
}

/**
 * Recompute HA bars from index `from` onward, reusing `prev[0..from)` as is.
 * `prev` must be the HA bars of a candle array whose first `from` candles equal `candles[0..from)`.
 * Used for live ticks: only the tail is recomputed.
 */
export function heikinAshiFrom(
  prev: readonly HeikinAshiBar[],
  candles: readonly Candle[],
  from: number,
): HeikinAshiBar[] {
  const start = Math.max(0, Math.min(from, prev.length, candles.length))
  const out = prev.slice(0, start)
  for (let i = start; i < candles.length; i++) {
    out.push(heikinAshiBar(candles[i], out[i - 1]))
  }
  return out
}
