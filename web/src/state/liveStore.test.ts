import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import type { Candle } from '../types'
import { initialSnapshot } from './liveCandles'
import {
  IDLE_LIVE,
  applyReport,
  createLiveStore,
  lastBarTimeOf,
  lastCloseOf,
  memoSelector,
  panelFeedsOf,
  panelLive,
  panelSelector,
  rawCandles,
  sameLive,
  shallowEqual,
  useLiveSelector,
  type LiveState,
  type LiveStore,
} from './liveStore'
import type { UseLiveCandles } from './useLiveCandles'

const T = 1_700_000_000
const reload = () => {}
const loadOlder = () => {}

function bar(time: number, close: number, volume = 1): Candle {
  return { time, open: close, high: close, low: close, close, volume }
}

/** A ready live report over `candles` (all plotted unless `bars` is given). */
function ready(
  candles: readonly Candle[],
  over: Partial<UseLiveCandles> = {},
  bars: readonly Candle[] = candles,
): UseLiveCandles {
  return {
    ...initialSnapshot,
    load: { kind: 'ready', candles, bars },
    status: 'live',
    reload,
    loadOlder,
    ...over,
  }
}

const BTC = { symbol: 'BTCUSDT', interval: '1m' as const }
const P0 = { id: 'p0', ...BTC }
const P1 = { id: 'p1', symbol: 'ETHUSDT', interval: '1m' as const }

describe('live report helpers', () => {
  it('reads raw candles, the last close and the last plotted bar time', () => {
    const candles = [bar(T, 10), bar(T + 60, 11, 0), bar(T + 120, 12)]
    const live = ready(candles, {}, [candles[0], candles[2]])
    expect(rawCandles(live)).toBe(candles)
    expect(lastCloseOf(live)).toBe(12)
    expect(lastBarTimeOf(live)).toBe(T + 120)
    expect(rawCandles(IDLE_LIVE)).toBeNull()
    expect(lastCloseOf(IDLE_LIVE)).toBeNull()
    expect(lastBarTimeOf(ready([]))).toBeNull()
  })

  it('compares reports field by field', () => {
    const live = ready([bar(T, 10)])
    expect(sameLive(live, { ...live })).toBe(true)
    // A new load object (a tick) is new data even with equal bars.
    expect(sameLive(live, { ...live, load: { ...live.load } })).toBe(false)
    expect(sameLive(live, { ...live, status: 'reconnecting' })).toBe(false)
  })
})

describe('applyReport', () => {
  const candles = [bar(T, 10), bar(T + 60, 11)]

  it('stores a report with its price stamp and keeps the state for an unchanged one', () => {
    const live = ready(candles)
    const s1 = applyReport({}, 'p0', live, BTC, 5_000)
    expect(s1.p0).toEqual({
      live,
      ...BTC,
      stamp: { symbol: 'BTCUSDT', barTime: T + 60, price: 11, at: 5 },
    })
    // A new object with the same fields (a panel re-render): nothing changes.
    expect(applyReport(s1, 'p0', { ...live }, BTC, 9_000)).toBe(s1)
  })

  it('keeps the stamp while the last close is unchanged and restamps on a new price', () => {
    const s1 = applyReport({}, 'p0', ready(candles), BTC, 5_000)
    // A refresh with the same last bar: new report, same stamp.
    const s2 = applyReport(s1, 'p0', ready(candles, { refreshing: true }), BTC, 7_000)
    expect(s2).not.toBe(s1)
    expect(s2.p0.stamp).toBe(s1.p0.stamp)
    const s3 = applyReport(s2, 'p0', ready([candles[0], bar(T + 60, 12)]), BTC, 8_000)
    expect(s3.p0.stamp).toEqual({ symbol: 'BTCUSDT', barTime: T + 60, price: 12, at: 8 })
  })

  it('removes a panel on null and leaves the other panels as they were', () => {
    const s1 = applyReport({}, 'p0', ready(candles), BTC, 1_000)
    const s2 = applyReport(s1, 'p1', ready(candles), P1, 1_000)
    const s3 = applyReport(s2, 'p0', null, undefined, 2_000)
    expect(Object.keys(s3)).toEqual(['p1'])
    expect(s3.p1).toBe(s2.p1)
    expect(applyReport(s3, 'p0', null, undefined, 3_000)).toBe(s3)
  })

  it('stores the same report again under a new market', () => {
    const s1 = applyReport({}, 'p0', ready(candles), BTC, 1_000)
    const s2 = applyReport(s1, 'p0', s1.p0.live, { ...BTC, interval: '5m' }, 1_000)
    expect(s2).not.toBe(s1)
    expect(s2.p0.interval).toBe('5m')
  })
})

describe('panelLive / panelFeedsOf', () => {
  const s: LiveState = applyReport({}, 'p0', ready([bar(T, 10)]), BTC, 1_000)

  it('returns IDLE_LIVE while the stored report is another market’s', () => {
    expect(panelLive(s, P0)).toBe(s.p0.live)
    expect(panelLive(s, { ...P0, interval: '5m' })).toBe(IDLE_LIVE)
    expect(panelLive(s, { ...P0, symbol: 'ETHUSDT' })).toBe(IDLE_LIVE)
    expect(panelLive(s, P1)).toBe(IDLE_LIVE)
  })

  it('lists the feed of every panel with a report for its market', () => {
    expect(panelFeedsOf(s, [P0, P1, { ...P0, id: 'p0', interval: '5m' }])).toEqual([
      { panelId: 'p0', symbol: 'BTCUSDT', status: 'live', stamp: s.p0.stamp },
    ])
  })
})

describe('createLiveStore', () => {
  it('notifies subscribers on a change only, with a new state object', () => {
    const store = createLiveStore(() => 1_000)
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    const s0 = store.getState()
    expect(store.getState()).toBe(s0)

    const live = ready([bar(T, 10)])
    store.report('p0', live, BTC)
    expect(listener).toHaveBeenCalledTimes(1)
    const s1 = store.getState()
    expect(s1).not.toBe(s0)
    expect(s1.p0.live).toBe(live)

    // The same report again (a re-render without new data): no notify, same state.
    store.report('p0', { ...live }, BTC)
    expect(listener).toHaveBeenCalledTimes(1)
    expect(store.getState()).toBe(s1)

    store.report('p0', null)
    expect(listener).toHaveBeenCalledTimes(2)
    expect(store.getState().p0).toBeUndefined()

    unsubscribe()
    store.report('p0', live, BTC)
    expect(listener).toHaveBeenCalledTimes(2)
  })

  it('lets a listener unsubscribe while notified', () => {
    const store = createLiveStore(() => 0)
    const second = vi.fn()
    const off: (() => void)[] = []
    off.push(store.subscribe(() => off[0]()))
    off.push(store.subscribe(second))
    store.report('p0', ready([bar(T, 1)]), BTC)
    store.report('p0', ready([bar(T, 2)]), BTC)
    expect(second).toHaveBeenCalledTimes(2)
  })

  it('a listener that throws does not stop the others being notified', () => {
    const store = createLiveStore(() => 0)
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    const after = vi.fn()
    store.subscribe(() => {
      throw new Error('boom')
    })
    store.subscribe(after)
    expect(() => store.report('p0', ready([bar(T, 1)]), BTC)).not.toThrow()
    expect(after).toHaveBeenCalledTimes(1)
    expect(error).toHaveBeenCalledTimes(1)
    error.mockRestore()
  })
})

describe('selectors', () => {
  it('memoSelector recomputes only on a new state and keeps an equal result', () => {
    let state = { a: 1, b: 2 }
    const select = vi.fn((s: { a: number; b: number }) => ({ a: s.a }))
    const get = memoSelector(() => state, select, shallowEqual)
    const first = get()
    expect(get()).toBe(first)
    expect(select).toHaveBeenCalledTimes(1)
    state = { a: 1, b: 3 }
    expect(get()).toBe(first)
    expect(select).toHaveBeenCalledTimes(2)
    state = { a: 2, b: 3 }
    expect(get()).toEqual({ a: 2 })
    expect(get()).not.toBe(first)
  })

  it('panelSelector reruns only when its own panel’s entry changes', () => {
    const store = createLiveStore(() => 0)
    const select = vi.fn((live: UseLiveCandles) => lastCloseOf(live))
    const sel = panelSelector(P0, select)
    expect(sel(store.getState())).toBeNull()
    store.report('p0', ready([bar(T, 10)]), BTC)
    expect(sel(store.getState())).toBe(10)
    expect(select).toHaveBeenCalledTimes(2)
    // Another panel's tick: cached.
    store.report('p1', ready([bar(T, 99)]), P1)
    expect(sel(store.getState())).toBe(10)
    store.report('p1', ready([bar(T, 98)]), P1)
    expect(sel(store.getState())).toBe(10)
    expect(select).toHaveBeenCalledTimes(2)
    // Its own tick.
    store.report('p0', ready([bar(T, 11)]), BTC)
    expect(sel(store.getState())).toBe(11)
    expect(select).toHaveBeenCalledTimes(3)
  })

  it('panelSelector passes the entry (price stamp) along with the report', () => {
    const store = createLiveStore(() => 4_000)
    store.report('p0', ready([bar(T, 10)]), BTC)
    const sel = panelSelector(P0, (_live, entry) => entry?.stamp?.at ?? null)
    expect(sel(store.getState())).toBe(4)
    expect(panelSelector(P1, (_live, entry) => entry)(store.getState())).toBeUndefined()
  })

  it('shallowEqual compares flat objects by key and Object.is', () => {
    const f = () => {}
    expect(shallowEqual({ a: 1, f }, { a: 1, f })).toBe(true)
    expect(shallowEqual({ a: 1 }, { a: 1, b: undefined } as { a: number })).toBe(false)
    expect(shallowEqual({ a: Number.NaN }, { a: Number.NaN })).toBe(true)
    expect(shallowEqual({ a: 1, f }, { a: 1, f: () => {} })).toBe(false)
  })

  it('useLiveSelector renders the selected value of the current state', () => {
    const store = createLiveStore(() => 0)
    store.report('p0', ready([bar(T, 42)]), BTC)
    const select = (s: LiveState) => lastCloseOf(panelLive(s, P0))
    function Price({ liveStore }: { liveStore: LiveStore }) {
      return createElement('span', null, String(useLiveSelector(liveStore, select)))
    }
    expect(renderToStaticMarkup(createElement(Price, { liveStore: store }))).toBe('<span>42</span>')
  })
})
