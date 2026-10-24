import type { Candle } from '../types'
import type { PriceSource } from './types'

export const PRICE_SOURCES: readonly PriceSource[] = [
  'close',
  'open',
  'high',
  'low',
  'hl2',
  'hlc3',
  'ohlc4',
]

/** The price `source` reads from `c` (TradingView definitions of hl2, hlc3, ohlc4). */
export function priceOf(c: Candle, source: PriceSource): number {
  switch (source) {
    case 'close':
      return c.close
    case 'open':
      return c.open
    case 'high':
      return c.high
    case 'low':
      return c.low
    case 'hl2':
      return (c.high + c.low) / 2
    case 'hlc3':
      return (c.high + c.low + c.close) / 3
    case 'ohlc4':
      return (c.open + c.high + c.low + c.close) / 4
  }
}
