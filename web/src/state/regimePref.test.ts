import { describe, expect, it } from 'vitest'
import {
  loadRegimeOn,
  REGIME_PREF_KEY,
  regimeParamsLabel,
  saveRegimeOn,
  type RegimeStorage,
} from './regimePref'

function memory(init: Record<string, string> = {}): RegimeStorage & { data: Map<string, string> } {
  const data = new Map(Object.entries(init))
  return { data, getItem: (k) => data.get(k) ?? null, setItem: (k, v) => void data.set(k, v) }
}

const throwing: RegimeStorage = {
  getItem: () => {
    throw new Error('denied')
  },
  setItem: () => {
    throw new Error('denied')
  },
}

describe('regime pref', () => {
  it('round-trips the switch', () => {
    const s = memory()
    expect(loadRegimeOn(s)).toBe(false)
    saveRegimeOn(true, s)
    expect(s.data.get(REGIME_PREF_KEY)).toBe('true')
    expect(loadRegimeOn(s)).toBe(true)
    saveRegimeOn(false, s)
    expect(loadRegimeOn(s)).toBe(false)
  })

  it('reads junk, missing storage and throwing storage as off', () => {
    expect(loadRegimeOn(memory({ [REGIME_PREF_KEY]: '{oops' }))).toBe(false)
    expect(loadRegimeOn(memory({ [REGIME_PREF_KEY]: '"yes"' }))).toBe(false)
    expect(loadRegimeOn(null)).toBe(false)
    expect(loadRegimeOn(throwing)).toBe(false)
    expect(() => saveRegimeOn(true, throwing)).not.toThrow()
    expect(() => saveRegimeOn(true, null)).not.toThrow()
  })

  it('labels the parameters', () => {
    expect(regimeParamsLabel(20, 0.05)).toBe('Regime 20 · ±5%')
    expect(regimeParamsLabel(50, 0.025)).toBe('Regime 50 · ±2.5%')
  })
})
