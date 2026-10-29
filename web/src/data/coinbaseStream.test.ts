import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Candle, ConnectionStatus, Ticker } from '../types'
import { CoinbaseProvider } from './coinbase'
import {
  invalidProductIds,
  mergeBars,
  openCoinbaseCandleStream,
  openCoinbaseTickerStream,
  parseFeedMessage,
  parseIsoSeconds,
} from './coinbaseStream'
import { MarketDataError } from './provider'

/** Browser-WebSocket stand-in: handler properties plus addEventListener('open'), send log. */
class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  readonly url: string
  readyState = 0
  closeCalls = 0
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
    this.closeCalls += 1
    this.readyState = 3
  }
  serverOpen(): void {
    this.readyState = 1
    for (const fn of this.openListeners) fn()
    this.onopen?.(new Event('open'))
  }
  serverMessage(data: unknown): void {
    this.onmessage?.({
      data: typeof data === 'string' ? data : JSON.stringify(data),
    } as MessageEvent)
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

const match = (tradeId: number, time: number, price: string, size: string, pid = PID) => ({
  type: 'match',
  trade_id: tradeId,
  maker_order_id: 'm',
  taker_order_id: 't',
  side: 'buy',
  size,
  price,
  product_id: pid,
  sequence: tradeId * 10,
  time: iso(time).replace('Z', '123Z'),
})
const heartbeat = (time: number, pid = PID) => ({
  type: 'heartbeat',
  last_trade_id: 1,
  product_id: pid,
  sequence: 1,
  time: iso(time),
})
const SUBSCRIPTIONS = {
  type: 'subscriptions',
  channels: [{ name: 'matches', product_ids: [PID], account_ids: null }],
}
const subscribeError = (reason: string) => ({
  type: 'error',
  message: 'Failed to subscribe',
  reason,
})

const bar = (time: number, o: number, h: number, l: number, c: number, v: number): Candle => ({
  time,
  open: o,
  high: h,
  low: l,
  close: c,
  volume: v,
})

/** Deferred promise for driving the REST seed/reconcile by hand. */
function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

interface FetchCall {
  limit: number
  endTime: number | undefined
  result: ReturnType<typeof deferred<Candle[]>>
}

/** Local clock for the stream (seconds); tests move it explicitly. */
let localNow = T + 30

function setup(opts?: { resolveProduct?: (s: AbortSignal) => Promise<string> }) {
  const calls: FetchCall[] = []
  const candles: [Candle, boolean][] = []
  /** The `provisional` argument of each onCandle call, parallel to `candles`. */
  const provisional: (boolean | undefined)[] = []
  const statuses: ConnectionStatus[] = []
  /** The last emitted candle at each status report (what the consumer has on screen then). */
  const lastAtStatus: ([Candle, boolean] | undefined)[] = []
  const unsubscribe = openCoinbaseCandleStream({
    WebSocketImpl: WS,
    url: 'wss://feed.test',
    staleTimeoutMs: 60_000,
    bucketStart: (t) => Math.floor(t / 60) * 60,
    resolveProduct: opts?.resolveProduct ?? (() => Promise.resolve(PID)),
    fetchCandles: (limit, endTime) => {
      const result = deferred<Candle[]>()
      calls.push({ limit, endTime, result })
      return result.promise
    },
    onCandle: (c, closed, p) => {
      candles.push([c, closed])
      provisional.push(p)
    },
    onStatus: (s) => {
      statuses.push(s)
      lastAtStatus.push(candles[candles.length - 1])
    },
    reconcileDelayMs: 1_000,
    now: () => localNow * 1000,
  })
  return { calls, candles, provisional, statuses, lastAtStatus, unsubscribe }
}

/** Flush resolved promises (and zero-delay fake timers). */
const flush = () => vi.advanceTimersByTimeAsync(0)

/** Heartbeats once a second from `from` to `to` (inclusive), as the live feed sends them. */
function beats(from: number, to: number) {
  for (let t = from; t <= to; t += 1) last().serverMessage(heartbeat(t))
}

/** Open the socket and complete the REST seed with `rows`. */
async function openAndSeed(s: ReturnType<typeof setup>, rows: Candle[]) {
  await flush()
  last().serverOpen()
  last().serverMessage(SUBSCRIPTIONS)
  const call = s.calls[s.calls.length - 1]
  call.result.resolve(rows)
  await flush()
}

let win: EventTarget
let nav: { onLine: boolean }

beforeEach(() => {
  localNow = T + 30
  FakeWebSocket.instances = []
  vi.useFakeTimers()
  vi.spyOn(Math, 'random').mockReturnValue(1)
  win = new EventTarget()
  nav = { onLine: true }
  vi.stubGlobal('window', win)
  vi.stubGlobal('navigator', nav)
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('parseIsoSeconds', () => {
  it('parses nano-, micro- and whole-second ISO times to floored UNIX seconds', () => {
    expect(parseIsoSeconds('2026-10-25T14:40:43.590266569Z')).toBe(1_792_939_243)
    expect(parseIsoSeconds('2026-10-25T14:41:42.000000Z')).toBe(1_792_939_302)
    expect(parseIsoSeconds('2026-10-25T14:41:42Z')).toBe(1_792_939_302)
  })
  it('rejects junk', () => {
    expect(parseIsoSeconds('yesterday')).toBeNull()
    expect(parseIsoSeconds(1_792_939_302)).toBeNull()
  })
})

describe('parseFeedMessage', () => {
  it('maps match, heartbeat, ticker, subscriptions and error payloads', () => {
    expect(parseFeedMessage(JSON.stringify(match(7, T + 5, '80266.19', '0.0019')))).toEqual({
      type: 'match',
      trade: { productId: PID, tradeId: 7, time: T + 5, price: 80266.19, size: 0.0019 },
    })
    expect(parseFeedMessage(heartbeat(T + 1))).toEqual({
      type: 'heartbeat',
      productId: PID,
      time: T + 1,
    })
    // Shape observed live on ticker_batch (2026-10-25).
    const ticker = {
      type: 'ticker',
      sequence: 1,
      product_id: 'ETH-USD',
      price: '2332.91',
      open_24h: '2339.23',
      volume_24h: '58757.55108387',
      low_24h: '2275.94',
      high_24h: '2368.5',
      volume_30d: '2449944.95852016',
      best_bid: '2332.90',
      best_ask: '2332.91',
      side: 'buy',
      time: '2026-10-25T14:41:41.045031Z',
      trade_id: 850245519,
      last_size: '0.00413794',
    }
    const parsed = parseFeedMessage(ticker)
    expect(parsed?.type).toBe('ticker')
    if (parsed?.type !== 'ticker') return
    expect(parsed.productId).toBe('ETH-USD')
    expect(parsed.ticker.last).toBe(2332.91)
    expect(parsed.ticker.open).toBe(2339.23)
    expect(parsed.ticker.high).toBe(2368.5)
    expect(parsed.ticker.low).toBe(2275.94)
    expect(parsed.ticker.volume).toBe(58757.55108387)
    expect(parsed.ticker.quoteVolume).toBeCloseTo(58757.55108387 * 2332.91, 6)
    expect(parsed.ticker.changePct).toBeCloseTo(((2332.91 - 2339.23) / 2339.23) * 100, 10)
    expect(parsed.ticker.time).toBe(1_792_939_301)
    expect(parseFeedMessage(SUBSCRIPTIONS)).toEqual({ type: 'subscriptions' })
    expect(
      parseFeedMessage({
        type: 'error',
        message: 'Failed to subscribe',
        reason: 'FOO-BAR is not a valid product',
      }),
    ).toEqual({ type: 'error', message: 'Failed to subscribe: FOO-BAR is not a valid product' })
  })

  it('ignores last_match, malformed and unknown messages', () => {
    expect(parseFeedMessage({ ...match(1, T, '1', '1'), type: 'last_match' })).toBeNull()
    expect(parseFeedMessage({ ...match(1, T, '1', '1'), price: 'abc' })).toBeNull()
    expect(parseFeedMessage({ ...match(1, T, '1', '1'), time: 'never' })).toBeNull()
    expect(parseFeedMessage('not json')).toBeNull()
    expect(parseFeedMessage({ type: 'l2update' })).toBeNull()
    expect(parseFeedMessage([1, 2])).toBeNull()
  })
})

describe('mergeBars', () => {
  it('never narrows the range or lowers the volume; close comes from the fuller view', () => {
    const rest = bar(T, 100, 110, 90, 105, 10)
    expect(mergeBars(rest, bar(T, 101, 108, 95, 107, 2))).toEqual(bar(T, 100, 110, 90, 105, 10))
    expect(mergeBars(rest, bar(T, 101, 115, 85, 99, 12))).toEqual(bar(T, 100, 115, 85, 99, 12))
  })

  it('liveHasClose: a heavier REST row missing its last component cannot override the close', () => {
    // REST lags: it lacks the final trades (close 104) but holds more volume overall.
    const rest = bar(T, 100, 112, 88, 104, 20)
    const live = bar(T, 101, 115, 90, 115, 11)
    expect(mergeBars(rest, live)).toEqual(bar(T, 100, 115, 88, 104, 20))
    expect(mergeBars(rest, live, true)).toEqual(bar(T, 100, 115, 88, 115, 20))
  })
})

describe('invalidProductIds', () => {
  it('extracts the ids a feed error names as invalid', () => {
    expect(invalidProductIds('Failed to subscribe: FOO-BAR is not a valid product')).toEqual([
      'FOO-BAR',
    ])
    expect(invalidProductIds('Failed to subscribe: rate limited')).toEqual([])
  })
})

describe('openCoinbaseCandleStream', () => {
  it('subscribes to matches + heartbeat for the product once the socket opens', async () => {
    const s = setup()
    await flush()
    expect(s.statuses).toEqual(['connecting'])
    expect(last().url).toBe('wss://feed.test')
    expect(last().sent).toEqual([])
    last().serverOpen()
    expect(JSON.parse(last().sent[0])).toEqual({
      type: 'subscribe',
      product_ids: [PID],
      channels: ['matches', 'heartbeat'],
    })
    // 'live' only once the server confirms the subscription.
    expect(s.statuses).toEqual(['connecting'])
    last().serverMessage(SUBSCRIPTIONS)
    expect(s.statuses).toEqual(['connecting', 'live'])
    s.unsubscribe()
  })

  it('seeds from REST and extends the forming bar with each trade', async () => {
    const s = setup()
    await openAndSeed(s, [bar(T - 60, 1, 1, 1, 1, 1), bar(T, 100, 110, 90, 105, 10)])
    expect(s.calls[0]).toMatchObject({ limit: 2, endTime: undefined })
    expect(s.candles).toEqual([[bar(T, 100, 110, 90, 105, 10), false]])
    last().serverMessage(match(1, T + 10, '112', '0.5'))
    last().serverMessage(match(2, T + 20, '108', '0.25'))
    expect(s.candles.slice(1)).toEqual([
      [bar(T, 100, 112, 90, 112, 10.5), false],
      [bar(T, 100, 112, 90, 108, 10.75), false],
    ])
    s.unsubscribe()
  })

  it('merges trades received during the seed without double counting or narrowing', async () => {
    const s = setup()
    await flush()
    last().serverOpen()
    last().serverMessage(SUBSCRIPTIONS)
    last().serverMessage(match(1, T + 5, '104', '1'))
    last().serverMessage(match(2, T + 6, '106', '2'))
    expect(s.candles).toEqual([]) // held until the seed lands
    s.calls[0].result.resolve([bar(T, 100, 110, 90, 103, 10)])
    await flush()
    // REST already holds 10 units (maybe including these 3): volume max, range kept, latest close.
    expect(s.candles).toEqual([[bar(T, 100, 110, 90, 106, 10), false]])
    s.unsubscribe()
  })

  it('a failed seed hides the subscription bucket, retries once, then shows the merged bar', async () => {
    const s = setup()
    await flush()
    last().serverOpen()
    last().serverMessage(SUBSCRIPTIONS)
    last().serverMessage(match(1, T + 5, '104', '1'))
    s.calls[0].result.reject(new MarketDataError('network', 'down'))
    await flush()
    last().serverMessage(match(2, T + 6, '106', '2'))
    // A trades-only bar would replace the fuller REST-loaded bar on screen: held back.
    expect(s.candles).toEqual([])
    await vi.advanceTimersByTimeAsync(2_999)
    expect(s.calls).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(s.calls[1]).toMatchObject({ limit: 2, endTime: undefined })
    s.calls[1].result.resolve([bar(T, 100, 110, 90, 103, 10)])
    await flush()
    // REST open/range/volume, the live close (the live bar has every trade since subscribing).
    expect(s.candles).toEqual([[bar(T, 100, 110, 90, 106, 10), false]])
    s.unsubscribe()
  })

  it('if the seed retry fails too, forming bars show from the next bucket on', async () => {
    const s = setup()
    await flush()
    last().serverOpen()
    last().serverMessage(SUBSCRIPTIONS)
    last().serverMessage(match(1, T + 5, '104', '1'))
    s.calls[0].result.reject(new Error('down'))
    await vi.advanceTimersByTimeAsync(3_000)
    s.calls[1].result.reject(new Error('down'))
    await flush()
    last().serverMessage(match(2, T + 20, '105', '1'))
    expect(s.candles).toEqual([])
    last().serverMessage(match(3, T + 61, '120', '2'))
    await vi.advanceTimersByTimeAsync(1_000)
    expect(s.calls[2]).toMatchObject({ limit: 1, endTime: T })
    s.calls[2].result.resolve([bar(T, 100, 110, 90, 105, 10)])
    await flush()
    // Bucket T is still closed (reconciled with REST); only its forming updates were held.
    expect(s.candles).toEqual([
      [bar(T, 100, 110, 90, 105, 10), true],
      [bar(T + 60, 120, 120, 120, 120, 2), false],
    ])
    s.unsubscribe()
  })

  it('never seeds the forming bar from a stale REST row (CDN-cached previous bucket)', async () => {
    localNow = T + 60 + 20 // local clock is in bucket T+60; no heartbeat yet
    const s = setup()
    await openAndSeed(s, [bar(T - 60, 1, 1, 1, 1, 1), bar(T, 100, 110, 90, 105, 0.1)])
    expect(s.candles).toEqual([])
    last().serverMessage(match(1, T + 61, '120', '2'))
    expect(s.candles).toEqual([[bar(T + 60, 120, 120, 120, 120, 2), false]])
    s.unsubscribe()
  })

  it('the server clock wins over the local clock for the seed decision', async () => {
    localNow = T + 60 + 1 // local clock runs ahead into the next bucket
    const s = setup()
    await flush()
    last().serverOpen()
    last().serverMessage(heartbeat(T + 59))
    last().serverMessage(SUBSCRIPTIONS)
    s.calls[0].result.resolve([bar(T, 100, 110, 90, 105, 10)])
    await flush()
    expect(s.candles).toEqual([[bar(T, 100, 110, 90, 105, 10), false]])
    s.unsubscribe()
  })

  it('ignores duplicate trade ids and other products', async () => {
    const s = setup()
    await openAndSeed(s, [])
    last().serverMessage(match(5, T + 1, '100', '1'))
    last().serverMessage(match(5, T + 2, '200', '1'))
    last().serverMessage(match(4, T + 2, '300', '1'))
    last().serverMessage(match(6, T + 3, '400', '1', 'ETH-USD'))
    expect(s.candles).toEqual([[bar(T, 100, 100, 100, 100, 1), false]])
    s.unsubscribe()
  })

  it('closes a bucket on roll, reconciled from REST, before the next bar is shown', async () => {
    const s = setup()
    await openAndSeed(s, [bar(T, 100, 110, 90, 105, 10)])
    last().serverMessage(match(1, T + 50, '111', '1')) // live: 100/111/90/111/11
    beats(T + 51, T + 59)
    last().serverMessage(match(2, T + 61, '120', '2')) // next bucket: held
    expect(s.candles).toHaveLength(2)

    await vi.advanceTimersByTimeAsync(999)
    expect(s.calls).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(s.calls[1]).toMatchObject({ limit: 1, endTime: T })
    // REST has more volume (trades before the seed's snapshot), but the live view was subscribed
    // through the roll, so the last trade it saw (111) is the close.
    s.calls[1].result.resolve([bar(T, 100, 111, 89, 110, 12)])
    await flush()
    expect(s.candles.slice(2)).toEqual([
      [bar(T, 100, 111, 89, 111, 12), true],
      [bar(T + 60, 120, 120, 120, 120, 2), false],
    ])
    s.unsubscribe()
  })

  it('a REST bar lagging behind the live view is merged, not trusted blindly', async () => {
    const s = setup()
    await openAndSeed(s, [bar(T, 100, 110, 90, 105, 10)])
    last().serverMessage(match(1, T + 55, '115', '5')) // live: 100/115/90/115/15
    last().serverMessage(heartbeat(T + 60))
    await vi.advanceTimersByTimeAsync(1_000)
    s.calls[1].result.resolve([bar(T, 100, 110, 90, 105, 10)])
    await flush()
    expect(s.candles[s.candles.length - 1]).toEqual([bar(T, 100, 115, 90, 115, 15), true])
    s.unsubscribe()
  })

  it('emits the live bar as closed when the REST read-back fails', async () => {
    const s = setup()
    await openAndSeed(s, [])
    last().serverMessage(match(1, T + 1, '100', '1'))
    last().serverMessage(heartbeat(T + 60))
    await vi.advanceTimersByTimeAsync(1_000)
    s.calls[1].result.reject(new Error('boom'))
    await flush()
    expect(s.candles[s.candles.length - 1]).toEqual([bar(T, 100, 100, 100, 100, 1), true])
    s.unsubscribe()
  })

  it('folds a late trade into the bar awaiting its close', async () => {
    const s = setup()
    await openAndSeed(s, [])
    last().serverMessage(match(1, T + 1, '100', '1'))
    last().serverMessage(heartbeat(T + 60))
    last().serverMessage(match(2, T + 59, '90', '1')) // arrives after the roll
    await vi.advanceTimersByTimeAsync(1_000)
    s.calls[1].result.resolve([])
    await flush()
    expect(s.candles[s.candles.length - 1]).toEqual([bar(T, 100, 100, 90, 90, 2), true])
    s.unsubscribe()
  })

  it('does not forward-fill buckets without trades', async () => {
    const s = setup()
    await openAndSeed(s, [])
    last().serverMessage(match(1, T + 1, '100', '1'))
    for (let t = T + 60; t <= T + 300; t += 30) last().serverMessage(heartbeat(t))
    await vi.advanceTimersByTimeAsync(1_000)
    s.calls[1].result.resolve([])
    await vi.advanceTimersByTimeAsync(5_000)
    // REST lacked the row, so bucket T is read back once more; empty buckets are never fetched.
    expect(s.calls).toHaveLength(3)
    expect(s.calls[2]).toMatchObject({ limit: 1, endTime: T })
    s.calls[2].result.resolve([])
    await flush()
    // Closed provisional, then (REST still has no row) the same bar settled as final.
    expect(s.candles).toEqual([
      [bar(T, 100, 100, 100, 100, 1), false],
      [bar(T, 100, 100, 100, 100, 1), true],
      [bar(T, 100, 100, 100, 100, 1), true],
    ])
    expect(s.provisional).toEqual([undefined, true, undefined])
    last().serverMessage(match(2, T + 301, '101', '1'))
    expect(s.candles[s.candles.length - 1]).toEqual([bar(T + 300, 101, 101, 101, 101, 1), false])
    s.unsubscribe()
  })

  it('reconnects with backoff, resubscribes and re-seeds', async () => {
    const s = setup()
    await openAndSeed(s, [bar(T, 100, 110, 90, 105, 10)])
    last().serverMessage(match(1, T + 10, '111', '1'))
    const first = last()
    first.serverClose()
    expect(s.statuses).toEqual(['connecting', 'live', 'reconnecting'])
    expect(first.onmessage).toBeNull()
    await vi.advanceTimersByTimeAsync(1_000)
    expect(FakeWebSocket.instances).toHaveLength(2)
    last().serverOpen()
    expect(JSON.parse(last().sent[0]).product_ids).toEqual([PID])
    last().serverMessage(SUBSCRIPTIONS)
    expect(s.statuses).toEqual(['connecting', 'live', 'reconnecting', 'live'])
    expect(s.calls).toHaveLength(2)
    // REST now knows more than our pre-drop live bar: merged, nothing lost.
    s.calls[1].result.resolve([bar(T, 100, 112, 88, 109, 14)])
    await flush()
    expect(s.candles[s.candles.length - 1]).toEqual([bar(T, 100, 112, 88, 109, 14), false])
    s.unsubscribe()
  })

  it('reconnects a stale socket, reporting the stall (heartbeats are 1/s)', async () => {
    const s = setup()
    await openAndSeed(s, [])
    await vi.advanceTimersByTimeAsync(60_000)
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(FakeWebSocket.instances[0].closeCalls).toBe(1)
    expect(s.statuses).toEqual(['connecting', 'live', 'reconnecting'])
    await vi.advanceTimersByTimeAsync(1_000)
    expect(FakeWebSocket.instances).toHaveLength(2)
    last().serverOpen()
    last().serverMessage(SUBSCRIPTIONS)
    // reconnecting -> live is what makes the consumer gap-fill.
    expect(s.statuses).toEqual(['connecting', 'live', 'reconnecting', 'live'])
    s.unsubscribe()
  })

  it('a stall: reconnecting -> live, then the skipped buckets are backfilled in order', async () => {
    const s = setup()
    await openAndSeed(s, [bar(T, 100, 110, 90, 105, 10)])
    last().serverMessage(match(1, T + 10, '111', '1'))
    await vi.advanceTimersByTimeAsync(60_000) // silent socket: stale
    await vi.advanceTimersByTimeAsync(1_000)
    last().serverOpen()
    last().serverMessage(SUBSCRIPTIONS) // re-seed: calls[1]
    last().serverMessage(heartbeat(T + 305)) // five buckets later
    expect(s.statuses).toEqual(['connecting', 'live', 'reconnecting', 'live'])
    s.calls[1].result.resolve([bar(T + 300, 130, 131, 129, 130, 3)])
    await flush()
    await vi.advanceTimersByTimeAsync(1_000)
    expect(s.calls[2]).toMatchObject({ limit: 1, endTime: T })
    s.calls[2].result.resolve([bar(T, 100, 112, 90, 108, 14)])
    await flush()
    await vi.advanceTimersByTimeAsync(1_000)
    // The skipped buckets T+60..T+240 over REST; a quiet pair simply has fewer rows.
    expect(s.calls[3]).toMatchObject({ limit: 4, endTime: T + 240 })
    s.calls[3].result.resolve([
      bar(T + 60, 120, 121, 119, 120, 1),
      bar(T + 180, 125, 126, 124, 125, 1),
    ])
    await flush()
    expect(s.candles.slice(2)).toEqual([
      // Not subscribed through the roll: REST, the fuller view, gives the close.
      [bar(T, 100, 112, 90, 108, 14), true],
      [bar(T + 60, 120, 121, 119, 120, 1), true],
      [bar(T + 180, 125, 126, 124, 125, 1), true],
      [bar(T + 300, 130, 131, 129, 130, 3), false],
    ])
    s.unsubscribe()
  })

  it('a seed failing after a stall hides the bucket the new subscription started in', async () => {
    const s = setup()
    await openAndSeed(s, [bar(T, 100, 110, 90, 105, 10)])
    last().serverMessage(match(1, T + 10, '111', '1'))
    await vi.advanceTimersByTimeAsync(61_000) // stall + reconnect
    last().serverOpen()
    last().serverMessage(SUBSCRIPTIONS)
    s.calls[1].result.reject(new Error('down')) // before any feed time on the new subscription
    await flush()
    last().serverMessage(heartbeat(T + 305))
    last().serverMessage(match(2, T + 306, '130', '1'))
    await vi.advanceTimersByTimeAsync(1_000)
    s.calls[2].result.resolve([bar(T, 100, 111, 90, 111, 11)])
    await vi.advanceTimersByTimeAsync(1_000)
    s.calls[3].result.resolve([])
    await flush()
    expect(s.candles.some(([c, closed]) => c.time === T + 300 && !closed)).toBe(false)
    expect(s.candles[s.candles.length - 1]).toEqual([bar(T, 100, 111, 90, 111, 11), true])
    s.unsubscribe()
  })

  it('a clock jump on a live socket (sleep/wake) backfills the skipped buckets', async () => {
    const s = setup()
    await openAndSeed(s, [])
    last().serverMessage(match(1, T + 30, '100', '1'))
    last().serverMessage(heartbeat(T + 200)) // T+60 and T+120 were skipped
    last().serverMessage(match(2, T + 201, '140', '1'))
    await vi.advanceTimersByTimeAsync(1_000)
    s.calls[1].result.resolve([bar(T, 100, 100, 100, 100, 1)])
    await flush()
    await vi.advanceTimersByTimeAsync(1_000)
    expect(s.calls[2]).toMatchObject({ limit: 2, endTime: T + 120 })
    // Rows outside the skipped range are ignored.
    s.calls[2].result.resolve([
      bar(T, 1, 1, 1, 1, 1),
      bar(T + 60, 110, 110, 110, 110, 1),
      bar(T + 120, 120, 120, 120, 120, 1),
    ])
    await flush()
    expect(s.candles.slice(1)).toEqual([
      [bar(T, 100, 100, 100, 100, 1), true],
      [bar(T + 60, 110, 110, 110, 110, 1), true],
      [bar(T + 120, 120, 120, 120, 120, 1), true],
      [bar(T + 180, 140, 140, 140, 140, 1), false],
    ])
    s.unsubscribe()
  })

  it('the live close wins when REST lacks the last component, even with more volume', async () => {
    const s = setup()
    await openAndSeed(s, [bar(T, 100, 110, 90, 105, 10)])
    last().serverMessage(match(1, T + 55, '115', '1')) // live: 100/115/90/115/11
    beats(T + 56, T + 60)
    await vi.advanceTimersByTimeAsync(1_000)
    s.calls[1].result.resolve([bar(T, 100, 112, 88, 104, 20)])
    await flush()
    expect(s.candles[s.candles.length - 1]).toEqual([bar(T, 100, 115, 88, 115, 20), true])
    s.unsubscribe()
  })

  it('a failed read-back of an incomplete live bar is retried and the bar re-emitted', async () => {
    const s = setup()
    await openAndSeed(s, [bar(T, 100, 110, 90, 105, 10)])
    last().serverMessage(match(1, T + 50, '111', '1'))
    beats(T + 51, T + 60)
    await vi.advanceTimersByTimeAsync(1_000)
    s.calls[1].result.reject(new Error('boom'))
    await flush()
    last().serverMessage(match(2, T + 61, '120', '2'))
    expect(s.candles.slice(2)).toEqual([
      [bar(T, 100, 111, 90, 111, 11), true],
      [bar(T + 60, 120, 120, 120, 120, 2), false],
    ])
    await vi.advanceTimersByTimeAsync(5_000)
    expect(s.calls[2]).toMatchObject({ limit: 1, endTime: T })
    s.calls[2].result.resolve([bar(T, 100, 111, 85, 110, 13)])
    await flush()
    // Same time as an already-closed bar: the consumer replaces it (applyLiveCandle).
    expect(s.candles[s.candles.length - 1]).toEqual([bar(T, 100, 111, 85, 111, 13), true])
    s.unsubscribe()
  })

  it('a live view subscribed before the bucket opened is complete: no second read-back', async () => {
    const s = setup()
    await openAndSeed(s, [])
    beats(T - 5, T)
    last().serverMessage(match(1, T + 1, '100', '1'))
    beats(T + 2, T + 60)
    await vi.advanceTimersByTimeAsync(1_000)
    s.calls[1].result.reject(new Error('boom'))
    await vi.advanceTimersByTimeAsync(10_000)
    expect(s.calls).toHaveLength(2)
    expect(s.candles[s.candles.length - 1]).toEqual([bar(T, 100, 100, 100, 100, 1), true])
    s.unsubscribe()
  })

  it('"not a valid product" goes offline and stops at once', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const s = setup()
    await flush()
    last().serverOpen()
    last().serverMessage(subscribeError('BTC-USD is not a valid product'))
    expect(s.statuses).toEqual(['connecting', 'offline'])
    expect(vi.getTimerCount()).toBe(0)
    await vi.advanceTimersByTimeAsync(120_000)
    expect(FakeWebSocket.instances).toHaveLength(1)
  })

  it('a rejected subscribe never reads live; it backs off and stops after 3 in a row', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const s = setup()
    await flush()
    last().serverOpen()
    last().serverMessage(subscribeError('rate limited'))
    expect(s.statuses).toEqual(['connecting', 'reconnecting'])
    expect(last().closeCalls).toBe(1)
    await vi.advanceTimersByTimeAsync(1_000)
    expect(FakeWebSocket.instances).toHaveLength(2)
    last().serverOpen()
    last().serverMessage(subscribeError('rate limited'))
    await vi.advanceTimersByTimeAsync(1_999)
    expect(FakeWebSocket.instances).toHaveLength(2) // escalating backoff
    await vi.advanceTimersByTimeAsync(1)
    last().serverOpen()
    last().serverMessage(subscribeError('rate limited'))
    expect(s.statuses).toEqual(['connecting', 'reconnecting', 'offline'])
    expect(vi.getTimerCount()).toBe(0)
    await vi.advanceTimersByTimeAsync(120_000)
    expect(FakeWebSocket.instances).toHaveLength(3)
  })

  it('a confirmed subscribe resets the rejection count', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const s = setup()
    await flush()
    for (let i = 0; i < 2; i++) {
      last().serverOpen()
      last().serverMessage(subscribeError('busy'))
      await vi.advanceTimersByTimeAsync(30_000)
    }
    last().serverOpen()
    last().serverMessage(SUBSCRIPTIONS)
    expect(s.statuses).toEqual(['connecting', 'reconnecting', 'live'])
    last().serverClose()
    for (let i = 0; i < 2; i++) {
      await vi.advanceTimersByTimeAsync(30_000)
      last().serverOpen()
      last().serverMessage(subscribeError('busy'))
    }
    expect(s.statuses[s.statuses.length - 1]).toBe('reconnecting')
    // An error after the subscription is confirmed is only logged.
    await vi.advanceTimersByTimeAsync(30_000)
    last().serverOpen()
    last().serverMessage(SUBSCRIPTIONS)
    last().serverMessage(subscribeError('something odd'))
    expect(s.statuses[s.statuses.length - 1]).toBe('live')
    s.unsubscribe()
  })

  it('unsubscribe closes the socket, clears every timer and is idempotent', async () => {
    const s = setup()
    await openAndSeed(s, [])
    last().serverMessage(match(1, T + 1, '100', '1'))
    last().serverMessage(heartbeat(T + 60)) // reconcile delay timer pending
    const sock = last()
    const before = s.candles.length
    s.unsubscribe()
    s.unsubscribe()
    expect(sock.closeCalls).toBe(1)
    expect(sock.onmessage).toBeNull()
    expect(vi.getTimerCount()).toBe(0)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(s.candles).toHaveLength(before)
    expect(s.statuses).toEqual(['connecting', 'live'])
  })

  it('unsubscribe before the product resolves opens no socket', async () => {
    const pending = deferred<string>()
    const s = setup({ resolveProduct: () => pending.promise })
    s.unsubscribe()
    pending.resolve(PID)
    await flush()
    expect(FakeWebSocket.instances).toHaveLength(0)
  })

  it('an unknown symbol goes offline and stops', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const s = setup({
      resolveProduct: () => Promise.reject(new MarketDataError('bad-symbol', 'Unknown symbol')),
    })
    await vi.advanceTimersByTimeAsync(60_000)
    expect(s.statuses).toEqual(['connecting', 'offline'])
    expect(FakeWebSocket.instances).toHaveLength(0)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('retries product resolution with backoff on network errors', async () => {
    let n = 0
    const s = setup({
      resolveProduct: () =>
        ++n === 1 ? Promise.reject(new MarketDataError('network', 'down')) : Promise.resolve(PID),
    })
    await flush()
    expect(s.statuses).toEqual(['connecting', 'reconnecting'])
    expect(FakeWebSocket.instances).toHaveLength(0)
    await vi.advanceTimersByTimeAsync(1_000)
    expect(FakeWebSocket.instances).toHaveLength(1)
    last().serverOpen()
    last().serverMessage(SUBSCRIPTIONS)
    expect(s.statuses).toEqual(['connecting', 'reconnecting', 'live'])
    s.unsubscribe()
  })
})

/** REST calls with this `endTime` (read-backs use the bucket time; seeds use undefined). */
const callsAt = (s: ReturnType<typeof setup>, endTime: number | undefined) =>
  s.calls.filter((c) => c.endTime === endTime)
/** Closed bars emitted for bucket `time`. */
const closedAt = (s: ReturnType<typeof setup>, time: number) =>
  s.candles.filter(([c, closed]) => c.time === time && closed).map(([c]) => c)

/** Seed bar T (close 100), one trade at T+6 (100), then a same-bucket reconnect. */
async function reconnectInBucket(s: ReturnType<typeof setup>) {
  await openAndSeed(s, [bar(T, 100, 110, 90, 100, 10)])
  last().serverMessage(heartbeat(T + 5))
  last().serverMessage(match(1, T + 6, '100', '1')) // live: 100/110/90/100/11
  last().serverClose() // trades at 105 happen meanwhile: only REST sees them
  await vi.advanceTimersByTimeAsync(1_000)
  last().serverOpen()
  last().serverMessage(SUBSCRIPTIONS) // re-seed: calls[1]
  beats(T + 20, T + 25) // same bucket, continuous from here on
}

describe('openCoinbaseCandleStream: when the live close is trusted', () => {
  it('P1: a close kept from before a same-bucket reconnect never overrides a fresher REST close', async () => {
    const s = setup()
    await reconnectInBucket(s)
    s.calls[1].result.reject(new Error('down')) // seed fails; no trade on the new subscription
    await flush()
    beats(T + 26, T + 61) // continuous through the roll
    await vi.advanceTimersByTimeAsync(1_000)
    const [first] = callsAt(s, T)
    expect(first).toMatchObject({ limit: 1, endTime: T })
    first.result.resolve([bar(T, 100, 110, 90, 105, 14)])
    await flush()
    // Not set on this subscription: the fuller view (REST, 14 > 11) gives the close.
    expect(closedAt(s, T)).toEqual([bar(T, 100, 110, 90, 105, 14)])
    // Not known complete, so read back once more; an unchanged row is re-sent with the same
    // values only to settle the provisional close.
    await vi.advanceTimersByTimeAsync(5_000)
    const reads = callsAt(s, T)
    expect(reads).toHaveLength(2)
    reads[1].result.resolve([bar(T, 100, 110, 90, 105, 14)])
    await flush()
    expect(closedAt(s, T)).toEqual([bar(T, 100, 110, 90, 105, 14), bar(T, 100, 110, 90, 105, 14)])
    expect(s.provisional.slice(-2)).toEqual([true, undefined])
    s.unsubscribe()
  })

  it('P2: a seed retry keeps the live close only if a trade on this subscription set it', async () => {
    const s = setup()
    await reconnectInBucket(s)
    s.calls[1].result.reject(new Error('down'))
    await vi.advanceTimersByTimeAsync(3_000) // seed retry
    expect(s.calls[2]).toMatchObject({ limit: 2, endTime: undefined })
    s.calls[2].result.resolve([bar(T, 100, 110, 90, 105, 14)])
    await flush()
    expect(s.candles[s.candles.length - 1]).toEqual([bar(T, 100, 110, 90, 105, 14), false])
    s.unsubscribe()
  })

  it('P3: a feed jump on the same socket (lost messages) drops the live close', async () => {
    const s = setup()
    await openAndSeed(s, [])
    last().serverMessage(match(1, T + 30, '100', '1'))
    last().serverMessage(heartbeat(T + 200)) // 170 s of silence on an open socket
    await vi.advanceTimersByTimeAsync(1_000)
    callsAt(s, T)[0].result.resolve([bar(T, 100, 105, 100, 105, 2)])
    await flush()
    expect(closedAt(s, T)).toEqual([bar(T, 100, 105, 100, 105, 2)])
    s.unsubscribe()
  })

  it('a feed gap inside the bucket after the last trade drops the live close', async () => {
    const s = setup()
    await openAndSeed(s, [bar(T, 100, 110, 90, 105, 10)])
    last().serverMessage(match(1, T + 5, '111', '1')) // live: 100/111/90/111/11
    beats(T + 10, T + 60) // 5 s gap after the trade: a trade may have been lost
    await vi.advanceTimersByTimeAsync(1_000)
    // The live close lost its authority at the gap, so the fuller view (REST, 12 > 11) gives
    // the close. Compare the next test: the same rows with a continuous feed keep 111.
    callsAt(s, T)[0].result.resolve([bar(T, 100, 111, 90, 108, 12)])
    await flush()
    expect(closedAt(s, T)).toEqual([bar(T, 100, 111, 90, 108, 12)])
    s.unsubscribe()
  })

  it('a continuous feed after a trade on this subscription keeps the live close', async () => {
    const s = setup()
    await openAndSeed(s, [bar(T, 100, 110, 90, 105, 10)])
    last().serverMessage(match(1, T + 5, '111', '1'))
    beats(T + 6, T + 60)
    await vi.advanceTimersByTimeAsync(1_000)
    callsAt(s, T)[0].result.resolve([bar(T, 100, 111, 90, 108, 12)])
    await flush()
    expect(closedAt(s, T)).toEqual([bar(T, 100, 111, 90, 111, 12)])
    s.unsubscribe()
  })

  it('a lagging first read-back is read back again and the corrected bar re-emitted', async () => {
    const s = setup()
    await openAndSeed(s, [bar(T, 100, 110, 90, 105, 10)])
    last().serverMessage(match(1, T + 50, '111', '1')) // live: 100/111/90/111/11
    beats(T + 51, T + 60)
    await vi.advanceTimersByTimeAsync(1_000)
    // The CDN still serves the seed-time row: the live view is fuller.
    callsAt(s, T)[0].result.resolve([bar(T, 100, 110, 90, 105, 10)])
    await flush()
    expect(closedAt(s, T)).toEqual([bar(T, 100, 111, 90, 111, 11)])
    await vi.advanceTimersByTimeAsync(5_000)
    const reads = callsAt(s, T)
    expect(reads).toHaveLength(2)
    // The fresh row has trades from before the seed snapshot (lower low, more volume).
    reads[1].result.resolve([bar(T, 100, 111, 88, 111, 13)])
    await flush()
    expect(closedAt(s, T)).toEqual([bar(T, 100, 111, 90, 111, 11), bar(T, 100, 111, 88, 111, 13)])
    s.unsubscribe()
  })

  it('a bucket seen continuously from its open with a live close needs no second read-back', async () => {
    const s = setup()
    await openAndSeed(s, [])
    beats(T - 5, T)
    last().serverMessage(match(1, T + 1, '100', '1'))
    beats(T + 2, T + 60)
    await vi.advanceTimersByTimeAsync(1_000)
    callsAt(s, T)[0].result.resolve([bar(T, 100, 100, 100, 100, 1)])
    await vi.advanceTimersByTimeAsync(10_000)
    expect(callsAt(s, T)).toHaveLength(1)
    expect(closedAt(s, T)).toEqual([bar(T, 100, 100, 100, 100, 1)])
    s.unsubscribe()
  })
})

describe('openCoinbaseCandleStream: provisional closes (a second read-back is pending)', () => {
  /** [closed, provisional] of every emission of bucket `time`. */
  const flagsAt = (s: ReturnType<typeof setup>, time: number) =>
    s.candles.flatMap(([c, closed], i) => (c.time === time ? [[closed, s.provisional[i]]] : []))

  it('a close not known complete is provisional; the corrected second read-back settles it', async () => {
    const s = setup()
    await openAndSeed(s, [bar(T, 100, 110, 90, 105, 10)])
    last().serverMessage(match(1, T + 50, '111', '1'))
    beats(T + 51, T + 60)
    await vi.advanceTimersByTimeAsync(1_000)
    callsAt(s, T)[0].result.resolve([bar(T, 100, 110, 90, 105, 10)])
    await flush()
    last().serverMessage(match(2, T + 61, '120', '2')) // the next bar forms meanwhile
    await vi.advanceTimersByTimeAsync(5_000)
    callsAt(s, T)[1].result.resolve([bar(T, 100, 111, 88, 111, 13)])
    await flush()
    expect(closedAt(s, T)).toEqual([bar(T, 100, 111, 90, 111, 11), bar(T, 100, 111, 88, 111, 13)])
    expect(flagsAt(s, T).filter(([closed]) => closed)).toEqual([
      [true, true],
      [true, undefined],
    ])
    s.unsubscribe()
  })

  it('a failed second read-back settles the provisional bar with its values', async () => {
    const s = setup()
    await openAndSeed(s, [bar(T, 100, 110, 90, 105, 10)])
    last().serverMessage(match(1, T + 50, '111', '1'))
    beats(T + 51, T + 60)
    await vi.advanceTimersByTimeAsync(1_000)
    callsAt(s, T)[0].result.reject(new Error('boom'))
    await flush()
    await vi.advanceTimersByTimeAsync(5_000)
    callsAt(s, T)[1].result.reject(new Error('boom'))
    await flush()
    expect(closedAt(s, T)).toEqual([bar(T, 100, 111, 90, 111, 11), bar(T, 100, 111, 90, 111, 11)])
    expect(flagsAt(s, T).filter(([closed]) => closed)).toEqual([
      [true, true],
      [true, undefined],
    ])
    s.unsubscribe()
  })

  it('a live view known complete closes final at once (no flag, no second send)', async () => {
    const s = setup()
    await openAndSeed(s, [])
    beats(T - 5, T)
    last().serverMessage(match(1, T + 1, '100', '1'))
    beats(T + 2, T + 60)
    await vi.advanceTimersByTimeAsync(1_000)
    callsAt(s, T)[0].result.resolve([bar(T, 100, 100, 100, 100, 1)])
    await vi.advanceTimersByTimeAsync(10_000)
    expect(flagsAt(s, T).filter(([closed]) => closed)).toEqual([[true, undefined]])
    s.unsubscribe()
  })

  it('forming bars never carry the flag; only the newest stall-backfill row does', async () => {
    const s = setup()
    await openAndSeed(s, [])
    last().serverMessage(match(1, T + 30, '100', '1'))
    last().serverMessage(heartbeat(T + 200))
    last().serverMessage(match(2, T + 201, '140', '1'))
    await vi.advanceTimersByTimeAsync(1_000)
    callsAt(s, T)[0].result.resolve([bar(T, 100, 100, 100, 100, 1)])
    await flush()
    await vi.advanceTimersByTimeAsync(1_000)
    s.calls[2].result.resolve([
      bar(T + 60, 110, 110, 110, 110, 1),
      bar(T + 120, 120, 121, 119, 120, 1),
    ])
    await flush()
    expect(flagsAt(s, T + 60)).toEqual([[true, undefined]])
    // The newest row of the run ended last: REST may still lag on it, like any read-back.
    expect(flagsAt(s, T + 120)).toEqual([[true, true]])
    expect(flagsAt(s, T + 180)).toEqual([[false, undefined]])
    expect(s.candles.every(([, closed], i) => closed || s.provisional[i] === undefined)).toBe(true)
    s.unsubscribe()
  })

  it('the provisional newest stall-backfill row is read back once more and settled', async () => {
    const s = setup()
    await openAndSeed(s, [])
    last().serverMessage(match(1, T + 30, '100', '1'))
    last().serverMessage(heartbeat(T + 200))
    await vi.advanceTimersByTimeAsync(1_000)
    callsAt(s, T)[0].result.resolve([bar(T, 100, 100, 100, 100, 1)])
    await flush()
    await vi.advanceTimersByTimeAsync(1_000)
    const [gap] = callsAt(s, T + 120)
    expect(gap).toMatchObject({ limit: 2, endTime: T + 120 })
    gap.result.resolve([bar(T + 120, 120, 121, 119, 120, 1)])
    await flush()
    await vi.advanceTimersByTimeAsync(4_999)
    expect(callsAt(s, T + 120)).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    const readBack = callsAt(s, T + 120)[1]
    expect(readBack).toMatchObject({ limit: 1, endTime: T + 120 })
    // The lagging first answer missed a late trade: the fuller row corrects it.
    readBack.result.resolve([bar(T + 120, 120, 125, 119, 124, 2)])
    await flush()
    expect(closedAt(s, T + 120)).toEqual([
      bar(T + 120, 120, 121, 119, 120, 1),
      bar(T + 120, 120, 125, 119, 124, 2),
    ])
    expect(flagsAt(s, T + 120)).toEqual([
      [true, true],
      [true, undefined],
    ])
    s.unsubscribe()
  })

  it('a failed read-back of the newest stall-backfill row settles it with its values', async () => {
    const s = setup()
    await openAndSeed(s, [])
    last().serverMessage(match(1, T + 30, '100', '1'))
    last().serverMessage(heartbeat(T + 200))
    await vi.advanceTimersByTimeAsync(1_000)
    callsAt(s, T)[0].result.resolve([bar(T, 100, 100, 100, 100, 1)])
    await flush()
    await vi.advanceTimersByTimeAsync(1_000)
    callsAt(s, T + 120)[0].result.resolve([bar(T + 120, 120, 121, 119, 120, 1)])
    await flush()
    await vi.advanceTimersByTimeAsync(5_000)
    callsAt(s, T + 120)[1].result.reject(new Error('down'))
    await flush()
    expect(flagsAt(s, T + 120)).toEqual([
      [true, true],
      [true, undefined],
    ])
    expect(closedAt(s, T + 120)).toEqual([
      bar(T + 120, 120, 121, 119, 120, 1),
      bar(T + 120, 120, 121, 119, 120, 1),
    ])
    s.unsubscribe()
  })
})

describe('openCoinbaseCandleStream: stall backfill failures', () => {
  /** Trade in T, then a jump to T+200 on the open socket: bar T plus gap T+60..T+120. */
  async function jumped(s: ReturnType<typeof setup>) {
    await openAndSeed(s, [])
    last().serverMessage(match(1, T + 30, '100', '1'))
    last().serverMessage(heartbeat(T + 200))
    last().serverMessage(match(2, T + 201, '140', '1'))
    await vi.advanceTimersByTimeAsync(1_000)
    callsAt(s, T)[0].result.resolve([bar(T, 100, 100, 100, 100, 1)])
    await vi.advanceTimersByTimeAsync(1_000)
    const [gap] = callsAt(s, T + 120)
    expect(gap).toMatchObject({ limit: 2, endTime: T + 120 })
    gap.result.reject(new MarketDataError('rate-limited', '429', 429))
    await flush()
  }

  it('a failed backfill is retried once after the retry delay, and its rows emitted', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const s = setup()
    await jumped(s)
    await vi.advanceTimersByTimeAsync(4_999)
    expect(callsAt(s, T + 120)).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    const retry = callsAt(s, T + 120)[1]
    expect(retry).toMatchObject({ limit: 2, endTime: T + 120 })
    retry.result.resolve([bar(T + 60, 110, 110, 110, 110, 1), bar(T + 120, 120, 120, 120, 120, 1)])
    await flush()
    expect(s.candles.slice(1)).toEqual([
      [bar(T, 100, 100, 100, 100, 1), true],
      [bar(T + 60, 110, 110, 110, 110, 1), true],
      [bar(T + 120, 120, 120, 120, 120, 1), true],
      [bar(T + 180, 140, 140, 140, 140, 1), false],
    ])
    expect(s.statuses).toEqual(['connecting', 'live'])
    s.unsubscribe()
  })

  it('a backfill failing twice on a live socket pulses reconnecting -> live for a gap fill', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const s = setup()
    await jumped(s)
    await vi.advanceTimersByTimeAsync(5_000)
    callsAt(s, T + 120)[1].result.reject(new Error('timeout'))
    await flush()
    // The consumer gap-fills on reconnecting -> live; the stream carries on in order.
    expect(s.statuses).toEqual(['connecting', 'live', 'reconnecting', 'live'])
    expect(s.candles[s.candles.length - 1]).toEqual([bar(T + 180, 140, 140, 140, 140, 1), false])
    expect(callsAt(s, T + 120)).toHaveLength(2)
    s.unsubscribe()
  })
})

describe('openCoinbaseCandleStream: stall backfill failing while the socket is down', () => {
  const PRE_GAP: [Candle, boolean] = [bar(T, 100, 100, 100, 100, 1), true]

  /**
   * Trade in T, a jump to T+200 on a live socket (gap T+60..T+120), the first backfill fails,
   * the network drops during the retry window and the retry fails with the socket down.
   */
  async function failedWhileDown(s: ReturnType<typeof setup>) {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    await openAndSeed(s, [])
    last().serverMessage(match(1, T + 30, '100', '1'))
    last().serverMessage(heartbeat(T + 200))
    last().serverMessage(match(2, T + 201, '140', '1'))
    await vi.advanceTimersByTimeAsync(1_000)
    callsAt(s, T)[0].result.resolve([bar(T, 100, 100, 100, 100, 1)])
    await vi.advanceTimersByTimeAsync(1_000)
    callsAt(s, T + 120)[0].result.reject(new Error('down'))
    await flush()
    last().serverClose() // the network drops during the retry window
    expect(s.statuses).toEqual(['connecting', 'live', 'reconnecting'])
    await vi.advanceTimersByTimeAsync(5_000)
    expect(FakeWebSocket.instances).toHaveLength(2) // reconnecting, not open yet
    callsAt(s, T + 120)[1].result.reject(new Error('down'))
    await flush()
    // No pulse is possible, and nothing past the pre-gap bar is shown.
    expect(s.statuses).toEqual(['connecting', 'live', 'reconnecting'])
    expect(s.candles[s.candles.length - 1]).toEqual(PRE_GAP)
  }

  /** Then the new socket opens and its first message is a trade in T+240, which reads 'live'. */
  async function jumpedWhileDown(s: ReturnType<typeof setup>) {
    await failedWhileDown(s)
    last().serverOpen()
    last().serverMessage(match(3, T + 250, '150', '2')) // first message: 'live'
    expect(s.statuses).toEqual(['connecting', 'live', 'reconnecting', 'live'])
    // The consumer sees 'live' with the pre-gap bar still last: its own check gap-fills.
    expect(s.lastAtStatus[3]).toEqual(PRE_GAP)
    expect(s.candles[s.candles.length - 1]).toEqual(PRE_GAP)
  }

  it("the socket's 'live' retries the backfill once more; its rows are emitted in order", async () => {
    const s = setup()
    await jumpedWhileDown(s)
    expect(callsAt(s, T + 120)).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(1_000)
    const retry = callsAt(s, T + 120)
    expect(retry).toHaveLength(3)
    expect(retry[2]).toMatchObject({ limit: 2, endTime: T + 120 })
    retry[2].result.resolve([
      bar(T + 60, 110, 110, 110, 110, 1),
      bar(T + 120, 120, 120, 120, 120, 1),
    ])
    await flush()
    expect(s.candles[s.candles.length - 1]).toEqual([bar(T + 120, 120, 120, 120, 120, 1), true])
    await vi.advanceTimersByTimeAsync(1_000)
    callsAt(s, T + 180)[0].result.resolve([bar(T + 180, 140, 141, 139, 141, 3)])
    await flush()
    expect(s.candles.slice(1)).toEqual([
      PRE_GAP,
      [bar(T + 60, 110, 110, 110, 110, 1), true],
      [bar(T + 120, 120, 120, 120, 120, 1), true],
      [bar(T + 180, 140, 141, 139, 141, 3), true],
      [bar(T + 240, 150, 150, 150, 150, 2), false],
    ])
    // Rows arrived: no pulse needed.
    expect(s.statuses).toEqual(['connecting', 'live', 'reconnecting', 'live'])
    s.unsubscribe()
  })

  it("if the retry on 'live' fails too, it pulses with the pre-gap bar still last, then moves on", async () => {
    const s = setup()
    await jumpedWhileDown(s)
    await vi.advanceTimersByTimeAsync(1_000)
    callsAt(s, T + 120)[2].result.reject(new Error('down'))
    await flush()
    expect(s.statuses).toEqual([
      'connecting',
      'live',
      'reconnecting',
      'live',
      'reconnecting',
      'live',
    ])
    expect(s.lastAtStatus.slice(4)).toEqual([PRE_GAP, PRE_GAP])
    await vi.advanceTimersByTimeAsync(1_000)
    callsAt(s, T + 180)[0].result.resolve([bar(T + 180, 140, 141, 139, 141, 3)])
    await flush()
    expect(s.candles.slice(2)).toEqual([
      [bar(T + 180, 140, 141, 139, 141, 3), true],
      [bar(T + 240, 150, 150, 150, 150, 2), false],
    ])
    await vi.advanceTimersByTimeAsync(30_000)
    expect(callsAt(s, T + 120)).toHaveLength(3) // retried once on 'live', no more
    s.unsubscribe()
  })

  it('a held run is not retried, and later bars stay held, until the socket is live', async () => {
    const s = setup()
    await failedWhileDown(s)
    const before = s.calls.length
    await vi.advanceTimersByTimeAsync(30_000) // the new socket never opens meanwhile
    expect(s.calls).toHaveLength(before)
    expect(s.candles[s.candles.length - 1]).toEqual(PRE_GAP)
    expect(s.statuses).toEqual(['connecting', 'live', 'reconnecting'])
    s.unsubscribe()
  })

  it('nothing is emitted after unsubscribe, held or mid-retry', async () => {
    const s = setup()
    await jumpedWhileDown(s)
    await vi.advanceTimersByTimeAsync(1_000)
    const candles = s.candles.length
    const statuses = s.statuses.length
    s.unsubscribe()
    callsAt(s, T + 120)[2].result.reject(new Error('down'))
    await vi.advanceTimersByTimeAsync(60_000)
    expect(s.candles).toHaveLength(candles)
    expect(s.statuses).toHaveLength(statuses)
    expect(s.candles[s.candles.length - 1]).toEqual(PRE_GAP)
  })
})

describe('openCoinbaseCandleStream: unseeded bucket with no live trade', () => {
  /** Seed and its retry both fail; heartbeats only (an illiquid pair). */
  async function unseeded(s: ReturnType<typeof setup>) {
    await flush()
    last().serverOpen()
    last().serverMessage(SUBSCRIPTIONS)
    beats(T + 30, T + 31)
    s.calls[0].result.reject(new Error('down'))
    await vi.advanceTimersByTimeAsync(3_000)
    s.calls[1].result.reject(new Error('down'))
    await flush()
    beats(T + 32, T + 60) // the subscription's bucket ends without a trade
    await vi.advanceTimersByTimeAsync(1_000)
  }

  it('is read back from REST on roll and emitted closed', async () => {
    const s = setup()
    await unseeded(s)
    const [read] = callsAt(s, T)
    expect(read).toMatchObject({ limit: 1, endTime: T })
    read.result.resolve([bar(T, 100, 104, 99, 103, 5)])
    await flush()
    expect(s.candles).toEqual([[bar(T, 100, 104, 99, 103, 5), true]])
    last().serverMessage(match(1, T + 61, '120', '2'))
    expect(s.candles[s.candles.length - 1]).toEqual([bar(T + 60, 120, 120, 120, 120, 2), false])
    s.unsubscribe()
  })

  it('emits nothing when REST has no row for it (no forward fill)', async () => {
    const s = setup()
    await unseeded(s)
    callsAt(s, T)[0].result.resolve([bar(T - 60, 1, 1, 1, 1, 1)])
    await vi.advanceTimersByTimeAsync(5_000)
    callsAt(s, T)[1].result.resolve([])
    await vi.advanceTimersByTimeAsync(10_000)
    expect(s.candles).toEqual([])
    // One read-back and one retry, no more.
    expect(callsAt(s, T)).toHaveLength(2)
    s.unsubscribe()
  })

  it('a missing REST row of it is read back once more and emitted if it appears', async () => {
    const s = setup()
    await unseeded(s)
    callsAt(s, T)[0].result.resolve([]) // REST lags (CDN cache): no row yet
    await vi.advanceTimersByTimeAsync(4_999)
    expect(callsAt(s, T)).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    const reads = callsAt(s, T)
    expect(reads).toHaveLength(2)
    expect(reads[1]).toMatchObject({ limit: 1, endTime: T })
    reads[1].result.resolve([bar(T, 100, 104, 99, 103, 5)])
    await flush()
    expect(s.candles).toEqual([[bar(T, 100, 104, 99, 103, 5), true]])
    s.unsubscribe()
  })

  it('a lagging REST row of it is read back again and re-emitted with the fuller view', async () => {
    const s = setup()
    await unseeded(s)
    callsAt(s, T)[0].result.resolve([bar(T, 100, 104, 99, 103, 5)])
    await flush()
    expect(s.candles).toEqual([[bar(T, 100, 104, 99, 103, 5), true]])
    await vi.advanceTimersByTimeAsync(5_000)
    callsAt(s, T)[1].result.resolve([bar(T, 100, 106, 98, 105, 7)])
    await flush()
    // Same time as the closed bar: the consumer replaces it.
    expect(s.candles).toEqual([
      [bar(T, 100, 104, 99, 103, 5), true],
      [bar(T, 100, 106, 98, 105, 7), true],
    ])
    s.unsubscribe()
  })

  it('a second read-back of it that matches the first re-sends the same bar, settled', async () => {
    const s = setup()
    await unseeded(s)
    callsAt(s, T)[0].result.resolve([bar(T, 100, 104, 99, 103, 5)])
    await vi.advanceTimersByTimeAsync(5_000)
    callsAt(s, T)[1].result.resolve([bar(T, 100, 104, 99, 103, 5)])
    await vi.advanceTimersByTimeAsync(10_000)
    expect(s.candles).toEqual([
      [bar(T, 100, 104, 99, 103, 5), true],
      [bar(T, 100, 104, 99, 103, 5), true],
    ])
    expect(s.provisional).toEqual([true, undefined])
    expect(callsAt(s, T)).toHaveLength(2)
    s.unsubscribe()
  })

  it('a failed read-back of it is retried once', async () => {
    const s = setup()
    await unseeded(s)
    callsAt(s, T)[0].result.reject(new Error('down'))
    await vi.advanceTimersByTimeAsync(5_000)
    const reads = callsAt(s, T)
    expect(reads).toHaveLength(2)
    reads[1].result.resolve([bar(T, 100, 104, 99, 103, 5)])
    await flush()
    expect(s.candles).toEqual([[bar(T, 100, 104, 99, 103, 5), true]])
    s.unsubscribe()
  })

  it('a successful seed queues no REST-only read-back for a quiet bucket', async () => {
    const s = setup()
    await openAndSeed(s, [])
    beats(T + 30, T + 60)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(s.calls).toHaveLength(1)
    s.unsubscribe()
  })
})

describe('openCoinbaseTickerStream', () => {
  function setupTickers() {
    const batches: Ticker[][] = []
    const statuses: ConnectionStatus[] = []
    const ids = ['BTC-USD', 'ETH-USD']
    const unsubscribe = openCoinbaseTickerStream({
      WebSocketImpl: WS,
      url: 'wss://feed.test',
      staleTimeoutMs: 60_000,
      resolveProducts: () =>
        Promise.resolve({
          productIds: ids,
          toSymbol: new Map(ids.map((id) => [id, id.replace('-', '')])),
        }),
      onTickers: (t) => batches.push(t),
      onStatus: (s) => statuses.push(s),
      flushMs: 250,
    })
    return { batches, statuses, unsubscribe }
  }
  const tick = (pid: string, price: string, time: string) => ({
    type: 'ticker',
    product_id: pid,
    price,
    open_24h: '100',
    volume_24h: '10',
    low_24h: '90',
    high_24h: '110',
    time,
  })

  it('subscribes ticker_batch for every product on one socket and coalesces updates', async () => {
    const s = setupTickers()
    await flush()
    last().serverOpen()
    expect(JSON.parse(last().sent[0])).toEqual({
      type: 'subscribe',
      product_ids: ['BTC-USD', 'ETH-USD'],
      channels: ['ticker_batch'],
    })
    last().serverMessage({ type: 'subscriptions', channels: [] })
    expect(s.statuses).toEqual(['connecting', 'live'])
    last().serverMessage(tick('BTC-USD', '101', '2026-10-25T14:41:41.1Z'))
    last().serverMessage(tick('ETH-USD', '99', '2026-10-25T14:41:41.2Z'))
    last().serverMessage(tick('BTC-USD', '102', '2026-10-25T14:41:42.3Z'))
    last().serverMessage(tick('DOGE-USD', '1', '2026-10-25T14:41:42.3Z'))
    expect(s.batches).toEqual([])
    await vi.advanceTimersByTimeAsync(250)
    expect(s.batches).toHaveLength(1)
    expect(s.batches[0].map((t) => [t.symbol, t.last, t.time])).toEqual([
      ['BTCUSD', 102, 1_792_939_302],
      ['ETHUSD', 99, 1_792_939_301],
    ])
    expect(s.batches[0][0]).toMatchObject({ open: 100, high: 110, low: 90, volume: 10 })
    expect(s.batches[0][0].changePct).toBeCloseTo(2, 10)
    expect(s.batches[0][0].quoteVolume).toBe(1020)
    s.unsubscribe()
  })

  it('unsubscribe drops pending updates, closes the socket and clears timers', async () => {
    const s = setupTickers()
    await flush()
    last().serverOpen()
    last().serverMessage(tick('BTC-USD', '101', '2026-10-25T14:41:41Z'))
    s.unsubscribe()
    s.unsubscribe()
    expect(last().closeCalls).toBe(1)
    expect(vi.getTimerCount()).toBe(0)
    await vi.advanceTimersByTimeAsync(1_000)
    expect(s.batches).toEqual([])
  })
})

describe('openCoinbaseTickerStream subscribe errors', () => {
  function setupTickers(productIds: string[], chunkSize: number) {
    const statuses: ConnectionStatus[] = []
    const unsubscribe = openCoinbaseTickerStream({
      WebSocketImpl: WS,
      url: 'wss://feed.test',
      staleTimeoutMs: 60_000,
      resolveProducts: () =>
        Promise.resolve({
          productIds,
          toSymbol: new Map(productIds.map((id) => [id, id.replace('-', '')])),
        }),
      onTickers: () => {},
      onStatus: (s) => statuses.push(s),
      chunkSize,
    })
    return { statuses, unsubscribe }
  }
  const sentIds = (ws: FakeWebSocket) =>
    ws.sent.map((m) => (JSON.parse(m) as { product_ids: string[] }).product_ids)
  const confirmed = { type: 'subscriptions', channels: [] }

  it('subscribes in chunks; an invalid id is dropped and the rest of its chunk resubscribed', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const s = setupTickers(['A-USD', 'BAD-USD', 'C-USD', 'D-USD', 'E-USD'], 2)
    await flush()
    last().serverOpen()
    expect(sentIds(last())).toEqual([['A-USD', 'BAD-USD'], ['C-USD', 'D-USD'], ['E-USD']])
    last().serverMessage(confirmed)
    last().serverMessage(subscribeError('BAD-USD is not a valid product'))
    expect(sentIds(last())[3]).toEqual(['A-USD'])
    last().serverMessage(confirmed)
    last().serverMessage(confirmed)
    expect(s.statuses).toEqual(['connecting', 'live'])
    expect(last().closeCalls).toBe(0)
    // The bad id stays dropped on every later socket.
    last().serverClose()
    await vi.advanceTimersByTimeAsync(1_000)
    last().serverOpen()
    expect(sentIds(last())).toEqual([
      ['A-USD', 'C-USD'],
      ['D-USD', 'E-USD'],
    ])
    s.unsubscribe()
  })

  it('a chunk left empty by a bad id is not resent', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const s = setupTickers(['A-USD', 'BAD-USD'], 1)
    await flush()
    last().serverOpen()
    last().serverMessage(subscribeError('BAD-USD is not a valid product'))
    last().serverMessage(confirmed)
    expect(last().sent).toHaveLength(2)
    expect(s.statuses).toEqual(['connecting', 'live'])
    s.unsubscribe()
  })

  it('every id invalid: offline and stop', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const s = setupTickers(['BAD-USD'], 100)
    await flush()
    last().serverOpen()
    last().serverMessage(subscribeError('BAD-USD is not a valid product'))
    expect(s.statuses).toEqual(['connecting', 'offline'])
    expect(vi.getTimerCount()).toBe(0)
  })

  it('an unexplained rejection fails the socket; 3 in a row go offline', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const s = setupTickers(['A-USD', 'B-USD'], 100)
    await flush()
    for (let i = 0; i < 3; i++) {
      last().serverOpen()
      last().serverMessage(subscribeError('Internal error'))
      await vi.advanceTimersByTimeAsync(30_000)
    }
    expect(FakeWebSocket.instances).toHaveLength(3)
    expect(s.statuses).toEqual(['connecting', 'reconnecting', 'offline'])
    expect(vi.getTimerCount()).toBe(0)
  })

  it('an error after every chunk is confirmed is only logged', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const s = setupTickers(['A-USD'], 100)
    await flush()
    last().serverOpen()
    last().serverMessage(confirmed)
    last().serverMessage(subscribeError('Internal error'))
    expect(warn).toHaveBeenCalled()
    expect(last().closeCalls).toBe(0)
    expect(s.statuses).toEqual(['connecting', 'live'])
    s.unsubscribe()
  })
})

describe('CoinbaseProvider streams', () => {
  const products = [
    {
      id: 'BTC-USD',
      base_currency: 'BTC',
      quote_currency: 'USD',
      quote_increment: '0.01',
      status: 'online',
      trading_disabled: false,
      cancel_only: false,
    },
    {
      id: 'OLD-USD',
      base_currency: 'OLD',
      quote_currency: 'USD',
      quote_increment: '0.01',
      status: 'delisted',
      trading_disabled: true,
    },
  ]
  const fetchFn = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    const body = url.endsWith('/products') ? products : []
    return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))
  })

  it('subscribeCandles maps BTCUSD to the BTC-USD product on the configured host', async () => {
    const p = new CoinbaseProvider({
      fetchFn: fetchFn as unknown as typeof fetch,
      WebSocketImpl: WS,
      wsBaseUrl: 'wss://ws.test/',
      minRequestIntervalMs: 0,
    })
    const un = p.subscribeCandles('btcusd', '4h', () => {})
    await flush()
    expect(last().url).toBe('wss://ws.test')
    last().serverOpen()
    expect(JSON.parse(last().sent[0]).product_ids).toEqual(['BTC-USD'])
    un()
  })

  it('subscribeTickers subscribes only tradable products', async () => {
    const p = new CoinbaseProvider({
      fetchFn: fetchFn as unknown as typeof fetch,
      WebSocketImpl: WS,
      minRequestIntervalMs: 0,
    })
    const un = p.subscribeTickers(() => {})
    await flush()
    expect(last().url).toBe('wss://ws-feed.exchange.coinbase.com')
    last().serverOpen()
    expect(JSON.parse(last().sent[0]).product_ids).toEqual(['BTC-USD'])
    un()
  })

  it('the live seed never steps back past an empty window (one candle request)', async () => {
    fetchFn.mockClear()
    const p = new CoinbaseProvider({
      fetchFn: fetchFn as unknown as typeof fetch,
      WebSocketImpl: WS,
      minRequestIntervalMs: 0,
      now: () => (T + 30) * 1000,
    })
    const un = p.subscribeCandles('BTCUSD', '1m', () => {})
    await flush()
    last().serverOpen()
    last().serverMessage(SUBSCRIPTIONS)
    await vi.advanceTimersByTimeAsync(100)
    const candleCalls = fetchFn.mock.calls.filter(([u]) => String(u).includes('/candles'))
    expect(candleCalls).toHaveLength(1)
    un()
  })

  it('subscribeCandles on an unknown symbol reports offline', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const p = new CoinbaseProvider({
      fetchFn: fetchFn as unknown as typeof fetch,
      WebSocketImpl: WS,
      minRequestIntervalMs: 0,
    })
    const statuses: ConnectionStatus[] = []
    p.subscribeCandles('NOPEUSD', '1m', () => {}, { onStatus: (s) => statuses.push(s) })
    await flush()
    expect(statuses).toEqual(['connecting', 'offline'])
    expect(FakeWebSocket.instances).toHaveLength(0)
  })
})
