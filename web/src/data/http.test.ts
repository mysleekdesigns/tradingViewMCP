import { describe, expect, it, vi } from 'vitest'
import {
  SharedLoad,
  bodyString,
  clampLimit,
  exchangeError,
  fetchWithin,
  jsonInit,
  parseRetryAfter,
  readJsonOrUndefined,
  requestMarketJson,
  withAbort,
} from './http'
import { MarketDataError } from './provider'

const res = (status: number, headers: Record<string, string> = {}, body: string | null = null) =>
  new Response(body, { status, headers })

describe('http helpers', () => {
  it('parseRetryAfter: delta-seconds only', () => {
    expect(parseRetryAfter(res(429, { 'Retry-After': '7' }))).toBe(7)
    expect(parseRetryAfter(res(429, { 'Retry-After': '0' }))).toBe(0)
    expect(parseRetryAfter(res(429, { 'Retry-After': '1.5' }))).toBe(1.5)
    expect(parseRetryAfter(res(429, { 'Retry-After': '-1' }))).toBeUndefined()
    expect(parseRetryAfter(res(429, { 'Retry-After': 'soon' }))).toBeUndefined()
    expect(parseRetryAfter(res(429, { 'Retry-After': ' ' }))).toBeUndefined()
    expect(parseRetryAfter(res(429))).toBeUndefined()
  })

  it('clampLimit', () => {
    expect(clampLimit(5000)).toBe(1000)
    expect(clampLimit(0)).toBe(1)
    expect(clampLimit(12.9)).toBe(12)
    expect(clampLimit(Number.NaN)).toBe(500)
    expect(clampLimit(Infinity)).toBe(500)
    expect(clampLimit(50, 20, 7)).toBe(20)
    expect(clampLimit(Number.NaN, 20, 7)).toBe(7)
  })

  it('withAbort rejects only the aborted caller', async () => {
    let resolve: (v: number) => void = () => undefined
    const p = new Promise<number>((r) => {
      resolve = r
    })
    const ctl = new AbortController()
    const a = withAbort(p, ctl.signal, 'things')
    const b = withAbort(p, undefined, 'things')
    ctl.abort()
    resolve(3)
    await expect(a).rejects.toMatchObject({
      kind: 'aborted',
      message: 'Request for things was cancelled',
    })
    await expect(b).resolves.toBe(3)
    const pre = new AbortController()
    pre.abort()
    await expect(withAbort(Promise.resolve(1), pre.signal, 'x')).rejects.toBeInstanceOf(
      MarketDataError,
    )
    // A non-Error rejection is wrapped.
    await expect(
      withAbort(Promise.reject('boom'), new AbortController().signal, 'x'),
    ).rejects.toThrow('boom')
  })

  it('SharedLoad: one load, retried after a failure, pre-aborted callers never start it', async () => {
    let n = 0
    const load = vi.fn(() => {
      n += 1
      return n === 1 ? Promise.reject(new Error('first fails')) : Promise.resolve(n)
    })
    const shared = new SharedLoad(load)
    const pre = new AbortController()
    pre.abort()
    await expect(shared.join(pre.signal, 'list')).rejects.toMatchObject({ kind: 'aborted' })
    expect(load).not.toHaveBeenCalled()
    await expect(shared.get()).rejects.toThrow('first fails')
    await Promise.resolve()
    const [x, y] = await Promise.all([shared.join(undefined, 'list'), shared.get()])
    expect([x, y]).toEqual([2, 2])
    expect(load).toHaveBeenCalledTimes(2)
  })

  it('readJsonOrUndefined / bodyString / jsonInit', async () => {
    expect(await readJsonOrUndefined(res(200, {}, '{"a":1}'))).toEqual({ a: 1 })
    expect(await readJsonOrUndefined(res(200, {}, 'nope'))).toBeUndefined()
    expect(bodyString({ msg: 'm' }, 'msg')).toBe('m')
    expect(bodyString({ msg: 1 }, 'msg')).toBeUndefined()
    expect(bodyString([], 'msg')).toBeUndefined()
    expect(jsonInit('GET')).toEqual({ method: 'GET' })
    expect(jsonInit('PUT', { a: 1 })).toEqual({
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: '{"a":1}',
    })
  })

  it('exchangeError maps the shared statuses and defers the rest to the rules', async () => {
    const onRateLimited = vi.fn()
    const rules = {
      detail: (b: unknown) => bodyString(b, 'msg'),
      badSymbol: (s: number) => (s === 400 ? 'bad sym' : null),
      onRateLimited,
    }
    const e429 = await exchangeError(res(429, { 'Retry-After': '3' }), 'x', rules)
    expect(e429).toMatchObject({ kind: 'rate-limited', status: 429, retryAfter: 3 })
    expect(onRateLimited).toHaveBeenCalledWith(3)
    expect(await exchangeError(res(418, { 'Retry-After': '9' }), 'x', rules)).toMatchObject({
      kind: 'banned',
      retryAfter: 9,
    })
    const e451 = await exchangeError(res(451, { 'Retry-After': '9' }), 'x', rules)
    expect(e451).toMatchObject({ kind: 'region-blocked', status: 451, retryAfter: undefined })
    expect(await exchangeError(res(400), 'x', rules)).toMatchObject({
      kind: 'bad-symbol',
      message: 'bad sym',
      status: 400,
    })
    expect(await exchangeError(res(500, {}, '{"msg":"down"}'), 'x', rules)).toMatchObject({
      kind: 'http',
      message: 'HTTP 500 fetching x: down',
    })
    expect(await exchangeError(res(500, {}, 'down'), 'x', rules)).toMatchObject({
      message: 'HTTP 500 fetching x',
    })
  })

  it('requestMarketJson: callerAbortWins rejects before any request', async () => {
    const fetchFn = vi.fn(() => Promise.resolve(res(200, {}, '[]')))
    const ctl = new AbortController()
    ctl.abort()
    const base = {
      fetchFn: fetchFn as unknown as typeof fetch,
      url: 'u',
      what: 'w',
      timeoutMs: 1000,
      signal: ctl.signal,
      toError: () => Promise.resolve(new MarketDataError('http', 'x')),
    }
    await expect(requestMarketJson({ ...base, callerAbortWins: true })).rejects.toMatchObject({
      kind: 'aborted',
    })
    expect(fetchFn).not.toHaveBeenCalled()
    // Without it the request goes out (a real fetch then rejects on the aborted signal).
    await expect(requestMarketJson(base)).resolves.toEqual([])
    expect(fetchFn).toHaveBeenCalledWith('u', { signal: expect.any(AbortSignal) })
  })

  it('requestMarketJson sends cache only when set', async () => {
    const fetchFn = vi.fn(() => Promise.resolve(res(200, {}, '{}')))
    await requestMarketJson({
      fetchFn: fetchFn as unknown as typeof fetch,
      url: 'u',
      what: 'w',
      timeoutMs: 1000,
      cache: 'no-cache',
      toError: () => Promise.resolve(new MarketDataError('http', 'x')),
    })
    expect(fetchFn).toHaveBeenCalledWith('u', {
      signal: expect.any(AbortSignal),
      cache: 'no-cache',
    })
  })

  it('fetchWithin: timeout wins, then caller abort, then network; timer released', async () => {
    vi.useFakeTimers()
    try {
      const failures = {
        aborted: () => new Error('aborted'),
        timeout: () => new Error('timeout'),
        network: (e: unknown) => new Error(`network: ${String(e)}`),
      }
      const hang = (_u: string, init?: RequestInit) =>
        new Promise<Response>((_r, reject) =>
          init?.signal?.addEventListener('abort', () => reject(new Error('AbortError'))),
        )
      const t = fetchWithin({ fetchImpl: hang, url: 'u', init: {}, timeoutMs: 50, failures }, () =>
        Promise.resolve(1),
      )
      const tCheck = expect(t).rejects.toThrow('timeout')
      await vi.advanceTimersByTimeAsync(50)
      await tCheck

      const ctl = new AbortController()
      const a = fetchWithin(
        { fetchImpl: hang, url: 'u', init: {}, timeoutMs: 50, signal: ctl.signal, failures },
        () => Promise.resolve(1),
      )
      ctl.abort()
      await expect(a).rejects.toThrow('aborted')

      const n = fetchWithin(
        { fetchImpl: () => Promise.reject('down'), url: 'u', init: {}, timeoutMs: 50, failures },
        () => Promise.resolve(1),
      )
      await expect(n).rejects.toThrow('network: down')

      let seen: boolean | null = null
      const ok = await fetchWithin(
        {
          fetchImpl: (_u, init) => {
            expect(init).toEqual({ method: 'GET', signal: expect.any(AbortSignal) })
            return Promise.resolve(res(204))
          },
          url: 'u',
          init: { method: 'GET' },
          timeoutMs: 50,
          failures,
        },
        (r, d) => {
          seen = d.timedOut
          return Promise.resolve(r.status)
        },
      )
      expect(ok).toBe(204)
      expect(seen).toBe(false)
      expect(vi.getTimerCount()).toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })
})
