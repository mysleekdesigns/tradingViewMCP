import type { SymbolInfo, Ticker } from '../types'

/**
 * Parsers for Binance 24h tickers (REST `/api/v3/ticker/24hr` and the
 * `!miniTicker@arr` stream) and `exchangeInfo` symbols. All are strict and never
 * throw: a malformed entry yields null and the caller skips it.
 */

export const MINI_TICKER_STREAM = '!miniTicker@arr'

/** Binance symbol syntax; anything else is rejected locally (it would fail the whole request). */
const SYMBOL_RE = /^[A-Z0-9\-_.]{1,20}$/

/** Strict numeric field: finite number or non-empty numeric string, else null. */
function finite(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value !== 'string' || value.trim() === '') return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return v !== null && typeof v === 'object' && !Array.isArray(v)
}

function nonEmptyString(v: unknown): v is string {
  return typeof v === 'string' && v.trim() !== ''
}

/** Uppercase/trim a user-supplied symbol; null if it is not valid Binance symbol syntax. */
export function normalizeSymbol(symbol: string): string | null {
  const s = symbol.trim().toUpperCase()
  return SYMBOL_RE.test(s) ? s : null
}

/** (last - open) / open * 100; 0 when open is 0. */
export function changePct(last: number, open: number): number {
  return open === 0 ? 0 : ((last - open) / open) * 100
}

function makeTicker(
  symbol: unknown,
  fields: { last: unknown; open: unknown; high: unknown; low: unknown; v: unknown; q: unknown },
  timeMs: unknown,
): Ticker | null {
  if (!nonEmptyString(symbol)) return null
  const last = finite(fields.last)
  const open = finite(fields.open)
  const high = finite(fields.high)
  const low = finite(fields.low)
  const volume = finite(fields.v)
  const quoteVolume = finite(fields.q)
  const ms = finite(timeMs)
  if (
    last === null ||
    open === null ||
    high === null ||
    low === null ||
    volume === null ||
    quoteVolume === null ||
    ms === null
  ) {
    return null
  }
  return {
    symbol,
    last,
    open,
    high,
    low,
    volume,
    quoteVolume,
    changePct: changePct(last, open),
    time: Math.floor(ms / 1000),
  }
}

/**
 * One REST 24hr ticker object (`symbol, lastPrice, openPrice, highPrice, lowPrice,
 * volume, quoteVolume, closeTime`, prices as strings). `time` = floor(closeTime / 1000).
 */
export function parseTicker24hr(raw: unknown): Ticker | null {
  if (!isRecord(raw)) return null
  return makeTicker(
    raw.symbol,
    {
      last: raw.lastPrice,
      open: raw.openPrice,
      high: raw.highPrice,
      low: raw.lowPrice,
      v: raw.volume,
      q: raw.quoteVolume,
    },
    raw.closeTime,
  )
}

/** One `24hrMiniTicker` element `{ e, E, s, c, o, h, l, v, q }`. `time` = floor(E / 1000). */
export function parseMiniTicker(raw: unknown): Ticker | null {
  if (!isRecord(raw) || raw.e !== '24hrMiniTicker') return null
  return makeTicker(
    raw.s,
    { last: raw.c, open: raw.o, high: raw.h, low: raw.l, v: raw.v, q: raw.q },
    raw.E,
  )
}

/**
 * A `!miniTicker@arr` message (JSON string or parsed value; the combined-stream
 * `{ stream, data }` wrapper is unwrapped). Malformed elements are skipped; a message
 * that is not an array yields [].
 */
export function parseMiniTickerMessage(data: unknown): Ticker[] {
  let msg: unknown = data
  if (typeof msg === 'string') {
    try {
      msg = JSON.parse(msg)
    } catch {
      return []
    }
  }
  if (isRecord(msg) && typeof msg.stream === 'string' && Array.isArray(msg.data)) msg = msg.data
  if (!Array.isArray(msg)) return []
  const out: Ticker[] = []
  for (const el of msg) {
    const t = parseMiniTicker(el)
    if (t !== null) out.push(t)
  }
  return out
}

/**
 * One `exchangeInfo.symbols[]` entry. Requires non-empty `symbol`, `baseAsset`,
 * `quoteAsset`, `status` strings and a `PRICE_FILTER` with a finite `tickSize >= 0`
 * (Binance uses 0 to mean "no tick rule"). Anything else is null (skipped).
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
