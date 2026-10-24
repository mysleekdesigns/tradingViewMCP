import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Drawing } from '../chart/drawings/types'
import {
  createDrawingsClient,
  createLocalServerRequest,
  parseDrawing,
  toStoredData,
} from './drawingsApi'
import { PersistenceError, type FetchLike } from './persistence'

interface Call {
  url: string
  method: string
  body: unknown
  headers: Record<string, string>
  signal: AbortSignal | undefined
  keepalive: boolean | undefined
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function fakeFetch(respond: (call: Call) => Response | Promise<Response>): {
  fetchImpl: FetchLike
  calls: Call[]
} {
  const calls: Call[] = []
  const fetchImpl: FetchLike = async (url, init) => {
    const call: Call = {
      url,
      method: init?.method ?? 'GET',
      body: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined,
      headers: (init?.headers ?? {}) as Record<string, string>,
      signal: init?.signal ?? undefined,
      keepalive: init?.keepalive,
    }
    calls.push(call)
    return respond(call)
  }
  return { fetchImpl, calls }
}

const TREND: Drawing = {
  id: 'd-1',
  symbol: 'BTCUSDT',
  kind: 'trendline',
  anchors: [
    { time: 1_700_000_000, price: 35_000 },
    { time: 1_700_036_000, price: 36_500.5 },
  ],
  style: { color: '#2962ff', lineWidth: 2, lineStyle: 'solid' },
}

const HLINE: Drawing = {
  id: 'd-2',
  symbol: 'BTCUSDT',
  kind: 'hline',
  anchors: [{ time: 1_700_000_000, price: 40_000 }],
  style: { color: '#FF0000', lineWidth: 1, lineStyle: 'dashed' },
}

function row(d: Drawing, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return { id: d.id, symbol: d.symbol, data: toStoredData(d), updatedAt: 1_700_000_100, ...extra }
}

afterEach(() => {
  vi.useRealTimers()
})

describe('parseDrawing', () => {
  it('accepts a server row and a flat mirror drawing, returning only known fields', () => {
    expect(parseDrawing(row(TREND))).toEqual(TREND)
    expect(parseDrawing({ ...HLINE, extra: 'x' })).toEqual(HLINE)
    const parsed = parseDrawing(row(TREND))
    expect(parsed).not.toHaveProperty('updatedAt')
    expect(parsed).not.toHaveProperty('data')
  })

  it('accepts every kind with its anchor count', () => {
    const one = [{ time: 1, price: 1 }]
    const two = [
      { time: 1, price: 1 },
      { time: 2, price: 2 },
    ]
    for (const [kind, anchors] of [
      ['hline', one],
      ['hray', one],
      ['trendline', two],
      ['fib', two],
      ['rectangle', two],
    ] as const) {
      expect(parseDrawing({ ...TREND, kind, anchors })).not.toBeNull()
    }
  })

  it.each([
    ['not an object', 'x'],
    ['null', null],
    ['array', [TREND]],
    ['bad id', { ...TREND, id: 'has space' }],
    ['id too long', { ...TREND, id: 'a'.repeat(65) }],
    ['lower-case symbol', { ...TREND, symbol: 'btcusdt' }],
    ['missing symbol', { ...TREND, symbol: undefined }],
    ['unknown kind', { ...TREND, kind: 'circle' }],
    ['too few anchors', { ...TREND, anchors: [TREND.anchors[0]] }],
    ['too many anchors for hline', { ...HLINE, anchors: TREND.anchors }],
    ['NaN time', { ...TREND, anchors: [{ time: NaN, price: 1 }, TREND.anchors[1]] }],
    ['string price', { ...TREND, anchors: [{ time: 1, price: '1' }, TREND.anchors[1]] }],
    ['zero price', { ...TREND, anchors: [{ time: 1, price: 0 }, TREND.anchors[1]] }],
    ['negative price', { ...TREND, anchors: [{ time: 1, price: -5 }, TREND.anchors[1]] }],
    ['infinite price', { ...TREND, anchors: [{ time: 1, price: Infinity }, TREND.anchors[1]] }],
    ['short colour', { ...TREND, style: { ...TREND.style, color: '#fff' } }],
    ['named colour', { ...TREND, style: { ...TREND.style, color: 'red' } }],
    ['lineWidth 0', { ...TREND, style: { ...TREND.style, lineWidth: 0 } }],
    ['lineWidth 5', { ...TREND, style: { ...TREND.style, lineWidth: 5 } }],
    ['lineWidth 1.5', { ...TREND, style: { ...TREND.style, lineWidth: 1.5 } }],
    ['unknown lineStyle', { ...TREND, style: { ...TREND.style, lineStyle: 'wavy' } }],
    ['missing style', { ...TREND, style: undefined }],
    ['data version 2', row(TREND, { data: { ...toStoredData(TREND), v: 2 } })],
    ['data not an object', row(TREND, { data: 'x' })],
  ])('rejects %s', (_label, raw) => {
    expect(parseDrawing(raw)).toBeNull()
  })
})

describe('toStoredData', () => {
  it('builds the v1 data object as a deep copy', () => {
    const data = toStoredData(TREND)
    expect(data).toEqual({ v: 1, kind: TREND.kind, anchors: TREND.anchors, style: TREND.style })
    expect(data.anchors[0]).not.toBe(TREND.anchors[0])
    expect(data.style).not.toBe(TREND.style)
    expect(parseDrawing({ id: TREND.id, symbol: TREND.symbol, data })).toEqual(TREND)
  })
})

describe('createDrawingsClient', () => {
  it('lists drawings for a symbol and skips invalid, foreign and duplicate rows', async () => {
    const { fetchImpl, calls } = fakeFetch(() =>
      jsonResponse(200, [
        row(TREND),
        { id: 'bad', symbol: 'BTCUSDT', data: { v: 1, kind: 'nope' } },
        row({ ...HLINE, symbol: 'ETHUSDT' }),
        row(TREND),
        row(HLINE),
      ]),
    )
    const client = createDrawingsClient({ fetchImpl })
    await expect(client.list(' btcusdt ')).resolves.toEqual([TREND, HLINE])
    expect(calls).toHaveLength(1)
    expect(calls[0]).toMatchObject({ url: '/api/drawings?symbol=BTCUSDT', method: 'GET' })
  })

  it('uses baseUrl without a trailing slash', async () => {
    const { fetchImpl, calls } = fakeFetch(() => jsonResponse(200, []))
    const client = createDrawingsClient({ fetchImpl, baseUrl: 'http://127.0.0.1:8787/' })
    await client.list('ETHUSDT')
    expect(calls[0].url).toBe('http://127.0.0.1:8787/api/drawings?symbol=ETHUSDT')
  })

  it('rejects a non-array list body', async () => {
    const { fetchImpl } = fakeFetch(() => jsonResponse(200, { items: [] }))
    const client = createDrawingsClient({ fetchImpl })
    await expect(client.list('BTCUSDT')).rejects.toMatchObject({ kind: 'parse' })
  })

  it('rejects an invalid symbol without a request', async () => {
    const { fetchImpl, calls } = fakeFetch(() => jsonResponse(200, []))
    const client = createDrawingsClient({ fetchImpl })
    await expect(client.list('BTC/USDT')).rejects.toBeInstanceOf(PersistenceError)
    expect(calls).toHaveLength(0)
  })

  it('PUTs { symbol, data } as JSON to /api/drawings/:id', async () => {
    const { fetchImpl, calls } = fakeFetch((c) =>
      jsonResponse(200, { id: TREND.id, symbol: 'BTCUSDT', data: c.body, updatedAt: 1 }),
    )
    const client = createDrawingsClient({ fetchImpl })
    await client.put(TREND)
    expect(calls).toEqual([
      expect.objectContaining({
        url: '/api/drawings/d-1',
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: { symbol: 'BTCUSDT', data: toStoredData(TREND) },
      }),
    ])
  })

  it('sends PUT and DELETE with keepalive only when asked', async () => {
    const { fetchImpl, calls } = fakeFetch((c) =>
      c.method === 'DELETE'
        ? new Response(null, { status: 204 })
        : jsonResponse(200, { id: TREND.id, symbol: 'BTCUSDT', data: c.body, updatedAt: 1 }),
    )
    const client = createDrawingsClient({ fetchImpl })
    await client.put(TREND, { keepalive: true })
    await client.remove('d-1', { keepalive: true })
    await client.put(TREND)
    await client.remove('d-1')
    expect(calls.map((c) => [c.method, c.keepalive])).toEqual([
      ['PUT', true],
      ['DELETE', true],
      ['PUT', undefined],
      ['DELETE', undefined],
    ])
    expect(calls[0].body).toEqual({ symbol: 'BTCUSDT', data: toStoredData(TREND) })
  })

  it('refuses an invalid id before any request', async () => {
    const { fetchImpl, calls } = fakeFetch(() => jsonResponse(200, {}))
    const client = createDrawingsClient({ fetchImpl })
    await expect(client.put({ ...TREND, id: '../x' })).rejects.toMatchObject({ kind: 'bad-id' })
    await expect(client.remove('a b')).rejects.toMatchObject({ kind: 'bad-id' })
    expect(calls).toHaveLength(0)
  })

  it('DELETEs a drawing; 204 and 404 both succeed', async () => {
    const statuses = [204, 404]
    const { fetchImpl, calls } = fakeFetch(() => {
      const status = statuses.shift() ?? 500
      return status === 204
        ? new Response(null, { status })
        : jsonResponse(status, { error: 'http', message: 'drawing d-1 not found' })
    })
    const client = createDrawingsClient({ fetchImpl })
    await expect(client.remove('d-1')).resolves.toBeUndefined()
    await expect(client.remove('d-1')).resolves.toBeUndefined()
    expect(calls.map((c) => [c.method, c.url])).toEqual([
      ['DELETE', '/api/drawings/d-1'],
      ['DELETE', '/api/drawings/d-1'],
    ])
  })

  it('surfaces the server error message', async () => {
    const { fetchImpl } = fakeFetch(() =>
      jsonResponse(400, { error: 'http', message: 'symbol must match ^[A-Z0-9]{2,30}$' }),
    )
    const client = createDrawingsClient({ fetchImpl })
    await expect(client.put(TREND)).rejects.toMatchObject({
      kind: 'http',
      status: 400,
      message: 'symbol must match ^[A-Z0-9]{2,30}$',
    })
  })

  it('aborts the list when the caller signal aborts', async () => {
    const { fetchImpl } = fakeFetch(
      (c) =>
        new Promise<Response>((_resolve, reject) => {
          c.signal?.addEventListener('abort', () => reject(new Error('aborted')))
        }),
    )
    const client = createDrawingsClient({ fetchImpl })
    const ctrl = new AbortController()
    const p = client.list('BTCUSDT', ctrl.signal)
    ctrl.abort()
    await expect(p).rejects.toMatchObject({ kind: 'network', message: 'request cancelled' })
  })
})

describe('createLocalServerRequest', () => {
  it('times out with a readable message', async () => {
    vi.useFakeTimers()
    const { fetchImpl } = fakeFetch(
      (c) =>
        new Promise<Response>((_resolve, reject) => {
          c.signal?.addEventListener('abort', () => reject(new Error('aborted')))
        }),
    )
    const request = createLocalServerRequest({ fetchImpl, timeoutMs: 100 })
    const p = request('/api/x', { method: 'GET' })
    const assertion = expect(p).rejects.toMatchObject({
      kind: 'network',
      message: 'The local server did not respond',
    })
    await vi.advanceTimersByTimeAsync(100)
    await assertion
  })

  it('maps transport failures and non-JSON bodies', async () => {
    const failing = createLocalServerRequest({
      fetchImpl: () => Promise.reject(new TypeError('Failed to fetch')),
    })
    await expect(failing('/api/x', { method: 'GET' })).rejects.toMatchObject({
      kind: 'network',
      message: 'Cannot reach the local server',
    })
    const garbage = createLocalServerRequest({
      fetchImpl: async () => new Response('<html>', { status: 200 }),
    })
    await expect(garbage('/api/x', { method: 'GET' })).rejects.toMatchObject({ kind: 'parse' })
    const plain500 = createLocalServerRequest({
      fetchImpl: async () => new Response('oops', { status: 500 }),
    })
    await expect(plain500('/api/x', { method: 'GET' })).rejects.toMatchObject({
      kind: 'http',
      status: 500,
      message: 'Server error (HTTP 500)',
    })
  })

  it('sends no body or content type for GET', async () => {
    const { fetchImpl, calls } = fakeFetch(() => jsonResponse(200, {}))
    await createLocalServerRequest({ fetchImpl })('/api/x', { method: 'GET' })
    expect(calls[0].headers).toEqual({})
    expect(calls[0].body).toBeUndefined()
  })
})
