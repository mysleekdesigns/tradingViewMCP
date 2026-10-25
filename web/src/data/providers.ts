// The one place that turns a ProviderId (Settings) into a MarketDataProvider.
import { BinanceProvider } from './binance'
import { CoinbaseProvider } from './coinbase'
import type { MarketDataProvider } from './provider'
import type { ProviderId } from './providerIds'
import { ServerProvider } from './server'

/** server -> ServerProvider (local server), binance -> BinanceProvider, coinbase -> CoinbaseProvider. */
export function createProvider(id: ProviderId): MarketDataProvider {
  switch (id) {
    case 'server':
      return new ServerProvider()
    case 'binance':
      return new BinanceProvider()
    case 'coinbase':
      return new CoinbaseProvider()
    default: {
      const unknown: never = id
      throw new Error(`Unknown provider: ${String(unknown)}`)
    }
  }
}
