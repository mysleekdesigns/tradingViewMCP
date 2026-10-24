import type { FastifyInstance } from 'fastify'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { buildApp } from '../src/app.ts'
import { openDb, type Db } from '../src/db/index.ts'
import { ApiError } from '../src/types.ts'

describe('app', () => {
  let db: Db
  let app: FastifyInstance

  beforeEach(async () => {
    db = openDb(':memory:')
    app = await buildApp({ db, fetch: () => Promise.reject(new Error('no network in tests')) })
    app.get('/test/api-error', async () => {
      throw new ApiError('rate-limited', 'slow down', 429, 7)
    })
    app.get('/test/api-error-plain', async () => {
      throw new ApiError('region-blocked', 'blocked', 503)
    })
    app.get('/test/boom', async () => {
      throw new Error('secret internal detail')
    })
    app.get(
      '/test/validated',
      {
        schema: {
          querystring: { type: 'object', required: ['n'], properties: { n: { type: 'integer' } } },
        },
      },
      async () => ({ ok: true }),
    )
    await app.ready()
  })

  afterEach(async () => {
    await app.close()
    db.close()
  })

  it('GET /api/health answers ok with a working db', async () => {
    const res = await app.inject({ method: 'GET', url: '/api/health' })
    expect(res.statusCode).toBe(200)
    expect(res.json()).toEqual({
      ok: true,
      db: true,
      upstreamSockets: 0,
      venue: { id: 'binance', name: 'Binance' },
    })
  })

  it('reports db false when the database is unusable', async () => {
    db.close()
    const res = await app.inject({ method: 'GET', url: '/api/health' })
    expect(res.statusCode).toBe(200)
    expect(res.json()).toMatchObject({ ok: true, db: false })
    db = openDb(':memory:') // so afterEach can close something
  })

  it('serialises ApiError with status, body, and Retry-After', async () => {
    const res = await app.inject({ method: 'GET', url: '/test/api-error' })
    expect(res.statusCode).toBe(429)
    expect(res.headers['retry-after']).toBe('7')
    expect(res.json()).toEqual({ error: 'rate-limited', message: 'slow down', retryAfter: 7 })
  })

  it('omits retryAfter and Retry-After when not set', async () => {
    const res = await app.inject({ method: 'GET', url: '/test/api-error-plain' })
    expect(res.statusCode).toBe(503)
    expect(res.headers['retry-after']).toBeUndefined()
    expect(res.json()).toEqual({ error: 'region-blocked', message: 'blocked' })
  })

  it('turns validation errors into 400 http', async () => {
    const res = await app.inject({ method: 'GET', url: '/test/validated?n=abc' })
    expect(res.statusCode).toBe(400)
    const body = res.json<{ error: string; message: string }>()
    expect(body.error).toBe('http')
    expect(typeof body.message).toBe('string')
  })

  it('hides unknown errors behind a 500', async () => {
    const res = await app.inject({ method: 'GET', url: '/test/boom' })
    expect(res.statusCode).toBe(500)
    expect(res.json()).toEqual({ error: 'http', message: 'internal error' })
  })

  it('returns 404 http for unknown routes', async () => {
    const res = await app.inject({ method: 'GET', url: '/api/nope' })
    expect(res.statusCode).toBe(404)
    expect(res.json()).toMatchObject({ error: 'http' })
  })
})
