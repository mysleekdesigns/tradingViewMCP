// LiveCandlesController with bars that had no trades (Binance returns a flat, zero-volume
// kline for every interval without trades). `load.candles` keeps every raw bar; `load.bars`
// is what the chart plots.

import { describe, expect, it, vi } from 'vitest'
import { MarketDataError, type MarketDataProvider } from '../data/provider'
import type { Candle, Interval } from '../types'
import {
  atHistoryLimit,
  GAP_FILL_RETRY_DELAYS_MS,
  LiveCandlesController,
  type LiveCandlesSnapshot,
} from './liveCandles'

const M = 60

function traded(time: number, close: number, volume = 2): Candle {
  return { time, open: close - 0.5, high: close + 1, low: close - 1, close, volume }
}

function flat(time: number, price: number): Candle {
  return { time, open: price, high: price, low: price, close: price, volume: 0 }
}

interface Request {
  resolve: (c: Candle[]) => void
  reject: (e: unknown) => void
  endTime: number | undefined
}

class FakeProvider implements MarketDataProvider {
  requests: Request[] = []
  private onCandle: ((c: Candle, closed: boolean) => void) | null = null

  getCandles(_s: string, _i: Interval, _limit?: number, endTime?: number): Promise<Candle[]> {
    return new Promise((resolve, reject) => this.requests.push({ resolve, reject, endTime }))
  }

  subscribeCandles(
    _s: string,
    _i: Interval,
    onCandle: (c: Candle, closed: boolean) => void,
  ): () => void {
    this.onCandle = onCandle
    return () => (this.onCandle = null)
  }

  getSymbols(): Promise<never> {
    return Promise.reject(new Error('unused'))
  }

  getTickers(): Promise<never> {
    return Promise.reject(new Error('unused'))
  }

  subscribeTickers(): () => void {
    return () => {}
  }

  tick(c: Candle, closed = false): void {
    this.onCandle?.(c, closed)
  }

  last(): Request {
    const r = this.requests[this.requests.length - 1]
    if (!r) throw new Error('no request')
    return r
  }
}

const flush = () => new Promise<void>((r) => setTimeout(r, 0))

async function ready(initial: Candle[], maxBars?: number) {
  const provider = new FakeProvider()
  const snapshots: LiveCandlesSnapshot[] = []
  const controller = new LiveCandlesController(
    {
      provider,
      symbol: 'SUIUSDT',
      interval: '1m',
      limit: 3,
      source: 'Binance',
      maxBars,
      now: () => 1,
    },
    (s) => snapshots.push(s),
  )
  controller.start()
  provider.last().resolve(initial)
  await flush()
  const load = () => {
    const l = snapshots[snapshots.length - 1].load
    if (l.kind !== 'ready') throw new Error(`not ready: ${l.kind}`)
    return l
  }
  const times = (cs: readonly Candle[]) => cs.map((c) => c.time)
  return { provider, controller, snapshots, load, times }
}

describe('LiveCandlesController: bars without trades', () => {
  it('keeps every raw bar in candles and only traded bars in bars', async () => {
    const { load, times } = await ready([traded(0, 1), flat(M, 1), traded(2 * M, 2)])
    expect(times(load().candles)).toEqual([0, M, 2 * M])
    expect(times(load().bars)).toEqual([0, 2 * M])
  })

  it('plotted bars are the same objects as the raw ones', async () => {
    const { load } = await ready([traded(0, 1), flat(M, 1), traded(2 * M, 2)])
    expect(load().bars[1]).toBe(load().candles[2])
  })

  it('a forming bar without trades is held raw but not plotted', async () => {
    const { provider, load, times } = await ready([traded(0, 1), traded(M, 2)])
    const before = load().bars
    provider.tick(flat(2 * M, 2))
    expect(times(load().candles)).toEqual([0, M, 2 * M])
    expect(load().bars).toBe(before)
  })

  it('the forming bar appears on its first trade and then updates', async () => {
    const { provider, load, times } = await ready([traded(0, 1), traded(M, 2)])
    provider.tick(flat(2 * M, 2))
    provider.tick(traded(2 * M, 2.5, 0.1))
    expect(times(load().bars)).toEqual([0, M, 2 * M])
    provider.tick(traded(2 * M, 3, 0.4))
    expect(load().bars[2].close).toBe(3)
    provider.tick(traded(2 * M, 3, 0.4), true)
    expect(times(load().bars)).toEqual([0, M, 2 * M])
  })

  it('a bar that closes without a trade never appears', async () => {
    const { provider, load } = await ready([traded(0, 1), traded(M, 2)])
    const before = load().bars
    provider.tick(flat(2 * M, 2))
    provider.tick(flat(2 * M, 2), true)
    provider.tick(flat(3 * M, 2))
    expect(load().bars).toBe(before)
    expect(load().candles).toHaveLength(4)
  })

  it('a whole window without trades is ready with no plotted bars', async () => {
    const { load } = await ready([flat(0, 1), flat(M, 1), flat(2 * M, 1)])
    expect(load().candles).toHaveLength(3)
    expect(load().bars).toEqual([])
  })
})

describe('LiveCandlesController.loadOlder with bars without trades', () => {
  it('pages from the earliest raw bar, not the earliest plotted bar', async () => {
    const { provider, controller } = await ready([
      flat(10 * M, 1),
      flat(11 * M, 1),
      traded(12 * M, 2),
    ])
    controller.loadOlder()
    expect(provider.last().endTime).toBe(10 * M - 1)
  })

  it('a page with no trades moves the cursor back, so the next page is older still', async () => {
    const { provider, controller, load, snapshots } = await ready([
      flat(10 * M, 1),
      flat(11 * M, 1),
      traded(12 * M, 2),
    ])
    controller.loadOlder()
    provider.last().resolve([flat(7 * M, 1), flat(8 * M, 1), flat(9 * M, 1)])
    await flush()
    expect(load().candles[0].time).toBe(7 * M)
    expect(load().bars.map((c) => c.time)).toEqual([12 * M])
    expect(snapshots[snapshots.length - 1].historyExhausted).toBe(false)
    controller.loadOlder()
    expect(provider.last().endTime).toBe(7 * M - 1)
  })

  it('still loads older history when nothing in the window is plotted', async () => {
    const { provider, controller, load } = await ready([flat(10 * M, 1), flat(11 * M, 1)])
    controller.loadOlder()
    expect(provider.last().endTime).toBe(10 * M - 1)
    provider.last().resolve([traded(8 * M, 1), flat(9 * M, 1)])
    await flush()
    expect(load().bars.map((c) => c.time)).toEqual([8 * M])
  })
})

describe('LiveCandlesController.loadOlder history limit', () => {
  it('atHistoryLimit is true at or above the limit and never without one', () => {
    expect(atHistoryLimit(4, 5)).toBe(false)
    expect(atHistoryLimit(5, 5)).toBe(true)
    expect(atHistoryLimit(1e9, undefined)).toBe(false)
  })

  it('is a no-op once maxBars raw bars are loaded, bars without trades included', async () => {
    const { provider, controller } = await ready(
      [flat(10 * M, 1), flat(11 * M, 1), traded(12 * M, 2)],
      3,
    )
    controller.loadOlder()
    expect(provider.requests).toHaveLength(1)
  })

  it('beyondLimit (a Strategy Tester history load) still pages past the limit', async () => {
    const { provider, controller } = await ready(
      [flat(10 * M, 1), flat(11 * M, 1), traded(12 * M, 2)],
      3,
    )
    controller.loadOlder(true)
    expect(provider.requests).toHaveLength(2)
    expect(provider.last().endTime).toBe(10 * M - 1)
  })

  it('pages normally below the limit', async () => {
    const { provider, controller } = await ready([flat(10 * M, 1), traded(12 * M, 2)], 3)
    controller.loadOlder()
    expect(provider.requests).toHaveLength(2)
  })
})

describe('LiveCandlesController.olderCooldown', () => {
  it('is set after a failed older page and clears when the cooldown ends', async () => {
    vi.useFakeTimers()
    try {
      // `ready` waits on a timer; advance the fake clock so it resolves.
      const pending = ready([traded(10 * M, 1)])
      await vi.advanceTimersByTimeAsync(0)
      const { provider, controller, snapshots } = await pending
      const latest = () => snapshots[snapshots.length - 1]
      expect(latest().olderCooldown).toBe(false)
      controller.loadOlder()
      provider.last().reject(new MarketDataError('network', 'down'))
      await vi.advanceTimersByTimeAsync(0)
      expect(latest()).toMatchObject({ olderCooldown: true, loadingOlder: false })
      controller.loadOlder() // ignored during the cooldown
      expect(provider.requests).toHaveLength(2)
      await vi.advanceTimersByTimeAsync(GAP_FILL_RETRY_DELAYS_MS[0])
      expect(latest().olderCooldown).toBe(false)
      controller.loadOlder()
      expect(provider.requests).toHaveLength(3)
      controller.stop()
    } finally {
      vi.useRealTimers()
    }
  })
})
