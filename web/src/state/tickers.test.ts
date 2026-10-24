import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MarketDataError, type MarketDataProvider, type SubscribeOptions } from '../data/provider'
import type { ConnectionStatus, Ticker } from '../types'
import {
  formatTickerChange,
  formatTickerPct,
  formatTickerPrice,
  tickDecimals,
  tickerDirection,
  TickersController,
  type TickersSnapshot,
} from './tickers'

function tk(symbol: string, last: number, time = 1_000, open = 100): Ticker {
  return {
    symbol,
    last,
    open,
    high: Math.max(last, open),
    low: Math.min(last, open),
    volume: 10,
    quoteVolume: 1_000,
    changePct: open === 0 ? 0 : ((last - open) / open) * 100,
    time,
  }
}

interface SeedRequest {
  symbols: readonly string[]
  resolve: (t: Ticker[]) => void
  reject: (e: unknown) => void
  signal: AbortSignal | undefined
}

class FakeProvider implements MarketDataProvider {
  seeds: SeedRequest[] = []
  subscriptions = 0
  unsubscribes = 0
  private onTickers: ((t: Ticker[]) => void) | null = null
  private onStatus: ((s: ConnectionStatus) => void) | null = null

  getCandles(): Promise<never> {
    return Promise.reject(new Error('unused'))
  }
  subscribeCandles(): () => void {
    throw new Error('unused')
  }
  getSymbols(): Promise<never> {
    return Promise.reject(new Error('unused'))
  }
  getTickers(symbols: readonly string[], opts?: { signal?: AbortSignal }): Promise<Ticker[]> {
    return new Promise((resolve, reject) => {
      this.seeds.push({ symbols: [...symbols], resolve, reject, signal: opts?.signal })
    })
  }
  subscribeTickers(onTickers: (t: Ticker[]) => void, opts?: SubscribeOptions): () => void {
    this.subscriptions++
    this.onTickers = onTickers
    this.onStatus = opts?.onStatus ?? null
    return () => {
      this.unsubscribes++
      this.onTickers = null
      this.onStatus = null
    }
  }

  push(tickers: Ticker[]): void {
    this.onTickers?.(tickers)
  }
  status(s: ConnectionStatus): void {
    this.onStatus?.(s)
  }
}

async function settle(): Promise<void> {
  await Promise.resolve()
  await Promise.resolve()
}

describe('TickersController', () => {
  let provider: FakeProvider
  let emits: TickersSnapshot[]
  let controller: TickersController

  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(1_000_000)
    provider = new FakeProvider()
    emits = []
    controller = new TickersController({ provider, throttleMs: 250 }, (s) => emits.push(s))
  })

  afterEach(() => {
    controller.stop()
    vi.useRealTimers()
  })

  const last = () => emits[emits.length - 1]

  it('subscribes once and seeds the watched symbols over REST', async () => {
    controller.setSymbols(['BTCUSDT', 'ETHUSDT'])
    controller.start()
    expect(provider.subscriptions).toBe(1)
    expect(provider.seeds).toHaveLength(1)
    expect(provider.seeds[0].symbols).toEqual(['BTCUSDT', 'ETHUSDT'])
    provider.seeds[0].resolve([tk('BTCUSDT', 101), tk('ETHUSDT', 99)])
    await settle()
    expect(last().tickers.get('BTCUSDT')?.last).toBe(101)
    expect(last().tickers.get('ETHUSDT')?.last).toBe(99)
  })

  it('keeps only watched symbols from the all-market stream', async () => {
    controller.setSymbols(['BTCUSDT'])
    controller.start()
    provider.push([tk('BTCUSDT', 105, 2_000), tk('DOGEUSDT', 1, 2_000)])
    expect(last().tickers.size).toBe(1)
    expect(last().tickers.has('DOGEUSDT')).toBe(false)
    const count = emits.length
    vi.advanceTimersByTime(1_000)
    provider.push([tk('DOGEUSDT', 2, 3_000)]) // unwatched only: no emit
    vi.advanceTimersByTime(1_000)
    expect(emits.length).toBe(count)
    await settle()
  })

  it('throttles data emits to at most one per window and flushes the latest', () => {
    controller.setSymbols(['BTCUSDT'])
    controller.start()
    const base = emits.length
    provider.push([tk('BTCUSDT', 101, 2)]) // leading emit
    expect(emits.length).toBe(base + 1)
    provider.push([tk('BTCUSDT', 102, 3)])
    provider.push([tk('BTCUSDT', 103, 4)])
    provider.push([tk('BTCUSDT', 104, 5)])
    expect(emits.length).toBe(base + 1)
    vi.advanceTimersByTime(249)
    expect(emits.length).toBe(base + 1)
    vi.advanceTimersByTime(1)
    expect(emits.length).toBe(base + 2)
    expect(last().tickers.get('BTCUSDT')?.last).toBe(104)
    // 1 s of 20 pushes/s → at most 4 emits + the leading one.
    const before = emits.length
    for (let i = 0; i < 20; i++) {
      vi.advanceTimersByTime(50)
      provider.push([tk('BTCUSDT', 200 + i, 10 + i)])
    }
    vi.advanceTimersByTime(250)
    expect(emits.length - before).toBeLessThanOrEqual(5)
    expect(last().tickers.get('BTCUSDT')?.last).toBe(219)
  })

  it('emits immutable snapshots (new Map per data emit)', () => {
    controller.setSymbols(['BTCUSDT'])
    controller.start()
    provider.push([tk('BTCUSDT', 101, 2)])
    const first = last().tickers
    vi.advanceTimersByTime(300)
    provider.push([tk('BTCUSDT', 102, 3)])
    expect(last().tickers).not.toBe(first)
    expect(first.get('BTCUSDT')?.last).toBe(101)
  })

  it('tracks the direction of the last price move', () => {
    controller.setSymbols(['BTCUSDT'])
    controller.start()
    provider.push([tk('BTCUSDT', 101, 2)])
    expect(last().moves.has('BTCUSDT')).toBe(false)
    vi.advanceTimersByTime(300)
    provider.push([tk('BTCUSDT', 100, 3)])
    expect(last().moves.get('BTCUSDT')).toBe('down')
    vi.advanceTimersByTime(300)
    provider.push([tk('BTCUSDT', 102, 4)])
    expect(last().moves.get('BTCUSDT')).toBe('up')
  })

  it('never lets an older REST seed overwrite a newer stream ticker', async () => {
    controller.setSymbols(['BTCUSDT'])
    controller.start()
    provider.push([tk('BTCUSDT', 110, 5_000)])
    provider.seeds[0].resolve([tk('BTCUSDT', 100, 4_000)])
    await settle()
    vi.advanceTimersByTime(300)
    expect(last().tickers.get('BTCUSDT')?.last).toBe(110)
  })

  it('reports connection status at once, unthrottled', () => {
    controller.start()
    provider.push([]) // nothing
    provider.status('live')
    expect(last().status).toBe('live')
    provider.status('reconnecting')
    expect(last().status).toBe('reconnecting')
  })

  it('seeds only added symbols and drops removed ones without touching the socket', async () => {
    controller.setSymbols(['BTCUSDT', 'ETHUSDT'])
    controller.start()
    provider.seeds[0].resolve([tk('BTCUSDT', 101), tk('ETHUSDT', 99)])
    await settle()
    controller.setSymbols(['BTCUSDT', 'ETHUSDT', 'SOLUSDT'])
    expect(provider.seeds).toHaveLength(2)
    expect(provider.seeds[1].symbols).toEqual(['SOLUSDT'])
    controller.setSymbols(['ETHUSDT', 'BTCUSDT', 'SOLUSDT']) // reorder only: nothing
    expect(provider.seeds).toHaveLength(2)
    controller.setSymbols(['BTCUSDT', 'SOLUSDT'])
    vi.advanceTimersByTime(300)
    expect(last().tickers.has('ETHUSDT')).toBe(false)
    // A late seed row for a symbol no longer watched is ignored.
    provider.seeds[1].resolve([tk('SOLUSDT', 20), tk('ETHUSDT', 1)])
    await settle()
    vi.advanceTimersByTime(300)
    expect(last().tickers.get('SOLUSDT')?.last).toBe(20)
    expect(last().tickers.has('ETHUSDT')).toBe(false)
    expect(provider.subscriptions).toBe(1)
    expect(provider.unsubscribes).toBe(0)
  })

  it('re-seeds every watched symbol after a reconnect, not on the first live', async () => {
    controller.setSymbols(['BTCUSDT', 'ETHUSDT'])
    controller.start()
    provider.status('live')
    expect(provider.seeds).toHaveLength(1)
    provider.status('reconnecting')
    provider.status('live')
    expect(provider.seeds).toHaveLength(2)
    expect(provider.seeds[0].signal?.aborted).toBe(true) // superseded
    expect(provider.seeds[1].symbols).toEqual(['BTCUSDT', 'ETHUSDT'])
    provider.status('offline')
    provider.status('live')
    expect(provider.seeds).toHaveLength(3)
    await settle()
  })

  it('retries a failed seed for still-missing symbols, honouring Retry-After', async () => {
    controller.setSymbols(['BTCUSDT', 'ETHUSDT'])
    controller.start()
    provider.push([tk('BTCUSDT', 101, 2)])
    provider.seeds[0].reject(new MarketDataError('rate-limited', '429', 429, 30))
    await settle()
    vi.advanceTimersByTime(29_000)
    expect(provider.seeds).toHaveLength(1)
    vi.advanceTimersByTime(1_000)
    expect(provider.seeds).toHaveLength(2)
    expect(provider.seeds[1].symbols).toEqual(['ETHUSDT'])
  })

  it('gives up seeding after the retry budget', async () => {
    controller.setSymbols(['BTCUSDT'])
    controller.start()
    for (let i = 0; i < 4; i++) {
      provider.seeds[i].reject(new MarketDataError('network', 'down'))
      await settle()
      vi.advanceTimersByTime(60_000)
    }
    expect(provider.seeds).toHaveLength(4)
  })

  it('stop() unsubscribes, aborts seeds, and never emits again', async () => {
    controller.setSymbols(['BTCUSDT'])
    controller.start()
    provider.push([tk('BTCUSDT', 101, 2)])
    provider.push([tk('BTCUSDT', 102, 3)]) // pending trailing emit
    const seed = provider.seeds[0]
    const count = emits.length
    controller.stop()
    controller.stop()
    expect(provider.unsubscribes).toBe(1)
    expect(seed.signal?.aborted).toBe(true)
    seed.resolve([tk('BTCUSDT', 999, 9)])
    await settle()
    vi.advanceTimersByTime(10_000)
    controller.setSymbols(['ETHUSDT'])
    controller.start()
    expect(emits.length).toBe(count)
    expect(provider.subscriptions).toBe(1)
  })

  it('reports offline when subscribing throws', () => {
    const broken = new FakeProvider()
    broken.subscribeTickers = () => {
      throw new Error('no WebSocket')
    }
    const out: TickersSnapshot[] = []
    const c = new TickersController({ provider: broken }, (s) => out.push(s))
    c.start()
    expect(out[out.length - 1].status).toBe('offline')
    c.stop()
  })
})

describe('ticker formatting', () => {
  it('derives decimals from tick size', () => {
    expect(tickDecimals(0.01)).toBe(2)
    expect(tickDecimals(1)).toBe(0)
    expect(tickDecimals(0.00001)).toBe(5)
    expect(tickDecimals(1e-8)).toBe(8)
    expect(tickDecimals(0.1)).toBe(1)
    expect(tickDecimals(0)).toBeNull()
    expect(tickDecimals(Number.NaN)).toBeNull()
  })
  it('formats prices with tick decimals and separators', () => {
    expect(formatTickerPrice(67123.4, 0.01)).toBe('67,123.40')
    expect(formatTickerPrice(0.123456, 0.0001)).toBe('0.1235')
    expect(formatTickerPrice(Number.NaN, 0.01)).toBe('—')
  })
  it('falls back to magnitude-based decimals', () => {
    expect(formatTickerPrice(67123.4)).toBe('67,123.40')
    expect(formatTickerPrice(2.5)).toBe('2.5000')
    expect(formatTickerPrice(0.00001234)).toBe('0.00001234')
  })
  it('formats signed change and never -0', () => {
    expect(formatTickerChange(105, 100, 0.01)).toBe('+5.00')
    expect(formatTickerChange(1095, 2100, 0.01)).toBe('-1,005.00')
    expect(formatTickerChange(100.001, 100.004, 0.01)).toBe('0.00')
  })
  it('formats percent and direction', () => {
    expect(formatTickerPct(1.234)).toBe('+1.23%')
    expect(formatTickerPct(-0.5)).toBe('-0.50%')
    expect(formatTickerPct(-0.001)).toBe('0.00%')
    expect(tickerDirection(1.2)).toBe('up')
    expect(tickerDirection(-0.2)).toBe('down')
    expect(tickerDirection(-0.001)).toBe('flat')
  })
})
