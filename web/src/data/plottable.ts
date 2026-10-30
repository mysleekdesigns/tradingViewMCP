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
import { lineageOf, recordLineage } from './candleStore'

/** True when at least one trade happened in the bar (volume > 0). NaN counts as no trade. */
export function isTradedBar(c: Candle): boolean {
  return c.volume > 0
}

/** Index of the first untraded bar of an array (its length when every bar traded). */
interface TradedInfo {
  length: number
  firstUntraded: number
}

const tradedInfo = new WeakMap<readonly Candle[], TradedInfo>()
/** The array `plottableCandles` returned for a raw array: element-wise `raw.filter(isTradedBar)`. */
const plottedOf = new WeakMap<readonly Candle[], readonly Candle[]>()

/**
 * Index of the first untraded bar of `raw` (raw.length if none). With a recorded lineage to an
 * array already seen, only the bars after the kept prefix are read (a live tick: O(1)).
 */
function firstUntradedOf(raw: readonly Candle[]): number {
  const known = tradedInfo.get(raw)
  if (known !== undefined && known.length === raw.length) return known.firstUntraded
  let first: number | null = null
  let from = 0
  const step = lineageOf(raw)
  if (step !== null) {
    const parent = tradedInfo.get(step.parent)
    if (parent !== undefined && parent.length === step.parent.length) {
      // raw[i] === parent[i] for i < samePrefix: an untraded bar inside the kept prefix is
      // raw's first untraded bar too; otherwise the kept prefix is all traded.
      if (parent.firstUntraded < step.samePrefix) first = parent.firstUntraded
      else from = step.samePrefix
    }
  }
  if (first === null) {
    first = from
    while (first < raw.length && isTradedBar(raw[first])) first++
  }
  tradedInfo.set(raw, { length: raw.length, firstUntraded: first })
  return first
}

/** First index of ascending `bars` with time >= `t`. */
function lowerBoundTime(bars: readonly Candle[], t: number): number {
  let lo = 0
  let hi = bars.length
  while (lo < hi) {
    const mid = (lo + hi) >>> 1
    if (bars[mid].time < t) lo = mid + 1
    else hi = mid
  }
  return lo
}

/**
 * `raw.filter(isTradedBar)` built from the plotted array of `raw`'s recorded parent, reading
 * only the bars after the kept prefix. `same` = how many leading bars it shares with that
 * plotted array (`plotted`). Null when no lineage is known.
 */
function filteredFromLineage(
  raw: readonly Candle[],
): { plotted: readonly Candle[]; tail: Candle[]; keep: number; same: number } | null {
  const step = lineageOf(raw)
  if (step === null) return null
  const plotted = plottedOf.get(step.parent)
  if (plotted === undefined) return null
  const parent = step.parent
  const s = step.samePrefix
  // plotted = parent's traded bars in order (same objects), so those taken from parent[0..s)
  // are exactly the ones opening before parent[s] (times ascending and unique).
  const keep = s >= parent.length ? plotted.length : lowerBoundTime(plotted, parent[s].time)
  const tail: Candle[] = []
  for (let i = s; i < raw.length; i++) if (isTradedBar(raw[i])) tail.push(raw[i])
  let m = 0
  while (m < tail.length && keep + m < plotted.length && tail[m] === plotted[keep + m]) m++
  return { plotted, tail, keep, same: keep + m }
}

/**
 * Traded bars of `raw` (ascending, unique times), in order. Elements keep identity, so the
 * chart's tail-update planning (`planSeriesSync`) works on the result exactly as on raw bars.
 *
 * - Every bar traded (the usual case on liquid pairs) → `raw` itself.
 * - Otherwise the filtered bars; when they are the same objects as `prev` (e.g. a tick on a
 *   still-untraded forming bar), `prev` itself is returned so consumers see no change.
 *
 * Cost: when `raw` came from `applyLiveCandle` on an array this function has already seen (a
 * live tick), only the changed tail is read; otherwise every bar is read once. A new filtered
 * array records its lineage to the previous plotted array, so `planSeriesSync` skips its scan.
 */
export function plottableCandles(
  raw: readonly Candle[],
  prev?: readonly Candle[] | null,
): readonly Candle[] {
  if (firstUntradedOf(raw) === raw.length) {
    plottedOf.set(raw, raw)
    return raw
  }
  const fast = filteredFromLineage(raw)
  let out: Candle[]
  if (fast !== null) {
    const { plotted, tail, keep, same } = fast
    const unchanged = same === plotted.length && keep + tail.length === plotted.length
    if (prev === plotted) {
      if (unchanged) {
        plottedOf.set(raw, prev)
        return prev
      }
      // Differs from prev by construction: no need for the element-wise comparison below.
      out = plotted.slice(0, keep)
      for (const c of tail) out.push(c)
      recordLineage(out, plotted, same)
      plottedOf.set(raw, out)
      return out
    }
    out = plotted.slice(0, keep)
    for (const c of tail) out.push(c)
    recordLineage(out, plotted, same)
  } else {
    out = raw.filter(isTradedBar)
  }
  const result = prev != null && sameBars(prev, out) ? prev : out
  plottedOf.set(raw, result)
  return result
}

function sameBars(a: readonly Candle[], b: readonly Candle[]): boolean {
  if (a.length !== b.length) return false
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false
  return true
}
