import { CHART_TYPES, INTERVALS, type ChartType, type Interval } from '../types'

/** What the app remembers across reloads. */
export interface Prefs {
  symbol: string
  interval: Interval
  chartType: ChartType
}

export const PREFS_KEY = 'tv.prefs.v1'

export const DEFAULT_PREFS: Readonly<Prefs> = Object.freeze({
  symbol: 'BTCUSDT',
  interval: '1h',
  chartType: 'candles',
})

/** Minimal storage surface, so tests can pass a fake (or one that throws). */
export type PrefsStorage = Pick<Storage, 'getItem' | 'setItem'>

function defaultStorage(): PrefsStorage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    // Accessing localStorage itself throws when storage is disabled.
    return null
  }
}

const SYMBOL_RE = /^[A-Z0-9]{2,30}$/

function isInterval(v: unknown): v is Interval {
  return typeof v === 'string' && (INTERVALS as readonly string[]).includes(v)
}

function isChartType(v: unknown): v is ChartType {
  return typeof v === 'string' && (CHART_TYPES as readonly string[]).includes(v)
}

/**
 * Validates an untrusted value field by field. Each bad or missing field falls back to its
 * default on its own, so one stale field does not wipe the others.
 */
export function parsePrefs(raw: unknown): Prefs {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return { ...DEFAULT_PREFS }
  const r = raw as Record<string, unknown>
  const symbol =
    typeof r.symbol === 'string' && SYMBOL_RE.test(r.symbol.trim().toUpperCase())
      ? r.symbol.trim().toUpperCase()
      : DEFAULT_PREFS.symbol
  return {
    symbol,
    interval: isInterval(r.interval) ? r.interval : DEFAULT_PREFS.interval,
    chartType: isChartType(r.chartType) ? r.chartType : DEFAULT_PREFS.chartType,
  }
}

export function loadPrefs(storage: PrefsStorage | null = defaultStorage()): Prefs {
  if (!storage) return { ...DEFAULT_PREFS }
  try {
    const text = storage.getItem(PREFS_KEY)
    if (text === null) return { ...DEFAULT_PREFS }
    return parsePrefs(JSON.parse(text))
  } catch {
    return { ...DEFAULT_PREFS }
  }
}

/** Best effort: a full or disabled storage must never break the app. */
export function savePrefs(prefs: Prefs, storage: PrefsStorage | null = defaultStorage()): void {
  if (!storage) return
  try {
    const clean = parsePrefs(prefs)
    storage.setItem(PREFS_KEY, JSON.stringify(clean))
  } catch {
    // Quota exceeded or storage disabled: keep running without persistence.
  }
}
