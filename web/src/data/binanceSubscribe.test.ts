import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Candle, ConnectionStatus } from '../types'
import { BinanceProvider } from './binance'

/** Minimal stand-in for the browser WebSocket; tests drive its events. */
class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSING = 2
  static readonly CLOSED = 3

  readonly url: string
  readyState = 0
  closeCalls = 0
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null

  constructor(url: string) {
    this.url = url
    FakeWebSocket.instances.push(this)
  }
  close(): void {
    this.closeCalls += 1
    this.readyState = 3
  }
  // Test drivers: fire through whatever handler is currently attached.
  serverOpen(): void {
    this.readyState = 1
    this.onopen?.(new Event('open'))
  }
  serverMessage(data: unknown): void {
    this.onmessage?.({
      data: typeof data === 'string' ? data : JSON.stringify(data),
    } as MessageEvent)
  }
  serverError(): void {
    this.onerror?.(new Event('error'))
  }
  serverClose(): void {
    this.readyState = 3
    this.onclose?.({ code: 1006 } as CloseEvent)
  }
}

const WS = FakeWebSocket as unknown as typeof WebSocket
const last = () => FakeWebSocket.instances[FakeWebSocket.instances.length - 1]

function kline(t: number, close: string, x = false, s = 'BTCUSDT', i = '1m') {
  return {
    e: 'kline',
    E: t + 1,
    s,
    k: { t, T: t + 59_999, s, i, o: '100', h: '110', l: '90', c: close, v: '5', x },
  }
}

function setup(staleTimeoutMs = 60_000) {
  const provider = new BinanceProvider({
    wsBaseUrl: 'wss://example.test:9443/',
    WebSocketImpl: WS,
    staleTimeoutMs,
  })
  const candles: [Candle, boolean][] = []
  const statuses: ConnectionStatus[] = []
  const unsubscribe = provider.subscribeCandles(
    'BTCUSDT',
    '1m',
    (c, closed) => candles.push([c, closed]),
    { onStatus: (s) => statuses.push(s) },
  )
  return { candles, statuses, unsubscribe }
}

let win: EventTarget
let nav: { onLine: boolean }

beforeEach(() => {
  FakeWebSocket.instances = []
  vi.useFakeTimers()
  vi.spyOn(Math, 'random').mockReturnValue(1) // top of jitter range: delay == base
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

describe('BinanceProvider.subscribeCandles', () => {
  it('opens one socket on the lowercase kline stream URL', () => {
    setup()
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(last().url).toBe('wss://example.test:9443/ws/btcusdt@kline_1m')
  })

  it('defaults to the Binance.US stream host', () => {
    const p = new BinanceProvider({ WebSocketImpl: WS })
    const un = p.subscribeCandles('ethusdt', '4h', () => {})
    expect(last().url).toBe('wss://stream.binance.us:9443/ws/ethusdt@kline_4h')
    un()
  })

  it('delivers parsed candles with the closed flag and ignores junk', () => {
    const { candles, statuses } = setup()
    expect(statuses).toEqual(['connecting'])
    last().serverOpen()
    expect(statuses).toEqual(['connecting', 'live'])
    last().serverMessage(kline(1_700_000_040_000, '101.5'))
    last().serverMessage('not json')
    last().serverMessage({ e: 'trade' })
    last().serverMessage(kline(1_700_000_040_000, '102', false, 'ETHUSDT'))
    last().serverMessage(kline(1_700_000_040_000, '102', false, 'BTCUSDT', '5m'))
    last().serverMessage(kline(1_700_000_040_000, '103', true))
    expect(candles).toEqual([
      [{ time: 1_700_000_040, open: 100, high: 110, low: 90, close: 101.5, volume: 5 }, false],
      [{ time: 1_700_000_040, open: 100, high: 110, low: 90, close: 103, volume: 5 }, true],
    ])
  })

  it('does not throw out of onmessage when onCandle throws', () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const p = new BinanceProvider({ WebSocketImpl: WS })
    p.subscribeCandles('BTCUSDT', '1m', () => {
      throw new Error('boom')
    })
    last().serverOpen()
    expect(() => last().serverMessage(kline(0, '1'))).not.toThrow()
    expect(errSpy).toHaveBeenCalled()
  })

  it('reconnects with backoff: connecting -> live -> reconnecting -> live', () => {
    const { statuses } = setup()
    const first = last()
    first.serverOpen()
    first.serverMessage(kline(0, '1'))
    first.serverClose()
    expect(statuses).toEqual(['connecting', 'live', 'reconnecting'])
    expect(first.onmessage).toBeNull() // handlers detached

    vi.advanceTimersByTime(999)
    expect(FakeWebSocket.instances).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(FakeWebSocket.instances).toHaveLength(2)
    last().serverOpen()
    expect(statuses).toEqual(['connecting', 'live', 'reconnecting', 'live'])
  })

  it('error followed by close counts as one drop and schedules one reconnect', () => {
    const { statuses } = setup()
    const s = last()
    s.serverOpen()
    s.serverError()
    s.serverClose()
    expect(s.closeCalls).toBe(1)
    expect(statuses.filter((x) => x === 'reconnecting')).toHaveLength(1)
    vi.advanceTimersByTime(1000)
    expect(FakeWebSocket.instances).toHaveLength(2)
    vi.advanceTimersByTime(60_000)
    expect(FakeWebSocket.instances).toHaveLength(2)
  })

  it('doubles the delay on consecutive failures and resets after a message', () => {
    setup()
    const expectReconnectAfter = (ms: number) => {
      const n = FakeWebSocket.instances.length
      vi.advanceTimersByTime(ms - 1)
      expect(FakeWebSocket.instances).toHaveLength(n)
      vi.advanceTimersByTime(1)
      expect(FakeWebSocket.instances).toHaveLength(n + 1)
    }
    last().serverClose() // failure 1
    expectReconnectAfter(1000)
    last().serverClose() // failure 2
    expectReconnectAfter(2000)
    last().serverOpen() // open without a message does not reset
    last().serverClose() // failure 3
    expectReconnectAfter(4000)
    last().serverOpen()
    last().serverMessage(kline(0, '1')) // resets
    last().serverClose()
    expectReconnectAfter(1000)
  })

  it('reports offline after 5 consecutive failures but keeps retrying at max backoff', () => {
    const { statuses } = setup()
    for (let i = 0; i < 4; i++) {
      last().serverClose()
      vi.advanceTimersByTime(30_000)
    }
    expect(statuses).toEqual(['connecting', 'reconnecting'])
    last().serverClose() // 5th failure
    expect(statuses).toEqual(['connecting', 'reconnecting', 'offline'])
    vi.advanceTimersByTime(16_000) // attempt index 4 -> 16 s
    const n = FakeWebSocket.instances.length
    last().serverClose() // 6th
    vi.advanceTimersByTime(29_999)
    expect(FakeWebSocket.instances).toHaveLength(n)
    vi.advanceTimersByTime(1)
    expect(FakeWebSocket.instances).toHaveLength(n + 1)
    last().serverOpen()
    expect(statuses[statuses.length - 1]).toBe('live')
  })

  it('reconnects when the stream goes silent past the stale timeout', () => {
    const { statuses } = setup(5_000)
    const first = last()
    first.serverOpen()
    vi.advanceTimersByTime(4_000)
    first.serverMessage(kline(0, '1')) // re-arms
    vi.advanceTimersByTime(4_999)
    expect(first.closeCalls).toBe(0)
    vi.advanceTimersByTime(1)
    expect(first.closeCalls).toBe(1)
    // The socket had opened and was healthy: an idle stale reconnect is silent.
    expect(statuses).toEqual(['connecting', 'live'])
    vi.advanceTimersByTime(1000)
    expect(FakeWebSocket.instances).toHaveLength(2)
  })

  it('stale timeouts on an opened but silent socket never drive status to offline', () => {
    const { statuses } = setup(5_000)
    for (let i = 0; i < 10; i++) {
      const n = FakeWebSocket.instances.length
      last().serverOpen() // healthy socket, but the pair is illiquid: no messages
      vi.advanceTimersByTime(5_000) // stale timeout
      expect(last().closeCalls).toBe(1)
      // Reconnect at attempt 0 (1 s), not a growing backoff.
      vi.advanceTimersByTime(999)
      expect(FakeWebSocket.instances).toHaveLength(n)
      vi.advanceTimersByTime(1)
      expect(FakeWebSocket.instances).toHaveLength(n + 1)
    }
    expect(statuses).not.toContain('offline')
    // Idle stale reconnects are silent: status never leaves 'live'.
    expect(statuses).toEqual(['connecting', 'live'])
  })

  it('an idle stale reconnect does not add to the failure count', () => {
    const { statuses } = setup()
    for (let i = 0; i < 4; i++) {
      last().serverClose() // 4 real failures
      vi.advanceTimersByTime(30_000)
    }
    last().serverOpen()
    vi.advanceTimersByTime(60_000) // idle stale: not a 5th failure
    expect(statuses).not.toContain('offline')
    vi.advanceTimersByTime(1_000)
    last().serverClose() // 5th real failure
    expect(statuses[statuses.length - 1]).toBe('offline')
  })

  it('an idle stale reconnect whose new socket opens stays live without a duplicate status', () => {
    const { statuses, candles } = setup(5_000)
    last().serverOpen()
    vi.advanceTimersByTime(5_000) // idle stale
    vi.advanceTimersByTime(1_000) // silent reconnect at attempt 0
    expect(FakeWebSocket.instances).toHaveLength(2)
    last().serverOpen()
    last().serverMessage(kline(0, '7'))
    expect(statuses).toEqual(['connecting', 'live'])
    expect(candles.map(([c]) => c.close)).toEqual([7])
  })

  it('falls back to reconnecting when the silent reconnect socket closes before opening', () => {
    const { statuses } = setup(5_000)
    last().serverOpen()
    vi.advanceTimersByTime(6_000) // idle stale + silent reconnect
    expect(FakeWebSocket.instances).toHaveLength(2)
    last().serverClose()
    expect(statuses).toEqual(['connecting', 'live', 'reconnecting'])
    vi.advanceTimersByTime(1_000)
    expect(FakeWebSocket.instances).toHaveLength(3)
    last().serverOpen()
    expect(statuses).toEqual(['connecting', 'live', 'reconnecting', 'live'])
  })

  it('falls back to reconnecting when the silent reconnect socket never opens', () => {
    const { statuses } = setup(5_000)
    last().serverOpen()
    vi.advanceTimersByTime(6_000) // idle stale + silent reconnect
    expect(statuses).toEqual(['connecting', 'live'])
    vi.advanceTimersByTime(5_000) // the new connect hangs past the stale timeout
    expect(statuses).toEqual(['connecting', 'live', 'reconnecting'])
  })

  it('reports offline when the browser is offline at an idle stale timeout', () => {
    const { statuses } = setup(5_000)
    last().serverOpen()
    nav.onLine = false
    vi.advanceTimersByTime(5_000)
    expect(statuses).toEqual(['connecting', 'live', 'offline'])
  })

  it('times out a connect that never opens', () => {
    setup(5_000)
    vi.advanceTimersByTime(5_000)
    expect(FakeWebSocket.instances[0].closeCalls).toBe(1)
    vi.advanceTimersByTime(1000)
    expect(FakeWebSocket.instances).toHaveLength(2)
  })

  it('goes offline on window offline and reconnects immediately on online', () => {
    const { statuses } = setup()
    last().serverOpen()
    nav.onLine = false
    win.dispatchEvent(new Event('offline'))
    expect(statuses).toEqual(['connecting', 'live', 'offline'])
    expect(FakeWebSocket.instances[0].closeCalls).toBe(1)
    vi.advanceTimersByTime(120_000)
    expect(FakeWebSocket.instances).toHaveLength(1)

    nav.onLine = true
    win.dispatchEvent(new Event('online'))
    expect(FakeWebSocket.instances).toHaveLength(2)
    expect(statuses).toEqual(['connecting', 'live', 'offline', 'reconnecting'])
    last().serverOpen()
    expect(statuses[statuses.length - 1]).toBe('live')
  })

  it("ignores an 'online' event while the current socket is OPEN", () => {
    const { statuses } = setup()
    const sock = last()
    sock.serverOpen()
    win.dispatchEvent(new Event('online'))
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(sock.closeCalls).toBe(0)
    expect(statuses).toEqual(['connecting', 'live'])
    sock.serverMessage(kline(0, '1'))
    expect(sock.onmessage).not.toBeNull()
  })

  it("an 'online' event while still CONNECTING replaces the socket", () => {
    setup()
    win.dispatchEvent(new Event('online'))
    expect(FakeWebSocket.instances).toHaveLength(2)
    expect(FakeWebSocket.instances[0].closeCalls).toBe(1)
  })

  it('starts offline without a socket when navigator is offline', () => {
    nav.onLine = false
    const { statuses } = setup()
    expect(statuses).toEqual(['offline'])
    expect(FakeWebSocket.instances).toHaveLength(0)
    nav.onLine = true
    win.dispatchEvent(new Event('online'))
    expect(FakeWebSocket.instances).toHaveLength(1)
  })

  it('unsubscribe closes the socket and suppresses all further callbacks', () => {
    const { candles, statuses, unsubscribe } = setup()
    const s = last()
    s.serverOpen()
    unsubscribe()
    expect(s.closeCalls).toBe(1)
    s.serverMessage(kline(0, '1'))
    s.serverClose()
    win.dispatchEvent(new Event('offline'))
    win.dispatchEvent(new Event('online'))
    vi.advanceTimersByTime(600_000)
    expect(candles).toEqual([])
    expect(statuses).toEqual(['connecting', 'live'])
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(vi.getTimerCount()).toBe(0)
    unsubscribe() // idempotent
    expect(s.closeCalls).toBe(1)
  })

  it('unsubscribe cancels a pending reconnect', () => {
    const { statuses, unsubscribe } = setup()
    last().serverClose()
    expect(vi.getTimerCount()).toBe(1)
    unsubscribe()
    expect(vi.getTimerCount()).toBe(0)
    vi.advanceTimersByTime(600_000)
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(statuses).toEqual(['connecting', 'reconnecting'])
  })

  it('unsubscribe works while CONNECTING', () => {
    const { statuses, unsubscribe } = setup()
    const s = last()
    expect(s.readyState).toBe(0)
    unsubscribe()
    expect(s.closeCalls).toBe(1)
    s.serverOpen()
    vi.advanceTimersByTime(600_000)
    expect(statuses).toEqual(['connecting'])
    expect(FakeWebSocket.instances).toHaveLength(1)
  })

  it('removes window listeners on unsubscribe', () => {
    const add = vi.spyOn(win, 'addEventListener')
    const remove = vi.spyOn(win, 'removeEventListener')
    const { unsubscribe } = setup()
    expect(add.mock.calls.map((c) => c[0]).sort()).toEqual(['offline', 'online'])
    unsubscribe()
    expect(remove.mock.calls).toEqual(add.mock.calls)
  })

  it('works without window/navigator (non-browser env)', () => {
    vi.stubGlobal('window', undefined)
    vi.stubGlobal('navigator', undefined)
    const { statuses, unsubscribe } = setup()
    last().serverOpen()
    expect(statuses).toEqual(['connecting', 'live'])
    unsubscribe()
    expect(last().closeCalls).toBe(1)
  })
})
