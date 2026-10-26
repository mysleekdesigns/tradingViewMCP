// The regime ribbon on/off switch, remembered across visits (localStorage, `tv.regime.v1`).

export const REGIME_PREF_KEY = 'tv.regime.v1'

/** The lab's default regime parameters (lab/lab/api.py); the panels show them in their chip. */
export const REGIME_WINDOW_BARS = 20
export const REGIME_THRESHOLD = 0.05

export type RegimeStorage = Pick<Storage, 'getItem' | 'setItem'>

function defaultStorage(): RegimeStorage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    return null
  }
}

/** True when the ribbon was on at the last visit; off when never saved or unreadable. */
export function loadRegimeOn(storage: RegimeStorage | null = defaultStorage()): boolean {
  if (!storage) return false
  try {
    const raw = storage.getItem(REGIME_PREF_KEY)
    return raw !== null && (JSON.parse(raw) as unknown) === true
  } catch {
    return false
  }
}

export function saveRegimeOn(on: boolean, storage: RegimeStorage | null = defaultStorage()): void {
  if (!storage) return
  try {
    storage.setItem(REGIME_PREF_KEY, JSON.stringify(on))
  } catch {
    // Quota exceeded or storage disabled: keep it in memory only.
  }
}

/** Chip text, e.g. "Regime 20 · ±5%". */
export function regimeParamsLabel(windowBars: number, threshold: number): string {
  const pct = Math.round(threshold * 1000) / 10
  return `Regime ${windowBars} · ±${pct}%`
}
