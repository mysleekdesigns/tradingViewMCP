import type { Candle, Interval, SymbolInfo } from '../types'

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
  /** Live candle updates; `closed` is true once the bar is final. Returns an unsubscribe function. */
  subscribeCandles(
    symbol: string,
    interval: Interval,
    onCandle: (c: Candle, closed: boolean) => void,
  ): () => void
  getSymbols(): Promise<SymbolInfo[]>
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
