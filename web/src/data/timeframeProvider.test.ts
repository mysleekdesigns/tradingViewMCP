import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Candle, ConnectionStatus, Interval } from '../types'
import { aggregateCandles } from './aggregate'
import { MarketDataError, type MarketDataProvider, type SubscribeOptions } from './provider'
import {
  BASE_PAGE,
  MAX_BASE_PAGES,
  SEED_BAN_WAIT_S,
  SEED_MAX_ATTEMPTS,
  SEED_RATE_LIMIT_WAIT_S,
  SEED_RETRY_DELAYS_MS,
  SEED_SLOW_RETRY_MAX_MS,
  createTimeframeProvider,
  seedSlowRetryMs,
  type SecondsSource,
} from './timeframeProvider'
import { bucketStart } from './timeframes'

const M = 60

function bar(time: number, close: number, volume = 1): Candle {
  return { time, open: close - 0.5, high: close + 1, low: close - 1, close, volume }
}

/** Continuous bars every `step` s over [from, to], with a deterministic price pattern. */
function series(from: number, to: number, step: number): Candle[] {
  const out: Candle[] = []
  for (let t = from; t <= to; t += step) out.push(bar(t, 100 + ((t / step) % 17), (t / step) % 5))
  return out
}

/** Binance klines semantics over a fixed dataset: the `limit` newest rows at or before endTime. */
function klines(data: readonly Candle[], limit = 500, endTime?: number): Candle[] {
  const upTo = endTime === undefined ? data : data.filter((c) => c.time <= endTime)
  return upTo.slice(-Math.min(limit, BASE_PAGE))
}

interface Call {
  interval: string
  limit: number | undefined
  endTime: number | undefined
  signal: AbortSignal | undefined
}

interface Sub {
  interval: string
  onCandle: (c: Candle, closed: boolean, provisional?: boolean) => void
  onStatus?: (s: ConnectionStatus) => void
  unsubscribed: boolean
}

/** Provider over per-interval datasets (or a custom REST handler); streams driven by tests. */
class FakeBase implements MarketDataProvider {
  calls: Call[] = []
  subs: Sub[] = []
  data: Partial<Record<Interval, Candle[]>> = {}
  handler: ((call: Call) => Promise<Candle[]>) | null = null

  getCandles(
    _symbol: string,
    interval: Interval,
    limit?: number,
    endTime?: number,
    opts?: { signal?: AbortSignal },
  ): Promise<Candle[]> {
    const call = { interval, limit, endTime, signal: opts?.signal }
    this.calls.push(call)
    if (this.handler) return this.handler(call)
    return Promise.resolve(klines(this.data[interval] ?? [], limit, endTime))
  }

  subscribeCandles(
    _symbol: string,
    interval: Interval,
    onCandle: (c: Candle, closed: boolean, provisional?: boolean) => void,
    opts?: SubscribeOptions,
  ): () => void {
    const sub: Sub = { interval, onCandle, onStatus: opts?.onStatus, unsubscribed: false }
    this.subs.push(sub)
    return () => {
      sub.unsubscribed = true
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
}

class FakeSeconds implements SecondsSource {
  calls: Call[] = []
  data: Candle[] = []
  sub: Sub | null = null
  getCandles(
    _symbol: string,
    interval: '1s',
    limit?: number,
    endTime?: number,
    opts?: { signal?: AbortSignal },
  ): Promise<Candle[]> {
    this.calls.push({ interval, limit, endTime, signal: opts?.signal })
    return Promise.resolve(klines(this.data, limit, endTime))
  }
  subscribeCandles(
    _symbol: string,
    interval: '1s',
    onCandle: (c: Candle, closed: boolean) => void,
    opts?: SubscribeOptions,
  ): () => void {
    this.sub = { interval, onCandle, onStatus: opts?.onStatus, unsubscribed: false }
    const sub = this.sub
    return () => {
      sub.unsubscribed = true
    }
  }
}

async function rejection(p: Promise<unknown>): Promise<unknown> {
  return p.then(
    () => {
      throw new Error('expected rejection')
    },
    (e: unknown) => e,
  )
}

/** A 3m bucket start well inside the datasets. */
const B = bucketStart(1_700_000_000, '3m')

describe('createTimeframeProvider: supports', () => {
  it('accepts canonical timeframes; seconds only with a seconds source', () => {
    const plain = createTimeframeProvider(new FakeBase())
    expect(plain.supports('1m')).toBe(true)
    expect(plain.supports('3m')).toBe(true)
    expect(plain.supports('3d')).toBe(true)
    expect(plain.supports('5s')).toBe(false)
    expect(plain.supports('60m')).toBe(false) // non-canonical (1h)
    expect(plain.supports('7s')).toBe(false)
    const withSeconds = createTimeframeProvider(new FakeBase(), { seconds: new FakeSeconds() })
    expect(withSeconds.supports('1s')).toBe(true)
    expect(withSeconds.supports('30s')).toBe(true)
  })

  it('rejects/throws a MarketDataError for an unsupported timeframe', async () => {
    const p = createTimeframeProvider(new FakeBase())
    const err = await rejection(p.getCandles('BTCUSDT', '5s'))
    expect(err).toBeInstanceOf(MarketDataError)
    expect(() => p.subscribeCandles('BTCUSDT', '5s', () => {})).toThrow(MarketDataError)
  })
})

describe('createTimeframeProvider: getCandles', () => {
  it('passes native intervals straight through', async () => {
    const base = new FakeBase()
    base.data['1h'] = series(0, 100 * 3_600, 3_600)
    const ctrl = new AbortController()
    const p = createTimeframeProvider(base)
    const out = await p.getCandles('BTCUSDT', '1h', 10, 50 * 3_600, { signal: ctrl.signal })
    expect(out).toEqual(klines(base.data['1h'], 10, 50 * 3_600))
    expect(base.calls).toEqual([
      { interval: '1h', limit: 10, endTime: 50 * 3_600, signal: ctrl.signal },
    ])
  })

  it('aggregates 3m from 1m: newest `limit` complete buckets, last one forming', async () => {
    const base = new FakeBase()
    const all = series(B - 4_000 * M, B + 4 * M, M) // forming bucket B+180 has 2 bars
    base.data['1m'] = all
    const p = createTimeframeProvider(base)
    const out = await p.getCandles('BTCUSDT', '3m', 500)
    const expected = aggregateCandles(all, '3m').slice(-500)
    expect(out).toEqual(expected)
    expect(out[out.length - 1].time).toBe(B + 3 * M)
    // 500 buckets = 1500 base bars: the newest page plus one older page.
    expect(base.calls.map((c) => c.interval)).toEqual(['1m', '1m'])
    expect(base.calls[0].endTime).toBeUndefined()
    expect(base.calls[1].endTime).toBe(all[all.length - BASE_PAGE].time - 1)
  })

  it('drops the partial oldest bucket when the pages start inside it', async () => {
    const base = new FakeBase()
    // 1000 bars ending 1 bar into a bucket: the page starts mid-bucket.
    const all = series(B - 5_000 * M + M, B + M, M)
    base.data['1m'] = all
    const p = createTimeframeProvider(base)
    const out = await p.getCandles('BTCUSDT', '3m', 10)
    expect(out).toEqual(aggregateCandles(all, '3m').slice(-10))
    const big = await p.getCandles('BTCUSDT', '7m', 1000)
    const full = aggregateCandles(all, '7m')
    // Every returned bucket equals the bucket built from all the data (none is partial).
    for (const c of big) expect(full.find((f) => f.time === c.time)).toEqual(c)
  })

  it('keeps a partial oldest bucket when history starts inside it', async () => {
    const base = new FakeBase()
    const all = series(B + M, B + 30 * M, M) // listing 1 minute into a bucket
    base.data['1m'] = all
    const out = await createTimeframeProvider(base).getCandles('BTCUSDT', '3m', 500)
    expect(out).toEqual(aggregateCandles(all, '3m'))
    expect(out[0].time).toBe(B)
    expect(base.calls).toHaveLength(1)
  })

  it('endTime selects the bucket holding it, returned whole', async () => {
    const base = new FakeBase()
    const all = series(B - 3_000 * M, B + 300 * M, M)
    base.data['1m'] = all
    const p = createTimeframeProvider(base)
    const endTime = B + M + 7 // inside bucket B
    const out = await p.getCandles('BTCUSDT', '3m', 5, endTime)
    expect(base.calls[0].endTime).toBe(B + 3 * M - 1)
    expect(out.map((c) => c.time)).toEqual([B - 12 * M, B - 9 * M, B - 6 * M, B - 3 * M, B])
    expect(out[4]).toEqual(aggregateCandles(all, '3m').find((c) => c.time === B))
  })

  it('pages older history with endTime = previous oldest - 1 (loadOlder style)', async () => {
    const base = new FakeBase()
    const all = series(B - 6_000 * M, B + 2 * M, M)
    base.data['1m'] = all
    const p = createTimeframeProvider(base)
    const recent = await p.getCandles('BTCUSDT', '3m', 100)
    const older = await p.getCandles('BTCUSDT', '3m', 100, recent[0].time - 1)
    const full = aggregateCandles(all, '3m')
    expect([...older, ...recent]).toEqual(full.slice(-200))
  })

  it('caps base requests at MAX_BASE_PAGES and returns only complete buckets', async () => {
    const seconds = new FakeSeconds()
    const S = bucketStart(1_700_000_000, '30s')
    seconds.data = series(S - 40_000, S + 12, 1)
    const p = createTimeframeProvider(new FakeBase(), { seconds })
    const out = await p.getCandles('BTCUSDT', '30s', 1000) // would need 30 pages
    expect(seconds.calls).toHaveLength(MAX_BASE_PAGES)
    const full = aggregateCandles(seconds.data, '30s')
    // 15 pages = 15000 s = 500 buckets; a partial oldest one is dropped.
    expect(out.length).toBeGreaterThanOrEqual(499)
    expect(out.length).toBeLessThanOrEqual(500)
    for (const c of out) expect(full.find((f) => f.time === c.time)).toEqual(c)
    expect(out[out.length - 1].time).toBe(S)
  })

  it('handles base gaps (overlapping pages) without holes or duplicates', async () => {
    const base = new FakeBase()
    const all = series(B - 9_000 * M, B, M).filter((c) => (c.time / M) % 4 !== 1)
    base.data['1m'] = all
    const out = await createTimeframeProvider(base).getCandles('BTCUSDT', '7m', 1000)
    const full = aggregateCandles(all, '7m')
    expect(out).toEqual(full.slice(-out.length))
    expect(out.length).toBeGreaterThan(900)
  })

  it('serves 1s straight from the seconds source', async () => {
    const seconds = new FakeSeconds()
    seconds.data = series(1_000, 2_000, 1)
    const p = createTimeframeProvider(new FakeBase(), { seconds })
    expect(await p.getCandles('BTCUSDT', '1s', 5, 1_500)).toEqual(klines(seconds.data, 5, 1_500))
    expect(seconds.calls).toHaveLength(1)
  })

  it('forwards the abort signal and base errors', async () => {
    const base = new FakeBase()
    base.handler = (call) =>
      Promise.reject(
        call.signal?.aborted
          ? new MarketDataError('aborted', 'cancelled')
          : new MarketDataError('rate-limited', 'slow down', 429, 5),
      )
    const p = createTimeframeProvider(base)
    const err = await rejection(p.getCandles('BTCUSDT', '3m'))
    expect((err as MarketDataError).kind).toBe('rate-limited')
    const ctrl = new AbortController()
    ctrl.abort()
    const aborted = await rejection(
      p.getCandles('BTCUSDT', '3m', 10, undefined, { signal: ctrl.signal }),
    )
    expect((aborted as MarketDataError).kind).toBe('aborted')
    expect(base.calls[1].signal).toBe(ctrl.signal)
  })
})

describe('createTimeframeProvider: subscribeCandles', () => {
  let base: FakeBase
  let emitted: [Candle, boolean][]
  let statuses: ConnectionStatus[]
  /** Buckets handed to onRepair. */
  let repairs: number[]
  /** Pending REST seeds, resolved by the test. */
  let seeds: { call: Call; resolve: (c: Candle[]) => void; reject: (e: unknown) => void }[]
  let unsubscribe: () => void

  const tick = (c: Candle, closed = false) => base.subs[0].onCandle(c, closed)
  const status = (s: ConnectionStatus) => base.subs[0].onStatus?.(s)

  beforeEach(() => {
    vi.useFakeTimers()
    base = new FakeBase()
    seeds = []
    base.handler = (call) =>
      new Promise((resolve, reject) => {
        seeds.push({ call, resolve, reject })
        call.signal?.addEventListener('abort', () =>
          reject(new MarketDataError('aborted', 'cancelled')),
        )
      })
    emitted = []
    statuses = []
    repairs = []
    unsubscribe = createTimeframeProvider(base).subscribeCandles(
      'BTCUSDT',
      '3m',
      (c, closed) => emitted.push([c, closed]),
      { onStatus: (s) => statuses.push(s), onRepair: (t) => repairs.push(t) },
    )
  })

  afterEach(() => {
    unsubscribe()
    vi.useRealTimers()
  })

  it('subscribes to the base interval and passes status through', () => {
    expect(base.subs.map((s) => s.interval)).toEqual(['1m'])
    status('connecting')
    status('live')
    expect(statuses).toEqual(['connecting', 'live'])
  })

  it('native timeframes subscribe directly', () => {
    const b = new FakeBase()
    const onCandle = vi.fn()
    createTimeframeProvider(b).subscribeCandles('BTCUSDT', '4h', onCandle)
    expect(b.subs).toHaveLength(1)
    expect(b.subs[0].interval).toBe('4h')
    expect(b.subs[0].onCandle).toBe(onCandle)
  })

  it('a bucket entered at its first base bar emits at once and closes on its last bar', () => {
    tick(bar(B, 10, 1))
    expect(emitted).toEqual([
      [{ time: B, open: 9.5, high: 11, low: 9, close: 10, volume: 1 }, false],
    ])
    // A later update of the same base bar is cumulative: same open, new close/high/volume.
    tick({ ...bar(B, 12, 2), open: 9.5 }, true)
    // Later base bars open elsewhere; the bucket keeps the first base bar's open.
    tick(bar(B + M, 8, 3), true)
    tick(bar(B + 2 * M, 11, 4))
    expect(emitted).toHaveLength(4)
    expect(emitted[3]).toEqual([
      { time: B, open: 9.5, high: 13, low: 7, close: 11, volume: 9 },
      false,
    ])
    tick({ ...bar(B + 2 * M, 11.5, 5), open: 10.5 }, true)
    expect(emitted[4]).toEqual([
      { time: B, open: 9.5, high: 13, low: 7, close: 11.5, volume: 10 },
      true,
    ])
    expect(seeds).toHaveLength(0)
    // The next bucket starts fresh; no second close for B.
    tick(bar(B + 3 * M, 20, 1))
    expect(emitted.slice(5)).toEqual([
      [{ time: B + 3 * M, open: 19.5, high: 21, low: 19, close: 20, volume: 1 }, false],
    ])
    expect(emitted.filter(([c, closed]) => c.time === B && closed)).toHaveLength(1)
  })

  it('closes a bucket when a later bucket starts if its last bar never closed', () => {
    tick(bar(B, 10))
    tick(bar(B + M, 10.5))
    tick(bar(B + 2 * M, 11))
    tick(bar(B + 3 * M, 12))
    expect(emitted.map(([c, closed]) => [c.time, closed])).toEqual([
      [B, false],
      [B, false],
      [B, false],
      [B, true],
      [B + 3 * M, false],
    ])
  })

  it('seeds a bucket entered mid-way over REST before emitting', async () => {
    tick(bar(B + M, 10, 2))
    expect(emitted).toHaveLength(0)
    expect(seeds).toHaveLength(1)
    expect(seeds[0].call).toMatchObject({ interval: '1m', limit: 3, endTime: B + 3 * M - 1 })
    tick(bar(B + M, 11, 3)) // held while seeding
    expect(emitted).toHaveLength(0)
    seeds[0].resolve([bar(B - M, 1), bar(B, 5, 4), bar(B + M, 10, 2)])
    await vi.advanceTimersByTimeAsync(0)
    // B-1m is outside the bucket; the REST row for the forming bar replaces the live one.
    expect(emitted).toEqual([
      [{ time: B, open: 4.5, high: 11, low: 4, close: 10, volume: 6 }, false],
    ])
    tick(bar(B + M, 12, 5))
    expect(emitted[1]).toEqual([
      { time: B, open: 4.5, high: 13, low: 4, close: 12, volume: 9 },
      false,
    ])
  })

  it('retries a failed seed with backoff', async () => {
    tick(bar(B + M, 10))
    seeds[0].reject(new MarketDataError('network', 'down'))
    await vi.advanceTimersByTimeAsync(SEED_RETRY_DELAYS_MS[0] - 1)
    expect(seeds).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(seeds).toHaveLength(2)
    seeds[1].resolve([bar(B, 5), bar(B + M, 10)])
    await vi.advanceTimersByTimeAsync(0)
    expect(emitted).toHaveLength(1)
    expect(emitted[0][0].open).toBe(4.5)
  })

  it('a 429 holds the retry until Retry-After, not the shorter backoff', async () => {
    tick(bar(B + M, 10))
    seeds[0].reject(new MarketDataError('rate-limited', 'slow down', 429, 30))
    await vi.advanceTimersByTimeAsync(30_000 - 1)
    expect(seeds).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(seeds).toHaveLength(2)
    seeds[1].resolve([bar(B, 5), bar(B + M, 10)])
    await vi.advanceTimersByTimeAsync(0)
    expect(emitted.map(([c, closed]) => [c.time, c.open, closed])).toEqual([[B, 4.5, false]])
  })

  it('a 429 without a usable Retry-After holds for SEED_RATE_LIMIT_WAIT_S', async () => {
    tick(bar(B + M, 10))
    seeds[0].reject(new MarketDataError('rate-limited', 'slow down', 429))
    await vi.advanceTimersByTimeAsync(SEED_RATE_LIMIT_WAIT_S * 1000 - 1)
    expect(seeds).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(seeds).toHaveLength(2)
    seeds[1].reject(new MarketDataError('rate-limited', 'slow down', 429, Number.NaN))
    await vi.advanceTimersByTimeAsync(SEED_RATE_LIMIT_WAIT_S * 1000 - 1)
    expect(seeds).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(1)
    expect(seeds).toHaveLength(3)
  })

  it("a 429 holds every subscription's seeds on the same provider", async () => {
    const b2 = new FakeBase()
    const reqs: { reject: (e: unknown) => void }[] = []
    b2.handler = () => new Promise((_resolve, reject) => reqs.push({ reject }))
    const p = createTimeframeProvider(b2)
    const un1 = p.subscribeCandles('BTCUSDT', '3m', () => {})
    const un2 = p.subscribeCandles('ETHUSDT', '10m', () => {})
    b2.subs[0].onCandle(bar(B + M, 10), false)
    reqs[0].reject(new MarketDataError('rate-limited', 'slow down', 429, 40))
    await vi.advanceTimersByTimeAsync(0)
    // The other subscription needs a seed now, but waits out the same hold.
    b2.subs[1].onCandle(bar(bucketStart(B, '10m') + M, 10), false)
    await vi.advanceTimersByTimeAsync(40_000 - 1)
    expect(reqs).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(reqs).toHaveLength(3)
    // Both buckets: the 3m retry and the 10m (two 5m bars) first seed.
    expect(b2.calls.map((c) => c.limit).sort((x, y) => (x ?? 0) - (y ?? 0))).toEqual([2, 3, 3])
    un1()
    un2()
  })

  it('a 418 holds seeds like a 429; a bucket that ends unseeded goes to repair', async () => {
    tick(bar(B + M, 10))
    seeds[0].reject(new MarketDataError('banned', 'IP banned', 418, 120))
    await vi.advanceTimersByTimeAsync(0)
    expect(repairs).toEqual([])
    // The stream goes on: the next bucket is seen from its first base bar and is emitted.
    tick(bar(B + 3 * M, 20))
    await vi.advanceTimersByTimeAsync(60_000)
    expect(seeds).toHaveLength(1)
    // B was never seeded, so it is never emitted (least of all as closed); it ended after a
    // failed seed, so it goes to the caller's REST repair.
    expect(emitted.map(([c, closed]) => [c.time, closed])).toEqual([[B + 3 * M, false]])
    expect(repairs).toEqual([B])
    // Reconnect mid-bucket: B+3m needs a seed, which waits out the ban.
    status('reconnecting')
    status('live')
    tick(bar(B + 4 * M, 21))
    await vi.advanceTimersByTimeAsync(60_000 - 1)
    expect(seeds).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(seeds.map((s) => s.call.endTime)).toEqual([B + 3 * M - 1, B + 6 * M - 1])
    seeds[1].resolve([bar(B + 3 * M, 20, 1), bar(B + 4 * M, 21, 1)])
    await vi.advanceTimersByTimeAsync(0)
    expect(emitted[emitted.length - 1]).toEqual([
      { time: B + 3 * M, open: 19.5, high: 22, low: 19, close: 21, volume: 2 },
      false,
    ])
    expect(emitted.some(([c]) => c.time === B)).toBe(false)
    expect(repairs).toEqual([B])
  })

  it('a 418 without Retry-After holds seeds for SEED_BAN_WAIT_S after a reconnect', async () => {
    tick(bar(B + M, 10))
    seeds[0].reject(new MarketDataError('banned', 'IP banned', 418))
    await vi.advanceTimersByTimeAsync(0)
    status('reconnecting')
    status('live')
    tick(bar(B + 2 * M, 11))
    await vi.advanceTimersByTimeAsync(SEED_BAN_WAIT_S * 1000 - 1)
    expect(seeds).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(seeds).toHaveLength(2)
  })

  it('stops fast retries after SEED_MAX_ATTEMPTS and never emits the bucket unseeded', async () => {
    tick(bar(B + M, 10))
    for (let k = 0; k < SEED_MAX_ATTEMPTS; k++) {
      expect(seeds).toHaveLength(k + 1)
      seeds[k].reject(new MarketDataError('network', 'down'))
      await vi.advanceTimersByTimeAsync(SEED_RETRY_DELAYS_MS[k] ?? 0)
    }
    await vi.advanceTimersByTimeAsync(60_000)
    expect(seeds).toHaveLength(SEED_MAX_ATTEMPTS)
    tick(bar(B + 2 * M, 11), true)
    expect(emitted).toHaveLength(0)
    // Superseded: B is dropped without a close; the next bucket streams normally.
    tick(bar(B + 3 * M, 20))
    await vi.advanceTimersByTimeAsync(600_000)
    expect(seeds).toHaveLength(SEED_MAX_ATTEMPTS)
    expect(emitted.map(([c, closed]) => [c.time, closed])).toEqual([[B + 3 * M, false]])
  })

  it('a reconnect gives a forming bucket past its fast retries a fresh budget', async () => {
    tick(bar(B + M, 10))
    for (let k = 0; k < SEED_MAX_ATTEMPTS; k++) {
      seeds[k].reject(new MarketDataError('network', 'down'))
      await vi.advanceTimersByTimeAsync(SEED_RETRY_DELAYS_MS[k] ?? 0)
    }
    expect(seeds).toHaveLength(SEED_MAX_ATTEMPTS)
    status('reconnecting')
    status('live')
    tick(bar(B + 2 * M, 11))
    expect(seeds).toHaveLength(SEED_MAX_ATTEMPTS + 1)
    seeds[SEED_MAX_ATTEMPTS].resolve([bar(B, 5), bar(B + M, 10), bar(B + 2 * M, 11)])
    await vi.advanceTimersByTimeAsync(0)
    expect(emitted.map(([c, closed]) => [c.time, c.open, closed])).toEqual([[B, 4.5, false]])
  })

  it('stops retrying a forming bucket once a later bucket supersedes it', async () => {
    tick(bar(B + M, 10))
    seeds[0].reject(new MarketDataError('network', 'down'))
    await vi.advanceTimersByTimeAsync(0)
    tick(bar(B + 3 * M, 20)) // during the retry wait
    await vi.advanceTimersByTimeAsync(600_000)
    expect(seeds).toHaveLength(1)
    expect(emitted.map(([c, closed]) => [c.time, closed])).toEqual([[B + 3 * M, false]])
  })

  it('a tainted bucket whose post-reconnect seed fails is not retried or closed', async () => {
    tick(bar(B, 10, 1))
    status('reconnecting')
    status('live')
    tick(bar(B + 4 * M, 30, 1))
    const seedB = seeds.find((s) => s.call.endTime === B + 3 * M - 1)!
    seedB.reject(new MarketDataError('network', 'down'))
    await vi.advanceTimersByTimeAsync(600_000)
    expect(seeds.filter((s) => s.call.endTime === B + 3 * M - 1)).toHaveLength(1)
    expect(emitted.filter(([c, closed]) => c.time === B && closed)).toHaveLength(0)
    // Later buckets are unaffected.
    tick(bar(B + 6 * M, 40, 1))
    expect(emitted[emitted.length - 1][0].time).toBe(B + 6 * M)
  })

  it('onRepair: a forming bucket past its fast retries is reported once, when superseded', async () => {
    tick(bar(B + M, 10))
    for (let k = 0; k < SEED_MAX_ATTEMPTS; k++) {
      seeds[k].reject(new MarketDataError('network', 'down'))
      await vi.advanceTimersByTimeAsync(SEED_RETRY_DELAYS_MS[k] ?? 0)
    }
    // Still forming: nothing to repair yet.
    expect(repairs).toEqual([])
    tick(bar(B + 3 * M, 20))
    expect(repairs).toEqual([B])
    tick(bar(B + 4 * M, 21))
    tick(bar(B + 6 * M, 22))
    expect(repairs).toEqual([B])
  })

  it('onRepair: a forming bucket superseded during its retry wait is reported', async () => {
    tick(bar(B + M, 10))
    seeds[0].reject(new MarketDataError('network', 'down'))
    await vi.advanceTimersByTimeAsync(0)
    tick(bar(B + 3 * M, 20))
    expect(repairs).toEqual([B])
    await vi.advanceTimersByTimeAsync(600_000)
    expect(seeds).toHaveLength(1)
    expect(repairs).toEqual([B])
  })

  it('onRepair: an ended bucket whose one seed fails is reported', async () => {
    tick(bar(B, 10, 1))
    status('reconnecting')
    status('live')
    tick(bar(B + 4 * M, 30, 1))
    const seedB = seeds.find((s) => s.call.endTime === B + 3 * M - 1)!
    seedB.reject(new MarketDataError('rate-limited', 'slow down', 429, 5))
    await vi.advanceTimersByTimeAsync(0)
    expect(repairs).toEqual([B])
    expect(emitted.filter(([c, closed]) => c.time === B && closed)).toHaveLength(0)
  })

  it('onRepair: never for buckets emitted closed, including a re-seeded tainted one', async () => {
    tick(bar(B, 10))
    tick(bar(B + 2 * M, 11), true)
    tick(bar(B + 3 * M, 12))
    status('reconnecting')
    status('live')
    tick(bar(B + 7 * M, 30, 1)) // B+3m tainted, B+6m entered mid-way
    for (const sd of seeds) sd.resolve([bar(B + 3 * M, 12), bar(B + 4 * M, 13), bar(B + 6 * M, 30)])
    await vi.advanceTimersByTimeAsync(0)
    expect(emitted.filter(([, closed]) => closed).map(([c]) => c.time)).toEqual([B, B + 3 * M])
    expect(repairs).toEqual([])
  })

  it('onRepair: never after unsubscribe, and a throwing callback is contained', async () => {
    const b2 = new FakeBase()
    const reqs: { reject: (e: unknown) => void }[] = []
    b2.handler = () => new Promise((_resolve, reject) => reqs.push({ reject }))
    const got: number[] = []
    const out: number[] = []
    const un = createTimeframeProvider(b2).subscribeCandles(
      'BTCUSDT',
      '3m',
      (c) => out.push(c.time),
      {
        onRepair: (t) => {
          got.push(t)
          throw new Error('boom')
        },
      },
    )
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})
    b2.subs[0].onCandle(bar(B + M, 10), false)
    reqs[0].reject(new MarketDataError('network', 'down'))
    await vi.advanceTimersByTimeAsync(0)
    b2.subs[0].onCandle(bar(B + 3 * M, 20), false) // throws inside onRepair
    expect(got).toEqual([B])
    expect(out).toEqual([B + 3 * M]) // the new bucket was still emitted
    expect(err).toHaveBeenCalled()
    // After a drop, B+3m (now superseded) and B+6m are seeding; they fail after unsubscribe.
    b2.subs[0].onCandle(bar(B + 4 * M, 21), false)
    b2.subs[0].onStatus?.('reconnecting')
    b2.subs[0].onStatus?.('live')
    b2.subs[0].onCandle(bar(B + 7 * M, 30), false)
    expect(reqs.length).toBeGreaterThan(1)
    un()
    for (const r of reqs.slice(1)) r.reject(new MarketDataError('network', 'down'))
    await vi.advanceTimersByTimeAsync(600_000)
    expect(got).toEqual([B])
    err.mockRestore()
  })

  it('after a drop, re-seeds the bucket and closes a tainted bucket from REST', async () => {
    tick(bar(B, 10, 1))
    tick(bar(B, 10, 1), true)
    status('reconnecting')
    status('live')
    expect(statuses).toEqual(['reconnecting', 'live'])
    // Back after the bucket ended: B is tainted (B+1m, B+2m were missed).
    tick(bar(B + 4 * M, 30, 1))
    const n = emitted.length
    expect(seeds.map((s) => s.call.endTime).sort()).toEqual([B + 3 * M - 1, B + 6 * M - 1])
    expect(emitted).toHaveLength(n) // nothing until the seeds land
    const seedB = seeds.find((s) => s.call.endTime === B + 3 * M - 1)!
    seedB.resolve([bar(B, 10, 1), bar(B + M, 15, 2), bar(B + 2 * M, 14, 3)])
    await vi.advanceTimersByTimeAsync(0)
    expect(emitted[emitted.length - 1]).toEqual([
      { time: B, open: 9.5, high: 16, low: 9, close: 14, volume: 6 },
      true,
    ])
    const seedNext = seeds.find((s) => s.call.endTime === B + 6 * M - 1)!
    seedNext.resolve([bar(B + 3 * M, 25, 1), bar(B + 4 * M, 30, 1)])
    await vi.advanceTimersByTimeAsync(0)
    expect(emitted[emitted.length - 1]).toEqual([
      { time: B + 3 * M, open: 24.5, high: 31, low: 24, close: 30, volume: 2 },
      false,
    ])
  })

  it('a drop aborts an in-flight seed', () => {
    tick(bar(B + M, 10))
    status('reconnecting')
    expect(seeds[0].call.signal?.aborted).toBe(true)
  })

  it('no look-ahead: base bars after a closed bucket never re-emit or change it', () => {
    tick(bar(B, 10))
    tick(bar(B + M, 10.5))
    tick(bar(B + 2 * M, 11), true)
    const closedB = emitted.find(([c, closed]) => c.time === B && closed)![0]
    for (let k = 3; k < 9; k++) tick(bar(B + k * M, 1_000 * k, 99), k % 3 === 2)
    const forB = emitted.filter(([c]) => c.time === B)
    expect(forB[forB.length - 1]).toEqual([closedB, true])
    expect(emitted.filter(([c]) => c.time === B).every(([c]) => c.close < 100)).toBe(true)
    // A late tick for B is ignored.
    tick(bar(B + M, -5), true)
    expect(emitted.filter(([c]) => c.time === B)).toHaveLength(forB.length)
  })

  it('unsubscribe tears down the base stream, aborts seeds, and stops emitting', async () => {
    tick(bar(B + M, 10))
    unsubscribe()
    expect(base.subs[0].unsubscribed).toBe(true)
    expect(seeds[0].call.signal?.aborted).toBe(true)
    seeds[0].resolve([bar(B, 1)])
    tick(bar(B + 3 * M, 5))
    status('offline')
    await vi.advanceTimersByTimeAsync(60_000)
    expect(emitted).toHaveLength(0)
    expect(statuses).toHaveLength(0)
  })

  it('aggregates 1s into 5s from the seconds source', () => {
    const seconds = new FakeSeconds()
    const out: [Candle, boolean][] = []
    createTimeframeProvider(new FakeBase(), { seconds }).subscribeCandles('BTCUSDT', '5s', (c, x) =>
      out.push([c, x]),
    )
    const S = 1_700_000_000
    for (let t = S; t < S + 5; t++) seconds.sub!.onCandle(bar(t, 100 + t - S), true)
    expect(out[out.length - 1]).toEqual([
      { time: S, open: 99.5, high: 105, low: 99, close: 104, volume: 5 },
      true,
    ])
    expect(out.filter(([, closed]) => closed)).toHaveLength(1)
  })
})

describe('createTimeframeProvider: slow seed retries for a long forming bucket', () => {
  const H4 = 4 * 3_600
  const D12 = bucketStart(1_700_000_000, '12h')

  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  function twelveHour() {
    const base = new FakeBase()
    const seeds: { call: Call; resolve: (c: Candle[]) => void; reject: (e: unknown) => void }[] = []
    base.handler = (call) => new Promise((resolve, reject) => seeds.push({ call, resolve, reject }))
    const out: [Candle, boolean][] = []
    const un = createTimeframeProvider(base).subscribeCandles('BTCUSDT', '12h', (c, x) =>
      out.push([c, x]),
    )
    return { base, seeds, out, un, tick: (c: Candle) => base.subs[0].onCandle(c, false) }
  }

  it('seedSlowRetryMs is half a bucket, capped at SEED_SLOW_RETRY_MAX_MS', () => {
    expect(seedSlowRetryMs(180)).toBe(90_000)
    expect(seedSlowRetryMs(30)).toBe(15_000)
    expect(seedSlowRetryMs(12 * 3_600)).toBe(SEED_SLOW_RETRY_MAX_MS)
    expect(seedSlowRetryMs(3 * 86_400)).toBe(300_000)
  })

  it('12h from 4h: after the fast retries fail, slow retries go on and the bucket emits once a seed lands', async () => {
    const { base, seeds, out, un, tick } = twelveHour()
    expect(base.subs[0].interval).toBe('4h')
    tick(bar(D12 + H4, 10)) // entered mid-bucket
    for (let k = 0; k < SEED_MAX_ATTEMPTS; k++) {
      expect(seeds).toHaveLength(k + 1)
      seeds[k].reject(new MarketDataError('http', '503', 503))
      await vi.advanceTimersByTimeAsync(SEED_RETRY_DELAYS_MS[k] ?? 0)
    }
    expect(seeds).toHaveLength(SEED_MAX_ATTEMPTS)
    await vi.advanceTimersByTimeAsync(SEED_SLOW_RETRY_MAX_MS - 1)
    expect(seeds).toHaveLength(SEED_MAX_ATTEMPTS)
    await vi.advanceTimersByTimeAsync(1)
    expect(seeds).toHaveLength(SEED_MAX_ATTEMPTS + 1)
    expect(seeds[SEED_MAX_ATTEMPTS].call).toMatchObject({ interval: '4h', limit: 3 })
    // Ticks for the bucket keep arriving; nothing is emitted while it is unseeded.
    tick(bar(D12 + H4, 11))
    seeds[SEED_MAX_ATTEMPTS].reject(new MarketDataError('network', 'down'))
    await vi.advanceTimersByTimeAsync(SEED_SLOW_RETRY_MAX_MS)
    expect(seeds).toHaveLength(SEED_MAX_ATTEMPTS + 2)
    expect(out).toHaveLength(0)
    seeds[SEED_MAX_ATTEMPTS + 1].resolve([bar(D12, 5), bar(D12 + H4, 11)])
    await vi.advanceTimersByTimeAsync(0)
    expect(out.map(([c, closed]) => [c.time, c.open, closed])).toEqual([[D12, 4.5, false]])
    // Live from here: the next tick updates it without another request.
    tick(bar(D12 + H4, 12))
    expect(out[out.length - 1][0].close).toBe(12)
    expect(seeds).toHaveLength(SEED_MAX_ATTEMPTS + 2)
    un()
  })

  it('a 418 with Retry-After 120 holds the seed for 120 s, then it seeds and emits (no reconnect)', async () => {
    const { seeds, out, un, tick } = twelveHour()
    tick(bar(D12 + H4, 10))
    seeds[0].reject(new MarketDataError('banned', 'IP banned', 418, 120))
    await vi.advanceTimersByTimeAsync(0)
    tick(bar(D12 + H4, 11))
    await vi.advanceTimersByTimeAsync(120_000 - 1)
    expect(seeds).toHaveLength(1)
    expect(out).toHaveLength(0)
    await vi.advanceTimersByTimeAsync(1)
    expect(seeds).toHaveLength(2)
    seeds[1].resolve([bar(D12, 5), bar(D12 + H4, 11)])
    await vi.advanceTimersByTimeAsync(0)
    expect(out.map(([c, closed]) => [c.time, c.open, c.close, closed])).toEqual([
      [D12, 4.5, 11, false],
    ])
    un()
  })

  it('slow retries wait out the shared rate-limit gate', async () => {
    const { seeds, un, tick } = twelveHour()
    tick(bar(D12 + H4, 10))
    for (let k = 0; k < SEED_MAX_ATTEMPTS - 1; k++) {
      seeds[k].reject(new MarketDataError('network', 'down'))
      await vi.advanceTimersByTimeAsync(SEED_RETRY_DELAYS_MS[k])
    }
    seeds[SEED_MAX_ATTEMPTS - 1].reject(new MarketDataError('rate-limited', '429', 429, 900))
    await vi.advanceTimersByTimeAsync(900_000 - 1)
    expect(seeds).toHaveLength(SEED_MAX_ATTEMPTS)
    await vi.advanceTimersByTimeAsync(1)
    expect(seeds).toHaveLength(SEED_MAX_ATTEMPTS + 1)
    un()
  })
})

describe('createTimeframeProvider: shared rate-limit gate', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  function gatedBase() {
    const base = new FakeBase()
    const pending: { call: Call; reject: (e: unknown) => void }[] = []
    let next: ((call: Call) => Promise<Candle[]>) | null = null
    base.handler = (call) =>
      next ? next(call) : new Promise((_resolve, reject) => pending.push({ call, reject }))
    return {
      base,
      pending,
      p: createTimeframeProvider(base),
      respond: (h: ((call: Call) => Promise<Candle[]>) | null) => (next = h),
    }
  }

  it("a 429 on one subscription's seed makes other calls reject at once without a request", async () => {
    const { base, pending, p, respond } = gatedBase()
    const un = p.subscribeCandles('BTCUSDT', '3m', () => {})
    base.subs[0].onCandle(bar(B + M, 10), false)
    pending[0].reject(new MarketDataError('rate-limited', 'slow down', 429, 30))
    await vi.advanceTimersByTimeAsync(10_000)
    const calls = base.calls.length
    // Another panel: a native interval (gap fill / loadOlder) and an aggregated one.
    const native = (await rejection(p.getCandles('ETHUSDT', '1h', 500))) as MarketDataError
    const custom = (await rejection(p.getCandles('ETHUSDT', '3m', 500))) as MarketDataError
    for (const err of [native, custom]) {
      expect(err).toBeInstanceOf(MarketDataError)
      expect(err.kind).toBe('rate-limited')
      expect(err.status).toBe(429)
      expect(err.retryAfter).toBe(20)
    }
    expect(base.calls).toHaveLength(calls)
    // Once the hold ends requests go through again.
    await vi.advanceTimersByTimeAsync(20_000)
    respond(() => Promise.resolve([bar(0, 1)]))
    expect(await p.getCandles('ETHUSDT', '1h', 1)).toEqual([bar(0, 1)])
    expect(base.calls.length).toBeGreaterThan(calls)
    un()
  })

  it('a 429 from getCandles holds seeds and every other call through the provider', async () => {
    const { base, pending, p, respond } = gatedBase()
    respond(() => Promise.reject(new MarketDataError('rate-limited', 'slow down', 429, 10)))
    await rejection(p.getCandles('BTCUSDT', '1h', 500))
    respond(null)
    const un = p.subscribeCandles('BTCUSDT', '3m', () => {})
    base.subs[0].onCandle(bar(B + M, 10), false)
    await vi.advanceTimersByTimeAsync(10_000 - 1)
    expect(pending).toHaveLength(0)
    expect(base.calls).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(pending).toHaveLength(1)
    un()
  })

  it('a 418 rejects gated calls as banned with the remaining wait', async () => {
    const { base, p, respond } = gatedBase()
    respond(() => Promise.reject(new MarketDataError('banned', 'IP banned', 418, 120)))
    await rejection(p.getCandles('BTCUSDT', '1h', 500))
    await vi.advanceTimersByTimeAsync(50_000)
    const err = (await rejection(p.getCandles('BTCUSDT', '4h', 500))) as MarketDataError
    expect(err.kind).toBe('banned')
    expect(err.status).toBe(418)
    expect(err.retryAfter).toBe(70)
    expect(base.calls).toHaveLength(1)
  })

  it('a missing Retry-After holds for the default; network errors do not hold', async () => {
    const { base, p, respond } = gatedBase()
    respond(() => Promise.reject(new MarketDataError('network', 'down')))
    await rejection(p.getCandles('BTCUSDT', '1h', 500))
    await rejection(p.getCandles('BTCUSDT', '1h', 500))
    expect(base.calls).toHaveLength(2)
    respond(() => Promise.reject(new MarketDataError('rate-limited', 'slow down', 429)))
    await rejection(p.getCandles('BTCUSDT', '1h', 500))
    const err = (await rejection(p.getCandles('BTCUSDT', '1h', 500))) as MarketDataError
    expect(err.retryAfter).toBe(SEED_RATE_LIMIT_WAIT_S)
    expect(base.calls).toHaveLength(3)
  })

  it('an already-aborted call is passed through and rejects as aborted', async () => {
    const { base, p, respond } = gatedBase()
    respond(() => Promise.reject(new MarketDataError('rate-limited', 'slow down', 429, 30)))
    await rejection(p.getCandles('BTCUSDT', '1h', 500))
    respond((call) =>
      Promise.reject(
        call.signal?.aborted
          ? new MarketDataError('aborted', 'cancelled')
          : new Error('should not be called'),
      ),
    )
    const ctrl = new AbortController()
    ctrl.abort()
    const err = (await rejection(
      p.getCandles('BTCUSDT', '1h', 500, undefined, { signal: ctrl.signal }),
    )) as MarketDataError
    expect(err.kind).toBe('aborted')
    // The gate is still closed for everything else.
    const held = (await rejection(p.getCandles('BTCUSDT', '1h', 500))) as MarketDataError
    expect(held.kind).toBe('rate-limited')
    expect(base.calls).toHaveLength(2)
  })
})

/**
 * 3m over 1m, where the base stream skips base bars without reporting a drop (Binance's
 * idle-stale reconnect keeps 'live'), and where the base source sends provisional closes
 * (Coinbase's pending second read-back).
 */
describe('createTimeframeProvider: skipped base bars and provisional base closes', () => {
  let base: FakeBase
  /** [candle, closed, provisional flag as passed] per emission. */
  let emitted: [Candle, boolean, boolean | undefined][]
  let repairs: number[]
  let seeds: { call: Call; resolve: (c: Candle[]) => void; reject: (e: unknown) => void }[]
  let unsubscribe: () => void

  const tick = (c: Candle, closed = false) => base.subs[0].onCandle(c, closed)
  const provisionalClose = (c: Candle) => base.subs[0].onCandle(c, true, true)
  const closesOf = (time: number) => emitted.filter(([c, closed]) => c.time === time && closed)

  beforeEach(() => {
    vi.useFakeTimers()
    base = new FakeBase()
    seeds = []
    base.handler = (call) =>
      new Promise((resolve, reject) => {
        seeds.push({ call, resolve, reject })
        call.signal?.addEventListener('abort', () =>
          reject(new MarketDataError('aborted', 'cancelled')),
        )
      })
    emitted = []
    repairs = []
    unsubscribe = createTimeframeProvider(base).subscribeCandles(
      'BTCUSDT',
      '3m',
      (c, closed, provisional) => emitted.push([c, closed, provisional]),
      { onRepair: (t) => repairs.push(t) },
    )
  })

  afterEach(() => {
    unsubscribe()
    vi.useRealTimers()
  })

  // P5: B and B+1m seen from the bucket's start, B+2m (high 150) and B+3m missed, then B+4m.
  const seenThenSkipped = () => {
    tick(bar(B, 100), true)
    tick(bar(B + M, 101), true) // bar(): high = close + 1, so B's live high is 102
    tick(bar(B + 4 * M, 120))
  }

  it('P5: a skip never closes a bucket from the bars seen; it is seeded first', async () => {
    seenThenSkipped()
    // Before the fix B was emitted closed at once with high 102 (B+2m missing).
    expect(closesOf(B)).toEqual([])
    // B+3m was entered at B+4m: not complete, not emitted until seeded.
    expect(emitted.some(([c]) => c.time === B + 3 * M)).toBe(false)
    expect(seeds.map((x) => x.call.endTime).sort()).toEqual([B + 3 * M - 1, B + 6 * M - 1])
    const seedB = seeds.find((x) => x.call.endTime === B + 3 * M - 1)!
    seedB.resolve([bar(B, 100), bar(B + M, 101), bar(B + 2 * M, 149)])
    await vi.advanceTimersByTimeAsync(0)
    expect(closesOf(B).map(([c]) => c.high)).toEqual([150])
    expect(repairs).toEqual([])
  })

  it('P5: a failed seed of the skipped bucket goes to onRepair and is never closed', async () => {
    seenThenSkipped()
    const seedB = seeds.find((x) => x.call.endTime === B + 3 * M - 1)!
    seedB.reject(new MarketDataError('network', 'down'))
    await vi.advanceTimersByTimeAsync(0)
    expect(repairs).toEqual([B])
    expect(closesOf(B)).toEqual([])
  })

  it('a skip re-sends a seed that was in flight (it may lack the skipped bars), not counted', async () => {
    tick(bar(B + M, 10)) // entered mid-way: seed
    expect(seeds).toHaveLength(1)
    tick(bar(B + 4 * M, 30)) // skips B+2m and B+3m
    expect(seeds[0].call.signal?.aborted).toBe(true)
    // B (now ended) gets a fresh request instead of going to repair; B+3m is seeded too.
    const forB = seeds.filter((x) => x.call.endTime === B + 3 * M - 1)
    expect(forB).toHaveLength(2)
    expect(repairs).toEqual([])
    forB[1].resolve([bar(B, 5), bar(B + M, 10), bar(B + 2 * M, 12)])
    await vi.advanceTimersByTimeAsync(0)
    expect(closesOf(B).map(([c]) => c.close)).toEqual([12])
  })

  it('consecutive base bars are not a skip: no seed, the bucket closes on its last bar', () => {
    tick(bar(B, 100), true)
    tick(bar(B + M, 101), true)
    tick(bar(B + 2 * M, 102), true)
    expect(seeds).toHaveLength(0)
    expect(closesOf(B)).toHaveLength(1)
  })

  it('a bucket closing on a provisional last base bar is closed+provisional until it settles', () => {
    tick(bar(B, 100), true)
    tick(bar(B + M, 101), true)
    provisionalClose(bar(B + 2 * M, 102))
    expect(closesOf(B).map(([c, , p]) => [c.close, p])).toEqual([[102, true]])
    // The next bucket opens (B superseded): same values, nothing re-sent for B.
    tick(bar(B + 3 * M, 110))
    tick(bar(B + 3 * M, 111))
    expect(closesOf(B)).toHaveLength(1)
    // The settling send (corrected) closes B once more, without the flag.
    tick(bar(B + 2 * M, 99), true)
    expect(closesOf(B).map(([c, , p]) => [c.close, c.low, p])).toEqual([
      [102, 99, true],
      [99, 98, undefined],
    ])
    // Settled: later sends for B are ignored.
    tick(bar(B + 2 * M, 50), true)
    expect(closesOf(B)).toHaveLength(2)
    expect(emitted.every(([, closed, p]) => closed || p === undefined)).toBe(true)
  })

  it('a bucket superseded while an earlier base bar is provisional waits for that bar', () => {
    tick(bar(B, 100), true)
    provisionalClose(bar(B + M, 101))
    tick(bar(B + 2 * M, 102)) // never closed
    tick(bar(B + 3 * M, 110)) // supersedes B
    expect(closesOf(B).map(([c, , p]) => [c.close, p])).toEqual([[102, true]])
    tick(bar(B + M, 103), true) // settles B+1m: high 104
    expect(closesOf(B).map(([c, , p]) => [c.high, p])).toEqual([
      [103, true],
      [104, undefined],
    ])
  })

  it('a settled base bar that leaves the bucket unchanged still sends the unflagged close', () => {
    tick(bar(B, 100), true)
    tick(bar(B + M, 101), true)
    provisionalClose(bar(B + 2 * M, 102))
    tick(bar(B + 2 * M, 102), true)
    expect(closesOf(B).map(([c, , p]) => [c.close, p])).toEqual([
      [102, true],
      [102, undefined],
    ])
  })
})
