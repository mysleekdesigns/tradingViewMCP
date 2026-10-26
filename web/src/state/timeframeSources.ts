// Where seconds candles come from for each data provider (see createTimeframeProvider).
import type { ProviderId } from '../data/providerIds'

/**
 * - 'provider': the provider's own one-second klines (BinanceProvider.secondsSource()).
 * - 'binance-direct': Binance's public 1s klines straight from the browser
 *   (createBinanceSecondsSource), used with the local server when it serves Binance global: the
 *   server has no 1s cache, so seconds charts bypass it (no cache, no fan-out).
 * - null: no seconds timeframes (Coinbase, Binance.US, or a server venue not known yet).
 */
export type SecondsSourceKind = 'provider' | 'binance-direct' | null

export function secondsSourceKind(
  providerId: ProviderId,
  /** The venue the chart data comes from (VenueInfo.id), null while unknown. */
  venueId: string | null,
): SecondsSourceKind {
  switch (providerId) {
    case 'binance':
      return 'provider'
    case 'server':
      return venueId === 'binance' ? 'binance-direct' : null
    case 'coinbase':
      return null
    default: {
      const unknown: never = providerId
      throw new Error(`Unknown provider: ${String(unknown)}`)
    }
  }
}
