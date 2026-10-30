import type { FastifyInstance } from 'fastify'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { buildApp } from '../src/app.ts'
import { openDb, type Db } from '../src/db/index.ts'
import {
  MAX_CLIENT_MESSAGE_BYTES,
  MAX_WATCH_SYMBOLS,
  parseWatchMessage,
  serializeWatched,
} from '../src/routes/stream.ts'
import type { Ticker } from '../src/types.ts'

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
}

interface TestClient {
  readyState: number
  send(data: string): void
  close(): void
  on(event: 'message', cb: (data: Buffer) => void): void
  on(event: 'close', cb: (code: number, reason: Buffer) => void): void
}

interface Connected {
  ws: TestClient
  /** Parsed messages, in order. */
  messages: unknown[]
  /** Bytes of every `tickers` frame received. */
  tickerBytes: () => number
  closed: Promise<number>
}

const SYMBOLS = Array.from({ length: 200 }, (_, i) => `SYM${i}USDT`)

/** One whole-market push: every symbol in SYMBOLS (plus BTCUSDT/ETHUSDT) changed. */
function marketPush(round: number): unknown[] {
  return ['BTCUSDT', 'ETHUSDT', ...SYMBOLS].map((s, i) => ({
    e: '24hrMiniTicker',
    E: 1_700_000_000_000 + round * 1000,
    s,
    c: String(100 + i + round / 10),
    o: '100',
    h: String(200 + i),
    l: '50',
    v: '1234.5',
    q: '123450.5',
  }))
}

const tick = async (): Promise<void> => {
  for (let i = 0; i < 5; i++) await new Promise<void>((r) => setImmediate(r))
}

const tickersOf = (messages: unknown[]): Ticker[][] =>
  messages
    .filter((m): m is { type: 'tickers'; tickers: Ticker[] } => {
      return typeof m === 'object' && m !== null && (m as { type?: unknown }).type === 'tickers'
    })
    .map((m) => m.tickers)

describe('/ws/tickers watch filter', () => {
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
    await app.close()
    db.close()
  })

  async function connect(): Promise<Connected> {
    const messages: unknown[] = []
    let bytes = 0
    let resolveClosed!: (code: number) => void
    const closed = new Promise<number>((r) => (resolveClosed = r))
    const ws = (await app.injectWS(
      '/ws/tickers',
      { headers: { host: 'localhost:80' } },
      {
        onInit: (raw) => {
          const c = raw as unknown as TestClient
          c.on('message', (data) => {
            const msg = JSON.parse(data.toString()) as { type?: unknown }
            if (msg.type === 'tickers') bytes += data.length
            messages.push(msg)
          })
          c.on('close', (code) => resolveClosed(code))
        },
      },
    )) as unknown as TestClient
    return { ws, messages, tickerBytes: () => bytes, closed }
  }

  it('a client that never sends watch gets the full array (backward compatible)', async () => {
    const c = await connect()
    FakeUpstream.instances[0]!.emit(marketPush(0))
    await tick()
    const pushes = tickersOf(c.messages)
    expect(pushes).toHaveLength(1)
    expect(pushes[0]!.map((t) => t.symbol)).toEqual(['BTCUSDT', 'ETHUSDT', ...SYMBOLS])
    c.ws.close()
  })

  it('a watching client gets only its symbols, with the same values as the full array', async () => {
    const all = await connect()
    const watching = await connect()
    watching.ws.send(JSON.stringify({ type: 'watch', symbols: ['ETHUSDT', 'BTCUSDT'] }))
    await tick()
    FakeUpstream.instances[0]!.emit(marketPush(1))
    await tick()
    const full = tickersOf(all.messages)[0]!
    const narrowed = tickersOf(watching.messages)
    expect(narrowed).toHaveLength(1)
    // Upstream order is kept and every field is identical to the unfiltered message.
    expect(narrowed[0]).toEqual(
      full.filter((t) => t.symbol === 'BTCUSDT' || t.symbol === 'ETHUSDT'),
    )
    expect(narrowed[0]!.map((t) => t.symbol)).toEqual(['BTCUSDT', 'ETHUSDT'])
    // Status events still reach the watching client.
    expect(watching.messages.some((m) => (m as { type?: unknown }).type === 'status')).toBe(true)
    all.ws.close()
    watching.ws.close()
  })

  it('bytes sent to a watching client drop by about the watched-to-total ratio', async () => {
    const all = await connect()
    const watching = await connect()
    watching.ws.send(JSON.stringify({ type: 'watch', symbols: ['BTCUSDT', 'ETHUSDT'] }))
    await tick()
    for (let round = 0; round < 10; round++) FakeUpstream.instances[0]!.emit(marketPush(round))
    await tick()
    const total = SYMBOLS.length + 2
    const fullBytes = all.tickerBytes()
    const watchedBytes = watching.tickerBytes()
    expect(tickersOf(all.messages)).toHaveLength(10)
    expect(tickersOf(watching.messages)).toHaveLength(10)
    // 2 of 202 symbols: the frames shrink to ~1% (plus the fixed envelope per frame).
    expect(watchedBytes).toBeLessThan((fullBytes * 2) / total + 10 * 40)
    expect(watchedBytes * 50).toBeLessThan(fullBytes)
    console.log(
      `/ws/tickers bytes over 10 pushes: full ${fullBytes}, watching 2/${total}: ${watchedBytes} (${((watchedBytes / fullBytes) * 100).toFixed(2)}%)`,
    )
    all.ws.close()
    watching.ws.close()
  })

  it('a push with none of the watched symbols sends no frame', async () => {
    const c = await connect()
    c.ws.send(JSON.stringify({ type: 'watch', symbols: ['NOPEUSDT'] }))
    await tick()
    FakeUpstream.instances[0]!.emit(marketPush(0))
    await tick()
    expect(tickersOf(c.messages)).toEqual([])
    c.ws.close()
  })

  it('a new watch replaces the set and unwatch restores the full array', async () => {
    const c = await connect()
    const up = (): FakeUpstream => FakeUpstream.instances[0]!
    c.ws.send(JSON.stringify({ type: 'watch', symbols: ['BTCUSDT'] }))
    await tick()
    up().emit(marketPush(0))
    await tick()
    c.ws.send(JSON.stringify({ type: 'watch', symbols: ['SYM5USDT', 'ETHUSDT'] }))
    await tick()
    up().emit(marketPush(1))
    await tick()
    c.ws.send(JSON.stringify({ type: 'unwatch' }))
    await tick()
    up().emit(marketPush(2))
    await tick()
    const pushes = tickersOf(c.messages).map((p) => p.map((t) => t.symbol))
    expect(pushes).toEqual([
      ['BTCUSDT'],
      ['ETHUSDT', 'SYM5USDT'],
      ['BTCUSDT', 'ETHUSDT', ...SYMBOLS],
    ])
    c.ws.close()
  })

  it('invalid client messages are ignored and never close the socket', async () => {
    const c = await connect()
    c.ws.send(JSON.stringify({ type: 'watch', symbols: ['BTCUSDT'] }))
    await tick()
    for (const junk of [
      'not json',
      '[]',
      'null',
      '{"method":"SUBSCRIBE"}',
      JSON.stringify({ type: 'watch', symbols: 'BTCUSDT' }),
      JSON.stringify({ type: 'watch', symbols: ['ETHUSDT', 5] }),
      JSON.stringify({ type: 'watch', symbols: Array.from({ length: 501 }, (_, i) => `A${i}`) }),
      JSON.stringify({ type: 'watch', symbols: ['X'.repeat(MAX_CLIENT_MESSAGE_BYTES)] }),
    ]) {
      c.ws.send(junk)
    }
    await tick()
    FakeUpstream.instances[0]!.emit(marketPush(0))
    await tick()
    expect(c.ws.readyState).toBe(1)
    // The last valid watch still applies.
    expect(tickersOf(c.messages).map((p) => p.map((t) => t.symbol))).toEqual([['BTCUSDT']])
    c.ws.close()
  })

  it('kline clients ignore watch messages', async () => {
    const messages: unknown[] = []
    const ws = (await app.injectWS(
      '/ws/klines?symbol=BTCUSDT&interval=1m',
      { headers: { host: 'localhost:80' } },
      {
        onInit: (raw) => {
          ;(raw as unknown as TestClient).on('message', (d) =>
            messages.push(JSON.parse(d.toString())),
          )
        },
      },
    )) as unknown as TestClient
    ws.send(JSON.stringify({ type: 'watch', symbols: ['ETHUSDT'] }))
    await tick()
    FakeUpstream.instances[0]!.emit({
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
        x: false,
      },
    })
    await tick()
    expect(messages.some((m) => (m as { type?: unknown }).type === 'kline')).toBe(true)
    ws.close()
  })
})

describe('parseWatchMessage', () => {
  it('accepts a watch list and builds a canonical key', () => {
    const w = parseWatchMessage(Buffer.from('{"type":"watch","symbols":["ETHUSDT","BTCUSDT"]}'))
    expect(w).not.toBeNull()
    expect(w).toBeDefined()
    expect([...w!.symbols]).toEqual(['ETHUSDT', 'BTCUSDT'])
    expect(w!.key).toBe('BTCUSDT,ETHUSDT')
    expect(parseWatchMessage('{"type":"watch","symbols":[]}')).toEqual({
      symbols: new Set(),
      key: '',
    })
  })

  it('skips strings that are not symbols and maps unwatch to null', () => {
    const w = parseWatchMessage('{"type":"watch","symbols":["btcusdt","B","BTC-USD","ETHUSDT"]}')
    expect(w?.key).toBe('ETHUSDT')
    expect(parseWatchMessage('{"type":"unwatch"}')).toBeNull()
  })

  it('rejects anything else', () => {
    expect(parseWatchMessage(undefined)).toBeUndefined()
    expect(parseWatchMessage(42)).toBeUndefined()
    expect(parseWatchMessage('{')).toBeUndefined()
    expect(parseWatchMessage('{"type":"watch"}')).toBeUndefined()
    expect(parseWatchMessage('{"type":"watch","symbols":[null]}')).toBeUndefined()
    const tooMany = {
      type: 'watch',
      symbols: Array.from({ length: MAX_WATCH_SYMBOLS + 1 }, (_, i) => `AB${i}`),
    }
    expect(parseWatchMessage(JSON.stringify(tooMany))).toBeUndefined()
    const max = {
      type: 'watch',
      symbols: Array.from({ length: MAX_WATCH_SYMBOLS }, (_, i) => `AB${i}`),
    }
    expect(parseWatchMessage(JSON.stringify(max))?.symbols.size).toBe(MAX_WATCH_SYMBOLS)
    expect(parseWatchMessage(Buffer.alloc(MAX_CLIENT_MESSAGE_BYTES + 1, 32))).toBeUndefined()
  })
})

describe('serializeWatched', () => {
  const t = (symbol: string): Ticker => ({
    symbol,
    last: 1,
    open: 1,
    high: 1,
    low: 1,
    volume: 1,
    quoteVolume: 1,
    changePct: 0,
    time: 1,
  })

  it('serializes once per (event, distinct set)', () => {
    const event = { type: 'tickers' as const, tickers: [t('A1'), t('B1'), t('C1')] }
    const w1 = parseWatchMessage('{"type":"watch","symbols":["C1","A1"]}')!
    const w2 = parseWatchMessage('{"type":"watch","symbols":["A1","C1"]}')!
    const a = serializeWatched(event, w1)
    expect(a).toBe(JSON.stringify({ type: 'tickers', tickers: [t('A1'), t('C1')] }))
    // Same set, other client: the cached string (same reference) is reused.
    expect(serializeWatched(event, w2)).toBe(a)
    expect(
      serializeWatched(event, parseWatchMessage('{"type":"watch","symbols":["Z9"]}')!),
    ).toBeNull()
  })
})
