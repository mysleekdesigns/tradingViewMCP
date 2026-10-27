// The funding / open-interest panes on/off switch, remembered across visits (localStorage,
// `tv.derivatives.v1`). Mirrors regimePref.ts.

import type { VenueInfo } from '../data/venue'
import { labOnChart } from './appSettings'

export const DERIVATIVES_PREF_KEY = 'tv.derivatives.v1'

export type DerivativesStorage = Pick<Storage, 'getItem' | 'setItem'>

function defaultStorage(): DerivativesStorage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    return null
  }
}

/** True when the panes were on at the last visit; off when never saved or unreadable. */
export function loadDerivativesOn(storage: DerivativesStorage | null = defaultStorage()): boolean {
  if (!storage) return false
  try {
    const raw = storage.getItem(DERIVATIVES_PREF_KEY)
    return raw !== null && (JSON.parse(raw) as unknown) === true
  } catch {
    return false
  }
}

export function saveDerivativesOn(
  on: boolean,
  storage: DerivativesStorage | null = defaultStorage(),
): void {
  if (!storage) return
  try {
    storage.setItem(DERIVATIVES_PREF_KEY, JSON.stringify(on))
  } catch {
    // Quota exceeded or storage disabled: keep it in memory only.
  }
}

/**
 * Why the panes are off for every chart of this venue (no requests are made), or null when they
 * may be drawn. The lab's futures archive is Binance's (USDⓈ-M, keyed by Binance symbols), so
 * only charts of Binance global spot data get it, the same rule as lab backtests (`labOnChart`).
 */
export function derivativesVenueBlocked(venue: VenueInfo | null): string | null {
  if (labOnChart(venue)) return null
  if (venue === null) return 'Funding / OI: waiting for the market venue'
  return `Funding / OI: Binance charts only (this chart shows ${venue.name})`
}
