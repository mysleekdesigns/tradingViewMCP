import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Candle, ConnectionStatus, Ticker } from '../types'
import { MarketDataError, type MarketDataErrorKind } from './provider'
import { ServerProvider } from './server'

// ---------- fakes ----------

type FetchHandler = (url: string, init?: RequestInit) => Promise<Response>

function fakeFetch(handler: FetchHandler) {
  return vi.fn((input: RequestInfo | URL, init?: RequestInit) => handler(String(input), init))
}

function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(typeof body === 'string' ? body : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  })
}

/** A fetch that never resolves but rejects (like the real one) when its signal aborts. */
function hangingFetch() {
  return fakeFetch(
    (_url, init) =>
      new Promise<Response>((_resolve, reject) => {
        const signal = init?.signal
        signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
      }),
  )
}

async function expectKind(
  p: Promise<unknown>,
  kind: MarketDataErrorKind,
  status?: number,
  retryAfter?: number,
): Promise<MarketDataError> {
  const err = await p.then(
    () => {
      throw new Error('expected rejection')
    },
    (e: unknown) => e,
  )
  expect(err).toBeInstanceOf(MarketDataError)
  const me = err as MarketDataError
  expect(me.kind).toBe(kind)
  if (status !== undefined) expect(me.status).toBe(status)
  if (retryAfter !== undefined) expect(me.retryAfter).toBe(retryAfter)
  return me
}

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
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
  serverOpen(): void {
    this.readyState = 1
    this.onopen?.(new Event('open'))
  }
  serverMessage(data: unknown): void {
    this.onmessage?.({
      data: typeof data === 'string' ? data : JSON.stringify(data),
    } as MessageEvent)
  }
  serverClose(code = 1006): void {
    this.readyState = 3
    this.onclose?.({ code } as CloseEvent)
  }
  serverError(): void {
    this.onerror?.(new Event('error'))
  }
}

const WS = FakeWebSocket as unknown as typeof WebSocket
const lastWs = () => FakeWebSocket.instances[FakeWebSocket.instances.length - 1]

const CANDLE_A: Candle = { time: 1_700_000_000, open: 1, high: 2, low: 0.5, close: 1.5, volume: 10 }
const CANDLE_B: Candle = { time: 1_700_003_600, open: 1.5, high: 3, low: 1, close: 2.5, volume: 7 }
const SYMBOL = {
  symbol: 'BTCUSDT',
  baseAsset: 'BTC',
  quoteAsset: 'USDT',
  status: 'TRADING',
  tickSize: 0.01,
}
const TICKER: Ticker = {
  symbol: 'BTCUSDT',
  last: 101,
  open: 100,
  high: 102,
  low: 99,
  volume: 5,
  quoteVolume: 500,
  changePct: 1,
  time: 1_700_000_000,
}

// ---------- REST ----------

describe('ServerProvider.getCandles', () => {
  it('builds a same-origin relative URL by default', async () => {
    const f = fakeFetch(async () => json([CANDLE_A]))
    await new ServerProvider({ fetchImpl: f }).getCandles(' btcusdt ', '1h')
    const url = String(f.mock.calls[0][0])
    expect(url.startsWith('/api/candles?')).toBe(true)
    const q = new URLSearchParams(url.split('?')[1])
    expect(q.get('symbol')).toBe('BTCUSDT')
    expect(q.get('interval')).toBe('1h')
    expect(q.get('limit')).toBe('500')
    expect(q.has('endTime')).toBe(false)
  })

  it('uses baseUrl, clamps limit, passes endTime in seconds', async () => {
    const f = fakeFetch(async () => json([]))
    await new ServerProvider({ baseUrl: 'http://127.0.0.1:8787/', fetchImpl: f }).getCandles(
      'ETHUSDT',
      '1d',
      5000,
      1_700_000_000.7,
    )
    const url = new URL(String(f.mock.calls[0][0]))
    expect(url.origin + url.pathname).toBe('http://127.0.0.1:8787/api/candles')
    expect(url.searchParams.get('limit')).toBe('1000')
    expect(url.searchParams.get('endTime')).toBe('1700000000')
  })

  it('parses candles, sorts ascending, and dedupes by time', async () => {
    const later = { ...CANDLE_A, close: 9 }
    const f = fakeFetch(async () => json([CANDLE_B, CANDLE_A, later]))
    const out = await new ServerProvider({ fetchImpl: f }).getCandles('BTCUSDT', '1h')
    expect(out).toEqual([later, CANDLE_B])
  })

  it.each([
    ['non-array body', { candles: [] }],
    ['missing field', [{ time: 1, open: 1, high: 1, low: 1, close: 1 }]],
    ['string price', [{ ...CANDLE_A, close: '1.5' }]],
    ['null price', [{ ...CANDLE_A, open: null }]],
  ])('rejects %s as parse', async (_name, body) => {
    const f = fakeFetch(async () => json(body))
    await expectKind(new ServerProvider({ fetchImpl: f }).getCandles('BTCUSDT', '1h'), 'parse')
  })

  it('rejects non-finite numbers (JSON 1e999 = Infinity) as parse', async () => {
    const raw = `[{"time":1,"open":1e999,"high":1,"low":1,"close":1,"volume":1}]`
    const f = fakeFetch(async () => json(raw))
    await expectKind(new ServerProvider({ fetchImpl: f }).getCandles('BTCUSDT', '1h'), 'parse')
  })

  it('rejects invalid JSON as parse', async () => {
    const f = fakeFetch(async () => json('{nope'))
    await expectKind(new ServerProvider({ fetchImpl: f }).getCandles('BTCUSDT', '1h'), 'parse')
  })
})

describe('ServerProvider error mapping', () => {
  it.each<[number, MarketDataErrorKind, number | undefined]>([
    [400, 'bad-symbol', undefined],
    [400, 'http', undefined],
    [429, 'rate-limited', 12],
    [503, 'banned', 120],
    [503, 'region-blocked', undefined],
    [502, 'network', undefined],
    [502, 'parse', undefined],
    [404, 'http', undefined],
  ])('HTTP %i {error:%s} -> kind', async (status, kind, retryAfter) => {
    const body: Record<string, unknown> = { error: kind, message: `server says ${kind}` }
    if (retryAfter !== undefined) body.retryAfter = retryAfter
    const f = fakeFetch(async () => json(body, status))
    const err = await expectKind(
      new ServerProvider({ fetchImpl: f }).getCandles('BTCUSDT', '1h'),
      kind,
      status,
    )
    expect(err.message).toBe(`server says ${kind}`)
    expect(err.retryAfter).toBe(retryAfter)
  })

  it('unknown error kind -> http', async () => {
    const f = fakeFetch(async () => json({ error: 'teapot', message: 'x' }, 500))
    await expectKind(new ServerProvider({ fetchImpl: f }).getCandles('BTCUSDT', '1h'), 'http', 500)
  })

  it('server-sent "aborted" is not trusted -> http', async () => {
    const f = fakeFetch(async () => json({ error: 'aborted', message: 'x' }, 500))
    await expectKind(new ServerProvider({ fetchImpl: f }).getCandles('BTCUSDT', '1h'), 'http', 500)
  })

  it('non-JSON error body -> http with status', async () => {
    const f = fakeFetch(async () => new Response('<html>bad gateway</html>', { status: 502 }))
    await expectKind(new ServerProvider({ fetchImpl: f }).getCandles('BTCUSDT', '1h'), 'http', 502)
  })

  it('uses the Retry-After header when the body has no retryAfter', async () => {
    const f = fakeFetch(async () =>
      json({ error: 'rate-limited', message: 'slow down' }, 429, { 'Retry-After': '30' }),
    )
    await expectKind(
      new ServerProvider({ fetchImpl: f }).getCandles('BTCUSDT', '1h'),
      'rate-limited',
      429,
      30,
    )
  })

  it('prefers the body retryAfter over the header', async () => {
    const f = fakeFetch(async () =>
      json({ error: 'banned', message: 'ban', retryAfter: 60 }, 503, { 'Retry-After': '5' }),
    )
    await expectKind(
      new ServerProvider({ fetchImpl: f }).getCandles('BTCUSDT', '1h'),
      'banned',
      503,
      60,
    )
  })

  it('transport failure -> network', async () => {
    const f = fakeFetch(async () => {
      throw new TypeError('Failed to fetch')
    })
    await expectKind(new ServerProvider({ fetchImpl: f }).getCandles('BTCUSDT', '1h'), 'network')
  })

  it('timeout -> network', async () => {
    const p = new ServerProvider({ fetchImpl: hangingFetch(), timeoutMs: 20 }).getCandles(
      'BTCUSDT',
      '1h',
    )
    const err = await expectKind(p, 'network')
    expect(err.message).toMatch(/Timed out/)
  })

  it('caller abort mid-request -> aborted', async () => {
    const ctrl = new AbortController()
    const p = new ServerProvider({ fetchImpl: hangingFetch() }).getCandles(
      'BTCUSDT',
      '1h',
      500,
      undefined,
      { signal: ctrl.signal },
    )
    ctrl.abort()
    await expectKind(p, 'aborted')
  })

  it('already-aborted signal -> aborted without a request', async () => {
    const f = fakeFetch(async () => json([]))
    const ctrl = new AbortController()
    ctrl.abort()
    await expectKind(
      new ServerProvider({ fetchImpl: f }).getCandles('BTCUSDT', '1h', 500, undefined, {
        signal: ctrl.signal,
      }),
      'aborted',
    )
    expect(f).not.toHaveBeenCalled()
  })
})

describe('ServerProvider.getSymbols', () => {
  it('fetches /api/symbols once and caches per instance', async () => {
    const f = fakeFetch(async () => json([SYMBOL]))
    const p = new ServerProvider({ fetchImpl: f })
    expect(await p.getSymbols()).toEqual([SYMBOL])
    expect(await p.getSymbols()).toEqual([SYMBOL])
    expect(f).toHaveBeenCalledTimes(1)
    expect(String(f.mock.calls[0][0])).toBe('/api/symbols')
  })

  it('does not cache failures', async () => {
    let calls = 0
    const f = fakeFetch(async () => {
      calls += 1
      return calls === 1 ? json({ error: 'network', message: 'down' }, 502) : json([SYMBOL])
    })
    const p = new ServerProvider({ fetchImpl: f })
    await expectKind(p.getSymbols(), 'network', 502)
    expect(await p.getSymbols()).toEqual([SYMBOL])
    expect(f).toHaveBeenCalledTimes(2)
  })

  it('rejects malformed entries as parse (and retries next time)', async () => {
    const f = fakeFetch(async () => json([{ ...SYMBOL, tickSize: 'x' }]))
    const p = new ServerProvider({ fetchImpl: f })
    await expectKind(p.getSymbols(), 'parse')
    await expectKind(p.getSymbols(), 'parse')
    expect(f).toHaveBeenCalledTimes(2)
  })

  it('a caller abort rejects only that caller', async () => {
    let resolve!: (r: Response) => void
    const f = fakeFetch(() => new Promise<Response>((r) => (resolve = r)))
    const p = new ServerProvider({ fetchImpl: f })
    const ctrl = new AbortController()
    const a = p.getSymbols({ signal: ctrl.signal })
    const b = p.getSymbols()
    ctrl.abort()
    await expectKind(a, 'aborted')
    resolve(json([SYMBOL]))
    expect(await b).toEqual([SYMBOL])
    expect(f).toHaveBeenCalledTimes(1)
  })
})

describe('ServerProvider.getTickers', () => {
  it('empty or all-invalid input -> [] without a request', async () => {
    const f = fakeFetch(async () => json([]))
    const p = new ServerProvider({ fetchImpl: f })
    expect(await p.getTickers([])).toEqual([])
    expect(await p.getTickers(['  ', 'bad symbol!'])).toEqual([])
    expect(f).not.toHaveBeenCalled()
  })

  it('requests /api/tickers?symbols=A,B (normalized, deduped) and parses', async () => {
    const f = fakeFetch(async () => json([TICKER]))
    const out = await new ServerProvider({ fetchImpl: f }).getTickers([
      'btcusdt',
      'ETHUSDT',
      'BTCUSDT',
    ])
    const url = String(f.mock.calls[0][0])
    expect(url.split('?')[0]).toBe('/api/tickers')
    expect(new URLSearchParams(url.split('?')[1]).get('symbols')).toBe('BTCUSDT,ETHUSDT')
    expect(out).toEqual([TICKER])
  })

  it('rejects malformed tickers as parse', async () => {
    const f = fakeFetch(async () => json([{ ...TICKER, last: null }]))
    await expectKind(new ServerProvider({ fetchImpl: f }).getTickers(['BTCUSDT']), 'parse')
  })
})

// ---------- WebSocket ----------

let win: EventTarget

beforeEach(() => {
  FakeWebSocket.instances = []
  win = new EventTarget()
  vi.stubGlobal('window', win)
  vi.stubGlobal('navigator', { onLine: true })
  vi.stubGlobal('location', { protocol: 'http:', host: 'localhost:5173' })
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function setupCandles(opts?: { baseUrl?: string }) {
  const p = new ServerProvider({ WebSocketImpl: WS, baseUrl: opts?.baseUrl })
  const candles: [Candle, boolean][] = []
  const statuses: ConnectionStatus[] = []
  const unsubscribe = p.subscribeCandles(
    'btcusdt',
    '1m',
    (c, closed) => candles.push([c, closed]),
    { onStatus: (s) => statuses.push(s) },
  )
  return { candles, statuses, unsubscribe }
}

describe('ServerProvider.subscribeCandles', () => {
  it('derives ws:// from location by default', () => {
    setupCandles()
    expect(lastWs().url).toBe('ws://localhost:5173/ws/klines?symbol=BTCUSDT&interval=1m')
  })

  it('uses wss:// on an https page', () => {
    vi.stubGlobal('location', { protocol: 'https:', host: 'app.test' })
    setupCandles()
    expect(lastWs().url).toBe('wss://app.test/ws/klines?symbol=BTCUSDT&interval=1m')
  })

  it('derives the WS URL from baseUrl (http -> ws, https -> wss)', () => {
    setupCandles({ baseUrl: 'http://127.0.0.1:8787/' })
    expect(lastWs().url).toBe('ws://127.0.0.1:8787/ws/klines?symbol=BTCUSDT&interval=1m')
    setupCandles({ baseUrl: 'https://srv.test' })
    expect(lastWs().url).toBe('wss://srv.test/ws/klines?symbol=BTCUSDT&interval=1m')
  })

  it('delivers kline messages with the closed flag and ignores malformed ones', () => {
    const { candles } = setupCandles()
    const ws = lastWs()
    ws.serverOpen()
    ws.serverMessage({ type: 'kline', candle: CANDLE_A, closed: false })
    ws.serverMessage('not json')
    ws.serverMessage({ type: 'kline', candle: { ...CANDLE_A, close: 'x' }, closed: true })
    ws.serverMessage({ type: 'kline', candle: CANDLE_A })
    ws.serverMessage({ type: 'kline', candle: CANDLE_A, closed: 'yes' })
    ws.serverMessage({ type: 'tickers', tickers: [TICKER] })
    ws.serverMessage({ type: 'status', status: 'weird' })
    ws.serverMessage({ type: 'nope' })
    ws.serverMessage([1, 2, 3])
    ws.serverMessage({ type: 'kline', candle: CANDLE_A, closed: true })
    expect(candles).toEqual([
      [CANDLE_A, false],
      [CANDLE_A, true],
    ])
  })

  it('combines socket and upstream status: live -> reconnecting -> live, de-duplicated', () => {
    const { statuses } = setupCandles()
    const ws = lastWs()
    expect(statuses).toEqual(['connecting'])
    ws.serverOpen()
    expect(statuses).toEqual(['connecting']) // live only once the first message arrives
    ws.serverMessage({ type: 'status', status: 'live' }) // attach status
    expect(statuses).toEqual(['connecting', 'live'])
    ws.serverMessage({ type: 'status', status: 'live' })
    expect(statuses).toEqual(['connecting', 'live'])
    ws.serverMessage({ type: 'status', status: 'reconnecting' })
    ws.serverMessage({ type: 'status', status: 'reconnecting' })
    expect(statuses).toEqual(['connecting', 'live', 'reconnecting'])
    ws.serverMessage({ type: 'status', status: 'live' })
    expect(statuses).toEqual(['connecting', 'live', 'reconnecting', 'live'])
  })

  it('upstream status before the socket is live does not override the socket status', () => {
    vi.useFakeTimers()
    vi.spyOn(Math, 'random').mockReturnValue(1)
    const { statuses } = setupCandles()
    lastWs().serverOpen()
    lastWs().serverMessage({ type: 'status', status: 'live' })
    lastWs().serverMessage({ type: 'status', status: 'reconnecting' })
    expect(statuses).toEqual(['connecting', 'live', 'reconnecting'])
    // Browser socket drops: socket status 'reconnecting' (same value, de-duped).
    lastWs().serverClose()
    expect(statuses).toEqual(['connecting', 'live', 'reconnecting'])
    vi.advanceTimersByTime(1_000)
    // New socket opens; the stale upstream status is forgotten, and the attach status
    // (upstream still reconnecting) is applied before the socket reads live: no live blip.
    lastWs().serverOpen()
    lastWs().serverMessage({ type: 'status', status: 'reconnecting' })
    expect(statuses).toEqual(['connecting', 'live', 'reconnecting'])
    lastWs().serverMessage({ type: 'status', status: 'live' })
    expect(statuses).toEqual(['connecting', 'live', 'reconnecting', 'live'])
  })

  it('W1: a 1008 close before any message reads offline, never live, and stops retrying', () => {
    vi.useFakeTimers()
    const { statuses, unsubscribe } = setupCandles()
    const before = FakeWebSocket.instances.length
    lastWs().serverOpen()
    lastWs().serverClose(1008)
    expect(statuses).toEqual(['connecting', 'offline'])
    expect(vi.getTimerCount()).toBe(0) // no reconnect or stale timer left
    vi.advanceTimersByTime(10 * 60_000)
    expect(FakeWebSocket.instances.length).toBe(before) // no reconnect
    // Window listeners are gone too: going online again does not reconnect.
    win.dispatchEvent(new Event('offline'))
    win.dispatchEvent(new Event('online'))
    expect(FakeWebSocket.instances.length).toBe(before)
    expect(statuses).toEqual(['connecting', 'offline'])
    unsubscribe() // still safe
    expect(statuses).toEqual(['connecting', 'offline'])
  })

  it('W1: an error followed by a 1008 close is still fatal', () => {
    vi.useFakeTimers()
    const { statuses } = setupCandles()
    const before = FakeWebSocket.instances.length
    lastWs().serverOpen()
    lastWs().serverError()
    lastWs().serverClose(1008)
    vi.advanceTimersByTime(10 * 60_000)
    expect(statuses).toEqual(['connecting', 'offline'])
    expect(FakeWebSocket.instances.length).toBe(before)
  })

  it('W1: a 1008 close after messages also stops (offline, no gap-fill cycle)', () => {
    vi.useFakeTimers()
    const { statuses } = setupCandles()
    const before = FakeWebSocket.instances.length
    lastWs().serverOpen()
    lastWs().serverMessage({ type: 'status', status: 'live' })
    lastWs().serverClose(1008)
    vi.advanceTimersByTime(10 * 60_000)
    expect(statuses).toEqual(['connecting', 'live', 'offline'])
    expect(FakeWebSocket.instances.length).toBe(before)
  })

  it('W1: a non-fatal close still reconnects', () => {
    vi.useFakeTimers()
    vi.spyOn(Math, 'random').mockReturnValue(1)
    const { statuses } = setupCandles()
    const before = FakeWebSocket.instances.length
    lastWs().serverOpen()
    lastWs().serverMessage({ type: 'status', status: 'live' })
    lastWs().serverClose(1011)
    expect(statuses).toEqual(['connecting', 'live', 'reconnecting'])
    vi.advanceTimersByTime(1_000)
    expect(FakeWebSocket.instances.length).toBe(before + 1)
  })

  it('W1: /ws/tickers closed with 1008 also stops', () => {
    vi.useFakeTimers()
    const statuses: ConnectionStatus[] = []
    new ServerProvider({ WebSocketImpl: WS }).subscribeTickers(() => {}, {
      onStatus: (s) => statuses.push(s),
    })
    const before = FakeWebSocket.instances.length
    lastWs().serverOpen()
    lastWs().serverClose(1008)
    vi.advanceTimersByTime(10 * 60_000)
    expect(statuses).toEqual(['connecting', 'offline'])
    expect(FakeWebSocket.instances.length).toBe(before)
  })

  it('unsubscribe is idempotent, closes the socket, and stops all callbacks', () => {
    const { candles, statuses, unsubscribe } = setupCandles()
    const ws = lastWs()
    ws.serverOpen()
    unsubscribe()
    unsubscribe()
    expect(ws.closeCalls).toBe(1)
    const before = statuses.length
    ws.serverMessage({ type: 'kline', candle: CANDLE_A, closed: true })
    ws.serverMessage({ type: 'status', status: 'reconnecting' })
    ws.serverClose()
    expect(candles).toEqual([])
    expect(statuses).toHaveLength(before)
  })

  it('does not throw out of onmessage when onCandle throws', () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    new ServerProvider({ WebSocketImpl: WS }).subscribeCandles('BTCUSDT', '1m', () => {
      throw new Error('boom')
    })
    lastWs().serverOpen()
    expect(() =>
      lastWs().serverMessage({ type: 'kline', candle: CANDLE_A, closed: false }),
    ).not.toThrow()
    expect(errSpy).toHaveBeenCalled()
  })
})

describe('ServerProvider.subscribeTickers', () => {
  it('opens /ws/tickers, delivers valid tickers, skips malformed ones', () => {
    const p = new ServerProvider({ WebSocketImpl: WS })
    const got: Ticker[][] = []
    const statuses: ConnectionStatus[] = []
    const un = p.subscribeTickers((t) => got.push(t), { onStatus: (s) => statuses.push(s) })
    const ws = lastWs()
    expect(ws.url).toBe('ws://localhost:5173/ws/tickers')
    ws.serverOpen()
    ws.serverMessage({
      type: 'tickers',
      tickers: [TICKER, { ...TICKER, symbol: 'ETHUSDT', last: 'x' }],
    })
    ws.serverMessage({ type: 'tickers', tickers: [{ bad: true }] })
    ws.serverMessage({ type: 'tickers', tickers: 'nope' })
    ws.serverMessage({ type: 'kline', candle: CANDLE_A, closed: true })
    expect(got).toEqual([[TICKER]])
    ws.serverMessage({ type: 'status', status: 'reconnecting' })
    ws.serverMessage({ type: 'status', status: 'live' })
    expect(statuses).toEqual(['connecting', 'live', 'reconnecting', 'live'])
    un()
    ws.serverMessage({ type: 'tickers', tickers: [TICKER] })
    expect(got).toHaveLength(1)
  })
})
