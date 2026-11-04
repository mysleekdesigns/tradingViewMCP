import { describe, expect, it } from 'vitest'
import {
  loadRegimeOn,
  REGIME_HMM_FITTING_TEXT,
  REGIME_PREF_KEY,
  REGIME_SOURCE_KEY,
  regimeHmmHistoryLabel,
  regimeHmmLabel,
  regimeHmmVenueBlocked,
  regimeParamsLabel,
  regimeSourceStored,
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

describe('regime source pref', () => {
  it('defaults to Markov and round-trips the choice under its own key', () => {
    const s = memory()
    expect(regimeSourceStored.load(s)).toBe('markov')
    regimeSourceStored.save('hmm', s)
    expect(REGIME_SOURCE_KEY).toBe('tv.regime.source.v1')
    expect(s.data.get(REGIME_SOURCE_KEY)).toBe('"hmm"')
    expect(regimeSourceStored.load(s)).toBe('hmm')
  })

  it('reads anything unknown as Markov and survives a throwing storage', () => {
    for (const raw of ['"garch"', 'true', 'null', '{', '1']) {
      expect(regimeSourceStored.load(memory({ [REGIME_SOURCE_KEY]: raw }))).toBe('markov')
    }
    expect(regimeSourceStored.load(throwing)).toBe('markov')
    expect(() => regimeSourceStored.save('hmm', throwing)).not.toThrow()
  })

  it('leaves the on/off switch and its stored format alone', () => {
    const s = memory({ [REGIME_PREF_KEY]: 'true' })
    regimeSourceStored.save('hmm', s)
    expect(s.data.get(REGIME_PREF_KEY)).toBe('true')
    expect(loadRegimeOn(s)).toBe(true)
  })
})

describe('regimeHmmVenueBlocked', () => {
  it('lets only Binance global data through, like lab backtests', () => {
    expect(regimeHmmVenueBlocked({ id: 'binance', name: 'Binance' })).toBeNull()
  })

  it('names the venue that cannot use the archive', () => {
    expect(regimeHmmVenueBlocked({ id: 'binanceus', name: 'Binance.US' })).toMatch(/Binance\.US/)
    expect(regimeHmmVenueBlocked({ id: 'coinbase', name: 'Coinbase' })).toMatch(/Coinbase/)
  })

  it('stays off while the venue is unknown', () => {
    expect(regimeHmmVenueBlocked(null)).toMatch(/venue/)
  })
})

describe('HMM chip text', () => {
  it('names the states once known', () => {
    expect(regimeHmmLabel(null)).toBe('HMM')
    expect(regimeHmmLabel(2)).toBe('HMM 2 states')
  })

  it('says what the first fit costs and how many bars are missing', () => {
    expect(REGIME_HMM_FITTING_TEXT).toBe('Fitting HMM, first run can take a minute or two')
    expect(regimeHmmHistoryLabel(3043, 1200)).toBe('HMM needs 3,043 bars, has 1,200')
  })
})

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
