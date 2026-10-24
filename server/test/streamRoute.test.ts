import type { FastifyInstance } from 'fastify'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { buildApp } from '../src/app.ts'
import { openDb, type Db } from '../src/db/index.ts'
import {
  isAllowedOrigin,
  MAX_BUFFERED_BYTES,
  MAX_UPSTREAM_KEYS,
  PING_INTERVAL_MS,
} from '../src/routes/stream.ts'
import { DEFAULT_RELEASE_GRACE_MS } from '../src/upstream/hub.ts'

/** Fake upstream WebSocket (the Binance side): records instances, the test emits messages. */
class FakeUpstream {
  static instances: FakeUpstream[] = []
  readonly url: string
  closed = false
  onopen: ((ev: unknown) => void) | null = null
  onmessage: ((ev: { data: unknown }) => void) | null = null
  onerror: ((ev: unknown) => void) | null = null
  onclose: ((ev: unknown) => void) | null = null
  constructor(url: string) {
    this.url = url
    FakeUpstream.instances.push(this)
  }
  close(): void {
    this.closed = true
  }
  emit(data: unknown): void {
    this.onmessage?.({ data: JSON.stringify(data) })
  }
  emitRaw(data: string): void {
    this.onmessage?.({ data })
  }
}

/** Minimal view of the `ws` client returned by `app.injectWS`. */
interface TestClient {
  readyState: number
  close(): void
  terminate(): void
  on(event: 'message', cb: (data: Buffer) => void): void
  on(event: 'close', cb: (code: number, reason: Buffer) => void): void
  on(event: 'ping', cb: () => void): void
}

interface Connected {
  ws: TestClient
  messages: unknown[]
  closed: Promise<{ code: number; reason: string }>
  next: (n: number) => Promise<unknown[]>
}

const kline = (x = false) => ({
  e: 'kline',
  E: 1_700_000_100_001,
  s: 'BTCUSDT',
  k: {
    t: 1_700_000_100_000,
    s: 'BTCUSDT',
    i: '1m',
    o: '1',
    h: '2',
    l: '0.5',
    c: '1.5',
    v: '10',
    x,
  },
})

const tick = async (): Promise<void> => {
  for (let i = 0; i < 5; i++) await new Promise<void>((r) => setImmediate(r))
}

describe('stream routes', () => {
  let db: Db
  let app: FastifyInstance

  beforeEach(async () => {
    FakeUpstream.instances = []
    db = openDb(':memory:')
    app = await buildApp({
      db,
      fetch: () => Promise.reject(new Error('no network in tests')),
      WebSocketImpl: FakeUpstream as unknown as typeof WebSocket,
      wsBase: 'wss://upstream.test',
    })
    await app.ready()
  })

  afterEach(async () => {
    vi.useRealTimers()
    await app.close()
    db.close()
  })

  async function connect(path: string, headers: Record<string, string> = {}): Promise<Connected> {
    const messages: unknown[] = []
    let resolveClosed!: (v: { code: number; reason: string }) => void
    const closed = new Promise<{ code: number; reason: string }>((r) => (resolveClosed = r))
    const waiters: (() => void)[] = []
    const ws = (await app.injectWS(
      path,
      // injectWS sends no Host; the app's DNS-rebinding guard requires a loopback one.
      { headers: { host: 'localhost:80', ...headers } },
      {
        onInit: (raw) => {
          const c = raw as unknown as TestClient
          c.on('message', (data) => {
            messages.push(JSON.parse(data.toString()))
            for (const w of waiters.splice(0)) w()
          })
          c.on('close', (code, reason) => resolveClosed({ code, reason: reason.toString() }))
        },
      },
    )) as unknown as TestClient
    const next = async (n: number): Promise<unknown[]> => {
      while (messages.length < n) await new Promise<void>((r) => waiters.push(r))
      return messages.slice(0, n)
    }
    return { ws, messages, closed, next }
  }

  const health = async (): Promise<number> =>
    (await app.inject({ method: 'GET', url: '/api/health' })).json<{ upstreamSockets: number }>()
      .upstreamSockets

  it('rejects an invalid symbol with close code 1008', async () => {
    const c = await connect('/ws/klines?symbol=bad!sym&interval=1m')
    await expect(c.closed).resolves.toEqual({ code: 1008, reason: 'invalid symbol' })
    expect(FakeUpstream.instances).toHaveLength(0)
  })

  it('rejects a missing symbol and an invalid interval with 1008', async () => {
    const a = await connect('/ws/klines?interval=1m')
    expect((await a.closed).code).toBe(1008)
    const b = await connect('/ws/klines?symbol=BTCUSDT&interval=2h')
    await expect(b.closed).resolves.toEqual({ code: 1008, reason: 'invalid interval' })
    expect(FakeUpstream.instances).toHaveLength(0)
  })

  it('sends status on attach and fans out klines; symbol is uppercased', async () => {
    const c = await connect('/ws/klines?symbol=btcusdt&interval=1m')
    expect(await c.next(1)).toEqual([{ type: 'status', status: 'reconnecting' }])
    expect(FakeUpstream.instances.map((u) => u.url)).toEqual([
      'wss://upstream.test/ws/btcusdt@kline_1m',
    ])
    FakeUpstream.instances[0]!.emit(kline(true))
    expect(await c.next(3)).toEqual([
      { type: 'status', status: 'reconnecting' },
      { type: 'status', status: 'live' },
      {
        type: 'kline',
        candle: { time: 1_700_000_100, open: 1, high: 2, low: 0.5, close: 1.5, volume: 10 },
        closed: true,
      },
    ])
    c.ws.close()
  })

  it('two client sockets share one upstream socket, closed when both leave', async () => {
    const a = await connect('/ws/klines?symbol=BTCUSDT&interval=1m')
    const b = await connect('/ws/klines?symbol=BTCUSDT&interval=1m')
    expect(FakeUpstream.instances).toHaveLength(1)
    expect(await health()).toBe(1)
    FakeUpstream.instances[0]!.emit(kline())
    await a.next(3)
    await b.next(3)
    expect(a.messages[2]).toEqual(b.messages[2])

    // The injectWS in-memory duplex never emits 'close' after a graceful close handshake, so
    // the server socket only closes when ws's 30 s close timeout destroys it. Fake that timer.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    a.ws.close()
    await tick()
    vi.advanceTimersByTime(30_000)
    await a.closed
    await tick()
    expect(FakeUpstream.instances[0]!.closed).toBe(false)
    expect(await health()).toBe(1)

    b.ws.close()
    await tick()
    vi.advanceTimersByTime(30_000)
    await b.closed
    await tick()
    // Kept for the release grace period, then closed.
    expect(FakeUpstream.instances[0]!.closed).toBe(false)
    vi.advanceTimersByTime(DEFAULT_RELEASE_GRACE_MS)
    expect(FakeUpstream.instances[0]!.closed).toBe(true)
    expect(await health()).toBe(0)
  })

  it('a terminated (errored) client still releases the upstream after the grace period', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const a = await connect('/ws/tickers')
    expect(await health()).toBe(1)
    a.ws.terminate()
    await a.closed
    await tick()
    expect(FakeUpstream.instances[0]!.closed).toBe(false)
    vi.advanceTimersByTime(DEFAULT_RELEASE_GRACE_MS)
    expect(FakeUpstream.instances[0]!.closed).toBe(true)
    expect(await health()).toBe(0)
  })

  it('a client reconnecting within the grace period reuses the upstream socket', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const a = await connect('/ws/tickers')
    a.ws.terminate()
    await a.closed
    await tick()
    const b = await connect('/ws/tickers')
    expect(await b.next(1)).toEqual([{ type: 'status', status: 'reconnecting' }])
    vi.advanceTimersByTime(DEFAULT_RELEASE_GRACE_MS * 2)
    expect(FakeUpstream.instances).toHaveLength(1)
    expect(FakeUpstream.instances[0]!.closed).toBe(false)
    expect(await health()).toBe(1)
    b.ws.terminate()
  })

  it('closing the app during the grace period closes the upstream at once', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const a = await connect('/ws/tickers')
    a.ws.terminate()
    await a.closed
    await tick()
    expect(FakeUpstream.instances[0]!.closed).toBe(false)
    await app.close()
    expect(FakeUpstream.instances[0]!.closed).toBe(true)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('serves tickers and ignores client messages', async () => {
    const c = await connect('/ws/tickers')
    expect(FakeUpstream.instances[0]!.url).toBe('wss://upstream.test/ws/!miniTicker@arr')
    ;(c.ws as unknown as { send(s: string): void }).send('{"method":"SUBSCRIBE"}')
    FakeUpstream.instances[0]!.emit([
      {
        e: '24hrMiniTicker',
        E: 1_700_000_000_000,
        s: 'ETHUSDT',
        c: '3',
        o: '2',
        h: '3',
        l: '2',
        v: '1',
        q: '3',
      },
    ])
    const msgs = await c.next(3)
    expect(msgs[2]).toEqual({
      type: 'tickers',
      tickers: [
        {
          symbol: 'ETHUSDT',
          last: 3,
          open: 2,
          high: 3,
          low: 2,
          volume: 1,
          quoteVolume: 3,
          changePct: 50,
          time: 1_700_000_000,
        },
      ],
    })
    expect(FakeUpstream.instances).toHaveLength(1)
    c.ws.close()
  })

  it('pings every client every 20 s', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    const c = await connect('/ws/tickers')
    let pings = 0
    c.ws.on('ping', () => (pings += 1))
    vi.advanceTimersByTime(PING_INTERVAL_MS - 1)
    await tick()
    expect(pings).toBe(0)
    vi.advanceTimersByTime(1)
    await tick()
    expect(pings).toBe(1)
    vi.advanceTimersByTime(PING_INTERVAL_MS)
    await tick()
    expect(pings).toBe(2)
    c.ws.close()
    await c.closed
  })

  it('re-sends the current status every 20 s as an app-level heartbeat', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    const c = await connect('/ws/klines?symbol=BTCUSDT&interval=1m')
    expect(await c.next(1)).toEqual([{ type: 'status', status: 'reconnecting' }])
    FakeUpstream.instances[0]!.emit(kline())
    await c.next(3)
    vi.advanceTimersByTime(PING_INTERVAL_MS - 1)
    await tick()
    expect(c.messages).toHaveLength(3)
    vi.advanceTimersByTime(1)
    expect(await c.next(4)).toContainEqual({ type: 'status', status: 'live' })
    expect(c.messages[3]).toEqual({ type: 'status', status: 'live' })
    vi.advanceTimersByTime(PING_INTERVAL_MS)
    await c.next(5)
    expect(c.messages[4]).toEqual({ type: 'status', status: 'live' })
    c.ws.terminate()
  })

  it('accepts a missing or localhost Origin and rejects others with 1008', async () => {
    const evil = await connect('/ws/tickers', { origin: 'http://evil.example' })
    await expect(evil.closed).resolves.toEqual({ code: 1008, reason: 'origin not allowed' })
    const sneaky = await connect('/ws/klines?symbol=BTCUSDT&interval=1m', {
      origin: 'http://localhost.evil.example:5173',
    })
    await expect(sneaky.closed).resolves.toEqual({ code: 1008, reason: 'origin not allowed' })
    expect(FakeUpstream.instances).toHaveLength(0)

    const local = await connect('/ws/klines?symbol=BTCUSDT&interval=1m', {
      origin: 'http://localhost:5173',
    })
    expect(await local.next(1)).toEqual([{ type: 'status', status: 'reconnecting' }])
    const loop = await connect('/ws/tickers', { origin: 'http://127.0.0.1:5173' })
    expect(await loop.next(1)).toEqual([{ type: 'status', status: 'reconnecting' }])
    expect(FakeUpstream.instances).toHaveLength(2)
    local.ws.terminate()
    loop.ws.terminate()
  })

  it('isAllowedOrigin', () => {
    expect(isAllowedOrigin(undefined)).toBe(true)
    expect(isAllowedOrigin('http://localhost')).toBe(true)
    expect(isAllowedOrigin('http://localhost:5173')).toBe(true)
    expect(isAllowedOrigin('http://127.0.0.1:3000')).toBe(true)
    expect(isAllowedOrigin('https://localhost:5173')).toBe(false)
    expect(isAllowedOrigin('http://192.168.1.2:5173')).toBe(false)
    expect(isAllowedOrigin('null')).toBe(false)
    expect(isAllowedOrigin('')).toBe(false)
    expect(isAllowedOrigin(['http://localhost'])).toBe(false)
  })

  it(`caps distinct upstream streams at ${MAX_UPSTREAM_KEYS} (1013); existing keys still attach`, async () => {
    const clients: Connected[] = []
    for (let i = 0; i < MAX_UPSTREAM_KEYS - 1; i++) {
      clients.push(await connect(`/ws/klines?symbol=S${i}USDT&interval=1m`))
    }
    clients.push(await connect('/ws/tickers'))
    expect(FakeUpstream.instances).toHaveLength(MAX_UPSTREAM_KEYS)
    const over = await connect('/ws/klines?symbol=OVERUSDT&interval=1m')
    await expect(over.closed).resolves.toEqual({ code: 1013, reason: 'too many streams' })
    expect(FakeUpstream.instances).toHaveLength(MAX_UPSTREAM_KEYS)
    const again = await connect('/ws/klines?symbol=S0USDT&interval=1m')
    expect(await again.next(1)).toEqual([{ type: 'status', status: 'reconnecting' }])
    const tickers = await connect('/ws/tickers')
    expect(await tickers.next(1)).toEqual([{ type: 'status', status: 'reconnecting' }])
    expect(FakeUpstream.instances).toHaveLength(MAX_UPSTREAM_KEYS)
    for (const c of [...clients, again, tickers]) c.ws.terminate()
  })

  it('serializes each hub event once regardless of client count', async () => {
    const a = await connect('/ws/tickers')
    const b = await connect('/ws/tickers')
    const c = await connect('/ws/tickers')
    const spy = vi.spyOn(JSON, 'stringify')
    FakeUpstream.instances[0]!.emitRaw(
      '[{"e":"24hrMiniTicker","E":1700000000000,"s":"ETHUSDT","c":"3","o":"2","h":"3","l":"2","v":"1","q":"3"}]',
    )
    await a.next(3)
    await b.next(3)
    await c.next(3)
    const tickerCalls = spy.mock.calls.filter(
      ([v]) => typeof v === 'object' && v !== null && (v as { type?: unknown }).type === 'tickers',
    )
    spy.mockRestore()
    expect(tickerCalls).toHaveLength(1)
    for (const x of [a, b, c]) x.ws.terminate()
  })

  it('terminates a client whose send buffer exceeds the backpressure limit', async () => {
    const serverSockets: { bufferedAmount: number }[] = []
    app.websocketServer.on('connection', (s: { bufferedAmount: number }) => serverSockets.push(s))
    const slow = await connect('/ws/tickers')
    const fast = await connect('/ws/tickers')
    await slow.next(1)
    expect(serverSockets).toHaveLength(2)
    Object.defineProperty(serverSockets[0], 'bufferedAmount', {
      get: () => MAX_BUFFERED_BYTES + 1,
    })
    FakeUpstream.instances[0]!.emit([
      {
        e: '24hrMiniTicker',
        E: 1_700_000_000_000,
        s: 'ETHUSDT',
        c: '3',
        o: '2',
        h: '3',
        l: '2',
        v: '1',
        q: '3',
      },
    ])
    // Terminated, not a graceful close queued behind the full buffer: abnormal closure.
    await expect(slow.closed).resolves.toMatchObject({ code: 1006 })
    const msgs = await fast.next(3)
    expect(msgs[2]).toMatchObject({ type: 'tickers' })
    expect(FakeUpstream.instances[0]!.closed).toBe(false)
    fast.ws.close()
  })

  it('closing the app closes every upstream socket', async () => {
    await connect('/ws/klines?symbol=BTCUSDT&interval=1m')
    await connect('/ws/tickers')
    expect(FakeUpstream.instances).toHaveLength(2)
    await app.close()
    expect(FakeUpstream.instances.every((u) => u.closed)).toBe(true)
  })
})
