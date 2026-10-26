import { describe, expect, it } from 'vitest'
import {
  activePanel,
  assignSlots,
  defaultLayout,
  LAYOUT_KEY,
  layoutReducer,
  loadLayout,
  parseLayout,
  pickPanel,
  sanitizeTimeframes,
  saveLayout,
  visiblePanels,
  type LayoutState,
  type LayoutStorage,
} from './layout'
import { PREFS_KEY } from './prefs'

function memoryStorage(init: Record<string, string> = {}): LayoutStorage & {
  data: Map<string, string>
} {
  const data = new Map(Object.entries(init))
  return {
    data,
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => void data.set(k, v),
  }
}

const throwing: LayoutStorage = {
  getItem: () => {
    throw new Error('denied')
  },
  setItem: () => {
    throw new Error('quota')
  },
}

const PREFS = { symbol: 'ETHUSDT', interval: '4h', chartType: 'line' } as const

function four(state: LayoutState = defaultLayout(PREFS)): LayoutState {
  return layoutReducer(state, { type: 'set-layout', kind: '4' })
}

describe('defaultLayout / migration from tv.prefs.v1', () => {
  it('starts as one chart from the saved prefs', () => {
    const s = defaultLayout(PREFS)
    expect(s.kind).toBe('1')
    expect(s.active).toBe(0)
    expect(visiblePanels(s)).toHaveLength(1)
    expect(activePanel(s)).toEqual({ id: 'p0', ...PREFS })
  })

  it('always keeps four panels with stable ids; extras use other intervals', () => {
    const s = defaultLayout(PREFS)
    expect(s.panels.map((p) => p.id)).toEqual(['p0', 'p1', 'p2', 'p3'])
    expect(s.panels.every((p) => p.symbol === 'ETHUSDT' && p.chartType === 'line')).toBe(true)
    const intervals = s.panels.map((p) => p.interval)
    expect(new Set(intervals).size).toBe(4)
    expect(intervals[0]).toBe('4h')
  })

  it('loadLayout without a saved layout migrates the single-chart prefs', () => {
    const storage = memoryStorage({ [PREFS_KEY]: JSON.stringify(PREFS) })
    const s = loadLayout(storage)
    expect(s.kind).toBe('1')
    expect(activePanel(s)).toMatchObject(PREFS)
  })
})

describe('parseLayout (tolerant)', () => {
  it('round-trips through saveLayout / loadLayout', () => {
    const storage = memoryStorage()
    let s = four()
    s = layoutReducer(s, { type: 'set-active', index: 2 })
    s = layoutReducer(s, { type: 'set-symbol', index: 2, symbol: 'SOLUSDT' })
    s = layoutReducer(s, { type: 'set-sync-crosshair', on: false })
    saveLayout(s, storage)
    expect(storage.data.has(LAYOUT_KEY)).toBe(true)
    expect(loadLayout(storage)).toEqual(s)
  })

  it('falls back field by field', () => {
    const s = parseLayout(
      {
        kind: '9',
        active: 7,
        syncSymbol: 'yes',
        panels: [
          { symbol: 'btc usdt', interval: '7s', chartType: 'area' },
          'junk',
          { symbol: 'solusdt', interval: '1d' },
        ],
      },
      PREFS,
    )
    expect(s.kind).toBe('1')
    expect(s.active).toBe(0)
    expect(s.syncSymbol).toBe(false)
    // Bad symbol and interval -> panel 0 defaults; the valid chart type stays.
    expect(s.panels[0]).toEqual({ id: 'p0', symbol: 'ETHUSDT', interval: '4h', chartType: 'area' })
    expect(s.panels[1]).toEqual(defaultLayout(PREFS).panels[1])
    expect(s.panels[2]).toMatchObject({ symbol: 'SOLUSDT', interval: '1d', chartType: 'line' })
  })

  it('returns defaults for non-objects, bad JSON, and throwing storage', () => {
    expect(parseLayout(null, PREFS)).toEqual(defaultLayout(PREFS))
    expect(parseLayout([1, 2], PREFS)).toEqual(defaultLayout(PREFS))
    expect(loadLayout(memoryStorage({ [LAYOUT_KEY]: '{oops' }), PREFS)).toEqual(
      defaultLayout(PREFS),
    )
    expect(loadLayout(throwing, PREFS)).toEqual(defaultLayout(PREFS))
    expect(loadLayout(null, PREFS)).toEqual(defaultLayout(PREFS))
    expect(() => saveLayout(defaultLayout(), throwing)).not.toThrow()
  })

  it('an active index outside the layout falls back to the first panel', () => {
    expect(parseLayout({ kind: '2', active: 3 }, PREFS).active).toBe(0)
    expect(parseLayout({ kind: '4', active: 3 }, PREFS).active).toBe(3)
  })

  it('symbol sync in a saved file aligns every panel to the active one', () => {
    const s = parseLayout(
      {
        kind: '2',
        active: 1,
        syncSymbol: true,
        panels: [{ symbol: 'BTCUSDT' }, { symbol: 'SOLUSDT' }, { symbol: 'XRPUSDT' }],
      },
      PREFS,
    )
    expect(s.panels.map((p) => p.symbol)).toEqual(['SOLUSDT', 'SOLUSDT', 'SOLUSDT', 'SOLUSDT'])
  })
})

describe('layoutReducer', () => {
  it('no-op actions return the same object', () => {
    const s = four()
    expect(layoutReducer(s, { type: 'set-layout', kind: '4' })).toBe(s)
    expect(layoutReducer(s, { type: 'set-active', index: 0 })).toBe(s)
    expect(layoutReducer(s, { type: 'set-active', index: 9 })).toBe(s)
    expect(layoutReducer(s, { type: 'set-symbol', index: 0, symbol: 'ETHUSDT' })).toBe(s)
    expect(layoutReducer(s, { type: 'set-symbol', index: 0, symbol: '!!' })).toBe(s)
    expect(layoutReducer(s, { type: 'set-interval', index: 0, interval: '4h' })).toBe(s)
    expect(layoutReducer(s, { type: 'set-sync-crosshair', on: true })).toBe(s)
    expect(layoutReducer(s, { type: 'replace-symbol', from: 'NOPE', to: 'BTCUSDT' })).toBe(s)
  })

  it('cannot activate a hidden panel', () => {
    const s = defaultLayout(PREFS)
    expect(layoutReducer(s, { type: 'set-active', index: 1 })).toBe(s)
    const two = layoutReducer(s, { type: 'set-layout', kind: '2' })
    expect(layoutReducer(two, { type: 'set-active', index: 1 }).active).toBe(1)
    expect(layoutReducer(two, { type: 'set-active', index: 2 })).toBe(two)
  })

  it('shrinking past the active panel activates the first; growing restores hidden panels', () => {
    let s = four()
    s = layoutReducer(s, { type: 'set-active', index: 3 })
    s = layoutReducer(s, { type: 'set-symbol', index: 3, symbol: 'DOGEUSDT' })
    const shrunk = layoutReducer(s, { type: 'set-layout', kind: '2' })
    expect(shrunk.active).toBe(0)
    expect(visiblePanels(shrunk)).toHaveLength(2)
    const grown = layoutReducer(shrunk, { type: 'set-layout', kind: '4' })
    expect(grown.panels[3].symbol).toBe('DOGEUSDT')
  })

  it('keeps the active panel when it is still visible', () => {
    let s = four()
    s = layoutReducer(s, { type: 'set-active', index: 1 })
    expect(layoutReducer(s, { type: 'set-layout', kind: '2' }).active).toBe(1)
  })

  it('without symbol sync, a symbol change touches only its panel', () => {
    const s = layoutReducer(four(), { type: 'set-symbol', index: 1, symbol: 'solusdt' })
    expect(s.panels.map((p) => p.symbol)).toEqual(['ETHUSDT', 'SOLUSDT', 'ETHUSDT', 'ETHUSDT'])
  })

  it('with symbol sync, a symbol change applies to every panel; intervals stay per panel', () => {
    let s = four()
    s = layoutReducer(s, { type: 'set-sync-symbol', on: true })
    const before = s.panels.map((p) => p.interval)
    s = layoutReducer(s, { type: 'set-symbol', index: 2, symbol: 'BTCUSDT' })
    expect(s.panels.every((p) => p.symbol === 'BTCUSDT')).toBe(true)
    expect(s.panels.map((p) => p.interval)).toEqual(before)
  })

  it('turning symbol sync on aligns every panel to the active panel', () => {
    let s = four()
    s = layoutReducer(s, { type: 'set-symbol', index: 1, symbol: 'SOLUSDT' })
    s = layoutReducer(s, { type: 'set-symbol', index: 2, symbol: 'XRPUSDT' })
    s = layoutReducer(s, { type: 'set-active', index: 1 })
    s = layoutReducer(s, { type: 'set-sync-symbol', on: true })
    expect(s.panels.map((p) => p.symbol)).toEqual(['SOLUSDT', 'SOLUSDT', 'SOLUSDT', 'SOLUSDT'])
    // Turning it off changes no symbol.
    const off = layoutReducer(s, { type: 'set-sync-symbol', on: false })
    expect(off.panels).toBe(s.panels)
  })

  it('interval and chart type are per panel', () => {
    let s = four()
    s = layoutReducer(s, { type: 'set-interval', index: 2, interval: '1m' })
    s = layoutReducer(s, { type: 'set-chart-type', index: 3, chartType: 'heikin-ashi' })
    expect(s.panels[2].interval).toBe('1m')
    expect(s.panels[0].interval).toBe('4h')
    expect(s.panels.map((p) => p.chartType)).toEqual(['line', 'line', 'line', 'heikin-ashi'])
  })

  it('replace-symbol swaps an unlisted symbol in every panel showing it', () => {
    let s = four()
    s = layoutReducer(s, { type: 'set-symbol', index: 1, symbol: 'SOLUSDT' })
    s = layoutReducer(s, { type: 'replace-symbol', from: 'ETHUSDT', to: 'BTCUSDT' })
    expect(s.panels.map((p) => p.symbol)).toEqual(['BTCUSDT', 'SOLUSDT', 'BTCUSDT', 'BTCUSDT'])
  })
})

describe('custom and seconds timeframes', () => {
  it('a saved custom or seconds timeframe round-trips', () => {
    const storage = memoryStorage()
    let s = four()
    s = layoutReducer(s, { type: 'set-interval', index: 0, interval: '5s' })
    s = layoutReducer(s, { type: 'set-interval', index: 1, interval: '3m' })
    s = layoutReducer(s, { type: 'set-interval', index: 2, interval: '3d' })
    saveLayout(s, storage)
    expect(loadLayout(storage, PREFS).panels.map((p) => p.interval)).toEqual(
      s.panels.map((p) => p.interval),
    )
    expect(s.panels.slice(0, 3).map((p) => p.interval)).toEqual(['5s', '3m', '3d'])
  })

  it('a saved timeframe is canonicalised; an invalid one falls back to the panel default', () => {
    const s = parseLayout(
      { panels: [{ interval: '120m' }, { interval: '7s' }, { interval: '1M' }] },
      PREFS,
    )
    const def = defaultLayout(PREFS)
    expect(s.panels[0].interval).toBe('2h')
    expect(s.panels[1].interval).toBe(def.panels[1].interval)
    expect(s.panels[2].interval).toBe(def.panels[2].interval)
  })

  it('set-interval canonicalises and ignores a malformed timeframe', () => {
    const s = four()
    expect(layoutReducer(s, { type: 'set-interval', index: 0, interval: '7s' })).toBe(s)
    expect(layoutReducer(s, { type: 'set-interval', index: 0, interval: '4h' })).toBe(s)
    expect(
      layoutReducer(s, { type: 'set-interval', index: 1, interval: '60s' }).panels[1].interval,
    ).toBe('1m')
  })

  it('sanitizeTimeframes moves unsupported panels to the fallback', () => {
    let s = four()
    s = layoutReducer(s, { type: 'set-interval', index: 0, interval: '5s' })
    s = layoutReducer(s, { type: 'set-interval', index: 3, interval: '30s' })
    const noSeconds = (tf: string) => !tf.endsWith('s')
    const next = sanitizeTimeframes(s, noSeconds, '1m')
    expect(next.panels.map((p) => p.interval)).toEqual([
      '1m',
      s.panels[1].interval,
      s.panels[2].interval,
      '1m',
    ])
    // Untouched panels keep their identity.
    expect(next.panels[1]).toBe(s.panels[1])
    expect(next.active).toBe(s.active)
  })

  it('sanitizeTimeframes returns the same object when every panel is supported', () => {
    const s = four()
    expect(sanitizeTimeframes(s, () => true, '1m')).toBe(s)
  })
})

describe('pickPanel', () => {
  const s = (() => {
    let x = four()
    x = layoutReducer(x, { type: 'set-symbol', index: 1, symbol: 'BTCUSDT' })
    x = layoutReducer(x, { type: 'set-symbol', index: 3, symbol: 'BTCUSDT' })
    return x
  })()
  const isBtc = (p: { symbol: string }) => p.symbol === 'BTCUSDT'

  it('prefers the active panel', () => {
    expect(pickPanel(layoutReducer(s, { type: 'set-active', index: 3 }), isBtc)).toBe(3)
  })

  it('else the first visible match, else -1', () => {
    expect(pickPanel(s, isBtc)).toBe(1)
    expect(pickPanel(s, () => false)).toBe(-1)
    // Panel 3 matches but is hidden in a 2-chart layout.
    const two = layoutReducer(s, { type: 'set-layout', kind: '2' })
    expect(pickPanel(two, (p) => p.id === 'p3')).toBe(-1)
  })
})

describe('assignSlots', () => {
  it('one slot per distinct symbol, unused slots empty', () => {
    expect(assignSlots([], ['BTC', 'ETH', 'BTC'])).toEqual(['BTC', 'ETH', '', ''])
  })

  it('a slot keeps its symbol while still needed', () => {
    const prev = ['BTC', 'ETH', 'SOL', '']
    expect(assignSlots(prev, ['XRP', 'ETH', 'SOL'])).toEqual(['XRP', 'ETH', 'SOL', ''])
    expect(assignSlots(prev, ['ETH'])).toEqual(['', 'ETH', '', ''])
  })

  it('returns the previous array when nothing changes', () => {
    const prev = assignSlots([], ['BTC', 'ETH'])
    expect(assignSlots(prev, ['ETH', 'BTC', 'ETH'])).toBe(prev)
  })
})
