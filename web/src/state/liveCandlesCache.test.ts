import { afterEach, describe, expect, it, vi } from 'vitest'
import type { CandleSource } from '../data/timeframeProvider'
import type { Candle } from '../types'
import { initialSnapshot, type LiveCandlesSnapshot } from './liveCandles'
import {
  LiveCandlesCache,
  liveCandlesKey,
  type LiveCandlesLease,
  type LiveCandlesParams,
} from './liveCandlesCache'

const bar = (time: number, close = 100): Candle => ({
  time,
  open: close,
  high: close + 1,
  low: close - 1,
  close,
  volume: 1,
})

/** A provider whose REST loads are resolved by hand and whose socket is a spy. */
function fakeSource() {
  const loads: {
    resolve: (c: Candle[]) => void
    signal: AbortSignal | undefined
    limit: number | undefined
  }[] = []
  const unsubscribes: ReturnType<typeof vi.fn>[] = []
  let push: ((c: Candle, closed: boolean) => void) | null = null
  const getCandles = vi.fn(
    (_s: string, _tf: string, limit?: number, _end?: number, opts?: { signal?: AbortSignal }) =>
      new Promise<Candle[]>((resolve) => {
        loads.push({ resolve, signal: opts?.signal, limit })
      }),
  )
  const subscribeCandles = vi.fn(
    (_s: string, _tf: string, onCandle: (c: Candle, closed: boolean) => void) => {
      push = onCandle
      const unsub = vi.fn()
      unsubscribes.push(unsub)
      return unsub as () => void
    },
  )
  const provider: CandleSource = { getCandles, subscribeCandles }
  return {
    provider,
    getCandles,
    subscribeCandles,
    loads,
    unsubscribes,
    tick: (c: Candle, closed = false) => push?.(c, closed),
  }
}

const params = (provider: CandleSource, extra: Partial<LiveCandlesParams> = {}) => ({
  provider,
  symbol: 'BTCUSDT',
  interval: '1h' as const,
  limit: 500,
  source: 'Binance',
  maxBars: 20_000 as number | undefined,
  ...extra,
})

function recorder() {
  const seen: LiveCandlesSnapshot[] = []
  const leases: LiveCandlesLease[] = []
  const listener = (s: LiveCandlesSnapshot, l: LiveCandlesLease) => {
    seen.push(s)
    leases.push(l)
  }
  return { seen, leases, listener, last: () => seen[seen.length - 1] }
}

/** Let promise callbacks run (resolved REST loads). */
const flush = () => new Promise<void>((r) => setTimeout(r, 0))

afterEach(() => {
  vi.useRealTimers()
})

describe('liveCandlesKey', () => {
  it('keys on provider identity and every load parameter', () => {
    const a = fakeSource().provider
    const b = fakeSource().provider
    const k = liveCandlesKey(params(a))
    expect(liveCandlesKey(params(a))).toBe(k)
    expect(liveCandlesKey(params(b))).not.toBe(k)
    expect(liveCandlesKey(params(a, { symbol: 'ETHUSDT' }))).not.toBe(k)
    expect(liveCandlesKey(params(a, { interval: '4h' }))).not.toBe(k)
    expect(liveCandlesKey(params(a, { limit: 1000 }))).not.toBe(k)
    expect(liveCandlesKey(params(a, { source: 'Coinbase' }))).not.toBe(k)
    expect(liveCandlesKey(params(a, { maxBars: undefined }))).not.toBe(k)
    expect(liveCandlesKey(params(a, { maxBars: 5000 }))).not.toBe(k)
  })
})

describe('LiveCandlesCache', () => {
  it('two acquires of the same market share one socket and one REST load', async () => {
    const src = fakeSource()
    const cache = new LiveCandlesCache()
    const a = recorder()
    const b = recorder()
    cache.acquire(params(src.provider), a.listener)
    cache.acquire(params(src.provider), b.listener)
    expect(cache.size).toBe(1)
    expect(src.subscribeCandles).toHaveBeenCalledTimes(1)
    expect(src.getCandles).toHaveBeenCalledTimes(1)
    expect(a.seen).toEqual([initialSnapshot])
    expect(b.seen).toEqual([initialSnapshot])

    src.loads[0].resolve([bar(3600), bar(7200)])
    await flush()
    expect(a.last().load.kind).toBe('ready')
    // Both panels get the very same snapshot object.
    expect(b.last()).toBe(a.last())

    src.tick(bar(7200, 105))
    expect(a.last()).toBe(b.last())
    const load = a.last().load
    expect(load.kind === 'ready' ? load.candles[1].close : null).toBe(105)
  })

  it('different limit or maxBars (or symbol, interval, provider) never share', () => {
    const src = fakeSource()
    const other = fakeSource()
    const cache = new LiveCandlesCache()
    const l = recorder().listener
    cache.acquire(params(src.provider), l)
    cache.acquire(params(src.provider, { limit: 1000 }), l)
    cache.acquire(params(src.provider, { maxBars: undefined }), l)
    cache.acquire(params(src.provider, { maxBars: 5000 }), l)
    cache.acquire(params(src.provider, { interval: '4h' }), l)
    cache.acquire(params(src.provider, { symbol: 'ETHUSDT' }), l)
    cache.acquire(params(other.provider), l)
    expect(cache.size).toBe(7)
    expect(src.subscribeCandles).toHaveBeenCalledTimes(6)
    expect(src.getCandles).toHaveBeenCalledTimes(6)
    expect(src.loads.map((x) => x.limit)).toEqual([500, 1000, 500, 500, 500, 500])
    expect(other.subscribeCandles).toHaveBeenCalledTimes(1)
  })

  it('a late subscriber gets the current snapshot immediately', async () => {
    const src = fakeSource()
    const cache = new LiveCandlesCache()
    const a = recorder()
    cache.acquire(params(src.provider), a.listener)
    src.loads[0].resolve([bar(3600)])
    await flush()
    const current = a.last()
    expect(current.load.kind).toBe('ready')

    const b = recorder()
    const lease = cache.acquire(params(src.provider), b.listener)
    expect(b.seen).toEqual([current])
    expect(b.seen[0]).toBe(current)
    expect(lease.getSnapshot()).toBe(current)
    expect(b.leases[0]).toBe(lease)
    expect(src.getCandles).toHaveBeenCalledTimes(1)
  })

  it('release of one subscriber keeps the controller running for the other', async () => {
    vi.useFakeTimers()
    const src = fakeSource()
    const cache = new LiveCandlesCache()
    const a = recorder()
    const b = recorder()
    const la = cache.acquire(params(src.provider), a.listener)
    cache.acquire(params(src.provider), b.listener)
    la.release()
    la.release() // idempotent
    await vi.advanceTimersByTimeAsync(10)
    expect(src.unsubscribes[0]).not.toHaveBeenCalled()
    expect(src.loads[0].signal?.aborted).toBe(false)
    const before = a.seen.length
    src.loads[0].resolve([bar(3600)])
    await vi.advanceTimersByTimeAsync(0)
    expect(a.seen).toHaveLength(before) // released: no more snapshots
    expect(b.last().load.kind).toBe('ready')
    // A released lease cannot drive the shared controller.
    la.reload()
    expect(src.getCandles).toHaveBeenCalledTimes(1)
  })

  it('release to zero stops it: socket closed, request aborted, no emits after', async () => {
    vi.useFakeTimers()
    const src = fakeSource()
    const cache = new LiveCandlesCache()
    const a = recorder()
    const lease = cache.acquire(params(src.provider), a.listener)
    lease.release()
    // Stopped after the (zero) grace delay, one macrotask later.
    expect(src.unsubscribes[0]).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(0)
    expect(src.unsubscribes[0]).toHaveBeenCalledTimes(1)
    expect(src.loads[0].signal?.aborted).toBe(true)
    expect(cache.size).toBe(0)
    const count = a.seen.length
    src.loads[0].resolve([bar(3600)])
    src.tick(bar(3600, 101))
    await vi.advanceTimersByTimeAsync(10)
    expect(a.seen).toHaveLength(count)

    // The next acquire starts a fresh controller.
    const b = recorder()
    cache.acquire(params(src.provider), b.listener)
    expect(src.subscribeCandles).toHaveBeenCalledTimes(2)
    expect(src.getCandles).toHaveBeenCalledTimes(2)
    expect(b.seen).toEqual([initialSnapshot])
  })

  it('StrictMode acquire/release/acquire reuses the running controller', async () => {
    vi.useFakeTimers()
    const src = fakeSource()
    const cache = new LiveCandlesCache()
    const first = recorder()
    const second = recorder()
    const l1 = cache.acquire(params(src.provider), first.listener)
    l1.release()
    const l2 = cache.acquire(params(src.provider), second.listener)
    await vi.advanceTimersByTimeAsync(50)
    expect(src.subscribeCandles).toHaveBeenCalledTimes(1)
    expect(src.getCandles).toHaveBeenCalledTimes(1)
    expect(src.unsubscribes[0]).not.toHaveBeenCalled()
    expect(second.seen).toEqual([initialSnapshot])
    src.loads[0].resolve([bar(3600)])
    await vi.advanceTimersByTimeAsync(0)
    expect(second.last().load.kind).toBe('ready')
    expect(first.seen).toEqual([initialSnapshot])
    l2.release()
    await vi.advanceTimersByTimeAsync(0)
    expect(src.unsubscribes[0]).toHaveBeenCalledTimes(1)
  })

  it('a grace delay keeps the controller for a re-acquire within it, then stops it', async () => {
    vi.useFakeTimers()
    const src = fakeSource()
    const cache = new LiveCandlesCache({ graceMs: 100 })
    const l1 = cache.acquire(params(src.provider), recorder().listener)
    l1.release()
    await vi.advanceTimersByTimeAsync(99)
    const l2 = cache.acquire(params(src.provider), recorder().listener)
    await vi.advanceTimersByTimeAsync(500)
    expect(src.subscribeCandles).toHaveBeenCalledTimes(1)
    expect(src.unsubscribes[0]).not.toHaveBeenCalled()
    l2.release()
    await vi.advanceTimersByTimeAsync(99)
    expect(src.unsubscribes[0]).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    expect(src.unsubscribes[0]).toHaveBeenCalledTimes(1)
    expect(cache.size).toBe(0)
  })

  it('reload and loadOlder from either lease act on the one shared controller', async () => {
    vi.useFakeTimers()
    const src = fakeSource()
    const cache = new LiveCandlesCache()
    const a = recorder()
    const b = recorder()
    const la = cache.acquire(params(src.provider), a.listener)
    const lb = cache.acquire(params(src.provider), b.listener)
    src.loads[0].resolve([bar(3600), bar(7200)])
    await vi.advanceTimersByTimeAsync(0)
    lb.reload()
    expect(src.getCandles).toHaveBeenCalledTimes(2)
    expect(a.last().refreshing).toBe(true)
    expect(b.last()).toBe(a.last())
    src.loads[1].resolve([bar(3600), bar(7200)])
    await vi.advanceTimersByTimeAsync(0)
    la.loadOlder(false)
    expect(src.getCandles).toHaveBeenCalledTimes(3)
    expect(b.last().loadingOlder).toBe(true)
    expect(a.last()).toBe(b.last())
  })

  it('a listener releasing during an emit does not disturb the others', async () => {
    const src = fakeSource()
    const cache = new LiveCandlesCache()
    const b = recorder()
    let la: LiveCandlesLease | null = null
    const aSeen: LiveCandlesSnapshot[] = []
    la = cache.acquire(params(src.provider), (s) => {
      aSeen.push(s)
      if (s.load.kind === 'ready') la?.release()
    })
    cache.acquire(params(src.provider), b.listener)
    src.loads[0].resolve([bar(3600)])
    await flush()
    expect(b.last().load.kind).toBe('ready')
    src.tick(bar(3600, 120))
    expect(aSeen.filter((s) => s.load.kind === 'ready')).toHaveLength(1)
    const load = b.last().load
    expect(load.kind === 'ready' ? load.candles[0].close : null).toBe(120)
  })
})
