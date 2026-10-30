// Phase 19: the ticker stream is narrowed to the watched set before Ticker objects are built
// (Binance direct: raw `s`; local server: a `watch` message on /ws/tickers).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Ticker } from '../types'
import { BinanceProvider } from './binance'
import { parseMiniTickerMessage } from './binanceTicker'
import { MAX_WATCH_SYMBOLS, parseStreamMessage, ServerProvider, watchMessage } from './server'

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
const lastWs = () => FakeWebSocket.instances[FakeWebSocket.instances.length - 1]

const mini = (s: string, c = '2', E = 1_700_000_000_000) => ({
  e: '24hrMiniTicker',
  E,
  s,
  c,
  o: '1',
  h: '3',
  l: '0.5',
  v: '10',
  q: '20',
})

const serverTicker = (symbol: string, last = 2): Ticker => ({
  symbol,
  last,
  open: 1,
  high: 3,
  low: 0.5,
  volume: 10,
  quoteVolume: 20,
  changePct: (last - 1) * 100,
  time: 1_700_000_000,
})

beforeEach(() => {
  FakeWebSocket.instances = []
  vi.useFakeTimers()
  vi.spyOn(Math, 'random').mockReturnValue(1)
  vi.stubGlobal('window', new EventTarget())
  vi.stubGlobal('navigator', { onLine: true })
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('parseMiniTickerMessage with a watched set', () => {
  const msg = [mini('BTCUSDT'), mini('ETHUSDT', '3'), mini('XRPUSDT'), { e: 'junk', s: 'BTCUSDT' }]

  it('keeps exactly the watched entries the unfiltered parse would produce', () => {
    const only = new Set(['ETHUSDT', 'BTCUSDT', 'NOTLISTED'])
    const all = parseMiniTickerMessage(msg)
    expect(parseMiniTickerMessage(msg, only)).toEqual(all.filter((t) => only.has(t.symbol)))
    expect(parseMiniTickerMessage(JSON.stringify(msg), only).map((t) => t.symbol)).toEqual([
      'BTCUSDT',
      'ETHUSDT',
    ])
  })

  it('matches the raw symbol exactly and skips non-objects', () => {
    expect(parseMiniTickerMessage([mini('btcusdt'), 5, null], new Set(['BTCUSDT']))).toEqual([])
    expect(parseMiniTickerMessage(msg, new Set())).toEqual([])
  })

  it('without a set, parses everything as before', () => {
    expect(parseMiniTickerMessage(msg).map((t) => t.symbol)).toEqual([
      'BTCUSDT',
      'ETHUSDT',
      'XRPUSDT',
    ])
  })
})

describe('BinanceProvider.subscribeTickers with symbols', () => {
  it('drops unwatched symbols per push, reading the set at each push on one socket', () => {
    const provider = new BinanceProvider({ wsBaseUrl: 'wss://example.test', WebSocketImpl: WS })
    let watched: ReadonlySet<string> = new Set(['BTCUSDT'])
    const pushes: string[][] = []
    const un = provider.subscribeTickers((t) => pushes.push(t.map((x) => x.symbol)), {
      symbols: () => watched,
    })
    lastWs().serverOpen()
    lastWs().serverMessage([mini('BTCUSDT'), mini('ETHUSDT')])
    // Nothing watched changed: no call at all.
    lastWs().serverMessage([mini('ETHUSDT'), mini('XRPUSDT')])
    watched = new Set(['ETHUSDT', 'XRPUSDT'])
    lastWs().serverMessage([mini('BTCUSDT'), mini('ETHUSDT'), mini('XRPUSDT')])
    expect(pushes).toEqual([['BTCUSDT'], ['ETHUSDT', 'XRPUSDT']])
    expect(FakeWebSocket.instances).toHaveLength(1)
    un()
  })

  it('without symbols, every symbol is delivered (old behaviour)', () => {
    const provider = new BinanceProvider({ wsBaseUrl: 'wss://example.test', WebSocketImpl: WS })
    const pushes: string[][] = []
    const un = provider.subscribeTickers((t) => pushes.push(t.map((x) => x.symbol)))
    lastWs().serverOpen()
    lastWs().serverMessage([mini('BTCUSDT'), mini('ETHUSDT')])
    expect(pushes).toEqual([['BTCUSDT', 'ETHUSDT']])
    un()
  })
})

describe('watchMessage', () => {
  it('sends the sorted set', () => {
    expect(JSON.parse(watchMessage(new Set(['ETHUSDT', 'BTCUSDT'])))).toEqual({
      type: 'watch',
      symbols: ['BTCUSDT', 'ETHUSDT'],
    })
    expect(JSON.parse(watchMessage(new Set()))).toEqual({ type: 'watch', symbols: [] })
  })

  it('falls back to unwatch when the server could not honour the set', () => {
    const big = new Set(Array.from({ length: MAX_WATCH_SYMBOLS + 1 }, (_, i) => `AB${i}`))
    expect(JSON.parse(watchMessage(big))).toEqual({ type: 'unwatch' })
    expect(JSON.parse(watchMessage(new Set(['BTCUSDT', 'BTC-USD'])))).toEqual({ type: 'unwatch' })
    const max = new Set(Array.from({ length: MAX_WATCH_SYMBOLS }, (_, i) => `AB${i}`))
    expect(JSON.parse(watchMessage(max)).symbols).toHaveLength(MAX_WATCH_SYMBOLS)
  })
})

describe('parseStreamMessage with a watched set', () => {
  it('skips unwatched ticker entries and keeps the rest unchanged', () => {
    const msg = { type: 'tickers', tickers: [serverTicker('A1'), serverTicker('B1', 3)] }
    expect(parseStreamMessage(JSON.stringify(msg), new Set(['B1']))).toEqual({
      type: 'tickers',
      tickers: [serverTicker('B1', 3)],
    })
    expect(parseStreamMessage(JSON.stringify(msg))).toEqual(msg)
  })
})

describe('ServerProvider.subscribeTickers with symbols', () => {
  const provider = () => new ServerProvider({ baseUrl: 'http://127.0.0.1:8787', WebSocketImpl: WS })
  const status = { type: 'status', status: 'live' }

  it('sends watch on open, again on each change, and on every new socket', async () => {
    let watched: ReadonlySet<string> = new Set(['ETHUSDT', 'BTCUSDT'])
    const listeners = new Set<() => void>()
    const got: string[][] = []
    const un = provider().subscribeTickers((t) => got.push(t.map((x) => x.symbol)), {
      symbols: () => watched,
      onSymbolsChange: (l) => {
        listeners.add(l)
        return () => listeners.delete(l)
      },
    })
    expect(lastWs().url).toBe('ws://127.0.0.1:8787/ws/tickers')
    expect(lastWs().sent).toEqual([])
    lastWs().serverOpen()
    expect(lastWs().sent.map((m) => JSON.parse(m) as unknown)).toEqual([
      { type: 'watch', symbols: ['BTCUSDT', 'ETHUSDT'] },
    ])
    lastWs().serverMessage(status)
    // Same set: nothing re-sent.
    for (const l of listeners) l()
    expect(lastWs().sent).toHaveLength(1)
    watched = new Set(['SOLUSDT'])
    for (const l of listeners) l()
    expect(JSON.parse(lastWs().sent[1])).toEqual({ type: 'watch', symbols: ['SOLUSDT'] })

    // A push that still carries other symbols (in flight before the server saw the watch).
    lastWs().serverMessage({
      type: 'tickers',
      tickers: [serverTicker('BTCUSDT'), serverTicker('SOLUSDT')],
    })
    expect(got).toEqual([['SOLUSDT']])

    // Reconnect: the new socket sends the current set as soon as it opens.
    lastWs().serverClose()
    await vi.advanceTimersByTimeAsync(60_000)
    expect(FakeWebSocket.instances).toHaveLength(2)
    lastWs().serverOpen()
    expect(lastWs().sent.map((m) => JSON.parse(m) as unknown)).toEqual([
      { type: 'watch', symbols: ['SOLUSDT'] },
    ])
    un()
    expect(listeners.size).toBe(0)
  })

  it('without onSymbolsChange, a changed set is re-sent on the next push', () => {
    let watched: ReadonlySet<string> = new Set(['BTCUSDT'])
    const un = provider().subscribeTickers(() => {}, { symbols: () => watched })
    lastWs().serverOpen()
    watched = new Set(['ETHUSDT'])
    expect(lastWs().sent).toHaveLength(1)
    lastWs().serverMessage({ type: 'tickers', tickers: [serverTicker('BTCUSDT')] })
    expect(JSON.parse(lastWs().sent[1])).toEqual({ type: 'watch', symbols: ['ETHUSDT'] })
    un()
  })

  it('without symbols, sends nothing and delivers every symbol (old behaviour)', () => {
    const got: string[][] = []
    const un = provider().subscribeTickers((t) => got.push(t.map((x) => x.symbol)))
    lastWs().serverOpen()
    lastWs().serverMessage({ type: 'tickers', tickers: [serverTicker('A1'), serverTicker('B1')] })
    expect(lastWs().sent).toEqual([])
    expect(got).toEqual([['A1', 'B1']])
    un()
  })

  it('sends nothing after unsubscribe', () => {
    let watched: ReadonlySet<string> = new Set(['BTCUSDT'])
    let listener: (() => void) | null = null
    const un = provider().subscribeTickers(() => {}, {
      symbols: () => watched,
      onSymbolsChange: (l) => {
        listener = l
        return () => {}
      },
    })
    const sock = lastWs()
    sock.serverOpen()
    un()
    sock.readyState = 1
    watched = new Set(['ETHUSDT'])
    ;(listener as (() => void) | null)?.()
    expect(sock.sent).toHaveLength(1)
  })
})

// Timing: filtered vs unfiltered parse of a whole-market push. Run with
//   PERF=1 npx vitest run src/data/tickerWatch.test.ts
describe.skipIf(!import.meta.env.PERF)('ticker filter timing', () => {
  const timeUs = (fn: () => void, iters: number, rounds = 7): number => {
    for (let i = 0; i < iters; i++) fn()
    const samples: number[] = []
    for (let r = 0; r < rounds; r++) {
      const t0 = performance.now()
      for (let i = 0; i < iters; i++) fn()
      samples.push(((performance.now() - t0) * 1000) / iters)
    }
    samples.sort((a, b) => a - b)
    return samples[Math.floor(samples.length / 2)]
  }

  it('parseMiniTickerMessage, 2000 symbols, unfiltered vs 10 watched', () => {
    vi.useRealTimers() // fake timers also fake performance.now
    const msg = Array.from({ length: 2_000 }, (_, i) => ({
      e: '24hrMiniTicker',
      E: 1_700_000_000_000,
      s: `SYM${i}USDT`,
      c: '1.2345',
      o: '1.2000',
      h: '1.3000',
      l: '1.1000',
      v: '123456.7',
      q: '150000.1',
    }))
    const watched = new Set(Array.from({ length: 10 }, (_, i) => `SYM${i * 150}USDT`))
    const text = JSON.stringify(msg)
    const rows = [
      `parsed array, unfiltered: ${timeUs(() => parseMiniTickerMessage(msg), 200).toFixed(1)} us`,
      `parsed array, 10 watched: ${timeUs(() => parseMiniTickerMessage(msg, watched), 200).toFixed(1)} us`,
      `JSON text, unfiltered: ${timeUs(() => parseMiniTickerMessage(text), 100).toFixed(1)} us`,
      `JSON text, 10 watched: ${timeUs(() => parseMiniTickerMessage(text, watched), 100).toFixed(1)} us`,
    ]
    console.log(rows.join('\n'))
    expect(parseMiniTickerMessage(msg, watched)).toHaveLength(10)
  })
})
