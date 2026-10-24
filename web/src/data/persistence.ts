/**
 * Client for the local server's SQLite persistence (`server/README.md`, "REST"):
 * `/api/watchlists/:id` and `/api/layouts/:id`, plus `createServerSync`, a small state machine
 * that keeps a localStorage-backed value in step with one server document.
 *
 * Only UI state lives here (symbols, indicator layouts). No keys, no trading endpoints.
 */

export const DEFAULT_PERSISTENCE_ID = 'default'
export const PERSISTENCE_TIMEOUT_MS = 5000

const ID_RE = /^[A-Za-z0-9_-]{1,64}$/

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>

export interface PersistenceClientOptions {
  /** Defaults to the global `fetch`, looked up at call time. */
  fetchImpl?: FetchLike
  /** '' = same origin (through the Vite `/api` proxy). No trailing slash. */
  baseUrl?: string
  timeoutMs?: number
}

export interface StoredWatchlist {
  id: string
  items: string[]
  updatedAt: number
}

export interface StoredLayout {
  id: string
  data: Record<string, unknown>
  updatedAt: number
}

export interface PersistenceClient {
  /** null when the server has no such watchlist (404). */
  getWatchlist(id: string): Promise<StoredWatchlist | null>
  putWatchlist(id: string, items: readonly string[]): Promise<StoredWatchlist>
  /** null when the server has no such layout (404). */
  getLayout(id: string): Promise<StoredLayout | null>
  putLayout(id: string, data: Record<string, unknown>): Promise<StoredLayout>
}

export type PersistenceErrorKind = 'network' | 'http' | 'parse' | 'bad-id'

export class PersistenceError extends Error {
  readonly kind: PersistenceErrorKind
  readonly status: number | undefined
  constructor(kind: PersistenceErrorKind, message: string, status?: number) {
    super(message)
    this.name = 'PersistenceError'
    this.kind = kind
    this.status = status
  }
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function parseStoredWatchlist(v: unknown): StoredWatchlist {
  if (
    !isRecord(v) ||
    typeof v.id !== 'string' ||
    typeof v.updatedAt !== 'number' ||
    !Number.isFinite(v.updatedAt) ||
    !Array.isArray(v.items) ||
    !v.items.every((x) => typeof x === 'string')
  ) {
    throw new PersistenceError('parse', 'malformed watchlist response')
  }
  return { id: v.id, items: [...(v.items as string[])], updatedAt: v.updatedAt }
}

function parseStoredLayout(v: unknown): StoredLayout {
  if (
    !isRecord(v) ||
    typeof v.id !== 'string' ||
    typeof v.updatedAt !== 'number' ||
    !Number.isFinite(v.updatedAt) ||
    !isRecord(v.data)
  ) {
    throw new PersistenceError('parse', 'malformed layout response')
  }
  return { id: v.id, data: v.data, updatedAt: v.updatedAt }
}

export function createPersistenceClient(opts: PersistenceClientOptions = {}): PersistenceClient {
  const baseUrl = opts.baseUrl ?? ''
  const timeoutMs = opts.timeoutMs ?? PERSISTENCE_TIMEOUT_MS
  const doFetch: FetchLike =
    opts.fetchImpl ??
    ((input, init) => {
      if (typeof globalThis.fetch !== 'function') {
        return Promise.reject(new Error('fetch is not available'))
      }
      return globalThis.fetch(input, init)
    })

  function url(collection: 'watchlists' | 'layouts', id: string): string {
    if (!ID_RE.test(id)) throw new PersistenceError('bad-id', `invalid id: ${id}`)
    return `${baseUrl}/api/${collection}/${encodeURIComponent(id)}`
  }

  /** JSON body of a 2xx response, null for 404 when `allow404`, otherwise throws. */
  async function request(
    target: string,
    init: RequestInit,
    allow404: boolean,
  ): Promise<unknown | null> {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), timeoutMs)
    try {
      let res: Response
      try {
        res = await doFetch(target, { ...init, signal: controller.signal })
      } catch (e) {
        throw new PersistenceError(
          'network',
          controller.signal.aborted ? 'request timed out' : `request failed: ${String(e)}`,
        )
      }
      if (allow404 && res.status === 404) return null
      if (!res.ok) throw new PersistenceError('http', `HTTP ${res.status}`, res.status)
      try {
        return (await res.json()) as unknown
      } catch {
        throw new PersistenceError('parse', 'response is not JSON', res.status)
      }
    } finally {
      clearTimeout(timer)
    }
  }

  const jsonPut = (body: unknown): RequestInit => ({
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

  return {
    async getWatchlist(id) {
      const body = await request(url('watchlists', id), { method: 'GET' }, true)
      return body === null ? null : parseStoredWatchlist(body)
    },
    async putWatchlist(id, items) {
      const body = await request(url('watchlists', id), jsonPut({ items }), false)
      return parseStoredWatchlist(body)
    },
    async getLayout(id) {
      const body = await request(url('layouts', id), { method: 'GET' }, true)
      return body === null ? null : parseStoredLayout(body)
    },
    async putLayout(id, data) {
      const body = await request(url('layouts', id), jsonPut({ data }), false)
      return parseStoredLayout(body)
    },
  }
}

// ---------------------------------------------------------------------------------------------
// Sync state machine
// ---------------------------------------------------------------------------------------------

export type ServerSyncState = 'pending' | 'synced' | 'local-only' | 'disposed'

/** Retries of a failed PUT (after the first attempt) before waiting for the next edit. */
export const MAX_PUT_RETRIES = 3
/** First retry delay; doubles per retry (1 s, 2 s, 4 s by default). */
export const PUT_RETRY_BASE_MS = 1000

export interface ServerSyncOptions<T> {
  /** Current local value (from localStorage), used for the one-time 404 migration. */
  initial: T
  /** GET the server document's payload; resolve null for 404; reject on any other failure. */
  load(): Promise<unknown | null>
  /** PUT the value. */
  save(value: T): Promise<unknown>
  /** Validate the server payload; null = invalid (ignored, local value kept). */
  parse(raw: unknown): T | null
  /** Called once when a valid server value is adopted. Never called after dispose(). */
  onRemote(value: T): void
  /** Equality key; defaults to JSON.stringify. */
  serialize?: (value: T) => string
  debounceMs?: number
  /** First PUT retry delay in ms (doubles per retry). Defaults to PUT_RETRY_BASE_MS. */
  retryBaseMs?: number
}

export interface ServerSync<T> {
  /** Issues the initial GET. Idempotent. */
  start(): void
  /** Report the latest local value. PUT (debounced) only once the initial GET has settled. */
  update(value: T): void
  /**
   * Another tab already wrote `value` (localStorage `storage` event) and saves it itself. Take it
   * as the latest value and as the server's copy: no echo PUT, a queued stale PUT is cancelled,
   * and an edit made while the GET was pending no longer counts as newer.
   */
  adopt(value: T): void
  /** Cancels timers and ignores in-flight results. Nothing is flushed. */
  dispose(): void
  readonly state: ServerSyncState
}

/**
 * Keeps one local value in step with one server document:
 *
 * - start → GET. 200 + valid → adopt (onRemote), no echo PUT, unless the user edited while the
 *   GET was pending: then the local edit (the newer user action) is kept and PUT. 200 + invalid
 *   → keep local; a pending edit (or any later one) is saved. 404 → PUT the latest local value
 *   (migration from localStorage). Any other failure → local-only for the rest of the session.
 * - Edits before the GET settles are only remembered, never PUT. The initial value alone never
 *   counts as an edit, so a stale tab that was merely opened cannot clobber the server.
 * - Afterwards edits are debounced, at most one PUT is in flight, and the latest value wins.
 *   A failed PUT is retried with exponential backoff (retryBaseMs, x2) up to MAX_PUT_RETRIES
 *   times, always with the latest value; then it waits for the next edit. A failed migration
 *   PUT drops to local-only.
 */
export function createServerSync<T>(opts: ServerSyncOptions<T>): ServerSync<T> {
  const serialize = opts.serialize ?? ((v: T) => JSON.stringify(v))
  const debounceMs = opts.debounceMs ?? 300
  const retryBaseMs = opts.retryBaseMs ?? PUT_RETRY_BASE_MS

  let state: ServerSyncState = 'pending'
  let started = false
  let latest: T = opts.initial
  /** Key of the value that is not a user edit (initial, or adopted from another tab). */
  let baselineKey = serialize(opts.initial)
  /** The user edited while the initial GET was pending. */
  let dirty = false
  /** Serialized value the server is known to hold; undefined = unknown. */
  let remoteKey: string | undefined
  let timer: ReturnType<typeof setTimeout> | null = null
  /** The pending timer is a backoff retry (an edit must not shorten it). */
  let retrying = false
  /** Consecutive failed PUTs; reset on success and on a fresh edit after retries ran out. */
  let putFailures = 0
  let inFlight = false

  function clearTimer(): void {
    if (timer !== null) {
      clearTimeout(timer)
      timer = null
    }
    retrying = false
  }

  function onSaved(key: string): void {
    inFlight = false
    if (state !== 'synced') return
    putFailures = 0
    remoteKey = key
    if (serialize(latest) !== remoteKey) schedule()
  }

  function onSaveFailed(): void {
    inFlight = false
    if (state !== 'synced') return
    remoteKey = undefined
    putFailures += 1
    if (putFailures > MAX_PUT_RETRIES) return // give up until the next edit
    clearTimer()
    retrying = true
    timer = setTimeout(flush, retryBaseMs * 2 ** (putFailures - 1))
  }

  function flush(): void {
    timer = null
    retrying = false
    if (state !== 'synced' || inFlight) return
    const value = latest
    const key = serialize(value)
    if (key === remoteKey) return
    inFlight = true
    opts.save(value).then(
      () => onSaved(key),
      () => onSaveFailed(),
    )
  }

  function schedule(): void {
    clearTimer()
    timer = setTimeout(flush, debounceMs)
  }

  function migrate(): void {
    const value = latest
    const key = serialize(value)
    inFlight = true
    opts.save(value).then(
      () => onSaved(key),
      () => {
        inFlight = false
        if (state !== 'disposed') state = 'local-only'
      },
    )
  }

  return {
    start() {
      if (started || state !== 'pending') return
      started = true
      let loading: Promise<unknown | null>
      try {
        loading = opts.load()
      } catch (e) {
        loading = Promise.reject(e)
      }
      loading.then(
        (raw) => {
          if (state !== 'pending') return
          state = 'synced'
          if (raw === null) {
            migrate()
            return
          }
          const value = opts.parse(raw)
          if (value === null) {
            // Invalid server copy: keep local; a pending edit (or the next one) replaces it.
            if (dirty) schedule()
            return
          }
          remoteKey = serialize(value)
          if (dirty) {
            // The user edited after this tab loaded: that edit is newer than the server copy.
            if (serialize(latest) !== remoteKey) schedule()
            return
          }
          latest = value
          opts.onRemote(value)
        },
        () => {
          if (state === 'pending') state = 'local-only'
        },
      )
    },
    update(value) {
      latest = value
      const key = serialize(value)
      if (state === 'pending') {
        dirty = key !== baselineKey
        return
      }
      if (state !== 'synced') return
      if (key === remoteKey && !inFlight) {
        clearTimer()
        return
      }
      if (retrying) return // the backoff retry sends the latest value
      if (putFailures > MAX_PUT_RETRIES) putFailures = 0 // a fresh edit after giving up
      schedule()
    },
    adopt(value) {
      latest = value
      const key = serialize(value)
      if (state === 'pending') {
        baselineKey = key
        dirty = false
        return
      }
      if (state !== 'synced') return
      remoteKey = key
      putFailures = 0
      // Drop any queued PUT of the old value. A stale PUT still in flight compares against
      // `latest` when it settles and re-sends the adopted value, so the server ends on it.
      clearTimer()
    },
    dispose() {
      state = 'disposed'
      clearTimer()
    },
    get state() {
      return state
    },
  }
}
