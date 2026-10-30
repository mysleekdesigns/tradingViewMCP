// Phase 19: Coinbase forming-bar coalescing (`formingFlushMs`). Same fake socket as
// coinbaseStream.test.ts; every case here also runs the uncoalesced stream where it helps.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Candle, ConnectionStatus } from '../types'
import { CoinbaseProvider } from './coinbase'
import { DEFAULT_FORMING_FLUSH_MS, openCoinbaseCandleStream } from './coinbaseStream'

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  readonly url: string
  readyState = 0
  sent: string[] = []
  private openListeners: (() => void)[] = []
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  constructor(url: string) {
    this.url = url
    FakeWebSocket.instances.push(this)
  }
  addEventListener(type: string, fn: () => void): void {
    if (type === 'open') this.openListeners.push(fn)
  }
  send(data: string): void {
    this.sent.push(data)
  }
  close(): void {
    this.readyState = 3
  }
  serverOpen(): void {
    this.readyState = 1
    for (const fn of this.openListeners) fn()
    this.onopen?.(new Event('open'))
  }
  serverMessage(data: unknown): void {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent)
  }
  serverClose(): void {
    this.readyState = 3
    this.onclose?.({ code: 1006 } as CloseEvent)
  }
}

const WS = FakeWebSocket as unknown as typeof WebSocket
const last = () => FakeWebSocket.instances[FakeWebSocket.instances.length - 1]
const PID = 'BTC-USD'
/** A minute boundary: 2026-10-25T00:00:00Z. */
const T = 1_792_886_400
const iso = (s: number) => new Date(s * 1000).toISOString()
const match = (tradeId: number, time: number, price: number, size = 0.5) => ({
  type: 'match',
  trade_id: tradeId,
  side: 'buy',
  size: String(size),
  price: String(price),
  product_id: PID,
  sequence: tradeId * 10,
  time: iso(time).replace('Z', '123Z'),
})
const heartbeat = (time: number) => ({
  type: 'heartbeat',
  last_trade_id: 1,
  product_id: PID,
  sequence: 1,
  time: iso(time),
})
const SUBSCRIPTIONS = { type: 'subscriptions', channels: [] }
const bar = (time: number, o: number, h: number, l: number, c: number, v: number): Candle => ({
  time,
  open: o,
  high: h,
  low: l,
  close: c,
  volume: v,
})
const flush = () => vi.advanceTimersByTimeAsync(0)

interface Emit {
  candle: Candle
  closed: boolean
  provisional: boolean | undefined
  /** Fake-clock ms at the emit. */
  at: number
}

/** `rest(limit, endTime)` answers every REST call at once. */
function setup(formingFlushMs: number, rest: (limit: number, end?: number) => Candle[] = () => []) {
  const emits: Emit[] = []
  /** Every event in order: a candle emit or a status. */
  const events: (Emit | ConnectionStatus)[] = []
  const unsubscribe = openCoinbaseCandleStream({
    WebSocketImpl: WS,
    url: 'wss://feed.test',
    staleTimeoutMs: 60_000,
    bucketStart: (t) => Math.floor(t / 60) * 60,
    resolveProduct: () => Promise.resolve(PID),
    fetchCandles: (limit, endTime) => Promise.resolve(rest(limit, endTime)),
    onCandle: (candle, closed, provisional) => {
      const e = { candle, closed, provisional, at: Date.now() }
      emits.push(e)
      events.push(e)
    },
    onStatus: (s) => events.push(s),
    reconcileDelayMs: 1_000,
    reconcileRetryMs: 2_000,
    now: () => (T + 1) * 1000,
    formingFlushMs,
  })
  return { emits, events, unsubscribe }
}

async function open() {
  await flush()
  last().serverOpen()
  last().serverMessage(SUBSCRIPTIONS)
  await flush()
}

beforeEach(() => {
  FakeWebSocket.instances = []
  vi.useFakeTimers()
  vi.setSystemTime(0)
  vi.spyOn(Math, 'random').mockReturnValue(1)
  vi.stubGlobal('window', new EventTarget())
  vi.stubGlobal('navigator', { onLine: true })
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

/**
 * Three minutes of a busy feed: a heartbeat each second and 20 trades per second spread over
 * it (every 50 ms), REST answering nothing (live-only bars), then quiet until every close and
 * read-back has gone out.
 */
async function busyFeed(formingFlushMs: number): Promise<Emit[]> {
  const s = setup(formingFlushMs)
  await open()
  let id = 0
  for (let sec = T + 1; sec < T + 180; sec++) {
    last().serverMessage(heartbeat(sec))
    for (let k = 0; k < 20; k++) {
      id += 1
      last().serverMessage(match(id, sec, 100 + ((id * 7) % 23) / 10, 0.01 * (1 + (id % 5))))
      await vi.advanceTimersByTimeAsync(50)
    }
  }
  last().serverMessage(heartbeat(T + 180))
  await vi.advanceTimersByTimeAsync(10_000)
  s.unsubscribe()
  return s.emits
}

describe('Coinbase forming-bar coalescing', () => {
  it('the provider coalesces forming bars with DEFAULT_FORMING_FLUSH_MS', async () => {
    expect(DEFAULT_FORMING_FLUSH_MS).toBeGreaterThanOrEqual(100)
    expect(DEFAULT_FORMING_FLUSH_MS).toBeLessThanOrEqual(250)
    const fetchFn = vi.fn((input: RequestInfo | URL) => {
      const body = String(input).endsWith('/products')
        ? [
            {
              id: PID,
              base_currency: 'BTC',
              quote_currency: 'USD',
              quote_increment: '0.01',
              status: 'online',
              trading_disabled: false,
              cancel_only: false,
            },
          ]
        : []
      return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))
    })
    const p = new CoinbaseProvider({
      fetchFn: fetchFn as unknown as typeof fetch,
      WebSocketImpl: WS,
      minRequestIntervalMs: 0,
      now: () => (T + 1) * 1000,
    })
    const got: Candle[] = []
    const un = p.subscribeCandles('BTCUSD', '1m', (c) => got.push(c))
    await open()
    last().serverMessage(match(1, T + 2, 100))
    last().serverMessage(match(2, T + 2, 101))
    last().serverMessage(match(3, T + 2, 99))
    // The bucket's first bar goes out at once; the burst after it is held.
    expect(got).toEqual([bar(T, 100, 100, 100, 100, 0.5)])
    await vi.advanceTimersByTimeAsync(DEFAULT_FORMING_FLUSH_MS - 1)
    expect(got).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(got).toEqual([bar(T, 100, 100, 100, 100, 0.5), bar(T, 100, 101, 99, 99, 1.5)])
    un()
  })

  it('a burst inside a window becomes one emit of the latest bar; a quiet update goes at once', async () => {
    const s = setup(100)
    await open()
    last().serverMessage(match(1, T + 2, 100))
    expect(s.emits.map((e) => e.candle)).toEqual([bar(T, 100, 100, 100, 100, 0.5)])
    for (let i = 2; i <= 6; i++) last().serverMessage(match(i, T + 2, 100 + i))
    expect(s.emits).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(100)
    expect(s.emits.map((e) => [e.candle, e.closed])).toEqual([
      [bar(T, 100, 100, 100, 100, 0.5), false],
      [bar(T, 100, 106, 100, 106, 3), false],
    ])
    // The window closes after an empty period; the next lone trade is not delayed.
    await vi.advanceTimersByTimeAsync(500)
    last().serverMessage(match(7, T + 3, 90))
    expect(s.emits).toHaveLength(3)
    expect(s.emits[2].candle).toEqual(bar(T, 100, 106, 90, 90, 3.5))
    s.unsubscribe()
  })

  it('keeps every closed bar and the last forming bar of each bucket, at most one forming per window', async () => {
    const plain = await busyFeed(0)
    FakeWebSocket.instances = []
    vi.setSystemTime(0)
    const coalesced = await busyFeed(100)

    const closedOf = (es: Emit[]) =>
      es.filter((e) => e.closed).map((e) => [e.candle, e.provisional])
    expect(closedOf(plain).length).toBeGreaterThanOrEqual(3)
    expect(closedOf(coalesced)).toEqual(closedOf(plain))

    // The coalesced stream is the plain one minus superseded forming updates.
    let j = 0
    for (const e of coalesced) {
      while (
        j < plain.length &&
        !(
          plain[j].closed === e.closed &&
          JSON.stringify(plain[j].candle) === JSON.stringify(e.candle)
        )
      ) {
        j += 1
      }
      expect(j).toBeLessThan(plain.length)
      j += 1
    }
    // The forming bar right before each close (what the chart shows when the bar ends) matches.
    const beforeClose = (es: Emit[]) =>
      es.flatMap((e, i) => (e.closed && i > 0 && !es[i - 1].closed ? [es[i - 1].candle] : []))
    expect(beforeClose(coalesced)).toEqual(beforeClose(plain))
    // And the final forming bar of every bucket.
    const lastForming = (es: Emit[]) => {
      const m = new Map<number, Candle>()
      for (const e of es) if (!e.closed) m.set(e.candle.time, e.candle)
      return [...m.entries()]
    }
    expect(lastForming(coalesced)).toEqual(lastForming(plain))

    const formingPlain = plain.filter((e) => !e.closed).length
    const formingCoalesced = coalesced.filter((e) => !e.closed)
    expect(formingCoalesced.length).toBeLessThan(formingPlain / 1.5)
    // Updates within a bucket are at least one window apart, except a held bar flushed early
    // because a closed bar (a read-back of the previous bucket) went out right after it.
    for (let i = 1; i < formingCoalesced.length; i++) {
      const [a, b] = [formingCoalesced[i - 1], formingCoalesced[i]]
      if (a.candle.time !== b.candle.time || b.at - a.at >= 100) continue
      const next = coalesced[coalesced.indexOf(b) + 1]
      expect(next?.closed).toBe(true)
      expect(next?.at).toBe(b.at)
    }
  })

  it('a held forming bar goes out before the closed bar, and the next bucket opens at once', async () => {
    const s = setup(60_000)
    await open()
    last().serverMessage(match(1, T + 2, 100))
    last().serverMessage(match(2, T + 50, 104))
    expect(s.emits).toHaveLength(1)
    // Roll into the next bucket: the close is read back after reconcileDelayMs.
    last().serverMessage(match(3, T + 61, 105))
    await vi.advanceTimersByTimeAsync(1_000)
    expect(s.emits.map((e) => [e.candle, e.closed])).toEqual([
      [bar(T, 100, 100, 100, 100, 0.5), false],
      [bar(T, 100, 104, 100, 104, 1), false],
      [bar(T, 100, 104, 100, 104, 1), true],
      [bar(T + 60, 105, 105, 105, 105, 0.5), false],
    ])
    s.unsubscribe()
  })

  it('a held forming bar goes out before a status change', async () => {
    const s = setup(60_000)
    await open()
    last().serverMessage(match(1, T + 2, 100))
    last().serverMessage(match(2, T + 3, 101))
    last().serverClose()
    const i = s.events.indexOf('reconnecting')
    expect(i).toBeGreaterThan(0)
    const before = s.events[i - 1]
    expect(typeof before).toBe('object')
    expect((before as Emit).candle).toEqual(bar(T, 100, 101, 100, 101, 1))
    s.unsubscribe()
  })

  it('nothing is emitted after unsubscribe, and no coalescing timer is left', async () => {
    const s = setup(100)
    await open()
    last().serverMessage(match(1, T + 2, 100))
    last().serverMessage(match(2, T + 2, 101))
    expect(s.emits).toHaveLength(1)
    s.unsubscribe()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(s.emits).toHaveLength(1)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('formingFlushMs 0 (the stream default) sends every update at once', async () => {
    const s = setup(0)
    await open()
    last().serverMessage(match(1, T + 2, 100))
    last().serverMessage(match(2, T + 2, 101))
    last().serverMessage(match(3, T + 2, 102))
    expect(s.emits).toHaveLength(3)
    s.unsubscribe()
  })
})
