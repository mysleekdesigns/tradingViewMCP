// Keyboard and screen-reader access to the chart, through TradingView's official
// `@tradingview/lwc-plugin-accessibility` (Apache-2.0). There was no hand-written equivalent: the
// chart canvas was invisible to assistive technology and unreachable from the keyboard.
//
// The plugin adds one focusable, labelled layer per pane (Tab reaches it). While that layer has
// focus: ←/→ move between bars, ↑/↓ switch series, PageUp/PageDown jump, Home/End go to the
// first/last bar, +/- zoom, Enter/Space speak a summary of the visible bars, and the crosshair
// follows the focused bar so sighted keyboard users see it too.
//
// Key conflicts with the app (state/shortcuts.ts, App.tsx, Chart.tsx, state/replay.ts) and how
// they are resolved here (A11Y_KEY_BINDINGS):
// - The plugin's H (help) and T (data table) would swallow two letters of the app's
//   "type A–Z / 0–9 to search symbols" (the plugin calls preventDefault, and App's handler
//   ignores defaultPrevented events). Rebound to ',' (help) and '.' (table; press again to
//   close), which nothing in the app uses. Their physical-key fallbacks (KeyH, KeyT) are removed
//   too: they do not collide (type-to-search takes Latin letters only), but on a non-Latin
//   layout they would still open help / the table while the spoken help names comma and period.
// - The plugin's Escape ("close panels") always calls preventDefault, which would stop Escape
//   from cancelling a drawing (Chart.tsx `chartKey`) and from cancelling a replay pick or exiting
//   replay (App.tsx `replayKeyAction`). Unbound: the table and help panels close with their own
//   key instead.
// - No other overlap: the plugin ignores every Alt/Ctrl/Meta chord (Alt+1…7, Alt+S, Alt+Shift+L,
//   Alt+H/J/T/F/R/A, Cmd/Ctrl+K) and Shift+arrows (replay step / play), and it binds none of
//   '[', ']', '/', '?', Delete or Backspace.
//
// Background data-update announcements are off: a live 1s/1m feed would talk continuously.
// Enter/Space gives an on-demand summary instead.

import {
  addAccessibilityPlugin,
  type AccessibilityOptions,
  type AccessibilityPluginController,
  type AnySeries,
  type KeyBindings,
  type PartialAccessibilityMessages,
} from '@tradingview/lwc-plugin-accessibility'
import type { IChartApi } from 'lightweight-charts'
import { timeToSeconds } from './timeFormat'

export interface ChartAccessibilityOptions {
  /** Accessible name of the chart, e.g. "BTCUSDT 1h chart". */
  title: string
  formatPrice(p: number): string
  /** Formats a bar time in UNIX seconds (UTC). */
  formatTime(t: number): string
}

export interface ChartAccessibility {
  /** Renames every pane's layer (symbol or interval changed). No-op after detach. */
  setTitle(title: string): void
  /** Removes the layers and their listeners. Idempotent; safe after `chart.remove()`. */
  detach(): void
}

/** Key for the spoken keyboard help (the plugin's 'H', rebound: see the header). */
export const A11Y_HELP_KEY = ','
/** Key that opens and closes the data table (the plugin's 'T', rebound: see the header). */
export const A11Y_TABLE_KEY = '.'

/**
 * The plugin's panels that sit inside the chart pane and take pointer input: the data table
 * (`pointer-events: auto`, scrollable) and the shortcuts panel. Class names from the plugin's
 * dist (`lw-chart-a11y-data-table`, `lw-chart-a11y-shortcuts-panel`).
 */
export const A11Y_OVERLAY_SELECTOR = '.lw-chart-a11y-data-table, .lw-chart-a11y-shortcuts-panel'

/**
 * True when a pointer/wheel event's target is inside one of the plugin's panels: a click or
 * scroll there belongs to the panel, never to the chart (no drawing, no replay pick, no zoom).
 */
export function isAccessibilityOverlay(target: EventTarget | null): boolean {
  const closest = (target as { closest?: unknown } | null)?.closest
  if (typeof closest !== 'function') return false
  return (closest as (selector: string) => unknown).call(target, A11Y_OVERLAY_SELECTOR) != null
}

/**
 * Merged onto the plugin's built-in key map: a `null` removes a built-in binding. Keys are
 * `KeyboardEvent.key` values, or `KeyboardEvent.code` values for the physical-key fallbacks.
 */
export const A11Y_KEY_BINDINGS: KeyBindings = {
  h: null,
  H: null,
  KeyH: null,
  t: null,
  T: null,
  KeyT: null,
  Escape: null,
  [A11Y_HELP_KEY]: 'help',
  [A11Y_TABLE_KEY]: 'viewAsTable',
}

/**
 * The plugin's spoken strings that name its keys, reworded for A11Y_KEY_BINDINGS. Everything
 * else stays the plugin's built-in English.
 */
export const A11Y_MESSAGES: PartialAccessibilityMessages = {
  paneLabel: ({ title, paneIndex, paneCount, seriesCount, seriesLabel }) => {
    const pane = paneCount > 1 ? `Pane ${paneIndex + 1} of ${paneCount}. ` : ''
    const series =
      seriesCount > 1 ? `${seriesCount} series. ` : seriesLabel ? `${seriesLabel}. ` : ''
    return `${title}. ${pane}${series}Press comma for keyboard help.`
  },
  help: ({ multiSeries, pageStep }) =>
    'Keyboard controls. Left and right arrows move between bars. ' +
    (multiSeries ? 'Up and down arrows switch between series. ' : '') +
    `Page Up jumps ${pageStep} bars forward, Page Down ${pageStep} bars back. ` +
    'Home and End jump to the first and last bars. Plus and minus zoom the chart in and out. ' +
    'Enter or Space reads a summary of the bars in view. ' +
    'Period shows the data as a table; press period again to close it. Comma repeats this help.',
  shortcutsHint: 'Press comma for keyboard shortcuts',
  shortcuts: ({ multiSeries, pageStep }) => [
    { keys: '← / →', action: 'Move between bars' },
    ...(multiSeries ? [{ keys: '↑ / ↓', action: 'Switch between series' }] : []),
    { keys: 'Page Up / Page Down', action: `Jump ${pageStep} bars` },
    { keys: 'Home / End', action: 'First / last bar' },
    { keys: '+ / −', action: 'Zoom in / out' },
    { keys: A11Y_TABLE_KEY, action: 'Show or hide the data table' },
    { keys: A11Y_HELP_KEY, action: 'Show or hide this panel' },
  ],
  tableClose: 'Press period to close.',
}

/** A series' spoken name: its title, else "Price" for the candles/bars, else "Series N". */
export function seriesLabelFor(series: AnySeries, index: number): string {
  const title = series.options().title
  if (title !== '') return title
  const type = series.seriesType()
  if (type === 'Candlestick' || type === 'Bar') return 'Price'
  return `Series ${index + 1}`
}

/** The options handed to `addAccessibilityPlugin` (exported for tests). */
export function accessibilityOptions(opts: ChartAccessibilityOptions): AccessibilityOptions {
  return {
    chartTitle: opts.title,
    keyBindings: A11Y_KEY_BINDINGS,
    messages: A11Y_MESSAGES,
    priceFormatter: (p) => opts.formatPrice(p),
    timeFormatter: (time) => {
      // Our series use UTCTimestamp numbers; business days map to 00:00 UTC (timeFormat.ts).
      const s = timeToSeconds(time)
      return Number.isFinite(s) ? opts.formatTime(s) : String(time)
    },
    seriesLabel: seriesLabelFor,
    dataScope: 'visible',
    dataUpdates: { mode: 'none' },
    showFocusIndicator: true,
    syncCrosshair: true,
    focusOnPointerDown: false,
    showShortcuts: false,
  }
}

/**
 * Attaches the accessibility layer to every pane of `chart` (panes added later are picked up on
 * the next redraw). Call after the chart is created; call `detach()` in the same effect's
 * cleanup, before `chart.remove()`.
 */
export function attachChartAccessibility(
  chart: IChartApi,
  opts: ChartAccessibilityOptions,
): ChartAccessibility {
  let controller: AccessibilityPluginController | null = addAccessibilityPlugin(
    chart,
    accessibilityOptions(opts),
  )
  let title = opts.title
  return {
    setTitle(next: string) {
      if (controller === null || next === title) return
      title = next
      controller.applyOptions({ chartTitle: next })
    },
    detach() {
      if (controller === null) return
      const c = controller
      controller = null
      c.detach()
    },
  }
}
