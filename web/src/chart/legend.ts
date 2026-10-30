import type { Candle } from '../types'

/**
 * Format a price with precision that scales with magnitude, so BTC shows cents and
 * low-priced alts keep their significant digits. Locale-independent.
 */
export function formatPrice(value: number): string {
  if (!Number.isFinite(value)) return '—'
  const abs = Math.abs(value)
  let decimals: number
  if (abs >= 1) decimals = 2
  else if (abs >= 0.01) decimals = 4
  else if (abs === 0) decimals = 2
  else decimals = 8
  return value.toFixed(decimals)
}

const VOLUME_UNITS: readonly (readonly [number, string])[] = [
  [1, ''],
  [1e3, 'K'],
  [1e6, 'M'],
  [1e9, 'B'],
  [1e12, 'T'],
]

/** Abbreviate a volume: 999.5 -> "999.50", 1234 -> "1.23K", 4_560_000 -> "4.56M". */
export function formatVolume(value: number): string {
  if (!Number.isFinite(value)) return '—'
  const abs = Math.abs(value)
  let i = 0
  while (i < VOLUME_UNITS.length - 1 && abs >= VOLUME_UNITS[i + 1][0]) i++
  // Promote when rounding would print 1000.00 of the current unit (e.g. 999_999 -> "1.00M").
  if (i < VOLUME_UNITS.length - 1 && Number((abs / VOLUME_UNITS[i][0]).toFixed(2)) >= 1000) i++
  const [size, suffix] = VOLUME_UNITS[i]
  return `${(value / size).toFixed(2)}${suffix}`
}

/** Percent change of `close` relative to `base`. Returns null when the base is zero or missing. */
export function changePercent(close: number, base: number | undefined): number | null {
  if (base === undefined || base === 0 || !Number.isFinite(base) || !Number.isFinite(close)) {
    return null
  }
  return ((close - base) / base) * 100
}

/** "+1.23%", "-0.50%", "0.00%"; "—" when unknown. */
export function formatChange(pct: number | null): string {
  if (pct === null) return '—'
  const rounded = Number(pct.toFixed(2))
  if (rounded === 0) return '0.00%'
  return `${rounded > 0 ? '+' : ''}${rounded.toFixed(2)}%`
}

/**
 * Index of the candle to show in the legend: the hovered bar when its time exists in
 * `candles`, otherwise the last candle (cursor left the chart / off the data). -1 if empty.
 * `candles` must be sorted ascending by time (binary search).
 */
export function selectLegendIndex(candles: readonly Candle[], hoveredTime?: number): number {
  if (candles.length === 0) return -1
  if (hoveredTime === undefined) return candles.length - 1
  let lo = 0
  let hi = candles.length - 1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    const t = candles[mid].time
    if (t === hoveredTime) return mid
    if (t < hoveredTime) lo = mid + 1
    else hi = mid - 1
  }
  return candles.length - 1
}

export type Direction = 'up' | 'down' | 'flat'

export interface LegendValues {
  time: number
  open: string
  high: string
  low: string
  close: string
  volume: string
  change: string
  direction: Direction
}

/**
 * Build the legend values for the candle at `index`. Change % is measured against the
 * previous candle's close (TradingView convention), or the bar's own open for the first bar.
 */
export function buildLegend(candles: readonly Candle[], index: number): LegendValues | null {
  const c = candles[index]
  if (index < 0 || c === undefined) return null
  const base = index > 0 ? candles[index - 1].close : c.open
  const pct = changePercent(c.close, base)
  const direction: Direction =
    pct === null || Number(pct.toFixed(2)) === 0 ? 'flat' : pct > 0 ? 'up' : 'down'
  return {
    time: c.time,
    open: formatPrice(c.open),
    high: formatPrice(c.high),
    low: formatPrice(c.low),
    close: formatPrice(c.close),
    volume: formatVolume(c.volume),
    change: formatChange(pct),
    direction,
  }
}

/**
 * Open time of the first plotted bar at or after logical index `from` (the left edge of the visible
 * range), or null when there are no bars or the view starts past the last one.
 */
export function firstVisibleBarTime(bars: readonly Candle[] | null, from: number): number | null {
  if (!bars || bars.length === 0 || !Number.isFinite(from)) return null
  const index = Math.max(0, Math.ceil(from))
  return index < bars.length ? bars[index].time : null
}

/** Cached: constructing an Intl formatter per call (what `toLocaleDateString` does) is costly. */
const AS_OF_FORMAT = new Intl.DateTimeFormat('en-US', {
  month: 'short',
  day: 'numeric',
  timeZone: 'UTC',
})

/**
 * "Oct 3": the UTC date of `time` (UNIX seconds), as a derivatives pane legend dates a value older
 * than the last plotted bar. Same text as `toLocaleDateString('en-US', { month: 'short', day:
 * 'numeric', timeZone: 'UTC' })`, from one cached formatter.
 */
export function formatAsOfDate(time: number): string {
  return AS_OF_FORMAT.format(new Date(time * 1000))
}
