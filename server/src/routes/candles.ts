import { createHash } from 'node:crypto'
import type { FastifyInstance } from 'fastify'
import type { RouteOptions } from '../app.ts'
import { createCandleCache } from '../cache/candleCache.ts'
import { klineKey } from '../upstream/hub.ts'
import { restClientFor } from '../upstream/rest.ts'
import { ApiError, isInterval } from '../types.ts'

/**
 * Closed-history pages (`endTime` set, every bar stored, `venue` = this server's venue): the
 * same URL always gets the same body.
 */
export const CACHE_IMMUTABLE = 'public, max-age=31536000, immutable'
/**
 * Every other page (forming or unsettled bar, or no / another venue in the URL): the browser
 * must revalidate every time.
 */
export const CACHE_REVALIDATE = 'no-cache'

/** Syntax of the `venue` query param (venue ids: lowercase letters, digits, `-`). */
export const VENUE_PARAM_RE = /^[a-z0-9-]{1,32}$/

/**
 * The `venue` query param when it is a single well-formed token, else null (treated as
 * absent: never an error, so clients that do not send it keep working).
 */
export function venueParam(raw: unknown): string | null {
  return typeof raw === 'string' && VENUE_PARAM_RE.test(raw) ? raw : null
}

/**
 * Weak ETag of a response body served by venue `venueId` (weak: the bytes differ per
 * Content-Encoding). The venue is part of the tag, so a revalidation after a venue switch never
 * gets a 304 onto the old venue's bytes, even when the two bodies happen to be identical.
 */
export function bodyEtag(body: string, venueId: string): string {
  return `W/"${venueId}:${createHash('sha1').update(body).digest('base64url')}"`
}

/** RFC 9110 If-None-Match: `*` or a list of entity tags, compared weakly. */
export function ifNoneMatchHits(header: string | string[] | undefined, etag: string): boolean {
  if (header === undefined) return false
  const raw = Array.isArray(header) ? header.join(',') : header
  const opaque = (tag: string): string => tag.trim().replace(/^W\//, '')
  const want = opaque(etag)
  return raw.split(',').some((t) => {
    const tag = t.trim()
    return tag === '*' || (tag !== '' && opaque(tag) === want)
  })
}

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

// GET /api/candles?symbol=BTCUSDT&interval=1h&limit=500[&endTime=<sec>][&venue=<id>].
// See server/README.md.
export default async function candlesRoutes(
  app: FastifyInstance,
  opts: RouteOptions,
): Promise<void> {
  const cache = createCandleCache({
    db: opts.db,
    client: restClientFor(opts),
    now: opts.now,
    // The forming bar comes from the shared upstream stream when the server holds it live.
    live: (symbol, interval) => opts.hub.lastKline(klineKey(symbol, interval)),
    log: app.log,
  })
  // Fixed for the process: a venue change needs a restart (and `ensureVenue` clears the cache).
  const venueId = opts.venue.id

  app.get('/api/candles', async (request, reply) => {
    const q = (request.query ?? {}) as Record<string, unknown>
    const symbol = normalizeSymbol(q.symbol)
    if (symbol === null) throw new ApiError('bad-symbol', 'Invalid symbol', 400)
    if (!isInterval(q.interval)) throw badQuery('interval must be one of 1m 5m 15m 1h 4h 1d 1w')
    const limit = parseNonNegInt(q.limit, 'limit') ?? DEFAULT_LIMIT
    if (limit < 1 || limit > MAX_LIMIT) throw badQuery(`limit must be 1..${MAX_LIMIT}`)
    const endTime = parseNonNegInt(q.endTime, 'endTime')
    const page = await cache.getPage(symbol, q.interval, limit, endTime)
    // Serialised here (same bytes Fastify's default JSON serialiser would send) to hash it.
    const body = JSON.stringify(page.candles)
    const etag = bodyEtag(body, venueId)
    // Immutable only when the URL names this server's venue: the URL then changes with the
    // venue, so a browser can never splice a cached page of another venue onto this one.
    const immutable = endTime !== undefined && page.settled && venueParam(q.venue) === venueId
    void reply
      .header('cache-control', immutable ? CACHE_IMMUTABLE : CACHE_REVALIDATE)
      .header('etag', etag)
    if (ifNoneMatchHits(request.headers['if-none-match'], etag)) return reply.code(304).send()
    return reply.type('application/json; charset=utf-8').send(body)
  })
}
