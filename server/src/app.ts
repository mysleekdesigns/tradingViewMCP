import websocket from '@fastify/websocket'
import Fastify, { type FastifyError, type FastifyInstance } from 'fastify'
import { DEFAULT_ALERT_COOLDOWN_SEC } from './alerts/evaluate.ts'
import type { AlertNotifier, DesktopNotifyMode } from './alerts/types.ts'
import type { Db } from './db/index.ts'
import alertsRoutes from './routes/alerts.ts'
import candlesRoutes from './routes/candles.ts'
import persistenceRoutes from './routes/persistence.ts'
import streamRoutes from './routes/stream.ts'
import symbolsRoutes from './routes/symbols.ts'
import { ApiError } from './types.ts'
import { createHub, type Hub } from './upstream/hub.ts'

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
  /**
   * Alert notifiers (desktop / webhook). Default: none, so tests never spawn processes or
   * reach the network. `index.ts` builds the real ones from config.
   */
  alertNotifier?: AlertNotifier
  /** Default `auto`: desktop notify only while no `/ws/alerts` client is attached. */
  desktopNotifyMode?: DesktopNotifyMode
  /** Minimum seconds between two triggers of the same `every` alert. Default 60. */
  alertCooldownSec?: number
}

export interface AppStats {
  upstreamSockets: number
}

export type RouteOptions = Required<Omit<AppDeps, 'db'>> & {
  db: Db
  stats: AppStats
  /** Shared upstream hub (stream routes and the alert engine). Closed on app close. */
  hub: Hub
}

export const DEFAULT_REST_BASE = 'https://api.binance.us'
export const DEFAULT_WS_BASE = 'wss://stream.binance.us:9443'

interface ErrorBody {
  error: string
  message: string
  retryAfter?: number
}

/** Host names (any port) the server answers to. Anything else is a DNS-rebinding attempt. */
const ALLOWED_HOSTNAMES = new Set(['localhost', '127.0.0.1', '[::1]'])

/**
 * True for a Host header naming this machine by loopback: `localhost`, `127.0.0.1` or `[::1]`,
 * with an optional numeric port (the Vite proxy forwards `localhost:5173` unchanged).
 * Missing, empty, repeated, or anything else is false.
 */
export function isAllowedHost(host: unknown): boolean {
  if (typeof host !== 'string') return false
  const m = /^(\[[^\]]*\]|[^:[\]]+)(?::(\d{1,5}))?$/.exec(host.trim().toLowerCase())
  if (!m) return false
  const [, name, port] = m
  if (port !== undefined && Number(port) > 65535) return false
  return name !== undefined && ALLOWED_HOSTNAMES.has(name)
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
  const WebSocketImpl = deps.WebSocketImpl ?? globalThis.WebSocket
  const wsBase = deps.wsBase ?? DEFAULT_WS_BASE
  const hub = createHub({ WebSocketImpl, wsBase, stats })
  app.addHook('onClose', async () => {
    hub.close()
  })
  const routeOptions: RouteOptions = {
    db: deps.db,
    fetch: deps.fetch ?? globalThis.fetch.bind(globalThis),
    WebSocketImpl,
    now: deps.now ?? Date.now,
    restBase: deps.restBase ?? DEFAULT_REST_BASE,
    wsBase,
    alertNotifier: deps.alertNotifier ?? {},
    desktopNotifyMode: deps.desktopNotifyMode ?? 'auto',
    alertCooldownSec: deps.alertCooldownSec ?? DEFAULT_ALERT_COOLDOWN_SEC,
    stats,
    hub,
  }

  await app.register(websocket)

  // DNS-rebinding guard: a page on evil.example whose name later resolves to 127.0.0.1 still
  // sends `Host: evil.example`. Runs before routing for every request, REST and WS upgrades.
  app.addHook('onRequest', async (request, reply) => {
    if (isAllowedHost(request.headers.host)) return
    return reply
      .status(403)
      .header('connection', 'close')
      .send({ error: 'http', message: 'host not allowed' } satisfies ErrorBody)
  })

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
  await app.register(alertsRoutes, routeOptions)

  return app
}
