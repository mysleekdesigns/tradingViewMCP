// The funding / open-interest panes on/off switch, remembered across visits (localStorage,
// `tv.derivatives.v1`). Mirrors regimePref.ts.

import type { VenueInfo } from '../data/venue'
import { labOnChart } from './appSettings'
import { storedValue, type ValueStorage } from './storedValue'

export const DERIVATIVES_PREF_KEY = 'tv.derivatives.v1'

export type DerivativesStorage = ValueStorage

/** Saved as JSON `true` / `false`; anything else reads as off. */
export const derivativesOnStored = storedValue<boolean>(DERIVATIVES_PREF_KEY, {
  parse: (raw) => raw === true,
  fallback: () => false,
})

/** True when the panes were on at the last visit; off when never saved or unreadable. */
export function loadDerivativesOn(storage?: DerivativesStorage | null): boolean {
  return derivativesOnStored.load(storage)
}

export function saveDerivativesOn(on: boolean, storage?: DerivativesStorage | null): void {
  derivativesOnStored.save(on, storage)
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
