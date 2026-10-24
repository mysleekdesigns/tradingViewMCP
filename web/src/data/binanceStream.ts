import type { Candle, ConnectionStatus } from '../types'

const BACKOFF_BASE_MS = 1_000
const BACKOFF_CAP_MS = 30_000
/** Reconnect if no WS message arrives for this long (silent drops). */
export const DEFAULT_STALE_TIMEOUT_MS = 60_000
/** Report 'offline' after this many consecutive failed connection attempts. */
const OFFLINE_AFTER_FAILURES = 5
/** WebSocket.OPEN; a literal so injected WebSocket implementations need not define it. */
const WS_OPEN = 1

/** Strict numeric field: finite number or non-empty numeric string, else null. */
function finite(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value !== 'string' || value.trim() === '') return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return v !== null && typeof v === 'object' && !Array.isArray(v)
}

/**
 * Parse one Binance kline WebSocket message (string or already-parsed object).
 * Accepts the raw stream shape `{ e: 'kline', s, k: {...} }` and the combined
 * stream wrapper `{ stream, data }`. Returns null for anything that is not a
 * well-formed kline, or (when `expected` is given) a kline for another
 * symbol/interval. Never throws.
 */
export function parseKlineMessage(
  data: unknown,
  expected?: { symbol: string; interval: string },
): { candle: Candle; closed: boolean } | null {
  let msg: unknown = data
  if (typeof msg === 'string') {
    try {
      msg = JSON.parse(msg)
    } catch {
      return null
    }
  }
  if (!isRecord(msg)) return null
  if (isRecord(msg.data) && typeof msg.stream === 'string') msg = msg.data
  if (!isRecord(msg) || msg.e !== 'kline' || !isRecord(msg.k)) return null
  const k = msg.k

  if (expected) {
    if (typeof k.s !== 'string' || k.s.toUpperCase() !== expected.symbol.trim().toUpperCase()) {
      return null
    }
    if (k.i !== expected.interval) return null
  }

  const openTimeMs = finite(k.t)
  const open = finite(k.o)
  const high = finite(k.h)
  const low = finite(k.l)
  const close = finite(k.c)
  const volume = finite(k.v)
  if (
    openTimeMs === null ||
    open === null ||
    high === null ||
    low === null ||
    close === null ||
    volume === null
  ) {
    return null
  }
  return {
    candle: { time: Math.floor(openTimeMs / 1000), open, high, low, close, volume },
    closed: k.x === true,
  }
}

/**
 * Reconnect delay in ms for the given zero-based attempt: exponential from 1 s,
 * capped at 30 s, with "equal jitter" so the result lies in [base/2, base].
 * `rand` must return values in [0, 1).
 */
export function backoffDelay(attempt: number, rand: () => number = Math.random): number {
  const a = Number.isFinite(attempt) && attempt > 0 ? Math.floor(attempt) : 0
  // 2^5 * 1 s already exceeds the cap; avoid overflow for huge attempts.
  const base = Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** Math.min(a, 10))
  const r = Math.min(1, Math.max(0, rand()))
  return Math.round(base / 2 + (base / 2) * r)
}

export interface ReconnectingSocketOptions {
  WebSocketImpl: typeof WebSocket
  url: string
  staleTimeoutMs: number
  /** Raw `MessageEvent.data` of every message on the current socket. */
  onMessage: (data: unknown) => void
  onStatus?: (status: ConnectionStatus) => void
  /**
   * Close codes that mean "never going to work" (e.g. the local server's 1008 for a bad
   * symbol/interval). On such a close: stop retrying, report 'offline', and tear down (no
   * timers or listeners left; later teardown calls are no-ops). While this is non-empty an
   * `error` event waits for the `close` event that always follows it, so the code is seen.
   */
  fatalCloseCodes?: readonly number[]
  /**
   * Report 'live' on the first message of a socket instead of on open (the message is
   * delivered first). For servers that send a status message on attach, so a socket that is
   * accepted and then closed before any message never reads 'live'.
   */
  liveOnFirstMessage?: boolean
}

/**
 * One logical stream over a WebSocket that reconnects by itself: jittered exponential
 * backoff, stale detection (no message for `staleTimeoutMs`, which also covers a connect
 * that never opens), window online/offline handling, and de-duplicated status reports
 * (`connecting` -> `live` -> `reconnecting` / `offline`). After 5 consecutive failures the
 * status reads 'offline' while retries continue at the capped backoff. A stale timeout on
 * a socket that had opened (an idle stream) reconnects silently at attempt 0: status
 * stays 'live' and it is not counted as a failure. A close with one of `fatalCloseCodes` stops
 * for good: 'offline', then torn down. Returns an idempotent teardown; no callback runs after it.
 */
export function openReconnectingSocket(opts: ReconnectingSocketOptions): () => void {
  const { WebSocketImpl: WS, url, staleTimeoutMs, onMessage, onStatus } = opts
  const fatalCodes: readonly number[] = opts.fatalCloseCodes ?? []
  const liveOnFirstMessage = opts.liveOnFirstMessage === true

  let disposed = false
  let ws: WebSocket | null = null
  /** Consecutive failed attempts; reset once a socket is open and delivers a message. */
  let failures = 0
  let receivedOnSocket = false
  /** The current socket reached OPEN. */
  let openedSocket = false
  let reconnectTimer: ReturnType<typeof setTimeout> | undefined
  let staleTimer: ReturnType<typeof setTimeout> | undefined
  let lastStatus: ConnectionStatus | null = null

  const emit = (status: ConnectionStatus): void => {
    if (disposed || status === lastStatus) return
    lastStatus = status
    onStatus?.(status)
  }
  const browserOffline = (): boolean =>
    typeof navigator !== 'undefined' && navigator.onLine === false

  const clearTimers = (): void => {
    if (reconnectTimer !== undefined) clearTimeout(reconnectTimer)
    if (staleTimer !== undefined) clearTimeout(staleTimer)
    reconnectTimer = undefined
    staleTimer = undefined
  }

  /** Detach handlers and close; no event from this socket reaches us afterwards. */
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

  const discardCurrent = (): void => {
    if (ws) {
      const sock = ws
      ws = null
      discard(sock)
    }
  }

  const armStale = (sock: WebSocket): void => {
    if (staleTimer !== undefined) clearTimeout(staleTimer)
    staleTimer = setTimeout(() => {
      staleTimer = undefined
      // A socket that opened and then went quiet is healthy but idle (illiquid pair):
      // reconnect promptly without counting it as a failure, so it never reads 'offline'.
      drop(sock, openedSocket)
    }, staleTimeoutMs)
  }

  const scheduleReconnect = (attempt = Math.max(0, failures - 1)): void => {
    if (reconnectTimer !== undefined) clearTimeout(reconnectTimer)
    const delay = backoffDelay(attempt)
    reconnectTimer = setTimeout(() => {
      reconnectTimer = undefined
      connect()
    }, delay)
  }

  /**
   * The socket closed, errored, or went stale. Idempotent per socket. `idleStale` marks a
   * stale timeout on a socket that had opened: not a failure, reconnect at attempt 0.
   */
  const drop = (sock: WebSocket, idleStale = false): void => {
    if (disposed || sock !== ws) return
    ws = null
    discard(sock)
    clearTimers()
    if (!idleStale) failures += 1
    if (browserOffline()) {
      // The window 'online' listener reconnects.
      emit('offline')
      return
    }
    if (idleStale) {
      // Silent reconnect: the stream was healthy, so status stays 'live' (no dot flicker,
      // no consumer gap fill). If the new socket fails to open, its drop() takes the normal
      // 'reconnecting' / backoff / 'offline' path.
      scheduleReconnect(0)
      return
    }
    emit(failures >= OFFLINE_AFTER_FAILURES ? 'offline' : 'reconnecting')
    scheduleReconnect()
  }

  const connect = (): void => {
    if (disposed) return
    if (browserOffline()) {
      emit('offline')
      return
    }
    let sock: WebSocket
    try {
      sock = new WS(url)
    } catch {
      failures += 1
      emit(failures >= OFFLINE_AFTER_FAILURES ? 'offline' : 'reconnecting')
      scheduleReconnect()
      return
    }
    ws = sock
    receivedOnSocket = false
    openedSocket = false
    sock.onopen = () => {
      if (disposed || sock !== ws) return
      openedSocket = true
      if (!liveOnFirstMessage) emit('live')
      armStale(sock)
    }
    sock.onmessage = (ev: MessageEvent) => {
      if (disposed || sock !== ws) return
      armStale(sock)
      const first = !receivedOnSocket
      if (first) {
        receivedOnSocket = true
        failures = 0
      }
      try {
        onMessage(ev.data)
      } finally {
        if (first && liveOnFirstMessage && sock === ws) emit('live')
      }
    }
    // With fatal codes, an error waits for the close that follows it (and the stale timer
    // still covers a close that never comes), so the close code decides.
    sock.onerror = () => {
      if (fatalCodes.length === 0) drop(sock)
    }
    sock.onclose = (ev: CloseEvent) => {
      if (disposed || sock !== ws) return
      const code = typeof ev?.code === 'number' ? ev.code : undefined
      if (code !== undefined && fatalCodes.includes(code)) {
        emit('offline')
        teardown()
        return
      }
      drop(sock)
    }
    // Also covers a connect that hangs without opening.
    armStale(sock)
  }

  const onOffline = (): void => {
    if (disposed) return
    discardCurrent()
    clearTimers()
    emit('offline')
  }
  const onOnline = (): void => {
    if (disposed) return
    // A healthy open socket needs nothing; don't tear it down.
    if (ws !== null && ws.readyState === WS_OPEN) return
    discardCurrent()
    clearTimers()
    failures = 0
    emit('reconnecting')
    connect()
  }

  const win = typeof window !== 'undefined' ? window : undefined
  const canListen = win !== undefined && typeof win.addEventListener === 'function'
  if (canListen) {
    win.addEventListener('offline', onOffline)
    win.addEventListener('online', onOnline)
  }

  function teardown(): void {
    if (disposed) return
    disposed = true
    clearTimers()
    discardCurrent()
    if (canListen) {
      win.removeEventListener('offline', onOffline)
      win.removeEventListener('online', onOnline)
    }
  }

  if (!browserOffline()) emit('connecting')
  connect()

  return teardown
}
