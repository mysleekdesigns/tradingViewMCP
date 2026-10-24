import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MarketDataError, type MarketDataProvider, type SubscribeOptions } from '../data/provider'
import type { Candle, ConnectionStatus, Interval } from '../types'
import {
  describeError,
  LiveCandlesController,
  type LiveCandlesSnapshot,
  type LoadState,
} from './liveCandles'

const H = 3600

function bar(time: number, close: number, volume = 1): Candle {
  return { time, open: close - 1, high: close + 1, low: close - 2, close, volume }
}

interface Deferred {
  resolve: (c: Candle[]) => void
  reject: (e: unknown) => void
  signal: AbortSignal | undefined
}

/** Provider whose REST calls resolve on demand and whose stream is driven by the test. */
class FakeProvider implements MarketDataProvider {
  requests: Deferred[] = []
  subscriptions = 0
  unsubscribes = 0
  private onCandle: ((c: Candle, closed: boolean) => void) | null = null
  private onStatus: ((s: ConnectionStatus) => void) | null = null

  getCandles(
    _symbol: string,
    _interval: Interval,
    _limit?: number,
    _endTime?: number,
    opts?: { signal?: AbortSignal },
  ): Promise<Candle[]> {
    return new Promise((resolve, reject) => {
      this.requests.push({ resolve, reject, signal: opts?.signal })
    })
  }

  subscribeCandles(
    _symbol: string,
    _interval: Interval,
    onCandle: (c: Candle, closed: boolean) => void,
    opts?: SubscribeOptions,
  ): () => void {
    this.subscriptions++
    this.onCandle = onCandle
    this.onStatus = opts?.onStatus ?? null
    return () => {
      this.unsubscribes++
      this.onCandle = null
      this.onStatus = null
    }
  }

  getSymbols(): Promise<never> {
    return Promise.reject(new Error('unused'))
  }

  tick(c: Candle, closed = false): void {
    this.onCandle?.(c, closed)
  }

  status(s: ConnectionStatus): void {
    this.onStatus?.(s)
  }

  last(): Deferred {
    const d = this.requests[this.requests.length - 1]
    if (!d) throw new Error('no request')
    return d
  }
}

/** Let promise callbacks run. */
const flush = () => new Promise<void>((r) => setTimeout(r, 0))

function setup() {
  const provider = new FakeProvider()
  const snapshots: LiveCandlesSnapshot[] = []
  let clock = 1_000
  const controller = new LiveCandlesController(
    {
      provider,
      symbol: 'BTCUSDT',
      interval: '1h',
      limit: 3,
      source: 'Binance.US',
      now: () => clock,
    },
    (s) => snapshots.push(s),
  )
  const latest = () => snapshots[snapshots.length - 1]
  const candles = () => {
    const load: LoadState = latest().load
    if (load.kind !== 'ready') throw new Error(`not ready: ${load.kind}`)
    return load.candles
  }
  return { provider, controller, snapshots, latest, candles, setClock: (t: number) => (clock = t) }
}

const rest = [bar(0, 100), bar(H, 101), bar(2 * H, 102)]

describe('LiveCandlesController', () => {
  it('starts loading and connecting, subscribing and fetching in parallel', () => {
    const { provider, controller, latest } = setup()
    controller.start()
    expect(provider.subscriptions).toBe(1)
    expect(provider.requests).toHaveLength(1)
    expect(latest()).toMatchObject({ load: { kind: 'loading' }, status: 'connecting' })
  })

  it('buffers live candles that arrive before REST and applies them afterwards', async () => {
    const { provider, controller, candles, latest } = setup()
    controller.start()
    provider.tick(bar(2 * H, 150)) // replaces REST's forming bar
    provider.tick(bar(3 * H, 160)) // new bar
    provider.tick(bar(3 * H, 161)) // newer tick for the same bar wins
    expect(latest().load.kind).toBe('loading')
    provider.last().resolve(rest)
    await flush()
    expect(candles().map((c) => [c.time, c.close])).toEqual([
      [0, 100],
      [H, 101],
      [2 * H, 150],
      [3 * H, 161],
    ])
  })

  it('a live tick replaces the forming bar or appends a new one, keeping other identities', async () => {
    const { provider, controller, candles, latest, setClock } = setup()
    controller.start()
    provider.last().resolve(rest)
    await flush()
    const before = candles()
    setClock(5_000)
    provider.tick(bar(2 * H, 103))
    const replaced = candles()
    expect(replaced).toHaveLength(3)
    expect(replaced[2].close).toBe(103)
    expect(replaced[0]).toBe(before[0])
    expect(replaced[1]).toBe(before[1])
    expect(latest().lastUpdate).toBe(5_000)

    provider.tick(bar(3 * H, 104), false)
    expect(candles()).toHaveLength(4)
    expect(candles()[3].close).toBe(104)
  })

  it('ignores a tick older than everything loaded (no snapshot)', async () => {
    const { provider, controller, snapshots } = setup()
    controller.start()
    provider.last().resolve(rest)
    await flush()
    const n = snapshots.length
    provider.tick(bar(-H, 1))
    expect(snapshots).toHaveLength(n)
  })

  it('reports connection status changes', () => {
    const { provider, controller, latest } = setup()
    controller.start()
    provider.status('live')
    expect(latest().status).toBe('live')
    provider.status('reconnecting')
    expect(latest().status).toBe('reconnecting')
    provider.status('offline')
    expect(latest().status).toBe('offline')
  })

  it('gap-fills over REST when the socket comes back after a drop, merging the result', async () => {
    const { provider, controller, candles, latest } = setup()
    controller.start()
    provider.status('live')
    provider.last().resolve(rest)
    await flush()
    expect(provider.requests).toHaveLength(1) // first 'live' is not a reconnect

    provider.status('reconnecting')
    provider.status('offline')
    expect(provider.requests).toHaveLength(1)
    provider.status('live')
    expect(provider.requests).toHaveLength(2)
    expect(latest().refreshing).toBe(true)

    // Ticks during the gap fill still move the chart...
    provider.tick(bar(4 * H, 200))
    expect(candles()[candles().length - 1].close).toBe(200)

    // ...and survive the merge even though REST's copy of that bar is older.
    provider.last().resolve([bar(2 * H, 102), bar(3 * H, 130), bar(4 * H, 140)])
    await flush()
    expect(candles().map((c) => [c.time, c.close])).toEqual([
      [0, 100],
      [H, 101],
      [2 * H, 102],
      [3 * H, 130],
      [4 * H, 200],
    ])
    expect(latest().refreshing).toBe(false)
  })

  it('gap fill keeps identity for bars whose values did not change', async () => {
    const { provider, controller, candles } = setup()
    controller.start()
    provider.last().resolve(rest)
    await flush()
    const before = candles()
    provider.status('reconnecting')
    provider.status('live')
    provider.last().resolve([bar(0, 100), bar(H, 101), bar(2 * H, 109)])
    await flush()
    expect(candles()[0]).toBe(before[0])
    expect(candles()[1]).toBe(before[1])
    expect(candles()[2]).not.toBe(before[2])
    expect(candles()[2].close).toBe(109)
  })

  it('does not gap-fill while the initial request is still running', () => {
    const { provider, controller } = setup()
    controller.start()
    provider.status('reconnecting')
    provider.status('live')
    expect(provider.requests).toHaveLength(1)
  })

  it('retries the load automatically when the socket reconnects after an initial error', async () => {
    const { provider, controller, latest } = setup()
    controller.start()
    provider.last().reject(new MarketDataError('network', 'down'))
    await flush()
    expect(latest().load.kind).toBe('error')
    provider.status('reconnecting')
    provider.status('live')
    expect(provider.requests).toHaveLength(2)
    expect(latest().load.kind).toBe('loading')
  })

  it('maps a failed first load to the error state with the described message', async () => {
    const { provider, controller, latest } = setup()
    controller.start()
    provider.last().reject(new MarketDataError('bad-symbol', 'Invalid symbol', 400))
    await flush()
    expect(latest().load).toEqual({
      kind: 'error',
      title: 'Unknown symbol "BTCUSDT"',
      message: 'Binance.US does not list this pair. Check the symbol and try again.',
    })
  })

  it('a failed refresh keeps the candles on screen and records the error', async () => {
    const { provider, controller, candles, latest } = setup()
    controller.start()
    provider.last().resolve(rest)
    await flush()
    const before = candles()
    controller.reload()
    expect(latest().refreshing).toBe(true)
    expect(latest().load.kind).toBe('ready')
    provider.last().reject(new MarketDataError('network', 'down'))
    await flush()
    expect(candles()).toBe(before)
    expect(latest().refreshing).toBe(false)
    expect(latest().refreshError).toBe('Network error — check connection')
  })

  it('a successful reload clears the previous refresh error', async () => {
    const { provider, controller, latest } = setup()
    controller.start()
    provider.last().resolve(rest)
    await flush()
    controller.reload()
    provider.last().reject(new MarketDataError('network', 'down'))
    await flush()
    controller.reload()
    provider.last().resolve(rest)
    await flush()
    expect(latest().refreshError).toBeNull()
  })

  it('reload after a failed first load shows loading again', async () => {
    const { provider, controller, latest } = setup()
    controller.start()
    provider.last().reject(new MarketDataError('network', 'down'))
    await flush()
    controller.reload()
    expect(latest().load.kind).toBe('loading')
    provider.last().resolve(rest)
    await flush()
    expect(latest().load.kind).toBe('ready')
  })

  it('a newer request aborts the older one and its late result is ignored', async () => {
    const { provider, controller, candles } = setup()
    controller.start()
    provider.last().resolve(rest)
    await flush()
    controller.reload()
    const first = provider.last()
    controller.reload()
    expect(first.signal?.aborted).toBe(true)
    first.resolve([bar(2 * H, 999)])
    await flush()
    expect(candles()[2].close).toBe(102)
  })

  describe('Retry-After', () => {
    beforeEach(() => vi.useFakeTimers())
    afterEach(() => vi.useRealTimers())

    it('blocks reload for the Retry-After window, then unblocks', async () => {
      const { provider, controller, latest } = setup()
      controller.start()
      provider.last().reject(new MarketDataError('rate-limited', '429', 429, 5))
      await vi.advanceTimersByTimeAsync(0)
      expect(latest().load).toMatchObject({ kind: 'error', retryAfter: 5 })
      expect(latest().retryBlocked).toBe(true)
      controller.reload()
      expect(provider.requests).toHaveLength(1)
      await vi.advanceTimersByTimeAsync(5_000)
      expect(latest().retryBlocked).toBe(false)
      controller.reload()
      expect(provider.requests).toHaveLength(2)
    })
  })

  describe('gap fill window', () => {
    it('merges when the fresh rows overlap the candles on screen', async () => {
      const { provider, controller, candles } = setup()
      controller.start()
      provider.last().resolve(rest)
      await flush()
      provider.status('reconnecting')
      provider.status('live')
      provider.last().resolve([bar(2 * H, 110), bar(3 * H, 111), bar(4 * H, 112)])
      await flush()
      expect(candles().map((c) => c.time)).toEqual([0, H, 2 * H, 3 * H, 4 * H])
      expect(candles()[2].close).toBe(110)
    })

    it('replaces the candles when the fresh rows start after the last bar (possible hole)', async () => {
      const { provider, controller, candles, latest } = setup()
      controller.start()
      provider.last().resolve(rest)
      await flush()
      provider.status('reconnecting')
      provider.status('live')
      const fresh = [bar(10 * H, 200), bar(11 * H, 201), bar(12 * H, 202)]
      provider.last().resolve(fresh)
      await flush()
      expect(candles()).toEqual(fresh)
      expect(latest().refreshing).toBe(false)
    })

    it('still applies ticks that arrived during the request on top of a replaced window', async () => {
      const { provider, controller, candles } = setup()
      controller.start()
      provider.last().resolve(rest)
      await flush()
      provider.status('reconnecting')
      provider.status('live')
      provider.tick(bar(12 * H, 999))
      provider.last().resolve([bar(10 * H, 200), bar(11 * H, 201), bar(12 * H, 202)])
      await flush()
      expect(candles().map((c) => [c.time, c.close])).toEqual([
        [10 * H, 200],
        [11 * H, 201],
        [12 * H, 999],
      ])
    })
  })

  describe('gap fill retries and Retry-After', () => {
    beforeEach(() => vi.useFakeTimers())
    afterEach(() => vi.useRealTimers())

    async function readyAndDropped() {
      const ctx = setup()
      ctx.controller.start()
      ctx.provider.last().resolve(rest)
      await vi.advanceTimersByTimeAsync(0)
      return ctx
    }

    it('defers a reconnect gap fill until the Retry-After window ends, then runs it once', async () => {
      const { provider, controller, latest } = await readyAndDropped()
      controller.reload()
      provider.last().reject(new MarketDataError('rate-limited', '429', 429, 5))
      await vi.advanceTimersByTimeAsync(0)
      expect(latest().retryBlocked).toBe(true)
      expect(provider.requests).toHaveLength(2)

      provider.status('reconnecting')
      provider.status('live')
      expect(provider.requests).toHaveLength(2) // no REST call while blocked
      await vi.advanceTimersByTimeAsync(4_999)
      expect(provider.requests).toHaveLength(2)
      await vi.advanceTimersByTimeAsync(1)
      expect(latest().retryBlocked).toBe(false)
      expect(provider.requests).toHaveLength(3)
      await vi.advanceTimersByTimeAsync(60_000)
      expect(provider.requests).toHaveLength(3)
    })

    it('retries a failed gap fill and succeeds on the 2nd attempt', async () => {
      const { provider, controller, latest, candles } = await readyAndDropped()
      provider.status('reconnecting')
      provider.status('live')
      expect(provider.requests).toHaveLength(2)
      provider.last().reject(new MarketDataError('network', 'down'))
      await vi.advanceTimersByTimeAsync(0)
      expect(latest().refreshError).toBe('Network error — check connection')
      await vi.advanceTimersByTimeAsync(1_999)
      expect(provider.requests).toHaveLength(2)
      await vi.advanceTimersByTimeAsync(1)
      expect(provider.requests).toHaveLength(3)
      provider.last().resolve([bar(2 * H, 102), bar(3 * H, 103)])
      await vi.advanceTimersByTimeAsync(0)
      expect(latest().refreshError).toBeNull()
      expect(candles().map((c) => c.time)).toEqual([0, H, 2 * H, 3 * H])
      expect(vi.getTimerCount()).toBe(0)
      controller.stop()
    })

    it('gives up after 3 retries with increasing delay and keeps the refresh error', async () => {
      const { provider, controller, latest } = await readyAndDropped()
      provider.status('reconnecting')
      provider.status('live')
      provider.last().reject(new MarketDataError('network', 'down'))
      await vi.advanceTimersByTimeAsync(0)
      for (const delay of [2_000, 4_000, 8_000]) {
        const n = provider.requests.length
        await vi.advanceTimersByTimeAsync(delay - 1)
        expect(provider.requests).toHaveLength(n)
        await vi.advanceTimersByTimeAsync(1)
        expect(provider.requests).toHaveLength(n + 1)
        provider.last().reject(new MarketDataError('network', 'down'))
        await vi.advanceTimersByTimeAsync(0)
      }
      expect(provider.requests).toHaveLength(5) // initial + gap fill + 3 retries
      await vi.advanceTimersByTimeAsync(120_000)
      expect(provider.requests).toHaveLength(5)
      expect(latest().refreshError).toBe('Network error — check connection')
      expect(latest().load.kind).toBe('ready')
      controller.stop()
    })

    it('a gap fill hit by 429 retries only after the Retry-After window', async () => {
      const { provider, controller, latest } = await readyAndDropped()
      provider.status('reconnecting')
      provider.status('live')
      provider.last().reject(new MarketDataError('rate-limited', '429', 429, 30))
      await vi.advanceTimersByTimeAsync(0)
      expect(latest().retryBlocked).toBe(true)
      await vi.advanceTimersByTimeAsync(29_999)
      expect(provider.requests).toHaveLength(2)
      await vi.advanceTimersByTimeAsync(1)
      expect(provider.requests).toHaveLength(3)
      controller.stop()
    })

    it('stop() cancels a pending gap-fill retry', async () => {
      const { provider, controller, snapshots } = await readyAndDropped()
      provider.status('reconnecting')
      provider.status('live')
      provider.last().reject(new MarketDataError('network', 'down'))
      await vi.advanceTimersByTimeAsync(0)
      expect(vi.getTimerCount()).toBe(1)
      controller.stop()
      expect(vi.getTimerCount()).toBe(0)
      const n = snapshots.length
      await vi.advanceTimersByTimeAsync(60_000)
      expect(provider.requests).toHaveLength(2)
      expect(snapshots).toHaveLength(n)
    })

    it('a newer request supersedes a pending gap-fill retry', async () => {
      const { provider, controller } = await readyAndDropped()
      provider.status('reconnecting')
      provider.status('live')
      provider.last().reject(new MarketDataError('network', 'down'))
      await vi.advanceTimersByTimeAsync(0)
      controller.reload()
      expect(provider.requests).toHaveLength(3)
      provider.last().resolve(rest)
      await vi.advanceTimersByTimeAsync(60_000)
      expect(provider.requests).toHaveLength(3)
      controller.stop()
    })
  })

  describe('stop()', () => {
    it('unsubscribes, aborts the in-flight request, and never calls the listener again', async () => {
      const { provider, controller, snapshots } = setup()
      controller.start()
      const req = provider.last()
      controller.stop()
      expect(provider.unsubscribes).toBe(1)
      expect(req.signal?.aborted).toBe(true)
      const n = snapshots.length
      req.resolve(rest)
      await flush()
      provider.tick(bar(3 * H, 1))
      provider.status('live')
      controller.reload()
      expect(snapshots).toHaveLength(n)
      expect(provider.requests).toHaveLength(1)
    })

    it('is idempotent', () => {
      const { provider, controller } = setup()
      controller.start()
      controller.stop()
      controller.stop()
      expect(provider.unsubscribes).toBe(1)
    })

    it('clears a pending Retry-After timer', async () => {
      vi.useFakeTimers()
      try {
        const { provider, controller, snapshots } = setup()
        controller.start()
        provider.last().reject(new MarketDataError('banned', '418', 418, 10))
        await vi.advanceTimersByTimeAsync(0)
        controller.stop()
        const n = snapshots.length
        expect(vi.getTimerCount()).toBe(0)
        await vi.advanceTimersByTimeAsync(10_000)
        expect(snapshots).toHaveLength(n)
      } finally {
        vi.useRealTimers()
      }
    })

    it('start() after stop() does nothing (restart means a new controller)', () => {
      const { provider, controller } = setup()
      controller.stop()
      controller.start()
      expect(provider.subscriptions).toBe(0)
    })
  })
})

describe('describeError', () => {
  const cases: [MarketDataError | Error, string][] = [
    [new MarketDataError('network', 'x'), 'Network error — check connection'],
    [new MarketDataError('rate-limited', 'x', 429, 3), 'Rate limited by the exchange'],
    [new MarketDataError('banned', 'x', 418, 3), 'Temporarily blocked by the exchange'],
    [new MarketDataError('region-blocked', 'x', 451), 'Not available in your region'],
    [new MarketDataError('http', 'x', 500), 'Could not load market data'],
    [new Error('boom'), 'Could not load market data'],
  ]
  it.each(cases)('maps %s to a title', (err, title) => {
    expect(describeError(err, 'BTCUSDT', 'Binance.US').title).toBe(title)
  })

  it('includes the HTTP status for unexpected responses', () => {
    expect(describeError(new MarketDataError('http', 'x', 503), 'X', 'S').message).toContain(
      'HTTP 503',
    )
  })
})
