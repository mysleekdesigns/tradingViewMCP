import type { Candle, Interval, SymbolInfo, Ticker } from '../types'
import {
  DEFAULT_STALE_TIMEOUT_MS,
  openReconnectingSocket,
  parseKlineMessage,
} from './binanceStream'
import {
  MINI_TICKER_STREAM,
  normalizeSymbol,
  parseMiniTickerMessage,
  parseSymbolInfo,
  parseTicker24hr,
} from './binanceTicker'
import { MarketDataError, type MarketDataProvider, type SubscribeOptions } from './provider'
import { BINANCE_REST, BINANCE_WS, DIRECT_VENUE, type VenueInfo } from './venue'

/**
 * exchangeInfo query: `showPermissionSets=false` drops the per-symbol permission matrix
 * (17.8 MB → 6.8 MB on Binance global, 2026-10-24) without changing any field we read.
 */
const EXCHANGE_INFO_QUERY = 'showPermissionSets=false'

const MAX_LIMIT = 1000
const DEFAULT_TIMEOUT_MS = 10_000
/** Binance error code for "Invalid symbol." */
const INVALID_SYMBOL_CODE = -1121

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

export class BinanceProvider implements MarketDataProvider {
  private readonly baseUrl: string
  private readonly fetchFn: typeof fetch
  private readonly timeoutMs: number
  private readonly wsBaseUrl: string
  private readonly WebSocketImpl: typeof WebSocket | undefined
  private readonly staleTimeoutMs: number
  private readonly venue: VenueInfo
  /** Memoized exchangeInfo; cleared when the fetch fails so the next call retries. */
  private symbolsPromise: Promise<SymbolInfo[]> | null = null

  constructor(opts?: {
    baseUrl?: string
    fetchFn?: typeof fetch
    timeoutMs?: number
    wsBaseUrl?: string
    WebSocketImpl?: typeof WebSocket
    staleTimeoutMs?: number
    /** Venue reported by getVenue; pass `{ id: 'binance-us', ... }` with the Binance.US hosts. */
    venue?: VenueInfo
  }) {
    this.venue = opts?.venue ?? DIRECT_VENUE
    this.baseUrl = (opts?.baseUrl ?? BINANCE_REST).replace(/\/+$/, '')
    // Bind so the global fetch is not called with a foreign `this`.
    this.fetchFn = opts?.fetchFn ?? ((input, init) => globalThis.fetch(input, init))
    this.timeoutMs = opts?.timeoutMs ?? DEFAULT_TIMEOUT_MS
    this.wsBaseUrl = (opts?.wsBaseUrl ?? BINANCE_WS).replace(/\/+$/, '')
    this.WebSocketImpl = opts?.WebSocketImpl
    this.staleTimeoutMs = opts?.staleTimeoutMs ?? DEFAULT_STALE_TIMEOUT_MS
  }

  /**
   * GET `path?params` and return the parsed JSON body. Maps failures to MarketDataError:
   * caller abort -> aborted, timeout/transport -> network, 429 -> rate-limited (+Retry-After),
   * 418 -> banned, 451 -> region-blocked, 400 with code -1121 -> bad-symbol, other non-2xx ->
   * http, unparseable body -> parse. `what` names the resource in messages.
   */
  private async requestJson(
    path: string,
    params: URLSearchParams | null,
    what: string,
    callerSignal?: AbortSignal,
  ): Promise<unknown> {
    const query = params ? `?${params.toString()}` : ''
    const url = `${this.baseUrl}${path}${query}`
    const timeout = AbortSignal.timeout(this.timeoutMs)
    const signal = callerSignal ? AbortSignal.any([callerSignal, timeout]) : timeout

    let res: Response
    try {
      res = await this.fetchFn(url, { signal })
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

    if (!res.ok) {
      const retryAfter = parseRetryAfter(res)
      if (res.status === 429) {
        throw new MarketDataError('rate-limited', `Rate limited fetching ${what}`, 429, retryAfter)
      }
      if (res.status === 418) {
        throw new MarketDataError('banned', `IP banned by provider`, 418, retryAfter)
      }
      if (res.status === 451) {
        throw new MarketDataError('region-blocked', `Provider unavailable in this region`, 451)
      }
      const { code, msg } = await readErrorCode(res)
      if (res.status === 400 && code === INVALID_SYMBOL_CODE) {
        throw new MarketDataError('bad-symbol', `Unknown symbol in request for ${what}`, res.status)
      }
      const suffix = msg ? `: ${msg}` : ''
      throw new MarketDataError('http', `HTTP ${res.status} fetching ${what}${suffix}`, res.status)
    }

    try {
      return (await res.json()) as unknown
    } catch {
      throw new MarketDataError('parse', `Invalid JSON in ${what} response`)
    }
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
    let body: unknown
    try {
      body = await this.requestJson('/api/v3/klines', params, `${sym} candles`, opts?.signal)
    } catch (err) {
      if (err instanceof MarketDataError && err.kind === 'bad-symbol') {
        throw new MarketDataError('bad-symbol', `Unknown symbol: ${sym}`, err.status)
      }
      throw err
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

  private resolveWebSocket(caller: string): typeof WebSocket {
    const WS = this.WebSocketImpl ?? globalThis.WebSocket
    if (typeof WS !== 'function') {
      throw new Error(`${caller}: WebSocket is not available in this environment`)
    }
    return WS
  }

  subscribeCandles(
    symbol: string,
    interval: Interval,
    onCandle: (c: Candle, closed: boolean) => void,
    opts?: SubscribeOptions,
  ): () => void {
    const WS = this.resolveWebSocket('subscribeCandles')
    const sym = symbol.trim().toUpperCase()
    const expected = { symbol: sym, interval }
    return openReconnectingSocket({
      WebSocketImpl: WS,
      url: `${this.wsBaseUrl}/ws/${sym.toLowerCase()}@kline_${interval}`,
      staleTimeoutMs: this.staleTimeoutMs,
      onStatus: opts?.onStatus,
      onMessage: (data) => {
        const parsed = parseKlineMessage(data, expected)
        if (parsed === null) return
        try {
          onCandle(parsed.candle, parsed.closed)
        } catch (err) {
          console.error('subscribeCandles: onCandle threw', err)
        }
      },
    })
  }

  /**
   * GET /api/v3/exchangeInfo once per instance. Malformed symbol entries (missing
   * strings or no usable PRICE_FILTER tickSize) are skipped, not fatal; a body without a
   * `symbols` array rejects with 'parse'. The shared fetch ignores caller signals (one
   * caller aborting must not cancel it for others); each caller's own signal only
   * rejects that caller's promise with 'aborted'.
   */
  getVenue(): Promise<VenueInfo> {
    return Promise.resolve(this.venue)
  }

  getSymbols(opts?: { signal?: AbortSignal }): Promise<SymbolInfo[]> {
    if (opts?.signal?.aborted) {
      return Promise.reject(new MarketDataError('aborted', 'Request for symbols was cancelled'))
    }
    if (this.symbolsPromise === null) {
      const p = this.fetchSymbols()
      this.symbolsPromise = p
      p.catch(() => {
        if (this.symbolsPromise === p) this.symbolsPromise = null
      })
    }
    return withAbort(this.symbolsPromise, opts?.signal, 'symbols')
  }

  private async fetchSymbols(): Promise<SymbolInfo[]> {
    const body = await this.requestJson(
      '/api/v3/exchangeInfo',
      new URLSearchParams(EXCHANGE_INFO_QUERY),
      'exchange info',
    )
    if (body === null || typeof body !== 'object' || Array.isArray(body)) {
      throw new MarketDataError('parse', 'Expected an exchangeInfo object')
    }
    const raw = (body as { symbols?: unknown }).symbols
    if (!Array.isArray(raw)) {
      throw new MarketDataError('parse', 'exchangeInfo has no symbols array')
    }
    const out: SymbolInfo[] = []
    for (const entry of raw) {
      const info = parseSymbolInfo(entry)
      if (info !== null) out.push(info)
    }
    return out
  }

  /**
   * One request: GET /api/v3/ticker/24hr?symbols=["A","B"] (both Binance hosts support it). Input
   * is trimmed, uppercased, de-duplicated; entries that are not valid symbol syntax are
   * dropped locally. Binance fails the WHOLE request with -1121 if any symbol is unknown,
   * so on bad-symbol with more than one symbol we fall back to one `symbol=X` request per
   * symbol in parallel and omit the ones that return bad-symbol; any other error rejects.
   * Malformed ticker objects are skipped. Result order follows the input order.
   */
  async getTickers(symbols: readonly string[], opts?: { signal?: AbortSignal }): Promise<Ticker[]> {
    const wanted: string[] = []
    for (const s of symbols) {
      const n = normalizeSymbol(s)
      if (n !== null && !wanted.includes(n)) wanted.push(n)
    }
    if (wanted.length === 0) return []
    const signal = opts?.signal

    let entries: unknown[]
    try {
      const body = await this.requestJson(
        '/api/v3/ticker/24hr',
        new URLSearchParams({ symbols: JSON.stringify(wanted) }),
        '24h tickers',
        signal,
      )
      if (!Array.isArray(body)) {
        throw new MarketDataError('parse', 'Expected an array of 24h tickers')
      }
      entries = body
    } catch (err) {
      if (!(err instanceof MarketDataError) || err.kind !== 'bad-symbol') throw err
      if (wanted.length === 1) return []
      const results = await Promise.all(
        wanted.map((sym) =>
          this.requestJson(
            '/api/v3/ticker/24hr',
            new URLSearchParams({ symbol: sym }),
            `${sym} 24h ticker`,
            signal,
          ).catch((e: unknown) => {
            if (e instanceof MarketDataError && e.kind === 'bad-symbol') return null
            throw e
          }),
        ),
      )
      entries = results.filter((r) => r !== null)
    }

    const bySymbol = new Map<string, Ticker>()
    for (const entry of entries) {
      const t = parseTicker24hr(entry)
      if (t !== null) bySymbol.set(t.symbol.toUpperCase(), t)
    }
    const out: Ticker[] = []
    for (const sym of wanted) {
      const t = bySymbol.get(sym)
      if (t !== undefined) out.push(t)
    }
    return out
  }

  subscribeTickers(onTickers: (tickers: Ticker[]) => void, opts?: SubscribeOptions): () => void {
    const WS = this.resolveWebSocket('subscribeTickers')
    return openReconnectingSocket({
      WebSocketImpl: WS,
      url: `${this.wsBaseUrl}/ws/${MINI_TICKER_STREAM}`,
      staleTimeoutMs: this.staleTimeoutMs,
      onStatus: opts?.onStatus,
      onMessage: (data) => {
        const tickers = parseMiniTickerMessage(data)
        if (tickers.length === 0) return
        try {
          onTickers(tickers)
        } catch (err) {
          console.error('subscribeTickers: onTickers threw', err)
        }
      },
    })
  }
}
