import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react'
import {
  createPersistenceClient,
  createServerSync,
  DEFAULT_PERSISTENCE_ID,
  type PersistenceClient,
  type ServerSync,
} from '../data/persistence'

/** Symbols shown on first run (and whenever the stored list is missing or invalid). */
export const DEFAULT_WATCHLIST: readonly string[] = [
  'BTCUSDT',
  'ETHUSDT',
  'SOLUSDT',
  'BNBUSDT',
  'XRPUSDT',
  'DOGEUSDT',
  'ADAUSDT',
]

export const WATCHLIST_STORAGE_KEY = 'tv.watchlist.v1'

/** Upper bound on stored entries; protects against a corrupted or hostile value. */
export const MAX_WATCHLIST_ITEMS = 200

const SYMBOL_RE = /^[A-Z0-9][A-Z0-9._-]{0,39}$/

export interface WatchlistApi {
  items: readonly string[]
  add(symbol: string): void
  remove(symbol: string): void
  move(from: number, to: number): void
  has(symbol: string): boolean
}

export type WatchlistAction =
  | { type: 'add'; symbol: string }
  | { type: 'remove'; symbol: string }
  | { type: 'move'; from: number; to: number }
  | { type: 'replace'; items: readonly string[] }

/** Trimmed, upper-cased symbol, or null when it is not a plausible exchange symbol. */
export function normalizeSymbol(symbol: string): string | null {
  const s = symbol.trim().toUpperCase()
  return SYMBOL_RE.test(s) ? s : null
}

/** Append `symbol` unless already present (case-insensitive). Returns `items` when unchanged. */
export function addSymbol(items: readonly string[], symbol: string): readonly string[] {
  const s = normalizeSymbol(symbol)
  if (s === null || items.includes(s) || items.length >= MAX_WATCHLIST_ITEMS) return items
  return [...items, s]
}

/** Drop `symbol`. Returns `items` when it was not there. */
export function removeSymbol(items: readonly string[], symbol: string): readonly string[] {
  const s = normalizeSymbol(symbol)
  if (s === null || !items.includes(s)) return items
  return items.filter((x) => x !== s)
}

/**
 * Move the entry at `from` to index `to` (the index it has after the move). `to` is clamped
 * into range; a `from` outside the list, a non-integer index, or a no-op returns `items`.
 */
export function moveItem(items: readonly string[], from: number, to: number): readonly string[] {
  if (!Number.isInteger(from) || !Number.isInteger(to)) return items
  if (from < 0 || from >= items.length) return items
  const target = Math.min(Math.max(to, 0), items.length - 1)
  if (target === from) return items
  const next = items.slice()
  const [moved] = next.splice(from, 1)
  next.splice(target, 0, moved)
  return next
}

export function watchlistReducer(
  items: readonly string[],
  action: WatchlistAction,
): readonly string[] {
  switch (action.type) {
    case 'add':
      return addSymbol(items, action.symbol)
    case 'remove':
      return removeSymbol(items, action.symbol)
    case 'move':
      return moveItem(items, action.from, action.to)
    case 'replace':
      return sameItems(items, action.items) ? items : action.items
  }
}

function sameItems(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((x, i) => x === b[i])
}

/**
 * Parse a stored watchlist. Valid means: JSON array of unique, non-empty, already upper-case
 * symbol strings, at most MAX_WATCHLIST_ITEMS long. An empty array is valid (the user removed
 * everything). Anything else returns null so the caller falls back to the default.
 */
export function parseWatchlist(raw: string | null): string[] | null {
  if (raw === null) return null
  let value: unknown
  try {
    value = JSON.parse(raw)
  } catch {
    return null
  }
  return validateWatchlist(value)
}

/** Same rules as parseWatchlist, for an already-decoded value (e.g. a server response). */
export function validateWatchlist(value: unknown): string[] | null {
  if (!Array.isArray(value) || value.length > MAX_WATCHLIST_ITEMS) return null
  const seen = new Set<string>()
  for (const v of value) {
    if (typeof v !== 'string' || normalizeSymbol(v) !== v || seen.has(v)) return null
    seen.add(v)
  }
  return [...seen]
}

export function serializeWatchlist(items: readonly string[]): string {
  return JSON.stringify(items)
}

/** Minimal storage surface, so tests can pass a fake. */
export interface KeyValueStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
}

function defaultStorage(): KeyValueStorage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    return null
  }
}

/** Stored list, or the default when storage is unavailable, empty, or invalid. */
export function loadWatchlist(
  storage: KeyValueStorage | null = defaultStorage(),
): readonly string[] {
  if (storage === null) return DEFAULT_WATCHLIST
  try {
    return parseWatchlist(storage.getItem(WATCHLIST_STORAGE_KEY)) ?? DEFAULT_WATCHLIST
  } catch {
    return DEFAULT_WATCHLIST
  }
}

/** Persist the list; failures (private mode, quota) are ignored. */
export function saveWatchlist(
  items: readonly string[],
  storage: KeyValueStorage | null = defaultStorage(),
): void {
  if (storage === null) return
  try {
    storage.setItem(WATCHLIST_STORAGE_KEY, serializeWatchlist(items))
  } catch {
    // Storage full or blocked: the list still works for this session.
  }
}

/**
 * Sync for the single watchlist with the local server (`/api/watchlists/default`). The server
 * copy wins once its GET returns; a 404 migrates the local list up; no server = local-only.
 */
export function createWatchlistSync(
  initial: readonly string[],
  onRemote: (items: string[]) => void,
  client: PersistenceClient = createPersistenceClient(),
  debounceMs?: number,
): ServerSync<readonly string[]> {
  return createServerSync<readonly string[]>({
    initial,
    load: async () => (await client.getWatchlist(DEFAULT_PERSISTENCE_ID))?.items ?? null,
    save: (items) => client.putWatchlist(DEFAULT_PERSISTENCE_ID, items),
    parse: validateWatchlist,
    onRemote: (items) => onRemote([...items]),
    debounceMs,
  })
}

/**
 * The user's watchlist. localStorage ('tv.watchlist.v1') is the synchronous initial value and
 * offline cache; the local server's SQLite copy is the source of truth when it is reachable.
 * Edits in another tab are picked up through the `storage` event.
 */
export function useWatchlist(): WatchlistApi {
  const [items, dispatch] = useReducer(watchlistReducer, undefined, () => loadWatchlist())
  const itemsRef = useRef(items)
  const syncRef = useRef<ServerSync<readonly string[]> | null>(null)

  useEffect(() => {
    const sync = createWatchlistSync(itemsRef.current, (next) =>
      dispatch({ type: 'replace', items: next }),
    )
    syncRef.current = sync
    sync.start()
    return () => {
      sync.dispose()
      if (syncRef.current === sync) syncRef.current = null
    }
  }, [])

  useEffect(() => {
    itemsRef.current = items
    saveWatchlist(items)
    syncRef.current?.update(items)
  }, [items])

  useEffect(() => {
    if (typeof window === 'undefined') return
    const onStorage = (e: StorageEvent) => {
      if (e.key !== WATCHLIST_STORAGE_KEY) return
      const next = parseWatchlist(e.newValue)
      if (next === null) return
      // Another tab already saved this value; adopt it so this tab neither echoes it nor
      // later PUTs its own stale copy over it.
      syncRef.current?.adopt(next)
      dispatch({ type: 'replace', items: next })
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  const add = useCallback((symbol: string) => dispatch({ type: 'add', symbol }), [])
  const remove = useCallback((symbol: string) => dispatch({ type: 'remove', symbol }), [])
  const move = useCallback((from: number, to: number) => dispatch({ type: 'move', from, to }), [])

  return useMemo(() => {
    const set = new Set(items)
    return {
      items,
      add,
      remove,
      move,
      has: (symbol: string) => {
        const s = normalizeSymbol(symbol)
        return s !== null && set.has(s)
      },
    }
  }, [items, add, remove, move])
}
