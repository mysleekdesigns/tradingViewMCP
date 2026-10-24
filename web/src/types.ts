export type Interval = '1m' | '5m' | '15m' | '1h' | '4h' | '1d' | '1w'

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
