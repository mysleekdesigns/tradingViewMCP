import { randomUUID } from 'node:crypto'
import type { FastifyInstance, FastifyRequest } from 'fastify'
import type { RouteOptions } from '../app.ts'
import { createAlertEngine } from '../alerts/engine.ts'
import { createAlertStore, MAX_ALERT_EVENTS, type AlertUpdate } from '../alerts/store.ts'
import {
  isAlertCondition,
  isAlertFrequency,
  type Alert,
  type AlertEvent,
  type AlertStreamMessage,
  type NewAlert,
} from '../alerts/types.ts'
import { ApiError } from '../types.ts'
import { isAllowedOrigin, MAX_BUFFERED_BYTES, PING_INTERVAL_MS } from './stream.ts'

// REST + WebSocket for price alerts. Contract: server/README.md "Alerts". Notification only.

export const MAX_ALERTS = 500
export const MAX_NOTE_CHARS = 200
export const DEFAULT_EVENTS_LIMIT = 100
export const MAX_EVENTS_LIMIT = MAX_ALERT_EVENTS
const BODY_LIMIT = 16 * 1024
const ID_RE = /^[A-Za-z0-9_-]{1,64}$/
const SYMBOL_RE = /^[A-Z0-9]{2,30}$/
const WS_OPEN = 1
const NEW_KEYS = new Set(['symbol', 'condition', 'price', 'frequency', 'note'])
const PATCH_KEYS = new Set(['condition', 'price', 'frequency', 'active', 'note'])

/** The subset of the `ws` server-side socket this route uses. */
interface ClientSocket {
  readonly readyState: number
  readonly bufferedAmount: number
  send(data: string): void
  ping(): void
  close(code?: number, reason?: string): void
  terminate(): void
  on(event: 'close' | 'error' | 'message', listener: (...args: unknown[]) => void): unknown
}

function badRequest(message: string): ApiError {
  return new ApiError('http', message, 400)
}

function notFound(message: string): ApiError {
  return new ApiError('http', message, 404)
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function bodyObject(body: unknown, allowed: ReadonlySet<string>): Record<string, unknown> {
  if (!isPlainObject(body)) throw badRequest('body must be a JSON object')
  for (const k of Object.keys(body)) {
    if (!allowed.has(k)) throw badRequest(`unknown field: ${JSON.stringify(k).slice(0, 80)}`)
  }
  return body
}

function parseId(params: unknown): string {
  const id = (params as { id?: unknown } | undefined)?.id
  if (typeof id !== 'string' || !ID_RE.test(id)) {
    throw badRequest('id must match ^[A-Za-z0-9_-]{1,64}$')
  }
  return id
}

export function parseSymbol(raw: unknown): string {
  if (typeof raw !== 'string') throw badRequest('symbol is required')
  const s = raw.trim().toUpperCase()
  if (!SYMBOL_RE.test(s)) throw badRequest('symbol must match ^[A-Z0-9]{2,30}$')
  return s
}

function parsePrice(raw: unknown): number {
  if (typeof raw !== 'number' || !Number.isFinite(raw) || raw <= 0) {
    throw badRequest('price must be a finite number > 0')
  }
  return raw
}

function parseCondition(raw: unknown): Alert['condition'] {
  if (!isAlertCondition(raw)) {
    throw badRequest('condition must be crosses_above, crosses_below, greater or less')
  }
  return raw
}

function parseFrequency(raw: unknown): Alert['frequency'] {
  if (!isAlertFrequency(raw)) throw badRequest('frequency must be once or every')
  return raw
}

function parseNote(raw: unknown): string {
  if (typeof raw !== 'string') throw badRequest('note must be a string')
  const n = raw.trim()
  if (n.length > MAX_NOTE_CHARS) throw badRequest(`note must be at most ${MAX_NOTE_CHARS} chars`)
  return n
}

/** Validates a `POST /api/alerts` body. Unknown fields are rejected. */
export function parseNewAlert(body: unknown): Required<NewAlert> {
  const b = bodyObject(body, NEW_KEYS)
  return {
    symbol: parseSymbol(b.symbol),
    condition: parseCondition(b.condition),
    price: parsePrice(b.price),
    frequency: parseFrequency(b.frequency),
    note: b.note === undefined ? '' : parseNote(b.note),
  }
}

/** Validates a `PATCH /api/alerts/:id` body: a non-empty subset of the patchable fields. */
export function parseAlertPatch(body: unknown): AlertUpdate {
  const b = bodyObject(body, PATCH_KEYS)
  if (Object.keys(b).length === 0) throw badRequest('patch body must not be empty')
  const out: AlertUpdate = {}
  if (b.condition !== undefined) out.condition = parseCondition(b.condition)
  if (b.price !== undefined) out.price = parsePrice(b.price)
  if (b.frequency !== undefined) out.frequency = parseFrequency(b.frequency)
  if (b.note !== undefined) out.note = parseNote(b.note)
  if (b.active !== undefined) {
    if (typeof b.active !== 'boolean') throw badRequest('active must be a boolean')
    out.active = b.active
  }
  if (Object.keys(out).length === 0) throw badRequest('patch body must not be empty')
  return out
}

function parseLimit(raw: unknown): number {
  if (raw === undefined) return DEFAULT_EVENTS_LIMIT
  if (typeof raw !== 'string' || !/^\d{1,6}$/.test(raw.trim())) {
    throw badRequest('limit must be an integer')
  }
  const n = Number(raw.trim())
  if (n < 1 || n > MAX_EVENTS_LIMIT) throw badRequest(`limit must be 1..${MAX_EVENTS_LIMIT}`)
  return n
}

export default async function alertsRoutes(
  app: FastifyInstance,
  opts: RouteOptions,
): Promise<void> {
  const store = createAlertStore(opts.db)
  const engine = createAlertEngine({
    store,
    hub: opts.hub,
    now: opts.now,
    cooldownSec: opts.alertCooldownSec,
    notifier: opts.alertNotifier,
    desktopMode: opts.desktopNotifyMode,
    log: {
      warn: (obj, msg) => app.log.warn(obj, msg),
    },
  })
  const nowSec = (): number => Math.floor(opts.now() / 1000)
  const changed = (): void => engine.broadcast({ type: 'changed' })

  /** Per-socket cleanups, so app.close() never leaves heartbeat timers behind. */
  const sockets = new Set<() => void>()
  app.addHook('onClose', async () => {
    for (const c of [...sockets]) c()
    engine.close()
  })

  // Persisted active alerts from a previous run start being watched immediately.
  engine.sync()

  // --- history (static paths win over /:id in find-my-way) -------------------------------

  app.get('/api/alerts/events', async (request): Promise<AlertEvent[]> => {
    const limit = parseLimit((request.query as { limit?: unknown } | undefined)?.limit)
    return store.listEvents(limit)
  })

  app.delete('/api/alerts/events', async (_request, reply) => {
    store.clearEvents()
    changed()
    return reply.status(204).send()
  })

  // --- alerts ----------------------------------------------------------------------------

  app.get('/api/alerts', async (request): Promise<Alert[]> => {
    const raw = (request.query as { symbol?: unknown } | undefined)?.symbol
    return store.list(raw === undefined ? undefined : parseSymbol(raw))
  })

  app.post('/api/alerts', { bodyLimit: BODY_LIMIT }, async (request, reply) => {
    const input = parseNewAlert(request.body)
    if (store.count() >= MAX_ALERTS) {
      throw new ApiError('http', `at most ${MAX_ALERTS} alerts`, 409)
    }
    const t = nowSec()
    const alert: Alert = {
      id: randomUUID(),
      symbol: input.symbol,
      condition: input.condition,
      price: input.price,
      frequency: input.frequency,
      active: true,
      note: input.note,
      createdAt: t,
      updatedAt: t,
      lastTriggeredAt: null,
    }
    store.insert(alert)
    engine.sync()
    changed()
    return reply.status(201).send(alert)
  })

  app.patch('/api/alerts/:id', { bodyLimit: BODY_LIMIT }, async (request): Promise<Alert> => {
    const id = parseId(request.params)
    const patch = parseAlertPatch(request.body)
    const updated = store.update(id, patch, nowSec())
    if (!updated) throw notFound(`alert ${id} not found`)
    if (patch.active === true || patch.price !== undefined || patch.condition !== undefined) {
      engine.reset(id)
    }
    engine.sync()
    changed()
    return updated
  })

  app.delete('/api/alerts/:id', async (request, reply) => {
    const id = parseId(request.params)
    if (!store.delete(id)) throw notFound(`alert ${id} not found`)
    engine.reset(id)
    engine.sync()
    changed()
    return reply.status(204).send()
  })

  // --- /ws/alerts ------------------------------------------------------------------------

  app.get('/ws/alerts', { websocket: true }, (socket: ClientSocket, req: FastifyRequest) => {
    if (!isAllowedOrigin(req.headers.origin)) {
      socket.close(1008, 'origin not allowed')
      return
    }
    let detach: () => void = () => {}
    let done = false
    const cleanup = (): void => {
      if (done) return
      done = true
      clearInterval(pinger)
      sockets.delete(cleanup)
      detach()
    }
    const send = (msg: AlertStreamMessage): void => {
      if (done || socket.readyState !== WS_OPEN) return
      if (socket.bufferedAmount > MAX_BUFFERED_BYTES) {
        cleanup()
        try {
          socket.terminate()
        } catch {
          // ignore
        }
        return
      }
      try {
        socket.send(JSON.stringify(msg))
      } catch {
        // Socket is going away; 'close' will clean up.
      }
    }
    const pinger = setInterval(() => {
      if (done || socket.readyState !== WS_OPEN) return
      try {
        socket.ping()
      } catch {
        // 'close' will clean up.
      }
      send({ type: 'heartbeat' })
    }, PING_INTERVAL_MS)
    sockets.add(cleanup)
    socket.on('close', cleanup)
    socket.on('error', cleanup)
    socket.on('message', () => {
      // Clients never send anything; ignore.
    })
    detach = engine.subscribe(send)
    if (done) {
      detach()
      return
    }
    send({ type: 'heartbeat' })
  })
}
