import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ConnectionStatus, Ticker } from '../types'
import { BinanceProvider } from './binance'
import { BINANCE_REST } from './venue'
import {
  changePct,
  normalizeSymbol,
  parseMiniTicker,
  parseMiniTickerMessage,
  parseTicker24hr,
} from './binanceTicker'
import { MarketDataError } from './provider'

// ---------- fixtures (inline, shaped like real Binance payloads) ----------

function rest24hr(symbol: string, last: string, open: string, closeTime = 1792868669656) {
  return {
    symbol,
    priceChange: '0',
    priceChangePercent: '0',
    weightedAvgPrice: '1',
    prevClosePrice: open,
    lastPrice: last,
    lastQty: '0.1',
    bidPrice: last,
    bidQty: '1',
    askPrice: last,
    askQty: '1',
    openPrice: open,
    highPrice: '81511.45',
    lowPrice: '79237.41',
    volume: '47.25124',
    quoteVolume: '3798679.9217329',
    openTime: closeTime - 86_400_000,
    closeTime,
    firstId: 1,
    lastId: 2,
    count: 2,
  }
}

function mini(s: string, c: string, o: string, E = 1792868670123) {
  return { e: '24hrMiniTicker', E, s, c, o, h: '110', l: '90', v: '1000', q: '100000' }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function fakeFetch(impl: (url: URL) => Promise<Response>) {
  return vi.fn((input: RequestInfo | URL) =>
    impl(new URL(String(input))),
  ) as unknown as typeof fetch & {
    mock: { calls: unknown[][] }
  }
}

const urlOf = (fetchFn: { mock: { calls: unknown[][] } }, i: number) =>
  new URL(String(fetchFn.mock.calls[i][0]))

/** Emulates Binance: `symbols=[...]` fails entirely with -1121 if any symbol is unknown. */
function binanceLike(known: Record<string, ReturnType<typeof rest24hr>>) {
  const invalid = () => jsonResponse({ code: -1121, msg: 'Invalid symbol.' }, 400)
  return fakeFetch(async (url) => {
    const many = url.searchParams.get('symbols')
    if (many !== null) {
      const list = JSON.parse(many) as string[]
      if (list.some((s) => !(s in known))) return invalid()
      return jsonResponse(list.map((s) => known[s]))
    }
    const one = url.searchParams.get('symbol') ?? ''
    return one in known ? jsonResponse(known[one]) : invalid()
  })
}

// ---------- pure parsers ----------

describe('changePct', () => {
  it('is (last - open) / open * 100', () => {
    expect(changePct(110, 100)).toBeCloseTo(10, 12)
    expect(changePct(95, 100)).toBeCloseTo(-5, 12)
  })
  it('is 0 when open is 0', () => {
    expect(changePct(5, 0)).toBe(0)
  })
})

describe('normalizeSymbol', () => {
  it('trims and uppercases', () => {
    expect(normalizeSymbol(' btcusdt ')).toBe('BTCUSDT')
  })
  it.each(['', 'BTC USDT', 'BTC"]', 'A'.repeat(21)])('rejects %j', (s) => {
    expect(normalizeSymbol(s)).toBeNull()
  })
})

describe('parseTicker24hr', () => {
  it('maps strings to numbers and closeTime ms to UNIX seconds', () => {
    expect(parseTicker24hr(rest24hr('BTCUSDT', '80597.51', '81418.01'))).toEqual({
      symbol: 'BTCUSDT',
      last: 80597.51,
      open: 81418.01,
      high: 81511.45,
      low: 79237.41,
      volume: 47.25124,
      quoteVolume: 3798679.9217329,
      changePct: ((80597.51 - 81418.01) / 81418.01) * 100,
      time: 1792868669,
    })
  })

  it('reports changePct 0 when openPrice is 0', () => {
    expect(parseTicker24hr(rest24hr('NEWUSDT', '1.5', '0'))?.changePct).toBe(0)
  })

  it.each([
    ['null', null],
    ['array', []],
    ['missing symbol', { ...rest24hr('A', '1', '1'), symbol: undefined }],
    ['empty lastPrice', rest24hr('A', '', '1')],
    ['NaN openPrice', rest24hr('A', '1', 'abc')],
    ['missing closeTime', { ...rest24hr('A', '1', '1'), closeTime: undefined }],
    ['Infinity volume', { ...rest24hr('A', '1', '1'), volume: 'Infinity' }],
  ])('returns null for %s', (_label, raw) => {
    expect(parseTicker24hr(raw)).toBeNull()
  })
})

describe('parseMiniTicker / parseMiniTickerMessage', () => {
  it('parses one element: c=last, o=open, E ms -> s', () => {
    expect(parseMiniTicker(mini('ETHUSDT', '2100', '2000', 1792868670999))).toEqual({
      symbol: 'ETHUSDT',
      last: 2100,
      open: 2000,
      high: 110,
      low: 90,
      volume: 1000,
      quoteVolume: 100000,
      changePct: 5,
      time: 1792868670,
    })
  })

  it('requires e === 24hrMiniTicker', () => {
    expect(parseMiniTicker({ ...mini('A', '1', '1'), e: '24hrTicker' })).toBeNull()
    expect(parseMiniTicker({ ...mini('A', '1', '1'), e: undefined })).toBeNull()
  })

  it('parses an array message from a JSON string, skipping malformed elements', () => {
    const msg = JSON.stringify([
      mini('BTCUSDT', '101', '100'),
      { e: '24hrMiniTicker', s: 'BAD' },
      null,
      mini('ETHUSDT', '', '100'),
      mini('SOLUSDT', '99', '100'),
    ])
    expect(parseMiniTickerMessage(msg).map((t) => t.symbol)).toEqual(['BTCUSDT', 'SOLUSDT'])
  })

  it('unwraps the combined-stream envelope', () => {
    const out = parseMiniTickerMessage({ stream: '!miniTicker@arr', data: [mini('A', '2', '1')] })
    expect(out.map((t) => t.changePct)).toEqual([100])
  })

  it.each([
    ['invalid JSON', '{nope'],
    ['object', { e: '24hrMiniTicker' }],
    ['number', 3],
    ['null', null],
  ])('returns [] for %s', (_label, data) => {
    expect(parseMiniTickerMessage(data)).toEqual([])
  })
})

// ---------- getTickers ----------

describe('BinanceProvider.getTickers', () => {
  const KNOWN = {
    BTCUSDT: rest24hr('BTCUSDT', '110', '100'),
    ETHUSDT: rest24hr('ETHUSDT', '90', '100'),
  }

  it('resolves [] for empty input without a request', async () => {
    const fetchFn = binanceLike(KNOWN)
    expect(await new BinanceProvider({ fetchFn }).getTickers([])).toEqual([])
    expect(fetchFn).not.toHaveBeenCalled()
  })

  it('makes one request with a JSON-encoded, normalized, de-duplicated symbols list', async () => {
    const fetchFn = binanceLike(KNOWN)
    const out = await new BinanceProvider({ fetchFn }).getTickers(['ethusdt', 'BTCUSDT', 'ETHUSDT'])
    expect(fetchFn).toHaveBeenCalledTimes(1)
    const url = urlOf(fetchFn, 0)
    expect(url.origin).toBe(BINANCE_REST)
    expect(url.pathname).toBe('/api/v3/ticker/24hr')
    expect(url.searchParams.get('symbols')).toBe('["ETHUSDT","BTCUSDT"]')
    // Input order, numbers parsed.
    expect(out.map((t) => [t.symbol, t.last, t.changePct])).toEqual([
      ['ETHUSDT', 90, -10],
      ['BTCUSDT', 110, 10],
    ])
  })

  it('drops syntactically invalid symbols locally; all invalid -> [] with no request', async () => {
    const fetchFn = binanceLike(KNOWN)
    const p = new BinanceProvider({ fetchFn })
    expect(await p.getTickers(['', 'BAD SYMBOL'])).toEqual([])
    expect(fetchFn).not.toHaveBeenCalled()
    await p.getTickers(['BTCUSDT', 'x y'])
    expect(urlOf(fetchFn, 0).searchParams.get('symbols')).toBe('["BTCUSDT"]')
  })

  it('omits an unknown symbol: on -1121 falls back to one request per symbol', async () => {
    const fetchFn = binanceLike(KNOWN)
    const out = await new BinanceProvider({ fetchFn }).getTickers([
      'BTCUSDT',
      'NOPEUSDT',
      'ETHUSDT',
    ])
    expect(out.map((t) => t.symbol)).toEqual(['BTCUSDT', 'ETHUSDT'])
    expect(fetchFn).toHaveBeenCalledTimes(4)
    expect([1, 2, 3].map((i) => urlOf(fetchFn, i).searchParams.get('symbol'))).toEqual([
      'BTCUSDT',
      'NOPEUSDT',
      'ETHUSDT',
    ])
  })

  it('a single unknown symbol resolves [] without fallback requests', async () => {
    const fetchFn = binanceLike(KNOWN)
    expect(await new BinanceProvider({ fetchFn }).getTickers(['NOPEUSDT'])).toEqual([])
    expect(fetchFn).toHaveBeenCalledTimes(1)
  })

  it('a non-bad-symbol error during fallback rejects the whole call', async () => {
    let n = 0
    const fetchFn = fakeFetch(async (url) => {
      n += 1
      if (n === 1) return jsonResponse({ code: -1121, msg: 'Invalid symbol.' }, 400)
      if (url.searchParams.get('symbol') === 'ETHUSDT') {
        return new Response('', { status: 429, headers: { 'Retry-After': '3' } })
      }
      return jsonResponse(KNOWN.BTCUSDT)
    })
    const err = await new BinanceProvider({ fetchFn })
      .getTickers(['BTCUSDT', 'ETHUSDT'])
      .catch((e: unknown) => e)
    expect(err).toBeInstanceOf(MarketDataError)
    expect((err as MarketDataError).kind).toBe('rate-limited')
    expect((err as MarketDataError).retryAfter).toBe(3)
  })

  it('skips malformed ticker objects and symbols the response did not ask for', async () => {
    const fetchFn = fakeFetch(async () =>
      jsonResponse([
        { ...KNOWN.BTCUSDT, lastPrice: 'x' },
        KNOWN.ETHUSDT,
        rest24hr('XRPUSDT', '1', '1'),
      ]),
    )
    const out = await new BinanceProvider({ fetchFn }).getTickers(['BTCUSDT', 'ETHUSDT'])
    expect(out.map((t) => t.symbol)).toEqual(['ETHUSDT'])
  })

  it('rejects a non-array body with parse', async () => {
    const fetchFn = fakeFetch(async () => jsonResponse(KNOWN.BTCUSDT))
    const err = await new BinanceProvider({ fetchFn }).getTickers(['BTCUSDT']).catch((e) => e)
    expect((err as MarketDataError).kind).toBe('parse')
  })

  it.each([
    [418, 'banned'],
    [451, 'region-blocked'],
    [500, 'http'],
  ])('maps HTTP %d to %s', async (status, kind) => {
    const fetchFn = fakeFetch(async () => new Response('', { status }))
    const err = await new BinanceProvider({ fetchFn }).getTickers(['BTCUSDT']).catch((e) => e)
    expect((err as MarketDataError).kind).toBe(kind)
  })

  it('maps a rejected fetch to network and a caller abort to aborted', async () => {
    const down = fakeFetch(async () => {
      throw new TypeError('Failed to fetch')
    })
    const e1 = await new BinanceProvider({ fetchFn: down }).getTickers(['BTCUSDT']).catch((e) => e)
    expect((e1 as MarketDataError).kind).toBe('network')

    const hang = vi.fn(
      (_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(init.signal?.reason))
        }),
    ) as unknown as typeof fetch
    const controller = new AbortController()
    const p = new BinanceProvider({ fetchFn: hang }).getTickers(['BTCUSDT'], {
      signal: controller.signal,
    })
    controller.abort()
    const e2 = await p.catch((e: unknown) => e)
    expect((e2 as MarketDataError).kind).toBe('aborted')
  })
})

// ---------- subscribeTickers ----------

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
  serverClose(): void {
    this.readyState = 3
    this.onclose?.({ code: 1006 } as CloseEvent)
  }
}

const WS = FakeWebSocket as unknown as typeof WebSocket
const lastWs = () => FakeWebSocket.instances[FakeWebSocket.instances.length - 1]

describe('BinanceProvider.subscribeTickers', () => {
  let win: EventTarget
  let nav: { onLine: boolean }

  beforeEach(() => {
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

  function setup(staleTimeoutMs = 60_000) {
    const provider = new BinanceProvider({
      wsBaseUrl: 'wss://example.test:9443/',
      WebSocketImpl: WS,
      staleTimeoutMs,
    })
    const pushes: Ticker[][] = []
    const statuses: ConnectionStatus[] = []
    const unsubscribe = provider.subscribeTickers((t) => pushes.push(t), {
      onStatus: (s) => statuses.push(s),
    })
    return { pushes, statuses, unsubscribe }
  }

  it('opens exactly one socket on !miniTicker@arr (vision stream host by default)', () => {
    const un = new BinanceProvider({ WebSocketImpl: WS }).subscribeTickers(() => {})
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(lastWs().url).toBe('wss://data-stream.binance.vision/ws/!miniTicker@arr')
    un()
  })

  it('delivers parsed tickers per message, skips malformed elements and empty pushes', () => {
    const { pushes, statuses } = setup()
    expect(lastWs().url).toBe('wss://example.test:9443/ws/!miniTicker@arr')
    lastWs().serverOpen()
    expect(statuses).toEqual(['connecting', 'live'])
    lastWs().serverMessage([mini('BTCUSDT', '110', '100'), { e: '24hrMiniTicker' }])
    lastWs().serverMessage('garbage')
    lastWs().serverMessage([{ e: 'x' }])
    lastWs().serverMessage([mini('ETHUSDT', '95', '100'), mini('SOLUSDT', '1', '0')])
    expect(pushes.map((p) => p.map((t) => [t.symbol, t.changePct]))).toEqual([
      [['BTCUSDT', 10]],
      [
        ['ETHUSDT', -5],
        ['SOLUSDT', 0],
      ],
    ])
  })

  it('does not throw out of onmessage when onTickers throws', () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    new BinanceProvider({ WebSocketImpl: WS }).subscribeTickers(() => {
      throw new Error('boom')
    })
    lastWs().serverOpen()
    expect(() => lastWs().serverMessage([mini('A', '1', '1')])).not.toThrow()
    expect(errSpy).toHaveBeenCalled()
  })

  it('reconnects with backoff after a drop and reports reconnecting -> live', () => {
    const { statuses } = setup()
    lastWs().serverOpen()
    lastWs().serverMessage([mini('A', '1', '1')])
    lastWs().serverClose()
    expect(statuses).toEqual(['connecting', 'live', 'reconnecting'])
    vi.advanceTimersByTime(999)
    expect(FakeWebSocket.instances).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(FakeWebSocket.instances).toHaveLength(2)
    expect(lastWs().url).toBe('wss://example.test:9443/ws/!miniTicker@arr')
    lastWs().serverOpen()
    expect(statuses[statuses.length - 1]).toBe('live')
  })

  it('reconnects when the stream goes stale', () => {
    const { statuses } = setup(5_000)
    lastWs().serverOpen()
    vi.advanceTimersByTime(5_000)
    expect(FakeWebSocket.instances[0].closeCalls).toBe(1)
    // Opened and healthy, only idle: the stale reconnect is silent.
    expect(statuses).toEqual(['connecting', 'live'])
    vi.advanceTimersByTime(1_000)
    expect(FakeWebSocket.instances).toHaveLength(2)
  })

  it('an idle stale reconnect keeps status live and resumes pushes on the new socket', () => {
    const { statuses, pushes } = setup(5_000)
    lastWs().serverOpen()
    vi.advanceTimersByTime(6_000) // idle stale + silent reconnect
    lastWs().serverOpen()
    lastWs().serverMessage([mini('BTCUSDT', '110', '100')])
    expect(statuses).toEqual(['connecting', 'live'])
    expect(pushes).toHaveLength(1)
  })

  it('reports offline after 5 consecutive failures', () => {
    const { statuses } = setup()
    for (let i = 0; i < 5; i++) {
      lastWs().serverClose()
      vi.advanceTimersByTime(30_000)
    }
    expect(statuses).toContain('offline')
  })

  it('goes offline on window offline and reconnects on online', () => {
    const { statuses } = setup()
    lastWs().serverOpen()
    nav.onLine = false
    win.dispatchEvent(new Event('offline'))
    expect(statuses[statuses.length - 1]).toBe('offline')
    expect(FakeWebSocket.instances[0].closeCalls).toBe(1)
    nav.onLine = true
    win.dispatchEvent(new Event('online'))
    expect(FakeWebSocket.instances).toHaveLength(2)
  })

  it('unsubscribe is idempotent, closes the socket, and suppresses all callbacks', () => {
    const { pushes, statuses, unsubscribe } = setup()
    const s = lastWs()
    s.serverOpen()
    unsubscribe()
    expect(s.closeCalls).toBe(1)
    s.serverMessage([mini('A', '1', '1')])
    s.serverClose()
    win.dispatchEvent(new Event('online'))
    vi.advanceTimersByTime(600_000)
    expect(pushes).toEqual([])
    expect(statuses).toEqual(['connecting', 'live'])
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(vi.getTimerCount()).toBe(0)
    unsubscribe()
    expect(s.closeCalls).toBe(1)
  })

  it('throws when no WebSocket implementation exists', () => {
    vi.stubGlobal('WebSocket', undefined)
    expect(() => new BinanceProvider().subscribeTickers(() => {})).toThrow(/WebSocket/)
  })
})
