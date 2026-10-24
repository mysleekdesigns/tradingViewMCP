// Shared market-data shapes. Mirrors web/src/types.ts; see server/README.md for the contract.
// All times are UNIX seconds (UTC).

export type Interval = '1m' | '5m' | '15m' | '1h' | '4h' | '1d' | '1w'

export const INTERVALS: readonly Interval[] = ['1m', '5m', '15m', '1h', '4h', '1d', '1w']

export const INTERVAL_SECONDS: Readonly<Record<Interval, number>> = {
  '1m': 60,
  '5m': 300,
  '15m': 900,
  '1h': 3600,
  '4h': 14400,
  '1d': 86400,
  '1w': 604800,
}

export function isInterval(v: unknown): v is Interval {
  return typeof v === 'string' && (INTERVALS as readonly string[]).includes(v)
}

/** One OHLCV bar. `time` is the bar's open time in UNIX seconds. */
export interface Candle {
  time: number
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface SymbolInfo {
  symbol: string
  baseAsset: string
  quoteAsset: string
  status: string
  tickSize: number
}

export interface Ticker {
  symbol: string
  last: number
  open: number
  high: number
  low: number
  volume: number
  quoteVolume: number
  changePct: number
  time: number
}

export type ErrorKind =
  | 'network'
  | 'bad-symbol'
  | 'rate-limited'
  | 'banned'
  | 'region-blocked'
  | 'aborted'
  | 'http'
  | 'parse'

/** An error with an HTTP status that the global error handler serialises as `{error, message, retryAfter?}`. */
export class ApiError extends Error {
  readonly kind: ErrorKind
  readonly status: number
  /** Seconds the client should wait before retrying (sent as `Retry-After`). */
  readonly retryAfter?: number

  constructor(kind: ErrorKind, message: string, status: number, retryAfter?: number) {
    super(message)
    this.name = 'ApiError'
    this.kind = kind
    this.status = status
    if (retryAfter !== undefined) this.retryAfter = retryAfter
  }
}
