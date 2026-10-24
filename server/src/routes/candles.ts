import type { FastifyInstance } from 'fastify'
import type { RouteOptions } from '../app.ts'
import { createCandleCache } from '../cache/candleCache.ts'
import { restClientFor } from '../upstream/rest.ts'
import { ApiError, isInterval } from '../types.ts'

export const SYMBOL_RE = /^[A-Z0-9]{2,30}$/
export const DEFAULT_LIMIT = 500
export const MAX_LIMIT = 1000

/** Trim + uppercase; null unless it matches SYMBOL_RE. */
export function normalizeSymbol(raw: unknown): string | null {
  if (typeof raw !== 'string') return null
  const s = raw.trim().toUpperCase()
  return SYMBOL_RE.test(s) ? s : null
}

function badQuery(message: string): ApiError {
  return new ApiError('http', message, 400)
}

function parseNonNegInt(raw: unknown, name: string): number | undefined {
  if (raw === undefined) return undefined
  if (typeof raw !== 'string' || !/^\d{1,15}$/.test(raw.trim())) {
    throw badQuery(`${name} must be a non-negative integer`)
  }
  return Number(raw.trim())
}

// GET /api/candles?symbol=BTCUSDT&interval=1h&limit=500[&endTime=<sec>]. See server/README.md.
export default async function candlesRoutes(
  app: FastifyInstance,
  opts: RouteOptions,
): Promise<void> {
  const cache = createCandleCache({ db: opts.db, client: restClientFor(opts), now: opts.now })

  app.get('/api/candles', async (request) => {
    const q = (request.query ?? {}) as Record<string, unknown>
    const symbol = normalizeSymbol(q.symbol)
    if (symbol === null) throw new ApiError('bad-symbol', 'Invalid symbol', 400)
    if (!isInterval(q.interval)) throw badQuery('interval must be one of 1m 5m 15m 1h 4h 1d 1w')
    const limit = parseNonNegInt(q.limit, 'limit') ?? DEFAULT_LIMIT
    if (limit < 1 || limit > MAX_LIMIT) throw badQuery(`limit must be 1..${MAX_LIMIT}`)
    const endTime = parseNonNegInt(q.endTime, 'endTime')
    return cache.getCandles(symbol, q.interval, limit, endTime)
  })
}
