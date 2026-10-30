import type { Candle, Interval, SymbolInfo, Ticker } from '../types'
import { DEFAULT_STALE_TIMEOUT_MS } from './binanceStream'
import {
  DEFAULT_FORMING_FLUSH_MS,
  makeTickerFields,
  openCoinbaseCandleStream,
  openCoinbaseTickerStream,
} from './coinbaseStream'
import {
  MarketDataError,
  type MarketDataProvider,
  type SubscribeOptions,
  type TickerSubscribeOptions,
} from './provider'
import { COINBASE_REST, COINBASE_VENUE, COINBASE_WS, type VenueInfo } from './venue'

/*
 * Coinbase Exchange public REST (https://api.exchange.coinbase.com), verified live 2026-10-25
 * with no credentials:
 * - CORS: every response carries `Access-Control-Allow-Origin: *`, so the browser can call it.
 * - GET /products: array of `{ id: 'BTC-USD', base_currency, quote_currency, quote_increment,
 *   status: 'online' | 'delisted', trading_disabled, cancel_only, ... }` (843 products, 515
 *   online). Ids are always `${base}-${quote}`, but we never split them: symbol <-> id comes
 *   from base/quote.
 * - GET /products/{id}/candles?granularity=G&start=ISO&end=ISO: rows `[time(s), low, high,
 *   open, close, volume]` as numbers, NEWEST FIRST. G only 60/300/900/3600/21600/86400
 *   (else 400 "Unsupported granularity"). start and end are inclusive and bucket-aligned;
 *   (end - start) / G must be <= 300 (a span of exactly 300*G returns 301 rows), else 400.
 *   Buckets without trades are omitted. The newest row is the still-forming bar. 1d rows
 *   start at 00:00 UTC. An `end` in the future is accepted, and an unaligned `end` includes the
 *   bucket it falls in.
 * - Caching: candles are `Cache-Control: public, max-age=300` and served by Cloudflare
 *   (`cf-cache-status: HIT`, `age` up to 300), so a bucket-aligned URL that anyone requested in
 *   the last 5 minutes returns stale recent bars. A per-second unaligned `end` is a cache MISS
 *   with fresh rows; see `pageEndParam`.
 * - GET /products/{id}/stats: `{ open, high, low, last, volume, volume_30day, ... }` strings,
 *   rolling 24h, no time and no quote volume. GET /products/stats: the same for every product
 *   as `{ [id]: { stats_24hour: {...}, stats_30day: {...} } }` (~100 KB).
 * - Unknown product: 404 `{"message":"NotFound"}`. Errors are `{ message }`.
 * - Rate limit (docs.cdp.coinbase.com/exchange/rest-api/rate-limits): public endpoints 10
 *   requests/s per IP, bursts up to 15; over the limit -> 429. Every request here goes through
 *   one per-instance throttle (8/s by default).
 */

/** Max rows Coinbase returns per candles request. */
const MAX_ROWS_PER_REQUEST = 300
/** Same cap the app uses for Binance; keeps a 1w/4h request to at most 24 pages. */
const MAX_LIMIT = 1000
const DEFAULT_TIMEOUT_MS = 10_000
/** 8 requests/s: under the documented 10/s public limit, leaving room for other tabs. */
const DEFAULT_MIN_REQUEST_INTERVAL_MS = 125
/** Wait applied to the throttle after a 429 that has no Retry-After. */
const DEFAULT_RATE_LIMIT_PAUSE_S = 1
/** getTickers with at most this many symbols uses per-product /stats; more uses one bulk call. */
const PER_PRODUCT_TICKERS_MAX = 4
/** Coinbase's candle Cache-Control max-age (seconds). */
const CANDLE_CACHE_MAX_AGE_S = 300
/**
 * Extra requests getCandles may spend stepping back past an empty window (illiquid pairs: Coinbase
 * omits empty buckets). Worst case for 1m: 5 pages to the start of the day, 1 daily probe, 5
 * pages into the traded day. Daily probes cover 300 days each, so the cap reaches ~10 years back.
 */
const MAX_EMPTY_STEP_REQUESTS = 12
/** 2015-01-01 UTC: before Coinbase Exchange's first candle; stepping back stops here. */
export const COINBASE_EARLIEST_S = 1_420_070_400

/**
 * The `end` query value for a candle page ending at bucket `pageEnd` (granularity `g`). Pages
 * whose rows could still change within the CDN cache lifetime get an unaligned end inside the
 * same bucket, `pageEnd + 1 + (now % (g - 1))`: the same rows, but a URL that changes every
 * second, so neither Cloudflare nor the browser serves a stale copy. Older pages keep the
 * aligned (cacheable) URL; closed history does not change.
 */
export function pageEndParam(pageEnd: number, g: number, nowS: number): number {
  if (pageEnd + g + CANDLE_CACHE_MAX_AGE_S < nowS) return pageEnd
  return pageEnd + 1 + (nowS % (g - 1))
}

const DAY = 86_400
/** 1970-01-01 was a Thursday; Monday 1970-01-05 00:00 UTC is a week boundary. */
const WEEK_ORIGIN = 4 * DAY

/** App symbol syntax (no separators). */
const SYMBOL_RE = /^[A-Z0-9]{2,30}$/

interface IntervalSpec {
  /** Coinbase granularity fetched, seconds. */
  granularity: number
  /** Bucket length, seconds. */
  bucket: number
  /** Bucket grid origin, UNIX seconds. */
  origin: number
}

/**
 * 4h aggregates 1h rows on the UTC 00/04/08/... grid; 1w aggregates 1d rows on Monday 00:00
 * UTC. Both match Binance's bucketing.
 */
const INTERVAL_SPEC: Readonly<Record<Interval, IntervalSpec>> = {
  '1m': { granularity: 60, bucket: 60, origin: 0 },
  '5m': { granularity: 300, bucket: 300, origin: 0 },
  '15m': { granularity: 900, bucket: 900, origin: 0 },
  '1h': { granularity: 3600, bucket: 3600, origin: 0 },
  '4h': { granularity: 3600, bucket: 4 * 3600, origin: 0 },
  '1d': { granularity: DAY, bucket: DAY, origin: 0 },
  '1w': { granularity: DAY, bucket: 7 * DAY, origin: WEEK_ORIGIN },
}

/** Open time (UNIX seconds) of the `interval` bucket containing `t`. */
export function bucketStart(t: number, interval: Interval): number {
  const { bucket, origin } = INTERVAL_SPEC[interval]
  return Math.floor((t - origin) / bucket) * bucket + origin
}

function toFiniteNumber(value: unknown, field: string): number {
  if (typeof value !== 'string' && typeof value !== 'number') {
    throw new MarketDataError('parse', `candle ${field}: expected a number`)
  }
  if (typeof value === 'string' && value.trim() === '') {
    throw new MarketDataError('parse', `candle ${field}: empty value`)
  }
  const n = Number(value)
  if (!Number.isFinite(n)) throw new MarketDataError('parse', `candle ${field}: not finite`)
  return n
}

/** One Coinbase candle row `[time(s), low, high, open, close, volume]` -> Candle. */
export function parseCoinbaseCandle(row: unknown): Candle {
  if (!Array.isArray(row) || row.length < 6) {
    throw new MarketDataError('parse', 'candle row: expected an array with 6 fields')
  }
  return {
    time: Math.floor(toFiniteNumber(row[0], 'time')),
    low: toFiniteNumber(row[1], 'low'),
    high: toFiniteNumber(row[2], 'high'),
    open: toFiniteNumber(row[3], 'open'),
    close: toFiniteNumber(row[4], 'close'),
    volume: toFiniteNumber(row[5], 'volume'),
  }
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

/**
 * Aggregate ascending, unique component candles into `interval` buckets (open of the first
 * component, max high, min low, close of the last, summed volume). Buckets with no components
 * are omitted (no forward fill).
 *
 * Partial buckets: getCandles only ever requests component windows aligned to whole buckets,
 * so a missing component (including the first) means Coinbase had no trades in it (it omits
 * empty slots), not that we failed to fetch it; the bucket then opens at its first traded
 * component, as Coinbase's own candles do. `firstBucket` drops any bucket that starts before
 * the requested window, so a truncated leading bucket can never be emitted as complete. The
 * newest bucket may still be forming, like the newest row of any getCandles result.
 */
export function aggregateCandles(
  components: readonly Candle[],
  interval: Interval,
  firstBucket = -Infinity,
): Candle[] {
  const out: Candle[] = []
  for (const c of components) {
    const b = bucketStart(c.time, interval)
    if (b < firstBucket) continue
    const cur = out[out.length - 1]
    if (cur !== undefined && cur.time === b) {
      cur.high = Math.max(cur.high, c.high)
      cur.low = Math.min(cur.low, c.low)
      cur.close = c.close
      cur.volume += c.volume
    } else {
      out.push({
        time: b,
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
        volume: c.volume,
      })
    }
  }
  return out
}

/** A Coinbase product as the app sees it. */
export interface CoinbaseProduct {
  productId: string
  info: SymbolInfo
}

function nonEmptyString(v: unknown): v is string {
  return typeof v === 'string' && v.trim() !== ''
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return v !== null && typeof v === 'object' && !Array.isArray(v)
}

/**
 * One /products entry -> `{ productId, info }`, or null when malformed. The app symbol is
 * `${base}${quote}` uppercased (BTC-USD -> BTCUSD). Status is 'TRADING' when online and not
 * disabled/cancel-only, 'HALT' when online but not tradable, else the Coinbase status
 * uppercased (e.g. 'DELISTED'). tickSize = quote_increment.
 */
export function parseProduct(entry: unknown): CoinbaseProduct | null {
  if (!isRecord(entry)) return null
  const { id, base_currency: base, quote_currency: quote, status } = entry
  if (!nonEmptyString(id) || !nonEmptyString(base) || !nonEmptyString(quote)) return null
  if (!nonEmptyString(status)) return null
  const tickSize = Number(entry.quote_increment)
  if (!Number.isFinite(tickSize) || tickSize <= 0) return null
  const baseAsset = base.trim().toUpperCase()
  const quoteAsset = quote.trim().toUpperCase()
  const symbol = `${baseAsset}${quoteAsset}`
  if (!SYMBOL_RE.test(symbol)) return null
  let st: string
  if (status === 'online') {
    st = entry.trading_disabled === true || entry.cancel_only === true ? 'HALT' : 'TRADING'
  } else {
    st = status.trim().toUpperCase()
  }
  return { productId: id, info: { symbol, baseAsset, quoteAsset, status: st, tickSize } }
}

export interface CoinbaseCatalog {
  symbols: SymbolInfo[]
  /** App symbol -> product id. */
  toProduct: ReadonlyMap<string, string>
  /** Product id -> app symbol. */
  toSymbol: ReadonlyMap<string, string>
}

/**
 * Build the symbol list and both maps. Dropping the dash can make two products collide
 * (e.g. `AB-CUSD` and `ABC-USD` would both be ABCUSD): when exactly one of them is TRADING it
 * wins; otherwise the symbol is left out entirely rather than guessing.
 */
export function buildCatalog(products: readonly unknown[]): CoinbaseCatalog {
  const bySymbol = new Map<string, CoinbaseProduct[]>()
  for (const entry of products) {
    const p = parseProduct(entry)
    if (p === null) continue
    const list = bySymbol.get(p.info.symbol)
    if (list === undefined) bySymbol.set(p.info.symbol, [p])
    else list.push(p)
  }
  const symbols: SymbolInfo[] = []
  const toProduct = new Map<string, string>()
  const toSymbol = new Map<string, string>()
  for (const [symbol, list] of bySymbol) {
    let chosen: CoinbaseProduct | undefined
    if (list.length === 1) chosen = list[0]
    else {
      const trading = list.filter((p) => p.info.status === 'TRADING')
      if (trading.length === 1) chosen = trading[0]
    }
    if (chosen === undefined) continue
    symbols.push(chosen.info)
    toProduct.set(symbol, chosen.productId)
    toSymbol.set(chosen.productId, symbol)
  }
  return { symbols, toProduct, toSymbol }
}

/** Coinbase 24h stats object (`open, high, low, last, volume` strings) -> Ticker, or null. */
export function parseStats(entry: unknown, symbol: string, time: number): Ticker | null {
  if (!isRecord(entry)) return null
  const num = (v: unknown): number | null => {
    if (typeof v !== 'string' && typeof v !== 'number') return null
    if (typeof v === 'string' && v.trim() === '') return null
    const n = Number(v)
    return Number.isFinite(n) ? n : null
  }
  const last = num(entry.last)
  const open = num(entry.open)
  const high = num(entry.high)
  const low = num(entry.low)
  const volume = num(entry.volume)
  if (last === null || open === null || high === null || low === null || volume === null) {
    return null
  }
  return { symbol, ...makeTickerFields({ last, open, high, low, volume }, time) }
}

/** `Retry-After` in seconds; undefined when absent or invalid. */
function parseRetryAfter(res: Response): number | undefined {
  const raw = res.headers.get('Retry-After')
  if (raw === null || raw.trim() === '') return undefined
  const n = Number(raw)
  return Number.isFinite(n) && n >= 0 ? n : undefined
}

async function readMessage(res: Response): Promise<string | undefined> {
  try {
    const body: unknown = await res.json()
    if (isRecord(body) && typeof body.message === 'string') return body.message
  } catch {
    // Non-JSON error body.
  }
  return undefined
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

function clampLimit(limit: number): number {
  if (!Number.isFinite(limit)) return 500
  return Math.min(MAX_LIMIT, Math.max(1, Math.floor(limit)))
}

function isoSeconds(t: number): string {
  return new Date(t * 1000).toISOString().replace('.000Z', 'Z')
}

/** Shared arguments of one getCandles call's requests. */
interface CandleRequest {
  pid: string
  sym: string
  interval: Interval
  nowS: number
  signal: AbortSignal | undefined
}

export interface CoinbaseProviderOptions {
  baseUrl?: string
  fetchFn?: typeof fetch
  timeoutMs?: number
  wsBaseUrl?: string
  WebSocketImpl?: typeof WebSocket
  staleTimeoutMs?: number
  /** Minimum spacing between REST requests (default 125 ms = 8/s). */
  minRequestIntervalMs?: number
  /** Delay before reading a just-closed bar back from REST (default 1.5 s). */
  reconcileDelayMs?: number
  /** Clock, ms (tests). */
  now?: () => number
}

/**
 * Coinbase Exchange spot market data straight from the browser. Symbols are exposed without
 * the dash (BTC-USD -> BTCUSD); 4h and 1w are aggregated from 1h / 1d; live candles are built
 * from the `matches` channel (see openCoinbaseCandleStream).
 */
export class CoinbaseProvider implements MarketDataProvider {
  private readonly baseUrl: string
  private readonly fetchFn: typeof fetch
  private readonly timeoutMs: number
  private readonly wsBaseUrl: string
  private readonly WebSocketImpl: typeof WebSocket | undefined
  private readonly staleTimeoutMs: number
  private readonly minRequestIntervalMs: number
  private readonly reconcileDelayMs: number | undefined
  private readonly now: () => number
  /** Memoized product catalog; cleared when the fetch fails so the next call retries. */
  private catalogPromise: Promise<CoinbaseCatalog> | null = null
  /** Earliest time (ms) the next REST request may start. */
  private nextRequestAt = 0

  constructor(opts?: CoinbaseProviderOptions) {
    this.baseUrl = (opts?.baseUrl ?? COINBASE_REST).replace(/\/+$/, '')
    // Bind so the global fetch is not called with a foreign `this`.
    this.fetchFn = opts?.fetchFn ?? ((input, init) => globalThis.fetch(input, init))
    this.timeoutMs = opts?.timeoutMs ?? DEFAULT_TIMEOUT_MS
    this.wsBaseUrl = (opts?.wsBaseUrl ?? COINBASE_WS).replace(/\/+$/, '')
    this.WebSocketImpl = opts?.WebSocketImpl
    this.staleTimeoutMs = opts?.staleTimeoutMs ?? DEFAULT_STALE_TIMEOUT_MS
    this.minRequestIntervalMs = opts?.minRequestIntervalMs ?? DEFAULT_MIN_REQUEST_INTERVAL_MS
    this.reconcileDelayMs = opts?.reconcileDelayMs
    this.now = opts?.now ?? (() => Date.now())
  }

  /** Wait for this instance's next request slot (spacing every REST call, any caller). */
  private async throttle(signal: AbortSignal | undefined, what: string): Promise<void> {
    const now = this.now()
    const at = Math.max(now, this.nextRequestAt)
    this.nextRequestAt = at + this.minRequestIntervalMs
    const wait = at - now
    if (wait <= 0) return
    await withAbort(new Promise<void>((r) => setTimeout(r, wait)), signal, what)
  }

  /**
   * Throttled GET `path?params` -> parsed JSON. Errors: caller abort -> aborted,
   * timeout/transport -> network, 429 -> rate-limited (+Retry-After, also pauses the throttle),
   * 418 -> banned, 451 -> region-blocked, 404 on a product path -> bad-symbol, other non-2xx ->
   * http, unparseable body -> parse.
   */
  private async requestJson(
    path: string,
    params: URLSearchParams | null,
    what: string,
    callerSignal?: AbortSignal,
    productScoped = false,
  ): Promise<unknown> {
    await this.throttle(callerSignal, what)
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
        const pause = (retryAfter ?? DEFAULT_RATE_LIMIT_PAUSE_S) * 1000
        this.nextRequestAt = Math.max(this.nextRequestAt, this.now() + pause)
        throw new MarketDataError('rate-limited', `Rate limited fetching ${what}`, 429, retryAfter)
      }
      if (res.status === 418) {
        throw new MarketDataError('banned', 'IP banned by provider', 418, retryAfter)
      }
      if (res.status === 451) {
        throw new MarketDataError('region-blocked', 'Provider unavailable in this region', 451)
      }
      const msg = await readMessage(res)
      if (res.status === 404 && productScoped) {
        throw new MarketDataError('bad-symbol', `Unknown product in request for ${what}`, 404)
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

  private catalog(signal?: AbortSignal): Promise<CoinbaseCatalog> {
    if (signal?.aborted) {
      return Promise.reject(new MarketDataError('aborted', 'Request for symbols was cancelled'))
    }
    if (this.catalogPromise === null) {
      const p = this.fetchCatalog()
      this.catalogPromise = p
      p.catch(() => {
        if (this.catalogPromise === p) this.catalogPromise = null
      })
    }
    return withAbort(this.catalogPromise, signal, 'symbols')
  }

  private async fetchCatalog(): Promise<CoinbaseCatalog> {
    const body = await this.requestJson('/products', null, 'products')
    if (!Array.isArray(body)) throw new MarketDataError('parse', 'Expected an array of products')
    return buildCatalog(body)
  }

  /** App symbol -> product id via the cached catalog; unknown -> bad-symbol. */
  private async productId(symbol: string, signal?: AbortSignal): Promise<string> {
    const sym = symbol.trim().toUpperCase()
    const cat = await this.catalog(signal)
    const id = cat.toProduct.get(sym)
    if (id === undefined) throw new MarketDataError('bad-symbol', `Unknown symbol: ${sym}`)
    return id
  }

  getVenue(): Promise<VenueInfo> {
    return Promise.resolve(COINBASE_VENUE)
  }

  /** GET /products once per instance (shared; a caller's abort only rejects that caller). */
  async getSymbols(opts?: { signal?: AbortSignal }): Promise<SymbolInfo[]> {
    const cat = await this.catalog(opts?.signal)
    return cat.symbols
  }

  /**
   * The last `limit` bars whose open time is <= `endTime` (UNIX s; default now), like
   * Binance's endTime. Fetches the aligned component window in pages of <= 300 rows,
   * sequentially through the throttle.
   *
   * Empty windows: Coinbase omits buckets without trades, so on an illiquid pair a window can
   * come back empty while older history exists. Then this keeps stepping further back (one
   * request of <= 300 rows at a time, throttled): component pages down to the start of the
   * UTC day, then daily-granularity probes (300 days each) to find the newest traded day, then
   * component pages inside that day. It stops at the first non-empty page (returning its bars,
   * possibly fewer than `limit`), after 12 extra requests (~10 years of daily probes), or once
   * it passes 2015-01-01 (before any Coinbase candle). `[]` therefore means history is
   * exhausted, which is how callers read it.
   */
  async getCandles(
    symbol: string,
    interval: Interval,
    limit = 500,
    endTime?: number,
    opts?: { signal?: AbortSignal },
  ): Promise<Candle[]> {
    return this.candles(symbol, interval, limit, endTime, opts?.signal, true)
  }

  /** getCandles; `stepBack` false returns an empty window as-is (live stream read-backs). */
  private async candles(
    symbol: string,
    interval: Interval,
    limit: number,
    endTime: number | undefined,
    signal: AbortSignal | undefined,
    stepBack: boolean,
  ): Promise<Candle[]> {
    const sym = symbol.trim().toUpperCase()
    const pid = await this.productId(sym, signal)
    const spec = INTERVAL_SPEC[interval]
    const lim = clampLimit(limit)
    const nowS = Math.floor(this.now() / 1000)
    const end = endTime === undefined ? nowS : Math.min(Math.floor(endTime), nowS)
    const lastBucket = bucketStart(end, interval)
    const firstBucket = lastBucket - (lim - 1) * spec.bucket
    const req: CandleRequest = { pid, sym, interval, nowS, signal }
    const bars = await this.fetchBars(req, firstBucket, lastBucket)
    if (bars.length > 0 || !stepBack) return bars.slice(-lim)
    return (await this.stepBack(req, firstBucket)).slice(-lim)
  }

  /** Bars of buckets `firstBucket..lastBucket` (aligned, inclusive), in pages of <= 300 rows. */
  private async fetchBars(
    req: CandleRequest,
    firstBucket: number,
    lastBucket: number,
  ): Promise<Candle[]> {
    const spec = INTERVAL_SPEC[req.interval]
    const g = spec.granularity
    const compEnd = Math.min(lastBucket + spec.bucket - g, Math.floor(req.nowS / g) * g)
    const rows: Candle[] = []
    for (let start = firstBucket; start <= compEnd; start += MAX_ROWS_PER_REQUEST * g) {
      const pageEnd = Math.min(start + (MAX_ROWS_PER_REQUEST - 1) * g, compEnd)
      for (const r of await this.fetchRows(req, g, start, pageEnd)) rows.push(r)
    }
    // Newest first per page: sort ascending, dedupe (Lightweight Charts throws on duplicates),
    // and keep only the requested window.
    rows.sort((a, b) => a.time - b.time)
    const components = dedupeByTime(rows).filter((c) => c.time >= firstBucket && c.time <= compEnd)
    return spec.bucket === g ? components : aggregateCandles(components, req.interval, firstBucket)
  }

  /** One candles request: rows of granularity `g` from `start` to `pageEnd` (aligned). */
  private async fetchRows(
    req: CandleRequest,
    g: number,
    start: number,
    pageEnd: number,
  ): Promise<Candle[]> {
    const params = new URLSearchParams({
      granularity: String(g),
      start: isoSeconds(start),
      end: isoSeconds(pageEndParam(pageEnd, g, req.nowS)),
    })
    let body: unknown
    try {
      body = await this.requestJson(
        `/products/${encodeURIComponent(req.pid)}/candles`,
        params,
        `${req.sym} candles`,
        req.signal,
        true,
      )
    } catch (err) {
      if (err instanceof MarketDataError && err.kind === 'bad-symbol') {
        throw new MarketDataError('bad-symbol', `Unknown symbol: ${req.sym}`, err.status)
      }
      throw err
    }
    if (!Array.isArray(body)) {
      throw new MarketDataError('parse', `Expected an array of candles for ${req.sym}`)
    }
    return body.map(parseCoinbaseCandle)
  }

  /**
   * The newest non-empty page of bars older than `before` (a bucket start), or [] when none
   * is found within MAX_EMPTY_STEP_REQUESTS requests or before COINBASE_EARLIEST_S. Sub-day
   * intervals page to the start of the current UTC day, then probe with daily rows for the
   * newest traded day and page inside it; 1d/1w page directly (300 days per request).
   */
  private async stepBack(req: CandleRequest, before: number): Promise<Candle[]> {
    const spec = INTERVAL_SPEC[req.interval]
    const g = spec.granularity
    const perPage = Math.max(1, Math.floor((MAX_ROWS_PER_REQUEST * g) / spec.bucket))
    let cursor = before
    // Component pages run down to `floor`; below it a daily probe decides where to go next.
    let floor = g < DAY ? Math.floor(cursor / DAY) * DAY : -Infinity
    for (let n = 0; n < MAX_EMPTY_STEP_REQUESTS && cursor > COINBASE_EARLIEST_S; n++) {
      if (cursor > floor) {
        const first = Math.max(floor, cursor - perPage * spec.bucket)
        const bars = await this.fetchBars(req, first, cursor - spec.bucket)
        if (bars.length > 0) return bars
        cursor = first
        continue
      }
      const probeStart = cursor - MAX_ROWS_PER_REQUEST * DAY
      const probeEnd = cursor - DAY
      const days = (await this.fetchRows(req, DAY, probeStart, probeEnd)).filter(
        (d) => d.time >= probeStart && d.time <= probeEnd,
      )
      if (days.length === 0) {
        cursor = probeStart
        floor = probeStart
        continue
      }
      const newest = days.reduce((t, d) => Math.max(t, d.time), -Infinity)
      cursor = newest + DAY
      floor = newest
    }
    return []
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
    return openCoinbaseCandleStream({
      WebSocketImpl: WS,
      url: this.wsBaseUrl,
      staleTimeoutMs: this.staleTimeoutMs,
      reconcileDelayMs: this.reconcileDelayMs,
      now: this.now,
      bucketStart: (t) => bucketStart(t, interval),
      resolveProduct: (signal) => this.productId(sym, signal),
      // No stepping back: a read-back of a bucket must not wander into older history.
      fetchCandles: (limit, endTime, signal) =>
        this.candles(sym, interval, limit, endTime, signal, false),
      onCandle,
      onStatus: opts?.onStatus,
      // Per-trade bars are 10-50 Hz on busy pairs: coalesce the forming updates.
      formingFlushMs: DEFAULT_FORMING_FLUSH_MS,
    })
  }

  /**
   * 24h stats for the given symbols. Up to 4 known symbols: one throttled /products/{id}/stats
   * each (404 -> omitted); more: one GET /products/stats. Unknown/invalid symbols are omitted;
   * order follows the input. `time` is when the request was made (stats carry no time).
   */
  async getTickers(symbols: readonly string[], opts?: { signal?: AbortSignal }): Promise<Ticker[]> {
    const wanted: string[] = []
    for (const s of symbols) {
      const n = s.trim().toUpperCase()
      if (SYMBOL_RE.test(n) && !wanted.includes(n)) wanted.push(n)
    }
    if (wanted.length === 0) return []
    const signal = opts?.signal
    const cat = await this.catalog(signal)
    const known = wanted.filter((s) => cat.toProduct.has(s))
    if (known.length === 0) return []
    const time = Math.floor(this.now() / 1000)
    const out: Ticker[] = []

    if (known.length <= PER_PRODUCT_TICKERS_MAX) {
      for (const sym of known) {
        const pid = cat.toProduct.get(sym) as string
        let body: unknown
        try {
          body = await this.requestJson(
            `/products/${encodeURIComponent(pid)}/stats`,
            null,
            `${sym} 24h stats`,
            signal,
            true,
          )
        } catch (err) {
          if (err instanceof MarketDataError && err.kind === 'bad-symbol') continue
          throw err
        }
        const t = parseStats(body, sym, time)
        if (t !== null) out.push(t)
      }
      return out
    }

    const body = await this.requestJson('/products/stats', null, '24h stats', signal)
    if (!isRecord(body)) throw new MarketDataError('parse', 'Expected a stats object')
    for (const sym of known) {
      const entry = body[cat.toProduct.get(sym) as string]
      if (!isRecord(entry)) continue
      const t = parseStats(entry.stats_24hour, sym, time)
      if (t !== null) out.push(t)
    }
    return out
  }

  /**
   * `opts.symbols` is accepted but not used: the feed subscribes every tradable product and
   * coalesces per 250 ms, and the watchlist controller keeps only its watched symbols.
   */
  subscribeTickers(
    onTickers: (tickers: Ticker[]) => void,
    opts?: TickerSubscribeOptions,
  ): () => void {
    const WS = this.resolveWebSocket('subscribeTickers')
    return openCoinbaseTickerStream({
      WebSocketImpl: WS,
      url: this.wsBaseUrl,
      staleTimeoutMs: this.staleTimeoutMs,
      resolveProducts: async (signal) => {
        const cat = await this.catalog(signal)
        const productIds = cat.symbols
          .filter((s) => s.status === 'TRADING')
          .map((s) => cat.toProduct.get(s.symbol) as string)
        return { productIds, toSymbol: cat.toSymbol }
      },
      onTickers,
      onStatus: opts?.onStatus,
    })
  }
}
