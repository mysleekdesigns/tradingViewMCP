// Phase 19: TickersController hands its watched set to the provider (so the stream can be
// narrowed before parsing), and the watchlist formatters reuse cached Intl.NumberFormat objects.
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { MarketDataProvider, TickerSubscribeOptions } from '../data/provider'
import type { Ticker } from '../types'
import { formatTickerChange, formatTickerPrice, TickersController } from './tickers'

const tk = (symbol: string, last: number, time = 1_000): Ticker => ({
  symbol,
  last,
  open: 100,
  high: Math.max(last, 100),
  low: Math.min(last, 100),
  volume: 10,
  quoteVolume: 1_000,
  changePct: last - 100,
  time,
})

class WatchingProvider implements MarketDataProvider {
  opts: TickerSubscribeOptions | undefined
  subscriptions = 0
  unsubscribes = 0
  onTickers: ((t: Ticker[]) => void) | null = null
  changes = 0
  offs = 0
  getCandles(): Promise<never> {
    return Promise.reject(new Error('unused'))
  }
  subscribeCandles(): () => void {
    throw new Error('unused')
  }
  getSymbols(): Promise<never> {
    return Promise.reject(new Error('unused'))
  }
  getTickers(): Promise<Ticker[]> {
    return Promise.resolve([])
  }
  subscribeTickers(onTickers: (t: Ticker[]) => void, opts?: TickerSubscribeOptions): () => void {
    this.subscriptions++
    this.opts = opts
    this.onTickers = onTickers
    const off = opts?.onSymbolsChange?.(() => {
      this.changes++
    })
    return () => {
      this.unsubscribes++
      off?.()
      this.offs++
    }
  }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('TickersController watched set', () => {
  it('passes a getter that follows setSymbols, on one socket', () => {
    const p = new WatchingProvider()
    const c = new TickersController({ provider: p, throttleMs: 0 }, () => {})
    c.setSymbols(['BTCUSDT'])
    c.start()
    const get = p.opts?.symbols
    expect(get).toBeDefined()
    const first = get!()
    expect([...first]).toEqual(['BTCUSDT'])
    c.setSymbols(['BTCUSDT', 'ETHUSDT'])
    const second = get!()
    expect([...second].sort()).toEqual(['BTCUSDT', 'ETHUSDT'])
    // A change gives a new Set (identity marks the change); no change keeps it.
    expect(second).not.toBe(first)
    c.setSymbols(['ETHUSDT', 'BTCUSDT'])
    expect(get!()).toBe(second)
    expect(p.subscriptions).toBe(1)
    expect(p.unsubscribes).toBe(0)
    c.stop()
  })

  it('notifies the provider after each real change, and never after stop', () => {
    const p = new WatchingProvider()
    const c = new TickersController({ provider: p, throttleMs: 0 }, () => {})
    c.start()
    c.setSymbols(['BTCUSDT'])
    c.setSymbols(['BTCUSDT'])
    c.setSymbols(['ETHUSDT'])
    expect(p.changes).toBe(2)
    c.stop()
    expect(p.offs).toBe(1)
    c.setSymbols(['SOLUSDT'])
    expect(p.changes).toBe(2)
  })

  it('a throwing provider listener does not break setSymbols', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const p = new WatchingProvider()
    p.subscribeTickers = (onTickers, opts) => {
      p.onTickers = onTickers
      p.opts = opts
      opts?.onSymbolsChange?.(() => {
        throw new Error('boom')
      })
      return () => {}
    }
    const snaps: number[] = []
    const c = new TickersController({ provider: p, throttleMs: 0 }, (s) =>
      snaps.push(s.tickers.size),
    )
    c.start()
    expect(() => c.setSymbols(['BTCUSDT'])).not.toThrow()
    p.onTickers?.([tk('BTCUSDT', 101)])
    expect(snaps.at(-1)).toBe(1)
    c.stop()
  })

  it('still ignores unwatched symbols a provider delivers anyway', () => {
    const p = new WatchingProvider()
    let last = 0
    const c = new TickersController({ provider: p, throttleMs: 0 }, (s) => (last = s.tickers.size))
    c.setSymbols(['BTCUSDT'])
    c.start()
    p.onTickers?.([tk('BTCUSDT', 101), tk('ETHUSDT', 99)])
    expect(last).toBe(1)
    c.stop()
  })
})

describe('cached watchlist formatters', () => {
  const opts = (d: number) => ({ minimumFractionDigits: d, maximumFractionDigits: d })
  const values = [
    0, -0, 1, -1, 0.5, 0.005, 0.015, 1.005, 2.675, 9.995, 12.3456789, 999.9999, 1_234.5,
    -1_234.5678, 80_266.19, 1_234_567.891, 0.00001234, 1e-9, 123_456_789_012.3456,
  ]

  it('formatTickerPrice is identical to toLocaleString for every tick size', () => {
    for (const tick of [undefined, 1, 0.1, 0.01, 0.001, 0.0001, 1e-6, 1e-8, 0.5, 0.25]) {
      for (const v of values) {
        const out = formatTickerPrice(v, tick)
        const d = out.includes('.') ? out.split('.')[1].length : 0
        expect(out).toBe(v.toLocaleString('en-US', opts(d)))
      }
    }
    expect(formatTickerPrice(80_266.19, 0.01)).toBe('80,266.19')
    expect(formatTickerPrice(Number.NaN)).toBe('—')
  })

  it('formatTickerChange is identical to the toLocaleString version', () => {
    const reference = (last: number, open: number, decimals: number): string => {
      const diff = Number((last - open).toFixed(decimals))
      if (diff === 0) return (0).toFixed(decimals)
      const text = Math.abs(diff).toLocaleString('en-US', opts(decimals))
      return `${diff > 0 ? '+' : '-'}${text}`
    }
    for (const [tick, d] of [
      [0.01, 2],
      [0.0001, 4],
      [1, 0],
      [1e-8, 8],
    ] as const) {
      for (const last of values) {
        for (const open of [0, 1, 1_000, 80_000.5]) {
          expect(formatTickerChange(last, open, tick)).toBe(reference(last, open, d))
        }
      }
    }
  })

  it('builds one NumberFormat per decimal count, not one per call', () => {
    const spy = vi.spyOn(Intl, 'NumberFormat')
    // Decimal counts not used by the tests above (7 and 9), so the cache is cold for them.
    for (let i = 0; i < 50; i++) {
      formatTickerPrice(1 + i, 1e-7)
      formatTickerChange(2 + i, 1, 1e-7)
      formatTickerPrice(1 + i, 3e-9)
    }
    expect(formatTickerPrice(1, 1e-7)).toBe('1.0000000')
    expect(formatTickerPrice(1, 3e-9)).toBe('1.000000000')
    expect(spy).toHaveBeenCalledTimes(2)
  })
})
