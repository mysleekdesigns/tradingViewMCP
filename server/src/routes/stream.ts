import type { FastifyInstance, FastifyRequest } from 'fastify'
import type { RouteOptions } from '../app.ts'
import { isInterval } from '../types.ts'
import { klineKey, TICKERS_KEY, type HubEvent, type StreamKey } from '../upstream/hub.ts'

// WebSocket fan-out. Contract: server/README.md "WebSocket fan-out".

/** Protocol ping plus app-level status heartbeat (browsers cannot see pings). */
export const PING_INTERVAL_MS = 20_000
/** A client whose send buffer exceeds this is too slow; drop it (terminate). */
export const MAX_BUFFERED_BYTES = 1024 * 1024
/** Distinct upstream streams the hub may hold; a new key beyond this closes 1013. */
export const MAX_UPSTREAM_KEYS = 50
const ALLOWED_ORIGIN_HOSTS = new Set(['localhost', '127.0.0.1'])
const WS_OPEN = 1
const SYMBOL_RE = /^[A-Z0-9]{2,30}$/
/** Most symbols a `/ws/tickers` `watch` message may name; a longer list is ignored. */
export const MAX_WATCH_SYMBOLS = 500
const WATCH_SYMBOL_RE = /^[A-Z0-9]{2,20}$/
/** Client frames larger than this are ignored unparsed (500 symbols fit in ~12 KB). */
export const MAX_CLIENT_MESSAGE_BYTES = 32 * 1024

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

/** No Origin (non-browser tool) or an http Origin on localhost / 127.0.0.1, any port. */
export function isAllowedOrigin(origin: string | string[] | undefined): boolean {
  if (origin === undefined) return true
  if (typeof origin !== 'string') return false
  try {
    const u = new URL(origin)
    return u.protocol === 'http:' && ALLOWED_ORIGIN_HOSTS.has(u.hostname)
  } catch {
    return false
  }
}

/** Serialize each hub event once, however many clients it fans out to. */
const serialized = new WeakMap<HubEvent, string>()
function serialize(event: HubEvent): string {
  let s = serialized.get(event)
  if (s === undefined) {
    s = JSON.stringify(event)
    serialized.set(event, s)
  }
  return s
}

/** A `/ws/tickers` client's watched symbols; `key` is canonical (sorted, comma-joined). */
export interface Watch {
  readonly symbols: ReadonlySet<string>
  readonly key: string
}

/**
 * A `/ws/tickers` client message: `{"type":"watch","symbols":[...]}` -> that set (strings that
 * are not `[A-Z0-9]{2,20}` are skipped: no upstream symbol can match them), `{"type":"unwatch"}`
 * -> null (every symbol again). Anything else (not JSON, too large, binary junk, a non-array or
 * more than MAX_WATCH_SYMBOLS entries, a non-string entry) -> undefined: ignored. Never throws.
 */
export function parseWatchMessage(data: unknown): Watch | null | undefined {
  let text: string
  if (typeof data === 'string') text = data
  else if (Buffer.isBuffer(data)) {
    if (data.length > MAX_CLIENT_MESSAGE_BYTES) return undefined
    text = data.toString('utf8')
  } else if (data instanceof ArrayBuffer) {
    if (data.byteLength > MAX_CLIENT_MESSAGE_BYTES) return undefined
    text = Buffer.from(data).toString('utf8')
  } else if (Array.isArray(data) && data.every((d) => Buffer.isBuffer(d))) {
    const total = data.reduce((n, d) => n + d.length, 0)
    if (total > MAX_CLIENT_MESSAGE_BYTES) return undefined
    text = Buffer.concat(data).toString('utf8')
  } else return undefined
  if (text.length > MAX_CLIENT_MESSAGE_BYTES) return undefined
  let msg: unknown
  try {
    msg = JSON.parse(text)
  } catch {
    return undefined
  }
  if (msg === null || typeof msg !== 'object' || Array.isArray(msg)) return undefined
  const { type, symbols } = msg as { type?: unknown; symbols?: unknown }
  if (type === 'unwatch') return null
  if (type !== 'watch' || !Array.isArray(symbols) || symbols.length > MAX_WATCH_SYMBOLS) {
    return undefined
  }
  const set = new Set<string>()
  for (const s of symbols as unknown[]) {
    if (typeof s !== 'string') return undefined
    if (WATCH_SYMBOL_RE.test(s)) set.add(s)
  }
  return { symbols: set, key: [...set].sort().join(',') }
}

type TickersEvent = Extract<HubEvent, { type: 'tickers' }>

/**
 * A tickers event narrowed to one watched set, serialized once per (event, distinct set) however
 * many clients share the set. null: none of the set changed in this push (nothing to send).
 */
const watchedSerialized = new WeakMap<TickersEvent, Map<string, string | null>>()
export function serializeWatched(event: TickersEvent, watch: Watch): string | null {
  let byKey = watchedSerialized.get(event)
  if (byKey === undefined) {
    byKey = new Map()
    watchedSerialized.set(event, byKey)
  }
  let s = byKey.get(watch.key)
  if (s === undefined) {
    const tickers = event.tickers.filter((t) => watch.symbols.has(t.symbol))
    s = tickers.length === 0 ? null : JSON.stringify({ type: 'tickers', tickers })
    byKey.set(watch.key, s)
  }
  return s
}

export default async function streamRoutes(
  app: FastifyInstance,
  opts: RouteOptions,
): Promise<void> {
  // Shared with the alert engine; created and closed by buildApp.
  const { hub } = opts

  /** `watchable`: the client may narrow tickers events with a `watch` message (`/ws/tickers`). */
  const attach = (socket: ClientSocket, key: StreamKey, watchable = false): void => {
    if (hub.status(key) === undefined && hub.size >= MAX_UPSTREAM_KEYS) {
      socket.close(1013, 'too many streams')
      return
    }
    let release: () => void = () => {}
    let done = false
    /** Until the client sends a `watch`, it gets every symbol (older clients never send one). */
    let watch: Watch | null = null
    const cleanup = (): void => {
      if (done) return
      done = true
      clearInterval(pinger)
      release()
    }
    const pinger = setInterval(() => {
      if (done || socket.readyState !== WS_OPEN) return
      try {
        socket.ping()
      } catch {
        // Socket is going away; 'close' will clean up.
      }
      // App-level heartbeat: the browser's stale timer only sees messages, not pings.
      const status = hub.status(key)
      if (status !== undefined) send({ type: 'status', status })
    }, PING_INTERVAL_MS)

    socket.on('close', cleanup)
    socket.on('error', cleanup)
    socket.on('message', (data: unknown) => {
      // Only `watch`/`unwatch` on /ws/tickers means anything; every other frame is ignored.
      if (!watchable || done) return
      const next = parseWatchMessage(data)
      if (next !== undefined) watch = next
    })

    const send = (event: HubEvent): void => {
      if (done || socket.readyState !== WS_OPEN) return
      if (socket.bufferedAmount > MAX_BUFFERED_BYTES) {
        cleanup()
        try {
          // A graceful close would queue behind the full buffer; drop the socket outright.
          socket.terminate()
        } catch {
          // ignore
        }
        return
      }
      let payload: string | null
      if (watch !== null && event.type === 'tickers') payload = serializeWatched(event, watch)
      else payload = serialize(event)
      if (payload === null) return
      try {
        socket.send(payload)
      } catch {
        // Socket is going away; 'close' will clean up.
      }
    }
    release = hub.acquire(key, send)
    if (done) release()
  }

  const reject = (socket: ClientSocket, reason: string): void => {
    socket.close(1008, reason)
  }

  const originOk = (socket: ClientSocket, req: FastifyRequest): boolean => {
    if (isAllowedOrigin(req.headers.origin)) return true
    reject(socket, 'origin not allowed')
    return false
  }

  app.get('/ws/klines', { websocket: true }, (socket: ClientSocket, req) => {
    if (!originOk(socket, req)) return
    const q = (req.query ?? {}) as Record<string, unknown>
    const rawSymbol = typeof q.symbol === 'string' ? q.symbol.trim().toUpperCase() : ''
    if (!SYMBOL_RE.test(rawSymbol)) {
      reject(socket, 'invalid symbol')
      return
    }
    if (!isInterval(q.interval)) {
      reject(socket, 'invalid interval')
      return
    }
    attach(socket, klineKey(rawSymbol, q.interval))
  })

  app.get('/ws/tickers', { websocket: true }, (socket: ClientSocket, req) => {
    if (!originOk(socket, req)) return
    attach(socket, TICKERS_KEY, true)
  })
}
