import type { FastifyInstance } from 'fastify'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Alert, AlertEvent, WebhookPayload } from '../src/alerts/types.ts'
import { buildApp } from '../src/app.ts'
import { openDb, type Db } from '../src/db/index.ts'
import { MAX_BUFFERED_BYTES, PING_INTERVAL_MS } from '../src/routes/stream.ts'

// End to end: REST → engine → shared hub (fake upstream) → /ws/alerts + notifiers. Offline.

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

const mini = (s: string, c: number, E = 1_700_000_000_000) => ({
  e: '24hrMiniTicker',
  E,
  s,
  c: String(c),
  o: String(c),
  h: String(c),
  l: String(c),
  v: '1',
  q: '1',
})

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

const tick = async (): Promise<void> => {
  for (let i = 0; i < 5; i++) await new Promise<void>((r) => setImmediate(r))
}

describe('/ws/alerts', () => {
  let db: Db
  let app: FastifyInstance
  let clock: number
  let desktop: ReturnType<typeof vi.fn<(t: string, b: string) => Promise<void>>>
  let webhook: ReturnType<typeof vi.fn<(p: WebhookPayload) => Promise<void>>>

  beforeEach(async () => {
    FakeUpstream.instances = []
    clock = 1_700_000_000_000
    desktop = vi.fn<(t: string, b: string) => Promise<void>>(() => Promise.resolve())
    webhook = vi.fn<(p: WebhookPayload) => Promise<void>>(() => Promise.resolve())
    db = openDb(':memory:')
    app = await buildApp({
      db,
      fetch: () => Promise.reject(new Error('no network in tests')),
      WebSocketImpl: FakeUpstream as unknown as typeof WebSocket,
      wsBase: 'wss://upstream.test',
      now: () => clock,
      alertNotifier: { desktop, webhook },
    })
    await app.ready()
  })

  afterEach(async () => {
    vi.useRealTimers()
    await app.close()
    db.close()
  })

  async function connect(headers: Record<string, string> = {}): Promise<Connected> {
    const messages: unknown[] = []
    let resolveClosed!: (v: { code: number; reason: string }) => void
    const closed = new Promise<{ code: number; reason: string }>((r) => (resolveClosed = r))
    const waiters: (() => void)[] = []
    const ws = (await app.injectWS(
      '/ws/alerts',
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

  const createAlert = async (body: Record<string, unknown>): Promise<Alert> => {
    const res = await app.inject({ method: 'POST', url: '/api/alerts', payload: body })
    expect(res.statusCode).toBe(201)
    return res.json<Alert>()
  }

  it('rejects a foreign Origin with 1008', async () => {
    const c = await connect({ origin: 'https://evil.example' })
    await expect(c.closed).resolves.toEqual({ code: 1008, reason: 'origin not allowed' })
  })

  it('sends a heartbeat on attach and broadcasts changed on every mutation', async () => {
    const c = await connect({ origin: 'http://localhost:5173' })
    expect(await c.next(1)).toEqual([{ type: 'heartbeat' }])
    const a = await createAlert({
      symbol: 'BTCUSDT',
      condition: 'greater',
      price: 65000,
      frequency: 'once',
    })
    await app.inject({ method: 'PATCH', url: `/api/alerts/${a.id}`, payload: { note: 'x' } })
    await app.inject({ method: 'DELETE', url: `/api/alerts/${a.id}` })
    await app.inject({ method: 'DELETE', url: '/api/alerts/events' })
    expect(await c.next(5)).toEqual([
      { type: 'heartbeat' },
      { type: 'changed' },
      { type: 'changed' },
      { type: 'changed' },
      { type: 'changed' },
    ])
    // Failed mutations broadcast nothing.
    await app.inject({ method: 'DELETE', url: `/api/alerts/${a.id}` })
    await tick()
    expect(c.messages).toHaveLength(5)
    c.ws.close()
  })

  it('heartbeats (and pings) every 20 s', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    const c = await connect()
    let pings = 0
    c.ws.on('ping', () => (pings += 1))
    await c.next(1)
    vi.advanceTimersByTime(PING_INTERVAL_MS - 1)
    await tick()
    expect(c.messages).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(await c.next(2)).toEqual([{ type: 'heartbeat' }, { type: 'heartbeat' }])
    expect(pings).toBe(1)
    c.ws.terminate()
    await c.closed
    // The server sees the socket close shortly after the client does.
    for (let i = 0; i < 20 && vi.getTimerCount() > 0; i++) await tick()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('a live tick triggers: event stored, triggered + changed pushed, once alert deactivated', async () => {
    const c = await connect()
    await c.next(1)
    const a = await createAlert({
      symbol: 'btcusdt',
      condition: 'crosses_above',
      price: 65000,
      frequency: 'once',
      note: 'tp',
    })
    await c.next(2)
    expect(FakeUpstream.instances).toHaveLength(1)
    const up = FakeUpstream.instances[0]!
    expect(up.url).toBe('wss://upstream.test/ws/!miniTicker@arr')

    up.emit([mini('BTCUSDT', 64990)])
    clock += 2000
    up.emit([mini('ETHUSDT', 3000), mini('BTCUSDT', 65012.5)])
    const msgs = await c.next(4)
    const events = (await app.inject({ url: '/api/alerts/events' })).json<AlertEvent[]>()
    expect(events).toHaveLength(1)
    const ev = events[0]!
    expect(ev).toMatchObject({
      alertId: a.id,
      symbol: 'BTCUSDT',
      condition: 'crosses_above',
      level: 65000,
      price: 65012.5,
      triggeredAt: 1_700_000_002,
      message: 'BTCUSDT crossing up 65,000 (last 65,012.5)',
    })
    const after = (await app.inject({ url: '/api/alerts' })).json<Alert[]>()[0]!
    expect(after).toMatchObject({ id: a.id, active: false, lastTriggeredAt: 1_700_000_002 })
    expect(msgs[2]).toEqual({ type: 'triggered', event: ev, alert: after })
    expect(msgs[3]).toEqual({ type: 'changed' })

    await tick()
    // auto mode with a browser attached: no desktop notification; webhook always.
    expect(desktop).not.toHaveBeenCalled()
    expect(webhook).toHaveBeenCalledTimes(1)
    expect(webhook.mock.calls[0]![0]).toEqual({
      text: 'BTCUSDT crossing up 65,000 (last 65,012.5)\nNote: tp',
      content: 'BTCUSDT crossing up 65,000 (last 65,012.5)\nNote: tp',
      event: ev,
      alert: after,
      allowed_mentions: { parse: [] },
    })
    c.ws.close()
  })

  it('auto mode sends a desktop notification when no browser is attached', async () => {
    await createAlert({ symbol: 'ETHUSDT', condition: 'less', price: 3000, frequency: 'every' })
    FakeUpstream.instances[0]!.emit([mini('ETHUSDT', 2999)])
    await tick()
    expect(desktop).toHaveBeenCalledWith('Alert: ETHUSDT', 'ETHUSDT less than 3,000 (last 2,999)')
    // every: stays active, and the stream stays held.
    const list = (await app.inject({ url: '/api/alerts' })).json<Alert[]>()
    expect(list[0]?.active).toBe(true)
    expect(FakeUpstream.instances[0]!.closed).toBe(false)
  })

  it('re-activating a once alert re-arms it', async () => {
    const a = await createAlert({
      symbol: 'BTCUSDT',
      condition: 'greater',
      price: 100,
      frequency: 'once',
    })
    const up = FakeUpstream.instances[0]!
    up.emit([mini('BTCUSDT', 101)])
    up.emit([mini('BTCUSDT', 102)])
    let events = (await app.inject({ url: '/api/alerts/events' })).json<AlertEvent[]>()
    expect(events).toHaveLength(1)
    const res = await app.inject({
      method: 'PATCH',
      url: `/api/alerts/${a.id}`,
      payload: { active: true },
    })
    expect(res.json<Alert>().active).toBe(true)
    // Still within the hub's release grace period, so the same upstream socket is reused.
    expect(FakeUpstream.instances).toHaveLength(1)
    up.emit([mini('BTCUSDT', 103)])
    events = (await app.inject({ url: '/api/alerts/events' })).json<AlertEvent[]>()
    expect(events).toHaveLength(2)
  })

  it('a server restart does not re-fire a still-true every alert; PATCH price re-arms it', async () => {
    const a = await createAlert({
      symbol: 'BTCUSDT',
      condition: 'greater',
      price: 100,
      frequency: 'every',
    })
    FakeUpstream.instances[0]!.emit([mini('BTCUSDT', 101)])
    const count = async (): Promise<number> =>
      (await app.inject({ url: '/api/alerts/events' })).json<AlertEvent[]>().length
    expect(await count()).toBe(1)

    // Restart (as `tsx watch` does on save): a new app on the same database.
    await app.close()
    FakeUpstream.instances = []
    app = await buildApp({
      db,
      fetch: () => Promise.reject(new Error('no network in tests')),
      WebSocketImpl: FakeUpstream as unknown as typeof WebSocket,
      wsBase: 'wss://upstream.test',
      now: () => clock,
      alertNotifier: { desktop, webhook },
    })
    await app.ready()
    clock += 120_000
    const up = FakeUpstream.instances[0]!
    up.emit([mini('BTCUSDT', 102)])
    expect(await count()).toBe(1)

    // An explicit edit re-arms: fires at once while the condition holds.
    const res = await app.inject({
      method: 'PATCH',
      url: `/api/alerts/${a.id}`,
      payload: { price: 99 },
    })
    expect(res.statusCode).toBe(200)
    up.emit([mini('BTCUSDT', 102)])
    expect(await count()).toBe(2)
  })

  it('terminates a slow client without affecting others', async () => {
    const serverSockets: { bufferedAmount: number }[] = []
    app.websocketServer.on('connection', (s: { bufferedAmount: number }) => serverSockets.push(s))
    const slow = await connect()
    const fast = await connect()
    await slow.next(1)
    await fast.next(1)
    Object.defineProperty(serverSockets[0], 'bufferedAmount', {
      get: () => MAX_BUFFERED_BYTES + 1,
    })
    await createAlert({ symbol: 'BTCUSDT', condition: 'greater', price: 1, frequency: 'once' })
    await expect(slow.closed).resolves.toMatchObject({ code: 1006 })
    expect(await fast.next(2)).toEqual([{ type: 'heartbeat' }, { type: 'changed' }])
    fast.ws.close()
  })

  it('closing the app releases the upstream and leaves no timers', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    await connect()
    await createAlert({ symbol: 'BTCUSDT', condition: 'greater', price: 1e9, frequency: 'once' })
    await app.close()
    expect(FakeUpstream.instances[0]!.closed).toBe(true)
    expect(vi.getTimerCount()).toBe(0)
  })
})
