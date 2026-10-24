// Binance public REST client (data-api.binance.vision by default): request-weight limiter,
// in-flight cap, GET coalescing, retry, and error mapping to ApiError. Public market-data endpoints only; never sends API keys.
import type { Db } from '../db/index.ts'
import { ApiError, type Candle, type Interval, type Ticker } from '../types.ts'
import {
  parseErrorBody,
  parseExchangeInfo,
  parseKlines,
  parseRetryAfter,
  parseTickers24hr,
  type ExchangeInfo,
} from './parse.ts'

/**
 * REQUEST_WEIGHT limit per 1 minute. Confirmed live from
 * `GET https://data-api.binance.vision/api/v3/exchangeInfo` → rateLimits (2026-10-24): 6000
 * (api.binance.us reported the same). Resynced from exchangeInfo whenever the symbol list is
 * refreshed.
 */
export const DEFAULT_WEIGHT_LIMIT_1M = 6000
/** Stay under this share of the limit so other users of the IP (e.g. the browser) have room. */
export const WEIGHT_SAFETY = 0.8
/**
 * Endpoint weights, measured live on data-api.binance.vision (2026-10-24) and earlier on
 * api.binance.us from `X-MBX-USED-WEIGHT-1M` deltas: klines = 2 for any limit 1..1000,
 * exchangeInfo = 20, ticker/24hr with `symbol` or a small `symbols` list = 2. Larger `symbols`
 * lists follow Binance's table (21-100 → 40, 101+ → 80).
 */
export const WEIGHT = { klines: 2, exchangeInfo: 20 } as const
export function tickerWeight(count: number): number {
  if (count <= 20) return 2
  if (count <= 100) return 40
  return 80
}

/**
 * exchangeInfo query. `showPermissionSets=false` drops the per-symbol permission matrix
 * (17.8 MB → 6.8 MB on Binance global, 2026-10-24) without changing any field we read; both
 * Binance hosts accept it.
 */
export const EXCHANGE_INFO_PARAMS: Readonly<Record<string, string>> = {
  showPermissionSets: 'false',
}

export const MAX_IN_FLIGHT = 4
export const TIMEOUT_MS = 10_000
export const MAX_RETRIES = 2
/** Wait for the weight window to reset only when it is this close; else fail fast. */
export const MAX_WAIT_MS = 2_000
/** Requests allowed to wait for an in-flight slot; more fail fast with rate-limited. */
export const MAX_QUEUE = 100
/** Longest a request waits for an in-flight slot before failing with rate-limited. */
export const QUEUE_TIMEOUT_MS = 15_000
const WINDOW_MS = 60_000
const INVALID_SYMBOL_CODE = -1121
const DEFAULT_429_RETRY_SEC = 60
const DEFAULT_418_RETRY_SEC = 120

export interface RestClientOptions {
  fetch: typeof fetch
  /** Millisecond clock. */
  now: () => number
  restBase: string
  /** Defaults to setTimeout; tests inject one that advances their clock. */
  sleep?: (ms: number) => Promise<void>
  /** [0, 1); used for retry jitter. */
  random?: () => number
  timeoutMs?: number
  maxInFlight?: number
  weightLimit1m?: number
  /** Defaults to MAX_QUEUE. */
  maxQueue?: number
  /** Defaults to QUEUE_TIMEOUT_MS (real timer). */
  queueTimeoutMs?: number
}

export interface KlinesQuery {
  symbol: string
  interval: Interval
  /** UNIX seconds, inclusive bar open time. */
  startTime?: number
  /** Milliseconds, sent as is. */
  endTimeMs?: number
  limit: number
}

export interface LimiterState {
  usedWeight: number
  windowStartMs: number
  weightLimit1m: number
  inFlight: number
  queued: number
  pausedUntilMs: number
  bannedUntilMs: number
}

export interface RestClient {
  /** Raw GET; `weight` is the endpoint's request weight. Identical concurrent GETs share one call. */
  getJson(path: string, params: Readonly<Record<string, string>>, weight: number): Promise<unknown>
  klines(q: KlinesQuery): Promise<Candle[]>
  exchangeInfo(): Promise<ExchangeInfo>
  /** GET /api/v3/ticker/24hr?symbols=[...]; throws bad-symbol if any symbol is unknown. */
  tickers24hr(symbols: readonly string[]): Promise<Ticker[]>
  setWeightLimit(limit1m: number): void
  state(): LimiterState
}

/** Transient failure that may be retried (network, timeout, 5xx). */
class Transient extends Error {
  readonly finalKind: 'network' | 'http'
  constructor(finalKind: 'network' | 'http', message: string) {
    super(message)
    this.finalKind = finalKind
  }
}

const realSleep = (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)))

export function createRestClient(opts: RestClientOptions): RestClient {
  const base = opts.restBase.replace(/\/+$/, '')
  const now = opts.now
  const sleep = opts.sleep ?? realSleep
  const random = opts.random ?? Math.random
  const timeoutMs = opts.timeoutMs ?? TIMEOUT_MS
  const maxInFlight = opts.maxInFlight ?? MAX_IN_FLIGHT
  const maxQueue = opts.maxQueue ?? MAX_QUEUE
  const queueTimeoutMs = opts.queueTimeoutMs ?? QUEUE_TIMEOUT_MS

  let weightLimit = opts.weightLimit1m ?? DEFAULT_WEIGHT_LIMIT_1M
  let windowStartMs = 0
  let usedWeight = 0
  let inFlight = 0
  /** Weight reserved by requests sent but not yet answered. */
  let inFlightWeight = 0
  let pausedUntilMs = 0
  let bannedUntilMs = 0
  const waiters: (() => void)[] = []
  const pending = new Map<string, Promise<unknown>>()

  const budget = (): number => Math.floor(weightLimit * WEIGHT_SAFETY)

  function rollWindow(): void {
    const start = Math.floor(now() / WINDOW_MS) * WINDOW_MS
    if (start !== windowStartMs) {
      windowStartMs = start
      usedWeight = 0
    }
  }

  function secondsUntil(ms: number): number {
    return Math.max(1, Math.ceil((ms - now()) / 1000))
  }

  function checkGates(): void {
    const t = now()
    if (t < bannedUntilMs) {
      throw new ApiError(
        'banned',
        'Upstream IP ban in effect (HTTP 418)',
        503,
        secondsUntil(bannedUntilMs),
      )
    }
    if (t < pausedUntilMs) {
      throw new ApiError(
        'rate-limited',
        'Upstream rate limit in effect (HTTP 429)',
        429,
        secondsUntil(pausedUntilMs),
      )
    }
  }

  async function reserveWeight(weight: number): Promise<void> {
    // At most a few short waits; a clock that does not advance must not spin forever.
    for (let i = 0; i < 3; i++) {
      rollWindow()
      if (usedWeight + weight <= budget()) {
        usedWeight += weight
        return
      }
      const waitMs = windowStartMs + WINDOW_MS - now()
      if (waitMs > MAX_WAIT_MS) break
      await sleep(waitMs)
    }
    const retryAt = windowStartMs + WINDOW_MS
    throw new ApiError(
      'rate-limited',
      'Local upstream request-weight budget exhausted',
      429,
      secondsUntil(retryAt),
    )
  }

  async function acquireSlot(): Promise<void> {
    if (inFlight < maxInFlight) {
      inFlight++
      return
    }
    if (waiters.length >= maxQueue) {
      throw new ApiError('rate-limited', 'Too many upstream requests queued', 429, 1)
    }
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => {
        const i = waiters.indexOf(wake)
        if (i >= 0) waiters.splice(i, 1)
        reject(
          new ApiError(
            'rate-limited',
            `Timed out after ${queueTimeoutMs} ms waiting for an upstream slot`,
            429,
            1,
          ),
        )
      }, queueTimeoutMs)
      const wake = (): void => {
        clearTimeout(timer)
        resolve()
      }
      waiters.push(wake)
    })
    // The releaser handed its slot over; inFlight is unchanged.
  }

  function releaseSlot(): void {
    const next = waiters.shift()
    if (next) next()
    else inFlight--
  }

  async function attempt(url: string, what: string, weight: number): Promise<unknown> {
    await acquireSlot()
    try {
      checkGates()
      await reserveWeight(weight)
      // reserveWeight rolled the window, so this is the window the request is sent in.
      const sentInWindow = windowStartMs
      inFlightWeight += weight
      let res: Response
      let text: string
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(), timeoutMs)
      try {
        try {
          res = await opts.fetch(url, { signal: controller.signal })
          text = await res.text()
        } catch (err) {
          if (controller.signal.aborted) {
            throw new Transient('network', `Timed out after ${timeoutMs} ms fetching ${what}`)
          }
          const detail = err instanceof Error ? err.message : String(err)
          throw new Transient('network', `Network error fetching ${what}: ${detail}`)
        }
      } finally {
        clearTimeout(timer)
        inFlightWeight -= weight
      }

      const header = res.headers.get('X-MBX-USED-WEIGHT-1M')
      const used = header === null ? NaN : Number(header)
      if (Number.isFinite(used) && used >= 0) {
        rollWindow()
        // A response to a request sent in the previous minute reports that minute's weight; it
        // must not poison the new window. Within a window take the max, so an out-of-order
        // (older) response cannot lower the count. Add what is still in flight on our side.
        if (windowStartMs === sentInWindow) {
          usedWeight = Math.max(usedWeight, used + inFlightWeight)
        }
      }

      if (!res.ok) {
        const retryAfter = parseRetryAfter(res.headers.get('Retry-After'))
        if (res.status === 429) {
          const sec = retryAfter ?? DEFAULT_429_RETRY_SEC
          pausedUntilMs = Math.max(pausedUntilMs, now() + sec * 1000)
          throw new ApiError('rate-limited', `Upstream rate limited fetching ${what}`, 429, sec)
        }
        if (res.status === 418) {
          const sec = retryAfter ?? DEFAULT_418_RETRY_SEC
          bannedUntilMs = Math.max(bannedUntilMs, now() + sec * 1000)
          throw new ApiError('banned', 'Upstream IP ban (HTTP 418)', 503, sec)
        }
        if (res.status === 451) {
          throw new ApiError('region-blocked', 'Upstream unavailable in this region', 503)
        }
        const { code, msg } = parseErrorBody(text)
        if (code === INVALID_SYMBOL_CODE) {
          throw new ApiError('bad-symbol', `Unknown symbol in request for ${what}`, 400)
        }
        const suffix = msg ? `: ${msg}` : ''
        if (res.status >= 500) {
          throw new Transient('http', `Upstream HTTP ${res.status} fetching ${what}${suffix}`)
        }
        throw new ApiError('http', `Upstream HTTP ${res.status} fetching ${what}${suffix}`, 502)
      }

      try {
        return JSON.parse(text) as unknown
      } catch {
        throw new ApiError('parse', `Invalid JSON in ${what} response`, 502)
      }
    } finally {
      releaseSlot()
    }
  }

  async function run(url: string, what: string, weight: number): Promise<unknown> {
    for (let i = 0; ; i++) {
      try {
        return await attempt(url, what, weight)
      } catch (err) {
        if (!(err instanceof Transient)) throw err
        if (i >= MAX_RETRIES) throw new ApiError(err.finalKind, err.message, 502)
        // Jittered exponential backoff: 250 ms, 500 ms, ... times [0.5, 1.5).
        await sleep(250 * 2 ** i * (0.5 + random()))
      }
    }
  }

  function getJson(
    path: string,
    params: Readonly<Record<string, string>>,
    weight: number,
  ): Promise<unknown> {
    const qs = new URLSearchParams(params).toString()
    const url = `${base}${path}${qs ? `?${qs}` : ''}`
    const existing = pending.get(url)
    if (existing) return existing
    const p = run(url, path, weight).finally(() => {
      pending.delete(url)
    })
    pending.set(url, p)
    return p
  }

  return {
    getJson,
    async klines(q) {
      const params: Record<string, string> = {
        symbol: q.symbol,
        interval: q.interval,
        limit: String(q.limit),
      }
      if (q.startTime !== undefined) params.startTime = String(q.startTime * 1000)
      if (q.endTimeMs !== undefined) params.endTime = String(q.endTimeMs)
      return parseKlines(await getJson('/api/v3/klines', params, WEIGHT.klines))
    },
    async exchangeInfo() {
      const info = parseExchangeInfo(
        await getJson('/api/v3/exchangeInfo', EXCHANGE_INFO_PARAMS, WEIGHT.exchangeInfo),
      )
      if (info.weightLimit1m !== undefined) weightLimit = info.weightLimit1m
      return info
    },
    async tickers24hr(symbols) {
      if (symbols.length === 0) return []
      const body = await getJson(
        '/api/v3/ticker/24hr',
        { symbols: JSON.stringify(symbols) },
        tickerWeight(symbols.length),
      )
      return parseTickers24hr(body)
    },
    setWeightLimit(limit1m) {
      if (Number.isFinite(limit1m) && limit1m > 0) weightLimit = limit1m
    },
    state() {
      rollWindow()
      return {
        usedWeight,
        windowStartMs,
        weightLimit1m: weightLimit,
        inFlight,
        queued: waiters.length,
        pausedUntilMs,
        bannedUntilMs,
      }
    },
  }
}

interface SharedEntry {
  fetch: typeof fetch
  now: () => number
  restBase: string
  client: RestClient
}

const shared = new WeakMap<Db, SharedEntry>()

/**
 * The one client (and so one limiter) per app. Keyed by the app's `db`; candles and symbols
 * routes both call this with the same RouteOptions and get the same instance.
 */
export function restClientFor(opts: {
  db: Db
  fetch: typeof fetch
  now: () => number
  restBase: string
}): RestClient {
  const hit = shared.get(opts.db)
  if (hit && hit.fetch === opts.fetch && hit.now === opts.now && hit.restBase === opts.restBase) {
    return hit.client
  }
  const client = createRestClient({ fetch: opts.fetch, now: opts.now, restBase: opts.restBase })
  shared.set(opts.db, { fetch: opts.fetch, now: opts.now, restBase: opts.restBase, client })
  return client
}
