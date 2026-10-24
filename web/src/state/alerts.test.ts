import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Alert, AlertEvent, AlertStreamMessage } from '../data/alertTypes'
import type { AlertsClient, AlertStreamOptions } from '../data/alertsApi'
import { PersistenceError } from '../data/persistence'
import type { ConnectionStatus } from '../types'
import {
  ALERTS_RETRY_MS,
  createAlertsStore,
  mergeEvents,
  sortAlerts,
  upsertAlert,
  type AlertsSnapshot,
} from './alerts'

function alert(id: string, createdAt: number, extra: Partial<Alert> = {}): Alert {
  return {
    id,
    symbol: 'BTCUSDT',
    condition: 'crosses_above',
    price: 65_000,
    frequency: 'once',
    active: true,
    note: '',
    createdAt,
    updatedAt: createdAt,
    lastTriggeredAt: null,
    ...extra,
  }
}

function event(id: number, alertId = 'a1', triggeredAt = 1_700_000_000 + id): AlertEvent {
  return {
    id,
    alertId,
    symbol: 'BTCUSDT',
    condition: 'crosses_above',
    level: 65_000,
    price: 65_010,
    triggeredAt,
    message: `BTCUSDT crossing up 65,000 (#${id})`,
  }
}

function deferred<T>(): { promise: Promise<T>; resolve: (v: T) => void } {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((res) => {
    resolve = res
  })
  return { promise, resolve }
}

interface FakeAlertsClient extends AlertsClient {
  calls: string[]
  alerts: Alert[]
  events: AlertEvent[]
  fail: boolean
  /** When set, list() waits for it. */
  gate: Promise<void> | null
}

function fakeClient(): FakeAlertsClient {
  const c: FakeAlertsClient = {
    calls: [],
    alerts: [],
    events: [],
    fail: false,
    gate: null,
    async list() {
      c.calls.push('list')
      if (c.gate) await c.gate
      if (c.fail) throw new PersistenceError('network', 'Cannot reach the local server')
      return c.alerts.slice()
    },
    async listEvents(limit) {
      c.calls.push(`events ${limit}`)
      if (c.fail) throw new PersistenceError('network', 'Cannot reach the local server')
      return c.events.slice()
    },
    async create(a) {
      c.calls.push('create')
      if (c.fail) throw new PersistenceError('http', 'price must be a finite number > 0', 400)
      return alert('new', 1_800_000_000, { ...a, note: a.note ?? '' })
    },
    async update(id, patch) {
      c.calls.push(`update ${id}`)
      const found = c.alerts.find((a) => a.id === id) ?? alert(id, 1)
      return { ...found, ...patch, updatedAt: found.updatedAt + 1 }
    },
    async remove(id) {
      c.calls.push(`remove ${id}`)
    },
    async clearEvents() {
      c.calls.push('clearEvents')
    },
  }
  return c
}

interface FakeStream {
  opts: AlertStreamOptions | null
  closed: number
  send(m: AlertStreamMessage): void
  status(s: ConnectionStatus): void
  open: (opts: AlertStreamOptions) => () => void
}

function fakeStream(): FakeStream {
  const s: FakeStream = {
    opts: null,
    closed: 0,
    send: (m) => s.opts?.onMessage(m),
    status: (st) => s.opts?.onStatus?.(st),
    open: (opts) => {
      s.opts = opts
      return () => {
        s.closed += 1
      }
    },
  }
  return s
}

describe('pure helpers', () => {
  it('sortAlerts orders newest first', () => {
    expect(sortAlerts([alert('a', 1), alert('b', 3), alert('c', 2)]).map((a) => a.id)).toEqual([
      'b',
      'c',
      'a',
    ])
  })

  it('upsertAlert replaces in place or inserts in order', () => {
    const list = [alert('b', 3), alert('a', 1)]
    expect(upsertAlert(list, alert('a', 1, { active: false }))[1].active).toBe(false)
    expect(upsertAlert(list, alert('c', 2)).map((a) => a.id)).toEqual(['b', 'c', 'a'])
  })

  it('mergeEvents dedupes by id (first wins), sorts newest first and caps at 100', () => {
    const merged = mergeEvents([event(2), { ...event(1), message: 'first' }], [event(1), event(3)])
    expect(merged.map((e) => e.id)).toEqual([3, 2, 1])
    expect(merged[2].message).toBe('first')
    const many = Array.from({ length: 150 }, (_, i) => event(i))
    expect(mergeEvents(many)).toHaveLength(100)
    expect(mergeEvents(many)[0].id).toBe(149)
  })
})

describe('createAlertsStore', () => {
  let snaps: AlertsSnapshot[]
  const onChange = (s: AlertsSnapshot) => snaps.push(s)

  beforeEach(() => {
    snaps = []
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('is unavailable with no requests and no socket when client is null', async () => {
    const stream = fakeStream()
    const store = createAlertsStore({ client: null, onChange, openStream: stream.open })
    store.start()
    await vi.runAllTimersAsync()
    expect(store.snapshot.status).toBe('unavailable')
    expect(stream.opts).toBeNull()
    await expect(store.create(alert('x', 1))).rejects.toThrow(/local server/)
  })

  it('loads alerts and the last 100 events, then opens the stream', async () => {
    const client = fakeClient()
    client.alerts = [alert('a1', 1), alert('a2', 2)]
    client.events = [event(2), event(1)]
    const stream = fakeStream()
    const store = createAlertsStore({ client, onChange, openStream: stream.open })
    expect(store.snapshot.status).toBe('loading')
    store.start()
    store.start()
    await vi.advanceTimersByTimeAsync(0)
    expect(client.calls).toEqual(['list', 'events 100'])
    expect(store.snapshot.status).toBe('ready')
    expect(store.snapshot.alerts.map((a) => a.id)).toEqual(['a2', 'a1'])
    expect(store.snapshot.events.map((e) => e.id)).toEqual([2, 1])
    expect(stream.opts).not.toBeNull()
  })

  it('prepends a triggered event once, replaces its alert, counts unseen, calls onTriggered once', async () => {
    const client = fakeClient()
    client.alerts = [alert('a1', 1)]
    client.events = [event(1)]
    const stream = fakeStream()
    const onTriggered = vi.fn()
    const store = createAlertsStore({ client, onChange, onTriggered, openStream: stream.open })
    store.start()
    await vi.advanceTimersByTimeAsync(0)
    const fired = alert('a1', 1, { active: false, lastTriggeredAt: 1_700_000_002, updatedAt: 5 })
    stream.send({ type: 'triggered', event: event(2), alert: fired })
    stream.send({ type: 'triggered', event: event(2), alert: fired })
    expect(store.snapshot.events.map((e) => e.id)).toEqual([2, 1])
    expect(store.snapshot.alerts).toEqual([fired])
    expect(store.snapshot.unseen).toBe(1)
    expect(onTriggered).toHaveBeenCalledTimes(1)
    expect(onTriggered).toHaveBeenCalledWith(event(2), fired)
    stream.send({ type: 'triggered', event: event(3), alert: fired })
    expect(store.snapshot.unseen).toBe(2)
    store.markSeen()
    expect(store.snapshot.unseen).toBe(0)
  })

  it('keeps going when onTriggered throws', async () => {
    const client = fakeClient()
    const stream = fakeStream()
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
    const store = createAlertsStore({
      client,
      onChange,
      openStream: stream.open,
      onTriggered: () => {
        throw new Error('boom')
      },
    })
    store.start()
    await vi.advanceTimersByTimeAsync(0)
    stream.send({ type: 'triggered', event: event(1), alert: alert('a1', 1) })
    expect(store.snapshot.unseen).toBe(1)
    expect(errors).toHaveBeenCalled()
    errors.mockRestore()
  })

  it('ignores heartbeats', async () => {
    const client = fakeClient()
    const stream = fakeStream()
    const store = createAlertsStore({ client, onChange, openStream: stream.open })
    store.start()
    await vi.advanceTimersByTimeAsync(0)
    const n = snaps.length
    stream.send({ type: 'heartbeat' })
    expect(snaps).toHaveLength(n)
    expect(client.calls).toEqual(['list', 'events 100'])
  })

  it('refetches on changed, coalescing a burst into one in-flight load plus one follow-up', async () => {
    const client = fakeClient()
    const stream = fakeStream()
    const store = createAlertsStore({ client, onChange, openStream: stream.open })
    store.start()
    await vi.advanceTimersByTimeAsync(0)
    client.calls = []
    const gate = deferred<void>()
    client.gate = gate.promise
    client.alerts = [alert('z', 9)]
    for (let i = 0; i < 5; i++) stream.send({ type: 'changed' })
    expect(client.calls).toEqual(['list', 'events 100'])
    client.gate = null
    gate.resolve()
    await vi.advanceTimersByTimeAsync(0)
    expect(client.calls).toEqual(['list', 'events 100', 'list', 'events 100'])
    expect(store.snapshot.alerts.map((a) => a.id)).toEqual(['z'])
  })

  it('keeps a trigger that arrives while a refetch is in flight', async () => {
    const client = fakeClient()
    client.alerts = [alert('a1', 1)]
    const stream = fakeStream()
    const store = createAlertsStore({ client, onChange, openStream: stream.open })
    store.start()
    await vi.advanceTimersByTimeAsync(0)
    const gate = deferred<void>()
    client.gate = gate.promise
    stream.send({ type: 'changed' })
    const fired = alert('a1', 1, { active: false, updatedAt: 10 })
    stream.send({ type: 'triggered', event: event(5), alert: fired })
    client.gate = null
    gate.resolve() // the response predates the trigger: no event 5, old alert
    await vi.advanceTimersByTimeAsync(0)
    expect(store.snapshot.events.map((e) => e.id)).toEqual([5])
    expect(store.snapshot.alerts).toEqual([fired])
  })

  it('refetches when the stream comes back live after a drop, not on the first live', async () => {
    const client = fakeClient()
    const stream = fakeStream()
    const store = createAlertsStore({ client, onChange, openStream: stream.open })
    store.start()
    await vi.advanceTimersByTimeAsync(0)
    stream.status('connecting')
    stream.status('live')
    await vi.advanceTimersByTimeAsync(0)
    expect(client.calls).toEqual(['list', 'events 100'])
    stream.status('reconnecting')
    stream.status('live')
    await vi.advanceTimersByTimeAsync(0)
    expect(client.calls).toEqual(['list', 'events 100', 'list', 'events 100'])
  })

  it('is unavailable after a failed initial load and retries every 30 s until it succeeds', async () => {
    const client = fakeClient()
    client.fail = true
    const stream = fakeStream()
    const store = createAlertsStore({ client, onChange, openStream: stream.open })
    store.start()
    await vi.advanceTimersByTimeAsync(0)
    expect(store.snapshot.status).toBe('unavailable')
    expect(client.calls.filter((c) => c === 'list')).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(ALERTS_RETRY_MS - 1)
    expect(client.calls.filter((c) => c === 'list')).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(client.calls.filter((c) => c === 'list')).toHaveLength(2)
    expect(store.snapshot.status).toBe('unavailable')
    client.fail = false
    client.alerts = [alert('a1', 1)]
    await vi.advanceTimersByTimeAsync(ALERTS_RETRY_MS)
    expect(store.snapshot.status).toBe('ready')
    expect(store.snapshot.alerts).toHaveLength(1)
    const calls = client.calls.length
    await vi.advanceTimersByTimeAsync(ALERTS_RETRY_MS * 3)
    expect(client.calls).toHaveLength(calls)
  })

  it('recovers at once when the stream reads live while unavailable', async () => {
    const client = fakeClient()
    client.fail = true
    const stream = fakeStream()
    const store = createAlertsStore({ client, onChange, openStream: stream.open })
    store.start()
    await vi.advanceTimersByTimeAsync(0)
    client.fail = false
    stream.status('live')
    await vi.advanceTimersByTimeAsync(0)
    expect(store.snapshot.status).toBe('ready')
  })

  it('keeps ready data when a later refetch fails', async () => {
    const client = fakeClient()
    client.alerts = [alert('a1', 1)]
    const stream = fakeStream()
    const store = createAlertsStore({ client, onChange, openStream: stream.open })
    store.start()
    await vi.advanceTimersByTimeAsync(0)
    client.fail = true
    stream.send({ type: 'changed' })
    await vi.advanceTimersByTimeAsync(ALERTS_RETRY_MS * 2)
    expect(store.snapshot.status).toBe('ready')
    expect(store.snapshot.alerts).toHaveLength(1)
    expect(client.calls.filter((c) => c === 'list')).toHaveLength(2)
  })

  it('applies mutation responses to local state straight away', async () => {
    const client = fakeClient()
    client.alerts = [alert('a1', 1)]
    client.events = [event(1)]
    const stream = fakeStream()
    const store = createAlertsStore({ client, onChange, openStream: stream.open })
    store.start()
    await vi.advanceTimersByTimeAsync(0)
    const created = await store.create({
      symbol: 'BTCUSDT',
      condition: 'less',
      price: 60_000,
      frequency: 'every',
    })
    expect(store.snapshot.alerts.map((a) => a.id)).toEqual(['new', 'a1'])
    expect(created.condition).toBe('less')
    const updated = await store.update('a1', { active: false })
    expect(updated.active).toBe(false)
    expect(store.snapshot.alerts.find((a) => a.id === 'a1')?.active).toBe(false)
    await store.remove('a1')
    expect(store.snapshot.alerts.map((a) => a.id)).toEqual(['new'])
    stream.send({ type: 'triggered', event: event(2, 'new'), alert: created })
    await store.clearHistory()
    expect(store.snapshot.events).toEqual([])
    expect(store.snapshot.unseen).toBe(0)
  })

  it('rejects a failed mutation with the user-readable server message', async () => {
    const client = fakeClient()
    const stream = fakeStream()
    const store = createAlertsStore({ client, onChange, openStream: stream.open })
    store.start()
    await vi.advanceTimersByTimeAsync(0)
    client.fail = true
    const err = await store
      .create({ symbol: 'BTCUSDT', condition: 'greater', price: -1, frequency: 'once' })
      .catch((e: unknown) => e)
    expect(err).toBeInstanceOf(Error)
    expect((err as Error).message).toBe('price must be a finite number > 0')
  })

  it('closes the stream, stops retries and ignores late results on dispose', async () => {
    const client = fakeClient()
    const gate = deferred<void>()
    client.gate = gate.promise
    const stream = fakeStream()
    const store = createAlertsStore({ client, onChange, openStream: stream.open })
    store.start()
    store.dispose()
    store.dispose()
    expect(stream.closed).toBe(1)
    client.gate = null
    gate.resolve()
    await vi.advanceTimersByTimeAsync(ALERTS_RETRY_MS * 2)
    expect(snaps).toEqual([])
    expect(client.calls).toEqual(['list', 'events 100'])
  })

  it('passes WebSocketImpl to the stream opener', () => {
    const client = fakeClient()
    const stream = fakeStream()
    const WS = class {} as unknown as typeof WebSocket
    const store = createAlertsStore({ client, openStream: stream.open, WebSocketImpl: WS })
    store.start()
    expect(stream.opts?.WebSocketImpl).toBe(WS)
    store.dispose()
  })
})
