/**
 * Client for the local server's price alerts (`server/README.md`, "Alerts"):
 * REST under `/api/alerts` and the push stream `GET /ws/alerts`.
 *
 * Alerts are evaluated on the server; this module only reads and edits alert definitions and
 * their trigger history. No keys, no exchange calls, no trading endpoints.
 */

import type { ConnectionStatus } from '../types'
import {
  ALERT_CONDITIONS,
  type Alert,
  type AlertCondition,
  type AlertEvent,
  type AlertFrequency,
  type AlertPatch,
  type AlertStreamMessage,
  type NewAlert,
} from './alertTypes'
import { DEFAULT_STALE_TIMEOUT_MS, openReconnectingSocket } from './binanceStream'
import { createLocalServerRequest, type LocalServerClientOptions } from './drawingsApi'
import { PersistenceError } from './persistence'
import { SERVER_FATAL_CLOSE_CODES } from './server'

export const ALERT_ID_RE = /^[A-Za-z0-9_-]{1,64}$/
/** Lenient exchange-symbol check for server data (the server owns the strict rule). */
const ALERT_SYMBOL_RE = /^[A-Z0-9][A-Z0-9._-]{0,39}$/
export const MAX_ALERT_NOTE_LENGTH = 200
export const DEFAULT_EVENTS_LIMIT = 100
export const ALERTS_WS_PATH = '/ws/alerts'

export interface AlertsClient {
  /** All alerts, newest first. Malformed rows are skipped. */
  list(): Promise<Alert[]>
  create(a: NewAlert): Promise<Alert>
  update(id: string, patch: AlertPatch): Promise<Alert>
  /** A 404 (already deleted, e.g. from another tab) counts as success. */
  remove(id: string): Promise<void>
  /** Trigger history, newest first. Malformed rows are skipped. */
  listEvents(limit?: number): Promise<AlertEvent[]>
  clearEvents(): Promise<void>
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function isFiniteNumber(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

function isCondition(v: unknown): v is AlertCondition {
  return typeof v === 'string' && (ALERT_CONDITIONS as readonly string[]).includes(v)
}

function isFrequency(v: unknown): v is AlertFrequency {
  return v === 'once' || v === 'every'
}

function isSymbol(v: unknown): v is string {
  return typeof v === 'string' && ALERT_SYMBOL_RE.test(v)
}

/** A server Alert, or null when any field is missing or out of range. A missing note reads ''. */
export function parseAlert(v: unknown): Alert | null {
  if (!isRecord(v)) return null
  const { id, symbol, condition, price, frequency, active, createdAt, updatedAt } = v
  if (typeof id !== 'string' || !ALERT_ID_RE.test(id)) return null
  if (!isSymbol(symbol) || !isCondition(condition) || !isFrequency(frequency)) return null
  if (!isFiniteNumber(price) || price <= 0) return null
  if (typeof active !== 'boolean') return null
  if (!isFiniteNumber(createdAt) || !isFiniteNumber(updatedAt)) return null
  let note = ''
  if (typeof v.note === 'string') note = v.note.slice(0, MAX_ALERT_NOTE_LENGTH)
  else if (v.note !== undefined && v.note !== null) return null
  let lastTriggeredAt: number | null = null
  if (isFiniteNumber(v.lastTriggeredAt)) lastTriggeredAt = v.lastTriggeredAt
  else if (v.lastTriggeredAt !== null && v.lastTriggeredAt !== undefined) return null
  return {
    id,
    symbol,
    condition,
    price,
    frequency,
    active,
    note,
    createdAt,
    updatedAt,
    lastTriggeredAt,
  }
}

/** A server AlertEvent, or null when malformed. */
export function parseAlertEvent(v: unknown): AlertEvent | null {
  if (!isRecord(v)) return null
  const { id, alertId, symbol, condition, level, price, triggeredAt, message } = v
  if (!isFiniteNumber(id) || !Number.isInteger(id)) return null
  if (typeof alertId !== 'string' || !ALERT_ID_RE.test(alertId)) return null
  if (!isSymbol(symbol) || !isCondition(condition)) return null
  if (!isFiniteNumber(level) || !isFiniteNumber(price) || !isFiniteNumber(triggeredAt)) return null
  if (typeof message !== 'string') return null
  return { id, alertId, symbol, condition, level, price, triggeredAt, message }
}

/** One `/ws/alerts` message (JSON string or parsed object), or null when malformed. Never throws. */
export function parseAlertStreamMessage(data: unknown): AlertStreamMessage | null {
  let msg: unknown = data
  if (typeof msg === 'string') {
    try {
      msg = JSON.parse(msg)
    } catch {
      return null
    }
  }
  if (!isRecord(msg)) return null
  switch (msg.type) {
    case 'heartbeat':
      return { type: 'heartbeat' }
    case 'changed':
      return { type: 'changed' }
    case 'triggered': {
      const event = parseAlertEvent(msg.event)
      const alert = parseAlert(msg.alert)
      if (event === null || alert === null || event.alertId !== alert.id) return null
      return { type: 'triggered', event, alert }
    }
    default:
      return null
  }
}

function parseList<T>(body: unknown, parse: (v: unknown) => T | null, what: string): T[] {
  if (!Array.isArray(body)) {
    throw new PersistenceError('parse', `The local server sent an unreadable ${what} list`)
  }
  const out: T[] = []
  for (const row of body) {
    const item = parse(row)
    if (item !== null) out.push(item)
  }
  return out
}

function parseOne(body: unknown): Alert {
  const alert = parseAlert(body)
  if (alert === null)
    throw new PersistenceError('parse', 'The local server sent an unreadable alert')
  return alert
}

function alertPath(id: string): string {
  if (!ALERT_ID_RE.test(id)) throw new PersistenceError('bad-id', `Invalid alert id: ${id}`)
  return `/api/alerts/${encodeURIComponent(id)}`
}

/**
 * The POST body: only the keys the server accepts (it rejects unknown fields with a 400).
 * `note` is omitted when undefined.
 */
export function pickNewAlert(a: NewAlert): NewAlert {
  const body: NewAlert = {
    symbol: a.symbol,
    condition: a.condition,
    price: a.price,
    frequency: a.frequency,
  }
  if (a.note !== undefined) body.note = a.note
  return body
}

const ALERT_PATCH_KEYS = ['condition', 'price', 'frequency', 'active', 'note'] as const

/** The PATCH body: only the AlertPatch keys that are present (not undefined). */
export function pickAlertPatch(patch: AlertPatch): AlertPatch {
  const body: Record<string, unknown> = {}
  for (const k of ALERT_PATCH_KEYS) {
    if (patch[k] !== undefined) body[k] = patch[k]
  }
  return body as AlertPatch
}

export function createAlertsClient(opts: LocalServerClientOptions = {}): AlertsClient {
  const request = createLocalServerRequest(opts)
  return {
    async list() {
      const { body } = await request('/api/alerts', { method: 'GET' })
      return parseList(body, parseAlert, 'alert')
    },
    async create(a) {
      const { body } = await request('/api/alerts', { method: 'POST', body: pickNewAlert(a) })
      return parseOne(body)
    },
    async update(id, patch) {
      const { body } = await request(alertPath(id), {
        method: 'PATCH',
        body: pickAlertPatch(patch),
      })
      return parseOne(body)
    },
    async remove(id) {
      await request(alertPath(id), { method: 'DELETE', okStatuses: [404] })
    },
    async listEvents(limit = DEFAULT_EVENTS_LIMIT) {
      const n = Number.isFinite(limit) ? Math.min(1000, Math.max(1, Math.floor(limit))) : 100
      const { body } = await request(`/api/alerts/events?limit=${n}`, { method: 'GET' })
      return parseList(body, parseAlertEvent, 'alert history')
    },
    async clearEvents() {
      await request('/api/alerts/events', { method: 'DELETE' })
    },
  }
}

/** Same-origin `ws(s)://host/ws/alerts` (the Vite proxy forwards `/ws`), or null without a page. */
export function defaultAlertStreamUrl(): string | null {
  if (typeof location === 'undefined' || typeof location.host !== 'string') return null
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${scheme}://${location.host}${ALERTS_WS_PATH}`
}

export interface AlertStreamOptions {
  onMessage: (m: AlertStreamMessage) => void
  onStatus?: (s: ConnectionStatus) => void
  /** Defaults to the global WebSocket. */
  WebSocketImpl?: typeof WebSocket
  /** Defaults to `defaultAlertStreamUrl()`. */
  url?: string
  /** Defaults to DEFAULT_STALE_TIMEOUT_MS (60 s; the server heartbeats every 20 s). */
  staleTimeoutMs?: number
}

/**
 * Subscribe to `/ws/alerts`. Reconnects by itself (jittered backoff, 60 s stale timeout), reads
 * 'live' on the first message (the server heartbeats on attach), and stops for good on close
 * code 1008. Malformed messages are dropped. Without a WebSocket or URL it reports 'offline'
 * and returns a no-op. Returns an idempotent teardown; no callback runs after it.
 */
export function openAlertStream(opts: AlertStreamOptions): () => void {
  const WS = opts.WebSocketImpl ?? (globalThis as { WebSocket?: typeof WebSocket }).WebSocket
  const url = opts.url ?? defaultAlertStreamUrl()
  if (typeof WS !== 'function' || url === null) {
    opts.onStatus?.('offline')
    return () => {}
  }
  let disposed = false
  const teardown = openReconnectingSocket({
    WebSocketImpl: WS,
    url,
    staleTimeoutMs: opts.staleTimeoutMs ?? DEFAULT_STALE_TIMEOUT_MS,
    fatalCloseCodes: SERVER_FATAL_CLOSE_CODES,
    liveOnFirstMessage: true,
    onStatus: (s) => {
      if (!disposed) opts.onStatus?.(s)
    },
    onMessage: (data) => {
      if (disposed) return
      const msg = parseAlertStreamMessage(data)
      if (msg === null) return
      try {
        opts.onMessage(msg)
      } catch (err) {
        console.error('openAlertStream: onMessage threw', err)
      }
    },
  })
  return () => {
    if (disposed) return
    disposed = true
    teardown()
  }
}
