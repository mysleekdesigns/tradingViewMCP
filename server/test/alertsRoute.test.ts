import type { FastifyInstance, LightMyRequestResponse } from 'fastify'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createAlertStore } from '../src/alerts/store.ts'
import type { Alert, AlertEvent } from '../src/alerts/types.ts'
import { buildApp } from '../src/app.ts'
import { openDb, type Db } from '../src/db/index.ts'
import { MAX_ALERTS } from '../src/routes/alerts.ts'

const NOW_MS = 1_700_000_000_000
const NOW = NOW_MS / 1000

/** Upstream WebSocket that never connects (REST tests only). */
class SilentUpstream {
  static count = 0
  onopen = null
  onmessage = null
  onerror = null
  onclose = null
  constructor() {
    SilentUpstream.count += 1
  }
  close(): void {}
}

const valid = { symbol: 'btcusdt ', condition: 'crosses_above', price: 65000, frequency: 'once' }

describe('alerts REST', () => {
  let db: Db
  let app: FastifyInstance
  let clock: number

  beforeEach(async () => {
    SilentUpstream.count = 0
    clock = NOW_MS
    db = openDb(':memory:')
    app = await buildApp({
      db,
      fetch: () => Promise.reject(new Error('no network in tests')),
      WebSocketImpl: SilentUpstream as unknown as typeof WebSocket,
      wsBase: 'wss://upstream.test',
      now: () => clock,
    })
    await app.ready()
  })

  afterEach(async () => {
    await app.close()
    db.close()
  })

  const post = (payload: object): Promise<LightMyRequestResponse> =>
    app.inject({ method: 'POST', url: '/api/alerts', payload })
  const patch = (id: string, payload: object): Promise<LightMyRequestResponse> =>
    app.inject({ method: 'PATCH', url: `/api/alerts/${id}`, payload })

  it('creates an alert (201) with a server id, normalized symbol and timestamps', async () => {
    const res = await post({ ...valid, note: '  breakout  ' })
    expect(res.statusCode).toBe(201)
    const a = res.json<Alert>()
    expect(a.id).toMatch(/^[A-Za-z0-9_-]{1,64}$/)
    expect(a).toEqual({
      id: a.id,
      symbol: 'BTCUSDT',
      condition: 'crosses_above',
      price: 65000,
      frequency: 'once',
      active: true,
      note: 'breakout',
      createdAt: NOW,
      updatedAt: NOW,
      lastTriggeredAt: null,
    })
    expect(res.headers['content-type']).toMatch(/json/)
  })

  it('lists alerts newest first, optionally filtered by symbol', async () => {
    const a = (await post(valid)).json<Alert>()
    clock += 1000
    const b = (await post({ ...valid, symbol: 'ETHUSDT' })).json<Alert>()
    clock += 1000
    const c = (await post(valid)).json<Alert>()
    const all = (await app.inject({ url: '/api/alerts' })).json<Alert[]>()
    expect(all.map((x) => x.id)).toEqual([c.id, b.id, a.id])
    const btc = (await app.inject({ url: '/api/alerts?symbol=btcusdt' })).json<Alert[]>()
    expect(btc.map((x) => x.id)).toEqual([c.id, a.id])
    const bad = await app.inject({ url: '/api/alerts?symbol=bad!' })
    expect(bad.statusCode).toBe(400)
  })

  it.each([
    ['missing body fields', {}],
    ['not an object', [1]],
    ['bad symbol', { ...valid, symbol: 'B' }],
    ['symbol not a string', { ...valid, symbol: 5 }],
    ['bad condition', { ...valid, condition: 'equals' }],
    ['zero price', { ...valid, price: 0 }],
    ['negative price', { ...valid, price: -1 }],
    ['string price', { ...valid, price: '65000' }],
    ['bad frequency', { ...valid, frequency: 'twice' }],
    ['note too long', { ...valid, note: 'x'.repeat(201) }],
    ['note not a string', { ...valid, note: 5 }],
    ['unknown field', { ...valid, active: false }],
  ])('rejects %s with 400', async (_name, body) => {
    const res = await post(body)
    expect(res.statusCode).toBe(400)
    expect(res.json()).toEqual({ error: 'http', message: expect.any(String) })
  })

  it('accepts a 200-char note (after trimming)', async () => {
    const res = await post({ ...valid, note: ` ${'x'.repeat(200)} ` })
    expect(res.statusCode).toBe(201)
    expect(res.json<Alert>().note).toHaveLength(200)
  })

  it(`returns 409 beyond ${MAX_ALERTS} alerts`, async () => {
    const store = createAlertStore(db)
    for (let i = 0; i < MAX_ALERTS; i++) {
      store.insert({
        id: `a${i}`,
        symbol: 'BTCUSDT',
        condition: 'greater',
        price: 1,
        frequency: 'once',
        active: false,
        note: '',
        createdAt: NOW,
        updatedAt: NOW,
        lastTriggeredAt: null,
      })
    }
    const res = await post(valid)
    expect(res.statusCode).toBe(409)
    expect(res.json<{ error: string }>().error).toBe('http')
  })

  it('patches a subset of fields', async () => {
    const a = (await post(valid)).json<Alert>()
    clock += 5000
    const res = await patch(a.id, { price: 70000, active: false, note: 'n' })
    expect(res.statusCode).toBe(200)
    expect(res.json<Alert>()).toEqual({
      ...a,
      price: 70000,
      active: false,
      note: 'n',
      updatedAt: NOW + 5,
    })
  })

  it('rejects empty, unknown-key and invalid patches with 400', async () => {
    const a = (await post(valid)).json<Alert>()
    for (const body of [
      {},
      { symbol: 'ETHUSDT' },
      { price: 0 },
      { active: 'yes' },
      { frequency: 'x' },
    ]) {
      expect((await patch(a.id, body)).statusCode).toBe(400)
    }
    expect((await patch('bad id!', { price: 1 })).statusCode).toBe(400)
  })

  it('returns 404 for patching or deleting an unknown alert', async () => {
    const p = await patch('missing', { price: 1 })
    expect(p.statusCode).toBe(404)
    expect(p.json()).toEqual({ error: 'http', message: expect.any(String) })
    const d = await app.inject({ method: 'DELETE', url: '/api/alerts/missing' })
    expect(d.statusCode).toBe(404)
  })

  it('deletes (204) and then 404s', async () => {
    const a = (await post(valid)).json<Alert>()
    const d = await app.inject({ method: 'DELETE', url: `/api/alerts/${a.id}` })
    expect(d.statusCode).toBe(204)
    expect(d.body).toBe('')
    expect((await app.inject({ method: 'DELETE', url: `/api/alerts/${a.id}` })).statusCode).toBe(
      404,
    )
    expect((await app.inject({ url: '/api/alerts' })).json()).toEqual([])
  })

  it('serves and clears event history; /events does not collide with /:id', async () => {
    const store = createAlertStore(db)
    const a = (await post({ ...valid, frequency: 'every' })).json<Alert>()
    for (let i = 0; i < 3; i++) {
      store.recordTrigger({
        alertId: a.id,
        price: 65001 + i,
        triggeredAt: NOW + i,
        message: `m${i}`,
      })
    }
    const list = (await app.inject({ url: '/api/alerts/events' })).json<AlertEvent[]>()
    expect(list.map((e) => e.message)).toEqual(['m2', 'm1', 'm0'])
    expect(list[0]).toEqual({
      id: expect.any(Number),
      alertId: a.id,
      symbol: 'BTCUSDT',
      condition: 'crosses_above',
      level: 65000,
      price: 65003,
      triggeredAt: NOW + 2,
      message: 'm2',
    })
    const lim = (await app.inject({ url: '/api/alerts/events?limit=2' })).json<AlertEvent[]>()
    expect(lim.map((e) => e.message)).toEqual(['m2', 'm1'])
    for (const bad of ['0', '1001', 'x', '-1', '1.5']) {
      expect((await app.inject({ url: `/api/alerts/events?limit=${bad}` })).statusCode).toBe(400)
    }
    expect((await app.inject({ url: '/api/alerts/events?limit=1000' })).statusCode).toBe(200)

    // History survives deleting the alert.
    await app.inject({ method: 'DELETE', url: `/api/alerts/${a.id}` })
    expect((await app.inject({ url: '/api/alerts/events' })).json()).toHaveLength(3)

    const cleared = await app.inject({ method: 'DELETE', url: '/api/alerts/events' })
    expect(cleared.statusCode).toBe(204)
    expect((await app.inject({ url: '/api/alerts/events' })).json()).toEqual([])
  })

  it('opens the shared tickers upstream only while an active alert exists', async () => {
    expect(SilentUpstream.count).toBe(0)
    const a = (await post(valid)).json<Alert>()
    expect(SilentUpstream.count).toBe(1)
    const health = (await app.inject({ url: '/api/health' })).json<{ upstreamSockets: number }>()
    expect(health.upstreamSockets).toBe(1)
    await patch(a.id, { active: false })
    await post({ ...valid, symbol: 'ETHUSDT' })
    // Re-acquired within the hub grace period: still one upstream socket.
    expect(SilentUpstream.count).toBe(1)
  })
})
