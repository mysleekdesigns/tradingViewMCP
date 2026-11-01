// Phase 19 Wave 5: the hooks moved onto useController keep their lifecycles. Driven through
// the hook harness (no DOM): one controller per key, stopped on a key change and on unmount,
// snapshots shown only for the current key, and callbacks reaching the running controller.
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Drawing } from '../chart/drawings/types'
import type { MarketDataProvider, TickerSubscribeOptions } from '../data/provider'
import type { Ticker } from '../types'
import { UNAVAILABLE_ALERTS_SNAPSHOT, useAlerts } from './alerts'
import { useDrawings } from './drawings'
import { mountHook } from './hookHarness.testkit'
import { initialTickersSnapshot } from './tickers'
import { useTickers } from './useTickers'

// The harness is imported inside the (hoisted) factory, before the hooks under test load.
vi.mock('react', async (orig) => ({
  ...(await orig<typeof import('react')>()),
  ...(await import('./hookHarness.testkit')).fakeReactHooks,
}))

afterEach(() => {
  vi.useRealTimers()
})

class FakeProvider implements MarketDataProvider {
  subscribes = 0
  unsubscribes = 0
  seeded: string[][] = []
  push: ((t: Ticker[]) => void) | null = null
  opts: TickerSubscribeOptions | undefined
  getCandles(): Promise<never> {
    return Promise.reject(new Error('unused'))
  }
  subscribeCandles(): () => void {
    throw new Error('unused')
  }
  getSymbols(): Promise<never> {
    return Promise.reject(new Error('unused'))
  }
  getTickers(symbols: readonly string[]): Promise<Ticker[]> {
    this.seeded.push([...symbols])
    return new Promise<never>(() => {})
  }
  subscribeTickers(onTickers: (t: Ticker[]) => void, opts?: TickerSubscribeOptions): () => void {
    this.subscribes++
    this.push = onTickers
    this.opts = opts
    return () => {
      this.unsubscribes++
    }
  }
}

const tk = (symbol: string, last: number): Ticker => ({
  symbol,
  last,
  open: 100,
  high: Math.max(last, 100),
  low: Math.min(last, 100),
  volume: 10,
  quoteVolume: 1_000,
  changePct: last - 100,
  time: 1_000,
})

describe('useTickers on useController', () => {
  type P = { provider: MarketDataProvider; items: readonly string[] }
  const useProbe = ({ provider, items }: P) => useTickers(provider, items)

  it('one socket per provider; items edits re-seed without reconnecting', () => {
    const p = new FakeProvider()
    const h = mountHook(useProbe, { provider: p, items: ['BTCUSDT'] })
    expect(h.result).toBe(initialTickersSnapshot)
    expect(p.subscribes).toBe(1)
    expect(p.seeded).toEqual([['BTCUSDT']])
    h.rerender({ provider: p, items: ['BTCUSDT', 'ETHUSDT'] })
    expect(p.subscribes).toBe(1)
    expect(p.seeded).toEqual([['BTCUSDT'], ['ETHUSDT']])
    expect([...(p.opts?.symbols?.() ?? [])]).toEqual(['BTCUSDT', 'ETHUSDT'])
    h.unmount()
    expect(p.unsubscribes).toBe(1)
  })

  it('a provider change stops the old socket and hides its snapshot at once', () => {
    vi.useFakeTimers()
    const a = new FakeProvider()
    const b = new FakeProvider()
    const h = mountHook(useProbe, { provider: a, items: ['BTCUSDT'] })
    a.push?.([tk('BTCUSDT', 101)])
    vi.runAllTimers()
    h.flush()
    expect(h.result.tickers.get('BTCUSDT')?.last).toBe(101)

    h.rerender({ provider: b, items: ['BTCUSDT'] })
    expect(a.unsubscribes).toBe(1)
    expect(b.subscribes).toBe(1)
    expect(b.seeded).toEqual([['BTCUSDT']])
    expect(h.result).toBe(initialTickersSnapshot)
    // The stopped controller's late pushes never land.
    a.push?.([tk('BTCUSDT', 150)])
    vi.runAllTimers()
    h.flush()
    expect(h.result).toBe(initialTickersSnapshot)
  })
})

function hline(id: string, symbol: string): Drawing {
  return {
    id,
    symbol,
    kind: 'hline',
    anchors: [{ time: 1_700_000_000, price: 100 }],
    style: { color: '#2962ff', lineWidth: 2, lineStyle: 'solid' },
  }
}

describe('useDrawings on useController', () => {
  type P = { symbol: string }
  const useProbe = ({ symbol }: P) => useDrawings(symbol, null)

  it('edits reach the running store; a symbol change shows the new mirror at once', () => {
    const h = mountHook(useProbe, { symbol: 'BTCUSDT' })
    expect(h.result.items).toEqual([])
    expect(h.result.status).toBe('local-only')
    expect(h.result.add(hline('d1', 'BTCUSDT'))).toBe(true)
    h.flush()
    expect(h.result.items.map((d) => d.id)).toEqual(['d1'])

    h.rerender({ symbol: 'ETHUSDT' })
    // ETH's store has not emitted: its (empty, no storage in Node) mirror shows.
    expect(h.result.items).toEqual([])
    expect(h.result.add(hline('d2', 'ETHUSDT'))).toBe(true)
    h.flush()
    expect(h.result.items.map((d) => d.id)).toEqual(['d2'])

    h.unmount()
    expect(h.result.add(hline('d3', 'ETHUSDT'))).toBe(false)
  })

  it('StrictMode: the replayed mount leaves one live store', () => {
    const h = mountHook(useProbe, { symbol: 'BTCUSDT' }, { strict: true })
    expect(h.result.add(hline('d1', 'BTCUSDT'))).toBe(true)
    h.flush()
    expect(h.result.items.map((d) => d.id)).toEqual(['d1'])
  })
})

describe('useAlerts on useController', () => {
  it('no client: unavailable, and edits reject once unmounted', async () => {
    const h = mountHook(() => useAlerts({ client: null }), null)
    expect(h.result.status).toBe(UNAVAILABLE_ALERTS_SNAPSHOT.status)
    expect(h.result.alerts).toBe(UNAVAILABLE_ALERTS_SNAPSHOT.alerts)
    h.unmount()
    await expect(h.result.clearHistory()).rejects.toThrow('Alerts are not available right now')
  })
})
