// Upstream WebSocket manager: one Binance(.US) socket per stream key, shared by every
// listener (browser client), reconnecting with jittered exponential backoff and stale
// detection. See server/README.md "WebSocket fan-out".

import { isInterval, type Interval, type Ticker } from '../types.ts'
import { parseKlineMessage, parseMiniTickerMessage, type KlineUpdate } from './streamParse.ts'

export type StreamStatus = 'live' | 'reconnecting'

/** `kline:<SYMBOL>:<interval>` or `tickers`. */
export type StreamKey = string

export type HubEvent =
  | ({ type: 'kline' } & KlineUpdate)
  | { type: 'tickers'; tickers: Ticker[] }
  | { type: 'status'; status: StreamStatus }

export type HubListener = (event: HubEvent) => void

export interface HubOptions {
  WebSocketImpl: typeof WebSocket
  /** e.g. `wss://stream.binance.us:9443` (no trailing `/ws`). */
  wsBase: string
  /** `upstreamSockets` is kept equal to the number of upstream sockets currently held. */
  stats: { upstreamSockets: number }
  /** Reconnect when no upstream message arrives for this long. Default 60 s. */
  staleTimeoutMs?: number
  /**
   * After this much silence on an open socket, send a `LIST_SUBSCRIPTIONS` request (repeated
   * while silent); its reply counts as a message. Binance.US kline streams only push on trades,
   * which can be minutes apart, so without this a healthy quiet stream would go stale. Default 25 s.
   */
  probeAfterMs?: number
  /** Random source in [0, 1) for backoff jitter. Default `Math.random`. */
  random?: () => number
  /**
   * After the last listener releases, keep the upstream entry (socket, status, and backoff
   * state) this long so a quick re-acquire reuses it. Default 5 s; 0 tears down at once.
   */
  releaseGraceMs?: number
}

export interface Hub {
  /** Attach a listener; it immediately receives the current status. Returns an idempotent release. */
  acquire(key: StreamKey, listener: HubListener): () => void
  /** Current status of a held key, or undefined if no upstream is held for it. */
  status(key: StreamKey): StreamStatus | undefined
  /** Number of keys currently held (including keys in their release grace period). */
  readonly size: number
  /** Close every upstream socket and clear all timers. Further acquires are no-ops. */
  close(): void
}

export const DEFAULT_STALE_TIMEOUT_MS = 60_000
export const DEFAULT_PROBE_AFTER_MS = 25_000
export const DEFAULT_RELEASE_GRACE_MS = 5_000
const BACKOFF_BASE_MS = 1_000
const BACKOFF_CAP_MS = 30_000

export const TICKERS_KEY: StreamKey = 'tickers'

export function klineKey(symbol: string, interval: Interval): StreamKey {
  return `kline:${symbol.toUpperCase()}:${interval}`
}

type ParsedKey = { kind: 'kline'; symbol: string; interval: Interval } | { kind: 'tickers' }

export function parseKey(key: StreamKey): ParsedKey | null {
  if (key === TICKERS_KEY) return { kind: 'tickers' }
  const m = /^kline:([A-Z0-9]{2,30}):([0-9a-zA-Z]+)$/.exec(key)
  if (!m) return null
  const [, symbol, interval] = m
  if (symbol === undefined || !isInterval(interval)) return null
  return { kind: 'kline', symbol, interval }
}

export function upstreamUrl(wsBase: string, key: ParsedKey): string {
  const base = wsBase.replace(/\/+$/, '')
  if (key.kind === 'tickers') return `${base}/ws/!miniTicker@arr`
  return `${base}/ws/${key.symbol.toLowerCase()}@kline_${key.interval}`
}

/**
 * Reconnect delay in ms for the zero-based attempt: exponential from 1 s, capped at 30 s,
 * with "equal jitter" so the result lies in [base/2, base].
 */
export function backoffDelay(attempt: number, rand: () => number = Math.random): number {
  const a = Number.isFinite(attempt) && attempt > 0 ? Math.floor(attempt) : 0
  const base = Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** Math.min(a, 10))
  const r = Math.min(1, Math.max(0, rand()))
  return Math.round(base / 2 + (base / 2) * r)
}

interface Entry {
  key: StreamKey
  parsed: ParsedKey
  listeners: Set<HubListener>
  ws: WebSocket | null
  status: StreamStatus
  /** Consecutive failed connections; reset on the first message of a socket. */
  failures: number
  receivedOnSocket: boolean
  reconnectTimer: ReturnType<typeof setTimeout> | undefined
  staleTimer: ReturnType<typeof setTimeout> | undefined
  /** Sends a liveness probe after `probeAfterMs` of silence on an open socket. */
  probeTimer: ReturnType<typeof setTimeout> | undefined
  /** Pending teardown after the last listener released; cleared by a re-acquire. */
  graceTimer: ReturnType<typeof setTimeout> | undefined
}

export function createHub(opts: HubOptions): Hub {
  const WS = opts.WebSocketImpl
  const staleMs = opts.staleTimeoutMs ?? DEFAULT_STALE_TIMEOUT_MS
  const probeMs = opts.probeAfterMs ?? DEFAULT_PROBE_AFTER_MS
  let probeId = 0
  const rand = opts.random ?? Math.random
  const graceMs = Math.max(0, opts.releaseGraceMs ?? DEFAULT_RELEASE_GRACE_MS)
  const entries = new Map<StreamKey, Entry>()
  let closed = false

  const syncStats = (): void => {
    let n = 0
    for (const e of entries.values()) if (e.ws !== null) n += 1
    opts.stats.upstreamSockets = n
  }

  const deliver = (entry: Entry, event: HubEvent): void => {
    for (const l of [...entry.listeners]) {
      if (!entry.listeners.has(l)) continue // released during this fan-out
      try {
        l(event)
      } catch {
        // One broken listener must not affect the others.
      }
    }
  }

  const setStatus = (entry: Entry, status: StreamStatus): void => {
    if (entry.status === status) return
    entry.status = status
    deliver(entry, { type: 'status', status })
  }

  const clearTimers = (entry: Entry): void => {
    if (entry.reconnectTimer !== undefined) clearTimeout(entry.reconnectTimer)
    if (entry.staleTimer !== undefined) clearTimeout(entry.staleTimer)
    if (entry.probeTimer !== undefined) clearTimeout(entry.probeTimer)
    entry.reconnectTimer = undefined
    entry.staleTimer = undefined
    entry.probeTimer = undefined
  }

  const discard = (sock: WebSocket): void => {
    sock.onopen = null
    sock.onmessage = null
    sock.onerror = null
    sock.onclose = null
    try {
      sock.close()
    } catch {
      // Already closed or closing.
    }
  }

  const armStale = (entry: Entry, sock: WebSocket): void => {
    if (entry.staleTimer !== undefined) clearTimeout(entry.staleTimer)
    entry.staleTimer = setTimeout(() => {
      entry.staleTimer = undefined
      drop(entry, sock)
    }, staleMs)
  }

  /** While the open socket stays silent, ask Binance for its subscriptions every `probeMs`. */
  const armProbe = (entry: Entry, sock: WebSocket): void => {
    if (entry.probeTimer !== undefined) clearTimeout(entry.probeTimer)
    entry.probeTimer = setTimeout(() => {
      entry.probeTimer = undefined
      if (closed || entry.ws !== sock) return
      try {
        probeId += 1
        sock.send(JSON.stringify({ method: 'LIST_SUBSCRIPTIONS', id: probeId }))
      } catch {
        // Socket is going away; the stale timer or close handler deals with it.
      }
      armProbe(entry, sock)
    }, probeMs)
  }

  const scheduleReconnect = (entry: Entry): void => {
    if (entry.reconnectTimer !== undefined) clearTimeout(entry.reconnectTimer)
    const delay = backoffDelay(Math.max(0, entry.failures - 1), rand)
    entry.reconnectTimer = setTimeout(() => {
      entry.reconnectTimer = undefined
      connect(entry)
    }, delay)
  }

  /** The socket closed, errored, or went stale. Idempotent per socket. */
  const drop = (entry: Entry, sock: WebSocket): void => {
    if (closed || entries.get(entry.key) !== entry || entry.ws !== sock) return
    entry.ws = null
    discard(sock)
    clearTimers(entry)
    entry.failures += 1
    syncStats()
    setStatus(entry, 'reconnecting')
    scheduleReconnect(entry)
  }

  const onMessage = (entry: Entry, data: unknown): void => {
    const p = entry.parsed
    if (p.kind === 'tickers') {
      const tickers = parseMiniTickerMessage(data)
      if (tickers.length > 0) deliver(entry, { type: 'tickers', tickers })
    } else {
      const k = parseKlineMessage(data, { symbol: p.symbol, interval: p.interval })
      if (k !== null) deliver(entry, { type: 'kline', candle: k.candle, closed: k.closed })
    }
  }

  const connect = (entry: Entry): void => {
    if (closed || entries.get(entry.key) !== entry) return
    let sock: WebSocket
    try {
      sock = new WS(upstreamUrl(opts.wsBase, entry.parsed))
    } catch {
      entry.failures += 1
      setStatus(entry, 'reconnecting')
      scheduleReconnect(entry)
      return
    }
    entry.ws = sock
    entry.receivedOnSocket = false
    syncStats()
    // The stream is subscribed by URL, so an open socket is live even before the first push
    // (a quiet Binance.US pair can go minutes without a trade).
    sock.onopen = () => {
      if (closed || entry.ws !== sock) return
      setStatus(entry, 'live')
      armProbe(entry, sock)
    }
    sock.onmessage = (ev: MessageEvent) => {
      if (closed || entry.ws !== sock) return
      armStale(entry, sock)
      armProbe(entry, sock)
      if (!entry.receivedOnSocket) {
        // Backoff resets only once data flows, so an accept-then-drop upstream still backs off.
        entry.receivedOnSocket = true
        entry.failures = 0
        setStatus(entry, 'live')
      }
      onMessage(entry, ev.data)
    }
    sock.onerror = () => drop(entry, sock)
    sock.onclose = () => drop(entry, sock)
    // Also covers a connect that hangs without opening or never delivers a message.
    armStale(entry, sock)
  }

  const teardown = (entry: Entry): void => {
    clearTimers(entry)
    if (entry.graceTimer !== undefined) clearTimeout(entry.graceTimer)
    entry.graceTimer = undefined
    if (entry.ws) {
      const sock = entry.ws
      entry.ws = null
      discard(sock)
    }
    entry.listeners.clear()
    if (entries.get(entry.key) === entry) entries.delete(entry.key)
  }

  return {
    acquire(key, listener) {
      const parsed = parseKey(key)
      if (parsed === null) throw new Error(`invalid stream key: ${key}`)
      if (closed) {
        try {
          listener({ type: 'status', status: 'reconnecting' })
        } catch {
          // ignore
        }
        return () => {}
      }
      let entry = entries.get(key)
      const isNew = entry === undefined
      if (entry === undefined) {
        entry = {
          key,
          parsed,
          listeners: new Set(),
          ws: null,
          status: 'reconnecting',
          failures: 0,
          receivedOnSocket: false,
          reconnectTimer: undefined,
          staleTimer: undefined,
          probeTimer: undefined,
          graceTimer: undefined,
        }
        entries.set(key, entry)
      }
      const e = entry
      if (e.graceTimer !== undefined) {
        // Re-acquired within the grace period: keep the socket and its backoff state.
        clearTimeout(e.graceTimer)
        e.graceTimer = undefined
      }
      // A wrapper so the same function can be attached twice and released independently.
      const wrapped: HubListener = (ev) => listener(ev)
      e.listeners.add(wrapped)
      try {
        wrapped({ type: 'status', status: e.status })
      } catch {
        // Isolated like any other delivery.
      }
      if (isNew) connect(e)

      let released = false
      return () => {
        if (released) return
        released = true
        e.listeners.delete(wrapped)
        if (e.listeners.size === 0 && entries.get(key) === e && e.graceTimer === undefined) {
          const finish = (): void => {
            e.graceTimer = undefined
            if (e.listeners.size > 0 || entries.get(key) !== e) return
            teardown(e)
            syncStats()
          }
          if (graceMs === 0) finish()
          else e.graceTimer = setTimeout(finish, graceMs)
        }
      }
    },
    status(key) {
      return entries.get(key)?.status
    },
    get size() {
      return entries.size
    },
    close() {
      if (closed) return
      closed = true
      for (const e of [...entries.values()]) teardown(e)
      entries.clear()
      syncStats()
    },
  }
}
