import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Drawing } from '../chart/drawings/types'
import type { DrawingsClient } from '../data/drawingsApi'
import {
  applyDrawingPatch,
  createDrawingsStore,
  DRAWINGS_STORAGE_PREFIX,
  drawingsReducer,
  drawingsStorageKey,
  MAX_DRAWINGS_PER_SYMBOL,
  parseMirror,
  readMirror,
  reconcileDrawings,
  serializeMirror,
  writeMirror,
  drawingsPendingKey,
  isPermanentWriteFailure,
  MAX_PENDING_REMOVALS,
  parsePending,
  readPending,
  writePending,
  type DrawingsStorage,
  type DrawingsSyncStatus,
  type PageLifecycleTarget,
} from './drawings'
import { PersistenceError } from '../data/persistence'

function memoryStorage(initial: Record<string, string> = {}): DrawingsStorage & {
  data: Map<string, string>
} {
  const data = new Map(Object.entries(initial))
  return {
    data,
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => void data.set(k, v),
    removeItem: (k) => void data.delete(k),
  }
}

const throwingStorage: DrawingsStorage = {
  getItem: () => {
    throw new Error('SecurityError')
  },
  setItem: () => {
    throw new Error('QuotaExceededError')
  },
  removeItem: () => {
    throw new Error('SecurityError')
  },
}

function hline(id: string, price = 100, symbol = 'BTCUSDT'): Drawing {
  return {
    id,
    symbol,
    kind: 'hline',
    anchors: [{ time: 1_700_000_000, price }],
    style: { color: '#2962ff', lineWidth: 2, lineStyle: 'solid' },
  }
}

const KEY = 'tv.drawings.v1.BTCUSDT'

describe('mirror', () => {
  it('uses the per-symbol key', () => {
    expect(DRAWINGS_STORAGE_PREFIX).toBe('tv.drawings.v1.')
    expect(drawingsStorageKey(' btcusdt ')).toBe(KEY)
  })

  it('round-trips through write and read', () => {
    const storage = memoryStorage()
    writeMirror('BTCUSDT', [hline('a'), hline('b', 200)], storage)
    expect(readMirror('BTCUSDT', storage)).toEqual([hline('a'), hline('b', 200)])
    expect(readMirror('ETHUSDT', storage)).toEqual([])
  })

  it('removes the key for an empty list', () => {
    const storage = memoryStorage({ [KEY]: serializeMirror([hline('a')]) })
    writeMirror('BTCUSDT', [], storage)
    expect(storage.data.has(KEY)).toBe(false)
  })

  it('reads corrupt JSON, non-arrays and missing keys as []', () => {
    expect(parseMirror('{oops', 'BTCUSDT')).toEqual([])
    expect(parseMirror('{"a":1}', 'BTCUSDT')).toEqual([])
    expect(parseMirror(null, 'BTCUSDT')).toEqual([])
    expect(readMirror('BTCUSDT', memoryStorage({ [KEY]: 'not json' }))).toEqual([])
  })

  it('drops invalid entries, other symbols and duplicate ids', () => {
    const raw = JSON.stringify([
      hline('a'),
      { ...hline('bad'), kind: 'circle' },
      hline('e', 5, 'ETHUSDT'),
      hline('a', 999),
      hline('b'),
    ])
    expect(parseMirror(raw, 'BTCUSDT')).toEqual([hline('a'), hline('b')])
  })

  it('caps the mirror at MAX_DRAWINGS_PER_SYMBOL', () => {
    const many = Array.from({ length: MAX_DRAWINGS_PER_SYMBOL + 5 }, (_, i) => hline(`d${i}`))
    expect(parseMirror(JSON.stringify(many), 'BTCUSDT')).toHaveLength(MAX_DRAWINGS_PER_SYMBOL)
  })

  it('swallows storage errors', () => {
    expect(readMirror('BTCUSDT', throwingStorage)).toEqual([])
    expect(() => writeMirror('BTCUSDT', [hline('a')], throwingStorage)).not.toThrow()
    expect(readMirror('BTCUSDT', null)).toEqual([])
  })
})

describe('drawingsReducer', () => {
  const S = 'BTCUSDT'

  it('adds a valid drawing for the symbol', () => {
    expect(drawingsReducer([], { type: 'add', drawing: hline('a') }, S)).toEqual([hline('a')])
  })

  it('ignores invalid, foreign-symbol and duplicate adds', () => {
    const items = [hline('a')]
    expect(drawingsReducer(items, { type: 'add', drawing: hline('a', 5) }, S)).toBe(items)
    expect(drawingsReducer(items, { type: 'add', drawing: hline('e', 5, 'ETHUSDT') }, S)).toBe(
      items,
    )
    const bad = { ...hline('x'), anchors: [] }
    expect(drawingsReducer(items, { type: 'add', drawing: bad }, S)).toBe(items)
  })

  it('refuses adds beyond the cap', () => {
    const full = Array.from({ length: MAX_DRAWINGS_PER_SYMBOL }, (_, i) => hline(`d${i}`))
    expect(drawingsReducer(full, { type: 'add', drawing: hline('one-more') }, S)).toBe(full)
  })

  it('updates anchors and style, ignoring invalid patches and unknown ids', () => {
    const items = [hline('a'), hline('b')]
    const moved = drawingsReducer(
      items,
      { type: 'update', id: 'b', patch: { anchors: [{ time: 1, price: 7 }] } },
      S,
    )
    expect(moved[1].anchors).toEqual([{ time: 1, price: 7 }])
    expect(moved[0]).toBe(items[0])
    const styled = drawingsReducer(
      items,
      {
        type: 'update',
        id: 'a',
        patch: { style: { color: '#ff0000', lineWidth: 4, lineStyle: 'dotted' } },
      },
      S,
    )
    expect(styled[0].style).toEqual({ color: '#ff0000', lineWidth: 4, lineStyle: 'dotted' })
    const twoAnchors = [
      { time: 1, price: 1 },
      { time: 2, price: 2 },
    ]
    expect(
      drawingsReducer(items, { type: 'update', id: 'a', patch: { anchors: twoAnchors } }, S),
    ).toBe(items)
    expect(drawingsReducer(items, { type: 'update', id: 'zz', patch: { anchors: [] } }, S)).toBe(
      items,
    )
  })

  it('removes and clears', () => {
    const items = [hline('a'), hline('b')]
    expect(drawingsReducer(items, { type: 'remove', id: 'a' }, S)).toEqual([hline('b')])
    expect(drawingsReducer(items, { type: 'remove', id: 'zz' }, S)).toBe(items)
    expect(drawingsReducer(items, { type: 'clear' }, S)).toEqual([])
  })

  it('applyDrawingPatch rejects a bad colour', () => {
    expect(
      applyDrawingPatch(hline('a'), {
        style: { color: 'blue', lineWidth: 1, lineStyle: 'solid' },
      }),
    ).toBeNull()
  })
})

describe('reconcileDrawings', () => {
  const none = { upserted: new Set<string>(), removed: new Set<string>() }

  it('lets a non-empty server list win over the mirror', () => {
    const r = reconcileDrawings([hline('s')], [hline('m')], none)
    expect(r).toEqual({ items: [hline('s')], puts: [], deletes: [] })
  })

  it('uploads the mirror when the server has nothing', () => {
    const r = reconcileDrawings([], [hline('m1'), hline('m2')], none)
    expect(r.items).toEqual([hline('m1'), hline('m2')])
    expect(r.puts).toEqual([hline('m1'), hline('m2')])
    expect(r.deletes).toEqual([])
  })

  it('does nothing when both are empty', () => {
    expect(reconcileDrawings([], [], none)).toEqual({ items: [], puts: [], deletes: [] })
  })

  it('keeps edits made while loading on top of the server list', () => {
    const r = reconcileDrawings(
      [hline('s1', 1), hline('s2', 2), hline('s3', 3)],
      [hline('s1', 10), hline('s2', 2), hline('new', 4)],
      { upserted: new Set(['s1', 'new']), removed: new Set(['s3']) },
    )
    expect(r.items).toEqual([hline('s1', 10), hline('s2', 2), hline('new', 4)])
    expect(r.puts).toEqual([hline('s1', 10), hline('new', 4)])
    expect(r.deletes).toEqual(['s3'])
  })
})

// ---------------------------------------------------------------------------------------------

interface FakeClient extends DrawingsClient {
  calls: string[]
  listResult: (symbol: string) => Promise<Drawing[]>
  putResult: (d: Drawing, keepalive: boolean) => Promise<void>
  removeResult: (id: string, keepalive: boolean) => Promise<void>
  lastSignal: AbortSignal | undefined
}

/** Calls are logged as `PUT id price`, `DELETE id`, with ` keepalive` appended when set. */
function fakeClient(): FakeClient {
  const c: FakeClient = {
    calls: [],
    lastSignal: undefined,
    listResult: () => Promise.resolve([]),
    putResult: () => Promise.resolve(),
    removeResult: () => Promise.resolve(),
    list(symbol, signal) {
      c.calls.push(`GET ${symbol}`)
      c.lastSignal = signal
      return c.listResult(symbol)
    },
    put(d, o) {
      const ka = o?.keepalive === true
      c.calls.push(`PUT ${d.id} ${d.anchors[0].price}${ka ? ' keepalive' : ''}`)
      return c.putResult(d, ka)
    },
    remove(id, o) {
      const ka = o?.keepalive === true
      c.calls.push(`DELETE ${id}${ka ? ' keepalive' : ''}`)
      return c.removeResult(id, ka)
    },
  }
  return c
}

/**
 * A fake server holding rows in memory. `down` makes every write fail like an unreachable
 * server; `loseKeepalive` drops keepalive writes (the page was gone before they landed).
 */
function fakeServer(initial: Drawing[]): FakeClient & {
  rows: Map<string, Drawing>
  down: boolean
  loseKeepalive: boolean
} {
  const c = fakeClient()
  const srv = Object.assign(c, {
    rows: new Map(initial.map((d) => [d.id, d])),
    down: false,
    loseKeepalive: false,
  })
  const fail = () =>
    Promise.reject(new PersistenceError('network', 'Cannot reach the local server'))
  srv.listResult = (symbol) =>
    Promise.resolve([...srv.rows.values()].filter((d) => d.symbol === symbol))
  srv.putResult = (d, ka) => {
    if (srv.down || (ka && srv.loseKeepalive)) return fail()
    srv.rows.set(d.id, d)
    return Promise.resolve()
  }
  srv.removeResult = (id, ka) => {
    if (srv.down || (ka && srv.loseKeepalive)) return fail()
    srv.rows.delete(id)
    return Promise.resolve()
  }
  return srv
}

function deferred<T>(): {
  promise: Promise<T>
  resolve: (v: T) => void
  reject: (e: unknown) => void
} {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

describe('createDrawingsStore', () => {
  let changes: { items: readonly Drawing[]; status: DrawingsSyncStatus }[]
  const onChange = (items: readonly Drawing[], status: DrawingsSyncStatus) =>
    changes.push({ items, status })

  beforeEach(() => {
    changes = []
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('shows the mirror while loading, then adopts the server list and rewrites the mirror', async () => {
    const storage = memoryStorage({ [KEY]: serializeMirror([hline('m')]) })
    const client = fakeClient()
    client.listResult = () => Promise.resolve([hline('s')])
    const store = createDrawingsStore({ symbol: 'BTCUSDT', client, onChange, storage })
    expect(store.items).toEqual([hline('m')])
    expect(store.status).toBe('loading')
    store.start()
    await vi.runAllTimersAsync()
    expect(store.status).toBe('synced')
    expect(store.items).toEqual([hline('s')])
    expect(readMirror('BTCUSDT', storage)).toEqual([hline('s')])
    expect(client.calls).toEqual(['GET BTCUSDT'])
    expect(changes.at(-1)?.status).toBe('synced')
  })

  it('uploads the mirror with one PUT per drawing when the server is empty', async () => {
    const storage = memoryStorage({ [KEY]: serializeMirror([hline('m1', 1), hline('m2', 2)]) })
    const client = fakeClient()
    const store = createDrawingsStore({ symbol: 'BTCUSDT', client, onChange, storage })
    store.start()
    await vi.runAllTimersAsync()
    expect(client.calls).toEqual(['GET BTCUSDT', 'PUT m1 1', 'PUT m2 2'])
    expect(store.items).toEqual([hline('m1', 1), hline('m2', 2)])
  })

  it('stays local-only with the mirror when the list fails, and sends nothing afterwards', async () => {
    const storage = memoryStorage({ [KEY]: serializeMirror([hline('m')]) })
    const client = fakeClient()
    client.listResult = () => Promise.reject(new Error('down'))
    const store = createDrawingsStore({ symbol: 'BTCUSDT', client, onChange, storage })
    store.start()
    await vi.runAllTimersAsync()
    expect(store.status).toBe('local-only')
    store.dispatch({ type: 'add', drawing: hline('n') })
    await vi.runAllTimersAsync()
    expect(client.calls).toEqual(['GET BTCUSDT'])
    expect(readMirror('BTCUSDT', storage)).toEqual([hline('m'), hline('n')])
  })

  it('is local-only without a client', () => {
    const storage = memoryStorage()
    const store = createDrawingsStore({ symbol: 'BTCUSDT', client: null, onChange, storage })
    store.start()
    expect(store.status).toBe('local-only')
    store.dispatch({ type: 'add', drawing: hline('a') })
    expect(readMirror('BTCUSDT', storage)).toEqual([hline('a')])
  })

  it('PUTs only the changed drawing and coalesces rapid updates', async () => {
    const client = fakeClient()
    client.listResult = () => Promise.resolve([hline('a', 1), hline('b', 2)])
    const store = createDrawingsStore({
      symbol: 'BTCUSDT',
      client,
      onChange,
      storage: memoryStorage(),
      debounceMs: 300,
    })
    store.start()
    await vi.runAllTimersAsync()
    for (const price of [10, 11, 12, 13]) {
      store.dispatch({ type: 'update', id: 'b', patch: { anchors: [{ time: 1, price }] } })
      await vi.advanceTimersByTimeAsync(100)
    }
    await vi.runAllTimersAsync()
    expect(client.calls).toEqual(['GET BTCUSDT', 'PUT b 13'])
  })

  it('PUTs an added drawing at once and DELETEs a removed one', async () => {
    const client = fakeClient()
    const store = createDrawingsStore({
      symbol: 'BTCUSDT',
      client,
      onChange,
      storage: memoryStorage(),
    })
    store.start()
    await vi.runAllTimersAsync()
    store.dispatch({ type: 'add', drawing: hline('n', 5) })
    await vi.runAllTimersAsync()
    store.dispatch({ type: 'remove', id: 'n' })
    await vi.runAllTimersAsync()
    expect(client.calls).toEqual(['GET BTCUSDT', 'PUT n 5', 'DELETE n'])
    expect(store.items).toEqual([])
  })

  it('clear DELETEs every drawing of the symbol', async () => {
    const client = fakeClient()
    client.listResult = () => Promise.resolve([hline('a'), hline('b')])
    const store = createDrawingsStore({
      symbol: 'BTCUSDT',
      client,
      onChange,
      storage: memoryStorage(),
    })
    store.start()
    await vi.runAllTimersAsync()
    store.dispatch({ type: 'clear' })
    await vi.runAllTimersAsync()
    expect(client.calls.slice(1).sort()).toEqual(['DELETE a', 'DELETE b'])
  })

  it('retries a failed PUT with backoff up to 3 times, then waits for the next edit', async () => {
    const client = fakeClient()
    client.putResult = () => Promise.reject(new Error('500'))
    const store = createDrawingsStore({
      symbol: 'BTCUSDT',
      client,
      onChange,
      storage: memoryStorage(),
      retryBaseMs: 1000,
    })
    store.start()
    await vi.runAllTimersAsync()
    store.dispatch({ type: 'add', drawing: hline('a', 1) })
    await vi.advanceTimersByTimeAsync(0)
    expect(client.calls.filter((c) => c.startsWith('PUT'))).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(999)
    expect(client.calls.filter((c) => c.startsWith('PUT'))).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1) // retry 1 after 1 s
    await vi.advanceTimersByTimeAsync(2000) // retry 2 after 2 s
    await vi.advanceTimersByTimeAsync(4000) // retry 3 after 4 s
    expect(client.calls.filter((c) => c.startsWith('PUT'))).toHaveLength(4)
    await vi.advanceTimersByTimeAsync(60_000)
    expect(client.calls.filter((c) => c.startsWith('PUT'))).toHaveLength(4)
    client.putResult = () => Promise.resolve()
    store.dispatch({ type: 'update', id: 'a', patch: { anchors: [{ time: 1, price: 2 }] } })
    await vi.runAllTimersAsync()
    expect(client.calls.at(-1)).toBe('PUT a 2')
  })

  it('sends the latest state after an edit made while a PUT was in flight', async () => {
    const client = fakeClient()
    const gate = deferred<void>()
    client.putResult = () => gate.promise
    const store = createDrawingsStore({
      symbol: 'BTCUSDT',
      client,
      onChange,
      storage: memoryStorage(),
      debounceMs: 10,
    })
    store.start()
    await vi.runAllTimersAsync()
    store.dispatch({ type: 'add', drawing: hline('a', 1) })
    await vi.advanceTimersByTimeAsync(0)
    store.dispatch({ type: 'update', id: 'a', patch: { anchors: [{ time: 1, price: 2 }] } })
    await vi.advanceTimersByTimeAsync(50)
    expect(client.calls.slice(1)).toEqual(['PUT a 1'])
    client.putResult = () => Promise.resolve()
    gate.resolve()
    await vi.runAllTimersAsync()
    expect(client.calls.slice(1)).toEqual(['PUT a 1', 'PUT a 2'])
  })

  it('keeps edits made while loading and syncs them once the list arrives', async () => {
    const client = fakeClient()
    const list = deferred<Drawing[]>()
    client.listResult = () => list.promise
    const store = createDrawingsStore({
      symbol: 'BTCUSDT',
      client,
      onChange,
      storage: memoryStorage(),
    })
    store.start()
    store.dispatch({ type: 'add', drawing: hline('mine', 9) })
    await vi.advanceTimersByTimeAsync(1000)
    expect(client.calls).toEqual(['GET BTCUSDT'])
    list.resolve([hline('s', 1), hline('gone', 2)])
    await vi.advanceTimersByTimeAsync(0)
    store.dispatch({ type: 'remove', id: 'gone' })
    await vi.runAllTimersAsync()
    expect(store.items).toEqual([hline('s', 1), hline('mine', 9)])
    expect(client.calls).toEqual(['GET BTCUSDT', 'PUT mine 9', 'DELETE gone'])
  })

  it('ignores a list that resolves after dispose (symbol changed) and aborts it', async () => {
    const storage = memoryStorage()
    const client = fakeClient()
    const list = deferred<Drawing[]>()
    client.listResult = () => list.promise
    const store = createDrawingsStore({ symbol: 'BTCUSDT', client, onChange, storage })
    store.start()
    store.dispose()
    expect(client.lastSignal?.aborted).toBe(true)
    list.resolve([hline('late')])
    await vi.runAllTimersAsync()
    expect(changes).toEqual([])
    expect(store.items).toEqual([])
    expect(readMirror('BTCUSDT', storage)).toEqual([])
  })

  it('adopts another tab’s mirror without echoing PUTs', async () => {
    const client = fakeClient()
    const store = createDrawingsStore({
      symbol: 'BTCUSDT',
      client,
      onChange,
      storage: memoryStorage(),
    })
    store.start()
    await vi.runAllTimersAsync()
    store.dispatch({ type: 'add', drawing: hline('a', 1) })
    await vi.runAllTimersAsync()
    store.adoptMirror(serializeMirror([hline('a', 3), hline('b', 4)]))
    await vi.runAllTimersAsync()
    expect(store.items).toEqual([hline('a', 3), hline('b', 4)])
    expect(client.calls).toEqual(['GET BTCUSDT', 'PUT a 1'])
    store.adoptMirror(null)
    expect(store.items).toEqual([])
  })

  it('never calls onChange after dispose and stops pending PUTs', async () => {
    const client = fakeClient()
    const store = createDrawingsStore({
      symbol: 'BTCUSDT',
      client,
      onChange,
      storage: memoryStorage(),
    })
    store.start()
    await vi.runAllTimersAsync()
    store.dispatch({ type: 'update', id: 'x', patch: {} })
    store.dispatch({ type: 'add', drawing: hline('a') })
    const before = changes.length
    store.dispose()
    expect(store.dispatch({ type: 'add', drawing: hline('b') })).toBe(false)
    await vi.runAllTimersAsync()
    expect(changes).toHaveLength(before)
    // The queued PUT is not sent normally; dispose hands it off once as a keepalive write.
    expect(client.calls).toEqual(['GET BTCUSDT', 'PUT a 100 keepalive'])
  })
})

// ---------------------------------------------------------------------------------------------
// Unsynced ids survive dispose and reload
// ---------------------------------------------------------------------------------------------

const PENDING_KEY = 'tv.drawings.v1.BTCUSDT.pending'

describe('pending ids storage', () => {
  it('uses a key next to the mirror', () => {
    expect(drawingsPendingKey(' btcusdt ')).toBe(PENDING_KEY)
  })

  it('round-trips and removes the key when empty', () => {
    const storage = memoryStorage()
    writePending('BTCUSDT', { upserts: ['a', 'b'], removed: ['c'] }, storage)
    expect(readPending('BTCUSDT', storage)).toEqual({ upserts: ['a', 'b'], removed: ['c'] })
    writePending('BTCUSDT', { upserts: [], removed: [] }, storage)
    expect(storage.data.has(PENDING_KEY)).toBe(false)
  })

  it('validates: corrupt JSON, wrong shapes, bad ids, duplicates, ids in both lists', () => {
    const empty = { upserts: [], removed: [] }
    expect(parsePending(null)).toEqual(empty)
    expect(parsePending('{oops')).toEqual(empty)
    expect(parsePending('[1,2]')).toEqual(empty)
    expect(parsePending('{"upserts":"a","removed":5}')).toEqual(empty)
    expect(
      parsePending(
        JSON.stringify({ upserts: ['a', 'a', '../x', 7, 'b'], removed: ['b', 'c', 'c', ''] }),
      ),
    ).toEqual({ upserts: ['a', 'b'], removed: ['c'] })
  })

  it('caps each list, keeping the newest ids', () => {
    const ids = (n: number) => Array.from({ length: n }, (_, i) => `r${i}`)
    const p = parsePending(
      JSON.stringify({ upserts: ids(MAX_DRAWINGS_PER_SYMBOL + 3), removed: [] }),
    )
    expect(p.upserts).toHaveLength(MAX_DRAWINGS_PER_SYMBOL)
    expect(p.upserts[0]).toBe('r3')
    const q = parsePending(JSON.stringify({ upserts: [], removed: ids(MAX_PENDING_REMOVALS + 1) }))
    expect(q.removed).toHaveLength(MAX_PENDING_REMOVALS)
  })

  it('swallows storage errors', () => {
    expect(readPending('BTCUSDT', throwingStorage)).toEqual({ upserts: [], removed: [] })
    expect(() =>
      writePending('BTCUSDT', { upserts: ['a'], removed: [] }, throwingStorage),
    ).not.toThrow()
  })
})

describe('isPermanentWriteFailure', () => {
  it('is true for 4xx other than 408/429 and for bad ids only', () => {
    const http = (st: number) => new PersistenceError('http', 'x', st)
    expect(isPermanentWriteFailure(http(400))).toBe(true)
    expect(isPermanentWriteFailure(http(404))).toBe(true)
    expect(isPermanentWriteFailure(http(499))).toBe(true)
    expect(isPermanentWriteFailure(new PersistenceError('bad-id', 'x'))).toBe(true)
    expect(isPermanentWriteFailure(http(408))).toBe(false)
    expect(isPermanentWriteFailure(http(429))).toBe(false)
    expect(isPermanentWriteFailure(http(500))).toBe(false)
    expect(isPermanentWriteFailure(new PersistenceError('network', 'x'))).toBe(false)
    expect(isPermanentWriteFailure(new Error('400'))).toBe(false)
  })
})

describe('createDrawingsStore persistence of unsynced edits', () => {
  const A = hline('a', 100)
  const B = hline('b', 200)
  const noop = () => {}

  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  function open(client: DrawingsClient, storage: DrawingsStorage, page?: PageLifecycleTarget) {
    const store = createDrawingsStore({ symbol: 'BTCUSDT', client, storage, onChange: noop, page })
    store.start()
    return store
  }

  // Reviewer scenario (a): server restarting, add, retries exhausted, reload.
  it('keeps an add made while the server is down after retries run out, then uploads it', async () => {
    const storage = memoryStorage()
    const srv = fakeServer([A])
    const s1 = open(srv, storage)
    await vi.runAllTimersAsync()
    srv.down = true
    s1.dispatch({ type: 'add', drawing: B })
    await vi.runAllTimersAsync() // 1 try + MAX_PUT_RETRIES retries, then gives up
    expect(srv.calls.filter((c) => c === 'PUT b 200')).toHaveLength(4)
    expect(readPending('BTCUSDT', storage)).toEqual({ upserts: ['b'], removed: [] })
    s1.dispose()
    expect(srv.calls.at(-1)).toBe('PUT b 200 keepalive') // fails: server still down
    srv.down = false
    srv.calls.length = 0
    const s2 = open(srv, storage)
    await vi.runAllTimersAsync()
    expect(s2.items.map((d) => d.id)).toEqual(['a', 'b'])
    expect(srv.calls).toEqual(['GET BTCUSDT', 'PUT b 200'])
    expect(srv.rows.get('b')).toEqual(B)
    expect(storage.data.has(PENDING_KEY)).toBe(false)
    expect(readMirror('BTCUSDT', storage)).toEqual([A, B])
  })

  // Reviewer scenario (b): a move, then a symbol switch/reload within the debounce.
  it('keeps a move made within the debounce before dispose and sends it with keepalive', async () => {
    const storage = memoryStorage()
    const srv = fakeServer([A])
    srv.loseKeepalive = true // the page was gone before the keepalive landed
    const s1 = open(srv, storage)
    await vi.runAllTimersAsync()
    s1.dispatch({
      type: 'update',
      id: 'a',
      patch: { anchors: [{ time: 1_700_000_000, price: 150 }] },
    })
    await vi.advanceTimersByTimeAsync(100)
    s1.dispose()
    await vi.runAllTimersAsync()
    expect(srv.calls).toEqual(['GET BTCUSDT', 'PUT a 150 keepalive'])
    expect(srv.rows.get('a')?.anchors[0].price).toBe(100) // lost in transit
    const s2 = open(srv, storage)
    await vi.runAllTimersAsync()
    expect(s2.items[0].anchors[0].price).toBe(150)
    expect(srv.calls.at(-1)).toBe('PUT a 150')
    expect(srv.rows.get('a')?.anchors[0].price).toBe(150)
    expect(storage.data.has(PENDING_KEY)).toBe(false)
  })

  it('a keepalive that lands is enough: the reload shows the move', async () => {
    const storage = memoryStorage()
    const srv = fakeServer([A])
    const s1 = open(srv, storage)
    await vi.runAllTimersAsync()
    s1.dispatch({ type: 'update', id: 'a', patch: { anchors: [{ time: 1, price: 150 }] } })
    s1.dispose()
    await vi.runAllTimersAsync()
    expect(srv.rows.get('a')?.anchors[0].price).toBe(150)
    const s2 = open(srv, storage)
    await vi.runAllTimersAsync()
    expect(s2.items[0].anchors[0].price).toBe(150)
  })

  it('does not resurrect a drawing whose DELETE failed', async () => {
    const storage = memoryStorage()
    const srv = fakeServer([A, B])
    const s1 = open(srv, storage)
    await vi.runAllTimersAsync()
    srv.down = true
    s1.dispatch({ type: 'remove', id: 'b' })
    await vi.runAllTimersAsync()
    expect(readPending('BTCUSDT', storage)).toEqual({ upserts: [], removed: ['b'] })
    s1.dispose()
    srv.down = false
    srv.calls.length = 0
    const s2 = open(srv, storage)
    await vi.runAllTimersAsync()
    expect(s2.items).toEqual([A])
    expect(srv.calls).toEqual(['GET BTCUSDT', 'DELETE b'])
    expect(srv.rows.has('b')).toBe(false)
    expect(storage.data.has(PENDING_KEY)).toBe(false)
  })

  it('drops a pending removal the server no longer has without a request', async () => {
    const storage = memoryStorage({ [KEY]: serializeMirror([A]) })
    writePending('BTCUSDT', { upserts: [], removed: ['gone'] }, storage)
    const srv = fakeServer([A])
    const s = open(srv, storage)
    await vi.runAllTimersAsync()
    expect(s.items).toEqual([A])
    expect(srv.calls).toEqual(['GET BTCUSDT'])
    expect(storage.data.has(PENDING_KEY)).toBe(false)
  })

  it('keeps an edit made while loading when disposed before the list arrived', async () => {
    const storage = memoryStorage()
    const srv = fakeServer([A])
    const gate = deferred<Drawing[]>()
    srv.listResult = () => gate.promise
    const s1 = open(srv, storage)
    s1.dispatch({ type: 'add', drawing: B })
    s1.dispose()
    gate.resolve([A])
    await vi.runAllTimersAsync()
    expect(srv.calls).toEqual(['GET BTCUSDT']) // nothing sent while loading
    srv.listResult = (symbol) =>
      Promise.resolve([...srv.rows.values()].filter((d) => d.symbol === symbol))
    const s2 = open(srv, storage)
    await vi.runAllTimersAsync()
    expect(s2.items.map((d) => d.id)).toEqual(['a', 'b'])
    expect(srv.rows.has('b')).toBe(true)
  })

  it('clears a pending id only when the server acknowledged its latest version', async () => {
    const storage = memoryStorage()
    const client = fakeClient()
    const gate = deferred<void>()
    client.putResult = () => gate.promise
    const s = createDrawingsStore({
      symbol: 'BTCUSDT',
      client,
      storage,
      onChange: noop,
      debounceMs: 10,
    })
    s.start()
    await vi.runAllTimersAsync()
    s.dispatch({ type: 'add', drawing: hline('a', 1) })
    await vi.advanceTimersByTimeAsync(0) // PUT a 1 in flight
    s.dispatch({ type: 'update', id: 'a', patch: { anchors: [{ time: 1, price: 2 }] } })
    const second = deferred<void>()
    client.putResult = () => second.promise
    gate.resolve()
    await vi.advanceTimersByTimeAsync(50)
    expect(client.calls.slice(1)).toEqual(['PUT a 1', 'PUT a 2'])
    expect(readPending('BTCUSDT', storage).upserts).toEqual(['a']) // v1 acked, v2 not yet
    second.resolve()
    await vi.runAllTimersAsync()
    expect(storage.data.has(PENDING_KEY)).toBe(false)
  })

  it('keeps this tab’s unsynced edit when another tab writes the mirror, and still PUTs it', async () => {
    const storage = memoryStorage()
    const srv = fakeServer([A, B])
    const s = createDrawingsStore({
      symbol: 'BTCUSDT',
      client: srv,
      storage,
      onChange: noop,
      debounceMs: 300,
    })
    s.start()
    await vi.runAllTimersAsync()
    srv.down = true // b's DELETE and n's PUT fail: both stay unsynced
    s.dispatch({ type: 'update', id: 'a', patch: { anchors: [{ time: 1, price: 150 }] } })
    s.dispatch({ type: 'remove', id: 'b' })
    s.dispatch({ type: 'add', drawing: hline('n', 7) })
    await vi.advanceTimersByTimeAsync(50)
    // The other tab saved its copy (old a, b, and its own c) before seeing our changes.
    const other = serializeMirror([A, B, hline('c', 300)])
    storage.data.set(KEY, other)
    s.adoptMirror(other)
    expect(s.items.map((d) => [d.id, d.anchors[0].price])).toEqual([
      ['a', 150],
      ['c', 300],
      ['n', 7],
    ])
    srv.down = false
    await vi.advanceTimersByTimeAsync(300)
    expect(srv.calls).toContain('PUT a 150')
    expect(srv.rows.get('a')?.anchors[0].price).toBe(150)
    // The merged list is saved on dispose (not echoed back on the storage event).
    expect(storage.data.get(KEY)).toBe(other)
    s.dispose()
    expect(readMirror('BTCUSDT', storage).map((d) => d.id)).toEqual(['a', 'c', 'n'])
  })

  it('does not rewrite the mirror when the server list matches it', async () => {
    const storage = memoryStorage({ [KEY]: serializeMirror([A, B]) })
    const setItem = vi.spyOn(storage, 'setItem')
    const removeItem = vi.spyOn(storage, 'removeItem')
    const srv = fakeServer([A, B])
    const s = open(srv, storage)
    await vi.runAllTimersAsync()
    expect(s.status).toBe('synced')
    expect(setItem).not.toHaveBeenCalled()
    expect(removeItem).not.toHaveBeenCalled()
  })

  it('does not retry a 400, drops it from pending, and keeps the local copy', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const storage = memoryStorage()
    const client = fakeClient()
    client.putResult = () => Promise.reject(new PersistenceError('http', 'bad drawing', 400))
    const s = open(client, storage)
    await vi.runAllTimersAsync()
    s.dispatch({ type: 'add', drawing: B })
    await vi.advanceTimersByTimeAsync(60_000)
    expect(client.calls).toEqual(['GET BTCUSDT', 'PUT b 200'])
    expect(warn).toHaveBeenCalledTimes(1)
    expect(s.items).toEqual([B])
    expect(readMirror('BTCUSDT', storage)).toEqual([B])
    expect(storage.data.has(PENDING_KEY)).toBe(false)
    s.dispose()
    expect(client.calls).toEqual(['GET BTCUSDT', 'PUT b 200']) // nothing left to flush
  })

  it('retries a 429 like a transient failure', async () => {
    const storage = memoryStorage()
    const client = fakeClient()
    client.putResult = () => Promise.reject(new PersistenceError('http', 'slow down', 429))
    const s = open(client, storage)
    await vi.runAllTimersAsync()
    s.dispatch({ type: 'add', drawing: B })
    await vi.runAllTimersAsync()
    expect(client.calls.filter((c) => c === 'PUT b 200')).toHaveLength(4)
    expect(readPending('BTCUSDT', storage).upserts).toEqual(['b'])
  })

  it('flushes unsynced writes with keepalive on pagehide and removes the listener on dispose', async () => {
    const listeners = new Set<() => void>()
    const page: PageLifecycleTarget = {
      addEventListener: (_type, l) => void listeners.add(l),
      removeEventListener: (_type, l) => void listeners.delete(l),
    }
    const storage = memoryStorage()
    const srv = fakeServer([A, B])
    const s = createDrawingsStore({
      symbol: 'BTCUSDT',
      client: srv,
      storage,
      onChange: noop,
      page,
      debounceMs: 300,
    })
    expect(listeners.size).toBe(0)
    s.start()
    expect(listeners.size).toBe(1)
    await vi.runAllTimersAsync()
    s.dispatch({ type: 'update', id: 'a', patch: { anchors: [{ time: 1, price: 150 }] } })
    for (const l of listeners) l()
    expect(srv.calls).toEqual(['GET BTCUSDT', 'PUT a 150 keepalive'])
    // The page stayed (bfcache): the normal write still runs and acknowledges the edit.
    await vi.runAllTimersAsync()
    expect(srv.calls).toEqual(['GET BTCUSDT', 'PUT a 150 keepalive', 'PUT a 150'])
    expect(storage.data.has(PENDING_KEY)).toBe(false)
    for (const l of listeners) l() // nothing unsynced: nothing sent
    expect(srv.calls).toHaveLength(3)
    s.dispose()
    expect(listeners.size).toBe(0)
  })

  it('add and update return whether the change was accepted', async () => {
    const storage = memoryStorage()
    const s = open(fakeServer([]), storage)
    await vi.runAllTimersAsync()
    expect(s.dispatch({ type: 'add', drawing: A })).toBe(true)
    expect(s.dispatch({ type: 'add', drawing: hline('a', 5) })).toBe(false) // duplicate id
    expect(s.dispatch({ type: 'add', drawing: hline('e', 5, 'ETHUSDT') })).toBe(false)
    expect(s.dispatch({ type: 'add', drawing: { ...hline('x'), anchors: [] } })).toBe(false)
    expect(s.dispatch({ type: 'add', drawing: hline('neg', -1) })).toBe(false)
    expect(
      s.dispatch({ type: 'update', id: 'a', patch: { anchors: [{ time: 1, price: 0 }] } }),
    ).toBe(false)
    expect(
      s.dispatch({ type: 'update', id: 'zz', patch: { anchors: [{ time: 1, price: 5 }] } }),
    ).toBe(false)
    expect(
      s.dispatch({
        type: 'update',
        id: 'a',
        patch: { style: { color: 'red', lineWidth: 1, lineStyle: 'solid' } },
      }),
    ).toBe(false)
    expect(
      s.dispatch({ type: 'update', id: 'a', patch: { anchors: [{ time: 1, price: 5 }] } }),
    ).toBe(true)
    expect(s.dispatch({ type: 'remove', id: 'zz' })).toBe(false)
    expect(s.dispatch({ type: 'remove', id: 'a' })).toBe(true)

    const full = Array.from({ length: MAX_DRAWINGS_PER_SYMBOL }, (_, i) => hline(`d${i}`))
    const capped = createDrawingsStore({
      symbol: 'BTCUSDT',
      client: null,
      storage: memoryStorage({ [KEY]: serializeMirror(full) }),
      onChange: noop,
    })
    expect(capped.dispatch({ type: 'add', drawing: hline('one-more') })).toBe(false)
  })

  it('records nothing pending without a client (direct mode)', () => {
    const storage = memoryStorage()
    const s = createDrawingsStore({ symbol: 'BTCUSDT', client: null, storage, onChange: noop })
    s.start()
    s.dispatch({ type: 'add', drawing: A })
    expect(storage.data.has(PENDING_KEY)).toBe(false)
  })
})
