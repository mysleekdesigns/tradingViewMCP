import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Drawing, DrawingPatch } from '../chart/drawings/types'
import {
  DRAWING_ID_RE,
  parseAnchors,
  parseDrawing,
  parseDrawingStyle,
  type DrawingsClient,
} from '../data/drawingsApi'
import { MAX_PUT_RETRIES, PersistenceError, PUT_RETRY_BASE_MS } from '../data/persistence'

/**
 * Drawings of one symbol, persisted per symbol: a localStorage mirror (`tv.drawings.v1.SYM`,
 * synchronous initial value and offline copy) plus the local server (`/api/drawings`), which
 * wins once its GET returns. Without a server (direct mode) the mirror is the only copy.
 */

export type DrawingsSyncStatus = 'loading' | 'synced' | 'local-only'

export interface DrawingsApi {
  /** Current symbol only. */
  items: readonly Drawing[]
  status: DrawingsSyncStatus
  /**
   * True when the drawing was accepted into state; false when rejected (invalid, another
   * symbol, duplicate id, MAX_DRAWINGS_PER_SYMBOL reached, or no store for this symbol yet).
   */
  add(d: Drawing): boolean
  /** True when applied; false for an unknown id or an invalid patch (e.g. price <= 0). */
  update(id: string, patch: DrawingPatch): boolean
  remove(id: string): void
  /** All drawings of the current symbol. */
  clear(): void
}

export const DRAWINGS_STORAGE_PREFIX = 'tv.drawings.v1.'
/** Upper bound per symbol; protects storage and the chart against runaway or hostile data. */
export const MAX_DRAWINGS_PER_SYMBOL = 500
/** Rapid updates to one drawing (a drag) are coalesced into one PUT after this quiet period. */
export const DRAWING_PUT_DEBOUNCE_MS = 300

export function drawingsStorageKey(symbol: string): string {
  return DRAWINGS_STORAGE_PREFIX + normalizeDrawingSymbol(symbol)
}

export function normalizeDrawingSymbol(symbol: string): string {
  return symbol.trim().toUpperCase()
}

// ---------------------------------------------------------------------------------------------
// Mirror (localStorage)
// ---------------------------------------------------------------------------------------------

/** Minimal storage surface, so tests can pass a fake (or one that throws). */
export type DrawingsStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

function defaultStorage(): DrawingsStorage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    return null
  }
}

/**
 * Parse a stored mirror value: a JSON array of drawings. Invalid entries, entries for another
 * symbol, and duplicate ids are dropped; at most MAX_DRAWINGS_PER_SYMBOL are kept. Corrupt
 * JSON or a non-array reads as [].
 */
export function parseMirror(raw: string | null, symbol: string): Drawing[] {
  if (raw === null) return []
  let value: unknown
  try {
    value = JSON.parse(raw)
  } catch {
    return []
  }
  if (!Array.isArray(value)) return []
  const sym = normalizeDrawingSymbol(symbol)
  const out: Drawing[] = []
  const seen = new Set<string>()
  for (const entry of value) {
    if (out.length >= MAX_DRAWINGS_PER_SYMBOL) break
    const d = parseDrawing(entry)
    if (d === null || d.symbol !== sym || seen.has(d.id)) continue
    seen.add(d.id)
    out.push(d)
  }
  return out
}

export function serializeMirror(items: readonly Drawing[]): string {
  return JSON.stringify(
    items.map((d) => ({
      id: d.id,
      symbol: d.symbol,
      kind: d.kind,
      anchors: d.anchors,
      style: d.style,
    })),
  )
}

export function readMirror(
  symbol: string,
  storage: DrawingsStorage | null = defaultStorage(),
): Drawing[] {
  if (storage === null) return []
  try {
    return parseMirror(storage.getItem(drawingsStorageKey(symbol)), symbol)
  } catch {
    return []
  }
}

/**
 * `readMirror` for the hook: the mirror as of a (symbol, client) pair. The client takes no part
 * in the read; it is an argument so the hook's memo (and the React Compiler's, which keys on
 * what the callback uses) re-reads the mirror when the client changes, without an
 * over-specified dependency list.
 */
export function readMirrorForClient(symbol: string, client: DrawingsClient | null): Drawing[] {
  void client
  return readMirror(symbol)
}

/** Persist the symbol's drawings (an empty list removes the key). Failures are ignored. */
export function writeMirror(
  symbol: string,
  items: readonly Drawing[],
  storage: DrawingsStorage | null = defaultStorage(),
): void {
  if (storage === null) return
  try {
    const key = drawingsStorageKey(symbol)
    if (items.length === 0) storage.removeItem(key)
    else storage.setItem(key, serializeMirror(items))
  } catch {
    // Storage full or blocked: the drawings still work for this session.
  }
}

/** Like writeMirror, but leaves storage alone when it already holds exactly this content. */
function writeMirrorIfChanged(
  symbol: string,
  items: readonly Drawing[],
  storage: DrawingsStorage | null,
): void {
  if (storage === null) return
  let current: string | null
  try {
    current = storage.getItem(drawingsStorageKey(symbol))
  } catch {
    writeMirror(symbol, items, storage) // unreadable: try to write anyway
    return
  }
  const wanted = items.length === 0 ? null : serializeMirror(items)
  if (current !== wanted) writeMirror(symbol, items, storage)
}

// ---------------------------------------------------------------------------------------------
// Unsynced ids (localStorage, next to the mirror)
// ---------------------------------------------------------------------------------------------

/**
 * Ids whose server copy may differ from the mirror: local adds/updates (`upserts`) and local
 * removals (`removed`) the server has not acknowledged. Survives reloads and symbol switches, so
 * the next load lets the local state win for these ids and sends them again.
 */
export interface PendingIds {
  upserts: string[]
  removed: string[]
}

/** Oldest entries are dropped beyond this (removals can outnumber the drawing cap). */
export const MAX_PENDING_REMOVALS = 2 * MAX_DRAWINGS_PER_SYMBOL

export function drawingsPendingKey(symbol: string): string {
  return `${drawingsStorageKey(symbol)}.pending`
}

function parseIdList(v: unknown, cap: number, skip: ReadonlySet<string>): string[] {
  if (!Array.isArray(v)) return []
  const out: string[] = []
  const seen = new Set<string>()
  for (const id of v) {
    if (typeof id !== 'string' || !DRAWING_ID_RE.test(id) || seen.has(id) || skip.has(id)) continue
    seen.add(id)
    out.push(id)
  }
  return out.slice(-cap)
}

/**
 * Parse a stored pending value `{ upserts: string[], removed: string[] }`. Invalid ids and
 * duplicates are dropped, an id in both lists counts as an upsert, and each list is capped
 * (newest kept). Anything malformed reads as empty.
 */
export function parsePending(raw: string | null): PendingIds {
  if (raw === null) return { upserts: [], removed: [] }
  let value: unknown
  try {
    value = JSON.parse(raw)
  } catch {
    return { upserts: [], removed: [] }
  }
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return { upserts: [], removed: [] }
  }
  const rec = value as Record<string, unknown>
  const upserts = parseIdList(rec.upserts, MAX_DRAWINGS_PER_SYMBOL, new Set())
  const removed = parseIdList(rec.removed, MAX_PENDING_REMOVALS, new Set(upserts))
  return { upserts, removed }
}

export function readPending(
  symbol: string,
  storage: DrawingsStorage | null = defaultStorage(),
): PendingIds {
  if (storage === null) return { upserts: [], removed: [] }
  try {
    return parsePending(storage.getItem(drawingsPendingKey(symbol)))
  } catch {
    return { upserts: [], removed: [] }
  }
}

/** Persist the pending ids (both lists empty removes the key). Failures are ignored. */
export function writePending(
  symbol: string,
  pending: PendingIds,
  storage: DrawingsStorage | null = defaultStorage(),
): void {
  if (storage === null) return
  try {
    const key = drawingsPendingKey(symbol)
    if (pending.upserts.length === 0 && pending.removed.length === 0) storage.removeItem(key)
    else {
      storage.setItem(
        key,
        JSON.stringify({
          upserts: pending.upserts.slice(-MAX_DRAWINGS_PER_SYMBOL),
          removed: pending.removed.slice(-MAX_PENDING_REMOVALS),
        }),
      )
    }
  } catch {
    // Storage full or blocked: the in-memory sync still runs.
  }
}

/**
 * Record `id` as unsynced: an upsert when it exists locally (`present`), else a removal.
 * Read-modify-write, so another tab's pending ids for the same symbol are kept.
 */
function markPending(
  symbol: string,
  id: string,
  present: boolean,
  storage: DrawingsStorage | null,
): void {
  if (storage === null) return
  const p = readPending(symbol, storage)
  const list = present ? p.upserts : p.removed
  if (list.at(-1) === id && !(present ? p.removed : p.upserts).includes(id)) return
  writePending(
    symbol,
    {
      upserts: [...p.upserts.filter((x) => x !== id), ...(present ? [id] : [])],
      removed: [...p.removed.filter((x) => x !== id), ...(present ? [] : [id])],
    },
    storage,
  )
}

function dropPending(symbol: string, id: string, storage: DrawingsStorage | null): void {
  if (storage === null) return
  const p = readPending(symbol, storage)
  if (!p.upserts.includes(id) && !p.removed.includes(id)) return
  writePending(
    symbol,
    { upserts: p.upserts.filter((x) => x !== id), removed: p.removed.filter((x) => x !== id) },
    storage,
  )
}

// ---------------------------------------------------------------------------------------------
// Reducer
// ---------------------------------------------------------------------------------------------

export type DrawingsAction =
  | { type: 'add'; drawing: Drawing }
  | { type: 'update'; id: string; patch: DrawingPatch }
  | { type: 'remove'; id: string }
  | { type: 'clear' }
  | { type: 'replace'; items: readonly Drawing[] }

/** `d` with `patch` applied, or null when the result is invalid (wrong anchor count etc.). */
export function applyDrawingPatch(d: Drawing, patch: DrawingPatch): Drawing | null {
  const anchors = patch.anchors === undefined ? d.anchors : parseAnchors(d.kind, patch.anchors)
  const style = patch.style === undefined ? d.style : parseDrawingStyle(patch.style)
  if (anchors === null || style === null) return null
  return { ...d, anchors, style }
}

/**
 * Pure state transitions for one symbol's drawings. Returns `items` unchanged when the action
 * does nothing: add of an invalid drawing, a drawing for `symbol`'s other symbols, a duplicate
 * id, or beyond MAX_DRAWINGS_PER_SYMBOL; update/remove of an unknown id; an invalid patch.
 */
export function drawingsReducer(
  items: readonly Drawing[],
  action: DrawingsAction,
  symbol: string,
): readonly Drawing[] {
  const sym = normalizeDrawingSymbol(symbol)
  switch (action.type) {
    case 'add': {
      const d = parseDrawing(action.drawing)
      if (d === null || d.symbol !== sym) return items
      if (items.length >= MAX_DRAWINGS_PER_SYMBOL || items.some((x) => x.id === d.id)) return items
      return [...items, d]
    }
    case 'update': {
      const i = items.findIndex((x) => x.id === action.id)
      if (i < 0) return items
      const next = applyDrawingPatch(items[i], action.patch)
      if (next === null) return items
      const out = items.slice()
      out[i] = next
      return out
    }
    case 'remove':
      return items.some((x) => x.id === action.id) ? items.filter((x) => x.id !== action.id) : items
    case 'clear':
      return items.length === 0 ? items : []
    case 'replace':
      return action.items.slice(0, MAX_DRAWINGS_PER_SYMBOL)
  }
}

// ---------------------------------------------------------------------------------------------
// Initial reconciliation
// ---------------------------------------------------------------------------------------------

export interface PendingEdits {
  /** Unsynced ids that exist locally: edited while loading, or never acknowledged before. */
  upserted: ReadonlySet<string>
  /** Unsynced ids removed locally (while loading, or a DELETE never acknowledged). */
  removed: ReadonlySet<string>
}

export interface Reconciled {
  items: Drawing[]
  /** Drawings to PUT. */
  puts: Drawing[]
  /** Ids to DELETE on the server. */
  deletes: string[]
}

/**
 * Decide the state once the server's list arrives. `local` is what the user sees (mirror plus
 * any edits made while loading).
 *
 * - Server empty, local non-empty (first run after direct mode, or the server DB was reset):
 *   keep local and upload every drawing.
 * - Otherwise the server list wins, except for unsynced ids (`edits`: touched while loading or
 *   persisted as pending by an earlier session): local adds/updates replace or extend the
 *   server list and are PUT; local removals are dropped and, when the server has them, DELETEd.
 */
export function reconcileDrawings(
  server: readonly Drawing[],
  local: readonly Drawing[],
  edits: PendingEdits,
): Reconciled {
  if (server.length === 0) {
    const items = local.slice(0, MAX_DRAWINGS_PER_SYMBOL)
    return { items, puts: items.slice(), deletes: [] }
  }
  const localById = new Map(local.map((d) => [d.id, d]))
  const items: Drawing[] = []
  const puts: Drawing[] = []
  const deletes: string[] = []
  const placed = new Set<string>()
  for (const d of server) {
    if (edits.removed.has(d.id)) {
      deletes.push(d.id)
      continue
    }
    const mine = edits.upserted.has(d.id) ? localById.get(d.id) : undefined
    items.push(mine ?? d)
    if (mine) puts.push(mine)
    placed.add(d.id)
  }
  for (const d of local) {
    if (placed.has(d.id) || !edits.upserted.has(d.id)) continue
    items.push(d)
    puts.push(d)
  }
  const capped = items.slice(0, MAX_DRAWINGS_PER_SYMBOL)
  const kept = new Set(capped.map((d) => d.id))
  return { items: capped, puts: puts.filter((d) => kept.has(d.id)), deletes }
}

// ---------------------------------------------------------------------------------------------
// Store (non-React core)
// ---------------------------------------------------------------------------------------------

/** The part of `window` the store needs: the `pagehide` event. */
export interface PageLifecycleTarget {
  addEventListener(type: 'pagehide', listener: () => void): void
  removeEventListener(type: 'pagehide', listener: () => void): void
}

function defaultPageTarget(): PageLifecycleTarget | null {
  return typeof window === 'undefined' ? null : window
}

export interface DrawingsStoreOptions {
  symbol: string
  /** null = local-only (direct mode, no server). */
  client: DrawingsClient | null
  /** Called after every change of items or status (never after dispose()). */
  onChange: (items: readonly Drawing[], status: DrawingsSyncStatus) => void
  storage?: DrawingsStorage | null
  /** Where `pagehide` is listened for; defaults to `window` (null = none). */
  page?: PageLifecycleTarget | null
  debounceMs?: number
  retryBaseMs?: number
}

export type DrawingsStoreAction = Exclude<DrawingsAction, { type: 'replace' }>

export interface DrawingsStore {
  readonly symbol: string
  readonly items: readonly Drawing[]
  readonly status: DrawingsSyncStatus
  /** Shows the mirror (already loaded at creation), issues the GET, listens for pagehide. */
  start(): void
  /** True when the action changed the state (false: rejected or a no-op). */
  dispatch(action: DrawingsStoreAction): boolean
  /**
   * Another tab wrote this symbol's mirror (`storage` event newValue). Take it without echoing
   * PUTs, but keep this tab's unsynced edits on top (their writes still go out).
   */
  adoptMirror(raw: string | null): void
  /**
   * Aborts the GET, cancels timers, ignores in-flight results, and sends every unsynced write
   * fire-and-forget with keepalive. The ids stay pending in storage until a later load sees
   * the server acknowledge them.
   */
  dispose(): void
}

interface Slot {
  timer: ReturnType<typeof setTimeout> | null
  /** The pending timer is a backoff retry (an edit must not shorten it). */
  retrying: boolean
  inFlight: boolean
  /** Version sent by the in-flight request. */
  sentVersion: number
  /** The id changed again while a request was in flight. */
  again: boolean
  failures: number
}

/** A rejection that will never succeed on retry: HTTP 4xx (except 408/429) or a bad id. */
export function isPermanentWriteFailure(e: unknown): boolean {
  if (!(e instanceof PersistenceError)) return false
  if (e.kind === 'bad-id') return true
  const st = e.status
  return e.kind === 'http' && st !== undefined && st >= 400 && st < 500 && st !== 408 && st !== 429
}

/**
 * One symbol's drawings and their sync with the server. Each drawing id is synced on its own:
 * the server is brought to the local state of that id (PUT when it exists, DELETE when not),
 * debounced per id, at most one request in flight per id, and a failed request is retried with
 * exponential backoff (retryBaseMs, x2) up to MAX_PUT_RETRIES times, then waits for the next
 * edit of that drawing. A 4xx is not retried. A list failure makes the symbol local-only
 * (mirror only, no requests).
 *
 * Every local change marks its id unsynced, in memory (with a version) and in storage
 * (`drawingsPendingKey`); only the server acknowledging that exact version clears it. On load,
 * unsynced ids win over the server list and are sent again, so an edit survives a server
 * outage, a reload, or a symbol switch inside the debounce window.
 */
export function createDrawingsStore(opts: DrawingsStoreOptions): DrawingsStore {
  const symbol = normalizeDrawingSymbol(opts.symbol)
  const client = opts.client
  const storage = opts.storage === undefined ? defaultStorage() : opts.storage
  const page = opts.page === undefined ? defaultPageTarget() : opts.page
  const debounceMs = opts.debounceMs ?? DRAWING_PUT_DEBOUNCE_MS
  const retryBaseMs = opts.retryBaseMs ?? PUT_RETRY_BASE_MS

  let items: readonly Drawing[] = readMirror(symbol, storage)
  let status: DrawingsSyncStatus = client === null ? 'local-only' : 'loading'
  let started = false
  let disposed = false
  /** adoptMirror kept local edits the stored mirror lacks; written on the next save/pagehide. */
  let mirrorStale = false
  const controller = new AbortController()
  /** Unsynced ids -> version of their latest local change. Only tracked with a client. */
  const dirty = new Map<string, number>()
  let version = 0
  const slots = new Map<string, Slot>()

  const emit = (): void => {
    if (!disposed) opts.onChange(items, status)
  }

  const has = (id: string): boolean => items.some((d) => d.id === id)

  /** A local change to `id` that the server must follow. */
  function touch(id: string): void {
    if (client === null) return
    version += 1
    dirty.delete(id) // keep insertion order = order of last change
    dirty.set(id, version)
    markPending(symbol, id, has(id), storage)
  }

  /** The server holds `sent` for `id`; clear it unless the id changed again since. */
  function acknowledge(id: string, sent: number): void {
    if (dirty.get(id) !== sent) return
    dirty.delete(id)
    dropPending(symbol, id, storage)
  }

  function slotFor(id: string): Slot {
    let s = slots.get(id)
    if (!s) {
      s = {
        timer: null,
        retrying: false,
        inFlight: false,
        sentVersion: -1,
        again: false,
        failures: 0,
      }
      slots.set(id, s)
    }
    return s
  }

  function clearSlotTimer(s: Slot): void {
    if (s.timer !== null) clearTimeout(s.timer)
    s.timer = null
    s.retrying = false
  }

  function markDirty(id: string, delay: number): void {
    if (disposed || status !== 'synced' || client === null) return
    const s = slotFor(id)
    if (s.retrying) return // the backoff retry sends the latest state
    if (s.failures > MAX_PUT_RETRIES) s.failures = 0 // a fresh edit after giving up
    clearSlotTimer(s)
    s.timer = setTimeout(() => flush(id), delay)
  }

  /** PUT or DELETE `id` to match the local state. Never throws synchronously. */
  function send(c: DrawingsClient, id: string, keepalive: boolean): Promise<void> {
    const d = items.find((x) => x.id === id)
    try {
      if (keepalive) return d ? c.put(d, { keepalive: true }) : c.remove(id, { keepalive: true })
      return d ? c.put(d) : c.remove(id)
    } catch (e) {
      return Promise.reject(e instanceof Error ? e : new Error(String(e)))
    }
  }

  /** Fire-and-forget keepalive writes of every unsynced id (page going away). */
  function flushKeepalive(): void {
    if (mirrorStale) {
      writeMirror(symbol, items, storage)
      mirrorStale = false
    }
    if (status !== 'synced' || client === null) return
    for (const [id, v] of dirty) {
      const s = slots.get(id)
      if (s?.inFlight === true && s.sentVersion === v) continue // latest already on its way
      send(client, id, true).catch(() => {
        // Still pending in storage: the next load sends it again.
      })
    }
  }

  function settle(id: string, sent: number, error: unknown): void {
    const s = slots.get(id)
    if (disposed || !s) return
    s.inFlight = false
    const ok = error === undefined
    if (!ok && isPermanentWriteFailure(error) && dirty.get(id) === sent) {
      console.warn(
        `Drawing ${id} was rejected by the server and will not be synced (kept until the next load):`,
        error,
      )
      dirty.delete(id)
      dropPending(symbol, id, storage)
      clearSlotTimer(s)
      slots.delete(id)
      return
    }
    if (ok || isPermanentWriteFailure(error)) {
      // A rejected older version followed by a newer edit: send the newer one normally.
      if (ok) acknowledge(id, sent)
      s.failures = 0
      if (s.again) {
        s.again = false
        flush(id)
      } else if (s.timer === null) {
        slots.delete(id)
      }
      return
    }
    s.failures += 1
    if (s.failures > MAX_PUT_RETRIES) {
      s.again = false
      clearSlotTimer(s)
      return // give up until the next edit (or the next load) of this drawing
    }
    clearSlotTimer(s)
    s.again = false
    s.retrying = true
    s.timer = setTimeout(() => flush(id), retryBaseMs * 2 ** (s.failures - 1))
  }

  function flush(id: string): void {
    const s = slots.get(id)
    if (disposed || !s || client === null) return
    s.timer = null
    s.retrying = false
    if (s.inFlight) {
      s.again = true
      return
    }
    s.inFlight = true
    const sent = dirty.get(id) ?? -1
    s.sentVersion = sent
    send(client, id, false).then(
      () => settle(id, sent, undefined),
      (e: unknown) => settle(id, sent, e ?? new Error('write failed')),
    )
  }

  function setItems(next: readonly Drawing[]): boolean {
    if (next === items) return false
    items = next
    writeMirror(symbol, items, storage)
    mirrorStale = false
    return true
  }

  function onLoaded(server: Drawing[]): void {
    if (disposed || status !== 'loading') return
    // Unsynced ids: edits made while loading plus those persisted by earlier sessions/tabs.
    const persisted = readPending(symbol, storage)
    const candidates = new Set<string>([
      ...dirty.keys(),
      ...persisted.upserts,
      ...persisted.removed,
    ])
    const upserted = new Set<string>()
    const removedIds = new Set<string>()
    for (const id of candidates) (has(id) ? upserted : removedIds).add(id)
    const r = reconcileDrawings(server, items, { upserted, removed: removedIds })
    items = r.items
    writeMirrorIfChanged(symbol, items, storage)
    mirrorStale = false
    status = 'synced'
    const toSend = [...r.puts.map((d) => d.id), ...r.deletes]
    const sending = new Set(toSend)
    for (const id of candidates) {
      if (sending.has(id)) continue
      dirty.delete(id) // nothing to send: the server already matches
      dropPending(symbol, id, storage)
    }
    for (const id of toSend) {
      if (!dirty.has(id)) touch(id)
      else markPending(symbol, id, has(id), storage)
      markDirty(id, 0)
    }
    emit()
  }

  function onLoadFailed(): void {
    if (disposed || status !== 'loading') return
    status = 'local-only'
    emit()
  }

  const onPageHide = (): void => {
    if (!disposed) flushKeepalive()
  }

  return {
    get symbol() {
      return symbol
    },
    get items() {
      return items
    },
    get status() {
      return status
    },
    start() {
      if (started || disposed) return
      started = true
      page?.addEventListener('pagehide', onPageHide)
      if (client === null) return
      let loading: Promise<Drawing[]>
      try {
        loading = client.list(symbol, controller.signal)
      } catch (e) {
        loading = Promise.reject(e instanceof Error ? e : new Error(String(e)))
      }
      loading.then(onLoaded, onLoadFailed)
    },
    dispatch(action) {
      if (disposed) return false
      const before = items
      if (!setItems(drawingsReducer(items, action, symbol))) return false
      // Ids whose server copy must follow the local state.
      let touched: string[]
      switch (action.type) {
        case 'add':
          touched = [action.drawing.id]
          break
        case 'update':
        case 'remove':
          touched = [action.id]
          break
        case 'clear':
          touched = before.map((d) => d.id)
          break
      }
      const delay = action.type === 'update' ? debounceMs : 0
      for (const id of touched) {
        touch(id)
        markDirty(id, delay)
      }
      emit()
      return true
    },
    adoptMirror(raw) {
      if (disposed) return
      const adopted = parseMirror(raw, symbol)
      let next: Drawing[] = adopted
      if (dirty.size > 0) {
        // This tab's unsynced edits stay on top; their scheduled writes still run.
        const mine = new Map(items.map((d) => [d.id, d]))
        next = []
        const placed = new Set<string>()
        for (const d of adopted) {
          placed.add(d.id)
          if (!dirty.has(d.id)) next.push(d)
          else {
            const local = mine.get(d.id)
            if (local) next.push(local) // else removed here: stays removed
          }
        }
        for (const id of dirty.keys()) {
          const local = mine.get(id)
          if (!placed.has(id) && local && next.length < MAX_DRAWINGS_PER_SYMBOL) next.push(local)
        }
      }
      items = next
      // Not written back here (two tabs editing one drawing would echo forever); the next
      // local change, pagehide, or dispose saves it.
      mirrorStale = serializeMirror(next) !== serializeMirror(adopted)
      emit()
    },
    dispose() {
      if (disposed) return
      flushKeepalive()
      disposed = true
      controller.abort()
      page?.removeEventListener('pagehide', onPageHide)
      for (const s of slots.values()) clearSlotTimer(s)
      slots.clear()
    },
  }
}

// ---------------------------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------------------------

interface Snapshot {
  symbol: string
  client: DrawingsClient | null
  items: readonly Drawing[]
  status: DrawingsSyncStatus
}

/**
 * Drawings of `symbol`. On a symbol change the mirror shows at once ('loading'), then the
 * server list replaces it ('synced'), or the mirror stays ('local-only') when the server cannot
 * be reached. `client` null = local-only (direct mode). Pass a stable client (memoized): a new
 * client object restarts the sync. Edits in another tab arrive through the `storage` event.
 */
export function useDrawings(symbol: string, client: DrawingsClient | null): DrawingsApi {
  const sym = normalizeDrawingSymbol(symbol)
  const [snap, setSnap] = useState<Snapshot | null>(null)
  const storeRef = useRef<DrawingsStore | null>(null)

  useEffect(() => {
    const store = createDrawingsStore({
      symbol: sym,
      client,
      onChange: (items, status) => setSnap({ symbol: sym, client, items, status }),
    })
    storeRef.current = store
    store.start()
    return () => {
      store.dispose()
      if (storeRef.current === store) storeRef.current = null
    }
  }, [sym, client])

  useEffect(() => {
    if (typeof window === 'undefined') return
    const key = drawingsStorageKey(sym)
    const onStorage = (e: StorageEvent) => {
      if (e.key !== key) return
      const store = storeRef.current
      if (store !== null && store.symbol === sym) store.adoptMirror(e.newValue)
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [sym])

  // Before this symbol's store has emitted, show its mirror synchronously. A client change
  // re-reads the (possibly edited) mirror, so `client` is a real input of the read.
  const mirror = useMemo(() => readMirrorForClient(sym, client), [sym, client])
  const current = snap !== null && snap.symbol === sym && snap.client === client ? snap : null
  const items = current?.items ?? mirror
  const status: DrawingsSyncStatus = current?.status ?? (client === null ? 'local-only' : 'loading')

  const run = useCallback(
    (action: DrawingsStoreAction): boolean => {
      const store = storeRef.current
      return store !== null && store.symbol === sym ? store.dispatch(action) : false
    },
    [sym],
  )
  const add = useCallback((drawing: Drawing) => run({ type: 'add', drawing }), [run])
  const update = useCallback(
    (id: string, patch: DrawingPatch) => run({ type: 'update', id, patch }),
    [run],
  )
  const remove = useCallback((id: string) => void run({ type: 'remove', id }), [run])
  const clear = useCallback(() => void run({ type: 'clear' }), [run])

  return useMemo(
    () => ({ items, status, add, update, remove, clear }),
    [items, status, add, update, remove, clear],
  )
}
