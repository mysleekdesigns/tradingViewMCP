import { changePercent, formatChange, formatPrice, type Direction } from '../chart/legend'
import { timeframeSeconds } from '../data/timeframes'
import type { Candle, Timeframe } from '../types'

const DAY_SECONDS = 86_400

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
function candleAtOrBefore(candles: readonly Candle[], target: number): Candle | undefined {
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
 * Price at the instant `target`: the open of the bar starting exactly then, else the close of
 * the latest bar before it (a gap means nothing traded in between, so that close still stands).
 */
function priceAt(candles: readonly Candle[], target: number): number | undefined {
  const bar = candleAtOrBefore(candles, target)
  if (bar === undefined) return undefined
  return bar.time === target ? bar.open : bar.close
}

/** `diff` rounded to the same decimals as `price`, so tiny moves read "0.00" and never "-0.00". */
function roundDiff(diff: number, price: string): { value: number; text: string } {
  const decimals = price.split('.')[1]?.length ?? 0
  const value = Number(diff.toFixed(decimals))
  if (value === 0) return { value: 0, text: (0).toFixed(decimals) }
  return { value, text: `${value > 0 ? '+' : ''}${value.toFixed(decimals)}` }
}

/**
 * Header quote for the latest candle. The latest candle may still be forming, so this is
 * a display value only.
 *
 * Timeframes shorter than a day (seconds included) compare against the price 24h before the last bar's open time. When less
 * than 24h is loaded (e.g. 1m with 500 bars), they fall back to the previous close, and `basis`
 * says so.
 */
export function summarizeQuote(candles: readonly Candle[], interval: Timeframe): Quote | null {
  const last = candles[candles.length - 1]
  if (last === undefined) return null
  const intraday = timeframeSeconds(interval) < DAY_SECONDS
  const dayBase = intraday ? priceAt(candles, last.time - DAY_SECONDS) : undefined
  const base = dayBase ?? candles[candles.length - 2]?.close
  const pct = changePercent(last.close, base)
  const price = formatPrice(last.close)
  const diff = base === undefined ? null : roundDiff(last.close - base, price)
  // Colour follows the change the user reads, so "+0.50" on BTC is green even at "0.00%".
  const direction: Direction =
    diff === null || diff.value === 0 ? 'flat' : diff.value > 0 ? 'up' : 'down'
  return {
    price,
    change: diff === null ? '—' : diff.text,
    changePct: formatChange(pct),
    direction,
    basis: dayBase !== undefined ? '24h' : 'vs prev close',
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
