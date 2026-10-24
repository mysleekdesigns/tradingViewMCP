import type { Candle, Interval, SymbolInfo } from '../types'
import { MarketDataError, type MarketDataProvider } from './provider'

export const BINANCE_US_REST = 'https://api.binance.us'

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

export class BinanceProvider implements MarketDataProvider {
  private readonly baseUrl: string
  private readonly fetchFn: typeof fetch
  private readonly timeoutMs: number

  constructor(opts?: { baseUrl?: string; fetchFn?: typeof fetch; timeoutMs?: number }) {
    this.baseUrl = (opts?.baseUrl ?? BINANCE_US_REST).replace(/\/+$/, '')
    // Bind so the global fetch is not called with a foreign `this`.
    this.fetchFn = opts?.fetchFn ?? ((input, init) => globalThis.fetch(input, init))
    this.timeoutMs = opts?.timeoutMs ?? DEFAULT_TIMEOUT_MS
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
    _symbol: string,
    _interval: Interval,
    _onCandle: (c: Candle, closed: boolean) => void,
  ): () => void {
    void _symbol
    void _interval
    void _onCandle
    throw new Error('subscribeCandles: not implemented until Phase 2')
  }

  getSymbols(): Promise<SymbolInfo[]> {
    return Promise.reject(new Error('getSymbols: not implemented until Phase 3'))
  }
}
