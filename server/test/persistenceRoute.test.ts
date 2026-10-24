import type { FastifyInstance } from 'fastify'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { buildApp } from '../src/app.ts'
import { openDb, type Db } from '../src/db/index.ts'

const T0_MS = 1_700_000_000_123

describe('persistence routes', () => {
  let db: Db
  let app: FastifyInstance
  let nowMs: number

  beforeEach(async () => {
    nowMs = T0_MS
    db = openDb(':memory:')
    app = await buildApp({
      db,
      now: () => nowMs,
      fetch: () => Promise.reject(new Error('no network in tests')),
    })
    await app.ready()
  })

  afterEach(async () => {
    await app.close()
    db.close()
  })

  const put = (url: string, payload: unknown) =>
    app.inject({ method: 'PUT', url, payload: payload as object })
  const get = (url: string) => app.inject({ method: 'GET', url })

  describe('watchlists', () => {
    it('round-trips with uppercasing, trimming and order-preserving dedupe', async () => {
      const res = await put('/api/watchlists/default', {
        items: [' btcusdt', 'ETHUSDT', 'BTCUSDT', 'sol-usd ', 'ethusdt'],
        extra: 'ignored',
      })
      expect(res.statusCode).toBe(200)
      const expected = {
        id: 'default',
        items: ['BTCUSDT', 'ETHUSDT', 'SOL-USD'],
        updatedAt: Math.floor(T0_MS / 1000),
      }
      expect(res.json()).toEqual(expected)
      const back = await get('/api/watchlists/default')
      expect(back.statusCode).toBe(200)
      expect(back.json()).toEqual(expected)
    })

    it('stores updatedAt as UNIX seconds and updates on overwrite', async () => {
      await put('/api/watchlists/w1', { items: ['BTCUSDT'] })
      nowMs = T0_MS + 5_000
      const res = await put('/api/watchlists/w1', { items: ['ETHUSDT'] })
      expect(res.json()).toEqual({ id: 'w1', items: ['ETHUSDT'], updatedAt: 1_700_000_005 })
      const row = db.prepare('SELECT COUNT(*) AS n FROM watchlists').get() as { n: number }
      expect(row.n).toBe(1)
    })

    it('accepts an empty list and exactly 200 items', async () => {
      expect((await put('/api/watchlists/a', { items: [] })).statusCode).toBe(200)
      const items = Array.from({ length: 200 }, (_, i) => `SYM${i}`)
      const res = await put('/api/watchlists/a', { items })
      expect(res.statusCode).toBe(200)
      expect((res.json() as { items: string[] }).items).toHaveLength(200)
    })

    it('GET of a missing id is 404 http', async () => {
      const res = await get('/api/watchlists/nope')
      expect(res.statusCode).toBe(404)
      expect(res.json()).toMatchObject({ error: 'http' })
    })

    it.each([
      ['too many items', { items: Array.from({ length: 201 }, (_, i) => `S${i}`) }],
      ['items not an array', { items: 'BTCUSDT' }],
      ['missing items', {}],
      ['non-string item', { items: ['BTCUSDT', 5] }],
      ['bad symbol chars', { items: ['BTC USDT'] }],
      ['leading punctuation', { items: ['.BTC'] }],
      ['empty symbol', { items: ['  '] }],
      ['symbol too long', { items: ['A'.repeat(41)] }],
      ['array body', [1, 2]],
    ])('rejects %s with 400 http', async (_name, body) => {
      const res = await put('/api/watchlists/default', body)
      expect(res.statusCode).toBe(400)
      expect(res.json()).toMatchObject({ error: 'http' })
      expect((await get('/api/watchlists/default')).statusCode).toBe(404)
    })
  })

  describe('ids', () => {
    it.each(['a.b', 'a%20b', 'x'.repeat(65), "x'%3B%20DROP%20TABLE%20watchlists%3B--"])(
      'rejects id %s with 400',
      async (id) => {
        const res = await put(`/api/watchlists/${id}`, { items: ['BTCUSDT'] })
        expect(res.statusCode).toBe(400)
        expect(res.json()).toMatchObject({ error: 'http' })
        expect((await get(`/api/layouts/${id}`)).statusCode).toBe(400)
        expect(
          (await app.inject({ method: 'DELETE', url: `/api/drawings/${id}` })).statusCode,
        ).toBe(400)
      },
    )

    it('treats SQL-looking content as literal data, never executed', async () => {
      const id = 'x--DROP_TABLE_watchlists'
      expect((await put(`/api/watchlists/${id}`, { items: ['BTCUSDT'] })).statusCode).toBe(200)
      const evil = { note: "'); DROP TABLE layouts; --" }
      const res = await put('/api/layouts/default', { data: evil })
      expect(res.statusCode).toBe(200)
      expect((await get('/api/layouts/default')).json()).toMatchObject({ data: evil })
      expect((await get(`/api/watchlists/${id}`)).json()).toMatchObject({ id, items: ['BTCUSDT'] })
      const tables = db
        .prepare("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        .all() as { name: string }[]
      expect(tables.map((t) => t.name)).toEqual(
        expect.arrayContaining(['watchlists', 'layouts', 'drawings']),
      )
    })
  })

  describe('layouts', () => {
    it('round-trips a nested object and never stores extra fields', async () => {
      const data = { panes: [{ id: 'p1', h: 0.3 }], symbol: 'BTCUSDT', unicode: 'é' }
      const res = await put('/api/layouts/default', { data, extra: { secret: 1 } })
      expect(res.statusCode).toBe(200)
      const expected = { id: 'default', data, updatedAt: Math.floor(T0_MS / 1000) }
      expect(res.json()).toEqual(expected)
      expect((await get('/api/layouts/default')).json()).toEqual(expected)
      const row = db.prepare('SELECT data FROM layouts WHERE id = ?').get('default') as {
        data: string
      }
      expect(row.data).not.toContain('secret')
    })

    it('GET of a missing id is 404', async () => {
      expect((await get('/api/layouts/none')).statusCode).toBe(404)
    })

    it.each([
      ['array data', { data: [1, 2] }],
      ['null data', { data: null }],
      ['string data', { data: 'x' }],
      ['missing data', {}],
    ])('rejects %s with 400 http', async (_name, body) => {
      const res = await put('/api/layouts/default', body)
      expect(res.statusCode).toBe(400)
      expect(res.json()).toMatchObject({ error: 'http' })
    })

    it('rejects data over 256 KB with error http', async () => {
      // Just over the cap: passes the route bodyLimit, fails the data size check.
      const res = await put('/api/layouts/default', { data: { s: 'a'.repeat(256 * 1024) } })
      expect([400, 413]).toContain(res.statusCode)
      expect(res.json()).toMatchObject({ error: 'http' })
      // Far over the cap: rejected by Fastify's bodyLimit, still a clean JSON error.
      const big = await put('/api/layouts/default', { data: { s: 'a'.repeat(1024 * 1024) } })
      expect([400, 413]).toContain(big.statusCode)
      expect(big.json()).toMatchObject({ error: 'http' })
      expect((await get('/api/layouts/default')).statusCode).toBe(404)
    })

    it('accepts data just under 256 KB', async () => {
      const res = await put('/api/layouts/default', { data: { s: 'a'.repeat(250 * 1024) } })
      expect(res.statusCode).toBe(200)
    })

    it('corrupt stored JSON returns 500, server keeps working', async () => {
      db.prepare('INSERT INTO layouts (id, data, updated_at) VALUES (?, ?, ?)').run(
        'bad',
        '{oops',
        1,
      )
      const res = await get('/api/layouts/bad')
      expect(res.statusCode).toBe(500)
      expect(res.json()).toMatchObject({ error: 'http' })
      db.prepare('INSERT INTO watchlists (id, items, updated_at) VALUES (?, ?, ?)').run(
        'bad',
        '{"not":"array"}',
        1,
      )
      expect((await get('/api/watchlists/bad')).statusCode).toBe(500)
      expect((await get('/api/health')).statusCode).toBe(200)
    })
  })

  describe('drawings', () => {
    it('round-trips, filters by symbol, orders by updated_at then id', async () => {
      nowMs = 3_000_000
      await put('/api/drawings/b', { symbol: 'btcusdt', data: { kind: 'hline', price: 1 } })
      await put('/api/drawings/a', { symbol: 'BTCUSDT', data: { kind: 'hline', price: 2 } })
      nowMs = 1_000_000
      await put('/api/drawings/z', { symbol: 'BTCUSDT', data: { kind: 'trend' } })
      await put('/api/drawings/e', { symbol: 'ETHUSDT', data: { kind: 'trend' } })

      const res = await get('/api/drawings?symbol=btcusdt')
      expect(res.statusCode).toBe(200)
      expect(res.json()).toEqual([
        { id: 'z', symbol: 'BTCUSDT', data: { kind: 'trend' }, updatedAt: 1000 },
        { id: 'a', symbol: 'BTCUSDT', data: { kind: 'hline', price: 2 }, updatedAt: 3000 },
        { id: 'b', symbol: 'BTCUSDT', data: { kind: 'hline', price: 1 }, updatedAt: 3000 },
      ])
      expect((await get('/api/drawings?symbol=ETHUSDT')).json()).toHaveLength(1)
      expect((await get('/api/drawings?symbol=SOLUSDT')).json()).toEqual([])
    })

    it('PUT returns the stored object and can move a drawing to another symbol', async () => {
      const res = await put('/api/drawings/d1', { symbol: 'BTCUSDT', data: { x: 1 }, junk: 2 })
      expect(res.json()).toEqual({
        id: 'd1',
        symbol: 'BTCUSDT',
        data: { x: 1 },
        updatedAt: Math.floor(T0_MS / 1000),
      })
      await put('/api/drawings/d1', { symbol: 'ETHUSDT', data: { x: 2 } })
      expect((await get('/api/drawings?symbol=BTCUSDT')).json()).toEqual([])
      expect((await get('/api/drawings?symbol=ETHUSDT')).json()).toMatchObject([{ data: { x: 2 } }])
    })

    it('DELETE returns 204, then 404', async () => {
      await put('/api/drawings/d1', { symbol: 'BTCUSDT', data: {} })
      const del = await app.inject({ method: 'DELETE', url: '/api/drawings/d1' })
      expect(del.statusCode).toBe(204)
      expect(del.body).toBe('')
      expect((await get('/api/drawings?symbol=BTCUSDT')).json()).toEqual([])
      const again = await app.inject({ method: 'DELETE', url: '/api/drawings/d1' })
      expect(again.statusCode).toBe(404)
      expect(again.json()).toMatchObject({ error: 'http' })
    })

    it.each(['', '?symbol=', '?symbol=B', '?symbol=BTC-USD', `?symbol=${'A'.repeat(31)}`])(
      'GET with bad symbol query %s is 400',
      async (qs) => {
        const res = await get(`/api/drawings${qs}`)
        expect(res.statusCode).toBe(400)
        expect(res.json()).toMatchObject({ error: 'http' })
      },
    )

    it.each([
      ['missing symbol', { data: {} }],
      ['bad symbol', { symbol: 'BTC/USDT', data: {} }],
      ['array data', { symbol: 'BTCUSDT', data: [] }],
      ['null data', { symbol: 'BTCUSDT', data: null }],
    ])('PUT rejects %s with 400', async (_name, body) => {
      const res = await put('/api/drawings/d1', body)
      expect(res.statusCode).toBe(400)
      expect(res.json()).toMatchObject({ error: 'http' })
    })

    it('rejects drawing data over 64 KB', async () => {
      const res = await put('/api/drawings/d1', {
        symbol: 'BTCUSDT',
        data: { s: 'a'.repeat(64 * 1024) },
      })
      expect([400, 413]).toContain(res.statusCode)
      expect(res.json()).toMatchObject({ error: 'http' })
      const big = await put('/api/drawings/d1', {
        symbol: 'BTCUSDT',
        data: { s: 'a'.repeat(512 * 1024) },
      })
      expect([400, 413]).toContain(big.statusCode)
      expect(big.json()).toMatchObject({ error: 'http' })
      expect((await get('/api/drawings?symbol=BTCUSDT')).json()).toEqual([])
    })
  })
})
