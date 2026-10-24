// Pure helpers behind App's drawing and alert wiring: price formatting by tick size, the
// context-menu horizontal line, alert price lines, the toast queue, and the two small UI
// prefs (default drawing style, open sidebar panel) kept in localStorage.

import { formatPrice } from '../chart/legend'
import type { AlertLine, Drawing, DrawingStyle } from '../chart/drawings/types'
import type { AlertToast } from '../components/AlertToasts'
import type { SidebarPanel } from '../components/SidebarRail'
import type { Alert } from '../data/alertTypes'
import { parseDrawingStyle } from '../data/drawingsApi'

/** Upper bound on decimals derived from a tick size (Binance ticks go down to 1e-8). */
const MAX_DECIMALS = 10

/**
 * Decimals implied by a tick size: 0.01 → 2, 0.5 → 1, 1 → 0, 0.00001 → 5. Null when the tick
 * size is missing, not finite, or not positive.
 */
export function priceDecimals(tickSize: number | undefined): number | null {
  if (tickSize === undefined || !Number.isFinite(tickSize) || tickSize <= 0) return null
  for (let d = 0; d <= MAX_DECIMALS; d++) {
    const scaled = tickSize * 10 ** d
    const whole = Math.round(scaled)
    // Relative tolerance: 0.1 * 10 is 1.0000000000000002 in floating point.
    if (whole >= 1 && Math.abs(scaled - whole) < 1e-6 * whole) return d
  }
  return MAX_DECIMALS
}

function groupThousands(text: string): string {
  const [int, frac] = text.split('.')
  const sign = int.startsWith('-') ? '-' : ''
  const digits = sign ? int.slice(1) : int
  const grouped = digits.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  return frac === undefined ? `${sign}${grouped}` : `${sign}${grouped}.${frac}`
}

/**
 * A price for display, with as many decimals as the symbol's tick size and thousands
 * separators ("65,012.35"). Without a usable tick size it falls back to the legend's
 * magnitude-based precision. Locale-independent.
 */
export function formatPriceByTick(price: number, tickSize: number | undefined): string {
  if (!Number.isFinite(price)) return '—'
  const decimals = priceDecimals(tickSize)
  const text = decimals === null ? formatPrice(price) : price.toFixed(decimals)
  return groupThousands(text)
}

/**
 * The horizontal line the context menu's "Draw horizontal line" adds. Its anchor time is the
 * bar under the cursor, else the last candle, else `nowSeconds`. Null for a price an anchor
 * cannot hold (not finite or not positive).
 */
export function buildHLineDrawing(opts: {
  id: string
  symbol: string
  price: number
  time: number | null
  lastCandleTime: number | null
  style: DrawingStyle
  nowSeconds: number
}): Drawing | null {
  const { id, symbol, price, style } = opts
  if (!Number.isFinite(price) || price <= 0) return null
  const time = opts.time ?? opts.lastCandleTime ?? Math.floor(opts.nowSeconds)
  return {
    id,
    symbol,
    kind: 'hline',
    anchors: [{ time, price }],
    style: { ...style },
  }
}

/**
 * Focus targets whose keys are never the chart's: form fields, editable content, and any
 * dialog, confirm (alertdialog), menu or listbox, which handle Delete/Backspace/Escape themselves.
 */
export const CHART_KEY_BLOCKERS =
  'input, textarea, select, [contenteditable]:not([contenteditable="false"]), ' +
  '[role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"]'

/** The parts of a DOM element the key check needs (an Element qualifies). */
interface KeyTargetLike {
  closest(selectors: string): unknown
  isContentEditable?: boolean
}

function isKeyTargetLike(target: unknown): target is KeyTargetLike {
  return (
    typeof target === 'object' &&
    target !== null &&
    typeof (target as { closest?: unknown }).closest === 'function'
  )
}

/**
 * Whether the chart may act on Delete / Backspace / Escape from a keydown with this target:
 * only when nothing is focused (the target is `body`) or focus is inside the chart, and never
 * inside an editable or a [role=dialog|alertdialog|menu|listbox]. So Backspace on a focused
 * toolbar or alerts-panel button never deletes the selected drawing.
 */
export function chartOwnsKeyTarget(
  target: unknown,
  body: unknown,
  container: { contains(other: never): boolean } | null,
): boolean {
  if (!isKeyTargetLike(target)) return false
  if (target.isContentEditable === true) return false
  if (target.closest(CHART_KEY_BLOCKERS) !== null) return false
  if (target === body) return true
  return container !== null && container.contains(target as never)
}

/**
 * Whether the chart may act on Escape (cancel tool / creation, deselect) from this target.
 * Wider than `chartOwnsKeyTarget`: a focused button outside the chart (e.g. the drawing rail
 * tool just clicked) still lets Escape through, but editables and dialogs/menus keep it.
 */
export function chartAcceptsEscape(target: unknown): boolean {
  if (!isKeyTargetLike(target)) return false
  if (target.isContentEditable === true) return false
  return target.closest(CHART_KEY_BLOCKERS) === null
}

/** Dashed price lines for the active alerts of `symbol`. */
export function alertLinesFor(alerts: readonly Alert[], symbol: string): AlertLine[] {
  return alerts
    .filter((a) => a.active && a.symbol === symbol)
    .map((a) => ({ id: a.id, price: a.price, title: 'Alert' }))
}

/** Toasts kept in memory; the component shows the newest few and "+n more". */
export const MAX_TOASTS = 20

/** Appends a toast; a key already queued is not added twice. Oldest are dropped past `max`. */
export function pushToast(
  toasts: readonly AlertToast[],
  toast: AlertToast,
  max = MAX_TOASTS,
): AlertToast[] {
  if (toasts.some((t) => t.key === toast.key)) return [...toasts]
  const next = [...toasts, toast]
  return next.length > max ? next.slice(next.length - max) : next
}

export function dismissToast(toasts: readonly AlertToast[], key: string): AlertToast[] {
  return toasts.filter((t) => t.key !== key)
}

// ---------------------------------------------------------------------------------------------
// Small UI prefs (best effort: storage may be full, disabled, or throw on access)
// ---------------------------------------------------------------------------------------------

export const DRAWING_STYLE_KEY = 'tv.drawingStyle.v1'
export const SIDEBAR_PANEL_KEY = 'tv.sidebar.v1'

export type UiStorage = Pick<Storage, 'getItem' | 'setItem'>

function defaultStorage(): UiStorage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    return null
  }
}

function readJson(storage: UiStorage | null, key: string): unknown {
  if (!storage) return undefined
  try {
    const text = storage.getItem(key)
    return text === null ? undefined : (JSON.parse(text) as unknown)
  } catch {
    return undefined
  }
}

function writeJson(storage: UiStorage | null, key: string, value: unknown): void {
  if (!storage) return
  try {
    storage.setItem(key, JSON.stringify(value))
  } catch {
    // Quota exceeded or storage disabled: keep it in memory only.
  }
}

/** The remembered default style for new drawings, or `fallback` when none (or invalid). */
export function loadDrawingStyle(
  fallback: DrawingStyle,
  storage: UiStorage | null = defaultStorage(),
): DrawingStyle {
  return parseDrawingStyle(readJson(storage, DRAWING_STYLE_KEY)) ?? { ...fallback }
}

export function saveDrawingStyle(
  style: DrawingStyle,
  storage: UiStorage | null = defaultStorage(),
): void {
  const clean = parseDrawingStyle(style)
  if (clean !== null) writeJson(storage, DRAWING_STYLE_KEY, clean)
}

/** The sidebar panel open at the last visit; 'watchlist' when never saved or unreadable. */
export function loadSidebarPanel(
  storage: UiStorage | null = defaultStorage(),
): SidebarPanel | null {
  const raw = readJson(storage, SIDEBAR_PANEL_KEY)
  if (raw === 'watchlist' || raw === 'alerts') return raw
  if (raw === 'none') return null
  return 'watchlist'
}

export function saveSidebarPanel(
  panel: SidebarPanel | null,
  storage: UiStorage | null = defaultStorage(),
): void {
  writeJson(storage, SIDEBAR_PANEL_KEY, panel ?? 'none')
}
