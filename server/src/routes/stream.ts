import type { FastifyInstance, FastifyRequest } from 'fastify'
import type { RouteOptions } from '../app.ts'
import { isInterval } from '../types.ts'
import { createHub, klineKey, TICKERS_KEY, type HubEvent, type StreamKey } from '../upstream/hub.ts'

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

export default async function streamRoutes(
  app: FastifyInstance,
  opts: RouteOptions,
): Promise<void> {
  const hub = createHub({
    WebSocketImpl: opts.WebSocketImpl,
    wsBase: opts.wsBase,
    stats: opts.stats,
  })
  app.addHook('onClose', async () => {
    hub.close()
  })

  const attach = (socket: ClientSocket, key: StreamKey): void => {
    if (hub.status(key) === undefined && hub.size >= MAX_UPSTREAM_KEYS) {
      socket.close(1013, 'too many streams')
      return
    }
    let release: () => void = () => {}
    let done = false
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
    socket.on('message', () => {
      // Clients never send anything; ignore.
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
      try {
        socket.send(serialize(event))
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
    attach(socket, TICKERS_KEY)
  })
}
