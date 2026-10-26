import { describe, expect, it } from 'vitest'
import {
  DEFAULT_PREFS,
  loadPrefs,
  parsePrefs,
  PREFS_KEY,
  savePrefs,
  type PrefsStorage,
} from './prefs'

function memoryStorage(initial: Record<string, string> = {}): PrefsStorage & {
  data: Record<string, string>
} {
  const data = { ...initial }
  return {
    data,
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => {
      data[k] = v
    },
  }
}

const throwing: PrefsStorage = {
  getItem: () => {
    throw new Error('SecurityError')
  },
  setItem: () => {
    throw new Error('QuotaExceededError')
  },
}

describe('prefs', () => {
  it('returns defaults when nothing is stored', () => {
    expect(loadPrefs(memoryStorage())).toEqual(DEFAULT_PREFS)
    expect(DEFAULT_PREFS).toEqual({ symbol: 'BTCUSDT', interval: '1h', chartType: 'candles' })
  })

  it('round-trips symbol, interval, and chart type', () => {
    const s = memoryStorage()
    savePrefs({ symbol: 'ETHBTC', interval: '1w', chartType: 'heikin-ashi' }, s)
    expect(JSON.parse(s.data[PREFS_KEY])).toEqual({
      symbol: 'ETHBTC',
      interval: '1w',
      chartType: 'heikin-ashi',
    })
    expect(loadPrefs(s)).toEqual({ symbol: 'ETHBTC', interval: '1w', chartType: 'heikin-ashi' })
  })

  it('falls back to defaults on corrupt JSON', () => {
    expect(loadPrefs(memoryStorage({ [PREFS_KEY]: '{not json' }))).toEqual(DEFAULT_PREFS)
    expect(loadPrefs(memoryStorage({ [PREFS_KEY]: 'null' }))).toEqual(DEFAULT_PREFS)
    expect(loadPrefs(memoryStorage({ [PREFS_KEY]: '[1,2]' }))).toEqual(DEFAULT_PREFS)
    expect(loadPrefs(memoryStorage({ [PREFS_KEY]: '42' }))).toEqual(DEFAULT_PREFS)
  })

  it('replaces each invalid field with its own default', () => {
    expect(parsePrefs({ symbol: 'SOLUSDT', interval: '1M', chartType: 'renko' })).toEqual({
      symbol: 'SOLUSDT',
      interval: '1h',
      chartType: 'candles',
    })
    expect(parsePrefs({ symbol: '', interval: '4h', chartType: 'area' })).toEqual({
      symbol: 'BTCUSDT',
      interval: '4h',
      chartType: 'area',
    })
    expect(parsePrefs({ symbol: 42, interval: null })).toEqual(DEFAULT_PREFS)
    expect(parsePrefs({ symbol: 'ETH/USDT' }).symbol).toBe('BTCUSDT')
    expect(parsePrefs({ symbol: ' ethusdt ' }).symbol).toBe('ETHUSDT')
  })

  it('accepts any timeframe parseTimeframe accepts, stored in canonical form', () => {
    expect(parsePrefs({ interval: '1d' }).interval).toBe('1d')
    expect(parsePrefs({ interval: '5s' }).interval).toBe('5s')
    expect(parsePrefs({ interval: '2h' }).interval).toBe('2h')
    expect(parsePrefs({ interval: '1D' }).interval).toBe('1d')
    expect(parsePrefs({ interval: '120m' }).interval).toBe('2h')
  })

  it('rejects strings that are not timeframes', () => {
    for (const bad of ['7s', '1M', '0m', '1y', 'abc', '', '999w']) {
      expect(parsePrefs({ interval: bad }).interval).toBe(DEFAULT_PREFS.interval)
    }
  })

  it('never throws when storage throws or is unavailable', () => {
    expect(loadPrefs(throwing)).toEqual(DEFAULT_PREFS)
    expect(() =>
      savePrefs({ symbol: 'ETHUSDT', interval: '5m', chartType: 'line' }, throwing),
    ).not.toThrow()
    expect(loadPrefs(null)).toEqual(DEFAULT_PREFS)
    expect(() => savePrefs(DEFAULT_PREFS, null)).not.toThrow()
  })

  it('does not persist invalid values', () => {
    const s = memoryStorage()
    savePrefs({ symbol: '', interval: 'x' as never, chartType: 'y' as never }, s)
    expect(JSON.parse(s.data[PREFS_KEY])).toEqual(DEFAULT_PREFS)
  })
})
