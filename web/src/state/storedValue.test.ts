// Phase 19 Wave 5: storedValue (one best-effort localStorage value) and useStoredState.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { mountHook } from './hookHarness.testkit'
import { browserStorage, storedValue, useStoredState, type ValueStorage } from './storedValue'

// The harness is imported inside the (hoisted) factory, before the hooks under test load.
vi.mock('react', async (orig) => ({
  ...(await orig<typeof import('react')>()),
  ...(await import('./hookHarness.testkit')).fakeReactHooks,
}))

function memory(init: Record<string, string> = {}): ValueStorage & { data: Map<string, string> } {
  const data = new Map(Object.entries(init))
  return {
    data,
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => void data.set(k, v),
  }
}

const throwing: ValueStorage = {
  getItem: () => {
    throw new Error('SecurityError')
  },
  setItem: () => {
    throw new Error('QuotaExceededError')
  },
}

interface Box {
  n: number
}

const box = storedValue<Box>('k', {
  parse: (raw) => {
    const n = (raw as { n?: unknown }).n
    if (typeof n !== 'number') throw new Error('bad')
    return { n }
  },
  fallback: () => ({ n: 0 }),
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('storedValue', () => {
  it('round-trips through JSON by default', () => {
    const s = memory()
    box.save({ n: 3 }, s)
    expect(s.data.get('k')).toBe('{"n":3}')
    expect(box.load(s)).toEqual({ n: 3 })
    expect(box.key).toBe('k')
  })

  it('falls back with no storage, nothing saved, bad JSON, a throwing parse or getItem', () => {
    expect(box.load(null)).toEqual({ n: 0 })
    expect(box.load(memory())).toEqual({ n: 0 })
    expect(box.load(memory({ k: '{oops' }))).toEqual({ n: 0 })
    expect(box.load(memory({ k: '' }))).toEqual({ n: 0 })
    expect(box.load(memory({ k: 'null' }))).toEqual({ n: 0 })
    expect(box.load(memory({ k: '{"n":"x"}' }))).toEqual({ n: 0 })
    expect(box.load(throwing)).toEqual({ n: 0 })
  })

  it('returns a fresh fallback per call', () => {
    const a = box.load(null)
    a.n = 9
    expect(box.load(null)).toEqual({ n: 0 })
  })

  it('swallows write failures (and a throwing serialize); no storage writes nothing', () => {
    expect(() => box.save({ n: 1 }, throwing)).not.toThrow()
    expect(() => box.save({ n: 1 }, null)).not.toThrow()
    const bad = storedValue<number>('b', {
      parse: Number,
      fallback: () => 0,
      serialize: () => {
        throw new Error('nope')
      },
    })
    const s = memory()
    expect(() => bad.save(1, s)).not.toThrow()
    expect(s.data.size).toBe(0)
  })

  it('custom decode/serialize store plain text', () => {
    const tab = storedValue<string>('t', {
      decode: (text) => text,
      parse: (raw) => (raw === 'a' || raw === 'b' ? raw : 'a'),
      fallback: () => 'a',
      serialize: (v) => v,
    })
    const s = memory()
    tab.save('b', s)
    expect(s.data.get('t')).toBe('b')
    expect(tab.load(s)).toBe('b')
    expect(tab.load(memory({ t: '"b"' }))).toBe('a')
  })

  it('defaults to localStorage, and to no storage when there is none or it throws', () => {
    expect(browserStorage()).toBeNull()
    expect(box.load()).toEqual({ n: 0 })
    expect(() => box.save({ n: 1 })).not.toThrow()

    const s = memory()
    vi.stubGlobal('localStorage', s)
    expect(browserStorage()).toBe(s)
    box.save({ n: 5 })
    expect(s.data.get('k')).toBe('{"n":5}')
    expect(box.load()).toEqual({ n: 5 })
    vi.unstubAllGlobals()

    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      get() {
        throw new Error('SecurityError')
      },
    })
    try {
      expect(browserStorage()).toBeNull()
      expect(box.load()).toEqual({ n: 0 })
    } finally {
      Reflect.deleteProperty(globalThis, 'localStorage')
    }
  })
})

describe('useStoredState', () => {
  function counting(initial: number) {
    const saves: number[] = []
    let loads = 0
    return {
      saves,
      loads: () => loads,
      stored: {
        load: () => {
          loads++
          return initial
        },
        save: (v: number) => void saves.push(v),
      },
    }
  }

  it('starts from load() once, saves after mount and after each change, not on re-render', () => {
    const c = counting(4)
    const h = mountHook(() => useStoredState(c.stored), null)
    expect(h.result[0]).toBe(4)
    expect(c.loads()).toBe(1)
    // The replaced effects ran after mount too.
    expect(c.saves).toEqual([4])
    h.rerender(null)
    expect(c.saves).toEqual([4])
    h.result[1](7)
    h.flush()
    expect(h.result[0]).toBe(7)
    expect(c.saves).toEqual([4, 7])
    h.result[1]((v) => v + 1)
    h.flush()
    expect(h.result[0]).toBe(8)
    expect(c.saves).toEqual([4, 7, 8])
    // Setting the same value commits nothing new.
    h.result[1](8)
    h.flush()
    expect(c.saves).toEqual([4, 7, 8])
    expect(c.loads()).toBe(1)
  })

  it('persists through a real storedValue', () => {
    const s = memory({ k: '{"n":2}' })
    vi.stubGlobal('localStorage', s)
    const h = mountHook(() => useStoredState(box), null)
    expect(h.result[0]).toEqual({ n: 2 })
    h.result[1]({ n: 6 })
    h.flush()
    expect(s.data.get('k')).toBe('{"n":6}')
  })
})
