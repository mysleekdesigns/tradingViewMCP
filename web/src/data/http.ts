import { isRecord } from './guards'
import { MarketDataError } from './provider'

/*
 * The request helpers every HTTP client in web/src/data shares (Phase 19 DRY). Two families
 * keep their own deadline mechanics, because callers and tests depend on them:
 *
 * - Market data (`requestMarketJson`: BinanceProvider, CoinbaseProvider, ServerProvider): the
 *   deadline is `AbortSignal.timeout`, combined with the caller's signal by `AbortSignal.any`;
 *   failures are `MarketDataError`s; a caller abort wins over the timeout.
 * - Local server and lab (`fetchWithin`: persistence, drawings/alerts, lab, research): the
 *   deadline is a `setTimeout` on our own AbortController that the caller's signal also aborts;
 *   each client maps failures to its own error class; the timeout wins over a caller abort.
 *
 * Nothing here places orders or sends credentials: every request is a plain fetch of a public
 * market-data endpoint or of the app's own local services.
 */

/** Anything fetch-shaped (tests pass doubles; the clients default to the global fetch). */
export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>

/** The global fetch, looked up at call time; rejects when the environment has none. */
export const globalFetch: FetchLike = (input, init) => {
  if (typeof globalThis.fetch !== 'function') {
    return Promise.reject(new Error('fetch is not available'))
  }
  return globalThis.fetch(input, init)
}

// ---------------------------------------------------------------------------------------------
// Shared pieces
// ---------------------------------------------------------------------------------------------

/** `Retry-After` in delta-seconds; undefined when absent, blank, negative or not a number. */
export function parseRetryAfter(res: Response): number | undefined {
  const raw = res.headers.get('Retry-After')
  if (raw === null || raw.trim() === '') return undefined
  const n = Number(raw)
  return Number.isFinite(n) && n >= 0 ? n : undefined
}

/** A candle `limit` as sent upstream: an integer in [1, max]; non-finite -> `fallback`. */
export function clampLimit(limit: number, max = 1000, fallback = 500): number {
  if (!Number.isFinite(limit)) return fallback
  return Math.min(max, Math.max(1, Math.floor(limit)))
}

/** MarketDataError 'aborted': "Request for <what> was cancelled". */
export function abortedError(what: string): MarketDataError {
  return new MarketDataError('aborted', `Request for ${what} was cancelled`)
}

/** Reject with kind 'aborted' when `signal` aborts before `p` settles. */
export function withAbort<T>(
  p: Promise<T>,
  signal: AbortSignal | undefined,
  what: string,
): Promise<T> {
  if (!signal) return p
  if (signal.aborted) return Promise.reject(abortedError(what))
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(abortedError(what))
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

/**
 * One shared in-flight load per instance (symbol lists, catalogs, the server venue): the first
 * caller starts it, later callers share it, a failure is forgotten so the next call retries.
 * The shared load never sees a caller's signal; `join` lets each caller's signal reject only
 * that caller.
 */
export class SharedLoad<T> {
  private pending: Promise<T> | null = null
  private readonly load: () => Promise<T>

  constructor(load: () => Promise<T>) {
    this.load = load
  }

  /** The shared promise, started on first use (and again after a failure). */
  get(): Promise<T> {
    if (this.pending === null) {
      const p = this.load()
      this.pending = p
      p.catch(() => {
        if (this.pending === p) this.pending = null
      })
    }
    return this.pending
  }

  /** `get()` for one caller: an aborted signal rejects 'aborted' (and never starts the load). */
  join(signal: AbortSignal | undefined, what: string): Promise<T> {
    if (signal?.aborted) return Promise.reject(abortedError(what))
    return withAbort(this.get(), signal, what)
  }
}

/** The parsed JSON body, or undefined when it is not JSON (or cannot be read). */
export async function readJsonOrUndefined(res: Response): Promise<unknown> {
  try {
    return (await res.json()) as unknown
  } catch {
    return undefined
  }
}

// ---------------------------------------------------------------------------------------------
// Market data
// ---------------------------------------------------------------------------------------------

export interface MarketRequest {
  fetchFn: typeof fetch
  url: string
  /** Names the resource in messages ("BTCUSDT candles"). */
  what: string
  timeoutMs: number
  signal?: AbortSignal
  /** Sent as `cache` when set; otherwise the init is exactly `{ signal }`. */
  cache?: RequestCache
  /**
   * The caller's abort wins at every stage (ServerProvider): an already-aborted signal rejects
   * before any request, and a body that fails to parse after the caller aborted rejects
   * 'aborted' instead of 'parse'.
   */
  callerAbortWins?: boolean
  /** The error a non-2xx response stands for. */
  toError: (res: Response) => Promise<MarketDataError>
}

/**
 * GET `url` and return the parsed JSON body. Caller abort -> 'aborted'; our timeout ->
 * 'network' ("Timed out after N ms fetching <what>"); other transport failure -> 'network';
 * non-2xx -> `toError(res)`; unparseable 2xx body -> 'parse'.
 */
export async function requestMarketJson(req: MarketRequest): Promise<unknown> {
  const { what, signal: callerSignal } = req
  if (req.callerAbortWins === true && callerSignal?.aborted) throw abortedError(what)
  const timeout = AbortSignal.timeout(req.timeoutMs)
  const signal = callerSignal ? AbortSignal.any([callerSignal, timeout]) : timeout

  let res: Response
  try {
    res = await req.fetchFn(
      req.url,
      req.cache === undefined ? { signal } : { signal, cache: req.cache },
    )
  } catch (err) {
    if (callerSignal?.aborted) throw abortedError(what)
    if (timeout.aborted) {
      throw new MarketDataError('network', `Timed out after ${req.timeoutMs} ms fetching ${what}`)
    }
    const detail = err instanceof Error ? err.message : String(err)
    throw new MarketDataError('network', `Network error fetching ${what}: ${detail}`)
  }

  if (!res.ok) throw await req.toError(res)

  try {
    return (await res.json()) as unknown
  } catch {
    if (req.callerAbortWins === true && callerSignal?.aborted) throw abortedError(what)
    throw new MarketDataError('parse', `Invalid JSON in ${what} response`)
  }
}

/** How an exchange's error answers differ (Binance, Coinbase). */
export interface ExchangeErrorRules {
  /** Text appended to the generic HTTP message ("HTTP 500 fetching x: <detail>"), from the body. */
  detail: (body: unknown) => string | undefined
  /** The bad-symbol message for this answer, or null when it is not one. */
  badSymbol: (status: number, body: unknown) => string | null
  /** Runs on a 429 before the error is returned (Coinbase pauses its throttle). */
  onRateLimited?: (retryAfter: number | undefined) => void
}

/**
 * A public exchange's non-2xx answer: 429 -> 'rate-limited' (+Retry-After), 418 -> 'banned'
 * (+Retry-After), 451 -> 'region-blocked'; otherwise the JSON body is read: bad-symbol per
 * `rules`, else 'http' ("HTTP <status> fetching <what>[: <detail>]").
 */
export async function exchangeError(
  res: Response,
  what: string,
  rules: ExchangeErrorRules,
): Promise<MarketDataError> {
  const retryAfter = parseRetryAfter(res)
  if (res.status === 429) {
    rules.onRateLimited?.(retryAfter)
    return new MarketDataError('rate-limited', `Rate limited fetching ${what}`, 429, retryAfter)
  }
  if (res.status === 418) {
    return new MarketDataError('banned', 'IP banned by provider', 418, retryAfter)
  }
  if (res.status === 451) {
    return new MarketDataError('region-blocked', 'Provider unavailable in this region', 451)
  }
  const body = await readJsonOrUndefined(res)
  const badSymbol = rules.badSymbol(res.status, body)
  if (badSymbol !== null) return new MarketDataError('bad-symbol', badSymbol, res.status)
  const detail = rules.detail(body)
  const suffix = detail ? `: ${detail}` : ''
  return new MarketDataError('http', `HTTP ${res.status} fetching ${what}${suffix}`, res.status)
}

/** `body[key]` when the body is an object and the value is a string, else undefined. */
export function bodyString(body: unknown, key: string): string | undefined {
  if (!isRecord(body)) return undefined
  const v = body[key]
  return typeof v === 'string' ? v : undefined
}

// ---------------------------------------------------------------------------------------------
// Local server and lab
// ---------------------------------------------------------------------------------------------

/** The errors one client raises for the transport failures `fetchWithin` detects. */
export interface FetchFailures {
  /** The caller's signal aborted (before the request, or while fetch was pending). */
  aborted: () => Error
  /** Our timer fired before fetch settled. */
  timeout: () => Error
  /** fetch rejected for any other reason. */
  network: (err: unknown) => Error
}

export interface TimedRequest {
  fetchImpl: FetchLike
  url: string
  /** Method, headers, body, keepalive; the signal is added. */
  init: RequestInit
  timeoutMs: number
  signal?: AbortSignal
  failures: FetchFailures
}

/** What `read` may check while the deadline is still running. */
export interface Deadline {
  readonly timedOut: boolean
}

/**
 * fetch under a `setTimeout` deadline that the caller's signal can also cut short, then
 * `read(res)` (any status) before the timer and listener are released. An already-aborted
 * signal rejects with `failures.aborted()` before any request; a fetch rejection maps to
 * `timeout` (timer fired), else `aborted` (caller), else `network(err)`.
 */
export async function fetchWithin<T>(
  req: TimedRequest,
  read: (res: Response, deadline: Deadline) => Promise<T>,
): Promise<T> {
  const callerSignal = req.signal
  if (callerSignal?.aborted) throw req.failures.aborted()
  const controller = new AbortController()
  let timedOut = false
  const timer = setTimeout(() => {
    timedOut = true
    controller.abort()
  }, req.timeoutMs)
  const onCallerAbort = (): void => controller.abort()
  callerSignal?.addEventListener('abort', onCallerAbort, { once: true })
  const deadline: Deadline = {
    get timedOut() {
      return timedOut
    },
  }
  try {
    let res: Response
    try {
      res = await req.fetchImpl(req.url, { ...req.init, signal: controller.signal })
    } catch (err) {
      if (timedOut) throw req.failures.timeout()
      if (callerSignal?.aborted) throw req.failures.aborted()
      throw req.failures.network(err)
    }
    return await read(res, deadline)
  } finally {
    clearTimeout(timer)
    callerSignal?.removeEventListener('abort', onCallerAbort)
  }
}

/** `{ method }`, plus a JSON content type and body when `body` is given. */
export function jsonInit(method: string, body?: unknown): RequestInit {
  const init: RequestInit = { method }
  if (body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' }
    init.body = JSON.stringify(body)
  }
  return init
}
