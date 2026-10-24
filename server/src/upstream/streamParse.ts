// Parsers for Binance(.US) WebSocket market-data pushes: kline and `!miniTicker@arr`.
// Strict and never throw: anything malformed yields null / [] and is ignored by the hub.

import type { Candle, Interval, Ticker } from '../types.ts'

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

const decoder = new TextDecoder()

/**
 * Decode a raw WebSocket `MessageEvent.data` (text frame string, or a binary frame as
 * ArrayBuffer / typed array) and JSON-parse it. Already-parsed values pass through.
 * Returns undefined when it cannot be decoded or parsed.
 */
export function decodeMessage(data: unknown): unknown {
  let text: string
  if (typeof data === 'string') text = data
  else if (data instanceof ArrayBuffer) text = decoder.decode(new Uint8Array(data))
  else if (ArrayBuffer.isView(data)) {
    text = decoder.decode(new Uint8Array(data.buffer, data.byteOffset, data.byteLength))
  } else return data
  try {
    return JSON.parse(text) as unknown
  } catch {
    return undefined
  }
}

/** Unwrap the combined-stream `{ stream, data }` envelope if present. */
function unwrap(msg: unknown): unknown {
  if (isRecord(msg) && typeof msg.stream === 'string' && 'data' in msg) return msg.data
  return msg
}

export interface KlineUpdate {
  candle: Candle
  closed: boolean
}

/**
 * One kline push `{ e: 'kline', E, s, k: { t, s, i, o, h, l, c, v, x, ... } }`.
 * `candle.time` = floor(k.t / 1000) (UNIX seconds); `closed` = (k.x === true).
 * When `expected` is given, a kline for another symbol/interval yields null.
 */
export function parseKlineMessage(
  data: unknown,
  expected?: { symbol: string; interval: Interval | string },
): KlineUpdate | null {
  const msg = unwrap(decodeMessage(data))
  if (!isRecord(msg) || msg.e !== 'kline' || !isRecord(msg.k)) return null
  const k = msg.k

  if (expected) {
    if (typeof k.s !== 'string' || k.s.toUpperCase() !== expected.symbol.trim().toUpperCase()) {
      return null
    }
    if (k.i !== expected.interval) return null
  }

  const openTimeMs = finite(k.t)
  const open = finite(k.o)
  const high = finite(k.h)
  const low = finite(k.l)
  const close = finite(k.c)
  const volume = finite(k.v)
  if (
    openTimeMs === null ||
    open === null ||
    high === null ||
    low === null ||
    close === null ||
    volume === null
  ) {
    return null
  }
  return {
    candle: { time: Math.floor(openTimeMs / 1000), open, high, low, close, volume },
    closed: k.x === true,
  }
}

/** (last - open) / open * 100; 0 when open is 0. */
export function changePct(last: number, open: number): number {
  return open === 0 ? 0 : ((last - open) / open) * 100
}

/** One `24hrMiniTicker` element `{ e, E, s, c, o, h, l, v, q }`. `time` = floor(E / 1000). */
export function parseMiniTicker(raw: unknown): Ticker | null {
  if (!isRecord(raw) || raw.e !== '24hrMiniTicker') return null
  const symbol = raw.s
  if (typeof symbol !== 'string' || symbol.trim() === '') return null
  const last = finite(raw.c)
  const open = finite(raw.o)
  const high = finite(raw.h)
  const low = finite(raw.l)
  const volume = finite(raw.v)
  const quoteVolume = finite(raw.q)
  const ms = finite(raw.E)
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
 * A `!miniTicker@arr` push (an array of `24hrMiniTicker`). Malformed elements are skipped;
 * a message that is not an array yields [].
 */
export function parseMiniTickerMessage(data: unknown): Ticker[] {
  const msg = unwrap(decodeMessage(data))
  if (!Array.isArray(msg)) return []
  const out: Ticker[] = []
  for (const el of msg) {
    const t = parseMiniTicker(el)
    if (t !== null) out.push(t)
  }
  return out
}
