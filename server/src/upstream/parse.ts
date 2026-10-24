// Strict parsers for Binance(.US) REST payloads. Ported from web/src/data/binance.ts and
// binanceTicker.ts (not imported: the server keeps its own copy). Shape errors in the top-level
// body throw ApiError('parse', ..., 502); malformed list entries are skipped where noted.
import { msToSec } from '../time.ts'
import { ApiError, type Candle, type SymbolInfo, type Ticker } from '../types.ts'

function parseError(message: string): ApiError {
  return new ApiError('parse', message, 502)
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return v !== null && typeof v === 'object' && !Array.isArray(v)
}

function nonEmptyString(v: unknown): v is string {
  return typeof v === 'string' && v.trim() !== ''
}

/** Finite number or non-empty numeric string, else null. */
export function finite(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value !== 'string' || value.trim() === '') return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

function requireFinite(value: unknown, field: string): number {
  const n = finite(value)
  if (n === null) throw parseError(`kline ${field}: not a finite number`)
  return n
}

/**
 * One kline row `[openTime(ms), open, high, low, close, volume, closeTime, ...]` (prices are
 * strings) → Candle with UNIX-second time. Throws 'parse' on any malformed field.
 */
export function parseKline(row: unknown): Candle {
  if (!Array.isArray(row) || row.length < 6) {
    throw parseError('kline row: expected an array with at least 6 fields')
  }
  return {
    time: msToSec(requireFinite(row[0], 'openTime')),
    open: requireFinite(row[1], 'open'),
    high: requireFinite(row[2], 'high'),
    low: requireFinite(row[3], 'low'),
    close: requireFinite(row[4], 'close'),
    volume: requireFinite(row[5], 'volume'),
  }
}

/** A klines body → candles sorted ascending with unique times (the later row wins). */
export function parseKlines(body: unknown): Candle[] {
  if (!Array.isArray(body)) throw parseError('expected an array of klines')
  const candles = body.map(parseKline)
  candles.sort((a, b) => a.time - b.time)
  const out: Candle[] = []
  for (const c of candles) {
    const last = out[out.length - 1]
    if (last !== undefined && last.time === c.time) out[out.length - 1] = c
    else out.push(c)
  }
  return out
}

/**
 * One `exchangeInfo.symbols[]` entry. Requires non-empty symbol/baseAsset/quoteAsset/status and a
 * PRICE_FILTER with finite tickSize >= 0. Anything else → null (skipped).
 */
export function parseSymbolInfo(raw: unknown): SymbolInfo | null {
  if (!isRecord(raw)) return null
  const { symbol, baseAsset, quoteAsset, status, filters } = raw
  if (
    !nonEmptyString(symbol) ||
    !nonEmptyString(baseAsset) ||
    !nonEmptyString(quoteAsset) ||
    !nonEmptyString(status) ||
    !Array.isArray(filters)
  ) {
    return null
  }
  const priceFilter: unknown = filters.find(
    (f: unknown) => isRecord(f) && f.filterType === 'PRICE_FILTER',
  )
  if (!isRecord(priceFilter)) return null
  const tickSize = finite(priceFilter.tickSize)
  if (tickSize === null || tickSize < 0) return null
  return { symbol, baseAsset, quoteAsset, status, tickSize }
}

export interface ExchangeInfo {
  symbols: SymbolInfo[]
  /** REQUEST_WEIGHT per 1 minute from `rateLimits`, when present. */
  weightLimit1m?: number
}

/** exchangeInfo body → symbols (malformed entries skipped) + the 1-minute weight limit. */
export function parseExchangeInfo(body: unknown): ExchangeInfo {
  if (!isRecord(body)) throw parseError('expected an exchangeInfo object')
  if (!Array.isArray(body.symbols)) throw parseError('exchangeInfo has no symbols array')
  const symbols: SymbolInfo[] = []
  for (const entry of body.symbols) {
    const info = parseSymbolInfo(entry)
    if (info !== null) symbols.push(info)
  }
  const out: ExchangeInfo = { symbols }
  if (Array.isArray(body.rateLimits)) {
    for (const rl of body.rateLimits) {
      if (
        isRecord(rl) &&
        rl.rateLimitType === 'REQUEST_WEIGHT' &&
        rl.interval === 'MINUTE' &&
        rl.intervalNum === 1
      ) {
        const limit = finite(rl.limit)
        if (limit !== null && limit > 0) out.weightLimit1m = limit
      }
    }
  }
  return out
}

/** (last - open) / open * 100; 0 when open is 0. */
export function changePct(last: number, open: number): number {
  return open === 0 ? 0 : ((last - open) / open) * 100
}

/** One REST 24hr ticker object → Ticker (time = closeTime in s), or null when malformed. */
export function parseTicker24hr(raw: unknown): Ticker | null {
  if (!isRecord(raw) || !nonEmptyString(raw.symbol)) return null
  const last = finite(raw.lastPrice)
  const open = finite(raw.openPrice)
  const high = finite(raw.highPrice)
  const low = finite(raw.lowPrice)
  const volume = finite(raw.volume)
  const quoteVolume = finite(raw.quoteVolume)
  const closeMs = finite(raw.closeTime)
  if (
    last === null ||
    open === null ||
    high === null ||
    low === null ||
    volume === null ||
    quoteVolume === null ||
    closeMs === null
  ) {
    return null
  }
  return {
    symbol: raw.symbol,
    last,
    open,
    high,
    low,
    volume,
    quoteVolume,
    changePct: changePct(last, open),
    time: msToSec(closeMs),
  }
}

/** A 24hr body (array for `symbols=`, object for `symbol=`) → tickers; bad entries skipped. */
export function parseTickers24hr(body: unknown): Ticker[] {
  if (!Array.isArray(body) && !isRecord(body)) throw parseError('expected 24hr ticker data')
  const list: unknown[] = Array.isArray(body) ? body : [body]
  const out: Ticker[] = []
  for (const entry of list) {
    const t = parseTicker24hr(entry)
    if (t !== null) out.push(t)
  }
  return out
}

/**
 * `Retry-After` in whole seconds, at least 1 (Binance sends delta-seconds; 0 would mean "retry
 * immediately", which re-trips the limit). Undefined when absent or invalid.
 */
export function parseRetryAfter(raw: string | null): number | undefined {
  if (raw === null || raw.trim() === '') return undefined
  const n = Number(raw)
  return Number.isFinite(n) && n >= 0 ? Math.max(1, Math.ceil(n)) : undefined
}

/** Binance error body `{ code, msg }`; fields missing when the body is not that shape. */
export function parseErrorBody(text: string): { code?: number; msg?: string } {
  try {
    const body: unknown = JSON.parse(text)
    if (isRecord(body)) {
      const out: { code?: number; msg?: string } = {}
      if (typeof body.code === 'number') out.code = body.code
      if (typeof body.msg === 'string') out.msg = body.msg
      return out
    }
  } catch {
    // Not JSON.
  }
  return {}
}
