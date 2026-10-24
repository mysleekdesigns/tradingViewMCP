/**
 * Client for the local server's drawing store (`server/README.md`, "REST"):
 * `GET /api/drawings?symbol=SYM`, `PUT /api/drawings/:id`, `DELETE /api/drawings/:id`.
 * Also the validator shared by the server rows and the localStorage mirror (`parseDrawing`),
 * and `createLocalServerRequest`, the JSON request helper the alert client reuses.
 *
 * Only chart annotations live here. No keys, no exchange calls, no trading endpoints.
 */

import {
  ANCHOR_COUNT,
  DRAWING_KINDS,
  type Anchor,
  type Drawing,
  type DrawingKind,
  type DrawingStyle,
  type LineStyleName,
  type StoredDrawingData,
} from '../chart/drawings/types'
import { PERSISTENCE_TIMEOUT_MS, PersistenceError, type FetchLike } from './persistence'

export const DRAWING_ID_RE = /^[A-Za-z0-9_-]{1,64}$/
/** Same rule as the server's drawing symbol check. */
export const DRAWING_SYMBOL_RE = /^[A-Z0-9]{2,30}$/

const COLOR_RE = /^#[0-9a-fA-F]{6}$/
const LINE_STYLES: readonly LineStyleName[] = ['solid', 'dashed', 'dotted']

export interface LocalServerClientOptions {
  /** Defaults to the global `fetch`, looked up at call time. */
  fetchImpl?: FetchLike
  /** '' = same origin (through the Vite `/api` proxy). A trailing slash is dropped. */
  baseUrl?: string
  timeoutMs?: number
}

export interface DrawingWriteOptions {
  /**
   * Send with `fetch(..., { keepalive: true })` so the request outlives the page (pagehide,
   * unmount). The browser caps keepalive bodies at 64 KB in total; one drawing is far below.
   */
  keepalive?: boolean
}

export interface DrawingsClient {
  /** Drawings stored for `symbol`; invalid rows are skipped. */
  list(symbol: string, signal?: AbortSignal): Promise<Drawing[]>
  put(drawing: Drawing, opts?: DrawingWriteOptions): Promise<void>
  /** A 404 (already gone) counts as success. */
  remove(id: string, opts?: DrawingWriteOptions): Promise<void>
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function isFiniteNumber(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

// ---------------------------------------------------------------------------------------------
// Shared JSON request helper
// ---------------------------------------------------------------------------------------------

export interface LocalServerRequestInit {
  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  /** JSON-encoded when given. */
  body?: unknown
  signal?: AbortSignal
  /** Statuses (besides 2xx) that resolve instead of throwing, e.g. 404 for a delete. */
  okStatuses?: readonly number[]
  /** Passed to fetch: the request may outlive the page. The timeout still applies. */
  keepalive?: boolean
}

export interface LocalServerResponse {
  status: number
  /** Parsed JSON body; undefined for 204, an allowed non-2xx status, or an empty body. */
  body: unknown
}

export type LocalServerRequest = (
  path: string,
  init: LocalServerRequestInit,
) => Promise<LocalServerResponse>

const NO_BODY_STATUSES = new Set([204, 205, 304])

/**
 * A request function for the local server. Every failure rejects with a `PersistenceError`
 * whose `message` is readable in the UI: the server's `{ error, message }` body for non-2xx
 * answers, otherwise a short transport/parse description. A caller abort rejects with
 * kind 'network' and message 'request cancelled' (check `signal.aborted` to tell it apart).
 */
export function createLocalServerRequest(opts: LocalServerClientOptions = {}): LocalServerRequest {
  const baseUrl = (opts.baseUrl ?? '').replace(/\/+$/, '')
  const timeoutMs = opts.timeoutMs ?? PERSISTENCE_TIMEOUT_MS
  const doFetch: FetchLike =
    opts.fetchImpl ??
    ((input, init) => {
      if (typeof globalThis.fetch !== 'function') {
        return Promise.reject(new Error('fetch is not available'))
      }
      return globalThis.fetch(input, init)
    })

  return async (path, init) => {
    if (init.signal?.aborted) throw new PersistenceError('network', 'request cancelled')
    const controller = new AbortController()
    let timedOut = false
    const timer = setTimeout(() => {
      timedOut = true
      controller.abort()
    }, timeoutMs)
    const onCallerAbort = (): void => controller.abort()
    init.signal?.addEventListener('abort', onCallerAbort, { once: true })

    const reqInit: RequestInit = { method: init.method, signal: controller.signal }
    if (init.keepalive === true) reqInit.keepalive = true
    if (init.body !== undefined) {
      reqInit.headers = { 'Content-Type': 'application/json' }
      reqInit.body = JSON.stringify(init.body)
    }
    try {
      let res: Response
      try {
        res = await doFetch(`${baseUrl}${path}`, reqInit)
      } catch {
        if (timedOut) throw new PersistenceError('network', 'The local server did not respond')
        if (init.signal?.aborted) throw new PersistenceError('network', 'request cancelled')
        throw new PersistenceError('network', 'Cannot reach the local server')
      }
      const allowed = init.okStatuses?.includes(res.status) === true
      if (!res.ok && !allowed) {
        let message = `Server error (HTTP ${res.status})`
        try {
          const errBody: unknown = await res.json()
          if (isRecord(errBody) && typeof errBody.message === 'string' && errBody.message !== '') {
            message = errBody.message
          }
        } catch {
          // Not JSON: keep the generic message.
        }
        throw new PersistenceError('http', message, res.status)
      }
      if (!res.ok || NO_BODY_STATUSES.has(res.status))
        return { status: res.status, body: undefined }
      let text: string
      try {
        text = await res.text()
      } catch {
        if (init.signal?.aborted) throw new PersistenceError('network', 'request cancelled')
        throw new PersistenceError('network', 'The local server response was cut off', res.status)
      }
      if (text.trim() === '') return { status: res.status, body: undefined }
      try {
        return { status: res.status, body: JSON.parse(text) as unknown }
      } catch {
        throw new PersistenceError(
          'parse',
          'The local server sent an unreadable response',
          res.status,
        )
      }
    } finally {
      clearTimeout(timer)
      init.signal?.removeEventListener('abort', onCallerAbort)
    }
  }
}

// ---------------------------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------------------------

function parseKind(v: unknown): DrawingKind | null {
  return typeof v === 'string' && (DRAWING_KINDS as readonly string[]).includes(v)
    ? (v as DrawingKind)
    : null
}

function parseAnchor(v: unknown): Anchor | null {
  if (!isRecord(v) || !isFiniteNumber(v.time) || !isFiniteNumber(v.price) || v.price <= 0) {
    return null
  }
  return { time: v.time, price: v.price }
}

export function parseDrawingStyle(v: unknown): DrawingStyle | null {
  if (!isRecord(v)) return null
  const { color, lineWidth, lineStyle } = v
  if (typeof color !== 'string' || !COLOR_RE.test(color)) return null
  if (lineWidth !== 1 && lineWidth !== 2 && lineWidth !== 3 && lineWidth !== 4) return null
  if (typeof lineStyle !== 'string' || !(LINE_STYLES as readonly string[]).includes(lineStyle)) {
    return null
  }
  return { color, lineWidth, lineStyle: lineStyle as LineStyleName }
}

/** `anchors` for `kind`: exactly ANCHOR_COUNT[kind] valid anchors, else null. */
export function parseAnchors(kind: DrawingKind, v: unknown): Anchor[] | null {
  if (!Array.isArray(v) || v.length !== ANCHOR_COUNT[kind]) return null
  const out: Anchor[] = []
  for (const a of v) {
    const anchor = parseAnchor(a)
    if (anchor === null) return null
    out.push(anchor)
  }
  return out
}

/**
 * Validates server/localStorage payloads: kind known, anchors.length === ANCHOR_COUNT[kind],
 * finite time/price (price > 0), style colour #rrggbb, lineWidth 1-4, lineStyle known; else
 * null. Accepts a server row `{ id, symbol, data: { v: 1, kind, anchors, style }, ... }` or a
 * flat `Drawing` `{ id, symbol, kind, anchors, style }` (the mirror format). The result is a
 * fresh object holding only the known fields; the symbol must already be upper case.
 */
export function parseDrawing(raw: unknown): Drawing | null {
  if (!isRecord(raw)) return null
  const { id, symbol } = raw
  if (typeof id !== 'string' || !DRAWING_ID_RE.test(id)) return null
  if (typeof symbol !== 'string' || !DRAWING_SYMBOL_RE.test(symbol)) return null
  let body: Record<string, unknown> = raw
  if (raw.data !== undefined) {
    if (!isRecord(raw.data) || raw.data.v !== 1) return null
    body = raw.data
  }
  const kind = parseKind(body.kind)
  if (kind === null) return null
  const anchors = parseAnchors(kind, body.anchors)
  if (anchors === null) return null
  const style = parseDrawingStyle(body.style)
  if (style === null) return null
  return { id, symbol, kind, anchors, style }
}

/** The server's `data` object for `d` (deep copy, known fields only). */
export function toStoredData(d: Drawing): StoredDrawingData {
  return {
    v: 1,
    kind: d.kind,
    anchors: d.anchors.map((a) => ({ time: a.time, price: a.price })),
    style: { color: d.style.color, lineWidth: d.style.lineWidth, lineStyle: d.style.lineStyle },
  }
}

// ---------------------------------------------------------------------------------------------
// Client
// ---------------------------------------------------------------------------------------------

function checkId(id: string): string {
  if (!DRAWING_ID_RE.test(id)) throw new PersistenceError('bad-id', `Invalid drawing id: ${id}`)
  return encodeURIComponent(id)
}

export function createDrawingsClient(opts: LocalServerClientOptions = {}): DrawingsClient {
  const request = createLocalServerRequest(opts)
  return {
    async list(symbol, signal) {
      const sym = symbol.trim().toUpperCase()
      if (!DRAWING_SYMBOL_RE.test(sym)) {
        throw new PersistenceError('bad-id', `Invalid symbol: ${symbol}`)
      }
      const { body } = await request(`/api/drawings?symbol=${encodeURIComponent(sym)}`, {
        method: 'GET',
        signal,
      })
      if (!Array.isArray(body)) {
        throw new PersistenceError('parse', 'The local server sent an unreadable drawing list')
      }
      const out: Drawing[] = []
      const seen = new Set<string>()
      for (const row of body) {
        const d = parseDrawing(row)
        if (d === null || d.symbol !== sym || seen.has(d.id)) continue
        seen.add(d.id)
        out.push(d)
      }
      return out
    },
    async put(drawing, writeOpts) {
      const path = `/api/drawings/${checkId(drawing.id)}`
      await request(path, {
        method: 'PUT',
        body: { symbol: drawing.symbol, data: toStoredData(drawing) },
        keepalive: writeOpts?.keepalive === true,
      })
    },
    async remove(id, writeOpts) {
      await request(`/api/drawings/${checkId(id)}`, {
        method: 'DELETE',
        okStatuses: [404],
        keepalive: writeOpts?.keepalive === true,
      })
    },
  }
}
