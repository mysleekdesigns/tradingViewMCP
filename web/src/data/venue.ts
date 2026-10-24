// The market-data venue: its display name and the one place that names the exchange hosts used
// by the direct (no-server) provider. The local server has its own copy in server/src/venue.ts.

/** Shown in the header, chart legend, loading and empty states. */
export const VENUE_NAME = 'Binance'

/**
 * Binance's market-data-only hosts: the same `/api/v3` payloads and streams as Binance.com
 * (global spot, the venue of the lab's data.binance.vision archive), reachable where
 * api.binance.com answers 451. Public, no API keys. See
 * https://developers.binance.com/docs/binance-spot-api-docs/faqs/market_data_only
 */
export const BINANCE_REST = 'https://data-api.binance.vision'
export const BINANCE_WS = 'wss://data-stream.binance.vision'

/** The previous venue, still selectable via `new BinanceProvider({ baseUrl, wsBaseUrl })`. */
export const BINANCE_US_REST = 'https://api.binance.us'
export const BINANCE_US_WS = 'wss://stream.binance.us:9443'

/** The venue the chart's data comes from, as reported by the provider. */
export interface VenueInfo {
  /** `binance` (Binance global spot) or `binance-us`; other ids are shown by name only. */
  id: string
  name: string
}

/** The direct (no-server) provider's default venue. */
export const DIRECT_VENUE: VenueInfo = { id: 'binance', name: VENUE_NAME }

/** The venue of the lab's data.binance.vision archive (Binance global spot). */
export const ARCHIVE_VENUE_ID = 'binance'

/** `{ id, name }` with non-empty strings, else null. */
export function parseVenueInfo(v: unknown): VenueInfo | null {
  if (v === null || typeof v !== 'object' || Array.isArray(v)) return null
  const { id, name } = v as Record<string, unknown>
  if (typeof id !== 'string' || id.trim() === '') return null
  if (typeof name !== 'string' || name.trim() === '') return null
  return { id, name }
}

/**
 * UI labels for the data source. `exchange` names the venue ("Binance"); `source` adds the
 * transport ("Binance · local server"). Until the server reports its venue: "local server".
 */
export function venueLabels(
  venue: VenueInfo | null,
  direct: boolean,
): { exchange: string; source: string } {
  if (venue === null) return { exchange: 'the exchange', source: 'local server' }
  return { exchange: venue.name, source: direct ? venue.name : `${venue.name} · local server` }
}
