import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MarketDataError, type MarketDataProvider } from '../data/provider'
import {
  createTimeframeProvider,
  type CandleSource,
  type CandleSubscribeOptions,
} from '../data/timeframeProvider'
import type { Candle, ConnectionStatus, Interval, Timeframe } from '../types'
import {
  closedThroughOf,
  DEFAULT_BAN_BLOCK_S,
  DEFAULT_RATE_LIMIT_BLOCK_S,
  describeError,
  GAP_FILL_RETRY_DELAYS_MS,
  LiveCandlesController,
  RECOVERY_MIN_SPACING_MS,
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
  private onCandle: ((c: Candle, closed: boolean, provisional?: boolean) => void) | null = null
  private onStatus: ((s: ConnectionStatus) => void) | null = null
  private onRepair: ((time: number) => void) | null = null

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
    onCandle: (c: Candle, closed: boolean, provisional?: boolean) => void,
    opts?: CandleSubscribeOptions,
  ): () => void {
    this.subscriptions++
    this.onCandle = onCandle
    this.onStatus = opts?.onStatus ?? null
    this.onRepair = opts?.onRepair ?? null
    return () => {
      this.unsubscribes++
      this.onCandle = null
      this.onStatus = null
      this.onRepair = null
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

  /** A closed bar the source will send once more (Coinbase's pending second read-back). */
  provisionalClose(c: Candle): void {
    this.onCandle?.(c, true, true)
  }

  status(s: ConnectionStatus): void {
    this.onStatus?.(s)
  }

  /** An aggregated source giving up on bar `time` (CandleSubscribeOptions.onRepair). */
  repair(time: number): void {
    this.onRepair?.(time)
  }

  /** The controller passed an onRepair hook. */
  hasRepairHook(): boolean {
    return this.onRepair !== null
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
    const { provider, controller, candles, latest, setClock } = setup()
    controller.start()
    provider.status('live')
    provider.last().resolve(rest)
    await flush()
    expect(provider.requests).toHaveLength(1) // first 'live' is not a reconnect

    provider.status('reconnecting')
    provider.status('offline')
    expect(provider.requests).toHaveLength(1)
    // Back during bar 4H: the gap fill is sent after 3H and 4H opened, so it covers them.
    setClock(4 * H * 1000 + 5_000)
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

  it('a closed tick identical to the bar on screen keeps the array but still counts as closed', async () => {
    const { provider, candles, snapshots } = await liveNow()
    provider.tick(bar(2 * H, 150))
    const before = candles()
    const n = snapshots.length
    expect(snapshots[n - 1].closedThrough).toBe(H)
    // Same values, now final: applyLiveCandle returns the same array, and the one snapshot
    // only moves closedThrough onto the bar (Phase 16: final values are confirmed now)...
    provider.tick(bar(2 * H, 150), true)
    expect(snapshots).toHaveLength(n + 1)
    expect(snapshots[n].load).toBe(snapshots[n - 1].load)
    expect(snapshots[n].closedThrough).toBe(2 * H)
    expect(candles()).toBe(before)
    // ...but the close was recorded, so the rollover needs no repair.
    provider.tick(bar(3 * H, 160))
    expect(provider.requests).toHaveLength(1)
    expect(candles().map((c) => c.time)).toEqual([0, H, 2 * H, 3 * H])
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

describe('onRepair from an aggregated source', () => {
  async function liveNow() {
    const ctx = setup()
    ctx.controller.start()
    ctx.provider.last().resolve(rest) // last REST bar: 2H, forming at load time
    await flush()
    return ctx
  }

  it('subscribes with an onRepair hook', () => {
    const { provider, controller } = setup()
    controller.start()
    expect(provider.hasRepairHook()).toBe(true)
  })

  it('a repair after the load runs one gap fill and the bar takes the REST value', async () => {
    const { provider, candles } = await liveNow()
    // The source withheld 2H (given up); its first emission is the next bar.
    provider.tick(bar(3 * H, 160))
    expect(provider.requests).toHaveLength(1)
    provider.repair(2 * H)
    expect(provider.requests).toHaveLength(2)
    expect(provider.last().endTime).toBeUndefined()
    provider.tick(bar(3 * H, 161)) // during the request: applied on top of its rows
    provider.last().resolve([bar(H, 101), bar(2 * H, 155), bar(3 * H, 158)])
    await flush()
    expect(candles().map((c) => [c.time, c.close])).toEqual([
      [0, 100],
      [H, 101],
      [2 * H, 155],
      [3 * H, 161],
    ])
  })

  it('a repair during a Retry-After block is deferred and runs once when it ends', async () => {
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
      provider.repair(2 * H)
      provider.repair(2 * H)
      expect(provider.requests).toHaveLength(2)
      await vi.advanceTimersByTimeAsync(5_000 - 1)
      expect(provider.requests).toHaveLength(2)
      await vi.advanceTimersByTimeAsync(1)
      expect(provider.requests).toHaveLength(3)
      await vi.advanceTimersByTimeAsync(60_000)
      expect(provider.requests).toHaveLength(3)
      controller.stop()
    } finally {
      vi.useRealTimers()
    }
  })

  it('a repair before the initial load is ready does nothing (the load covers it)', async () => {
    const { provider, controller, candles } = setup()
    controller.start()
    provider.repair(2 * H)
    expect(provider.requests).toHaveLength(1)
    provider.last().resolve(rest)
    await flush()
    expect(provider.requests).toHaveLength(1)
    expect(candles()).toHaveLength(3)
  })

  it('a repair after stop() makes no request', async () => {
    const { provider, controller } = await liveNow()
    // Grab the hook as a source would hold it, then stop: a late call is ignored.
    const hook = (provider as unknown as { onRepair: (t: number) => void }).onRepair
    controller.stop()
    hook(2 * H)
    expect(provider.requests).toHaveLength(1)
  })

  it('one gap fill per repair call: the result itself triggers nothing more', async () => {
    const { provider } = await liveNow()
    provider.tick(bar(3 * H, 160))
    provider.repair(2 * H)
    provider.last().resolve([bar(2 * H, 155), bar(3 * H, 160)])
    await flush()
    expect(provider.requests).toHaveLength(2)
    // Ticks after the result (same bar, then the next one after an x:true) add nothing.
    provider.tick(bar(3 * H, 161))
    provider.tick(bar(3 * H, 162), true)
    provider.tick(bar(4 * H, 170))
    expect(provider.requests).toHaveLength(2)
    // A later repair (the source reports each bar once) gets exactly one more.
    provider.repair(3 * H)
    expect(provider.requests).toHaveLength(3)
  })
})

describe('onRepair while a request is in flight', () => {
  async function liveNow() {
    const ctx = setup()
    ctx.controller.start()
    ctx.provider.last().resolve(rest)
    await flush()
    return ctx
  }

  it('does not abort an in-flight gap fill; one gap fill runs after it lands', async () => {
    const { provider } = await liveNow()
    provider.tick(bar(2 * H, 150))
    provider.tick(bar(3 * H, 160)) // missed close: gap fill #1
    expect(provider.requests).toHaveLength(2)
    const first = provider.last()
    provider.repair(2 * H)
    provider.repair(2 * H)
    expect(first.signal?.aborted).toBe(false)
    expect(provider.requests).toHaveLength(2)
    first.resolve([bar(2 * H, 155), bar(3 * H, 160)])
    await flush()
    expect(provider.requests).toHaveLength(3)
    expect(provider.last().endTime).toBeUndefined()
    provider.last().resolve([bar(2 * H, 156), bar(3 * H, 160)])
    await flush()
    expect(provider.requests).toHaveLength(3)
  })

  it('a request started after the repair covers it (no extra gap fill)', async () => {
    const { provider, controller } = await liveNow()
    provider.tick(bar(2 * H, 150))
    provider.tick(bar(3 * H, 160))
    provider.repair(2 * H)
    controller.reload() // starts after the repair: aborts the gap fill and covers the repair
    expect(provider.requests).toHaveLength(3)
    provider.last().resolve([bar(2 * H, 155), bar(3 * H, 160)])
    await flush()
    expect(provider.requests).toHaveLength(3)
  })

  it('a queued repair behind a failed gap fill rides on its retry', async () => {
    vi.useFakeTimers()
    try {
      const { provider, controller } = setup()
      controller.start()
      provider.last().resolve(rest)
      await vi.advanceTimersByTimeAsync(0)
      provider.tick(bar(2 * H, 150))
      provider.tick(bar(3 * H, 160))
      provider.repair(2 * H)
      provider.last().reject(new MarketDataError('network', 'down'))
      await vi.advanceTimersByTimeAsync(0)
      expect(provider.requests).toHaveLength(2)
      await vi.advanceTimersByTimeAsync(GAP_FILL_RETRY_DELAYS_MS[0])
      expect(provider.requests).toHaveLength(3)
      provider.last().resolve([bar(2 * H, 155), bar(3 * H, 160)])
      await vi.advanceTimersByTimeAsync(60_000)
      expect(provider.requests).toHaveLength(3)
      controller.stop()
    } finally {
      vi.useRealTimers()
    }
  })

  it('a queued repair behind a failed reload runs as a gap-fill retry', async () => {
    vi.useFakeTimers()
    try {
      const { provider, controller } = setup()
      controller.start()
      provider.last().resolve(rest)
      await vi.advanceTimersByTimeAsync(0)
      controller.reload()
      provider.repair(2 * H)
      provider.last().reject(new MarketDataError('network', 'down'))
      await vi.advanceTimersByTimeAsync(GAP_FILL_RETRY_DELAYS_MS[0] - 1)
      expect(provider.requests).toHaveLength(2)
      await vi.advanceTimersByTimeAsync(1)
      expect(provider.requests).toHaveLength(3)
      controller.stop()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('shared rate-limit gate (createTimeframeProvider)', () => {
  it("another chart's 429 blocks this controller's reload and gap fill without a request", async () => {
    vi.useFakeTimers()
    try {
      const base = new FakeProvider()
      const shared = createTimeframeProvider(base)
      const snapshots: LiveCandlesSnapshot[] = []
      const controller = new LiveCandlesController(
        { provider: shared, symbol: 'BTCUSDT', interval: '1h', limit: 3, source: 'Binance' },
        (snap) => snapshots.push(snap),
      )
      controller.start()
      base.last().resolve(rest)
      await vi.advanceTimersByTimeAsync(0)
      // Another panel on the same provider gets a 429.
      const other = shared.getCandles('ETHUSDT', '4h', 500).catch(() => null)
      base.last().reject(new MarketDataError('rate-limited', 'slow down', 429, 30))
      await other
      const calls = base.requests.length
      controller.reload()
      await vi.advanceTimersByTimeAsync(0)
      expect(base.requests).toHaveLength(calls)
      expect(snapshots[snapshots.length - 1].retryBlocked).toBe(true)
      // A reconnect gap fill during the hold is deferred, not sent.
      base.status('reconnecting')
      base.status('live')
      await vi.advanceTimersByTimeAsync(29_000)
      expect(base.requests).toHaveLength(calls)
      await vi.advanceTimersByTimeAsync(1_000)
      expect(base.requests).toHaveLength(calls + 1)
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

describe('custom timeframes (CandleSource)', () => {
  /** A CandleSource (not a full MarketDataProvider) that records the timeframe it is asked for. */
  function sourceSetup(interval: Timeframe, nowMs: number) {
    const provider = new FakeProvider()
    const seen: Timeframe[] = []
    const source: CandleSource = {
      getCandles: (symbol, tf, limit, endTime, opts) => {
        seen.push(tf)
        return provider.getCandles(symbol, '1m', limit, endTime, opts)
      },
      subscribeCandles: (symbol, tf, onCandle, opts) => {
        seen.push(tf)
        return provider.subscribeCandles(symbol, '1m', onCandle, opts)
      },
    }
    const controller = new LiveCandlesController(
      {
        provider: source,
        symbol: 'BTCUSDT',
        interval,
        limit: 3,
        source: 'Binance',
        now: () => nowMs,
      },
      () => {},
    )
    return { provider, controller, seen }
  }

  it('passes the timeframe through to the source', () => {
    const { controller, seen } = sourceSetup('5s', 1_000)
    controller.start()
    expect(seen).toEqual(['5s', '5s'])
    controller.stop()
  })

  it('a quiet reconnect inside the same Binance-aligned 3d bucket skips the gap fill', async () => {
    const D3 = 1_792_713_600 // a live BTCUSDT 3d open (Monday-anchored, not epoch-aligned)
    const nowMs = (D3 + 2 * 86_400) * 1000
    const { provider, controller } = sourceSetup('3d', nowMs)
    controller.start()
    provider.last().resolve([bar(D3 - 259_200, 100), bar(D3, 101)])
    await flush()
    provider.tick(bar(D3, 150))
    provider.status('reconnecting')
    provider.status('live')
    expect(provider.requests).toHaveLength(1)
    controller.stop()
  })

  it('a reconnect after a 5s bucket boundary gap fills', async () => {
    const S = 1_700_000_000
    let nowMs = (S + 2) * 1000
    const provider = new FakeProvider()
    const controller = new LiveCandlesController(
      {
        provider,
        symbol: 'BTCUSDT',
        interval: '5s',
        limit: 3,
        source: 'Binance',
        now: () => nowMs,
      },
      () => {},
    )
    controller.start()
    provider.last().resolve([bar(S - 5, 100), bar(S, 101)])
    await flush()
    provider.tick(bar(S, 150))
    nowMs = (S + 6) * 1000
    provider.status('reconnecting')
    provider.status('live')
    expect(provider.requests).toHaveLength(2)
    controller.stop()
  })
})

describe('closedThrough (the last bar with final values, contiguous)', () => {
  async function loaded(rows: Candle[] = rest) {
    const ctx = setup()
    ctx.controller.start()
    ctx.provider.status('live')
    ctx.provider.last().resolve(rows)
    await flush()
    return ctx
  }

  it('closedThroughOf: the bar before the earliest unconfirmed one, the last bar, or null', () => {
    const c = [bar(0, 1), bar(H, 2), bar(2 * H, 3), bar(3 * H, 4)]
    expect(closedThroughOf([], new Set())).toBeNull()
    expect(closedThroughOf(c, new Set())).toBe(3 * H)
    expect(closedThroughOf(c, new Set([3 * H]))).toBe(2 * H)
    expect(closedThroughOf(c, new Set([3 * H, H]))).toBe(0)
    expect(closedThroughOf(c, new Set([0, 3 * H]))).toBeNull()
  })

  it('is null before the first load and while it fails', async () => {
    const { provider, controller, latest } = setup()
    controller.start()
    expect(latest().closedThrough).toBeNull()
    provider.tick(bar(2 * H, 150), true)
    expect(latest().closedThrough).toBeNull()
    provider.last().reject(new MarketDataError('network', 'down'))
    await flush()
    expect(latest().closedThrough).toBeNull()
  })

  it('REST load: the second-to-last REST row (the newest may still be forming)', async () => {
    const { latest } = await loaded()
    expect(latest().closedThrough).toBe(H)
  })

  it('a single-row REST load has no final bar', async () => {
    const { latest } = await loaded([bar(2 * H, 102)])
    expect(latest().closedThrough).toBeNull()
  })

  it('an x:true tick advances it onto the bar; the next bar opening keeps it there', async () => {
    const { provider, latest } = await loaded()
    provider.tick(bar(2 * H, 150))
    expect(latest().closedThrough).toBe(H)
    provider.tick(bar(2 * H, 151), true)
    expect(latest().closedThrough).toBe(2 * H)
    provider.tick(bar(3 * H, 160))
    expect(latest().closedThrough).toBe(2 * H)
    provider.tick(bar(3 * H, 161), true)
    expect(latest().closedThrough).toBe(3 * H)
  })

  it('an x:true tick that arrived during the first load still counts', async () => {
    const { provider, controller, latest } = setup()
    controller.start()
    provider.tick(bar(2 * H, 150), true)
    provider.last().resolve(rest)
    await flush()
    expect(latest().closedThrough).toBe(2 * H)
  })

  it('a partial bar left by a drop holds it back while ticks for later bars arrive', async () => {
    const { provider, latest, candles } = await loaded()
    provider.tick(bar(2 * H, 150)) // 2H forming
    provider.status('reconnecting')
    provider.status('live') // gap fill in flight
    expect(provider.requests).toHaveLength(2)
    provider.tick(bar(5 * H, 180)) // appended right after the partial 2H
    expect(candles().map((c) => c.time)).toEqual([0, H, 2 * H, 5 * H])
    expect(latest().closedThrough).toBe(H)
    // A closed tick for a later bar cannot jump over the partial one.
    provider.tick(bar(5 * H, 181), true)
    expect(latest().closedThrough).toBe(H)
  })

  it('advances once the gap fill brings the final values (its newest row stays unconfirmed)', async () => {
    const { provider, latest } = await loaded()
    provider.tick(bar(2 * H, 150))
    provider.status('reconnecting')
    provider.status('live')
    provider.tick(bar(5 * H, 180))
    provider.last().resolve([bar(2 * H, 152), bar(3 * H, 153), bar(4 * H, 154), bar(5 * H, 179)])
    await flush()
    expect(latest().closedThrough).toBe(4 * H)
    provider.tick(bar(5 * H, 182), true)
    expect(latest().closedThrough).toBe(5 * H)
  })

  it('a pending x:true tick for the newest gap-fill row makes it final', async () => {
    const { provider, latest } = await loaded()
    provider.tick(bar(2 * H, 150))
    provider.status('reconnecting')
    provider.status('live')
    provider.tick(bar(3 * H, 170), true)
    provider.last().resolve([bar(2 * H, 152), bar(3 * H, 170)])
    await flush()
    expect(latest().closedThrough).toBe(3 * H)
  })

  it('the missed-close repair (rollover without x:true) holds it until the repair lands', async () => {
    const { provider, latest } = await loaded()
    provider.tick(bar(2 * H, 150))
    provider.tick(bar(3 * H, 160)) // 2H never closed: repair requested
    expect(provider.requests).toHaveLength(2)
    expect(latest().closedThrough).toBe(H)
    provider.last().resolve([bar(H, 101), bar(2 * H, 155), bar(3 * H, 158)])
    await flush()
    expect(latest().closedThrough).toBe(2 * H)
  })

  it('a failed gap fill leaves it at the last confirmed bar', async () => {
    const { provider, controller, latest } = await loaded()
    provider.tick(bar(2 * H, 150))
    provider.status('reconnecting')
    provider.status('live')
    provider.tick(bar(5 * H, 180))
    provider.last().reject(new MarketDataError('network', 'down'))
    await flush()
    expect(latest().refreshError).not.toBeNull()
    expect(latest().closedThrough).toBe(H)
    provider.tick(bar(5 * H, 181), true)
    provider.tick(bar(6 * H, 190))
    expect(latest().closedThrough).toBe(H)
    controller.stop() // drops the scheduled retry
  })

  it('a bar final before a reload stays final as the newest row of its result', async () => {
    const { provider, controller, latest } = await loaded()
    provider.tick(bar(2 * H, 151), true)
    controller.reload()
    provider.last().resolve(rest.map((c) => (c.time === 2 * H ? bar(2 * H, 151) : c)))
    await flush()
    expect(latest().closedThrough).toBe(2 * H)
  })

  it('a forming tick that rewrites an older bar makes it unconfirmed again', async () => {
    const { provider, latest } = await loaded()
    provider.tick(bar(2 * H, 150), true)
    provider.tick(bar(3 * H, 160))
    expect(latest().closedThrough).toBe(2 * H)
    provider.tick(bar(2 * H, 151)) // a late partial value replaces the final one on screen
    expect(latest().closedThrough).toBe(H)
  })

  it('older pages are final: a null closedThrough moves to the last prepended bar', async () => {
    const { provider, controller, latest } = await loaded([bar(10 * H, 110)])
    expect(latest().closedThrough).toBeNull()
    controller.loadOlder()
    provider.last().resolve([bar(8 * H, 108), bar(9 * H, 109)])
    await flush()
    expect(latest().closedThrough).toBe(9 * H)
  })

  it('a replaced window forgets the dropped bars', async () => {
    const { provider, latest } = await loaded()
    provider.tick(bar(2 * H, 150))
    provider.status('reconnecting')
    provider.status('live')
    provider.last().resolve([bar(10 * H, 200), bar(11 * H, 201), bar(12 * H, 202)])
    await flush()
    expect(latest().closedThrough).toBe(11 * H)
  })
})

describe('closedThrough: provisional closes (Coinbase read-back pending)', () => {
  async function loaded() {
    const ctx = setup()
    ctx.controller.start()
    ctx.provider.status('live')
    ctx.provider.last().resolve(rest) // 2H newest: closedThrough H
    await flush()
    return ctx
  }

  it('a provisional close is not final; the settling send (same values) makes it final', async () => {
    const { provider, latest } = await loaded()
    provider.tick(bar(2 * H, 150))
    provider.provisionalClose(bar(2 * H, 151))
    expect(latest().closedThrough).toBe(H)
    provider.tick(bar(3 * H, 160)) // the next bar opens before the second read-back
    expect(latest().closedThrough).toBe(H)
    // A provisional close is still a close: the rollover asks for no repair.
    expect(provider.requests).toHaveLength(1)
    provider.tick(bar(2 * H, 151), true) // settled, unchanged
    expect(latest().closedThrough).toBe(2 * H)
    expect(provider.requests).toHaveLength(1)
  })

  it('a corrected settling send replaces the provisional values and makes the bar final', async () => {
    const { provider, latest, candles } = await loaded()
    provider.provisionalClose(bar(2 * H, 151))
    provider.tick(bar(3 * H, 160))
    provider.tick(bar(2 * H, 149), true)
    expect(candles().find((c) => c.time === 2 * H)?.close).toBe(149)
    expect(latest().closedThrough).toBe(2 * H)
  })

  it('a provisional close received during a request is re-applied as provisional', async () => {
    const { provider, controller, latest } = await loaded()
    controller.reload()
    provider.provisionalClose(bar(2 * H, 151))
    provider.last().resolve(rest)
    await flush()
    expect(latest().closedThrough).toBe(H)
    provider.tick(bar(2 * H, 151), true)
    expect(latest().closedThrough).toBe(2 * H)
  })

  it('a provisional close on a bar that was final makes it not final again', async () => {
    const { provider, latest } = await loaded()
    provider.tick(bar(2 * H, 151), true)
    expect(latest().closedThrough).toBe(2 * H)
    provider.provisionalClose(bar(2 * H, 152))
    expect(latest().closedThrough).toBe(H)
  })
})

describe('closedThrough: hole cap after a drop (bars may be missing)', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  const rows = [bar(0, 100), bar(H, 101), bar(2 * H, 102), bar(3 * H, 103), bar(4 * H, 104)]
  const down = () => new MarketDataError('network', 'down')

  /** Bars 0..4H loaded, bar 4H final (x:true), then a drop; reconnect at 7H+5s: gap fill. */
  async function droppedAfterFinal4() {
    const ctx = setup(5)
    ctx.setClock(4 * H * 1000 + 10_000)
    ctx.controller.start()
    ctx.provider.status('live')
    ctx.provider.last().resolve(rows)
    await vi.advanceTimersByTimeAsync(0)
    ctx.provider.tick(bar(4 * H, 104), true)
    expect(ctx.latest().closedThrough).toBe(4 * H)
    ctx.provider.status('reconnecting')
    ctx.setClock(7 * H * 1000 + 5_000)
    ctx.provider.status('live')
    expect(ctx.provider.requests).toHaveLength(2)
    return ctx
  }

  /** Fail the current gap fill and every automatic retry. */
  async function exhaustRetries(provider: FakeProvider) {
    provider.last().reject(down())
    await vi.advanceTimersByTimeAsync(0)
    for (const delay of GAP_FILL_RETRY_DELAYS_MS) {
      await vi.advanceTimersByTimeAsync(delay)
      provider.last().reject(down())
      await vi.advanceTimersByTimeAsync(0)
    }
    expect(provider.requests).toHaveLength(5) // load + gap fill + 3 retries
    await vi.advanceTimersByTimeAsync(120_000)
    expect(provider.requests).toHaveLength(5)
  }

  it('probe: retries give up, bars 5H and 6H never arrive, bar 7H closes: stays at 4H', async () => {
    const { provider, controller, latest, candles } = await droppedAfterFinal4()
    await exhaustRetries(provider)
    provider.tick(bar(7 * H, 170)) // opens 7H right after 4H (one recovery gap fill, below)
    provider.tick(bar(7 * H, 171), true)
    expect(candles().map((c) => c.time)).toEqual([0, H, 2 * H, 3 * H, 4 * H, 7 * H])
    // Contiguous over the bars present would say 7H; the hole 5H..6H caps it at 4H.
    expect(closedThroughOf(candles(), new Set())).toBe(7 * H)
    expect(latest().closedThrough).toBe(4 * H)
    // The recovery gap fill fails too: still capped, and no automatic retry of it.
    expect(provider.requests).toHaveLength(6)
    provider.last().reject(down())
    await vi.advanceTimersByTimeAsync(120_000)
    expect(provider.requests).toHaveLength(6)
    expect(latest().closedThrough).toBe(4 * H)
    controller.stop()
  })

  it('a gap fill that covers the hole lifts the cap', async () => {
    const { provider, controller, latest, candles } = await droppedAfterFinal4()
    provider.tick(bar(7 * H, 171), true)
    expect(latest().closedThrough).toBe(4 * H)
    provider.last().resolve([bar(4 * H, 104), bar(5 * H, 105), bar(6 * H, 106), bar(7 * H, 107)])
    await vi.advanceTimersByTimeAsync(0)
    // 7H's pending x:true tick is re-applied on top of the newest row: final.
    expect(candles().map((c) => c.time)).toEqual([0, H, 2 * H, 3 * H, 4 * H, 5 * H, 6 * H, 7 * H])
    expect(latest().closedThrough).toBe(7 * H)
    controller.stop()
  })

  it('a window that starts after the cap (on a bar appended since) replaces instead of merging', async () => {
    const { provider, controller, latest, candles } = await droppedAfterFinal4()
    provider.tick(bar(7 * H, 171), true)
    provider.last().resolve([bar(7 * H, 171), bar(8 * H, 180)])
    await vi.advanceTimersByTimeAsync(0)
    // Merging would keep 0..4H, 7H, 8H with 5H and 6H silently missing.
    expect(candles().map((c) => c.time)).toEqual([7 * H, 8 * H])
    expect(latest().closedThrough).toBe(7 * H)
    controller.stop()
  })

  it('a failed gap fill keeps the cap; a later successful reload lifts it', async () => {
    const { provider, controller, latest } = await droppedAfterFinal4()
    provider.tick(bar(7 * H, 171), true)
    provider.last().reject(down())
    await vi.advanceTimersByTimeAsync(0)
    expect(latest().closedThrough).toBe(4 * H)
    controller.reload() // supersedes the scheduled retry
    provider
      .last()
      .resolve([
        bar(3 * H, 103),
        bar(4 * H, 104),
        bar(5 * H, 105),
        bar(6 * H, 106),
        bar(7 * H, 171),
      ])
    await vi.advanceTimersByTimeAsync(0)
    // The newest REST row (7H) was final on screen before the reload: it stays final.
    expect(latest().closedThrough).toBe(7 * H)
    controller.stop()
  })

  it('a repair for a bar never delivered caps it until a later request lands', async () => {
    vi.useRealTimers()
    const { provider, controller, latest, setClock } = setup()
    controller.start()
    provider.status('live')
    provider.last().resolve(rest)
    await flush()
    provider.tick(bar(2 * H, 150), true)
    expect(latest().closedThrough).toBe(2 * H)
    setClock(4 * H * 1000 + 5_000) // 3H ended; 4H opened
    provider.repair(3 * H) // 3H will never be emitted
    expect(provider.requests).toHaveLength(2)
    provider.tick(bar(4 * H, 170))
    provider.tick(bar(4 * H, 171), true) // appended right after 2H
    expect(latest().closedThrough).toBe(2 * H)
    provider.last().resolve([bar(2 * H, 150), bar(3 * H, 160), bar(4 * H, 171)])
    await flush()
    expect(latest().closedThrough).toBe(4 * H)
    controller.stop()
  })

  it('closedThroughOf with a cap: the last bar at or before it', () => {
    const c = [bar(0, 1), bar(H, 2), bar(4 * H, 3), bar(7 * H, 4)]
    expect(closedThroughOf(c, new Set(), 4 * H)).toBe(4 * H)
    expect(closedThroughOf(c, new Set(), 5 * H)).toBe(4 * H)
    expect(closedThroughOf(c, new Set([H]), 5 * H)).toBe(0)
    expect(closedThroughOf(c, new Set(), -1)).toBeNull()
    expect(closedThroughOf(c, new Set(), Number.NEGATIVE_INFINITY)).toBeNull()
  })
})

describe('closedThrough: automatic recovery when it is stuck', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  const down = () => new MarketDataError('network', 'down')

  /**
   * Loaded, 2H forming; a drop; reconnect at 3H+5s (gap fill); 3H opens during it (the
   * missed-close repair replaces it); that repair and its 3 retries all fail; 3H closes.
   * 2H stays partial, so closedThrough is stuck at H.
   */
  async function retriesExhausted() {
    const ctx = setup()
    ctx.setClock(2 * H * 1000 + 10_000)
    ctx.controller.start()
    ctx.provider.status('live')
    ctx.provider.last().resolve(rest)
    await vi.advanceTimersByTimeAsync(0)
    ctx.provider.tick(bar(2 * H, 150)) // 2H partial
    ctx.provider.status('reconnecting')
    ctx.setClock(3 * H * 1000 + 5_000)
    ctx.provider.status('live')
    expect(ctx.provider.requests).toHaveLength(2)
    ctx.provider.tick(bar(3 * H, 160)) // 2H never closed: the repair supersedes the gap fill
    expect(ctx.provider.requests).toHaveLength(3)
    ctx.provider.last().reject(down())
    await vi.advanceTimersByTimeAsync(0)
    for (const delay of GAP_FILL_RETRY_DELAYS_MS) {
      await vi.advanceTimersByTimeAsync(delay)
      ctx.provider.last().reject(down())
      await vi.advanceTimersByTimeAsync(0)
    }
    expect(ctx.provider.requests).toHaveLength(6)
    await vi.advanceTimersByTimeAsync(120_000)
    expect(ctx.provider.requests).toHaveLength(6) // given up
    ctx.provider.tick(bar(3 * H, 161), true)
    expect(ctx.latest().closedThrough).toBe(H)
    return ctx
  }

  it('after the retries gave up, the next bar opening runs one gap fill; success advances it', async () => {
    const { provider, controller, latest, setClock } = await retriesExhausted()
    setClock(4 * H * 1000 + 5_000)
    provider.tick(bar(4 * H, 170))
    expect(provider.requests).toHaveLength(7)
    provider.tick(bar(4 * H, 171))
    provider.tick(bar(4 * H, 172))
    expect(provider.requests).toHaveLength(7) // ticks on the same bar add nothing
    provider.last().resolve([bar(2 * H, 152), bar(3 * H, 161), bar(4 * H, 172)])
    await vi.advanceTimersByTimeAsync(0)
    expect(latest().refreshError).toBeNull()
    expect(latest().closedThrough).toBe(3 * H)
    // Healthy again: the next bars open without any request.
    provider.tick(bar(4 * H, 173), true)
    setClock(5 * H * 1000 + 5_000)
    provider.tick(bar(5 * H, 180))
    expect(provider.requests).toHaveLength(7)
    expect(latest().closedThrough).toBe(4 * H)
    controller.stop()
  })

  it('no request storm: one recovery per bar interval, a failed one is not retried on a timer', async () => {
    const { provider, controller, setClock } = await retriesExhausted()
    const at = 4 * H * 1000 + 5_000
    setClock(at)
    provider.tick(bar(4 * H, 170))
    expect(provider.requests).toHaveLength(7)
    provider.last().reject(down())
    await vi.advanceTimersByTimeAsync(120_000)
    expect(provider.requests).toHaveLength(7)
    provider.tick(bar(4 * H, 171), true)
    // A bar opening less than one interval after the last recovery: nothing.
    setClock(at + H * 1000 - 1)
    provider.tick(bar(5 * H, 180))
    expect(provider.requests).toHaveLength(7)
    provider.tick(bar(5 * H, 181), true)
    setClock(at + H * 1000)
    provider.tick(bar(6 * H, 190))
    expect(provider.requests).toHaveLength(8)
    controller.stop()
  })

  it('a repair queued behind a failed recovery does not block later recoveries', async () => {
    const { provider, controller, latest, setClock } = await retriesExhausted()
    setClock(4 * H * 1000 + 5_000)
    provider.tick(bar(4 * H, 170)) // recovery (retries already used up)
    expect(provider.requests).toHaveLength(7)
    provider.repair(3 * H) // while the recovery is in flight: queued behind it
    provider.last().reject(down()) // no retry left: the queued repair must not stay pending
    await vi.advanceTimersByTimeAsync(120_000)
    expect(provider.requests).toHaveLength(7)
    provider.tick(bar(4 * H, 171), true)
    expect(latest().closedThrough).toBe(H)
    // The next bar one spacing later runs one recovery (stuck behind 2H and the repair cap).
    setClock(5 * H * 1000 + 5_000)
    provider.tick(bar(5 * H, 180))
    expect(provider.requests).toHaveLength(8)
    provider.tick(bar(5 * H, 181))
    expect(provider.requests).toHaveLength(8)
    provider.last().reject(down())
    await vi.advanceTimersByTimeAsync(0)
    provider.tick(bar(5 * H, 182), true)
    // Less than one interval after that recovery: nothing; one interval later: one more.
    setClock(6 * H * 1000 + 4_000)
    provider.tick(bar(6 * H, 190))
    expect(provider.requests).toHaveLength(8)
    provider.tick(bar(6 * H, 191), true)
    setClock(7 * H * 1000 + 5_000)
    provider.tick(bar(7 * H, 200))
    expect(provider.requests).toHaveLength(9)
    provider
      .last()
      .resolve([
        bar(2 * H, 152),
        bar(3 * H, 161),
        bar(4 * H, 171),
        bar(5 * H, 182),
        bar(6 * H, 191),
        bar(7 * H, 200),
      ])
    await vi.advanceTimersByTimeAsync(0)
    expect(latest().closedThrough).toBe(6 * H)
    controller.stop()
  })

  it('never while a gap-fill retry is scheduled or a Retry-After block is active', async () => {
    const { provider, controller, setClock, latest } = setup()
    setClock(2 * H * 1000 + 10_000)
    controller.start()
    provider.status('live')
    provider.last().resolve(rest)
    await vi.advanceTimersByTimeAsync(0)
    provider.tick(bar(2 * H, 150))
    provider.status('reconnecting')
    setClock(3 * H * 1000 + 5_000)
    provider.status('live') // hole cap at 2H
    provider.last().reject(down())
    await vi.advanceTimersByTimeAsync(0)
    provider.tick(bar(2 * H, 151), true) // late close: the rollover needs no repair
    provider.tick(bar(3 * H, 160)) // cap active, but a retry is scheduled: no recovery
    expect(provider.requests).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(GAP_FILL_RETRY_DELAYS_MS[0])
    expect(provider.requests).toHaveLength(3)
    provider.last().reject(new MarketDataError('rate-limited', '429', 429, 600))
    await vi.advanceTimersByTimeAsync(0)
    expect(latest().retryBlocked).toBe(true)
    provider.tick(bar(3 * H, 161), true)
    setClock(4 * H * 1000 + 5_000)
    provider.tick(bar(4 * H, 170)) // blocked: no recovery
    expect(provider.requests).toHaveLength(3)
    controller.stop()
  })

  it('a missed x:true right after a REST apply is repaired when the bar after next opens', async () => {
    const { provider, controller, latest, setClock } = setup()
    setClock(3 * H * 1000 + 5_000)
    controller.start()
    provider.status('live')
    provider.last().resolve(rest) // 2H is the newest row (not final)
    await vi.advanceTimersByTimeAsync(0)
    provider.tick(bar(3 * H, 160)) // the first tick after REST is on 3H: 2H's x:true was missed
    provider.tick(bar(3 * H, 161), true)
    expect(provider.requests).toHaveLength(1)
    expect(latest().closedThrough).toBe(H)
    setClock(4 * H * 1000 + 5_000)
    provider.tick(bar(4 * H, 170)) // 2H (older than the previous last bar 3H) is still stuck
    expect(provider.requests).toHaveLength(2)
    provider.last().resolve([bar(2 * H, 152), bar(3 * H, 161), bar(4 * H, 170)])
    await vi.advanceTimersByTimeAsync(0)
    expect(latest().closedThrough).toBe(3 * H)
    controller.stop()
  })

  it('short bars: recoveries are at least RECOVERY_MIN_SPACING_MS apart, not one per bar', async () => {
    const provider = new FakeProvider()
    let clock = 10_000
    const controller = new LiveCandlesController(
      {
        provider,
        symbol: 'BTCUSDT',
        interval: '1s',
        limit: 3,
        source: 'Binance',
        now: () => clock,
      },
      () => {},
    )
    controller.start()
    provider.status('live')
    provider.last().resolve([bar(7, 1), bar(8, 2), bar(9, 3)]) // 9 newest: not final
    await vi.advanceTimersByTimeAsync(0)
    provider.tick(bar(10, 4)) // 9's close was missed (first tick after REST)
    provider.tick(bar(10, 4), true)
    // Every later second opens a bar while 9 is stuck; each recovery fails at once.
    let failed = 1
    for (let t = 11; t <= 70; t++) {
      clock = t * 1000
      provider.tick(bar(t, 5))
      if (provider.requests.length > failed) {
        failed = provider.requests.length
        provider.last().reject(new MarketDataError('network', 'down'))
        await vi.advanceTimersByTimeAsync(0)
      }
      provider.tick(bar(t, 5), true)
    }
    // Recoveries at 11 s and 41 s (11 + 30): 60 bars opened, 2 requests.
    expect(RECOVERY_MIN_SPACING_MS).toBe(30_000)
    expect(provider.requests).toHaveLength(3)
    controller.stop()
  })
})

describe('closedThrough: bars skipped without a reported drop (Binance idle reconnect)', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  const rows = [bar(0, 100), bar(H, 101), bar(2 * H, 102), bar(3 * H, 103), bar(4 * H, 104)]
  const down = () => new MarketDataError('network', 'down')

  /** 0..4H loaded at 4H+10s, 4H's x:true arrives: closedThrough 4H, status 'live' throughout. */
  async function final4() {
    const ctx = setup(5)
    ctx.setClock(4 * H * 1000 + 10_000)
    ctx.controller.start()
    ctx.provider.status('live')
    ctx.provider.last().resolve(rows)
    await vi.advanceTimersByTimeAsync(0)
    ctx.provider.tick(bar(4 * H, 104), true)
    expect(ctx.latest().closedThrough).toBe(4 * H)
    return ctx
  }

  it('P4: a tick that skips 5H caps at 4H and runs one gap fill; its result lifts the cap', async () => {
    const { provider, controller, latest, candles, setClock } = await final4()
    // The socket went quiet and reconnected silently: 5H was never streamed.
    setClock(6 * H * 1000 + 5_000)
    provider.tick(bar(6 * H, 160))
    expect(provider.requests).toHaveLength(2)
    provider.tick(bar(6 * H, 161), true)
    expect(provider.requests).toHaveLength(2)
    // Before the fix: [0..4H, 6H] with closedThrough 6H, computed across the missing 5H.
    expect(candles().map((c) => c.time)).toEqual([0, H, 2 * H, 3 * H, 4 * H, 6 * H])
    expect(latest().closedThrough).toBe(4 * H)
    provider.last().resolve([bar(4 * H, 104), bar(5 * H, 105), bar(6 * H, 161)])
    await vi.advanceTimersByTimeAsync(0)
    expect(candles().map((c) => c.time)).toEqual([0, H, 2 * H, 3 * H, 4 * H, 5 * H, 6 * H])
    expect(latest().closedThrough).toBe(6 * H)
    controller.stop()
  })

  it('a skipped interval without trades is settled by that one REST call', async () => {
    const { provider, controller, latest, setClock, snapshots } = await final4()
    setClock(6 * H * 1000 + 5_000)
    provider.tick(bar(6 * H, 160), true)
    provider.last().resolve([bar(4 * H, 104), { ...bar(5 * H, 104), volume: 0 }, bar(6 * H, 160)])
    await vi.advanceTimersByTimeAsync(0)
    const load = snapshots[snapshots.length - 1].load
    expect(load.kind === 'ready' ? load.bars.map((c) => c.time) : []).toEqual([
      0,
      H,
      2 * H,
      3 * H,
      4 * H,
      6 * H,
    ])
    expect(latest().closedThrough).toBe(6 * H)
    expect(provider.requests).toHaveLength(2)
    controller.stop()
  })

  it('a failed skip gap fill keeps the cap and is not retried on a timer', async () => {
    const { provider, controller, latest, setClock } = await final4()
    setClock(6 * H * 1000 + 5_000)
    provider.tick(bar(6 * H, 160), true)
    provider.last().reject(down())
    await vi.advanceTimersByTimeAsync(120_000)
    expect(provider.requests).toHaveLength(2)
    expect(latest().closedThrough).toBe(4 * H)
    controller.stop()
  })

  it('consecutive bars are not a skip: no request', async () => {
    const { provider, controller, latest, setClock } = await final4()
    setClock(5 * H * 1000 + 5_000)
    provider.tick(bar(5 * H, 150), true)
    setClock(6 * H * 1000 + 5_000)
    provider.tick(bar(6 * H, 160))
    expect(provider.requests).toHaveLength(1)
    expect(latest().closedThrough).toBe(5 * H)
    controller.stop()
  })

  it('a skip while an older request is in flight queues one gap fill behind it', async () => {
    const { provider, controller, latest, setClock } = await final4()
    setClock(4 * H * 1000 + 20_000)
    controller.reload() // sent during 4H: it cannot hold 5H
    setClock(6 * H * 1000 + 5_000)
    provider.tick(bar(6 * H, 160), true)
    expect(provider.requests).toHaveLength(2)
    provider.last().resolve(rows)
    await vi.advanceTimersByTimeAsync(0)
    // The reload landed but was sent before the skip: still capped, and the queued fill runs.
    expect(latest().closedThrough).toBe(4 * H)
    expect(provider.requests).toHaveLength(3)
    provider.last().resolve([bar(4 * H, 104), bar(5 * H, 105), bar(6 * H, 160)])
    await vi.advanceTimersByTimeAsync(0)
    expect(latest().closedThrough).toBe(6 * H)
    controller.stop()
  })

  it('a skip during a Retry-After block waits for the block, then fills once', async () => {
    const { provider, controller, latest, setClock } = await final4()
    controller.reload()
    provider.last().reject(new MarketDataError('rate-limited', '429', 429, 30))
    await vi.advanceTimersByTimeAsync(0)
    expect(latest().retryBlocked).toBe(true)
    setClock(6 * H * 1000 + 5_000)
    provider.tick(bar(6 * H, 160), true)
    expect(provider.requests).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(provider.requests).toHaveLength(3)
    provider.last().resolve([bar(4 * H, 104), bar(5 * H, 105), bar(6 * H, 160)])
    await vi.advanceTimersByTimeAsync(0)
    expect(latest().closedThrough).toBe(6 * H)
    controller.stop()
  })

  it('short bars: skip gap fills are at least RECOVERY_MIN_SPACING_MS apart', async () => {
    const provider = new FakeProvider()
    let clock = 10_000
    const controller = new LiveCandlesController(
      {
        provider,
        symbol: 'BTCUSDT',
        interval: '1s',
        limit: 3,
        source: 'Binance',
        now: () => clock,
      },
      () => {},
    )
    controller.start()
    provider.status('live')
    provider.last().resolve([bar(7, 1), bar(8, 2), bar(9, 3)])
    await vi.advanceTimersByTimeAsync(0)
    provider.tick(bar(9, 3), true)
    // Every other second is skipped; each fill fails at once.
    let made = 1
    for (let t = 11; t <= 71; t += 2) {
      clock = t * 1000 + 500
      provider.tick(bar(t, 5), true)
      if (provider.requests.length > made) {
        made = provider.requests.length
        provider.last().reject(new MarketDataError('network', 'down'))
        await vi.advanceTimersByTimeAsync(0)
      }
    }
    // 31 skips in 60 s: fills at 11.5 s, 41.5 s and 71.5 s.
    expect(provider.requests).toHaveLength(4)
    controller.stop()
  })
})

describe('closedThrough: hole cap guards', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  const rows = [bar(0, 100), bar(H, 101), bar(2 * H, 102), bar(3 * H, 103), bar(4 * H, 104)]
  const down = () => new MarketDataError('network', 'down')

  async function final4() {
    const ctx = setup(5)
    ctx.setClock(4 * H * 1000 + 10_000)
    ctx.controller.start()
    ctx.provider.status('live')
    ctx.provider.last().resolve(rows)
    await vi.advanceTimersByTimeAsync(0)
    ctx.provider.tick(bar(4 * H, 104), true)
    return ctx
  }

  it('a second drop after a failed gap fill keeps the lower cap', async () => {
    const { provider, controller, latest, setClock } = await final4()
    provider.status('reconnecting')
    setClock(7 * H * 1000 + 5_000)
    provider.status('live') // cap 4H
    provider.tick(bar(7 * H, 171), true) // appended after the possible hole
    provider.last().reject(down())
    await vi.advanceTimersByTimeAsync(0)
    provider.status('reconnecting')
    setClock(8 * H * 1000 + 5_000)
    provider.status('live') // the last bar is now 7H: a cap at 7H must not replace 4H
    expect(provider.requests).toHaveLength(3) // this reconnect needed its own gap fill
    expect(latest().closedThrough).toBe(4 * H)
    controller.stop()
  })

  it('a request in flight when the cap was set does not lift it', async () => {
    const { provider, controller, latest, setClock } = await final4()
    setClock(5 * H * 1000 + 5_000)
    controller.reload() // in flight
    provider.repair(4 * H) // cap at 4H - 1: closedThrough 3H
    expect(latest().closedThrough).toBe(3 * H)
    expect(provider.requests).toHaveLength(2)
    provider.last().resolve([bar(3 * H, 103), bar(4 * H, 104), bar(5 * H, 150)])
    await vi.advanceTimersByTimeAsync(0)
    expect(latest().closedThrough).toBe(3 * H)
    // The repair queued behind it lands and lifts the cap.
    expect(provider.requests).toHaveLength(3)
    provider.last().resolve([bar(3 * H, 103), bar(4 * H, 104), bar(5 * H, 150)])
    await vi.advanceTimersByTimeAsync(0)
    expect(latest().closedThrough).toBe(4 * H)
    controller.stop()
  })

  it('an empty REST result does not lift the cap', async () => {
    const { provider, controller, latest, setClock } = await final4()
    provider.status('reconnecting')
    setClock(7 * H * 1000 + 5_000)
    provider.status('live')
    provider.tick(bar(7 * H, 171), true)
    provider.last().resolve([])
    await vi.advanceTimersByTimeAsync(0)
    expect(latest().closedThrough).toBe(4 * H)
    controller.stop()
  })

  it('onRepair caps at the bar before the repaired one', async () => {
    const { provider, controller, latest, setClock } = await final4()
    setClock(5 * H * 1000 + 5_000)
    provider.tick(bar(5 * H, 150), true) // 5H on screen and closed, but the source gives up on it
    expect(latest().closedThrough).toBe(5 * H)
    provider.repair(5 * H)
    expect(latest().closedThrough).toBe(4 * H)
    controller.stop()
  })

  it('a provisional close held before the first load stays provisional', async () => {
    const { provider, controller, latest } = setup()
    controller.start()
    provider.status('live')
    provider.provisionalClose(bar(2 * H, 151)) // before REST lands: held in pending
    provider.last().resolve(rest)
    await vi.advanceTimersByTimeAsync(0)
    expect(latest().closedThrough).toBe(H)
    provider.tick(bar(2 * H, 151), true) // settled
    expect(latest().closedThrough).toBe(2 * H)
    controller.stop()
  })
})
