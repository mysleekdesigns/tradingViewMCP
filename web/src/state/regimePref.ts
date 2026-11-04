// The regime ribbon on/off switch and its source, remembered across visits (localStorage,
// `tv.regime.v1` and `tv.regime.source.v1`).

import type { VenueInfo } from '../data/venue'
import type { RegimeModel } from '../data/labTypes'
import { labOnChart } from './appSettings'
import { storedValue, type ValueStorage } from './storedValue'

export const REGIME_PREF_KEY = 'tv.regime.v1'
export const REGIME_SOURCE_KEY = 'tv.regime.source.v1'

/** The lab's default regime parameters (lab/lab/api.py); the panels show them in their chip. */
export const REGIME_WINDOW_BARS = 20
export const REGIME_THRESHOLD = 0.05

export type RegimeStorage = ValueStorage

/** Saved as JSON `true` / `false`; anything else reads as off. */
export const regimeOnStored = storedValue<boolean>(REGIME_PREF_KEY, {
  parse: (raw) => raw === true,
  fallback: () => false,
})

/** True when the ribbon was on at the last visit; off when never saved or unreadable. */
export function loadRegimeOn(storage?: RegimeStorage | null): boolean {
  return regimeOnStored.load(storage)
}

export function saveRegimeOn(on: boolean, storage?: RegimeStorage | null): void {
  regimeOnStored.save(on, storage)
}

/** Saved as JSON `"markov"` / `"hmm"`; anything else reads as Markov. */
export const regimeSourceStored = storedValue<RegimeModel>(REGIME_SOURCE_KEY, {
  parse: (raw) => (raw === 'hmm' ? 'hmm' : 'markov'),
  fallback: () => 'markov',
})

/**
 * Why the HMM source is off on every chart of this venue (no requests are made), or null. It
 * starts from the lab's Binance global archive, so only charts of that data get it, the same
 * rule as lab backtests. While the venue is unknown (null) it stays off.
 */
export function regimeHmmVenueBlocked(venue: VenueInfo | null): string | null {
  if (labOnChart(venue)) return null
  return venue === null
    ? 'HMM waits for the local server to report its market venue'
    : `HMM needs Binance global data, this chart shows ${venue.name}`
}

/** Chip text, e.g. "Regime 20 · ±5%". */
export function regimeParamsLabel(windowBars: number, threshold: number): string {
  const pct = Math.round(threshold * 1000) / 10
  return `Regime ${windowBars} · ±${pct}%`
}

/** Chip text on the HMM source, e.g. "HMM 2 states"; "HMM" until the lab has answered. */
export function regimeHmmLabel(nStates: number | null): string {
  return nStates === null ? 'HMM' : `HMM ${nStates} states`
}

/** Chip text while the first fit runs. */
export const REGIME_HMM_FITTING_TEXT = 'Fitting HMM, first run can take a minute or two'

/** Chip text when the lab has too few bars, e.g. "HMM needs 3,043 bars, has 1,200". */
export function regimeHmmHistoryLabel(needed: number, have: number): string {
  return `HMM needs ${needed.toLocaleString('en-US')} bars, has ${have.toLocaleString('en-US')}`
}
