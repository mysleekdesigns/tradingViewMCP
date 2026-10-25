// Which market-data provider the app uses. Chosen in Settings and applied on the next load.

/**
 * - server: Binance global spot through the local server (cache, drawings, alerts)
 * - binance: Binance global spot straight from the browser (no server features)
 * - coinbase: Coinbase Exchange straight from the browser (no server features)
 */
export type ProviderId = 'server' | 'binance' | 'coinbase'

export const PROVIDER_IDS: readonly ProviderId[] = ['server', 'binance', 'coinbase']

export const PROVIDER_LABELS: Readonly<Record<ProviderId, string>> = {
  server: 'Binance · local server',
  binance: 'Binance (direct)',
  coinbase: 'Coinbase Exchange (direct)',
}

export function isProviderId(v: unknown): v is ProviderId {
  return typeof v === 'string' && (PROVIDER_IDS as readonly string[]).includes(v)
}

/** Direct providers talk to the exchange from the browser; server-only features are off. */
export function isDirectProvider(id: ProviderId): boolean {
  return id !== 'server'
}
