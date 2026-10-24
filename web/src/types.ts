export type Interval = '1m' | '5m' | '15m' | '1h' | '4h' | '1d' | '1w'

/** Every supported interval, shortest first (toolbar order). */
export const INTERVALS: readonly Interval[] = ['1m', '5m', '15m', '1h', '4h', '1d', '1w']

/** How the main series draws candles. */
export type ChartType = 'candles' | 'hollow' | 'line' | 'area' | 'heikin-ashi'

export const CHART_TYPES: readonly ChartType[] = [
  'candles',
  'hollow',
  'line',
  'area',
  'heikin-ashi',
]

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

/** Live-stream connection state reported by `subscribeCandles`. */
export type ConnectionStatus = 'connecting' | 'live' | 'reconnecting' | 'offline'

/** Rolling 24h statistics for one symbol (watchlist prices). */
export interface Ticker {
  symbol: string
  /** Last traded price. */
  last: number
  /** Price at the start of the rolling 24h window. */
  open: number
  high: number
  low: number
  /** Base-asset volume over the window. */
  volume: number
  /** Quote-asset volume over the window. */
  quoteVolume: number
  /** (last - open) / open * 100; 0 when open is 0. */
  changePct: number
  /** Exchange event time, UNIX seconds. */
  time: number
}
