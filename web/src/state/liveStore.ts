import { useMemo, useSyncExternalStore } from 'react'
import type { Candle, ConnectionStatus, Timeframe } from '../types'
import type { PanelState } from './layout'
import { initialSnapshot } from './liveCandles'
import { priceStamp, type PanelFeed, type PriceStamp } from './paperFeeds'
import type { UseLiveCandles } from './useLiveCandles'

/**
 * The live candles every chart panel reports (ChartPanel's `onLive`), kept outside React state.
 *
 * Each panel's `useLiveCandles` snapshot changes on every forming-bar tick. Storing it in App
 * state re-rendered the whole app shell per tick; here it lives in a small external store that
 * components read with `useLiveSelector`, which re-renders a component only when the value it
 * selects changes (the quote's text, the status bar's clock, the paper ticket's price). App's
 * root subscribes only to values that change rarely (a bar count once per bar, a status change,
 * a history page), and handlers read the latest state at call time (`getState`).
 */

/** The market a live report belongs to. */
export interface PanelMarket {
  symbol: string
  interval: Timeframe
}

/** A panel's last live report and the market it belongs to. */
export interface PanelLive extends PanelMarket {
  live: UseLiveCandles
  /** When its latest price last changed (paper trading ticks and order times). */
  stamp: PriceStamp | null
}

/** Every panel's last report, by panel id. Replaced (never mutated) on every change. */
export type LiveState = Readonly<Record<string, PanelLive>>

/** The part of a panel a live report is looked up by. */
export type PanelKey = Pick<PanelState, 'id' | 'symbol' | 'interval'>

/** Live state of a panel that has not reported yet (or is not mounted). */
export const IDLE_LIVE: UseLiveCandles = {
  ...initialSnapshot,
  reload: () => {},
  loadOlder: () => {},
}

const EMPTY: LiveState = {}

/** Same keys, same values (Object.is): a panel's re-render without new live data. */
export function sameLive(a: UseLiveCandles, b: UseLiveCandles): boolean {
  const keys = Object.keys(a) as (keyof UseLiveCandles)[]
  return keys.length === Object.keys(b).length && keys.every((k) => Object.is(a[k], b[k]))
}

/**
 * The panel's stored report, or undefined while there is none or it is still the previous
 * market's (the render right after a symbol or timeframe change): those candles are another
 * market's.
 */
export function panelEntry(state: LiveState, panel: PanelKey): PanelLive | undefined {
  const e = state[panel.id]
  return e !== undefined && e.symbol === panel.symbol && e.interval === panel.interval
    ? e
    : undefined
}

/** The panel's live state, or IDLE_LIVE while it has none for its current market. */
export function panelLive(state: LiveState, panel: PanelKey): UseLiveCandles {
  return panelEntry(state, panel)?.live ?? IDLE_LIVE
}

/** Every loaded bar, traded or not (pagination cursor, history planning, the quote). */
export function rawCandles(live: UseLiveCandles): readonly Candle[] | null {
  return live.load.kind === 'ready' ? live.load.candles : null
}

/**
 * The last traded price. The last candle may still be forming; it is a display/default value
 * only. A bar without trades carries the previous close, so the raw last close is it.
 */
export function lastCloseOf(live: UseLiveCandles): number | null {
  const candles = rawCandles(live)
  return candles !== null && candles.length > 0 ? candles[candles.length - 1].close : null
}

/** Open time of the last plotted bar. */
export function lastBarTimeOf(live: UseLiveCandles): number | null {
  const bars = live.load.kind === 'ready' ? live.load.bars : null
  return bars !== null && bars.length > 0 ? bars[bars.length - 1].time : null
}

/**
 * `prev` with panel `panelId`'s report applied: removed for `live === null` (or no market),
 * else stored with its price stamp. Returns `prev` itself when nothing changed (the same market
 * and a `sameLive` report), so a re-render without new data notifies no one.
 */
export function applyReport(
  prev: LiveState,
  panelId: string,
  live: UseLiveCandles | null,
  market: PanelMarket | undefined,
  nowMs: number,
): LiveState {
  const cur = prev[panelId]
  if (live === null || market === undefined) {
    if (cur === undefined) return prev
    const next = { ...prev }
    delete next[panelId]
    return next
  }
  if (
    cur !== undefined &&
    cur.symbol === market.symbol &&
    cur.interval === market.interval &&
    sameLive(cur.live, live)
  ) {
    return prev
  }
  // Stamped with the wall-clock time the last bar's close changed, never on a history load or
  // refresh that leaves it as it was (see priceStamp).
  const stamp = priceStamp(cur?.stamp ?? null, market.symbol, rawCandles(live), nowMs)
  return {
    ...prev,
    [panelId]: { live, symbol: market.symbol, interval: market.interval, stamp },
  }
}

/** The paper feed state of every panel in `panels` that has a report for its market. */
export function panelFeedsOf(state: LiveState, panels: readonly PanelKey[]): PanelFeed[] {
  const out: PanelFeed[] = []
  for (const p of panels) {
    const e = panelEntry(state, p)
    if (e !== undefined) {
      out.push({ panelId: p.id, symbol: p.symbol, status: e.live.status, stamp: e.stamp })
    }
  }
  return out
}

export interface LiveStore {
  /** The latest state; the same object until a report changes something. */
  getState(): LiveState
  /** Called after every change; returns the unsubscribe. */
  subscribe(listener: () => void): () => void
  /** ChartPanel's `onLive`: a panel's report (null on unmount). Stable for the store's life. */
  report(panelId: string, live: UseLiveCandles | null, market?: PanelMarket): void
}

/** A live store. `now` (ms) stamps price changes; injectable for tests. */
export function createLiveStore(now: () => number = Date.now): LiveStore {
  let state: LiveState = EMPTY
  const listeners = new Set<() => void>()
  return {
    getState: () => state,
    subscribe: (listener) => {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
    report: (panelId, live, market) => {
      const next = applyReport(state, panelId, live, market, now())
      if (next === state) return
      state = next
      // A copy: a listener may unsubscribe (or subscribe) while we notify. One that throws must
      // not starve the rest (the quote, status bar and dialogs would freeze until the next report).
      for (const l of [...listeners]) {
        try {
          l()
        } catch (err) {
          console.error('liveStore: listener threw', err)
        }
      }
    },
  }
}

/**
 * A selector over one panel: `select(live, entry)` for the panel's current market, recomputed
 * only when that panel's entry changes (another panel's tick returns the cached value). `live`
 * is IDLE_LIVE and `entry` undefined while the panel has no report for its market.
 */
export function panelSelector<T>(
  panel: PanelKey,
  select: (live: UseLiveCandles, entry: PanelLive | undefined) => T,
): (state: LiveState) => T {
  let has = false
  let lastEntry: PanelLive | undefined
  let last: T
  return (state) => {
    const entry = panelEntry(state, panel)
    if (has && entry === lastEntry) return last
    lastEntry = entry
    last = select(entry?.live ?? IDLE_LIVE, entry)
    has = true
    return last
  }
}

/** Flat objects with the same keys and Object.is-equal values. */
export function shallowEqual<T extends object>(a: T, b: T): boolean {
  if (a === b) return true
  const ka = Object.keys(a) as (keyof T)[]
  return (
    ka.length === Object.keys(b).length &&
    ka.every((k) => Object.prototype.hasOwnProperty.call(b, k) && Object.is(a[k], b[k]))
  )
}

/**
 * A `getSnapshot` for useSyncExternalStore: `select(getState())`, recomputed only when the state
 * object changes, and the previous result kept while `isEqual` says the new one is the same (so
 * a selector that builds an object does not re-render on every tick).
 */
export function memoSelector<S, T>(
  getState: () => S,
  select: (state: S) => T,
  isEqual: (a: T, b: T) => boolean = Object.is,
): () => T {
  let has = false
  let lastState: S
  let last: T
  return () => {
    const state = getState()
    if (has && Object.is(state, lastState)) return last
    const next = select(state)
    lastState = state
    if (!has || !isEqual(last, next)) {
      last = next
      has = true
    }
    return last
  }
}

/**
 * `select` of the live store's state, re-rendering only when it changes (by `isEqual`). Pass a
 * stable `select` (useCallback): a new function starts a fresh memo. Prefer primitives; for
 * objects pass an `isEqual` such as `shallowEqual`.
 */
export function useLiveSelector<T>(
  store: LiveStore,
  select: (state: LiveState) => T,
  isEqual: (a: T, b: T) => boolean = Object.is,
): T {
  const getSnapshot = useMemo(
    () => memoSelector(store.getState, select, isEqual),
    [store, select, isEqual],
  )
  return useSyncExternalStore(store.subscribe, getSnapshot, getSnapshot)
}

/** Header props that follow the active chart's live state (the quote is a leaf of its own). */
export interface HeaderLive {
  status: ConnectionStatus
  loading: boolean
  onRefresh: () => void
  refreshDisabled: boolean
}

/**
 * `panel`'s Header state. Pair with `shallowEqual`: a forming-bar tick leaves every field as it
 * was, so the Header around the quote does not re-render.
 */
export function headerLiveSelector(panel: PanelKey): (state: LiveState) => HeaderLive {
  return panelSelector(panel, (live) => ({
    status: live.status,
    loading: live.load.kind === 'loading' || live.refreshing,
    onRefresh: live.reload,
    refreshDisabled: live.retryBlocked,
  }))
}

/** Status bar props that follow the active chart's live state (the clock is a leaf). */
export interface StatusBarLive {
  /** Plotted bars (bars without trades are left out, as on TradingView), or null. */
  barCount: number | null
  status: ConnectionStatus
  refreshError: string | null
  /** Short name of the display zone, read at each report (a DST switch renames it). */
  timezoneName: string
}

/**
 * `panel`'s status bar state; `zoneName()` gives the display zone's short name now. Pair with
 * `shallowEqual`: a forming-bar tick changes none of it (the bar count moves once per bar).
 */
export function statusBarSelector(
  panel: PanelKey,
  zoneName: () => string,
): (state: LiveState) => StatusBarLive {
  return panelSelector(panel, (live) => ({
    barCount: live.load.kind === 'ready' ? live.load.bars.length : null,
    status: live.status,
    refreshError: live.load.kind === 'ready' ? live.refreshError : null,
    timezoneName: zoneName(),
  }))
}
