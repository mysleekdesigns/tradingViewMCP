import type { Candle, ConnectionStatus, Interval, SymbolInfo, Ticker } from '../types'
import { DEFAULT_STALE_TIMEOUT_MS, openReconnectingSocket } from './binanceStream'
import { normalizeSymbol } from './binanceTicker'
import {
  MarketDataError,
  type MarketDataErrorKind,
  type MarketDataProvider,
  type SubscribeOptions,
} from './provider'

/**
 * `MarketDataProvider` backed by the local server (`server/`, contract in `server/README.md`).
 * The browser talks to same-origin `/api` and `/ws` (Vite proxies them to 127.0.0.1:8787); the
 * server caches candles in SQLite and fans one upstream socket out to every browser socket.
 */

const MAX_LIMIT = 1000
const DEFAULT_LIMIT = 500
const DEFAULT_TIMEOUT_MS = 10_000
/** WebSocket close codes after which the stream stops for good (policy violation = bad params). */
export const SERVER_FATAL_CLOSE_CODES: readonly number[] = [1008]

/** Error kinds the server may send; 'aborted' is client-only, anything else maps to 'http'. */
const SERVER_ERROR_KINDS: ReadonlySet<MarketDataErrorKind> = new Set<MarketDataErrorKind>([
  'network',
  'bad-symbol',
  'rate-limited',
  'banned',
  'region-blocked',
  'http',
  'parse',
])

function isRecord(v: unknown): v is Record<string, unknown> {
  return v !== null && typeof v === 'object' && !Array.isArray(v)
}

function isFiniteNumber(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

function nonNegative(v: unknown): number | undefined {
  return isFiniteNumber(v) && v >= 0 ? v : undefined
}

/** `Retry-After` delta-seconds; undefined when absent or invalid. */
function parseRetryAfterHeader(res: Response): number | undefined {
  const raw = res.headers.get('Retry-After')
  if (raw === null || raw.trim() === '') return undefined
  return nonNegative(Number(raw))
}

function clampLimit(limit: number): number {
  if (!Number.isFinite(limit)) return DEFAULT_LIMIT
  return Math.min(MAX_LIMIT, Math.max(1, Math.floor(limit)))
}

/** A server Candle (all numbers finite), or null. */
export function toCandle(v: unknown): Candle | null {
  if (!isRecord(v)) return null
  const { time, open, high, low, close, volume } = v
  if (
    !isFiniteNumber(time) ||
    !isFiniteNumber(open) ||
    !isFiniteNumber(high) ||
    !isFiniteNumber(low) ||
    !isFiniteNumber(close) ||
    !isFiniteNumber(volume)
  ) {
    return null
  }
  return { time, open, high, low, close, volume }
}

function toSymbolInfo(v: unknown): SymbolInfo | null {
  if (!isRecord(v)) return null
  const { symbol, baseAsset, quoteAsset, status, tickSize } = v
  if (
    typeof symbol !== 'string' ||
    typeof baseAsset !== 'string' ||
    typeof quoteAsset !== 'string' ||
    typeof status !== 'string' ||
    !isFiniteNumber(tickSize) ||
    tickSize <= 0
  ) {
    return null
  }
  return { symbol, baseAsset, quoteAsset, status, tickSize }
}

/** A server Ticker (all numbers finite), or null. */
export function toTicker(v: unknown): Ticker | null {
  if (!isRecord(v)) return null
  const { symbol, last, open, high, low, volume, quoteVolume, changePct, time } = v
  if (
    typeof symbol !== 'string' ||
    symbol === '' ||
    !isFiniteNumber(last) ||
    !isFiniteNumber(open) ||
    !isFiniteNumber(high) ||
    !isFiniteNumber(low) ||
    !isFiniteNumber(volume) ||
    !isFiniteNumber(quoteVolume) ||
    !isFiniteNumber(changePct) ||
    !isFiniteNumber(time)
  ) {
    return null
  }
  return { symbol, last, open, high, low, volume, quoteVolume, changePct, time }
}

/** Parse a whole array strictly: any malformed element rejects with 'parse'. */
function parseArray<T>(body: unknown, parse: (v: unknown) => T | null, what: string): T[] {
  if (!Array.isArray(body)) throw new MarketDataError('parse', `Expected an array of ${what}`)
  const out: T[] = []
  for (const entry of body) {
    const item = parse(entry)
    if (item === null) throw new MarketDataError('parse', `Malformed entry in ${what} response`)
    out.push(item)
  }
  return out
}

/** Server WS message, or null when malformed. */
type StreamMessage =
  | { type: 'kline'; candle: Candle; closed: boolean }
  | { type: 'tickers'; tickers: Ticker[] }
  | { type: 'status'; status: 'live' | 'reconnecting' }

export function parseStreamMessage(data: unknown): StreamMessage | null {
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
    case 'kline': {
      const candle = toCandle(msg.candle)
      if (candle === null || typeof msg.closed !== 'boolean') return null
      return { type: 'kline', candle, closed: msg.closed }
    }
    case 'tickers': {
      if (!Array.isArray(msg.tickers)) return null
      const tickers: Ticker[] = []
      for (const t of msg.tickers) {
        const parsed = toTicker(t)
        if (parsed !== null) tickers.push(parsed)
      }
      return { type: 'tickers', tickers }
    }
    case 'status':
      return msg.status === 'live' || msg.status === 'reconnecting'
        ? { type: 'status', status: msg.status }
        : null
    default:
      return null
  }
}

/** Reject with kind 'aborted' when `signal` aborts before `p` settles. */
function withAbort<T>(p: Promise<T>, signal: AbortSignal | undefined, what: string): Promise<T> {
  if (!signal) return p
  const aborted = () => new MarketDataError('aborted', `Request for ${what} was cancelled`)
  if (signal.aborted) return Promise.reject(aborted())
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(aborted())
    signal.addEventListener('abort', onAbort, { once: true })
    p.then(
      (v) => {
        signal.removeEventListener('abort', onAbort)
        resolve(v)
      },
      (e: unknown) => {
        signal.removeEventListener('abort', onAbort)
        reject(e instanceof Error ? e : new Error(String(e)))
      },
    )
  })
}

export class ServerProvider implements MarketDataProvider {
  private readonly baseUrl: string
  private readonly fetchImpl: typeof fetch
  private readonly WebSocketImpl: typeof WebSocket | undefined
  private readonly staleTimeoutMs: number
  private readonly timeoutMs: number
  /** Memoized symbol list; cleared when the fetch fails so the next call retries. */
  private symbolsPromise: Promise<SymbolInfo[]> | null = null

  constructor(opts?: {
    baseUrl?: string
    fetchImpl?: typeof fetch
    WebSocketImpl?: typeof WebSocket
    staleTimeoutMs?: number
    timeoutMs?: number
  }) {
    this.baseUrl = (opts?.baseUrl ?? '').replace(/\/+$/, '')
    // Bind so the global fetch is not called with a foreign `this`.
    this.fetchImpl = opts?.fetchImpl ?? ((input, init) => globalThis.fetch(input, init))
    this.WebSocketImpl = opts?.WebSocketImpl
    this.staleTimeoutMs = opts?.staleTimeoutMs ?? DEFAULT_STALE_TIMEOUT_MS
    this.timeoutMs = opts?.timeoutMs ?? DEFAULT_TIMEOUT_MS
  }

  /** ws(s):// origin for the server: from `baseUrl` when given, else the page's host. */
  private wsOrigin(): string {
    if (this.baseUrl !== '') return this.baseUrl.replace(/^http(s?):\/\//i, 'ws$1://')
    if (typeof location === 'undefined') {
      throw new Error('ServerProvider: no baseUrl and no location to derive the WebSocket URL')
    }
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
    return `${scheme}://${location.host}`
  }

  private resolveWebSocket(caller: string): typeof WebSocket {
    const WS = this.WebSocketImpl ?? globalThis.WebSocket
    if (typeof WS !== 'function') {
      throw new Error(`${caller}: WebSocket is not available in this environment`)
    }
    return WS
  }

  /**
   * GET `path?params` and return the parsed JSON body. Caller abort -> 'aborted';
   * timeout/transport -> 'network'; non-2xx -> the server's `{error, message, retryAfter}`
   * (unknown kind -> 'http'; `Retry-After` header used when the body has no retryAfter);
   * unparseable 2xx body -> 'parse'.
   */
  private async requestJson(
    path: string,
    params: URLSearchParams | null,
    what: string,
    callerSignal?: AbortSignal,
  ): Promise<unknown> {
    const query = params ? `?${params.toString()}` : ''
    const url = `${this.baseUrl}${path}${query}`
    if (callerSignal?.aborted) {
      throw new MarketDataError('aborted', `Request for ${what} was cancelled`)
    }
    const timeout = AbortSignal.timeout(this.timeoutMs)
    const signal = callerSignal ? AbortSignal.any([callerSignal, timeout]) : timeout

    let res: Response
    try {
      res = await this.fetchImpl(url, { signal })
    } catch (err) {
      if (callerSignal?.aborted) {
        throw new MarketDataError('aborted', `Request for ${what} was cancelled`)
      }
      if (timeout.aborted) {
        throw new MarketDataError(
          'network',
          `Timed out after ${this.timeoutMs} ms fetching ${what}`,
        )
      }
      const detail = err instanceof Error ? err.message : String(err)
      throw new MarketDataError('network', `Network error fetching ${what}: ${detail}`)
    }

    if (!res.ok) throw await this.toError(res, what)

    try {
      return (await res.json()) as unknown
    } catch {
      if (callerSignal?.aborted) {
        throw new MarketDataError('aborted', `Request for ${what} was cancelled`)
      }
      throw new MarketDataError('parse', `Invalid JSON in ${what} response`)
    }
  }

  private async toError(res: Response, what: string): Promise<MarketDataError> {
    let body: unknown
    try {
      body = await res.json()
    } catch {
      body = undefined
    }
    const headerRetry = parseRetryAfterHeader(res)
    if (!isRecord(body)) {
      return new MarketDataError(
        'http',
        `HTTP ${res.status} fetching ${what}`,
        res.status,
        headerRetry,
      )
    }
    const kind =
      typeof body.error === 'string' && SERVER_ERROR_KINDS.has(body.error as MarketDataErrorKind)
        ? (body.error as MarketDataErrorKind)
        : 'http'
    const message =
      typeof body.message === 'string' && body.message !== ''
        ? body.message
        : `HTTP ${res.status} fetching ${what}`
    const retryAfter = nonNegative(body.retryAfter) ?? headerRetry
    return new MarketDataError(kind, message, res.status, retryAfter)
  }

  async getCandles(
    symbol: string,
    interval: Interval,
    limit = DEFAULT_LIMIT,
    endTime?: number,
    opts?: { signal?: AbortSignal },
  ): Promise<Candle[]> {
    const sym = symbol.trim().toUpperCase()
    const params = new URLSearchParams({ symbol: sym, interval, limit: String(clampLimit(limit)) })
    // Server API takes UNIX seconds, like the provider interface.
    if (endTime !== undefined && Number.isFinite(endTime)) {
      params.set('endTime', String(Math.floor(endTime)))
    }
    const body = await this.requestJson('/api/candles', params, `${sym} candles`, opts?.signal)
    const candles = parseArray(body, toCandle, `${sym} candles`)
    // The contract promises ascending unique times; enforce it since Lightweight Charts
    // throws on duplicates (later row wins).
    candles.sort((a, b) => a.time - b.time)
    const out: Candle[] = []
    for (const c of candles) {
      if (out.length > 0 && out[out.length - 1].time === c.time) out[out.length - 1] = c
      else out.push(c)
    }
    return out
  }

  /**
   * GET /api/symbols once per instance. The shared fetch ignores caller signals (one caller
   * aborting must not cancel it for others); each caller's signal only rejects its own promise.
   */
  getSymbols(opts?: { signal?: AbortSignal }): Promise<SymbolInfo[]> {
    if (opts?.signal?.aborted) {
      return Promise.reject(new MarketDataError('aborted', 'Request for symbols was cancelled'))
    }
    if (this.symbolsPromise === null) {
      const p = this.requestJson('/api/symbols', null, 'symbols').then((body) =>
        parseArray(body, toSymbolInfo, 'symbols'),
      )
      this.symbolsPromise = p
      p.catch(() => {
        if (this.symbolsPromise === p) this.symbolsPromise = null
      })
    }
    return withAbort(this.symbolsPromise, opts?.signal, 'symbols')
  }

  async getTickers(symbols: readonly string[], opts?: { signal?: AbortSignal }): Promise<Ticker[]> {
    const wanted: string[] = []
    for (const s of symbols) {
      const n = normalizeSymbol(s)
      if (n !== null && !wanted.includes(n)) wanted.push(n)
    }
    if (wanted.length === 0) return []
    const body = await this.requestJson(
      '/api/tickers',
      new URLSearchParams({ symbols: wanted.join(',') }),
      '24h tickers',
      opts?.signal,
    )
    return parseArray(body, toTicker, '24h tickers')
  }

  subscribeCandles(
    symbol: string,
    interval: Interval,
    onCandle: (c: Candle, closed: boolean) => void,
    opts?: SubscribeOptions,
  ): () => void {
    const WS = this.resolveWebSocket('subscribeCandles')
    const sym = symbol.trim().toUpperCase()
    const params = new URLSearchParams({ symbol: sym, interval })
    return this.openStream(
      `/ws/klines?${params.toString()}`,
      opts?.onStatus,
      (msg) => {
        if (msg.type !== 'kline') return
        try {
          onCandle(msg.candle, msg.closed)
        } catch (err) {
          console.error('subscribeCandles: onCandle threw', err)
        }
      },
      WS,
    )
  }

  subscribeTickers(onTickers: (tickers: Ticker[]) => void, opts?: SubscribeOptions): () => void {
    const WS = this.resolveWebSocket('subscribeTickers')
    return this.openStream(
      '/ws/tickers',
      opts?.onStatus,
      (msg) => {
        if (msg.type !== 'tickers' || msg.tickers.length === 0) return
        try {
          onTickers(msg.tickers)
        } catch (err) {
          console.error('subscribeTickers: onTickers threw', err)
        }
      },
      WS,
    )
  }

  /**
   * One reconnecting socket to the server. The reported status combines the browser socket
   * with the server's upstream status: while the socket is 'live' and the upstream is
   * 'reconnecting', report 'reconnecting'; upstream back to 'live' reports 'live' (which makes
   * consumers gap-fill). The upstream status is forgotten whenever the socket leaves 'live'
   * (the server resends it on attach). The socket reads 'live' only once its first message
   * arrives; a 1008 close (bad symbol/interval) reports 'offline' and stops retrying. Statuses
   * are de-duplicated; nothing runs after teardown.
   */
  private openStream(
    path: string,
    onStatus: ((s: ConnectionStatus) => void) | undefined,
    onData: (msg: Exclude<StreamMessage, { type: 'status' }>) => void,
    WS: typeof WebSocket,
  ): () => void {
    let disposed = false
    let socketStatus: ConnectionStatus | null = null
    let upstream: 'live' | 'reconnecting' | null = null
    let reported: ConnectionStatus | null = null

    const report = (): void => {
      if (disposed || socketStatus === null) return
      const effective: ConnectionStatus =
        socketStatus === 'live' && upstream === 'reconnecting' ? 'reconnecting' : socketStatus
      if (effective === reported) return
      reported = effective
      onStatus?.(effective)
    }

    const teardown = openReconnectingSocket({
      WebSocketImpl: WS,
      url: `${this.wsOrigin()}${path}`,
      staleTimeoutMs: this.staleTimeoutMs,
      // 1008 = bad symbol/interval (server/README.md): retrying cannot help.
      fatalCloseCodes: SERVER_FATAL_CLOSE_CODES,
      // The server sends its upstream status on attach, so 'live' waits for that first message:
      // a socket accepted and then closed with 1008 never reads 'live'.
      liveOnFirstMessage: true,
      onStatus: (s) => {
        if (s !== 'live') upstream = null
        socketStatus = s
        report()
      },
      onMessage: (data) => {
        if (disposed) return
        const msg = parseStreamMessage(data)
        if (msg === null) return
        if (msg.type === 'status') {
          upstream = msg.status
          report()
          return
        }
        onData(msg)
      },
    })

    return () => {
      if (disposed) return
      disposed = true
      teardown()
    }
  }
}
