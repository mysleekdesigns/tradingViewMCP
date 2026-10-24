import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react'
import {
  createPersistenceClient,
  createServerSync,
  DEFAULT_PERSISTENCE_ID,
  type PersistenceClient,
  type ServerSync,
} from '../data/persistence'
import { INDICATOR_BY_ID } from '../indicators/registry'
import { PRICE_SOURCES } from '../indicators/sources'
import type { IndicatorDef, IndicatorId, PriceSource } from '../indicators/types'

export const INDICATORS_STORAGE_KEY = 'tv.indicators.v1'

/** Upper bound on active indicators; also protects against a corrupted or hostile value. */
export const MAX_INDICATORS = 20

/** Default colours for second and later instances of a one-line indicator (EMA 20 + EMA 50). */
export const INSTANCE_PALETTE: readonly string[] = [
  '#2962ff',
  '#f7a21b',
  '#e91e63',
  '#00bcd4',
  '#4caf50',
  '#ab47bc',
]

const UID_RE = /^[a-z0-9-]{1,40}$/
const COLOR_RE = /^#[0-9a-f]{6}$/i

export interface IndicatorConfig {
  /** Unique per instance, stable across reloads. */
  uid: string
  type: IndicatorId
  /** Every InputDef key present, clamped to [min, max], integers rounded. */
  inputs: Record<string, number>
  /** def.source ?? 'close' when the def has no source selector. */
  source: PriceSource
  /** Line key -> '#rrggbb', one per def.lines entry. */
  colors: Record<string, string>
  visible: boolean
}

export type IndicatorPatch = Partial<
  Pick<IndicatorConfig, 'inputs' | 'source' | 'colors' | 'visible'>
>

export interface IndicatorsApi {
  /** Order = legend order and pane order. */
  items: readonly IndicatorConfig[]
  /** Returns the new uid, or null if the type is unknown or MAX_INDICATORS is reached. */
  add(type: IndicatorId): string | null
  remove(uid: string): void
  /** Sanitized through the same rules as parse. */
  update(uid: string, patch: IndicatorPatch): void
  toggleVisible(uid: string): void
}

export type IndicatorsAction =
  | { type: 'add'; def: IndicatorDef; uid: string }
  | { type: 'remove'; uid: string }
  | {
      type: 'update'
      defs: ReadonlyMap<IndicatorId, IndicatorDef>
      uid: string
      patch: IndicatorPatch
    }
  | { type: 'toggle'; uid: string }
  | { type: 'replace'; items: readonly IndicatorConfig[] }

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

/** Every input key of `def`: finite numbers clamped to [min, max] (integers rounded), else default. */
export function sanitizeInputs(def: IndicatorDef, raw: unknown): Record<string, number> {
  const r = isRecord(raw) ? raw : {}
  const out: Record<string, number> = {}
  for (const i of def.inputs) {
    const v = r[i.key]
    if (typeof v !== 'number' || !Number.isFinite(v)) {
      out[i.key] = i.default
      continue
    }
    const n = i.integer ? Math.round(v) : v
    out[i.key] = Math.min(Math.max(n, i.min), i.max)
  }
  return out
}

/** One '#rrggbb' (lower-case) per line of `def`; invalid or missing entries get the default. */
export function sanitizeColors(def: IndicatorDef, raw: unknown): Record<string, string> {
  const r = isRecord(raw) ? raw : {}
  const out: Record<string, string> = {}
  for (const line of def.lines) {
    const v = r[line.key]
    out[line.key] = typeof v === 'string' && COLOR_RE.test(v) ? v.toLowerCase() : line.color
  }
  return out
}

function sanitizeSource(def: IndicatorDef, raw: unknown): PriceSource {
  const fallback = def.source ?? 'close'
  if (def.source === null) return fallback
  return typeof raw === 'string' && (PRICE_SOURCES as readonly string[]).includes(raw)
    ? (raw as PriceSource)
    : fallback
}

function newUid(type: IndicatorId, taken: ReadonlySet<string>): string {
  for (;;) {
    const rand = Math.random().toString(36).slice(2, 8) || '0'
    const uid = `${type}-${Date.now().toString(36)}-${rand}`
    if (!taken.has(uid)) return uid
  }
}

/**
 * A new instance of `def` with default inputs and colours. A second instance of a one-line
 * indicator takes the first INSTANCE_PALETTE colour its siblings do not use. `uid` is used when
 * it is valid and not taken; otherwise a fresh unique uid is generated.
 */
export function createIndicatorConfig(
  def: IndicatorDef,
  existing: readonly IndicatorConfig[],
  uid?: string,
): IndicatorConfig {
  const taken = new Set(existing.map((c) => c.uid))
  const id = uid !== undefined && UID_RE.test(uid) && !taken.has(uid) ? uid : newUid(def.id, taken)
  const colors = sanitizeColors(def, undefined)
  const siblings = existing.filter((c) => c.type === def.id)
  if (def.lines.length === 1 && siblings.length > 0) {
    const key = def.lines[0].key
    const used = new Set(siblings.map((c) => c.colors[key]?.toLowerCase()))
    colors[key] =
      INSTANCE_PALETTE.find((p) => !used.has(p)) ??
      INSTANCE_PALETTE[siblings.length % INSTANCE_PALETTE.length]
  }
  return {
    uid: id,
    type: def.id,
    inputs: sanitizeInputs(def, undefined),
    source: sanitizeSource(def, undefined),
    colors,
    visible: true,
  }
}

/**
 * Validate an untrusted stored value. Entries with an unknown type or a bad / duplicate uid are
 * dropped; every other field falls back to its default on its own. At most MAX_INDICATORS.
 */
export function parseIndicators(
  raw: unknown,
  defs: ReadonlyMap<IndicatorId, IndicatorDef>,
): IndicatorConfig[] {
  if (!Array.isArray(raw)) return []
  const out: IndicatorConfig[] = []
  const seen = new Set<string>()
  for (const entry of raw) {
    if (out.length >= MAX_INDICATORS) break
    if (!isRecord(entry)) continue
    const { uid, type } = entry
    if (typeof type !== 'string') continue
    const def = defs.get(type as IndicatorId)
    if (def === undefined) continue
    if (typeof uid !== 'string' || !UID_RE.test(uid) || seen.has(uid)) continue
    seen.add(uid)
    out.push({
      uid,
      type: def.id,
      inputs: sanitizeInputs(def, entry.inputs),
      source: sanitizeSource(def, entry.source),
      colors: sanitizeColors(def, entry.colors),
      visible: typeof entry.visible === 'boolean' ? entry.visible : true,
    })
  }
  return out
}

/** Apply `patch` to `item`; invalid patch fields leave the current value in place. */
export function applyPatch(
  def: IndicatorDef,
  item: IndicatorConfig,
  patch: IndicatorPatch,
): IndicatorConfig {
  const inputs =
    patch.inputs !== undefined
      ? sanitizeInputs(def, { ...item.inputs, ...validEntries(patch.inputs, 'number') })
      : item.inputs
  const colors =
    patch.colors !== undefined
      ? sanitizeColors(def, { ...item.colors, ...validEntries(patch.colors, 'string') })
      : item.colors
  const source =
    patch.source !== undefined && def.source !== null && PRICE_SOURCES.includes(patch.source)
      ? patch.source
      : item.source
  const visible = typeof patch.visible === 'boolean' ? patch.visible : item.visible
  return { ...item, inputs, colors, source, visible }
}

/** Keep only entries that would pass sanitization, so a bad value does not reset a good one. */
function validEntries(raw: unknown, kind: 'number' | 'string'): Record<string, unknown> {
  if (!isRecord(raw)) return {}
  const out: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(raw)) {
    if (kind === 'number' && typeof v === 'number' && Number.isFinite(v)) out[k] = v
    if (kind === 'string' && typeof v === 'string' && COLOR_RE.test(v)) out[k] = v
  }
  return out
}

export function indicatorsReducer(
  items: readonly IndicatorConfig[],
  action: IndicatorsAction,
): readonly IndicatorConfig[] {
  switch (action.type) {
    case 'add': {
      if (items.length >= MAX_INDICATORS || items.some((c) => c.uid === action.uid)) return items
      if (!UID_RE.test(action.uid)) return items
      return [...items, createIndicatorConfig(action.def, items, action.uid)]
    }
    case 'remove':
      return items.some((c) => c.uid === action.uid)
        ? items.filter((c) => c.uid !== action.uid)
        : items
    case 'update': {
      const i = items.findIndex((c) => c.uid === action.uid)
      const def = i < 0 ? undefined : action.defs.get(items[i].type)
      if (def === undefined) return items
      const next = items.slice()
      next[i] = applyPatch(def, items[i], action.patch)
      return next
    }
    case 'toggle': {
      const i = items.findIndex((c) => c.uid === action.uid)
      if (i < 0) return items
      const next = items.slice()
      next[i] = { ...items[i], visible: !items[i].visible }
      return next
    }
    case 'replace':
      return action.items
  }
}

function defaultStorage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    // Accessing localStorage itself throws when storage is disabled.
    return null
  }
}

/** Stored indicators, or [] when storage is unavailable, empty, or invalid. */
export function loadIndicators(
  defs: ReadonlyMap<IndicatorId, IndicatorDef>,
  storage: Pick<Storage, 'getItem'> | null = defaultStorage(),
): IndicatorConfig[] {
  if (!storage) return []
  try {
    const text = storage.getItem(INDICATORS_STORAGE_KEY)
    if (text === null) return []
    return parseIndicators(JSON.parse(text), defs)
  } catch {
    return []
  }
}

/** Best effort: a full or disabled storage must never break the app. */
export function saveIndicators(
  items: readonly IndicatorConfig[],
  storage: Pick<Storage, 'setItem'> | null = defaultStorage(),
): void {
  if (!storage) return
  try {
    storage.setItem(INDICATORS_STORAGE_KEY, JSON.stringify(items))
  } catch {
    // Quota exceeded or storage disabled: indicators still work for this session.
  }
}

/**
 * The indicators another tab wrote, from a `storage` event's key and newValue; null when the
 * event is for another key, the key was removed, or the value is not valid JSON. Parsed with
 * the same rules as loadIndicators.
 */
export function indicatorsFromStorageEvent(
  key: string | null,
  newValue: string | null,
  defs: ReadonlyMap<IndicatorId, IndicatorDef>,
): IndicatorConfig[] | null {
  if (key !== INDICATORS_STORAGE_KEY || newValue === null) return null
  try {
    return parseIndicators(JSON.parse(newValue), defs)
  } catch {
    return null
  }
}

/**
 * Listen for another tab's indicator writes (`storage` events on `target`, normally `window`)
 * and pass each valid list to `onAdopt`. Returns the unsubscribe function.
 */
export function listenIndicatorsStorage(
  target: Pick<EventTarget, 'addEventListener' | 'removeEventListener'>,
  getDefs: () => ReadonlyMap<IndicatorId, IndicatorDef>,
  onAdopt: (items: IndicatorConfig[]) => void,
): () => void {
  const onStorage = (e: Event): void => {
    const ev = e as Partial<StorageEvent>
    const next = indicatorsFromStorageEvent(ev.key ?? null, ev.newValue ?? null, getDefs())
    if (next !== null) onAdopt(next)
  }
  target.addEventListener('storage', onStorage)
  return () => target.removeEventListener('storage', onStorage)
}

/**
 * Parse the server layout document `{ indicators: IndicatorConfig[] }`. null when the document
 * has no `indicators` array; entries are sanitized like localStorage values.
 */
export function parseIndicatorLayout(
  data: unknown,
  defs: ReadonlyMap<IndicatorId, IndicatorDef>,
): IndicatorConfig[] | null {
  if (!isRecord(data) || !Array.isArray(data.indicators)) return null
  return parseIndicators(data.indicators, defs)
}

/**
 * Sync for the single indicator layout with the local server (`/api/layouts/default`, data
 * `{ indicators }`). Server copy wins once its GET returns; 404 migrates the local list up.
 */
export function createIndicatorsSync(
  initial: readonly IndicatorConfig[],
  defs: ReadonlyMap<IndicatorId, IndicatorDef>,
  onRemote: (items: IndicatorConfig[]) => void,
  client: PersistenceClient = createPersistenceClient(),
  debounceMs?: number,
): ServerSync<readonly IndicatorConfig[]> {
  return createServerSync<readonly IndicatorConfig[]>({
    initial,
    load: async () => (await client.getLayout(DEFAULT_PERSISTENCE_ID))?.data ?? null,
    save: (items) => client.putLayout(DEFAULT_PERSISTENCE_ID, { indicators: items }),
    parse: (raw) => parseIndicatorLayout(raw, defs),
    onRemote: (items) => onRemote([...items]),
    debounceMs,
  })
}

/**
 * The active indicators of the (single, for now) layout. localStorage ('tv.indicators.v1') is
 * the synchronous initial value and offline cache; the local server's SQLite layout is the
 * source of truth when reachable. Edits in another tab are picked up through the `storage` event.
 */
export function useIndicators(
  defs: ReadonlyMap<IndicatorId, IndicatorDef> = INDICATOR_BY_ID,
): IndicatorsApi {
  const [items, dispatch] = useReducer(indicatorsReducer, undefined, () => loadIndicators(defs))
  const itemsRef = useRef<readonly IndicatorConfig[]>(items)
  const defsRef = useRef(defs)
  const syncRef = useRef<ServerSync<readonly IndicatorConfig[]> | null>(null)

  useEffect(() => {
    const sync = createIndicatorsSync(itemsRef.current, defsRef.current, (next) => {
      itemsRef.current = next
      dispatch({ type: 'replace', items: next })
    })
    syncRef.current = sync
    sync.start()
    return () => {
      sync.dispose()
      if (syncRef.current === sync) syncRef.current = null
    }
  }, [])

  useEffect(() => {
    itemsRef.current = items
    saveIndicators(items)
    syncRef.current?.update(items)
  }, [items])

  useEffect(() => {
    defsRef.current = defs
  }, [defs])

  useEffect(() => {
    if (typeof window === 'undefined') return
    return listenIndicatorsStorage(
      window,
      () => defsRef.current,
      (next) => {
        // The other tab saves this value to the server itself: record it as the server copy so
        // this tab neither echoes it nor later PUTs its own stale layout over it.
        itemsRef.current = next
        syncRef.current?.adopt(next)
        dispatch({ type: 'replace', items: next })
      },
    )
  }, [])

  const add = useCallback((type: IndicatorId): string | null => {
    const def = defsRef.current.get(type)
    const current = itemsRef.current
    if (def === undefined || current.length >= MAX_INDICATORS) return null
    // The uid is made here, not in the reducer, so a StrictMode double-run stays deterministic.
    const action: IndicatorsAction = {
      type: 'add',
      def,
      uid: createIndicatorConfig(def, current).uid,
    }
    // Track the result now so a second add in the same tick sees this one (cap, colours, uid).
    itemsRef.current = indicatorsReducer(current, action)
    dispatch(action)
    return action.uid
  }, [])

  const remove = useCallback((uid: string) => dispatch({ type: 'remove', uid }), [])

  const update = useCallback(
    (uid: string, patch: IndicatorPatch) =>
      dispatch({ type: 'update', defs: defsRef.current, uid, patch }),
    [],
  )

  const toggleVisible = useCallback((uid: string) => dispatch({ type: 'toggle', uid }), [])

  return useMemo(
    () => ({ items, add, remove, update, toggleVisible }),
    [items, add, remove, update, toggleVisible],
  )
}
