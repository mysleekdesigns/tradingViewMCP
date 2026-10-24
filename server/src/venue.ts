// Market-data venue: the one place that names the upstream hosts, plus the startup check that
// keeps the SQLite market-data cache from mixing bars from two venues.
import type { Db } from './db/index.ts'

export type VenueId = 'binance' | 'binance-us'

export interface Venue {
  id: VenueId
  /** Shown in logs and docs, e.g. "Binance". */
  name: string
  /** Public REST base, no trailing slash. */
  restBase: string
  /** Public WebSocket base, no trailing `/ws`. */
  wsBase: string
}

/**
 * Public market-data hosts only (no API keys, no signed endpoints).
 * - `binance`: Binance's market-data-only domains. Same `/api/v3` payloads and streams as
 *   Binance.com (global spot, the venue of the lab's data.binance.vision archive), reachable
 *   where api.binance.com answers 451. See
 *   https://developers.binance.com/docs/binance-spot-api-docs/faqs/market_data_only
 * - `binance-us`: the previous venue, kept selectable.
 */
export const VENUES: Readonly<Record<VenueId, Venue>> = {
  binance: {
    id: 'binance',
    name: 'Binance',
    restBase: 'https://data-api.binance.vision',
    wsBase: 'wss://data-stream.binance.vision',
  },
  'binance-us': {
    id: 'binance-us',
    name: 'Binance.US',
    restBase: 'https://api.binance.us',
    wsBase: 'wss://stream.binance.us:9443',
  },
}

export const DEFAULT_VENUE_ID: VenueId = 'binance'

export function isVenueId(value: unknown): value is VenueId {
  return typeof value === 'string' && Object.hasOwn(VENUES, value)
}

/** `kv_cache` key recording which venue the cached market data came from. */
export const VENUE_KEY = 'market_venue'

export interface VenueCheck {
  /** Venue recorded before this call; null when none was recorded. */
  previous: string | null
  /** True when the cache was cleared because the recorded venue was absent or different. */
  changed: boolean
  /** Rows deleted (all 0 when `changed` is false). */
  removed: { candles: number; ranges: number; kv: number }
}

/**
 * Makes the market-data cache belong to `venueId`. If the recorded venue differs (or none is
 * recorded), deletes every row of `candles`, `candle_ranges` and `kv_cache` (the symbol list
 * and any other cached upstream JSON) and records the venue, all in one transaction. User data
 * (watchlists, layouts, drawings, alerts, alert events) is never touched. Call once at startup,
 * before the app serves requests.
 */
export function ensureVenue(db: Db, venueId: VenueId, nowSec: number): VenueCheck {
  const select = db.prepare<[string], { value: string }>('SELECT value FROM kv_cache WHERE key = ?')
  const delCandles = db.prepare('DELETE FROM candles')
  const delRanges = db.prepare('DELETE FROM candle_ranges')
  const delKv = db.prepare('DELETE FROM kv_cache')
  const insert = db.prepare<[string, string, number]>(
    'INSERT INTO kv_cache (key, value, updated_at) VALUES (?, ?, ?)',
  )
  return db.transaction((): VenueCheck => {
    const row = select.get(VENUE_KEY)
    const previous = row === undefined ? null : row.value
    if (previous === venueId) {
      return { previous, changed: false, removed: { candles: 0, ranges: 0, kv: 0 } }
    }
    const candles = delCandles.run().changes
    const ranges = delRanges.run().changes
    // The old marker row is counted too; report only cached upstream entries.
    const kv = delKv.run().changes - (row === undefined ? 0 : 1)
    insert.run(VENUE_KEY, venueId, nowSec)
    return { previous, changed: true, removed: { candles, ranges, kv } }
  })()
}

/** User data that names symbols the current venue does not list. */
export interface OrphanReport {
  /** Symbols of active alerts, sorted, unique. */
  alerts: string[]
  /** Symbols in any watchlist, sorted, unique. */
  watchlist: string[]
}

/**
 * Active-alert and watchlist symbols missing from `listed` (the venue's symbol list). Read-only;
 * nothing is deleted, the caller only reports them.
 */
export function orphanedUserSymbols(db: Db, listed: ReadonlySet<string>): OrphanReport {
  const alertRows = db
    .prepare<[], { symbol: string }>('SELECT DISTINCT symbol FROM alerts WHERE active = 1')
    .all()
  const alerts = new Set<string>()
  for (const r of alertRows) if (!listed.has(r.symbol)) alerts.add(r.symbol)

  const watchlist = new Set<string>()
  for (const r of db.prepare<[], { items: string }>('SELECT items FROM watchlists').all()) {
    let items: unknown
    try {
      items = JSON.parse(r.items)
    } catch {
      continue
    }
    if (!Array.isArray(items)) continue
    for (const s of items) if (typeof s === 'string' && !listed.has(s)) watchlist.add(s)
  }
  return { alerts: [...alerts].sort(), watchlist: [...watchlist].sort() }
}
