import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { PersistenceClient } from '../data/persistence'
import {
  addSymbol,
  createWatchlistSync,
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
  validateWatchlist,
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

describe('validateWatchlist', () => {
  it('accepts decoded lists with the same rules as parseWatchlist', () => {
    expect(validateWatchlist(['BTCUSDT', 'ETHUSDT'])).toEqual(['BTCUSDT', 'ETHUSDT'])
    expect(validateWatchlist([])).toEqual([])
    expect(validateWatchlist(['btcusdt'])).toBeNull()
    expect(validateWatchlist(['BTCUSDT', 'BTCUSDT'])).toBeNull()
    expect(validateWatchlist({ items: [] })).toBeNull()
  })
})

function fakeClient(over: Partial<PersistenceClient>): PersistenceClient {
  const fail = () => Promise.reject(new Error('unexpected call'))
  return { getWatchlist: fail, putWatchlist: fail, getLayout: fail, putLayout: fail, ...over }
}

describe('createWatchlistSync', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('adopts a valid server list from /api/watchlists/default', async () => {
    const getWatchlist = vi.fn(async (id: string) => ({ id, items: ['SOLUSDT'], updatedAt: 1 }))
    const remote: string[][] = []
    const sync = createWatchlistSync(
      ['BTCUSDT'],
      (i) => remote.push(i),
      fakeClient({ getWatchlist }),
    )
    sync.start()
    await vi.advanceTimersByTimeAsync(0)
    expect(getWatchlist).toHaveBeenCalledWith('default')
    expect(remote).toEqual([['SOLUSDT']])
  })

  it('ignores an invalid server list', async () => {
    const remote: string[][] = []
    const sync = createWatchlistSync(
      ['BTCUSDT'],
      (i) => remote.push(i),
      fakeClient({ getWatchlist: async (id) => ({ id, items: ['bad sym'], updatedAt: 1 }) }),
    )
    sync.start()
    await vi.advanceTimersByTimeAsync(0)
    expect(remote).toEqual([])
    expect(sync.state).toBe('synced')
  })

  it('migrates the local list on 404 and debounces later edits', async () => {
    const putWatchlist = vi.fn(async (id: string, items: readonly string[]) => ({
      id,
      items: [...items],
      updatedAt: 1,
    }))
    const sync = createWatchlistSync(
      ['BTCUSDT'],
      () => {},
      fakeClient({ getWatchlist: async () => null, putWatchlist }),
      300,
    )
    sync.start()
    await vi.advanceTimersByTimeAsync(0)
    expect(putWatchlist).toHaveBeenCalledWith('default', ['BTCUSDT'])
    sync.update(['BTCUSDT', 'ETHUSDT'])
    sync.update(['ETHUSDT'])
    await vi.advanceTimersByTimeAsync(300)
    expect(putWatchlist).toHaveBeenCalledTimes(2)
    expect(putWatchlist).toHaveBeenLastCalledWith('default', ['ETHUSDT'])
  })

  it('stays local-only when the server is unreachable', async () => {
    const putWatchlist = vi.fn()
    const sync = createWatchlistSync(
      ['BTCUSDT'],
      () => {},
      fakeClient({ getWatchlist: () => Promise.reject(new Error('ECONNREFUSED')), putWatchlist }),
    )
    sync.start()
    await vi.advanceTimersByTimeAsync(0)
    sync.update(['ETHUSDT'])
    await vi.advanceTimersByTimeAsync(5000)
    expect(sync.state).toBe('local-only')
    expect(putWatchlist).not.toHaveBeenCalled()
  })
})
