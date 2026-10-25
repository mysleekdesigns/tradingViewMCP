// App-level wiring of Settings: what the chosen data provider means for the rest of the UI
// (venue, labels, server features, lab overlays, default symbol), the symbol fallback when the
// persisted symbol is not listed by that provider, and small formatting helpers. Pure.

import { formatDateTime } from '../chart/timeFormat'
import type { AlertsStatus } from '../data/alertTypes'
import { isDirectProvider, PROVIDER_LABELS, type ProviderId } from '../data/providerIds'
import {
  ARCHIVE_VENUE_ID,
  COINBASE_VENUE,
  DIRECT_VENUE,
  VENUE_NAME,
  type VenueInfo,
} from '../data/venue'
import type { Settings, ThemeName } from './settings'

/** Everything the app derives from the provider in use (fixed for the page's lifetime). */
export interface ProviderProfile {
  id: ProviderId
  /** Talks to the exchange from the browser: no drawings sync, no alerts. */
  direct: boolean
  /** The venue known up front (direct providers); null until the local server reports its own. */
  initialVenue: VenueInfo | null
  /** Source name inside chart error messages, e.g. "Coinbase" or "the local server". */
  errorSource: string
  /** Shown when the persisted symbol is not listed by this provider. */
  defaultSymbol: string
}

export function providerProfile(id: ProviderId): ProviderProfile {
  switch (id) {
    case 'server':
      return {
        id,
        direct: false,
        initialVenue: null,
        errorSource: 'the local server',
        defaultSymbol: 'BTCUSDT',
      }
    case 'binance':
      return {
        id,
        direct: true,
        initialVenue: DIRECT_VENUE,
        errorSource: VENUE_NAME,
        defaultSymbol: 'BTCUSDT',
      }
    case 'coinbase':
      return {
        id,
        direct: true,
        initialVenue: COINBASE_VENUE,
        errorSource: COINBASE_VENUE.name,
        defaultSymbol: 'BTCUSD',
      }
    default: {
      const unknown: never = id
      throw new Error(`Unknown provider: ${String(unknown)}`)
    }
  }
}

/** A provider switch takes effect on reload (the provider is fixed for the page's lifetime). */
export function providerNeedsReload(active: ProviderId, next: Settings): boolean {
  return next.provider !== active
}

/** Tooltip / notice for the Alert button when alerts cannot be created. */
export function alertsUnavailableReason(
  id: ProviderId,
  status: AlertsStatus,
): string | null {
  if (status === 'ready') return null
  if (status === 'loading') return 'Connecting to the local server…'
  if (isDirectProvider(id)) {
    return `Alerts need the local server. The data provider is ${PROVIDER_LABELS[id]}; change it in Settings.`
  }
  return 'Alerts need the local server, which cannot be reached'
}

/**
 * Lab backtests (the Binance global spot archive) may be drawn on the chart only when the chart's
 * runtime venue is that archive's venue. Decided on the venue the data actually comes from, not
 * the provider id: the local server can run on Binance.US (`TV_MARKET_VENUE=binance-us`), and
 * Coinbase is a different market, so markers and equity would only pretend to line up. While
 * the server has not reported its venue (null) nothing is drawn.
 */
export function labOnChart(venue: VenueInfo | null): boolean {
  return venue !== null && venue.id === ARCHIVE_VENUE_ID
}

/**
 * Notice for "Show on chart" / a research candidate when `labOnChart(venue)` is false. Names the
 * chart's venue and the way out: Settings for a direct provider, the server's venue otherwise.
 */
export function labOverlayBlockedNotice(venue: VenueInfo | null, direct: boolean): string {
  if (venue === null) {
    return 'The local server has not reported its market venue yet, so backtest trades are not drawn on the chart. Try again in a moment.'
  }
  const lead = `Backtests use Binance spot data, but the chart shows ${venue.name} prices, so trades are not drawn on it.`
  return direct
    ? `${lead} Switch the data provider to Binance in Settings to see them on the chart.`
    : `${lead} Run the local server on Binance (TV_MARKET_VENUE=binance) or switch the data provider to Binance in Settings to see them on the chart.`
}

/**
 * The symbol to switch to when `current` is not listed by the active provider: its default when
 * listed, else the first listed symbol. Null when `current` is listed, the list is empty, or the
 * choice would be `current` itself (so a caller can never loop).
 */
export function fallbackSymbol(
  current: string,
  listed: readonly string[],
  defaultSymbol: string,
): string | null {
  if (listed.length === 0 || listed.includes(current)) return null
  const next = listed.includes(defaultSymbol) ? defaultSymbol : listed[0]
  return next === current ? null : next
}

export function symbolFallbackNotice(from: string, to: string, venueName: string): string {
  return `${from} is not listed on ${venueName}. Showing ${to} instead.`
}

export function toggledTheme(theme: ThemeName): ThemeName {
  return theme === 'dark' ? 'light' : 'dark'
}

/** "14:08:31" of an epoch-ms instant in the display zone, for "last update" stamps. */
export function formatClock(ms: number, tz: string): string {
  const text = formatDateTime(ms / 1000, tz, { seconds: true })
  const space = text.indexOf(' ')
  return space < 0 ? text : text.slice(space + 1)
}

/** Shortcut hints for tooltips, in the platform's notation ("⌥S" on macOS, "Alt+S" else). */
export function shortcutHint(isMac: boolean, kind: 'screenshot' | 'theme' | 'help'): string {
  switch (kind) {
    case 'screenshot':
      return isMac ? '⌥S' : 'Alt+S'
    case 'theme':
      return isMac ? '⌥⇧L' : 'Alt+Shift+L'
    case 'help':
      return '?'
  }
}
