import { describe, expect, it } from 'vitest'
import { BinanceProvider } from './binance'
import { CoinbaseProvider } from './coinbase'
import { createDrawingsClient } from './drawingsApi'
import { createAlertsClient } from './alertsApi'
import { createLabClient } from './labApi'
import { createPersistenceClient } from './persistence'
import { createResearchClient } from './researchApi'
import { ServerProvider } from './server'
import type { Drawing } from '../chart/drawings/types'

/*
 * Characterisation of every HTTP caller in web/src/data (Phase 19 wave 5, "no behaviour
 * change"). The snapshot was recorded against the pre-refactor copies of the request helpers
 * (binance.ts, coinbase.ts, server.ts, persistence.ts, drawingsApi.ts, labApi.ts,
 * researchApi.ts) and pins, per caller and per server answer: the exact requests (URL, method,
 * headers, body, keepalive, cache, signal presence) and the exact outcome (resolved value, or
 * the error's class, kind, message, status, retryAfter and plan errors).
 */

const TIMEOUT_MS = 40

type Respond = (
  url: string,
  signal: AbortSignal | undefined,
  ctl: AbortController,
) => Promise<Response>

interface Scenario {
  name: string
  respond: Respond
  /** Abort the caller's signal before the call. */
  preAbort?: boolean
  /** Abort the caller's signal 5 ms into the call. */
  midAbort?: boolean
}

function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  })
}

function text(body: string | null, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(body, { status, headers })
}

/** Like a real fetch: never settles until its signal aborts, then rejects with AbortError. */
function hang(signal: AbortSignal | undefined): Promise<Response> {
  return new Promise((_resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('The operation was aborted.', 'AbortError'))
      return
    }
    signal?.addEventListener('abort', () =>
      reject(new DOMException('The operation was aborted.', 'AbortError')),
    )
  })
}

/** A 2xx response whose body read aborts the caller first and then fails. */
function bodyFailsAfterAbort(ctl: AbortController): Response {
  const fail = () => {
    ctl.abort()
    return Promise.reject(new DOMException('The operation was aborted.', 'AbortError'))
  }
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    headers: new Headers({ 'Content-Type': 'application/json' }),
    json: fail,
    text: fail,
  } as unknown as Response
}

/** A 2xx response whose body read fails (connection cut), without any abort. */
function bodyFails(): Response {
  const fail = () => Promise.reject(new TypeError('network error'))
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    headers: new Headers(),
    json: fail,
    text: fail,
  } as unknown as Response
}

const ERROR_SCENARIOS: Scenario[] = [
  {
    name: '429 Retry-After 7, empty JSON body',
    respond: async () => json({}, 429, { 'Retry-After': '7' }),
  },
  {
    name: '429 Retry-After "soon", text body',
    respond: async () => text('slow down', 429, { 'Retry-After': 'soon' }),
  },
  { name: '429 Retry-After "  "', respond: async () => text(null, 429, { 'Retry-After': '  ' }) },
  { name: '418 Retry-After 120', respond: async () => text(null, 418, { 'Retry-After': '120' }) },
  { name: '451', respond: async () => text(null, 451, { 'Retry-After': '5' }) },
  {
    name: '400 code -1121',
    respond: async () => json({ code: -1121, msg: 'Invalid symbol.' }, 400),
  },
  {
    name: '400 code -1100',
    respond: async () => json({ code: -1100, msg: 'Illegal characters' }, 400),
  },
  { name: '400 msg empty', respond: async () => json({ code: -1, msg: '' }, 400) },
  {
    name: '404 message + detail',
    respond: async () => json({ message: 'NotFound', detail: 'Run not found' }, 404),
  },
  { name: '404 no body', respond: async () => text(null, 404) },
  {
    name: '422 plan errors',
    respond: async () =>
      json(
        {
          detail: 'Unprocessable',
          errors: [
            { path: 'rules.long_entry', message: 'unknown feature' },
            { path: '', message: 'second' },
          ],
        },
        422,
      ),
  },
  { name: '422 detail only', respond: async () => json({ detail: 'Unprocessable' }, 422) },
  { name: '502 text', respond: async () => text('Bad Gateway', 502) },
  { name: '504 no body', respond: async () => text(null, 504) },
  {
    name: '503 detail',
    respond: async () => json({ detail: '  No candles; run lab download  ' }, 503),
  },
  { name: '503 empty JSON', respond: async () => json({}, 503) },
  {
    name: '500 server error body + Retry-After header',
    respond: async () =>
      json(
        { error: 'rate-limited', message: 'Upstream busy', retryAfter: 3, detail: ' Detail ' },
        500,
        { 'Retry-After': '9' },
      ),
  },
  {
    name: '500 server error body, negative retryAfter, header 4',
    respond: async () =>
      json({ error: 'banned', message: 'Banned upstream', retryAfter: -1 }, 500, {
        'Retry-After': '4',
      }),
  },
  {
    name: '500 untrusted kind "aborted", empty message',
    respond: async () => json({ error: 'aborted', message: '' }, 500),
  },
  { name: '500 JSON array body', respond: async () => json([1, 2], 500) },
  { name: '500 text body', respond: async () => text('oops', 500) },
  { name: '200 invalid JSON', respond: async () => text('not json', 200) },
  { name: '200 empty body', respond: async () => text('', 200) },
  { name: '200 whitespace body', respond: async () => text('  \n', 200) },
  { name: '204 no content', respond: async () => text(null, 204) },
  { name: '200 wrong shape', respond: async () => json({ unexpected: true }) },
  { name: '200 null', respond: async () => json(null) },
  {
    name: 'fetch rejects TypeError',
    respond: async () => {
      throw new TypeError('Failed to fetch')
    },
  },
  {
    name: 'fetch rejects a string',
    respond: () => Promise.reject('plain string failure' as unknown as Error),
  },
  { name: 'timeout', respond: (_url, signal) => hang(signal) },
  { name: 'caller aborts mid-request', respond: (_url, signal) => hang(signal), midAbort: true },
  { name: 'caller aborted before the call', respond: async () => json({}), preAbort: true },
  {
    name: 'body read fails after caller abort',
    respond: async (_u, _s, ctl) => bodyFailsAfterAbort(ctl),
  },
  { name: 'body read fails', respond: async () => bodyFails() },
]

interface RecordedCall {
  url: string
  init: Record<string, unknown> | undefined
}

function describeInit(init: RequestInit | undefined): Record<string, unknown> | undefined {
  if (init === undefined) return undefined
  const out: Record<string, unknown> = {}
  for (const key of Object.keys(init).sort()) {
    const v = (init as Record<string, unknown>)[key]
    out[key] = key === 'signal' ? (v instanceof AbortSignal ? '<AbortSignal>' : v) : v
  }
  return out
}

function describeError(e: unknown): Record<string, unknown> {
  if (!(e instanceof Error)) return { thrown: String(e) }
  const out: Record<string, unknown> = {
    class: e.constructor.name,
    name: e.name,
    message: e.message,
  }
  for (const key of ['kind', 'status', 'retryAfter', 'errors', 'pause']) {
    if (key in e) out[key] = (e as unknown as Record<string, unknown>)[key]
  }
  return out
}

/**
 * Run `call` once per scenario. `route` may answer some URLs itself (e.g. a catalog the
 * request under test depends on); everything else gets the scenario's response.
 */
async function characterise(
  call: (fetchImpl: typeof fetch, signal: AbortSignal | undefined) => Promise<unknown>,
  route?: (url: string) => Response | null,
  scenarios: Scenario[] = ERROR_SCENARIOS,
): Promise<Record<string, string>> {
  const out: Record<string, string> = {}
  for (const s of scenarios) {
    const ctl = new AbortController()
    const calls: RecordedCall[] = []
    const fetchImpl = ((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      calls.push({ url, init: describeInit(init) })
      const routed = route?.(url) ?? null
      if (routed !== null) return Promise.resolve(routed)
      return s.respond(url, init?.signal ?? undefined, ctl)
    }) as typeof fetch
    if (s.preAbort) ctl.abort()
    if (s.midAbort) setTimeout(() => ctl.abort(), 5)
    let result: unknown
    try {
      result = { resolved: await call(fetchImpl, ctl.signal) }
    } catch (e) {
      result = { rejected: describeError(e) }
    }
    out[s.name] = JSON.stringify({ calls, result }, (_k, v: unknown) =>
      v === undefined ? '<undefined>' : v,
    )
  }
  return out
}

// ---------------------------------------------------------------------------------------------
// Market data providers
// ---------------------------------------------------------------------------------------------

const KLINE = [1700000000000, '1', '2', '0.5', '1.5', '10', 1700003599999]

describe('BinanceProvider requests (characterisation)', () => {
  const make = (fetchFn: typeof fetch) =>
    new BinanceProvider({ fetchFn, timeoutMs: TIMEOUT_MS, baseUrl: 'https://bn.test/' })

  it('getCandles', async () => {
    const r = await characterise((f, signal) =>
      make(f).getCandles(' btcusdt ', '1h', 5000, 1700000000.9, { signal }),
    )
    expect(r).toMatchSnapshot()
  })

  it('getCandles success (sort + dedupe)', async () => {
    const r = await characterise(
      (f, signal) => make(f).getCandles('BTCUSDT', '1m', Number.NaN, undefined, { signal }),
      () =>
        json([KLINE, [1699999940000, '1', '1', '1', '1', '1'], [...KLINE.slice(0, 4), '9', '9']]),
      [{ name: 'ok', respond: async () => json([]) }],
    )
    expect(r).toMatchSnapshot()
  })

  it('getSymbols', async () => {
    const r = await characterise((f, signal) => make(f).getSymbols({ signal }))
    expect(r).toMatchSnapshot()
  })

  it('getTickers (two symbols, falls back per symbol on -1121)', async () => {
    const r = await characterise((f, signal) =>
      make(f).getTickers(['btcusdt', 'ETHUSDT', 'BTCUSDT'], { signal }),
    )
    expect(r).toMatchSnapshot()
  })
})

const PRODUCTS = ['BTC', 'ETH', 'SOL', 'ADA', 'XRP'].map((b) => ({
  id: `${b}-USD`,
  base_currency: b,
  quote_currency: 'USD',
  quote_increment: '0.01',
  status: 'online',
}))
const NOW_MS = 1_700_000_000_000

describe('CoinbaseProvider requests (characterisation)', () => {
  const make = (fetchFn: typeof fetch) => {
    const p = new CoinbaseProvider({
      fetchFn,
      timeoutMs: TIMEOUT_MS,
      baseUrl: 'https://cb.test/',
      minRequestIntervalMs: 0,
      now: () => NOW_MS,
    })
    return p
  }
  /** Throttle pause left behind by the call (ms after the fake clock), via the private field. */
  const pauseOf = (p: CoinbaseProvider) =>
    (p as unknown as { nextRequestAt: number }).nextRequestAt - NOW_MS
  const catalog = (url: string) => (url.endsWith('/products') ? json(PRODUCTS) : null)

  it('getSymbols (/products, not product-scoped)', async () => {
    const r = await characterise(async (f, signal) => {
      const p = make(f)
      try {
        return await p.getSymbols({ signal })
      } catch (e) {
        throw Object.assign(e as Error, { pause: pauseOf(p) })
      }
    })
    expect(r).toMatchSnapshot()
  })

  it('getCandles (product-scoped) and the 429 throttle pause', async () => {
    const r = await characterise(async (f, signal) => {
      const p = make(f)
      try {
        return await p.getCandles(' btcusd ', '1h', 2, undefined, { signal })
      } catch (e) {
        throw Object.assign(e as Error, { pause: pauseOf(p) })
      }
    }, catalog)
    expect(r).toMatchSnapshot()
    // The pause after a 429 (Retry-After, else 1 s) is part of the pinned behaviour.
    const pauses: Record<string, number> = {}
    for (const s of ERROR_SCENARIOS.filter((x) => x.name.startsWith('429'))) {
      const f = ((input: RequestInfo | URL, init?: RequestInit) =>
        catalog(String(input)) !== null
          ? Promise.resolve(json(PRODUCTS))
          : s.respond(
              String(input),
              init?.signal ?? undefined,
              new AbortController(),
            )) as typeof fetch
      const p = make(f)
      await p.getCandles('BTCUSD', '1h', 2).catch(() => undefined)
      pauses[s.name] = pauseOf(p)
    }
    expect(pauses).toMatchSnapshot()
  })

  it('getTickers <= 4 symbols (product-scoped stats, 404 omitted)', async () => {
    const r = await characterise((f, signal) => make(f).getTickers(['BTCUSD'], { signal }), catalog)
    expect(r).toMatchSnapshot()
  })

  it('getTickers > 4 symbols (bulk /products/stats)', async () => {
    const r = await characterise(
      (f, signal) =>
        make(f).getTickers(['BTCUSD', 'ETHUSD', 'SOLUSD', 'ADAUSD', 'XRPUSD'], { signal }),
      catalog,
    )
    expect(r).toMatchSnapshot()
  })
})

describe('ServerProvider requests (characterisation)', () => {
  const make = (fetchImpl: typeof fetch) =>
    new ServerProvider({ fetchImpl, timeoutMs: TIMEOUT_MS, baseUrl: 'http://srv.test/' })

  it('getCandles (live page)', async () => {
    const r = await characterise((f, signal) =>
      make(f).getCandles('btcusdt', '1h', 0, undefined, { signal }),
    )
    expect(r).toMatchSnapshot()
  })

  it('getCandles (closed page, venue unknown then known)', async () => {
    const health = (url: string) =>
      url.includes('/api/health') ? json({ venue: { id: 'binance', name: 'Binance' } }) : null
    const r = await characterise(
      async (f, signal) => {
        const p = make(f)
        const first = await p.getCandles('BTCUSDT', '1h', 10, 1700000000.7, { signal })
        await new Promise((r) => setTimeout(r, 0))
        const second = await p.getCandles('BTCUSDT', '1h', 10, 1699990000, { signal })
        return { first, second }
      },
      health,
      [
        {
          name: 'ok',
          respond: async () =>
            json([
              { time: 2, open: 1, high: 1, low: 1, close: 1, volume: 1 },
              { time: 1, open: 1, high: 1, low: 1, close: 1, volume: 1 },
              { time: 2, open: 5, high: 5, low: 5, close: 5, volume: 5 },
            ]),
        },
      ],
    )
    expect(r).toMatchSnapshot()
  })

  it('getSymbols', async () => {
    const r = await characterise((f, signal) => make(f).getSymbols({ signal }))
    expect(r).toMatchSnapshot()
  })

  it('getTickers', async () => {
    const r = await characterise((f, signal) =>
      make(f).getTickers(['ethusdt', 'BTCUSDT'], { signal }),
    )
    expect(r).toMatchSnapshot()
  })

  it('getVenue', async () => {
    const r = await characterise((f, signal) => make(f).getVenue({ signal }))
    expect(r).toMatchSnapshot()
  })
})

// ---------------------------------------------------------------------------------------------
// Local server and lab clients
// ---------------------------------------------------------------------------------------------

const DRAWING: Drawing = {
  id: 'd1',
  symbol: 'BTCUSDT',
  kind: 'trendline',
  anchors: [
    { time: 1, price: 2 },
    { time: 3, price: 4 },
  ],
  style: { color: '#112233', lineWidth: 2, lineStyle: 'dashed' },
} as Drawing

describe('persistence client requests (characterisation)', () => {
  const make = (fetchImpl: typeof fetch) =>
    createPersistenceClient({ fetchImpl, timeoutMs: TIMEOUT_MS, baseUrl: 'http://p.test' })

  it('getWatchlist', async () => {
    const r = await characterise((f) => make(f).getWatchlist('default'))
    expect(r).toMatchSnapshot()
  })

  it('putLayout', async () => {
    const r = await characterise((f) => make(f).putLayout('main', { a: 1 }))
    expect(r).toMatchSnapshot()
  })
})

describe('drawings client requests (characterisation)', () => {
  const make = (fetchImpl: typeof fetch) =>
    createDrawingsClient({ fetchImpl, timeoutMs: TIMEOUT_MS, baseUrl: 'http://d.test/' })

  it('list', async () => {
    const r = await characterise((f, signal) => make(f).list(' btcusdt ', signal))
    expect(r).toMatchSnapshot()
  })

  it('put (keepalive)', async () => {
    const r = await characterise((f) => make(f).put(DRAWING, { keepalive: true }))
    expect(r).toMatchSnapshot()
  })

  it('remove (404 is success)', async () => {
    const r = await characterise((f) => make(f).remove('d1'))
    expect(r).toMatchSnapshot()
  })
})

describe('alerts client requests (characterisation)', () => {
  it('list and create', async () => {
    const make = (fetchImpl: typeof fetch) =>
      createAlertsClient({ fetchImpl, timeoutMs: TIMEOUT_MS })
    const list = await characterise((f) => make(f).list())
    const create = await characterise((f) =>
      make(f).create({
        symbol: 'BTCUSDT',
        condition: 'crosses_above',
        price: 1,
        frequency: 'once',
      } as never),
    )
    expect({ list, create }).toMatchSnapshot()
  })
})

describe('lab client requests (characterisation)', () => {
  const make = (fetchImpl: typeof fetch) =>
    createLabClient({
      fetchImpl,
      timeoutMs: TIMEOUT_MS,
      backtestTimeoutMs: TIMEOUT_MS * 2,
      basePath: '/lab/',
    })

  it('health (GET)', async () => {
    const r = await characterise((f, signal) => make(f).health(signal))
    expect(r).toMatchSnapshot()
  })

  it('validate (POST; 422 is a result)', async () => {
    const r = await characterise((f, signal) => make(f).validate({ name: 'p' } as never, signal))
    expect(r).toMatchSnapshot()
  })

  it('backtest (POST, own timeout)', async () => {
    const r = await characterise((f, signal) =>
      make(f).backtest({ name: 'p' } as never, { runId: 'r1', signal }),
    )
    expect(r).toMatchSnapshot()
  })
})

describe('research client requests (characterisation)', () => {
  const make = (fetchImpl: typeof fetch) =>
    createResearchClient({ fetchImpl, timeoutMs: TIMEOUT_MS, basePath: '/lab/research/' })

  it('runs', async () => {
    const r = await characterise((f, signal) => make(f).runs(signal))
    expect(r).toMatchSnapshot()
  })

  it('journalEntry', async () => {
    const r = await characterise((f, signal) => make(f).journalEntry('2026-x.md', signal))
    expect(r).toMatchSnapshot()
  })
})
