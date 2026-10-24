import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MarketDataError, type MarketDataProvider, type SubscribeOptions } from '../data/provider'
import type { Candle, ConnectionStatus, Interval } from '../types'
import {
  DEFAULT_BAN_BLOCK_S,
  DEFAULT_RATE_LIMIT_BLOCK_S,
  describeError,
  GAP_FILL_RETRY_DELAYS_MS,
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
  limit: number | undefined
  endTime: number | undefined
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
    limit?: number,
    endTime?: number,
    opts?: { signal?: AbortSignal },
  ): Promise<Candle[]> {
    return new Promise((resolve, reject) => {
      this.requests.push({ resolve, reject, signal: opts?.signal, limit, endTime })
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

  getTickers(): Promise<never> {
    return Promise.reject(new Error('unused'))
  }

  subscribeTickers(): () => void {
    return () => {}
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

function setup(limit = 3) {
  const provider = new FakeProvider()
  const snapshots: LiveCandlesSnapshot[] = []
  let clock = 1_000
  const controller = new LiveCandlesController(
    {
      provider,
      symbol: 'BTCUSDT',
      interval: '1h',
      limit,
      source: 'Binance',
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
      message: 'Binance does not list this pair. Check the symbol and try again.',
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

describe('LiveCandlesController.loadOlder', () => {
  // On screen: bars at 10H, 11H, 12H.
  const recent = [bar(10 * H, 110), bar(11 * H, 111), bar(12 * H, 112)]
  const older = [bar(7 * H, 107), bar(8 * H, 108), bar(9 * H, 109)]

  async function ready(limit = 3) {
    const ctx = setup(limit)
    ctx.controller.start()
    ctx.provider.last().resolve(recent)
    await flush()
    return ctx
  }

  it('starts with loadingOlder and historyExhausted false', () => {
    const { controller, latest } = setup()
    controller.start()
    expect(latest()).toMatchObject({ loadingOlder: false, historyExhausted: false })
  })

  it('is a no-op before candles are ready', () => {
    const { provider, controller, latest } = setup()
    controller.start()
    controller.loadOlder()
    expect(provider.requests).toHaveLength(1)
    expect(latest().loadingOlder).toBe(false)
  })

  it('is a no-op when the load is ready but empty', async () => {
    const { provider, controller } = setup()
    controller.start()
    provider.last().resolve([])
    await flush()
    controller.loadOlder()
    expect(provider.requests).toHaveLength(1)
  })

  it('is a no-op after an initial load error', async () => {
    const { provider, controller } = setup()
    controller.start()
    provider.last().reject(new MarketDataError('network', 'down'))
    await flush()
    controller.loadOlder()
    expect(provider.requests).toHaveLength(1)
  })

  it('requests one page ending one second before the oldest bar (UNIX seconds)', async () => {
    const { provider, controller, latest } = await ready()
    controller.loadOlder()
    expect(provider.requests).toHaveLength(2)
    expect(provider.last().endTime).toBe(10 * H - 1)
    expect(provider.last().limit).toBe(3)
    expect(provider.last().signal).toBeDefined()
    expect(latest().loadingOlder).toBe(true)
  })

  it('caps the page size at 1000', async () => {
    const { provider, controller } = await ready(5000)
    controller.loadOlder()
    expect(provider.last().limit).toBe(1000)
  })

  it('prepends the older rows and keeps the identity of existing bars', async () => {
    const { provider, controller, candles, latest, setClock } = await ready()
    const before = candles()
    controller.loadOlder()
    setClock(9_000)
    provider.last().resolve(older)
    await flush()
    const after = candles()
    expect(after.map((c) => c.time)).toEqual([7 * H, 8 * H, 9 * H, 10 * H, 11 * H, 12 * H])
    expect(after[3]).toBe(before[0])
    expect(after[4]).toBe(before[1])
    expect(after[5]).toBe(before[2])
    expect(latest()).toMatchObject({ loadingOlder: false, historyExhausted: false })
    expect(latest().lastUpdate).toBe(9_000)
  })

  it('drops returned rows at or after the current oldest bar', async () => {
    const { provider, controller, candles } = await ready()
    const before = candles()
    controller.loadOlder()
    provider.last().resolve([bar(9 * H, 109), bar(10 * H, 999), bar(11 * H, 999)])
    await flush()
    expect(candles().map((c) => [c.time, c.close])).toEqual([
      [9 * H, 109],
      [10 * H, 110],
      [11 * H, 111],
      [12 * H, 112],
    ])
    expect(candles()[1]).toBe(before[0])
  })

  it('marks history exhausted when nothing older comes back, then stops requesting', async () => {
    const { provider, controller, candles, latest } = await ready()
    const before = candles()
    controller.loadOlder()
    provider.last().resolve([])
    await flush()
    expect(latest()).toMatchObject({ loadingOlder: false, historyExhausted: true })
    expect(candles()).toBe(before)
    controller.loadOlder()
    expect(provider.requests).toHaveLength(2)
  })

  it('rapid repeated calls make one request at a time', async () => {
    const { provider, controller } = await ready()
    for (let i = 0; i < 10; i++) controller.loadOlder()
    expect(provider.requests).toHaveLength(2)
    provider.last().resolve(older)
    await flush()
    controller.loadOlder()
    controller.loadOlder()
    expect(provider.requests).toHaveLength(3)
    expect(provider.last().endTime).toBe(7 * H - 1)
  })

  it('applies live ticks that arrive during the request, and keeps them after the prepend', async () => {
    const { provider, controller, candles } = await ready()
    controller.loadOlder()
    provider.tick(bar(12 * H, 150))
    expect(candles()[2].close).toBe(150)
    provider.tick(bar(13 * H, 160))
    expect(candles()).toHaveLength(4)
    const tick = candles()[3]
    provider.last().resolve(older)
    await flush()
    expect(candles().map((c) => [c.time, c.close])).toEqual([
      [7 * H, 107],
      [8 * H, 108],
      [9 * H, 109],
      [10 * H, 110],
      [11 * H, 111],
      [12 * H, 150],
      [13 * H, 160],
    ])
    expect(candles()[6]).toBe(tick)
  })

  it('a live tick does not abort the older page', async () => {
    const { provider, controller } = await ready()
    controller.loadOlder()
    const req = provider.last()
    provider.tick(bar(13 * H, 160))
    expect(req.signal?.aborted).toBe(false)
  })

  it('an overlapping gap fill does not cancel it; both results land', async () => {
    const { provider, controller, candles, latest } = await ready()
    controller.loadOlder()
    const olderReq = provider.last()
    provider.status('reconnecting')
    provider.status('live')
    const gapReq = provider.last()
    expect(gapReq).not.toBe(olderReq)
    expect(olderReq.signal?.aborted).toBe(false)
    gapReq.resolve([bar(12 * H, 120), bar(13 * H, 130)])
    await flush()
    olderReq.resolve(older)
    await flush()
    expect(candles().map((c) => c.time)).toEqual([
      7 * H,
      8 * H,
      9 * H,
      10 * H,
      11 * H,
      12 * H,
      13 * H,
    ])
    expect(latest()).toMatchObject({ loadingOlder: false, refreshing: false })
  })

  it('reload aborts the older page and its late result is ignored', async () => {
    const { provider, controller, candles, latest } = await ready()
    controller.loadOlder()
    const olderReq = provider.last()
    controller.reload()
    expect(olderReq.signal?.aborted).toBe(true)
    expect(latest().loadingOlder).toBe(false)
    olderReq.resolve(older)
    await flush()
    provider.last().resolve(recent)
    await flush()
    expect(candles().map((c) => c.time)).toEqual([10 * H, 11 * H, 12 * H])
  })

  it('a replaced (disjoint) gap-fill window discards the older page', async () => {
    const { provider, controller, candles, latest } = await ready()
    controller.loadOlder()
    const olderReq = provider.last()
    provider.status('reconnecting')
    provider.status('live')
    const fresh = [bar(20 * H, 200), bar(21 * H, 201), bar(22 * H, 202)]
    provider.last().resolve(fresh)
    await flush()
    expect(olderReq.signal?.aborted).toBe(true)
    expect(latest().loadingOlder).toBe(false)
    olderReq.resolve(older)
    await flush()
    expect(candles()).toEqual(fresh)
  })

  it('a replaced window resets historyExhausted', async () => {
    const { provider, controller, latest } = await ready()
    controller.loadOlder()
    provider.last().resolve([])
    await flush()
    expect(latest().historyExhausted).toBe(true)
    provider.status('reconnecting')
    provider.status('live')
    provider.last().resolve([bar(20 * H, 200), bar(21 * H, 201)])
    await flush()
    expect(latest().historyExhausted).toBe(false)
    controller.loadOlder()
    expect(provider.last().endTime).toBe(20 * H - 1)
  })

  it('stop() aborts the older page and never emits its result', async () => {
    const { provider, controller, snapshots } = await ready()
    controller.loadOlder()
    const olderReq = provider.last()
    controller.stop()
    expect(olderReq.signal?.aborted).toBe(true)
    const n = snapshots.length
    olderReq.resolve(older)
    await flush()
    controller.loadOlder()
    expect(snapshots).toHaveLength(n)
    expect(provider.requests).toHaveLength(2)
  })

  it('an aborted request clears loadingOlder silently', async () => {
    const { provider, controller, latest } = await ready()
    controller.loadOlder()
    provider.last().reject(new MarketDataError('aborted', 'aborted'))
    await flush()
    expect(latest()).toMatchObject({ loadingOlder: false, refreshError: null })
  })

  /** ready() under fake timers (flush() would never fire). */
  async function readyFake(limit = 3) {
    const ctx = setup(limit)
    ctx.controller.start()
    ctx.provider.last().resolve(recent)
    await vi.advanceTimersByTimeAsync(0)
    return ctx
  }

  it('a failed page keeps the candles and records refreshError; a retry can follow', async () => {
    vi.useFakeTimers()
    try {
      const { provider, controller, candles, latest } = await readyFake()
      const before = candles()
      controller.loadOlder()
      provider.last().reject(new MarketDataError('network', 'down'))
      await vi.advanceTimersByTimeAsync(0)
      expect(candles()).toBe(before)
      expect(latest()).toMatchObject({
        loadingOlder: false,
        historyExhausted: false,
        retryBlocked: false,
        refreshError: 'Network error — check connection',
      })
      await vi.advanceTimersByTimeAsync(GAP_FILL_RETRY_DELAYS_MS[0]) // failure cooldown
      controller.loadOlder()
      expect(provider.requests).toHaveLength(3)
    } finally {
      vi.useRealTimers()
    }
  })

  it('a successful page clears a refreshError left by a failed page', async () => {
    vi.useFakeTimers()
    try {
      const { provider, controller, latest } = await readyFake()
      controller.loadOlder()
      provider.last().reject(new MarketDataError('network', 'down'))
      await vi.advanceTimersByTimeAsync(GAP_FILL_RETRY_DELAYS_MS[0]) // failure cooldown
      controller.loadOlder()
      provider.last().resolve(older)
      await vi.advanceTimersByTimeAsync(0)
      expect(latest().refreshError).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  describe('failure cooldown and offline guard', () => {
    beforeEach(() => vi.useFakeTimers())
    afterEach(() => {
      vi.useRealTimers()
      vi.unstubAllGlobals()
    })

    async function failOlder(ctx: Awaited<ReturnType<typeof readyFake>>) {
      ctx.controller.loadOlder()
      ctx.provider.last().reject(new MarketDataError('network', 'down'))
      await vi.advanceTimersByTimeAsync(0)
    }

    it('loadOlder is a no-op during the cooldown after a failed page', async () => {
      const ctx = await readyFake()
      await failOlder(ctx)
      for (let i = 0; i < 20; i++) ctx.controller.loadOlder() // drag frames
      expect(ctx.provider.requests).toHaveLength(2)
      await vi.advanceTimersByTimeAsync(1_999)
      ctx.controller.loadOlder()
      expect(ctx.provider.requests).toHaveLength(2)
      await vi.advanceTimersByTimeAsync(1)
      ctx.controller.loadOlder()
      expect(ctx.provider.requests).toHaveLength(3)
      ctx.controller.stop()
    })

    it('the cooldown escalates 2s, 4s, 8s and stays capped at 8s', async () => {
      const ctx = await readyFake()
      for (const delay of [2_000, 4_000, 8_000, 8_000]) {
        await failOlder(ctx)
        const n = ctx.provider.requests.length
        await vi.advanceTimersByTimeAsync(delay - 1)
        ctx.controller.loadOlder()
        expect(ctx.provider.requests).toHaveLength(n)
        await vi.advanceTimersByTimeAsync(1)
      }
      ctx.controller.stop()
    })

    it('a successful page resets the escalation', async () => {
      const ctx = await readyFake()
      await failOlder(ctx)
      await vi.advanceTimersByTimeAsync(2_000)
      await failOlder(ctx) // 2nd failure: 4s
      await vi.advanceTimersByTimeAsync(4_000)
      ctx.controller.loadOlder()
      ctx.provider.last().resolve(older)
      await vi.advanceTimersByTimeAsync(0)
      await failOlder(ctx) // back to 2s
      await vi.advanceTimersByTimeAsync(2_000)
      const n = ctx.provider.requests.length
      ctx.controller.loadOlder()
      expect(ctx.provider.requests).toHaveLength(n + 1)
      ctx.controller.stop()
    })

    it('a reload ends the cooldown and resets the escalation', async () => {
      const ctx = await readyFake()
      await failOlder(ctx)
      ctx.controller.reload()
      ctx.provider.last().resolve(recent)
      await vi.advanceTimersByTimeAsync(0)
      const n = ctx.provider.requests.length
      ctx.controller.loadOlder()
      expect(ctx.provider.requests).toHaveLength(n + 1)
      ctx.controller.stop()
    })

    it('a replaced window ends the cooldown', async () => {
      const ctx = await readyFake()
      await failOlder(ctx)
      ctx.provider.status('reconnecting')
      ctx.provider.status('live')
      ctx.provider.last().resolve([bar(20 * H, 200), bar(21 * H, 201)]) // disjoint: replaces
      await vi.advanceTimersByTimeAsync(0)
      const n = ctx.provider.requests.length
      ctx.controller.loadOlder()
      expect(ctx.provider.requests).toHaveLength(n + 1)
      expect(ctx.provider.last().endTime).toBe(20 * H - 1)
      ctx.controller.stop()
    })

    it('an aborted page starts no cooldown', async () => {
      const ctx = await readyFake()
      ctx.controller.loadOlder()
      ctx.provider.last().reject(new MarketDataError('aborted', 'aborted'))
      await vi.advanceTimersByTimeAsync(0)
      ctx.controller.loadOlder()
      expect(ctx.provider.requests).toHaveLength(3)
      ctx.controller.stop()
    })

    it('stop() clears the cooldown timer', async () => {
      const ctx = await readyFake()
      await failOlder(ctx)
      ctx.controller.stop()
      expect(vi.getTimerCount()).toBe(0)
    })

    it('skips loadOlder while the browser is offline', async () => {
      const ctx = await readyFake()
      vi.stubGlobal('navigator', { onLine: false })
      ctx.controller.loadOlder()
      expect(ctx.provider.requests).toHaveLength(1)
      expect(ctx.latest().loadingOlder).toBe(false)
      vi.stubGlobal('navigator', { onLine: true })
      ctx.controller.loadOlder()
      expect(ctx.provider.requests).toHaveLength(2)
      ctx.controller.stop()
    })

    it('works when navigator is undefined', async () => {
      const ctx = await readyFake()
      vi.stubGlobal('navigator', undefined)
      ctx.controller.loadOlder()
      expect(ctx.provider.requests).toHaveLength(2)
      ctx.controller.stop()
    })
  })

  it('does not clear a refreshError left by a failed reload', async () => {
    const { provider, controller, latest } = await ready()
    controller.reload()
    provider.last().reject(new MarketDataError('network', 'down'))
    await flush()
    controller.loadOlder()
    provider.last().resolve(older)
    await flush()
    expect(latest().refreshError).toBe('Network error — check connection')
  })

  describe('Retry-After', () => {
    beforeEach(() => vi.useFakeTimers())
    afterEach(() => vi.useRealTimers())

    it('a 429 on an older page blocks loadOlder and reload for the window', async () => {
      const ctx = setup()
      ctx.controller.start()
      ctx.provider.last().resolve(recent)
      await vi.advanceTimersByTimeAsync(0)
      ctx.controller.loadOlder()
      ctx.provider.last().reject(new MarketDataError('rate-limited', '429', 429, 5))
      await vi.advanceTimersByTimeAsync(0)
      expect(ctx.latest()).toMatchObject({
        retryBlocked: true,
        loadingOlder: false,
        refreshError: 'Rate limited by the exchange',
      })
      ctx.controller.loadOlder()
      ctx.controller.reload()
      expect(ctx.provider.requests).toHaveLength(2)
      await vi.advanceTimersByTimeAsync(4_999)
      ctx.controller.loadOlder()
      expect(ctx.provider.requests).toHaveLength(2)
      await vi.advanceTimersByTimeAsync(1)
      expect(ctx.latest().retryBlocked).toBe(false)
      ctx.controller.loadOlder()
      expect(ctx.provider.requests).toHaveLength(3)
      ctx.controller.stop()
    })

    it('a 418 from a reload blocks loadOlder until the window ends', async () => {
      const ctx = setup()
      ctx.controller.start()
      ctx.provider.last().resolve(recent)
      await vi.advanceTimersByTimeAsync(0)
      ctx.controller.reload()
      ctx.provider.last().reject(new MarketDataError('banned', '418', 418, 10))
      await vi.advanceTimersByTimeAsync(0)
      ctx.controller.loadOlder()
      expect(ctx.provider.requests).toHaveLength(2)
      await vi.advanceTimersByTimeAsync(10_000)
      ctx.controller.loadOlder()
      expect(ctx.provider.requests).toHaveLength(3)
      ctx.controller.stop()
    })

    it('a gap fill deferred by an older-page 429 runs when the window ends', async () => {
      const ctx = setup()
      ctx.controller.start()
      ctx.provider.last().resolve(recent)
      await vi.advanceTimersByTimeAsync(0)
      ctx.controller.loadOlder()
      ctx.provider.last().reject(new MarketDataError('rate-limited', '429', 429, 5))
      await vi.advanceTimersByTimeAsync(0)
      ctx.provider.status('reconnecting')
      ctx.provider.status('live')
      expect(ctx.provider.requests).toHaveLength(2)
      await vi.advanceTimersByTimeAsync(5_000)
      expect(ctx.provider.requests).toHaveLength(3)
      expect(ctx.provider.last().endTime).toBeUndefined()
      ctx.controller.stop()
    })
  })
})

describe('Retry-After defaults when the header is missing', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  async function readyFake() {
    const ctx = setup()
    ctx.controller.start()
    ctx.provider.last().resolve(rest)
    await vi.advanceTimersByTimeAsync(0)
    return ctx
  }

  it('exports 60 s for 429 and a longer default for 418', () => {
    expect(DEFAULT_RATE_LIMIT_BLOCK_S).toBe(60)
    expect(DEFAULT_BAN_BLOCK_S).toBeGreaterThan(DEFAULT_RATE_LIMIT_BLOCK_S)
  })

  it('an initial-load 429 without Retry-After blocks reload for 60 s', async () => {
    const { provider, controller, latest } = setup()
    controller.start()
    provider.last().reject(new MarketDataError('rate-limited', '429', 429))
    await vi.advanceTimersByTimeAsync(0)
    expect(latest().load).toMatchObject({ kind: 'error', retryAfter: 60 })
    expect(latest().retryBlocked).toBe(true)
    await vi.advanceTimersByTimeAsync(59_999)
    controller.reload()
    expect(provider.requests).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(latest().retryBlocked).toBe(false)
    controller.reload()
    expect(provider.requests).toHaveLength(2)
    controller.stop()
  })

  it('a reload 418 without Retry-After blocks for the ban default', async () => {
    const { provider, controller, latest } = await readyFake()
    controller.reload()
    provider.last().reject(new MarketDataError('banned', '418', 418))
    await vi.advanceTimersByTimeAsync(0)
    expect(latest().retryBlocked).toBe(true)
    await vi.advanceTimersByTimeAsync(DEFAULT_BAN_BLOCK_S * 1000 - 1)
    expect(latest().retryBlocked).toBe(true)
    await vi.advanceTimersByTimeAsync(1)
    expect(latest().retryBlocked).toBe(false)
    controller.stop()
  })

  it('a Retry-After of 0 on a 429 still blocks with the default', async () => {
    const { provider, controller, latest } = await readyFake()
    controller.reload()
    provider.last().reject(new MarketDataError('rate-limited', '429', 429, 0))
    await vi.advanceTimersByTimeAsync(0)
    expect(latest().retryBlocked).toBe(true)
    await vi.advanceTimersByTimeAsync(60_000)
    expect(latest().retryBlocked).toBe(false)
    controller.stop()
  })

  it('a gap-fill 429 without Retry-After blocks and retries only after 60 s', async () => {
    const { provider, controller, latest } = await readyFake()
    provider.status('reconnecting')
    provider.status('live')
    expect(provider.requests).toHaveLength(2)
    provider.last().reject(new MarketDataError('rate-limited', '429', 429))
    await vi.advanceTimersByTimeAsync(0)
    expect(latest().retryBlocked).toBe(true)
    await vi.advanceTimersByTimeAsync(59_999)
    expect(provider.requests).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(1)
    expect(provider.requests).toHaveLength(3)
    controller.stop()
  })

  it('an older-page 429 without Retry-After blocks loadOlder for 60 s', async () => {
    const { provider, controller, latest } = await readyFake()
    controller.loadOlder()
    provider.last().reject(new MarketDataError('rate-limited', '429', 429))
    await vi.advanceTimersByTimeAsync(0)
    expect(latest().retryBlocked).toBe(true)
    await vi.advanceTimersByTimeAsync(59_999)
    controller.loadOlder()
    expect(provider.requests).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(1)
    controller.loadOlder()
    expect(provider.requests).toHaveLength(3)
    controller.stop()
  })

  it('an older-page 418 without Retry-After blocks loadOlder for the ban default', async () => {
    const { provider, controller, latest } = await readyFake()
    controller.loadOlder()
    provider.last().reject(new MarketDataError('banned', '418', 418))
    await vi.advanceTimersByTimeAsync(0)
    expect(latest().retryBlocked).toBe(true)
    await vi.advanceTimersByTimeAsync(DEFAULT_BAN_BLOCK_S * 1000 - 1)
    controller.loadOlder()
    expect(provider.requests).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(1)
    controller.loadOlder()
    expect(provider.requests).toHaveLength(3)
    controller.stop()
  })

  it('a network error does not block', async () => {
    const { provider, controller, latest } = await readyFake()
    controller.reload()
    provider.last().reject(new MarketDataError('network', 'down'))
    await vi.advanceTimersByTimeAsync(0)
    expect(latest().retryBlocked).toBe(false)
    controller.stop()
  })
})

describe('post-reconnect gap fill only when a bar may be missed', () => {
  // REST window ends with the forming bar at 2H; the clock is in ms.
  async function liveAt(nowMs: number) {
    const ctx = setup()
    ctx.setClock(nowMs)
    ctx.controller.start()
    ctx.provider.last().resolve(rest)
    await flush()
    return ctx
  }

  it('a quiet reconnect within the same bar skips the gap fill', async () => {
    const ctx = await liveAt(2 * H * 1000 + 10_000)
    ctx.provider.tick(bar(2 * H, 150))
    ctx.setClock(2 * H * 1000 + 70_000)
    ctx.provider.status('reconnecting')
    ctx.provider.status('live')
    expect(ctx.provider.requests).toHaveLength(1)
    expect(ctx.latest()).toMatchObject({ status: 'live', refreshing: false })
  })

  it('a reconnect after a bar boundary gap fills', async () => {
    const ctx = await liveAt(2 * H * 1000 + 10_000)
    ctx.provider.tick(bar(2 * H, 150))
    ctx.setClock(3 * H * 1000 + 5_000)
    ctx.provider.status('reconnecting')
    ctx.provider.status('live')
    expect(ctx.provider.requests).toHaveLength(2)
    expect(ctx.provider.last().endTime).toBeUndefined()
  })

  it('a reconnect with no tick received yet gap fills', async () => {
    const ctx = await liveAt(2 * H * 1000 + 10_000)
    ctx.provider.status('reconnecting')
    ctx.provider.status('live')
    expect(ctx.provider.requests).toHaveLength(2)
  })

  it('a reconnect when the last tick is a full interval old gap fills', async () => {
    // The current bar's open matches the last candle, but the last tick is too old.
    const ctx = await liveAt(2 * H * 1000 + 10_000)
    ctx.provider.tick(bar(2 * H, 150), true) // closed: the rollover needs no repair
    ctx.provider.tick(bar(3 * H, 160)) // last candle now 3H
    ctx.setClock(3 * H * 1000 + 30_000) // last tick at 2H+10s: over one interval ago
    ctx.provider.status('reconnecting')
    ctx.provider.status('live')
    expect(ctx.provider.requests).toHaveLength(2)
  })

  it('a reconnect when the last candle is behind the current bar gap fills', async () => {
    const ctx = await liveAt(3 * H * 1000 + 10_000) // REST last bar 2H, now in 3H
    ctx.provider.tick(bar(2 * H, 150))
    ctx.provider.status('reconnecting')
    ctx.provider.status('live')
    expect(ctx.provider.requests).toHaveLength(2)
  })
})

describe('rollover without a closed tick on the previous bar', () => {
  async function liveNow() {
    const ctx = setup()
    ctx.controller.start()
    ctx.provider.last().resolve(rest) // last REST bar: 2H
    await flush()
    return ctx
  }

  it('a rollover after an x:true tick needs no gap fill', async () => {
    const { provider } = await liveNow()
    provider.tick(bar(2 * H, 150))
    provider.tick(bar(2 * H, 151), true)
    provider.tick(bar(3 * H, 160))
    expect(provider.requests).toHaveLength(1)
  })

  it('a rollover without an x:true tick on the previous bar requests one gap fill', async () => {
    const { provider, candles } = await liveNow()
    provider.tick(bar(2 * H, 150))
    provider.tick(bar(3 * H, 160))
    expect(provider.requests).toHaveLength(2)
    expect(provider.last().endTime).toBeUndefined()
    // The new bar is shown at once and survives the gap-fill result.
    expect(candles().map((c) => c.time)).toEqual([0, H, 2 * H, 3 * H])
    provider.last().resolve([bar(H, 101), bar(2 * H, 155), bar(3 * H, 158)])
    await flush()
    expect(candles().map((c) => [c.time, c.close])).toEqual([
      [0, 100],
      [H, 101],
      [2 * H, 155],
      [3 * H, 160],
    ])
  })

  it('repeated ticks on the new bar still give only one gap fill', async () => {
    const { provider } = await liveNow()
    provider.tick(bar(2 * H, 150))
    provider.tick(bar(3 * H, 160))
    provider.tick(bar(3 * H, 161))
    provider.tick(bar(3 * H, 162), true)
    expect(provider.requests).toHaveLength(2)
  })

  it('the first tick after a REST load never triggers a gap fill, even on a new bar', async () => {
    const { provider } = await liveNow()
    provider.tick(bar(3 * H, 160)) // REST's 2H row is authoritative
    expect(provider.requests).toHaveLength(1)
  })

  it('the first tick after a gap-fill result does not trigger another', async () => {
    const { provider } = await liveNow()
    provider.tick(bar(2 * H, 150))
    provider.tick(bar(3 * H, 160))
    provider.last().resolve([bar(2 * H, 155), bar(3 * H, 160)])
    await flush()
    provider.tick(bar(4 * H, 170))
    expect(provider.requests).toHaveLength(2)
  })

  it('a late tick for an older bar neither fires nor hides a later missed close', async () => {
    const { provider } = await liveNow()
    provider.tick(bar(2 * H, 150), true)
    provider.tick(bar(3 * H, 160))
    provider.tick(bar(2 * H, 151)) // late: ignored for tracking
    expect(provider.requests).toHaveLength(1)
    provider.tick(bar(4 * H, 170)) // 3H never closed
    expect(provider.requests).toHaveLength(2)
  })

  it('a missed-close gap fill during a Retry-After window waits for the window', async () => {
    vi.useFakeTimers()
    try {
      const { provider, controller, latest } = setup()
      controller.start()
      provider.last().resolve(rest)
      await vi.advanceTimersByTimeAsync(0)
      controller.reload()
      provider.last().reject(new MarketDataError('rate-limited', '429', 429, 5))
      await vi.advanceTimersByTimeAsync(0)
      expect(latest().retryBlocked).toBe(true)
      provider.tick(bar(2 * H, 150))
      provider.tick(bar(3 * H, 160))
      expect(provider.requests).toHaveLength(2)
      await vi.advanceTimersByTimeAsync(5_000)
      expect(provider.requests).toHaveLength(3)
      controller.stop()
    } finally {
      vi.useRealTimers()
    }
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
    expect(describeError(err, 'BTCUSDT', 'Binance').title).toBe(title)
  })

  it('includes the HTTP status for unexpected responses', () => {
    expect(describeError(new MarketDataError('http', 'x', 503), 'X', 'S').message).toContain(
      'HTTP 503',
    )
  })
})
