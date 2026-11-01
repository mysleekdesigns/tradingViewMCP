import { parseTimeframe } from '../data/timeframes'
import { CHART_TYPES, type ChartType, type Timeframe } from '../types'
import { storedValue, type ValueStorage } from './storedValue'

/** What the app remembers across reloads. */
export interface Prefs {
  symbol: string
  interval: Timeframe
  chartType: ChartType
}

export const PREFS_KEY = 'tv.prefs.v1'

export const DEFAULT_PREFS: Readonly<Prefs> = Object.freeze({
  symbol: 'BTCUSDT',
  interval: '1h',
  chartType: 'candles',
})

/** Minimal storage surface, so tests can pass a fake (or one that throws). */
export type PrefsStorage = ValueStorage

const SYMBOL_RE = /^[A-Z0-9]{2,30}$/

/** A saved timeframe in canonical form ('60s' → '1m'), or null when it is not one. */
function cleanTimeframe(v: unknown): Timeframe | null {
  return typeof v === 'string' ? parseTimeframe(v) : null
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
    interval: cleanTimeframe(r.interval) ?? DEFAULT_PREFS.interval,
    chartType: isChartType(r.chartType) ? r.chartType : DEFAULT_PREFS.chartType,
  }
}

/** `tv.prefs.v1`; saved cleaned (parsePrefs), so an invalid field is written as its default. */
export const prefsStored = storedValue<Prefs>(PREFS_KEY, {
  parse: parsePrefs,
  fallback: () => ({ ...DEFAULT_PREFS }),
  serialize: (prefs) => JSON.stringify(parsePrefs(prefs)),
})

export function loadPrefs(storage?: PrefsStorage | null): Prefs {
  return prefsStored.load(storage)
}

/** Best effort: a full or disabled storage must never break the app. */
export function savePrefs(prefs: Prefs, storage?: PrefsStorage | null): void {
  prefsStored.save(prefs, storage)
}
