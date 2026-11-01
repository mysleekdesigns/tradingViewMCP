import type { Candle, Interval, SymbolInfo, Ticker } from '../types'
import {
  DEFAULT_STALE_TIMEOUT_MS,
  openReconnectingSocket,
  parseKlineMessage,
  resolveWebSocket,
} from './binanceStream'
import {
  MINI_TICKER_STREAM,
  normalizeSymbol,
  parseMiniTickerMessage,
  parseSymbolInfo,
  parseTicker24hr,
} from './binanceTicker'
import { dedupeByTime, isRecord, toFiniteNumber, type FiniteFieldWording } from './guards'
import {
  SharedLoad,
  bodyString,
  clampLimit,
  exchangeError,
  requestMarketJson,
  type ExchangeErrorRules,
} from './http'
import {
  MarketDataError,
  type MarketDataProvider,
  type SubscribeOptions,
  type TickerSubscribeOptions,
} from './provider'
import type { SecondsSource } from './timeframeProvider'
import { BINANCE_REST, BINANCE_WS, DIRECT_VENUE, type VenueInfo } from './venue'

/**
 * exchangeInfo query: `showPermissionSets=false` drops the per-symbol permission matrix
 * (17.8 MB → 6.8 MB on Binance global, 2026-10-24) without changing any field we read.
 */
const EXCHANGE_INFO_QUERY = 'showPermissionSets=false'

const DEFAULT_TIMEOUT_MS = 10_000
/** Binance error code for "Invalid symbol." */
const INVALID_SYMBOL_CODE = -1121

/** Error wording of kline row fields. */
const KLINE_FIELD: FiniteFieldWording = {
  prefix: 'kline',
  wrongType: 'expected string or number',
  notFinite: 'not a finite number',
}

/** Binance error answers: `{ code, msg }`; 400 with code -1121 is an unknown symbol. */
function binanceErrorRules(what: string): ExchangeErrorRules {
  return {
    detail: (body) => bodyString(body, 'msg'),
    badSymbol: (status, body) =>
      status === 400 && isRecord(body) && body.code === INVALID_SYMBOL_CODE
        ? `Unknown symbol in request for ${what}`
        : null,
  }
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
  const openTimeMs = toFiniteNumber(row[0], 'openTime', KLINE_FIELD)
  return {
    time: Math.floor(openTimeMs / 1000),
    open: toFiniteNumber(row[1], 'open', KLINE_FIELD),
    high: toFiniteNumber(row[2], 'high', KLINE_FIELD),
    low: toFiniteNumber(row[3], 'low', KLINE_FIELD),
    close: toFiniteNumber(row[4], 'close', KLINE_FIELD),
    volume: toFiniteNumber(row[5], 'volume', KLINE_FIELD),
  }
}

export interface BinanceProviderOptions {
  baseUrl?: string
  fetchFn?: typeof fetch
  timeoutMs?: number
  wsBaseUrl?: string
  WebSocketImpl?: typeof WebSocket
  staleTimeoutMs?: number
  /** Venue reported by getVenue; pass `{ id: 'binance-us', ... }` with the Binance.US hosts. */
  venue?: VenueInfo
}

/** A kline interval Binance serves: every app Interval plus `1s` (Binance-only, not Coinbase). */
type KlineInterval = Interval | '1s'

export class BinanceProvider implements MarketDataProvider {
  private readonly baseUrl: string
  private readonly fetchFn: typeof fetch
  private readonly timeoutMs: number
  private readonly wsBaseUrl: string
  private readonly WebSocketImpl: typeof WebSocket | undefined
  private readonly staleTimeoutMs: number
  private readonly venue: VenueInfo
  /** Memoized exchangeInfo; forgotten when the fetch fails so the next call retries. */
  private readonly symbols = new SharedLoad(() => this.fetchSymbols())

  constructor(opts?: BinanceProviderOptions) {
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
  private requestJson(
    path: string,
    params: URLSearchParams | null,
    what: string,
    callerSignal?: AbortSignal,
  ): Promise<unknown> {
    const query = params ? `?${params.toString()}` : ''
    return requestMarketJson({
      fetchFn: this.fetchFn,
      url: `${this.baseUrl}${path}${query}`,
      what,
      timeoutMs: this.timeoutMs,
      signal: callerSignal,
      toError: (res) => exchangeError(res, what, binanceErrorRules(what)),
    })
  }

  getCandles(
    symbol: string,
    interval: Interval,
    limit = 500,
    endTime?: number,
    opts?: { signal?: AbortSignal },
  ): Promise<Candle[]> {
    return this.fetchKlines(symbol, interval, limit, endTime, opts)
  }

  /** `1s` klines from the same hosts, for seconds timeframes (see createTimeframeProvider). */
  secondsSource(): SecondsSource {
    return {
      getCandles: (symbol, _interval, limit = 500, endTime, opts) =>
        this.fetchKlines(symbol, '1s', limit, endTime, opts),
      subscribeCandles: (symbol, _interval, onCandle, opts) =>
        this.streamKlines(symbol, '1s', onCandle, opts),
    }
  }

  private async fetchKlines(
    symbol: string,
    interval: KlineInterval,
    limit: number,
    endTime: number | undefined,
    opts: { signal?: AbortSignal } | undefined,
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

  subscribeCandles(
    symbol: string,
    interval: Interval,
    onCandle: (c: Candle, closed: boolean) => void,
    opts?: SubscribeOptions,
  ): () => void {
    return this.streamKlines(symbol, interval, onCandle, opts)
  }

  private streamKlines(
    symbol: string,
    interval: KlineInterval,
    onCandle: (c: Candle, closed: boolean) => void,
    opts: SubscribeOptions | undefined,
  ): () => void {
    const WS = resolveWebSocket(this.WebSocketImpl, 'subscribeCandles')
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
    return this.symbols.join(opts?.signal, 'symbols')
  }

  private async fetchSymbols(): Promise<SymbolInfo[]> {
    const body = await this.requestJson(
      '/api/v3/exchangeInfo',
      new URLSearchParams(EXCHANGE_INFO_QUERY),
      'exchange info',
    )
    if (!isRecord(body)) throw new MarketDataError('parse', 'Expected an exchangeInfo object')
    const raw = body.symbols
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

  /**
   * One `!miniTicker@arr` socket. With `opts.symbols`, each push keeps only the symbols in the
   * set read at that push (checked on the raw `s` before any Ticker is built).
   */
  subscribeTickers(
    onTickers: (tickers: Ticker[]) => void,
    opts?: TickerSubscribeOptions,
  ): () => void {
    const WS = resolveWebSocket(this.WebSocketImpl, 'subscribeTickers')
    const watched = opts?.symbols
    return openReconnectingSocket({
      WebSocketImpl: WS,
      url: `${this.wsBaseUrl}/ws/${MINI_TICKER_STREAM}`,
      staleTimeoutMs: this.staleTimeoutMs,
      onStatus: opts?.onStatus,
      onMessage: (data) => {
        const tickers = parseMiniTickerMessage(data, watched?.())
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

/**
 * Binance `1s` klines (REST `/api/v3/klines?interval=1s`, WS `<symbol>@kline_1s`) on the
 * given hosts (default: Binance's market-data-only hosts). Pass it to createTimeframeProvider
 * to enable seconds timeframes.
 */
export function createBinanceSecondsSource(opts?: BinanceProviderOptions): SecondsSource {
  return new BinanceProvider(opts).secondsSource()
}
