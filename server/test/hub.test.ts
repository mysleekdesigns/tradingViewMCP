import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  backoffDelay,
  createHub,
  DEFAULT_RELEASE_GRACE_MS,
  klineKey,
  parseKey,
  TICKERS_KEY,
  upstreamUrl,
  type Hub,
  type HubEvent,
} from '../src/upstream/hub.ts'

/** Fake upstream WebSocket: records instances; the test drives open/message/close. */
class FakeWS {
  static instances: FakeWS[] = []
  readonly url: string
  readyState = 0
  closed = false
  onopen: ((ev: unknown) => void) | null = null
  onmessage: ((ev: { data: unknown }) => void) | null = null
  onerror: ((ev: unknown) => void) | null = null
  onclose: ((ev: unknown) => void) | null = null
  constructor(url: string) {
    this.url = url
    FakeWS.instances.push(this)
  }
  close(): void {
    this.closed = true
    this.readyState = 3
  }
  emitOpen(): void {
    this.readyState = 1
    this.onopen?.({})
  }
  emitMessage(data: unknown): void {
    this.onmessage?.({ data: typeof data === 'string' ? data : JSON.stringify(data) })
  }
  /** Simulate a remote drop. */
  emitClose(): void {
    this.readyState = 3
    this.onclose?.({})
  }
  emitError(): void {
    this.onerror?.({})
  }
}

const klineMsg = (symbol: string, interval: string, t = 1_700_000_100_000, x = false) => ({
  e: 'kline',
  E: t + 1,
  s: symbol,
  k: { t, s: symbol, i: interval, o: '1', h: '2', l: '0.5', c: '1.5', v: '10', x },
})

const tickerMsg = [
  {
    e: '24hrMiniTicker',
    E: 1_700_000_000_000,
    s: 'BTCUSDT',
    c: '2',
    o: '1',
    h: '2',
    l: '1',
    v: '1',
    q: '1',
  },
]

describe('hub', () => {
  let stats: { upstreamSockets: number }
  let hub: Hub
  const BTC = klineKey('BTCUSDT', '1m')

  beforeEach(() => {
    vi.useFakeTimers()
    FakeWS.instances = []
    stats = { upstreamSockets: 0 }
    hub = createHub({
      WebSocketImpl: FakeWS as unknown as typeof WebSocket,
      wsBase: 'wss://stream.example:9443',
      stats,
      random: () => 0.5,
    })
  })

  afterEach(() => {
    hub.close()
    vi.useRealTimers()
  })

  it('builds Binance.US stream URLs', () => {
    hub.acquire(BTC, () => {})
    hub.acquire(TICKERS_KEY, () => {})
    expect(FakeWS.instances.map((w) => w.url)).toEqual([
      'wss://stream.example:9443/ws/btcusdt@kline_1m',
      'wss://stream.example:9443/ws/!miniTicker@arr',
    ])
    expect(upstreamUrl('wss://x/', { kind: 'tickers' })).toBe('wss://x/ws/!miniTicker@arr')
  })

  it('two listeners on the same key share one upstream socket and both get messages', () => {
    const a: HubEvent[] = []
    const b: HubEvent[] = []
    hub.acquire(BTC, (e) => a.push(e))
    hub.acquire(BTC, (e) => b.push(e))
    expect(FakeWS.instances).toHaveLength(1)
    expect(stats.upstreamSockets).toBe(1)
    const ws = FakeWS.instances[0]!
    ws.emitOpen()
    ws.emitMessage(klineMsg('BTCUSDT', '1m'))
    const kl = (es: HubEvent[]) => es.filter((e) => e.type === 'kline')
    expect(kl(a)).toHaveLength(1)
    expect(kl(b)).toHaveLength(1)
    expect(kl(a)[0]).toEqual({
      type: 'kline',
      candle: { time: 1_700_000_100, open: 1, high: 2, low: 0.5, close: 1.5, volume: 10 },
      closed: false,
    })
  })

  it('filters klines for another symbol/interval and ignores malformed messages', () => {
    const a: HubEvent[] = []
    hub.acquire(BTC, (e) => a.push(e))
    const ws = FakeWS.instances[0]!
    ws.emitMessage(klineMsg('ETHUSDT', '1m'))
    ws.emitMessage(klineMsg('BTCUSDT', '5m'))
    ws.emitMessage('garbage')
    expect(a.filter((e) => e.type === 'kline')).toHaveLength(0)
  })

  it('closes the upstream after the grace period once the last listener releases; release is idempotent', () => {
    const r1 = hub.acquire(BTC, () => {})
    const r2 = hub.acquire(BTC, () => {})
    const ws = FakeWS.instances[0]!
    r1()
    r1()
    expect(ws.closed).toBe(false)
    expect(stats.upstreamSockets).toBe(1)
    r2()
    // Kept for the grace period so a quick re-acquire can reuse it.
    expect(ws.closed).toBe(false)
    expect(stats.upstreamSockets).toBe(1)
    expect(hub.size).toBe(1)
    r2() // idempotent: does not restart the grace timer
    vi.advanceTimersByTime(DEFAULT_RELEASE_GRACE_MS - 1)
    expect(ws.closed).toBe(false)
    vi.advanceTimersByTime(1)
    expect(ws.closed).toBe(true)
    expect(vi.getTimerCount()).toBe(0)
    expect(stats.upstreamSockets).toBe(0)
    expect(hub.size).toBe(0)
    r2()
    expect(stats.upstreamSockets).toBe(0)
    // Re-acquire opens a fresh socket.
    hub.acquire(BTC, () => {})
    expect(FakeWS.instances).toHaveLength(2)
    expect(FakeWS.instances[1]!.closed).toBe(false)
    expect(stats.upstreamSockets).toBe(1)
  })

  it('a re-acquire within the grace period reuses the socket and its backoff state', () => {
    const r1 = hub.acquire(BTC, () => {})
    FakeWS.instances[0]!.emitClose() // failure 1 -> attempt 0 (750 ms)
    vi.advanceTimersByTime(750)
    expect(FakeWS.instances).toHaveLength(2)
    FakeWS.instances[1]!.emitClose() // failure 2 -> attempt 1 (1500 ms)
    r1()
    vi.advanceTimersByTime(1_000)
    const statuses: string[] = []
    hub.acquire(BTC, (e) => {
      if (e.type === 'status') statuses.push(e.status)
    })
    // No immediate fresh socket: the pending backoff still governs the reconnect.
    expect(FakeWS.instances).toHaveLength(2)
    expect(statuses).toEqual(['reconnecting'])
    vi.advanceTimersByTime(499)
    expect(FakeWS.instances).toHaveLength(2)
    vi.advanceTimersByTime(1)
    expect(FakeWS.instances).toHaveLength(3)
    // The grace timer was cancelled: the entry outlives the original grace deadline.
    vi.advanceTimersByTime(DEFAULT_RELEASE_GRACE_MS)
    expect(FakeWS.instances[2]!.closed).toBe(false)
    expect(hub.size).toBe(1)
  })

  it('a live socket is reused across a quick release/re-acquire', () => {
    const r1 = hub.acquire(BTC, () => {})
    const ws = FakeWS.instances[0]!
    ws.emitOpen()
    ws.emitMessage(klineMsg('BTCUSDT', '1m'))
    r1()
    vi.advanceTimersByTime(DEFAULT_RELEASE_GRACE_MS - 1)
    const got: HubEvent[] = []
    const r2 = hub.acquire(BTC, (e) => got.push(e))
    expect(got).toEqual([{ type: 'status', status: 'live' }])
    expect(FakeWS.instances).toHaveLength(1)
    ws.emitMessage(klineMsg('BTCUSDT', '1m'))
    expect(got.filter((e) => e.type === 'kline')).toHaveLength(1)
    r2()
    vi.advanceTimersByTime(DEFAULT_RELEASE_GRACE_MS)
    expect(ws.closed).toBe(true)
    expect(stats.upstreamSockets).toBe(0)
    expect(hub.size).toBe(0)
  })

  it('close() during the grace period cleans up immediately with no timer left', () => {
    const r = hub.acquire(BTC, () => {})
    r()
    expect(vi.getTimerCount()).toBeGreaterThan(0)
    hub.close()
    expect(FakeWS.instances[0]!.closed).toBe(true)
    expect(stats.upstreamSockets).toBe(0)
    expect(hub.size).toBe(0)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('releaseGraceMs: 0 tears down at once', () => {
    const h = createHub({
      WebSocketImpl: FakeWS as unknown as typeof WebSocket,
      wsBase: 'wss://x',
      stats,
      releaseGraceMs: 0,
    })
    const r = h.acquire(BTC, () => {})
    r()
    expect(FakeWS.instances[0]!.closed).toBe(true)
    expect(h.size).toBe(0)
    h.close()
  })

  it('the same listener function attached twice is released independently', () => {
    const fn = vi.fn()
    const r1 = hub.acquire(BTC, fn)
    hub.acquire(BTC, fn)
    r1()
    expect(FakeWS.instances[0]!.closed).toBe(false)
  })

  it('different keys get different sockets', () => {
    hub.acquire(BTC, () => {})
    hub.acquire(klineKey('BTCUSDT', '1h'), () => {})
    hub.acquire(klineKey('ETHUSDT', '1m'), () => {})
    hub.acquire(TICKERS_KEY, () => {})
    hub.acquire(TICKERS_KEY, () => {})
    expect(FakeWS.instances).toHaveLength(4)
    expect(stats.upstreamSockets).toBe(4)
  })

  it('sends the current status on attach', () => {
    const a: HubEvent[] = []
    hub.acquire(BTC, (e) => a.push(e))
    expect(a).toEqual([{ type: 'status', status: 'reconnecting' }])
    FakeWS.instances[0]!.emitOpen()
    FakeWS.instances[0]!.emitMessage(klineMsg('BTCUSDT', '1m'))
    expect(hub.status(BTC)).toBe('live')
    const b: HubEvent[] = []
    hub.acquire(BTC, (e) => b.push(e))
    expect(b).toEqual([{ type: 'status', status: 'live' }])
  })

  it('reconnects with backoff after a drop: reconnecting -> live', () => {
    const statuses: string[] = []
    hub.acquire(BTC, (e) => {
      if (e.type === 'status') statuses.push(e.status)
    })
    const ws1 = FakeWS.instances[0]!
    ws1.emitOpen()
    ws1.emitMessage(klineMsg('BTCUSDT', '1m'))
    ws1.emitClose()
    ws1.emitError() // late duplicate events are ignored
    expect(statuses).toEqual(['reconnecting', 'live', 'reconnecting'])
    expect(stats.upstreamSockets).toBe(0)
    // attempt 0 with random 0.5: 750 ms
    vi.advanceTimersByTime(749)
    expect(FakeWS.instances).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(FakeWS.instances).toHaveLength(2)
    expect(stats.upstreamSockets).toBe(1)
    const ws2 = FakeWS.instances[1]!
    ws2.emitOpen()
    expect(statuses.at(-1)).toBe('reconnecting') // live only after the first message
    ws2.emitMessage(klineMsg('BTCUSDT', '1m'))
    expect(statuses).toEqual(['reconnecting', 'live', 'reconnecting', 'live'])
  })

  it('backs off exponentially on consecutive failures, capped at 30 s', () => {
    hub.acquire(BTC, () => {})
    FakeWS.instances[0]!.emitClose() // failure 1 -> attempt 0 (750 ms)
    vi.advanceTimersByTime(750)
    FakeWS.instances[1]!.emitClose() // failure 2 -> attempt 1 (1500 ms)
    vi.advanceTimersByTime(1499)
    expect(FakeWS.instances).toHaveLength(2)
    vi.advanceTimersByTime(1)
    expect(FakeWS.instances).toHaveLength(3)
    expect(backoffDelay(0, () => 0)).toBe(500)
    expect(backoffDelay(0, () => 0.999999)).toBe(1000)
    expect(backoffDelay(20, () => 0.999999)).toBe(30_000)
    expect(backoffDelay(20, () => 0)).toBe(15_000)
  })

  it('reconnects when no upstream message arrives for 60 s', () => {
    const statuses: string[] = []
    hub.acquire(TICKERS_KEY, (e) => {
      if (e.type === 'status') statuses.push(e.status)
    })
    const ws1 = FakeWS.instances[0]!
    ws1.emitOpen()
    ws1.emitMessage(tickerMsg)
    vi.advanceTimersByTime(59_000)
    ws1.emitMessage(tickerMsg) // re-arms the stale timer
    vi.advanceTimersByTime(59_999)
    expect(ws1.closed).toBe(false)
    vi.advanceTimersByTime(1)
    expect(ws1.closed).toBe(true)
    expect(statuses).toEqual(['reconnecting', 'live', 'reconnecting'])
    vi.advanceTimersByTime(1_000)
    expect(FakeWS.instances).toHaveLength(2)
  })

  it('a socket that never opens is treated as stale too', () => {
    hub.acquire(BTC, () => {})
    vi.advanceTimersByTime(60_000)
    expect(FakeWS.instances[0]!.closed).toBe(true)
  })

  it('fans out tickers and isolates a throwing listener', () => {
    const good: HubEvent[] = []
    hub.acquire(TICKERS_KEY, (e) => {
      if (e.type !== 'status') throw new Error('boom')
    })
    hub.acquire(TICKERS_KEY, (e) => good.push(e))
    FakeWS.instances[0]!.emitMessage(tickerMsg)
    expect(good.filter((e) => e.type === 'tickers')).toEqual([
      {
        type: 'tickers',
        tickers: [
          {
            symbol: 'BTCUSDT',
            last: 2,
            open: 1,
            high: 2,
            low: 1,
            volume: 1,
            quoteVolume: 1,
            changePct: 100,
            time: 1_700_000_000,
          },
        ],
      },
    ])
  })

  it('a listener throwing on the attach status does not break acquire', () => {
    expect(() =>
      hub.acquire(BTC, () => {
        throw new Error('boom')
      }),
    ).not.toThrow()
    expect(FakeWS.instances).toHaveLength(1)
  })

  it('close() shuts every upstream socket and clears timers', () => {
    hub.acquire(BTC, () => {})
    hub.acquire(TICKERS_KEY, () => {})
    FakeWS.instances[0]!.emitClose() // pending reconnect timer
    hub.close()
    expect(FakeWS.instances.every((w) => w.closed)).toBe(true)
    expect(stats.upstreamSockets).toBe(0)
    expect(hub.size).toBe(0)
    expect(vi.getTimerCount()).toBe(0)
    vi.advanceTimersByTime(120_000)
    expect(FakeWS.instances).toHaveLength(2)
    // Acquire after close is a no-op.
    const ev: HubEvent[] = []
    hub.acquire(BTC, (e) => ev.push(e))
    expect(FakeWS.instances).toHaveLength(2)
    expect(ev).toEqual([{ type: 'status', status: 'reconnecting' }])
  })

  it('a throwing WebSocket constructor schedules a retry', () => {
    let fail = true
    class Flaky extends FakeWS {
      constructor(url: string) {
        if (fail) throw new Error('bad url')
        super(url)
      }
    }
    const h = createHub({
      WebSocketImpl: Flaky as unknown as typeof WebSocket,
      wsBase: 'wss://x',
      stats,
      random: () => 0,
    })
    h.acquire(BTC, () => {})
    expect(FakeWS.instances).toHaveLength(0)
    fail = false
    vi.advanceTimersByTime(500)
    expect(FakeWS.instances).toHaveLength(1)
    h.close()
  })

  it('parseKey validates keys', () => {
    expect(parseKey('tickers')).toEqual({ kind: 'tickers' })
    expect(parseKey('kline:BTCUSDT:4h')).toEqual({
      kind: 'kline',
      symbol: 'BTCUSDT',
      interval: '4h',
    })
    expect(parseKey('kline:BTCUSDT:2h')).toBeNull()
    expect(parseKey('kline:btc:1m')).toBeNull()
    expect(() => hub.acquire('nope', () => {})).toThrow()
  })
})
