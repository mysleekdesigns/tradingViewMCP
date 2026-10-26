import { parseTimeframe } from '../data/timeframes'
import { CHART_TYPES, type ChartType, type Timeframe } from '../types'
import { DEFAULT_PREFS, loadPrefs, type Prefs } from './prefs'

/**
 * Multi-chart layout: one, two (side by side) or four (2x2) chart panels. Each panel has its
 * own symbol, interval and chart type; one panel is active and takes the toolbar, the symbol
 * search, the watchlist and the keyboard. All four panels always exist, so shrinking the layout
 * and growing it again brings the hidden panels back as they were.
 */

export type LayoutKind = '1' | '2' | '4'

export const LAYOUT_KINDS: readonly LayoutKind[] = ['1', '2', '4']

export const MAX_PANELS = 4

export const LAYOUT_KEY = 'tv.layout.v1'

export interface PanelState {
  /** Stable per slot ('p0'..'p3'): React key, crosshair-sync id, live-data key. */
  id: string
  symbol: string
  interval: Timeframe
  chartType: ChartType
}

export interface LayoutState {
  kind: LayoutKind
  /** Always MAX_PANELS long; only the first `panelCount(kind)` are shown. */
  panels: readonly PanelState[]
  /** Index of the active panel, always < panelCount(kind). */
  active: number
  /** A symbol change on the active panel applies to every panel (intervals stay per panel). */
  syncSymbol: boolean
  /** Hovering one chart moves the crosshair to the same time on the others. */
  syncCrosshair: boolean
}

export type LayoutAction =
  | { type: 'set-layout'; kind: LayoutKind }
  | { type: 'set-active'; index: number }
  | { type: 'set-symbol'; index: number; symbol: string }
  | { type: 'set-interval'; index: number; interval: Timeframe }
  | { type: 'set-chart-type'; index: number; chartType: ChartType }
  | { type: 'set-sync-symbol'; on: boolean }
  | { type: 'set-sync-crosshair'; on: boolean }
  /** Every panel showing `from` switches to `to` (e.g. a symbol the provider does not list). */
  | { type: 'replace-symbol'; from: string; to: string }

const PANEL_IDS = ['p0', 'p1', 'p2', 'p3'] as const

/** Intervals the extra panels start on, so a new 2/4 layout is useful at once. */
const EXTRA_INTERVALS: readonly Timeframe[] = ['1h', '4h', '1d', '15m']

const SYMBOL_RE = /^[A-Z0-9]{2,30}$/

export function panelCount(kind: LayoutKind): number {
  return kind === '1' ? 1 : kind === '2' ? 2 : 4
}

export function visiblePanels(state: LayoutState): readonly PanelState[] {
  return state.panels.slice(0, panelCount(state.kind))
}

export function activePanel(state: LayoutState): PanelState {
  return state.panels[state.active]
}

/**
 * Starting layout: one chart from the saved prefs (`tv.prefs.v1`, the single-chart app's
 * state), extra panels on the same symbol and chart type at other intervals.
 */
export function defaultLayout(prefs: Prefs = DEFAULT_PREFS): LayoutState {
  return {
    kind: '1',
    panels: PANEL_IDS.map((_, i) => defaultPanel(i, prefs)),
    active: 0,
    syncSymbol: false,
    syncCrosshair: true,
  }
}

function defaultPanel(index: number, prefs: Prefs): PanelState {
  return {
    id: PANEL_IDS[index],
    symbol: prefs.symbol,
    interval: index === 0 ? prefs.interval : pickExtraInterval(index, prefs.interval),
    chartType: prefs.chartType,
  }
}

/** The index-th extra interval, skipping the first panel's own interval. */
function pickExtraInterval(index: number, first: Timeframe): Timeframe {
  const options = EXTRA_INTERVALS.filter((iv) => iv !== first)
  return options[(index - 1) % options.length]
}

/** A saved timeframe in canonical form ('60s' → '1m'), or null when it is not one. */
function cleanTimeframe(v: unknown): Timeframe | null {
  return typeof v === 'string' ? parseTimeframe(v) : null
}

function isChartType(v: unknown): v is ChartType {
  return typeof v === 'string' && (CHART_TYPES as readonly string[]).includes(v)
}

function isLayoutKind(v: unknown): v is LayoutKind {
  return typeof v === 'string' && (LAYOUT_KINDS as readonly string[]).includes(v)
}

function cleanSymbol(v: unknown): string | null {
  if (typeof v !== 'string') return null
  const s = v.trim().toUpperCase()
  return SYMBOL_RE.test(s) ? s : null
}

/**
 * Validates an untrusted value field by field: a bad field falls back to its default on its
 * own, so one stale field does not reset the whole layout. Panel 0's defaults come from
 * `prefs`, so a user upgrading from the single-chart app keeps their chart.
 */
export function parseLayout(raw: unknown, prefs: Prefs = DEFAULT_PREFS): LayoutState {
  const base = defaultLayout(prefs)
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return base
  const r = raw as Record<string, unknown>
  const rawPanels = Array.isArray(r.panels) ? (r.panels as unknown[]) : []
  const panels = base.panels.map((def, i): PanelState => {
    const p = rawPanels[i]
    if (typeof p !== 'object' || p === null || Array.isArray(p)) return def
    const q = p as Record<string, unknown>
    return {
      id: def.id,
      symbol: cleanSymbol(q.symbol) ?? def.symbol,
      interval: cleanTimeframe(q.interval) ?? def.interval,
      chartType: isChartType(q.chartType) ? q.chartType : def.chartType,
    }
  })
  const kind = isLayoutKind(r.kind) ? r.kind : base.kind
  const active =
    typeof r.active === 'number' &&
    Number.isInteger(r.active) &&
    r.active >= 0 &&
    r.active < panelCount(kind)
      ? r.active
      : 0
  const syncSymbol = typeof r.syncSymbol === 'boolean' ? r.syncSymbol : base.syncSymbol
  const syncCrosshair = typeof r.syncCrosshair === 'boolean' ? r.syncCrosshair : base.syncCrosshair
  const state: LayoutState = { kind, panels, active, syncSymbol, syncCrosshair }
  // Symbol sync means one symbol everywhere; an edited file cannot break that.
  return syncSymbol ? withSymbolEverywhere(state, panels[active].symbol) : state
}

/** Minimal storage surface, so tests can pass a fake (or one that throws). */
export type LayoutStorage = Pick<Storage, 'getItem' | 'setItem'>

function defaultStorage(): LayoutStorage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    // Accessing localStorage itself throws when storage is disabled.
    return null
  }
}

/**
 * The saved layout, or (first run with layouts) one chart from the saved prefs. Never throws.
 */
export function loadLayout(
  storage: LayoutStorage | null = defaultStorage(),
  prefs: Prefs = loadPrefs(storage),
): LayoutState {
  if (!storage) return defaultLayout(prefs)
  try {
    const text = storage.getItem(LAYOUT_KEY)
    if (text === null) return defaultLayout(prefs)
    return parseLayout(JSON.parse(text), prefs)
  } catch {
    return defaultLayout(prefs)
  }
}

/** Best effort: a full or disabled storage must never break the app. */
export function saveLayout(
  state: LayoutState,
  storage: LayoutStorage | null = defaultStorage(),
): void {
  if (!storage) return
  try {
    const { kind, active, syncSymbol, syncCrosshair } = state
    const panels = state.panels.map(({ symbol, interval, chartType }) => ({
      symbol,
      interval,
      chartType,
    }))
    storage.setItem(LAYOUT_KEY, JSON.stringify({ kind, panels, active, syncSymbol, syncCrosshair }))
  } catch {
    // Quota exceeded or storage disabled: keep running without persistence.
  }
}

function withSymbolEverywhere(state: LayoutState, symbol: string): LayoutState {
  if (state.panels.every((p) => p.symbol === symbol)) return state
  return { ...state, panels: state.panels.map((p) => (p.symbol === symbol ? p : { ...p, symbol })) }
}

function patchPanel(state: LayoutState, index: number, patch: Partial<PanelState>): LayoutState {
  const panel = state.panels[index]
  if (panel === undefined) return state
  const keys = Object.keys(patch) as (keyof PanelState)[]
  if (keys.every((k) => panel[k] === patch[k])) return state
  return {
    ...state,
    panels: state.panels.map((p, i) => (i === index ? { ...p, ...patch } : p)),
  }
}

/**
 * Pure transitions. An action that changes nothing returns `state` itself, so React can skip
 * the re-render (and the save).
 */
export function layoutReducer(state: LayoutState, action: LayoutAction): LayoutState {
  switch (action.type) {
    case 'set-layout': {
      if (action.kind === state.kind) return state
      // A panel that is no longer shown cannot stay active; the first panel takes over.
      const active = state.active < panelCount(action.kind) ? state.active : 0
      return { ...state, kind: action.kind, active }
    }
    case 'set-active': {
      const i = action.index
      if (i === state.active || !Number.isInteger(i) || i < 0 || i >= panelCount(state.kind))
        return state
      return { ...state, active: i }
    }
    case 'set-symbol': {
      const symbol = cleanSymbol(action.symbol)
      if (symbol === null) return state
      if (state.syncSymbol) return withSymbolEverywhere(state, symbol)
      return patchPanel(state, action.index, { symbol })
    }
    case 'set-interval': {
      const interval = cleanTimeframe(action.interval)
      return interval === null ? state : patchPanel(state, action.index, { interval })
    }
    case 'set-chart-type':
      return patchPanel(state, action.index, { chartType: action.chartType })
    case 'set-sync-symbol': {
      if (action.on === state.syncSymbol) return state
      const next = { ...state, syncSymbol: action.on }
      // Turning sync on aligns every panel to the active one.
      return action.on ? withSymbolEverywhere(next, activePanel(state).symbol) : next
    }
    case 'set-sync-crosshair':
      return action.on === state.syncCrosshair ? state : { ...state, syncCrosshair: action.on }
    case 'replace-symbol': {
      const to = cleanSymbol(action.to)
      if (to === null || action.from === to || !state.panels.some((p) => p.symbol === action.from))
        return state
      return {
        ...state,
        panels: state.panels.map((p) => (p.symbol === action.from ? { ...p, symbol: to } : p)),
      }
    }
  }
}

/**
 * Panels on a timeframe the current provider cannot serve (e.g. '5s' saved while on Coinbase)
 * move to `fallback`. Returns `state` itself when every panel is supported.
 */
export function sanitizeTimeframes(
  state: LayoutState,
  supports: (tf: Timeframe) => boolean,
  fallback: Timeframe,
): LayoutState {
  if (state.panels.every((p) => supports(p.interval))) return state
  return {
    ...state,
    panels: state.panels.map((p) => (supports(p.interval) ? p : { ...p, interval: fallback })),
  }
}

/**
 * The visible panel a market-specific overlay (a backtest, a focus request) belongs to: the
 * active panel when it matches, else the first visible match, else -1.
 */
export function pickPanel(state: LayoutState, matches: (panel: PanelState) => boolean): number {
  const n = panelCount(state.kind)
  if (state.active < n && matches(state.panels[state.active])) return state.active
  for (let i = 0; i < n; i++) if (matches(state.panels[i])) return i
  return -1
}

/**
 * Symbols for a fixed number of drawings-store slots (hooks cannot be called in a loop of
 * varying length). Each needed symbol gets exactly one slot, so two panels on one symbol share
 * one store; a slot keeps its symbol while it is still needed, so switching one panel does not
 * restart the others' stores. Unused slots are ''. Returns `prev` itself when nothing changes.
 */
export function assignSlots(
  prev: readonly string[],
  needed: readonly string[],
  size: number = MAX_PANELS,
): readonly string[] {
  const want = [...new Set(needed)].slice(0, size)
  const next: string[] = Array.from({ length: size }, (_, i) => {
    const s = prev[i] ?? ''
    return s !== '' && want.includes(s) ? s : ''
  })
  for (const s of want) {
    if (next.includes(s)) continue
    const free = next.indexOf('')
    if (free >= 0) next[free] = s
  }
  const same = prev.length === size && next.every((s, i) => s === prev[i])
  return same ? prev : next
}
