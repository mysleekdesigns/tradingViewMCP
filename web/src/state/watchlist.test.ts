import { describe, expect, it } from 'vitest'
import {
  addSymbol,
  DEFAULT_WATCHLIST,
  loadWatchlist,
  MAX_WATCHLIST_ITEMS,
  moveItem,
  normalizeSymbol,
  parseWatchlist,
  removeSymbol,
  saveWatchlist,
  serializeWatchlist,
  WATCHLIST_STORAGE_KEY,
  watchlistReducer,
  type KeyValueStorage,
} from './watchlist'

class MemoryStorage implements KeyValueStorage {
  data = new Map<string, string>()
  getItem(key: string): string | null {
    return this.data.get(key) ?? null
  }
  setItem(key: string, value: string): void {
    this.data.set(key, value)
  }
}

const throwingStorage: KeyValueStorage = {
  getItem() {
    throw new Error('SecurityError')
  },
  setItem() {
    throw new Error('QuotaExceededError')
  },
}

describe('DEFAULT_WATCHLIST', () => {
  it('is a valid stored list', () => {
    expect(parseWatchlist(serializeWatchlist(DEFAULT_WATCHLIST))).toEqual([...DEFAULT_WATCHLIST])
    expect(DEFAULT_WATCHLIST[0]).toBe('BTCUSDT')
  })
})

describe('normalizeSymbol', () => {
  it('trims and upper-cases', () => {
    expect(normalizeSymbol('  ethusdt ')).toBe('ETHUSDT')
  })
  it('rejects empty and junk', () => {
    expect(normalizeSymbol('')).toBeNull()
    expect(normalizeSymbol('   ')).toBeNull()
    expect(normalizeSymbol('BTC USDT')).toBeNull()
    expect(normalizeSymbol('<script>')).toBeNull()
  })
})

describe('addSymbol', () => {
  it('appends a new symbol, normalised', () => {
    expect(addSymbol(['BTCUSDT'], 'ethusdt')).toEqual(['BTCUSDT', 'ETHUSDT'])
  })
  it('dedupes (case-insensitive) and returns the same array', () => {
    const items = ['BTCUSDT', 'ETHUSDT']
    expect(addSymbol(items, 'BTCUSDT')).toBe(items)
    expect(addSymbol(items, 'btcusdt')).toBe(items)
  })
  it('ignores invalid symbols', () => {
    const items = ['BTCUSDT']
    expect(addSymbol(items, '')).toBe(items)
  })
  it('stops at the size cap', () => {
    const items = Array.from({ length: MAX_WATCHLIST_ITEMS }, (_, i) => `S${i}USDT`)
    expect(addSymbol(items, 'NEWUSDT')).toBe(items)
  })
})

describe('removeSymbol', () => {
  it('removes the symbol', () => {
    expect(removeSymbol(['BTCUSDT', 'ETHUSDT', 'SOLUSDT'], 'ETHUSDT')).toEqual([
      'BTCUSDT',
      'SOLUSDT',
    ])
  })
  it('returns the same array when absent', () => {
    const items = ['BTCUSDT']
    expect(removeSymbol(items, 'XRPUSDT')).toBe(items)
  })
})

describe('moveItem', () => {
  const items = ['A', 'B', 'C', 'D']
  it('moves down and up', () => {
    expect(moveItem(items, 0, 2)).toEqual(['B', 'C', 'A', 'D'])
    expect(moveItem(items, 3, 1)).toEqual(['A', 'D', 'B', 'C'])
  })
  it('clamps the target into range', () => {
    expect(moveItem(items, 1, 99)).toEqual(['A', 'C', 'D', 'B'])
    expect(moveItem(items, 2, -5)).toEqual(['C', 'A', 'B', 'D'])
  })
  it('returns the same array for no-ops and bad sources', () => {
    expect(moveItem(items, 1, 1)).toBe(items)
    expect(moveItem(items, 0, -1)).toBe(items) // clamps to 0
    expect(moveItem(items, -1, 2)).toBe(items)
    expect(moveItem(items, 4, 0)).toBe(items)
    expect(moveItem(items, 1.5, 0)).toBe(items)
    expect(moveItem([], 0, 0)).toEqual([])
  })
  it('does not mutate the input', () => {
    const copy = [...items]
    moveItem(items, 0, 3)
    expect(items).toEqual(copy)
  })
})

describe('watchlistReducer', () => {
  it('dispatches add/remove/move/replace', () => {
    let s: readonly string[] = ['BTCUSDT']
    s = watchlistReducer(s, { type: 'add', symbol: 'ethusdt' })
    s = watchlistReducer(s, { type: 'add', symbol: 'SOLUSDT' })
    s = watchlistReducer(s, { type: 'move', from: 2, to: 0 })
    expect(s).toEqual(['SOLUSDT', 'BTCUSDT', 'ETHUSDT'])
    s = watchlistReducer(s, { type: 'remove', symbol: 'BTCUSDT' })
    expect(s).toEqual(['SOLUSDT', 'ETHUSDT'])
    const same = watchlistReducer(s, { type: 'replace', items: ['SOLUSDT', 'ETHUSDT'] })
    expect(same).toBe(s)
    expect(watchlistReducer(s, { type: 'replace', items: ['XRPUSDT'] })).toEqual(['XRPUSDT'])
  })
})

describe('parseWatchlist', () => {
  it('accepts a valid list, including an empty one', () => {
    expect(parseWatchlist('["BTCUSDT","ETHUSDT"]')).toEqual(['BTCUSDT', 'ETHUSDT'])
    expect(parseWatchlist('[]')).toEqual([])
  })
  it('rejects missing, malformed, and invalid values', () => {
    expect(parseWatchlist(null)).toBeNull()
    expect(parseWatchlist('')).toBeNull()
    expect(parseWatchlist('{not json')).toBeNull()
    expect(parseWatchlist('{"a":1}')).toBeNull()
    expect(parseWatchlist('"BTCUSDT"')).toBeNull()
    expect(parseWatchlist('["BTCUSDT",1]')).toBeNull()
    expect(parseWatchlist('["btcusdt"]')).toBeNull() // not upper-case
    expect(parseWatchlist('[""]')).toBeNull()
    expect(parseWatchlist('[" BTCUSDT"]')).toBeNull()
    expect(parseWatchlist('["BTCUSDT","BTCUSDT"]')).toBeNull() // duplicate
    const tooMany = JSON.stringify(
      Array.from({ length: MAX_WATCHLIST_ITEMS + 1 }, (_, i) => `S${i}`),
    )
    expect(parseWatchlist(tooMany)).toBeNull()
  })
  it('round-trips with serializeWatchlist', () => {
    const items = ['SOLUSDT', 'BTCUSDT']
    expect(parseWatchlist(serializeWatchlist(items))).toEqual(items)
  })
})

describe('loadWatchlist / saveWatchlist', () => {
  it('falls back to the default when nothing or junk is stored', () => {
    const storage = new MemoryStorage()
    expect(loadWatchlist(storage)).toBe(DEFAULT_WATCHLIST)
    storage.setItem(WATCHLIST_STORAGE_KEY, '["bad", ')
    expect(loadWatchlist(storage)).toBe(DEFAULT_WATCHLIST)
  })
  it('persists under tv.watchlist.v1 and loads it back', () => {
    const storage = new MemoryStorage()
    saveWatchlist(['ETHUSDT', 'BTCUSDT'], storage)
    expect(storage.data.get('tv.watchlist.v1')).toBe('["ETHUSDT","BTCUSDT"]')
    expect(loadWatchlist(storage)).toEqual(['ETHUSDT', 'BTCUSDT'])
  })
  it('keeps an intentionally empty list', () => {
    const storage = new MemoryStorage()
    saveWatchlist([], storage)
    expect(loadWatchlist(storage)).toEqual([])
  })
  it('survives storage that throws or is missing', () => {
    expect(loadWatchlist(throwingStorage)).toBe(DEFAULT_WATCHLIST)
    expect(() => saveWatchlist(['BTCUSDT'], throwingStorage)).not.toThrow()
    expect(loadWatchlist(null)).toBe(DEFAULT_WATCHLIST)
    expect(() => saveWatchlist(['BTCUSDT'], null)).not.toThrow()
  })
  it('uses the default storage argument safely in Node (no localStorage)', () => {
    expect(loadWatchlist()).toEqual([...DEFAULT_WATCHLIST])
    expect(() => saveWatchlist(['BTCUSDT'])).not.toThrow()
  })
})
