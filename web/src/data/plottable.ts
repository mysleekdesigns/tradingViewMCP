// Which bars the chart plots.
//
// Binance returns a kline for every interval, even when no trade happened in it (rare on Binance
// global, common on thin venues such as Binance.US): such a bar has volume 0 and
// open = high = low = close = the previous close, and draws as a flat dash. TradingView builds
// bars from trades, so an interval without trades has no bar at all. We do the same: bars with
// no volume are left out of everything the chart shows (price series, volume, indicators,
// legend, drawings index, backtest overlay, bar count). The raw bars stay in the controller for
// pagination, gap fills, and the quote. Nothing is forward-filled or synthesised; times stay in
// UNIX seconds.

import type { Candle } from '../types'

/** True when at least one trade happened in the bar (volume > 0). NaN counts as no trade. */
export function isTradedBar(c: Candle): boolean {
  return c.volume > 0
}

/**
 * Traded bars of `raw` (ascending, unique times), in order. Elements keep identity, so the
 * chart's tail-update planning (`planSeriesSync`) works on the result exactly as on raw bars.
 *
 * - Every bar traded (the usual case on liquid pairs) → `raw` itself.
 * - Otherwise the filtered bars; when they are the same objects as `prev` (e.g. a tick on a
 *   still-untraded forming bar), `prev` itself is returned so consumers see no change.
 */
export function plottableCandles(
  raw: readonly Candle[],
  prev?: readonly Candle[] | null,
): readonly Candle[] {
  if (raw.every(isTradedBar)) return raw
  const out = raw.filter(isTradedBar)
  if (prev != null && sameBars(prev, out)) return prev
  return out
}

function sameBars(a: readonly Candle[], b: readonly Candle[]): boolean {
  if (a.length !== b.length) return false
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false
  return true
}
