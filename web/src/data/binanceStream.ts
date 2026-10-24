import type { Candle } from '../types'

export const BINANCE_US_WS = 'wss://stream.binance.us:9443'

const BACKOFF_BASE_MS = 1_000
const BACKOFF_CAP_MS = 30_000

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

/**
 * Parse one Binance kline WebSocket message (string or already-parsed object).
 * Accepts the raw stream shape `{ e: 'kline', s, k: {...} }` and the combined
 * stream wrapper `{ stream, data }`. Returns null for anything that is not a
 * well-formed kline, or (when `expected` is given) a kline for another
 * symbol/interval. Never throws.
 */
export function parseKlineMessage(
  data: unknown,
  expected?: { symbol: string; interval: string },
): { candle: Candle; closed: boolean } | null {
  let msg: unknown = data
  if (typeof msg === 'string') {
    try {
      msg = JSON.parse(msg)
    } catch {
      return null
    }
  }
  if (!isRecord(msg)) return null
  if (isRecord(msg.data) && typeof msg.stream === 'string') msg = msg.data
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

/**
 * Reconnect delay in ms for the given zero-based attempt: exponential from 1 s,
 * capped at 30 s, with "equal jitter" so the result lies in [base/2, base].
 * `rand` must return values in [0, 1).
 */
export function backoffDelay(attempt: number, rand: () => number = Math.random): number {
  const a = Number.isFinite(attempt) && attempt > 0 ? Math.floor(attempt) : 0
  // 2^5 * 1 s already exceeds the cap; avoid overflow for huge attempts.
  const base = Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** Math.min(a, 10))
  const r = Math.min(1, Math.max(0, rand()))
  return Math.round(base / 2 + (base / 2) * r)
}
