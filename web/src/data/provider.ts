import type { Candle, ConnectionStatus, Interval, SymbolInfo, Ticker } from '../types'
import type { VenueInfo } from './venue'

export interface SubscribeOptions {
  /** Connection state changes. Never called after unsubscribe. */
  onStatus?: (status: ConnectionStatus) => void
}

/**
 * Source of public market data. Every exchange-specific detail (hosts, symbol
 * formats, string prices, ms timestamps) stays behind this interface.
 */
export interface MarketDataProvider {
  /**
   * Historical candles, ascending by time with unique times. `endTime` is UNIX
   * seconds. The last row may still be forming. Aborting `signal` rejects with
   * kind 'aborted'.
   */
  getCandles(
    symbol: string,
    interval: Interval,
    limit?: number,
    endTime?: number,
    opts?: { signal?: AbortSignal },
  ): Promise<Candle[]>
  /**
   * Live candle updates; `closed` is true once the bar has ended. The provider
   * reconnects by itself (with backoff) and reports state through
   * `opts.onStatus`; after a 'live' that follows 'reconnecting'/'offline' the
   * caller should re-fetch recent candles via `getCandles` to fill the gap.
   * Returns an unsubscribe function that tears everything down (idempotent).
   *
   * `provisional` (optional, only ever passed as `true`, only with `closed`): the
   * provider will send this bar closed once more, possibly with corrected values
   * (Coinbase's second REST read-back), so its values are not final yet. That later
   * send carries no flag and settles the bar. Providers whose closes are final
   * (Binance, the server) never pass it; consumers that only need "the bar ended"
   * can ignore it and treat a repeated closed bar as a replacement.
   */
  subscribeCandles(
    symbol: string,
    interval: Interval,
    onCandle: (c: Candle, closed: boolean, provisional?: boolean) => void,
    opts?: SubscribeOptions,
  ): () => void
  /**
   * Every spot symbol the exchange lists, unfiltered (callers filter by `status` and
   * quote asset). Fetched once per provider instance and cached; a failed fetch is not
   * cached, so the next call retries.
   */
  getSymbols(opts?: { signal?: AbortSignal }): Promise<SymbolInfo[]>
  /**
   * The venue the data comes from (e.g. `{ id: 'binance', name: 'Binance' }`), for labels.
   * Optional: providers that cannot tell leave it out.
   */
  getVenue?(opts?: { signal?: AbortSignal }): Promise<VenueInfo>
  /**
   * Rolling 24h stats for the given symbols over REST, used to seed the watchlist before
   * the ticker stream delivers. Unknown symbols are omitted from the result. An empty
   * `symbols` resolves to [] without a request.
   */
  getTickers(symbols: readonly string[], opts?: { signal?: AbortSignal }): Promise<Ticker[]>
  /**
   * Live 24h stats for all symbols over ONE socket (Binance `!miniTicker@arr`). Each call
   * of `onTickers` carries only the symbols that changed since the last push. Same
   * reconnect/backoff/stale/status behaviour as `subscribeCandles`. Returns an idempotent
   * unsubscribe that closes the socket.
   */
  subscribeTickers(onTickers: (tickers: Ticker[]) => void, opts?: SubscribeOptions): () => void
}

/**
 * - network: request failed or timed out
 * - rate-limited: HTTP 429; wait `retryAfter` seconds before calling again
 * - banned: HTTP 418 (IP ban); stop calling until `retryAfter` has passed
 * - region-blocked: HTTP 451; this provider is unavailable from this location
 * - aborted: the caller cancelled the request
 */
export type MarketDataErrorKind =
  | 'network'
  | 'bad-symbol'
  | 'rate-limited'
  | 'banned'
  | 'region-blocked'
  | 'aborted'
  | 'http'
  | 'parse'

export class MarketDataError extends Error {
  readonly kind: MarketDataErrorKind
  readonly status?: number
  /** Seconds to wait before retrying (from `Retry-After`), when known. */
  readonly retryAfter?: number

  constructor(kind: MarketDataErrorKind, message: string, status?: number, retryAfter?: number) {
    super(message)
    this.name = 'MarketDataError'
    this.kind = kind
    this.status = status
    this.retryAfter = retryAfter
  }
}
