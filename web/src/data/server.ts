import type { Candle, ConnectionStatus, Interval, SymbolInfo, Ticker } from '../types'
import { DEFAULT_STALE_TIMEOUT_MS, openReconnectingSocket, resolveWebSocket } from './binanceStream'
import { normalizeSymbol } from './binanceTicker'
import {
  MarketDataError,
  type MarketDataErrorKind,
  type MarketDataProvider,
  type SubscribeOptions,
  type TickerSubscribeOptions,
} from './provider'
import { dedupeByTime, isFiniteNumber, isRecord, nonNegative } from './guards'
import { SharedLoad, clampLimit, parseRetryAfter, requestMarketJson } from './http'
import { parseVenueInfo, type VenueInfo } from './venue'

/**
 * `MarketDataProvider` backed by the local server (`server/`, contract in `server/README.md`).
 * The browser talks to same-origin `/api` and `/ws` (Vite proxies them to 127.0.0.1:8787); the
 * server caches candles in SQLite and fans one upstream socket out to every browser socket.
 */

const MAX_LIMIT = 1000
/** Venue ids the server accepts in the `/api/candles` `venue` param (server `VENUE_PARAM_RE`). */
const VENUE_ID_RE = /^[a-z0-9-]{1,32}$/
const DEFAULT_LIMIT = 500
const DEFAULT_TIMEOUT_MS = 10_000
/** `WebSocket.OPEN` (test doubles may lack the static). */
const WS_OPEN = 1
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

/** Most symbols one `/ws/tickers` `watch` message may carry (server-enforced, same value). */
export const MAX_WATCH_SYMBOLS = 500
/** Symbol syntax the server accepts in a `watch` message (Binance spot symbols). */
const WATCH_SYMBOL_RE = /^[A-Z0-9]{2,20}$/

/**
 * The `/ws/tickers` client message for a watched set: `{"type":"watch","symbols":[...]}`
 * (sorted), or `{"type":"unwatch"}` (every symbol again) when the set cannot be expressed:
 * more than MAX_WATCH_SYMBOLS entries, or an entry the server would not accept (it could then
 * never be delivered).
 */
export function watchMessage(symbols: ReadonlySet<string>): string {
  if (symbols.size > MAX_WATCH_SYMBOLS) return JSON.stringify({ type: 'unwatch' })
  const list: string[] = []
  for (const s of symbols) {
    if (!WATCH_SYMBOL_RE.test(s)) return JSON.stringify({ type: 'unwatch' })
    list.push(s)
  }
  list.sort()
  return JSON.stringify({ type: 'watch', symbols: list })
}

/** Server WS message, or null when malformed. */
type StreamMessage =
  | { type: 'kline'; candle: Candle; closed: boolean }
  | { type: 'tickers'; tickers: Ticker[] }
  | { type: 'status'; status: 'live' | 'reconnecting' }

/**
 * Parse one server WS message. With `only`, ticker entries whose `symbol` is not in the set are
 * skipped before validation (the server filters already once it has the client's `watch`).
 */
export function parseStreamMessage(
  data: unknown,
  only?: ReadonlySet<string>,
): StreamMessage | null {
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
        if (
          only !== undefined &&
          !(isRecord(t) && typeof t.symbol === 'string' && only.has(t.symbol))
        ) {
          continue
        }
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

export class ServerProvider implements MarketDataProvider {
  private readonly baseUrl: string
  private readonly fetchImpl: typeof fetch
  private readonly WebSocketImpl: typeof WebSocket | undefined
  private readonly staleTimeoutMs: number
  private readonly timeoutMs: number
  /** Memoized symbol list; forgotten when the fetch fails so the next call retries. */
  private readonly symbols = new SharedLoad(() =>
    this.requestJson('/api/symbols', null, 'symbols').then((body) =>
      parseArray(body, toSymbolInfo, 'symbols'),
    ),
  )
  /**
   * Memoized `/api/health` venue lookup, shared by `getVenue` and the candle URLs; forgotten
   * when it fails so the next caller retries. Records `venueId` when the server reports a
   * usable id.
   */
  private readonly venue = new SharedLoad(() =>
    this.requestJson('/api/health', null, 'server health').then((body) => {
      const venue = isRecord(body) ? parseVenueInfo(body.venue) : null
      if (venue === null) throw new MarketDataError('parse', 'Server health has no venue')
      if (VENUE_ID_RE.test(venue.id)) this.venueId = venue.id
      return venue
    }),
  )
  /**
   * The server's venue id once the lookup succeeded. Sent as `venue` on closed-history pages
   * (`endTime` set), which makes the server's `immutable` caching safe across a venue switch:
   * another venue means another URL.
   */
  private venueId: string | null = null

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

  /**
   * GET `path?params` and return the parsed JSON body. Caller abort -> 'aborted';
   * timeout/transport -> 'network'; non-2xx -> the server's `{error, message, retryAfter}`
   * (unknown kind -> 'http'; `Retry-After` header used when the body has no retryAfter);
   * unparseable 2xx body -> 'parse'.
   */
  private requestJson(
    path: string,
    params: URLSearchParams | null,
    what: string,
    callerSignal?: AbortSignal,
    cache?: RequestCache,
  ): Promise<unknown> {
    const query = params ? `?${params.toString()}` : ''
    return requestMarketJson({
      fetchFn: this.fetchImpl,
      url: `${this.baseUrl}${path}${query}`,
      what,
      timeoutMs: this.timeoutMs,
      signal: callerSignal,
      cache,
      callerAbortWins: true,
      toError: (res) => this.toError(res, what),
    })
  }

  private async toError(res: Response, what: string): Promise<MarketDataError> {
    let body: unknown
    try {
      body = await res.json()
    } catch {
      body = undefined
    }
    const headerRetry = parseRetryAfter(res)
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
    const params = new URLSearchParams({
      symbol: sym,
      interval,
      limit: String(clampLimit(limit, MAX_LIMIT, DEFAULT_LIMIT)),
    })
    // Server API takes UNIX seconds, like the provider interface.
    let venueUnknown = false
    if (endTime !== undefined && Number.isFinite(endTime)) {
      params.set('endTime', String(Math.floor(endTime)))
      // A closed-history page may be cached as immutable only under a URL naming the venue.
      if (this.venueId !== null) params.set('venue', this.venueId)
      else venueUnknown = true
    }
    // Without the venue the server answers no-cache; `no-cache` here also makes the browser
    // revalidate a copy an older build cached as immutable under this venue-less URL.
    const pending = this.requestJson(
      '/api/candles',
      params,
      `${sym} candles`,
      opts?.signal,
      venueUnknown ? 'no-cache' : undefined,
    )
    // Never waits for the venue: this page goes out without it, later pages carry it.
    if (venueUnknown) this.venue.get().catch(() => undefined)
    const body = await pending
    const candles = parseArray(body, toCandle, `${sym} candles`)
    // The contract promises ascending unique times; enforce it since Lightweight Charts
    // throws on duplicates (later row wins).
    candles.sort((a, b) => a.time - b.time)
    return dedupeByTime(candles)
  }

  /**
   * GET /api/symbols once per instance. The shared fetch ignores caller signals (one caller
   * aborting must not cancel it for others); each caller's signal only rejects its own promise.
   */
  getSymbols(opts?: { signal?: AbortSignal }): Promise<SymbolInfo[]> {
    return this.symbols.join(opts?.signal, 'symbols')
  }

  /**
   * The server's market-data venue, from `GET /api/health` → `venue`. One shared request per
   * instance (also used for the candle URLs' `venue` param); a failure is not cached, so the
   * next call retries. Each caller's signal only rejects its own promise.
   */
  getVenue(opts?: { signal?: AbortSignal }): Promise<VenueInfo> {
    return this.venue.join(opts?.signal, 'server health')
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
    const WS = resolveWebSocket(this.WebSocketImpl, 'subscribeCandles')
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

  /**
   * `/ws/tickers`. With `opts.symbols`, every socket sends `watchMessage(symbols())` when it
   * opens and again whenever the set's identity changes (on `opts.onSymbolsChange`, and checked
   * on each push as a fallback), so the server sends only watched symbols; entries outside the
   * set are also skipped client-side (an older server ignores `watch` and sends everything).
   */
  subscribeTickers(
    onTickers: (tickers: Ticker[]) => void,
    opts?: TickerSubscribeOptions,
  ): () => void {
    const BaseWS = resolveWebSocket(this.WebSocketImpl, 'subscribeTickers')
    const getSymbols = opts?.symbols
    let disposed = false
    /** The newest socket (older ones are discarded by the reconnecting socket). */
    const latest: { sock: WebSocket | null } = { sock: null }
    /** The set last sent on `latest.sock` (null: nothing sent on it yet). */
    let sentFor: ReadonlySet<string> | null = null

    const sendWatch = (): void => {
      if (disposed || getSymbols === undefined) return
      const sock = latest.sock
      if (sock === null || sock.readyState !== WS_OPEN) return
      const set = getSymbols()
      if (set === sentFor) return
      try {
        sock.send(watchMessage(set))
        sentFor = set
      } catch {
        // Closing; the next socket sends on open.
      }
    }

    let WS = BaseWS
    if (getSymbols !== undefined) {
      WS = class WatchingWebSocket extends BaseWS {
        constructor(url: string | URL, protocols?: string | string[]) {
          super(url, protocols)
          latest.sock = this
          sentFor = null
          this.addEventListener('open', () => {
            if (latest.sock === this) sendWatch()
          })
        }
      }
    }
    const offChange = getSymbols !== undefined ? opts?.onSymbolsChange?.(sendWatch) : undefined

    const teardown = this.openStream(
      '/ws/tickers',
      opts?.onStatus,
      (msg) => {
        if (msg.type !== 'tickers') return
        sendWatch()
        if (msg.tickers.length === 0) return
        try {
          onTickers(msg.tickers)
        } catch (err) {
          console.error('subscribeTickers: onTickers threw', err)
        }
      },
      WS,
      getSymbols,
    )
    return () => {
      if (disposed) return
      disposed = true
      latest.sock = null
      offChange?.()
      teardown()
    }
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
    only?: () => ReadonlySet<string>,
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
        const msg = parseStreamMessage(data, only?.())
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
