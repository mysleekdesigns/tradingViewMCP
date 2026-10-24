import { changePercent, formatChange, formatPrice, type Direction } from '../chart/legend'
import type { Candle, Interval } from '../types'

const DAY_SECONDS = 86_400

/** Intervals shorter than a day compare against the close 24h earlier; longer ones against the previous bar. */
const INTRADAY: ReadonlySet<Interval> = new Set(['1m', '5m', '15m', '1h', '4h'])

export interface Quote {
  price: string
  /** Signed absolute change, e.g. "+512.30" or "-12.00". */
  change: string
  changePct: string
  direction: Direction
  /** What the change is measured against, e.g. "24h" or "vs prev close". */
  basis: string
}

/** Last candle with time <= `target`, or undefined. `candles` sorted ascending. */
function closeAtOrBefore(candles: readonly Candle[], target: number): Candle | undefined {
  let lo = 0
  let hi = candles.length - 1
  let found: Candle | undefined
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (candles[mid].time <= target) {
      found = candles[mid]
      lo = mid + 1
    } else {
      hi = mid - 1
    }
  }
  return found
}

/**
 * Header quote for the latest candle. The latest candle may still be forming, so this is
 * a display value only.
 */
export function summarizeQuote(candles: readonly Candle[], interval: Interval): Quote | null {
  const last = candles[candles.length - 1]
  if (last === undefined) return null
  const intraday = INTRADAY.has(interval)
  const base = intraday
    ? closeAtOrBefore(candles, last.time - DAY_SECONDS)?.close
    : candles[candles.length - 2]?.close
  const pct = changePercent(last.close, base)
  const diff = base === undefined ? null : last.close - base
  const direction: Direction =
    pct === null || Number(pct.toFixed(2)) === 0 ? 'flat' : pct > 0 ? 'up' : 'down'
  return {
    price: formatPrice(last.close),
    change: diff === null ? '—' : `${diff > 0 ? '+' : ''}${formatPrice(diff)}`,
    changePct: formatChange(pct),
    direction,
    basis: intraday ? '24h' : 'vs prev close',
  }
}

/** "2026-10-24 14:00" in UTC (chart times are UTC). */
export function formatUtc(timeSeconds: number): string {
  return new Date(timeSeconds * 1000).toISOString().slice(0, 16).replace('T', ' ')
}

/** "14:08:31" in UTC, for "loaded at" stamps. */
export function formatUtcClock(date: Date): string {
  return date.toISOString().slice(11, 19)
}
