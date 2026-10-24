import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Alert, AlertEvent } from './alertTypes'
import {
  createAlertsClient,
  openAlertStream,
  parseAlert,
  parseAlertEvent,
  parseAlertStreamMessage,
  pickAlertPatch,
  pickNewAlert,
} from './alertsApi'
import type { FetchLike } from './persistence'
import type { ConnectionStatus } from '../types'

interface Call {
  url: string
  method: string
  body: unknown
  headers: Record<string, string>
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
    }
    calls.push(call)
    return respond(call)
  }
  return { fetchImpl, calls }
}

const ALERT: Alert = {
  id: 'a1',
  symbol: 'BTCUSDT',
  condition: 'crosses_above',
  price: 65_000,
  frequency: 'once',
  active: true,
  note: '',
  createdAt: 1_700_000_000,
  updatedAt: 1_700_000_000,
  lastTriggeredAt: null,
}

const EVENT: AlertEvent = {
  id: 7,
  alertId: 'a1',
  symbol: 'BTCUSDT',
  condition: 'crosses_above',
  level: 65_000,
  price: 65_012.5,
  triggeredAt: 1_700_000_500,
  message: 'BTCUSDT crossing up 65,000 (last 65,012.5)',
}

describe('parseAlert', () => {
  it('accepts a valid alert and copies only known fields', () => {
    expect(parseAlert({ ...ALERT, extra: 1 })).toEqual(ALERT)
    expect(parseAlert({ ...ALERT, lastTriggeredAt: 1_700_000_500, note: 'hi' })).toEqual({
      ...ALERT,
      lastTriggeredAt: 1_700_000_500,
      note: 'hi',
    })
  })

  it('treats a missing note or lastTriggeredAt as empty', () => {
    const { note: _n, lastTriggeredAt: _l, ...rest } = ALERT
    void _n
    void _l
    expect(parseAlert(rest)).toEqual(ALERT)
  })

  it.each([
    ['null', null],
    ['string', 'alert'],
    ['bad id', { ...ALERT, id: 'a/1' }],
    ['lower-case symbol', { ...ALERT, symbol: 'btcusdt' }],
    ['unknown condition', { ...ALERT, condition: 'equals' }],
    ['zero price', { ...ALERT, price: 0 }],
    ['NaN price', { ...ALERT, price: NaN }],
    ['string price', { ...ALERT, price: '65000' }],
    ['unknown frequency', { ...ALERT, frequency: 'twice' }],
    ['non-boolean active', { ...ALERT, active: 1 }],
    ['numeric note', { ...ALERT, note: 5 }],
    ['missing createdAt', { ...ALERT, createdAt: undefined }],
    ['string lastTriggeredAt', { ...ALERT, lastTriggeredAt: 'yesterday' }],
  ])('rejects %s', (_label, raw) => {
    expect(parseAlert(raw)).toBeNull()
  })
})

describe('parseAlertEvent', () => {
  it('accepts a valid event', () => {
    expect(parseAlertEvent({ ...EVENT, extra: true })).toEqual(EVENT)
  })

  it.each([
    ['null', null],
    ['string id', { ...EVENT, id: '7' }],
    ['fractional id', { ...EVENT, id: 7.5 }],
    ['bad alertId', { ...EVENT, alertId: '' }],
    ['unknown condition', { ...EVENT, condition: 'x' }],
    ['NaN level', { ...EVENT, level: NaN }],
    ['missing price', { ...EVENT, price: undefined }],
    ['missing triggeredAt', { ...EVENT, triggeredAt: null }],
    ['missing message', { ...EVENT, message: undefined }],
  ])('rejects %s', (_label, raw) => {
    expect(parseAlertEvent(raw)).toBeNull()
  })
})

describe('parseAlertStreamMessage', () => {
  it('parses heartbeat, changed and triggered (string or object)', () => {
    expect(parseAlertStreamMessage('{"type":"heartbeat"}')).toEqual({ type: 'heartbeat' })
    expect(parseAlertStreamMessage({ type: 'changed', extra: 1 })).toEqual({ type: 'changed' })
    expect(
      parseAlertStreamMessage(JSON.stringify({ type: 'triggered', event: EVENT, alert: ALERT })),
    ).toEqual({ type: 'triggered', event: EVENT, alert: ALERT })
  })

  it.each([
    ['invalid JSON', '{nope'],
    ['number', 42],
    ['array', [{ type: 'changed' }]],
    ['unknown type', { type: 'kline' }],
    ['triggered without alert', { type: 'triggered', event: EVENT }],
    ['triggered with bad event', { type: 'triggered', event: { ...EVENT, id: 'x' }, alert: ALERT }],
    [
      'triggered with mismatched ids',
      { type: 'triggered', event: { ...EVENT, alertId: 'other' }, alert: ALERT },
    ],
  ])('returns null for %s', (_label, raw) => {
    expect(parseAlertStreamMessage(raw)).toBeNull()
  })
})

describe('createAlertsClient', () => {
  it('lists alerts newest first as sent, skipping malformed rows', async () => {
    const second = { ...ALERT, id: 'a2', createdAt: 1_700_000_100 }
    const { fetchImpl, calls } = fakeFetch(() =>
      jsonResponse(200, [second, { id: 'broken' }, ALERT]),
    )
    const client = createAlertsClient({ fetchImpl })
    await expect(client.list()).resolves.toEqual([second, ALERT])
    expect(calls[0]).toMatchObject({ url: '/api/alerts', method: 'GET' })
  })

  it('POSTs a NewAlert and returns the created alert (201)', async () => {
    const { fetchImpl, calls } = fakeFetch(() => jsonResponse(201, ALERT))
    const client = createAlertsClient({ fetchImpl, baseUrl: 'http://127.0.0.1:8787' })
    const body = { symbol: 'BTCUSDT', condition: 'crosses_above', price: 65_000, frequency: 'once' }
    await expect(client.create(body as Parameters<typeof client.create>[0])).resolves.toEqual(ALERT)
    expect(calls[0]).toMatchObject({
      url: 'http://127.0.0.1:8787/api/alerts',
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
    })
  })

  it('PATCHes /api/alerts/:id with the patch', async () => {
    const { fetchImpl, calls } = fakeFetch(() => jsonResponse(200, { ...ALERT, active: false }))
    const client = createAlertsClient({ fetchImpl })
    await expect(client.update('a1', { active: false })).resolves.toMatchObject({ active: false })
    expect(calls[0]).toMatchObject({
      url: '/api/alerts/a1',
      method: 'PATCH',
      body: { active: false },
    })
  })

  it('sends only the known NewAlert keys on create (note omitted when undefined)', async () => {
    const { fetchImpl, calls } = fakeFetch(() => jsonResponse(201, ALERT))
    const client = createAlertsClient({ fetchImpl })
    // e.g. form values spread with extra UI fields: the server 400s on unknown keys.
    const withExtras = {
      symbol: 'BTCUSDT',
      condition: 'crosses_above',
      price: 65_000,
      frequency: 'once',
      note: undefined,
      editing: true,
      id: 'x',
    } as unknown as Parameters<typeof client.create>[0]
    await client.create(withExtras)
    await client.create({ ...withExtras, note: 'hi' })
    expect(calls[0]?.body).toEqual({
      symbol: 'BTCUSDT',
      condition: 'crosses_above',
      price: 65_000,
      frequency: 'once',
    })
    expect(Object.keys(calls[0]?.body as object)).not.toContain('note')
    expect(calls[1]?.body).toEqual({
      symbol: 'BTCUSDT',
      condition: 'crosses_above',
      price: 65_000,
      frequency: 'once',
      note: 'hi',
    })
  })

  it('sends only the AlertPatch keys present on update', async () => {
    const { fetchImpl, calls } = fakeFetch(() => jsonResponse(200, ALERT))
    const client = createAlertsClient({ fetchImpl })
    const patch = {
      price: 66_000,
      note: '',
      frequency: undefined,
      symbol: 'ETHUSDT',
      createdAt: 1,
    } as unknown as Parameters<typeof client.update>[1]
    await client.update('a1', patch)
    expect(calls[0]?.body).toEqual({ price: 66_000, note: '' })
  })

  it('pickNewAlert / pickAlertPatch copy known keys only', () => {
    expect(
      pickNewAlert({ symbol: 'BTCUSDT', condition: 'less', price: 1, frequency: 'every' }),
    ).toEqual({ symbol: 'BTCUSDT', condition: 'less', price: 1, frequency: 'every' })
    expect(
      pickAlertPatch({
        condition: 'greater',
        price: 2,
        frequency: 'once',
        active: false,
        note: 'n',
      }),
    ).toEqual({ condition: 'greater', price: 2, frequency: 'once', active: false, note: 'n' })
    expect(pickAlertPatch({})).toEqual({})
  })

  it('rejects a malformed create/update response', async () => {
    const { fetchImpl } = fakeFetch(() => jsonResponse(200, { ...ALERT, price: -1 }))
    const client = createAlertsClient({ fetchImpl })
    await expect(client.update('a1', { price: 1 })).rejects.toMatchObject({ kind: 'parse' })
  })

  it('DELETEs an alert; 404 counts as success', async () => {
    const statuses = [204, 404]
    const { fetchImpl, calls } = fakeFetch(() => {
      const status = statuses.shift() ?? 500
      return status === 204
        ? new Response(null, { status })
        : jsonResponse(status, { error: 'http', message: 'alert a1 not found' })
    })
    const client = createAlertsClient({ fetchImpl })
    await client.remove('a1')
    await client.remove('a1')
    expect(calls.map((c) => [c.method, c.url])).toEqual([
      ['DELETE', '/api/alerts/a1'],
      ['DELETE', '/api/alerts/a1'],
    ])
  })

  it('lists events with a limit (default 100) and clears history', async () => {
    const { fetchImpl, calls } = fakeFetch((c) =>
      c.method === 'DELETE' ? new Response(null, { status: 204 }) : jsonResponse(200, [EVENT, 5]),
    )
    const client = createAlertsClient({ fetchImpl })
    await expect(client.listEvents()).resolves.toEqual([EVENT])
    await client.listEvents(20)
    await client.clearEvents()
    expect(calls.map((c) => [c.method, c.url])).toEqual([
      ['GET', '/api/alerts/events?limit=100'],
      ['GET', '/api/alerts/events?limit=20'],
      ['DELETE', '/api/alerts/events'],
    ])
  })

  it.each([
    [400, 'price must be a finite number > 0'],
    [404, 'alert zz not found'],
    [409, 'alert limit reached'],
  ])('surfaces the server message for HTTP %i', async (status, message) => {
    const { fetchImpl } = fakeFetch(() => jsonResponse(status, { error: 'http', message }))
    const client = createAlertsClient({ fetchImpl })
    await expect(client.update('zz', { price: 1 })).rejects.toMatchObject({
      kind: 'http',
      status,
      message,
    })
  })

  it('rejects an invalid id before any request', async () => {
    const { fetchImpl, calls } = fakeFetch(() => jsonResponse(200, ALERT))
    const client = createAlertsClient({ fetchImpl })
    await expect(client.update('../etc', {})).rejects.toMatchObject({ kind: 'bad-id' })
    expect(calls).toHaveLength(0)
  })

  it('rejects a non-array list', async () => {
    const { fetchImpl } = fakeFetch(() => jsonResponse(200, { alerts: [] }))
    const client = createAlertsClient({ fetchImpl })
    await expect(client.list()).rejects.toMatchObject({ kind: 'parse' })
    await expect(client.listEvents()).rejects.toMatchObject({ kind: 'parse' })
  })
})

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
}
const WS = FakeWebSocket as unknown as typeof WebSocket
const lastWs = () => FakeWebSocket.instances[FakeWebSocket.instances.length - 1]

describe('openAlertStream', () => {
  beforeEach(() => {
    FakeWebSocket.instances = []
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('reads live on the first message and delivers parsed messages only', () => {
    const statuses: ConnectionStatus[] = []
    const messages: unknown[] = []
    const stop = openAlertStream({
      WebSocketImpl: WS,
      url: 'ws://localhost:5173/ws/alerts',
      onMessage: (m) => messages.push(m),
      onStatus: (s) => statuses.push(s),
    })
    const ws = lastWs()
    expect(ws.url).toBe('ws://localhost:5173/ws/alerts')
    ws.serverOpen()
    expect(statuses).toEqual(['connecting'])
    ws.serverMessage({ type: 'heartbeat' })
    expect(statuses).toEqual(['connecting', 'live'])
    ws.serverMessage('garbage')
    ws.serverMessage({ type: 'triggered', event: EVENT, alert: ALERT })
    expect(messages).toEqual([
      { type: 'heartbeat' },
      { type: 'triggered', event: EVENT, alert: ALERT },
    ])
    stop()
    expect(ws.closeCalls).toBe(1)
    stop()
  })

  it('reconnects after a drop and after 60 s of silence', () => {
    const stop = openAlertStream({
      WebSocketImpl: WS,
      url: 'ws://x/ws/alerts',
      onMessage: () => {},
    })
    lastWs().serverOpen()
    lastWs().serverMessage({ type: 'heartbeat' })
    lastWs().serverClose(1006)
    vi.advanceTimersByTime(1_000)
    expect(FakeWebSocket.instances).toHaveLength(2)
    lastWs().serverOpen()
    lastWs().serverMessage({ type: 'heartbeat' })
    vi.advanceTimersByTime(59_999)
    expect(FakeWebSocket.instances).toHaveLength(2)
    vi.advanceTimersByTime(1 + 1_000)
    expect(FakeWebSocket.instances).toHaveLength(3)
    stop()
  })

  it('stops for good on close code 1008', () => {
    const statuses: ConnectionStatus[] = []
    openAlertStream({
      WebSocketImpl: WS,
      url: 'ws://x/ws/alerts',
      onMessage: () => {},
      onStatus: (s) => statuses.push(s),
    })
    lastWs().serverClose(1008)
    vi.advanceTimersByTime(120_000)
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(statuses).toEqual(['connecting', 'offline'])
  })

  it('reports offline without a WebSocket or URL', () => {
    const statuses: ConnectionStatus[] = []
    const stop = openAlertStream({ onMessage: () => {}, onStatus: (s) => statuses.push(s) })
    expect(statuses).toEqual(['offline'])
    stop()
  })
})
