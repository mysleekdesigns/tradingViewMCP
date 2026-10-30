import { changePercent, formatChange, formatPrice, type Direction } from '../chart/legend'
import { timeframeSeconds } from '../data/timeframes'
import { formatClock } from '../state/appSettings'
import { panelSelector, rawCandles, type LiveState, type PanelKey } from '../state/liveStore'
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

/**
 * Same text on screen: equal fields (or both null). Lets a live quote keep its previous object
 * while a tick leaves the displayed price and change as they were.
 */
export function sameQuote(a: Quote | null, b: Quote | null): boolean {
  if (a === b) return true
  if (a === null || b === null) return false
  return (
    a.price === b.price &&
    a.change === b.change &&
    a.changePct === b.changePct &&
    a.direction === b.direction &&
    a.basis === b.basis
  )
}

/** "2026-10-24 14:00" in UTC (chart times are UTC). */
export function formatUtc(timeSeconds: number): string {
  return new Date(timeSeconds * 1000).toISOString().slice(0, 16).replace('T', ' ')
}

/** "14:08:31" in UTC, for "loaded at" stamps. */
export function formatUtcClock(date: Date): string {
  return date.toISOString().slice(11, 19)
}

/** A live-store selector that quotes nothing (replay: the live candles are hidden). */
export const NO_QUOTE = (): Quote | null => null

/**
 * A live-store selector for `panel`'s quote: recomputed only when that panel's report changes
 * (another panel's tick returns the cached quote), null while it has no candles. Pair it with
 * `sameQuote` so a tick that leaves the text as it was keeps the previous object.
 */
export function liveQuoteSelector(panel: PanelKey): (state: LiveState) => Quote | null {
  return panelSelector(panel, (live) => {
    const candles = rawCandles(live)
    return candles ? summarizeQuote(candles, panel.interval) : null
  })
}

/**
 * A live-store selector for `panel`'s "14:08:31" last-update clock in `timezone`, or null before
 * any data. A string, so a tick within the same second changes nothing; the clock is formatted
 * only when the whole second of the update changes (formatClock shows whole seconds).
 */
export function lastUpdateSelector(
  panel: PanelKey,
  timezone: string,
): (state: LiveState) => string | null {
  let lastSecond: number | null = null
  let lastText: string | null = null
  return panelSelector(panel, (live) => {
    if (live.lastUpdate === null) return null
    const second = Math.floor(live.lastUpdate / 1000)
    if (second !== lastSecond || lastText === null) {
      lastSecond = second
      lastText = formatClock(live.lastUpdate, timezone)
    }
    return lastText
  })
}
