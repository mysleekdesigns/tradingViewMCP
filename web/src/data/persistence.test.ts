import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  createPersistenceClient,
  createServerSync,
  PersistenceError,
  type FetchLike,
  type ServerSyncOptions,
} from './persistence'

interface Call {
  url: string
  method: string
  body: unknown
  headers: Record<string, string>
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function fakeFetch(respond: (call: Call) => Response | Promise<Response>): {
  fetchImpl: FetchLike
  calls: Call[]
} {
  const calls: Call[] = []
  const fetchImpl: FetchLike = async (url, init) => {
    const call: Call = {
      url,
      method: init?.method ?? 'GET',
      body: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined,
      headers: (init?.headers ?? {}) as Record<string, string>,
    }
    calls.push(call)
    return respond(call)
  }
  return { fetchImpl, calls }
}

describe('createPersistenceClient', () => {
  it('GETs a watchlist from the same origin by default', async () => {
    const { fetchImpl, calls } = fakeFetch(() =>
      jsonResponse(200, { id: 'default', items: ['BTCUSDT'], updatedAt: 1700000000 }),
    )
    const client = createPersistenceClient({ fetchImpl })
    await expect(client.getWatchlist('default')).resolves.toEqual({
      id: 'default',
      items: ['BTCUSDT'],
      updatedAt: 1700000000,
    })
    expect(calls).toEqual([
      expect.objectContaining({ url: '/api/watchlists/default', method: 'GET' }),
    ])
  })

  it('PUTs a watchlist as { items } JSON under baseUrl', async () => {
    const { fetchImpl, calls } = fakeFetch((c) =>
      jsonResponse(200, {
        id: 'default',
        items: (c.body as { items: string[] }).items,
        updatedAt: 1,
      }),
    )
    const client = createPersistenceClient({ fetchImpl, baseUrl: 'http://127.0.0.1:8787' })
    const out = await client.putWatchlist('default', ['ETHUSDT', 'BTCUSDT'])
    expect(out.items).toEqual(['ETHUSDT', 'BTCUSDT'])
    expect(calls[0].url).toBe('http://127.0.0.1:8787/api/watchlists/default')
    expect(calls[0].method).toBe('PUT')
    expect(calls[0].body).toEqual({ items: ['ETHUSDT', 'BTCUSDT'] })
    expect(calls[0].headers['Content-Type']).toBe('application/json')
  })

  it('GETs and PUTs layouts as { data }', async () => {
    const data = { indicators: [{ uid: 'a', type: 'ema' }] }
    const { fetchImpl, calls } = fakeFetch(() =>
      jsonResponse(200, { id: 'default', data, updatedAt: 2 }),
    )
    const client = createPersistenceClient({ fetchImpl })
    await expect(client.getLayout('default')).resolves.toEqual({
      id: 'default',
      data,
      updatedAt: 2,
    })
    await client.putLayout('default', data)
    expect(calls[0]).toMatchObject({ url: '/api/layouts/default', method: 'GET' })
    expect(calls[1]).toMatchObject({ url: '/api/layouts/default', method: 'PUT', body: { data } })
  })

  it('returns null on 404 for GET', async () => {
    const { fetchImpl } = fakeFetch(() => jsonResponse(404, { error: 'http', message: 'nope' }))
    const client = createPersistenceClient({ fetchImpl })
    await expect(client.getWatchlist('default')).resolves.toBeNull()
    await expect(client.getLayout('default')).resolves.toBeNull()
  })

  it('throws on 404 for PUT and on other HTTP errors', async () => {
    const { fetchImpl } = fakeFetch(() => jsonResponse(404, { error: 'http', message: 'x' }))
    const client = createPersistenceClient({ fetchImpl })
    await expect(client.putWatchlist('default', [])).rejects.toMatchObject({
      kind: 'http',
      status: 404,
    })
    const c500 = createPersistenceClient({
      fetchImpl: fakeFetch(() => jsonResponse(500, {})).fetchImpl,
    })
    await expect(c500.getWatchlist('default')).rejects.toBeInstanceOf(PersistenceError)
    await expect(c500.getLayout('default')).rejects.toMatchObject({ kind: 'http', status: 500 })
  })

  it('throws network on a rejected fetch and parse on bad bodies', async () => {
    const down = createPersistenceClient({
      fetchImpl: () => Promise.reject(new TypeError('Failed to fetch')),
    })
    await expect(down.getWatchlist('default')).rejects.toMatchObject({ kind: 'network' })

    const html = createPersistenceClient({
      fetchImpl: async () => new Response('<!doctype html>', { status: 200 }),
    })
    await expect(html.getWatchlist('default')).rejects.toMatchObject({ kind: 'parse' })

    const badShape = createPersistenceClient({
      fetchImpl: fakeFetch(() => jsonResponse(200, { id: 'default', items: [1], updatedAt: 1 }))
        .fetchImpl,
    })
    await expect(badShape.getWatchlist('default')).rejects.toMatchObject({ kind: 'parse' })

    const badLayout = createPersistenceClient({
      fetchImpl: fakeFetch(() => jsonResponse(200, { id: 'default', data: [], updatedAt: 1 }))
        .fetchImpl,
    })
    await expect(badLayout.getLayout('default')).rejects.toMatchObject({ kind: 'parse' })
  })

  it('rejects invalid ids without calling fetch', async () => {
    const { fetchImpl, calls } = fakeFetch(() => jsonResponse(200, {}))
    const client = createPersistenceClient({ fetchImpl })
    await expect(client.getWatchlist('../etc')).rejects.toMatchObject({ kind: 'bad-id' })
    expect(calls).toHaveLength(0)
  })

  describe('timeout', () => {
    beforeEach(() => vi.useFakeTimers())
    afterEach(() => vi.useRealTimers())

    it('aborts after timeoutMs with a network error', async () => {
      const fetchImpl: FetchLike = (_url, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(new Error('AbortError')))
        })
      const client = createPersistenceClient({ fetchImpl, timeoutMs: 5000 })
      const p = client.getWatchlist('default')
      const assertion = expect(p).rejects.toMatchObject({
        kind: 'network',
        message: 'request timed out',
      })
      await vi.advanceTimersByTimeAsync(5000)
      await assertion
    })
  })
})

// ---------------------------------------------------------------------------------------------

interface Deferred<T> {
  promise: Promise<T>
  resolve(v: T): void
  reject(e: unknown): void
}

function deferred<T>(): Deferred<T> {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

const parseList = (raw: unknown): string[] | null =>
  Array.isArray(raw) && raw.every((x) => typeof x === 'string') ? (raw as string[]) : null

function harness(overrides: Partial<ServerSyncOptions<string[]>> = {}) {
  const get = deferred<unknown | null>()
  const saves: string[][] = []
  const saveResults: Deferred<unknown>[] = []
  const remote: string[][] = []
  const sync = createServerSync<string[]>({
    initial: ['LOCAL'],
    load: () => get.promise,
    save: (v) => {
      saves.push(v)
      const d = deferred<unknown>()
      saveResults.push(d)
      return d.promise
    },
    parse: parseList,
    onRemote: (v) => remote.push(v),
    debounceMs: 300,
    ...overrides,
  })
  return { sync, get, saves, saveResults, remote }
}

describe('createServerSync', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('adopts a valid server value without echoing it back', async () => {
    const h = harness()
    h.sync.start()
    h.get.resolve(['SERVER'])
    await vi.advanceTimersByTimeAsync(0)
    expect(h.sync.state).toBe('synced')
    expect(h.remote).toEqual([['SERVER']])
    // The hook re-reports the adopted value; that must not trigger a PUT.
    h.sync.update(['SERVER'])
    await vi.advanceTimersByTimeAsync(1000)
    expect(h.saves).toEqual([])
  })

  it('ignores an invalid server value, keeps local, and saves the next edit', async () => {
    const h = harness()
    h.sync.start()
    h.get.resolve({ not: 'a list' })
    await vi.advanceTimersByTimeAsync(0)
    expect(h.remote).toEqual([])
    expect(h.saves).toEqual([])
    h.sync.update(['EDIT'])
    await vi.advanceTimersByTimeAsync(300)
    expect(h.saves).toEqual([['EDIT']])
  })

  it('migrates the local value on 404', async () => {
    const h = harness()
    h.sync.start()
    h.get.resolve(null)
    await vi.advanceTimersByTimeAsync(0)
    expect(h.saves).toEqual([['LOCAL']])
    expect(h.remote).toEqual([])
    h.saveResults[0].resolve({})
    await vi.advanceTimersByTimeAsync(0)
    expect(h.sync.state).toBe('synced')
    // Same value again: nothing more to send.
    h.sync.update(['LOCAL'])
    await vi.advanceTimersByTimeAsync(1000)
    expect(h.saves).toHaveLength(1)
  })

  it('migrates the latest local edit made while the GET was pending', async () => {
    const h = harness()
    h.sync.start()
    h.sync.update(['A'])
    h.sync.update(['A', 'B'])
    h.get.resolve(null)
    await vi.advanceTimersByTimeAsync(0)
    expect(h.saves).toEqual([['A', 'B']])
  })

  it('goes local-only when the migration PUT fails', async () => {
    const h = harness()
    h.sync.start()
    h.get.resolve(null)
    await vi.advanceTimersByTimeAsync(0)
    h.saveResults[0].reject(new Error('500'))
    await vi.advanceTimersByTimeAsync(0)
    expect(h.sync.state).toBe('local-only')
    h.sync.update(['X'])
    await vi.advanceTimersByTimeAsync(1000)
    expect(h.saves).toHaveLength(1)
  })

  it('stays local-only after a GET failure and never retries', async () => {
    const load = vi.fn(() => Promise.reject(new Error('ECONNREFUSED')))
    const h = harness({ load })
    h.sync.start()
    h.sync.start()
    await vi.advanceTimersByTimeAsync(0)
    expect(h.sync.state).toBe('local-only')
    h.sync.update(['A'])
    h.sync.update(['B'])
    await vi.advanceTimersByTimeAsync(60_000)
    expect(load).toHaveBeenCalledTimes(1)
    expect(h.saves).toEqual([])
    expect(h.remote).toEqual([])
  })

  it('treats a synchronously throwing load as a failure', async () => {
    const h = harness({
      load: () => {
        throw new Error('no fetch')
      },
    })
    h.sync.start()
    await vi.advanceTimersByTimeAsync(0)
    expect(h.sync.state).toBe('local-only')
  })

  it('never PUTs before the initial GET settles; an unedited tab adopts the server copy', async () => {
    const h = harness()
    h.sync.start()
    h.sync.update(['LOCAL']) // the initial value reported by the hook: not an edit
    await vi.advanceTimersByTimeAsync(10_000)
    expect(h.saves).toEqual([])
    expect(h.sync.state).toBe('pending')
    h.get.resolve(['SERVER'])
    await vi.advanceTimersByTimeAsync(1000)
    expect(h.remote).toEqual([['SERVER']])
    expect(h.saves).toEqual([])
  })

  it('W3: an edit made while the GET is pending wins over the server copy and is PUT', async () => {
    const h = harness()
    h.sync.start()
    h.sync.update(['EDIT1'])
    await vi.advanceTimersByTimeAsync(10_000)
    expect(h.saves).toEqual([]) // still never before the GET settles
    h.get.resolve(['SERVER'])
    await vi.advanceTimersByTimeAsync(300)
    expect(h.remote).toEqual([]) // not adopted
    expect(h.saves).toEqual([['EDIT1']])
  })

  it('W3: a pending edit equal to the server copy is not PUT', async () => {
    const h = harness()
    h.sync.start()
    h.sync.update(['SERVER'])
    h.get.resolve(['SERVER'])
    await vi.advanceTimersByTimeAsync(1000)
    expect(h.saves).toEqual([])
  })

  it('W3: an edit that returns to the initial value before the GET is not dirty', async () => {
    const h = harness()
    h.sync.start()
    h.sync.update(['LOCAL', 'X'])
    h.sync.update(['LOCAL'])
    h.get.resolve(['SERVER'])
    await vi.advanceTimersByTimeAsync(1000)
    expect(h.remote).toEqual([['SERVER']])
    expect(h.saves).toEqual([])
  })

  it('W3: a pending edit is PUT when the server copy is invalid', async () => {
    const h = harness()
    h.sync.start()
    h.sync.update(['EDIT'])
    h.get.resolve({ not: 'a list' })
    await vi.advanceTimersByTimeAsync(300)
    expect(h.saves).toEqual([['EDIT']])
  })

  it('debounces edits and sends only the latest', async () => {
    const h = harness()
    h.sync.start()
    h.get.resolve(['S'])
    await vi.advanceTimersByTimeAsync(0)
    h.sync.update(['S', 'A'])
    await vi.advanceTimersByTimeAsync(200)
    h.sync.update(['S', 'A', 'B'])
    await vi.advanceTimersByTimeAsync(200)
    h.sync.update(['S', 'A', 'B', 'C'])
    expect(h.saves).toEqual([])
    await vi.advanceTimersByTimeAsync(300)
    expect(h.saves).toEqual([['S', 'A', 'B', 'C']])
  })

  it('cancels a pending PUT when the value returns to the server copy', async () => {
    const h = harness()
    h.sync.start()
    h.get.resolve(['S'])
    await vi.advanceTimersByTimeAsync(0)
    h.sync.update(['S', 'A'])
    h.sync.update(['S'])
    await vi.advanceTimersByTimeAsync(1000)
    expect(h.saves).toEqual([])
  })

  it('keeps one PUT in flight and sends the latest value after it', async () => {
    const h = harness()
    h.sync.start()
    h.get.resolve(['S'])
    await vi.advanceTimersByTimeAsync(0)
    h.sync.update(['A'])
    await vi.advanceTimersByTimeAsync(300)
    expect(h.saves).toEqual([['A']])
    h.sync.update(['B'])
    await vi.advanceTimersByTimeAsync(300)
    expect(h.saves).toHaveLength(1) // still in flight
    h.saveResults[0].resolve({})
    await vi.advanceTimersByTimeAsync(300)
    expect(h.saves).toEqual([['A'], ['B']])
  })

  it('W4: retries a failed PUT with backoff (1 s, 2 s, 4 s), then waits for the next edit', async () => {
    const h = harness({ retryBaseMs: 1000 })
    h.sync.start()
    h.get.resolve(['S'])
    await vi.advanceTimersByTimeAsync(0)
    h.sync.update(['A'])
    await vi.advanceTimersByTimeAsync(300)
    expect(h.saves).toHaveLength(1)
    h.saveResults[0].reject(new Error('502'))
    await vi.advanceTimersByTimeAsync(999)
    expect(h.saves).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(h.saves).toHaveLength(2)
    h.saveResults[1].reject(new Error('502'))
    await vi.advanceTimersByTimeAsync(1999)
    expect(h.saves).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(1)
    expect(h.saves).toHaveLength(3)
    h.saveResults[2].reject(new Error('502'))
    await vi.advanceTimersByTimeAsync(4000)
    expect(h.saves).toHaveLength(4)
    h.saveResults[3].reject(new Error('502'))
    // Retries exhausted: no retry storm.
    await vi.advanceTimersByTimeAsync(10 * 60_000)
    expect(h.saves).toHaveLength(4)
    expect(h.sync.state).toBe('synced')
    // The next edit tries again, with a fresh retry budget.
    h.sync.update(['A', 'B'])
    await vi.advanceTimersByTimeAsync(300)
    expect(h.saves).toHaveLength(5)
    expect(h.saves[4]).toEqual(['A', 'B'])
    h.saveResults[4].reject(new Error('502'))
    await vi.advanceTimersByTimeAsync(1000)
    expect(h.saves).toHaveLength(6)
    h.saveResults[5].resolve({})
    await vi.advanceTimersByTimeAsync(60_000)
    expect(h.saves).toHaveLength(6)
  })

  it('W4: a failed PUT with a newer value queued retries with the latest value', async () => {
    const h = harness({ retryBaseMs: 1000 })
    h.sync.start()
    h.get.resolve(['S'])
    await vi.advanceTimersByTimeAsync(0)
    h.sync.update(['A'])
    await vi.advanceTimersByTimeAsync(300)
    h.sync.update(['B']) // queued while A is in flight
    await vi.advanceTimersByTimeAsync(300)
    expect(h.saves).toEqual([['A']])
    h.saveResults[0].reject(new Error('502'))
    await vi.advanceTimersByTimeAsync(1000)
    expect(h.saves).toEqual([['A'], ['B']])
    h.saveResults[1].resolve({})
    await vi.advanceTimersByTimeAsync(60_000)
    expect(h.saves).toEqual([['A'], ['B']])
  })

  it('W4: an edit during the backoff does not shorten it; the retry sends the latest', async () => {
    const h = harness({ retryBaseMs: 1000 })
    h.sync.start()
    h.get.resolve(['S'])
    await vi.advanceTimersByTimeAsync(0)
    h.sync.update(['A'])
    await vi.advanceTimersByTimeAsync(300)
    h.saveResults[0].reject(new Error('502'))
    await vi.advanceTimersByTimeAsync(0)
    h.sync.update(['C'])
    await vi.advanceTimersByTimeAsync(500)
    expect(h.saves).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(500)
    expect(h.saves).toEqual([['A'], ['C']])
  })

  it('W4: a successful PUT resets the retry budget', async () => {
    const h = harness({ retryBaseMs: 1000 })
    h.sync.start()
    h.get.resolve(['S'])
    await vi.advanceTimersByTimeAsync(0)
    h.sync.update(['A'])
    await vi.advanceTimersByTimeAsync(300)
    h.saveResults[0].reject(new Error('502'))
    await vi.advanceTimersByTimeAsync(1000)
    h.saveResults[1].resolve({})
    await vi.advanceTimersByTimeAsync(0)
    h.sync.update(['B'])
    await vi.advanceTimersByTimeAsync(300)
    h.saveResults[2].reject(new Error('502'))
    // First retry again (1 s), not the second (2 s).
    await vi.advanceTimersByTimeAsync(1000)
    expect(h.saves).toEqual([['A'], ['A'], ['B'], ['B']])
  })

  it('W2: adopt() after sync records the value as the server copy: no echo PUT', async () => {
    const h = harness()
    h.sync.start()
    h.get.resolve(['S'])
    await vi.advanceTimersByTimeAsync(0)
    h.sync.adopt(['OTHER'])
    h.sync.update(['OTHER']) // the hook reports the replaced state
    await vi.advanceTimersByTimeAsync(5000)
    expect(h.saves).toEqual([])
  })

  it('W2: adopt() cancels a queued PUT of the stale value', async () => {
    const h = harness()
    h.sync.start()
    h.get.resolve(['S'])
    await vi.advanceTimersByTimeAsync(0)
    h.sync.update(['STALE'])
    await vi.advanceTimersByTimeAsync(100)
    h.sync.adopt(['OTHER'])
    h.sync.update(['OTHER'])
    await vi.advanceTimersByTimeAsync(5000)
    expect(h.saves).toEqual([])
  })

  it('W2: adopt() while a stale PUT is in flight re-sends the adopted value after it', async () => {
    const h = harness()
    h.sync.start()
    h.get.resolve(['S'])
    await vi.advanceTimersByTimeAsync(0)
    h.sync.update(['STALE'])
    await vi.advanceTimersByTimeAsync(300)
    expect(h.saves).toEqual([['STALE']])
    h.sync.adopt(['OTHER'])
    h.sync.update(['OTHER'])
    h.saveResults[0].resolve({})
    await vi.advanceTimersByTimeAsync(300)
    expect(h.saves).toEqual([['STALE'], ['OTHER']])
  })

  it('W2: adopt() while pending clears a dirty edit, so the server copy is adopted', async () => {
    const h = harness()
    h.sync.start()
    h.sync.update(['MY-EDIT'])
    h.sync.adopt(['OTHER'])
    h.sync.update(['OTHER'])
    h.get.resolve(['SERVER'])
    await vi.advanceTimersByTimeAsync(1000)
    expect(h.saves).toEqual([])
    expect(h.remote).toEqual([['SERVER']])
  })

  it('W2: adopt() while pending then 404 migrates the adopted value', async () => {
    const h = harness()
    h.sync.start()
    h.sync.adopt(['OTHER'])
    h.get.resolve(null)
    await vi.advanceTimersByTimeAsync(0)
    expect(h.saves).toEqual([['OTHER']])
  })

  it('dispose cancels timers, flushes nothing, and ignores a late GET', async () => {
    const h = harness()
    h.sync.start()
    h.get.resolve(['S'])
    await vi.advanceTimersByTimeAsync(0)
    h.sync.update(['A'])
    h.sync.dispose()
    await vi.advanceTimersByTimeAsync(1000)
    expect(h.saves).toEqual([])
    expect(h.sync.state).toBe('disposed')

    const late = harness()
    late.sync.start()
    late.sync.dispose()
    late.get.resolve(['S'])
    await vi.advanceTimersByTimeAsync(1000)
    expect(late.remote).toEqual([])
    expect(late.saves).toEqual([])

    const late404 = harness()
    late404.sync.start()
    late404.sync.dispose()
    late404.get.resolve(null)
    await vi.advanceTimersByTimeAsync(1000)
    expect(late404.saves).toEqual([])
  })
})
