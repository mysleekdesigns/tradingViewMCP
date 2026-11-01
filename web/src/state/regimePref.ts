// The regime ribbon on/off switch, remembered across visits (localStorage, `tv.regime.v1`).

import { storedValue, type ValueStorage } from './storedValue'

export const REGIME_PREF_KEY = 'tv.regime.v1'

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

/** Chip text, e.g. "Regime 20 · ±5%". */
export function regimeParamsLabel(windowBars: number, threshold: number): string {
  const pct = Math.round(threshold * 1000) / 10
  return `Regime ${windowBars} · ±${pct}%`
}
