import websocket from '@fastify/websocket'
import Fastify, { type FastifyError, type FastifyInstance } from 'fastify'
import type { Db } from './db/index.ts'
import candlesRoutes from './routes/candles.ts'
import persistenceRoutes from './routes/persistence.ts'
import streamRoutes from './routes/stream.ts'
import symbolsRoutes from './routes/symbols.ts'
import { ApiError } from './types.ts'

export interface AppDeps {
  db: Db
  /** Upstream REST fetch; tests inject fakes. Default `globalThis.fetch`. */
  fetch?: typeof fetch
  /** Upstream WebSocket constructor. Default `globalThis.WebSocket`. */
  WebSocketImpl?: typeof WebSocket
  /** Millisecond clock. Default `Date.now`. */
  now?: () => number
  /** Default `https://api.binance.us`. */
  restBase?: string
  /** Default `wss://stream.binance.us:9443`. */
  wsBase?: string
}

export interface AppStats {
  upstreamSockets: number
}

export type RouteOptions = Required<Omit<AppDeps, 'db'>> & { db: Db; stats: AppStats }

export const DEFAULT_REST_BASE = 'https://api.binance.us'
export const DEFAULT_WS_BASE = 'wss://stream.binance.us:9443'

interface ErrorBody {
  error: string
  message: string
  retryAfter?: number
}

function isFastifyError(err: unknown): err is FastifyError {
  return err instanceof Error && 'code' in err
}

export async function buildApp(
  deps: AppDeps,
  opts: { logger?: boolean } = {},
): Promise<FastifyInstance> {
  const app = Fastify({ logger: opts.logger ?? false })

  const stats: AppStats = { upstreamSockets: 0 }
  const routeOptions: RouteOptions = {
    db: deps.db,
    fetch: deps.fetch ?? globalThis.fetch.bind(globalThis),
    WebSocketImpl: deps.WebSocketImpl ?? globalThis.WebSocket,
    now: deps.now ?? Date.now,
    restBase: deps.restBase ?? DEFAULT_REST_BASE,
    wsBase: deps.wsBase ?? DEFAULT_WS_BASE,
    stats,
  }

  await app.register(websocket)

  app.setErrorHandler((err: unknown, request, reply) => {
    if (err instanceof ApiError) {
      const body: ErrorBody = { error: err.kind, message: err.message }
      if (err.retryAfter !== undefined) {
        body.retryAfter = err.retryAfter
        void reply.header('Retry-After', String(Math.ceil(err.retryAfter)))
      }
      return reply.status(err.status).send(body)
    }
    if (isFastifyError(err) && err.validation) {
      return reply.status(400).send({ error: 'http', message: err.message } satisfies ErrorBody)
    }
    // Other Fastify client errors (bad JSON body, payload too large, ...) keep their 4xx status.
    if (
      isFastifyError(err) &&
      typeof err.statusCode === 'number' &&
      err.statusCode >= 400 &&
      err.statusCode < 500
    ) {
      return reply
        .status(err.statusCode)
        .send({ error: 'http', message: err.message } satisfies ErrorBody)
    }
    request.log.error({ err }, 'unhandled error')
    return reply.status(500).send({ error: 'http', message: 'internal error' } satisfies ErrorBody)
  })

  app.setNotFoundHandler((request, reply) => {
    return reply
      .status(404)
      .send({ error: 'http', message: `route ${request.method} ${request.url} not found` })
  })

  const ping = deps.db.prepare<[], { ok: number }>('SELECT 1 AS ok')
  app.get('/api/health', async () => {
    let db: boolean
    try {
      db = ping.get()?.ok === 1
    } catch {
      db = false
    }
    return { ok: true, db, upstreamSockets: stats.upstreamSockets }
  })

  await app.register(candlesRoutes, routeOptions)
  await app.register(symbolsRoutes, routeOptions)
  await app.register(streamRoutes, routeOptions)
  await app.register(persistenceRoutes, routeOptions)

  return app
}
