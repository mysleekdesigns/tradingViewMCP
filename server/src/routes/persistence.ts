import type { FastifyInstance } from 'fastify'
import type { RouteOptions } from '../app.ts'
import { ApiError } from '../types.ts'

// REST persistence for watchlists, layouts and drawings. See server/README.md for the contract.
// All SQL goes through statements prepared once per plugin instance; no string-built SQL.

export const MAX_WATCHLIST_ITEMS = 200
export const MAX_LAYOUT_BYTES = 256 * 1024
export const MAX_DRAWING_BYTES = 64 * 1024
/** Headroom above the JSON cap for the request envelope, so oversize bodies fail cleanly. */
const BODY_HEADROOM = 16 * 1024

const ID_RE = /^[A-Za-z0-9_-]{1,64}$/
const WATCH_SYMBOL_RE = /^[A-Z0-9][A-Z0-9._-]{0,39}$/
const DRAWING_SYMBOL_RE = /^[A-Z0-9]{2,30}$/

export interface Watchlist {
  id: string
  items: string[]
  updatedAt: number
}

export interface Layout {
  id: string
  data: Record<string, unknown>
  updatedAt: number
}

export interface Drawing {
  id: string
  symbol: string
  data: Record<string, unknown>
  updatedAt: number
}

interface IdParams {
  id: string
}

function badRequest(message: string): ApiError {
  return new ApiError('http', message, 400)
}

function notFound(message: string): ApiError {
  return new ApiError('http', message, 404)
}

function parseId(params: unknown): string {
  const id = (params as Partial<IdParams> | undefined)?.id
  if (typeof id !== 'string' || !ID_RE.test(id)) {
    throw badRequest('id must match ^[A-Za-z0-9_-]{1,64}$')
  }
  return id
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function bodyObject(body: unknown): Record<string, unknown> {
  if (!isPlainObject(body)) throw badRequest('body must be a JSON object')
  return body
}

/** Validates `data` as a plain object and returns its JSON serialisation, enforcing `maxBytes`. */
function serializeData(data: unknown, maxBytes: number): string {
  if (!isPlainObject(data)) throw badRequest('data must be a JSON object')
  const json = JSON.stringify(data)
  if (Buffer.byteLength(json, 'utf8') > maxBytes) {
    throw new ApiError('http', `data exceeds ${maxBytes} bytes`, 413)
  }
  return json
}

function parseWatchItems(raw: unknown): string[] {
  if (!Array.isArray(raw)) throw badRequest('items must be an array of symbols')
  if (raw.length > MAX_WATCHLIST_ITEMS) {
    throw badRequest(`items must have at most ${MAX_WATCHLIST_ITEMS} symbols`)
  }
  const seen = new Set<string>()
  const out: string[] = []
  for (const item of raw) {
    if (typeof item !== 'string') throw badRequest('items must be strings')
    const sym = item.trim().toUpperCase()
    if (!WATCH_SYMBOL_RE.test(sym)) throw badRequest(`invalid symbol: ${JSON.stringify(item)}`)
    if (seen.has(sym)) continue
    seen.add(sym)
    out.push(sym)
  }
  return out
}

function parseDrawingSymbol(raw: unknown): string {
  if (typeof raw !== 'string') throw badRequest('symbol is required')
  const sym = raw.trim().toUpperCase()
  if (!DRAWING_SYMBOL_RE.test(sym)) throw badRequest('symbol must match ^[A-Z0-9]{2,30}$')
  return sym
}

/** Parses a stored JSON object; corrupt rows throw a plain Error, which the handler turns into 500. */
function parseStoredObject(json: string, what: string): Record<string, unknown> {
  const v: unknown = JSON.parse(json)
  if (!isPlainObject(v)) throw new Error(`stored ${what} is not a JSON object`)
  return v
}

function parseStoredItems(json: string): string[] {
  const v: unknown = JSON.parse(json)
  if (!Array.isArray(v) || !v.every((x) => typeof x === 'string')) {
    throw new Error('stored watchlist items are not a string array')
  }
  return v as string[]
}

interface WatchlistRow {
  id: string
  items: string
  updated_at: number
}

interface LayoutRow {
  id: string
  data: string
  updated_at: number
}

interface DrawingRow {
  id: string
  symbol: string
  data: string
  updated_at: number
}

export default async function persistenceRoutes(
  app: FastifyInstance,
  opts: RouteOptions,
): Promise<void> {
  const { db } = opts
  const nowSec = (): number => Math.floor(opts.now() / 1000)

  const getWatchlist = db.prepare<[string], WatchlistRow>(
    'SELECT id, items, updated_at FROM watchlists WHERE id = ?',
  )
  const upsertWatchlist = db.prepare<[string, string, number]>(
    `INSERT INTO watchlists (id, items, updated_at) VALUES (?, ?, ?)
     ON CONFLICT(id) DO UPDATE SET items = excluded.items, updated_at = excluded.updated_at`,
  )
  const getLayout = db.prepare<[string], LayoutRow>(
    'SELECT id, data, updated_at FROM layouts WHERE id = ?',
  )
  const upsertLayout = db.prepare<[string, string, number]>(
    `INSERT INTO layouts (id, data, updated_at) VALUES (?, ?, ?)
     ON CONFLICT(id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at`,
  )
  const listDrawings = db.prepare<[string], DrawingRow>(
    'SELECT id, symbol, data, updated_at FROM drawings WHERE symbol = ? ORDER BY updated_at, id',
  )
  const upsertDrawing = db.prepare<[string, string, string, number]>(
    `INSERT INTO drawings (id, symbol, data, updated_at) VALUES (?, ?, ?, ?)
     ON CONFLICT(id) DO UPDATE SET symbol = excluded.symbol, data = excluded.data,
       updated_at = excluded.updated_at`,
  )
  const deleteDrawing = db.prepare<[string]>('DELETE FROM drawings WHERE id = ?')

  // --- watchlists -------------------------------------------------------------------------

  app.get('/api/watchlists/:id', async (request): Promise<Watchlist> => {
    const id = parseId(request.params)
    const row = getWatchlist.get(id)
    if (!row) throw notFound(`watchlist ${id} not found`)
    return { id: row.id, items: parseStoredItems(row.items), updatedAt: row.updated_at }
  })

  app.put(
    '/api/watchlists/:id',
    // 200 symbols x 40 chars is far below this; it only bounds abusive bodies.
    { bodyLimit: 64 * 1024 },
    async (request): Promise<Watchlist> => {
      const id = parseId(request.params)
      const items = parseWatchItems(bodyObject(request.body).items)
      const updatedAt = nowSec()
      upsertWatchlist.run(id, JSON.stringify(items), updatedAt)
      return { id, items, updatedAt }
    },
  )

  // --- layouts ----------------------------------------------------------------------------

  app.get('/api/layouts/:id', async (request): Promise<Layout> => {
    const id = parseId(request.params)
    const row = getLayout.get(id)
    if (!row) throw notFound(`layout ${id} not found`)
    return { id: row.id, data: parseStoredObject(row.data, 'layout'), updatedAt: row.updated_at }
  })

  app.put(
    '/api/layouts/:id',
    { bodyLimit: MAX_LAYOUT_BYTES + BODY_HEADROOM },
    async (request): Promise<Layout> => {
      const id = parseId(request.params)
      const body = bodyObject(request.body)
      const json = serializeData(body.data, MAX_LAYOUT_BYTES)
      const updatedAt = nowSec()
      upsertLayout.run(id, json, updatedAt)
      return { id, data: JSON.parse(json) as Record<string, unknown>, updatedAt }
    },
  )

  // --- drawings ---------------------------------------------------------------------------

  app.get('/api/drawings', async (request): Promise<Drawing[]> => {
    const symbol = parseDrawingSymbol((request.query as { symbol?: unknown } | undefined)?.symbol)
    return listDrawings.all(symbol).map((row) => ({
      id: row.id,
      symbol: row.symbol,
      data: parseStoredObject(row.data, 'drawing'),
      updatedAt: row.updated_at,
    }))
  })

  app.put(
    '/api/drawings/:id',
    { bodyLimit: MAX_DRAWING_BYTES + BODY_HEADROOM },
    async (request): Promise<Drawing> => {
      const id = parseId(request.params)
      const body = bodyObject(request.body)
      const symbol = parseDrawingSymbol(body.symbol)
      const json = serializeData(body.data, MAX_DRAWING_BYTES)
      const updatedAt = nowSec()
      upsertDrawing.run(id, symbol, json, updatedAt)
      return { id, symbol, data: JSON.parse(json) as Record<string, unknown>, updatedAt }
    },
  )

  app.delete('/api/drawings/:id', async (request, reply) => {
    const id = parseId(request.params)
    const result = deleteDrawing.run(id)
    if (result.changes === 0) throw notFound(`drawing ${id} not found`)
    return reply.status(204).send()
  })
}
