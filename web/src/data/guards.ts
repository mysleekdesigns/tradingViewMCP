import type { Candle } from '../types'
import { MarketDataError } from './provider'

/*
 * Type guards and small validators shared by the providers, stream parsers and API clients
 * (Phase 19 DRY: one copy instead of one per file). All are pure and never throw, except
 * `toFiniteNumber`, which is the strict REST-row field parser.
 */

/** A plain object (not null, not an array). */
export function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

/** A number that is not NaN or ±Infinity (strings are not numbers here). */
export function isFiniteNumber(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

/** A finite number >= 0, else undefined. */
export function nonNegative(v: unknown): number | undefined {
  return isFiniteNumber(v) && v >= 0 ? v : undefined
}

/** A string with at least one non-whitespace character. */
export function nonEmptyString(v: unknown): v is string {
  return typeof v === 'string' && v.trim() !== ''
}

/**
 * Strict numeric field (exchange payloads send prices as strings): a finite number or a
 * non-empty numeric string, else null.
 */
export function finite(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value !== 'string' || value.trim() === '') return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

/** A finite number (numbers only), else null; the lab sends null for NaN/inf. */
export function numberOrNull(v: unknown): number | null {
  return isFiniteNumber(v) ? v : null
}

/** A finite number (numbers only), else `fallback`. */
export function numberOr(v: unknown, fallback: number): number {
  return isFiniteNumber(v) ? v : fallback
}

/** The string, else `fallback`. */
export function stringOr(v: unknown, fallback = ''): string {
  return typeof v === 'string' ? v : fallback
}

/** The string entries of an array (anything else dropped); [] when not an array. */
export function stringsOf(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []
}

/** `parse` applied to every item, nulls dropped. */
export function compact<T>(items: readonly unknown[], parse: (v: unknown) => T | null): T[] {
  const out: T[] = []
  for (const item of items) {
    const parsed = parse(item)
    if (parsed !== null) out.push(parsed)
  }
  return out
}

/** Error wording of one `toFiniteNumber` caller (each provider keeps its own messages). */
export interface FiniteFieldWording {
  /** What the row is, e.g. 'kline' -> "kline open: empty value". */
  prefix: string
  /** Message when the value is neither a string nor a number. */
  wrongType: string
  /** Message when the value does not parse to a finite number. */
  notFinite: string
}

/**
 * One numeric field of a REST row (number or numeric string), else MarketDataError 'parse':
 * `${prefix} ${field}: ${wrongType | 'empty value' | notFinite}`.
 */
export function toFiniteNumber(value: unknown, field: string, wording: FiniteFieldWording): number {
  if (typeof value !== 'string' && typeof value !== 'number') {
    throw new MarketDataError('parse', `${wording.prefix} ${field}: ${wording.wrongType}`)
  }
  if (typeof value === 'string' && value.trim() === '') {
    throw new MarketDataError('parse', `${wording.prefix} ${field}: empty value`)
  }
  const n = Number(value)
  if (!Number.isFinite(n)) {
    throw new MarketDataError('parse', `${wording.prefix} ${field}: ${wording.notFinite}`)
  }
  return n
}

/** Keep one candle per time (the later row wins); input must be sorted ascending. */
export function dedupeByTime(sorted: readonly Candle[]): Candle[] {
  const out: Candle[] = []
  for (const c of sorted) {
    if (out.length > 0 && out[out.length - 1].time === c.time) out[out.length - 1] = c
    else out.push(c)
  }
  return out
}

/** Same time and the same OHLCV values (exact ===). */
export function sameCandle(a: Candle, b: Candle): boolean {
  return (
    a.time === b.time &&
    a.open === b.open &&
    a.high === b.high &&
    a.low === b.low &&
    a.close === b.close &&
    a.volume === b.volume
  )
}
