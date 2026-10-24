import type { Candle, ConnectionStatus, Interval, SymbolInfo } from '../types'
import { BINANCE_US_WS, backoffDelay, parseKlineMessage } from './binanceStream'
import { MarketDataError, type MarketDataProvider, type SubscribeOptions } from './provider'

export const BINANCE_US_REST = 'https://api.binance.us'

const MAX_LIMIT = 1000
const DEFAULT_TIMEOUT_MS = 10_000
/** Binance error code for "Invalid symbol." */
const INVALID_SYMBOL_CODE = -1121
/** Reconnect if no WS message arrives for this long (silent drops). */
const DEFAULT_STALE_TIMEOUT_MS = 60_000
/** Report 'offline' after this many consecutive failed connection attempts. */
const OFFLINE_AFTER_FAILURES = 5
/** WebSocket.OPEN; a literal so injected WebSocket implementations need not define it. */
const WS_OPEN = 1

/** `Retry-After` in seconds (Binance sends delta-seconds); undefined when absent or invalid. */
function parseRetryAfter(res: Response): number | undefined {
  const raw = res.headers.get('Retry-After')
  if (raw === null || raw.trim() === '') return undefined
  const n = Number(raw)
  return Number.isFinite(n) && n >= 0 ? n : undefined
}

/** Keep one candle per time (the later row wins); input must be sorted ascending. */
function dedupeByTime(sorted: Candle[]): Candle[] {
  const out: Candle[] = []
  for (const c of sorted) {
    if (out.length > 0 && out[out.length - 1].time === c.time) out[out.length - 1] = c
    else out.push(c)
  }
  return out
}

function toFiniteNumber(value: unknown, field: string): number {
  if (typeof value !== 'string' && typeof value !== 'number') {
    throw new MarketDataError('parse', `kline ${field}: expected string or number`)
  }
  if (typeof value === 'string' && value.trim() === '') {
    throw new MarketDataError('parse', `kline ${field}: empty value`)
  }
  const n = Number(value)
  if (!Number.isFinite(n)) {
    throw new MarketDataError('parse', `kline ${field}: not a finite number`)
  }
  return n
}

/**
 * Map one Binance kline row
 * `[openTime(ms), open, high, low, close, volume, closeTime, ...]` (prices/volume
 * are strings) to a Candle with UNIX-second time.
 */
export function parseKline(row: unknown): Candle {
  if (!Array.isArray(row) || row.length < 6) {
    throw new MarketDataError('parse', 'kline row: expected an array with at least 6 fields')
  }
  const openTimeMs = toFiniteNumber(row[0], 'openTime')
  return {
    time: Math.floor(openTimeMs / 1000),
    open: toFiniteNumber(row[1], 'open'),
    high: toFiniteNumber(row[2], 'high'),
    low: toFiniteNumber(row[3], 'low'),
    close: toFiniteNumber(row[4], 'close'),
    volume: toFiniteNumber(row[5], 'volume'),
  }
}

function clampLimit(limit: number): number {
  if (!Number.isFinite(limit)) return 500
  return Math.min(MAX_LIMIT, Math.max(1, Math.floor(limit)))
}

async function readErrorCode(res: Response): Promise<{ code?: number; msg?: string }> {
  try {
    const body: unknown = await res.json()
    if (body !== null && typeof body === 'object') {
      const { code, msg } = body as { code?: unknown; msg?: unknown }
      return {
        code: typeof code === 'number' ? code : undefined,
        msg: typeof msg === 'string' ? msg : undefined,
      }
    }
  } catch {
    // Non-JSON error body; fall through.
  }
  return {}
}

export class BinanceProvider implements MarketDataProvider {
  private readonly baseUrl: string
  private readonly fetchFn: typeof fetch
  private readonly timeoutMs: number
  private readonly wsBaseUrl: string
  private readonly WebSocketImpl: typeof WebSocket | undefined
  private readonly staleTimeoutMs: number

  constructor(opts?: {
    baseUrl?: string
    fetchFn?: typeof fetch
    timeoutMs?: number
    wsBaseUrl?: string
    WebSocketImpl?: typeof WebSocket
    staleTimeoutMs?: number
  }) {
    this.baseUrl = (opts?.baseUrl ?? BINANCE_US_REST).replace(/\/+$/, '')
    // Bind so the global fetch is not called with a foreign `this`.
    this.fetchFn = opts?.fetchFn ?? ((input, init) => globalThis.fetch(input, init))
    this.timeoutMs = opts?.timeoutMs ?? DEFAULT_TIMEOUT_MS
    this.wsBaseUrl = (opts?.wsBaseUrl ?? BINANCE_US_WS).replace(/\/+$/, '')
    this.WebSocketImpl = opts?.WebSocketImpl
    this.staleTimeoutMs = opts?.staleTimeoutMs ?? DEFAULT_STALE_TIMEOUT_MS
  }

  async getCandles(
    symbol: string,
    interval: Interval,
    limit = 500,
    endTime?: number,
    opts?: { signal?: AbortSignal },
  ): Promise<Candle[]> {
    const sym = symbol.trim().toUpperCase()
    const params = new URLSearchParams({
      symbol: sym,
      interval,
      limit: String(clampLimit(limit)),
    })
    if (endTime !== undefined) {
      // Provider API takes UNIX seconds; Binance wants ms.
      params.set('endTime', String(Math.floor(endTime * 1000)))
    }
    const url = `${this.baseUrl}/api/v3/klines?${params.toString()}`

    const callerSignal = opts?.signal
    const timeout = AbortSignal.timeout(this.timeoutMs)
    const signal = callerSignal ? AbortSignal.any([callerSignal, timeout]) : timeout

    let res: Response
    try {
      res = await this.fetchFn(url, { signal })
    } catch (err) {
      if (callerSignal?.aborted) {
        throw new MarketDataError('aborted', `Request for ${sym} candles was cancelled`)
      }
      if (timeout.aborted) {
        throw new MarketDataError(
          'network',
          `Timed out after ${this.timeoutMs} ms fetching ${sym} candles`,
        )
      }
      const detail = err instanceof Error ? err.message : String(err)
      throw new MarketDataError('network', `Network error fetching ${sym} candles: ${detail}`)
    }

    if (!res.ok) {
      const retryAfter = parseRetryAfter(res)
      if (res.status === 429) {
        throw new MarketDataError('rate-limited', `Rate limited fetching ${sym}`, 429, retryAfter)
      }
      if (res.status === 418) {
        throw new MarketDataError('banned', `IP banned by provider`, 418, retryAfter)
      }
      if (res.status === 451) {
        throw new MarketDataError('region-blocked', `Provider unavailable in this region`, 451)
      }
      const { code, msg } = await readErrorCode(res)
      if (res.status === 400 && code === INVALID_SYMBOL_CODE) {
        throw new MarketDataError('bad-symbol', `Unknown symbol: ${sym}`, res.status)
      }
      const suffix = msg ? `: ${msg}` : ''
      throw new MarketDataError(
        'http',
        `HTTP ${res.status} fetching ${sym} candles${suffix}`,
        res.status,
      )
    }

    let body: unknown
    try {
      body = await res.json()
    } catch {
      throw new MarketDataError('parse', `Invalid JSON in ${sym} candles response`)
    }
    if (!Array.isArray(body)) {
      throw new MarketDataError('parse', `Expected an array of klines for ${sym}`)
    }

    // The last row is usually the still-forming candle (close time > now). It is
    // kept so the chart shows the current bar; consumers must not treat it as closed.
    // Lightweight Charts throws on duplicate times, so dedupe after sorting.
    const candles = body.map(parseKline)
    candles.sort((a, b) => a.time - b.time)
    return dedupeByTime(candles)
  }

  subscribeCandles(
    symbol: string,
    interval: Interval,
    onCandle: (c: Candle, closed: boolean) => void,
    opts?: SubscribeOptions,
  ): () => void {
    const WS = this.WebSocketImpl ?? globalThis.WebSocket
    if (typeof WS !== 'function') {
      throw new Error('subscribeCandles: WebSocket is not available in this environment')
    }
    const sym = symbol.trim().toUpperCase()
    const url = `${this.wsBaseUrl}/ws/${sym.toLowerCase()}@kline_${interval}`
    const expected = { symbol: sym, interval }
    const staleTimeoutMs = this.staleTimeoutMs

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
      opts?.onStatus?.(status)
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
        emit('reconnecting')
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
        emit('live')
        armStale(sock)
      }
      sock.onmessage = (ev: MessageEvent) => {
        if (disposed || sock !== ws) return
        armStale(sock)
        if (!receivedOnSocket) {
          receivedOnSocket = true
          failures = 0
        }
        const parsed = parseKlineMessage(ev.data, expected)
        if (parsed === null) return
        try {
          onCandle(parsed.candle, parsed.closed)
        } catch (err) {
          console.error('subscribeCandles: onCandle threw', err)
        }
      }
      sock.onerror = () => drop(sock)
      sock.onclose = () => drop(sock)
      // Also covers a connect that hangs without opening.
      armStale(sock)
    }

    const onOffline = (): void => {
      if (disposed) return
      if (ws) {
        const sock = ws
        ws = null
        discard(sock)
      }
      clearTimers()
      emit('offline')
    }
    const onOnline = (): void => {
      if (disposed) return
      // A healthy open socket needs nothing; don't tear it down.
      if (ws !== null && ws.readyState === WS_OPEN) return
      if (ws) {
        const sock = ws
        ws = null
        discard(sock)
      }
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

    if (!browserOffline()) emit('connecting')
    connect()

    return () => {
      if (disposed) return
      disposed = true
      clearTimers()
      if (ws) {
        const sock = ws
        ws = null
        discard(sock)
      }
      if (canListen) {
        win.removeEventListener('offline', onOffline)
        win.removeEventListener('online', onOnline)
      }
    }
  }

  getSymbols(): Promise<SymbolInfo[]> {
    return Promise.reject(new Error('getSymbols: not implemented until Phase 3'))
  }
}
