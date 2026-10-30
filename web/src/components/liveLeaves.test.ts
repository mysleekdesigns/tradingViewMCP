import { createElement as h, type ComponentType } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { formatClock } from '../state/appSettings'
import { initialSnapshot } from '../state/liveCandles'
import {
  createLiveStore,
  headerLiveSelector,
  memoSelector,
  shallowEqual,
  statusBarSelector,
  type LiveStore,
} from '../state/liveStore'
import type { UseLiveCandles } from '../state/useLiveCandles'
import type { Candle } from '../types'
import { Header } from './Header'
import { LastUpdateClock, LastUpdateText } from './LastUpdateClock'
import { lastUpdateSelector, liveQuoteSelector, NO_QUOTE, sameQuote, summarizeQuote } from './quote'
import { QuoteReadout, QuoteView } from './QuoteReadout'
import { StatusBar } from './StatusBar'

/*
 * Phase 19 Wave 1: the ticking text (header quote, status-bar clock) lives in leaves that read
 * the live store themselves. A forming-bar tick must change only the leaves' snapshots, never
 * the Header's or StatusBar's (their wrappers' useSyncExternalStore then skips the render).
 */

const MEMO = Symbol.for('react.memo')
const noop = () => {}
const reload = () => {}
const loadOlder = () => {}
const T = 1_700_000_000
const T0_MS = Date.UTC(2026, 9, 30, 14, 3, 7)

function bar(time: number, open: number, close: number): Candle {
  return { time, open, high: Math.max(open, close), low: Math.min(open, close), close, volume: 1 }
}

function ready(candles: readonly Candle[], over: Partial<UseLiveCandles> = {}): UseLiveCandles {
  return {
    ...initialSnapshot,
    load: { kind: 'ready', candles, bars: candles },
    status: 'live',
    reload,
    loadOlder,
    ...over,
  }
}

const P0 = { id: 'p0', symbol: 'BTCUSDT', interval: '1m' as const }
const P1 = { id: 'p1', symbol: 'ETHUSDT', interval: '1m' as const }
const MARKET0 = { symbol: P0.symbol, interval: P0.interval }
const MARKET1 = { symbol: P1.symbol, interval: P1.interval }

const render = (c: unknown, props: Record<string, unknown>) =>
  renderToStaticMarkup(h(c as ComponentType<Record<string, unknown>>, props))

function headerProps(over: Record<string, unknown> = {}) {
  return {
    symbol: 'BTCUSDT',
    description: 'BTC / USDT',
    interval: '1m',
    onIntervalChange: noop,
    chartType: 'candles',
    onChartTypeChange: noop,
    onOpenIndicators: noop,
    onOpenSearch: noop,
    onOpenAlert: noop,
    alertDisabledReason: null,
    replayOn: false,
    onToggleReplay: noop,
    replayDisabledReason: null,
    regimeOn: false,
    onToggleRegime: noop,
    watchlistOpen: true,
    onToggleWatchlist: noop,
    source: 'Binance · local server',
    status: 'live',
    loading: false,
    onRefresh: noop,
    refreshDisabled: false,
    onOpenSeasonals: noop,
    onScreenshot: noop,
    theme: 'dark',
    onToggleTheme: noop,
    onOpenShortcuts: noop,
    onOpenSettings: noop,
    isMac: true,
    ...over,
  }
}

/** A store with panel p0 (and p1) reporting, plus a forming-bar tick helper for p0. */
function tickingStore() {
  let now = T0_MS
  const store = createLiveStore(() => now)
  const history = [bar(T, 100, 101), bar(T + 60, 101, 102)]
  let lastUpdate = T0_MS
  store.report('p0', ready(history, { lastUpdate }), MARKET0)
  store.report('p1', ready([bar(T, 5, 6)], { lastUpdate }), MARKET1)
  /** The forming bar (same open time) at `close`, `dtMs` later. */
  const tick = (close: number, dtMs: number, panelId = 'p0') => {
    now += dtMs
    lastUpdate += dtMs
    const candles = [history[0], bar(T + 60, 101, close)]
    if (panelId === 'p0') store.report('p0', ready(candles, { lastUpdate }), MARKET0)
    else store.report('p1', ready([bar(T, 5, close)], { lastUpdate }), MARKET1)
  }
  return { store, tick }
}

const snap = <T>(
  store: LiveStore,
  select: (s: ReturnType<LiveStore['getState']>) => T,
  eq?: (a: T, b: T) => boolean,
) => memoSelector(store.getState, select, eq)

describe('leaf components are memoised', () => {
  it.each([
    ['QuoteView', QuoteView],
    ['QuoteReadout', QuoteReadout],
    ['LastUpdateText', LastUpdateText],
    ['LastUpdateClock', LastUpdateClock],
  ] as [string, unknown][])(
    '%s is React.memo with shallow props and a named inner function',
    (name, c) => {
      const m = c as { $$typeof: symbol; compare: unknown; type: { name: string } }
      expect(m.$$typeof).toBe(MEMO)
      expect(m.compare).toBeNull()
      expect(m.type.name).toBe(name)
    },
  )
})

describe('a forming-bar tick changes only the leaves', () => {
  it('keeps the Header and StatusBar snapshots while the quote and clock follow the tick', () => {
    const { store, tick } = tickingStore()
    const header = snap(store, headerLiveSelector(P0), shallowEqual)
    const status = snap(
      store,
      statusBarSelector(P0, () => 'UTC'),
      shallowEqual,
    )
    const quote = snap(store, liveQuoteSelector(P0), sameQuote)
    const clock = snap(store, lastUpdateSelector(P0, 'UTC'))
    const h0 = header()
    const s0 = status()
    const q0 = quote()
    const c0 = clock()
    expect(q0?.price).toBe('102.00')
    expect(c0).toBe('14:03:07')
    expect(s0).toEqual({ barCount: 2, status: 'live', refreshError: null, timezoneName: 'UTC' })

    tick(103.5, 1_000)
    // The shell's snapshots are the same objects: their wrappers do not re-render.
    expect(header()).toBe(h0)
    expect(status()).toBe(s0)
    // The leaves see the new price and the new second.
    const q1 = quote()
    expect(q1).not.toBe(q0)
    expect(q1?.price).toBe('103.50')
    expect(clock()).toBe('14:03:08')

    // A tick within the same second at the same price: no leaf changes either.
    tick(103.5, 300)
    expect(quote()).toBe(q1)
    expect(clock()).toBe('14:03:08')
    expect(header()).toBe(h0)
    expect(status()).toBe(s0)
  })

  it('another panel ticking changes nothing for the active panel', () => {
    const { store, tick } = tickingStore()
    const quote = snap(store, liveQuoteSelector(P0), sameQuote)
    const clock = snap(store, lastUpdateSelector(P0, 'UTC'))
    const header = snap(store, headerLiveSelector(P0), shallowEqual)
    const q0 = quote()
    const c0 = clock()
    const h0 = header()
    tick(7, 5_000, 'p1')
    expect(quote()).toBe(q0)
    expect(clock()).toBe(c0)
    expect(header()).toBe(h0)
  })

  it('the status bar follows a new bar and a status change (rare, not per tick)', () => {
    const { store } = tickingStore()
    const status = snap(
      store,
      statusBarSelector(P0, () => 'UTC'),
      shallowEqual,
    )
    const s0 = status()
    store.report(
      'p0',
      ready([bar(T, 100, 101), bar(T + 60, 101, 102), bar(T + 120, 102, 102)], {
        status: 'reconnecting',
        lastUpdate: T0_MS + 60_000,
      }),
      MARKET0,
    )
    const s1 = status()
    expect(s1).not.toBe(s0)
    expect(s1).toEqual({
      barCount: 3,
      status: 'reconnecting',
      refreshError: null,
      timezoneName: 'UTC',
    })
  })

  it('the header state follows status, loading and retry changes', () => {
    const { store } = tickingStore()
    const header = snap(store, headerLiveSelector(P0), shallowEqual)
    expect(header()).toEqual({
      status: 'live',
      loading: false,
      onRefresh: reload,
      refreshDisabled: false,
    })
    store.report('p0', ready([bar(T, 1, 2)], { refreshing: true, retryBlocked: true }), MARKET0)
    expect(header()).toEqual({
      status: 'live',
      loading: true,
      onRefresh: reload,
      refreshDisabled: true,
    })
    store.report('p0', { ...initialSnapshot, reload, loadOlder }, MARKET0)
    expect(header()).toMatchObject({ status: 'connecting', loading: true })
  })
})

describe('lastUpdateSelector', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('matches formatClock in any zone, and is null before any update', () => {
    const store = createLiveStore()
    for (const zone of ['UTC', 'America/New_York', 'Asia/Tokyo']) {
      const select = lastUpdateSelector(P0, zone)
      expect(select(store.getState())).toBeNull()
    }
    store.report('p0', ready([bar(T, 1, 2)], { lastUpdate: T0_MS + 999 }), MARKET0)
    for (const zone of ['UTC', 'America/New_York', 'Asia/Tokyo']) {
      expect(lastUpdateSelector(P0, zone)(store.getState())).toBe(formatClock(T0_MS, zone))
      expect(formatClock(T0_MS + 999, zone)).toBe(formatClock(T0_MS, zone))
    }
  })

  it('formats once per second, not once per tick', () => {
    const spy = vi.spyOn(Intl.DateTimeFormat.prototype, 'formatToParts')
    const { store, tick } = tickingStore()
    const clock = snap(store, lastUpdateSelector(P0, 'Asia/Tokyo'))
    expect(clock()).toBe('23:03:07')
    const first = spy.mock.calls.length
    expect(first).toBeGreaterThan(0)
    for (let i = 0; i < 4; i++) tick(102 + i, 200) // 800 ms, all within 23:03:07 Tokyo
    expect(clock()).toBe('23:03:07')
    expect(spy.mock.calls.length).toBe(first)
    tick(110, 300) // 23:03:08.100
    expect(clock()).toBe('23:03:08')
    expect(spy.mock.calls.length).toBeGreaterThan(first)
  })
})

describe('liveQuoteSelector', () => {
  it('quotes the panel candles like summarizeQuote, null without candles or another market', () => {
    const store = createLiveStore()
    const select = liveQuoteSelector(P0)
    expect(select(store.getState())).toBeNull()
    const candles = [bar(T, 100, 101), bar(T + 60, 101, 99.5)]
    store.report('p0', ready(candles), MARKET0)
    expect(select(store.getState())).toEqual(summarizeQuote(candles, '1m'))
    // The report is still the previous market's (right after a symbol switch): no quote.
    expect(liveQuoteSelector({ ...P0, symbol: 'ETHUSDT' })(store.getState())).toBeNull()
    expect(NO_QUOTE()).toBeNull()
  })
})

describe('leaves render exactly the markup Header and StatusBar render from props', () => {
  const sets: [string, Candle[]][] = [
    ['up', [bar(T, 100, 101), bar(T + 60, 101, 105.5)]],
    ['down', [bar(T, 100, 101), bar(T + 60, 101, 99.25)]],
    ['flat', [bar(T, 100, 101), bar(T + 60, 101, 101)]],
    ['single', [bar(T, 100, 101)]],
  ]

  it('Header with a QuoteReadout slot equals Header with the quote prop', () => {
    const empty = createLiveStore()
    expect(
      render(
        Header,
        headerProps({
          quoteSlot: h(QuoteReadout, { liveStore: empty, panel: P0, replayCandles: null }),
        }),
      ),
    ).toBe(render(Header, headerProps({ quote: null })))
    for (const [, candles] of sets) {
      const store = createLiveStore()
      store.report('p0', ready(candles), MARKET0)
      const slot = h(QuoteReadout, { liveStore: store, panel: P0, replayCandles: null })
      expect(render(Header, headerProps({ quoteSlot: slot }))).toBe(
        render(Header, headerProps({ quote: summarizeQuote(candles, '1m') })),
      )
    }
  })

  it('in replay the slot quotes the replayed bars, never the live ones', () => {
    const store = createLiveStore()
    store.report('p0', ready([bar(T, 5, 6), bar(T + 60, 6, 7)]), MARKET0)
    for (const [, candles] of sets) {
      const slot = h(QuoteReadout, { liveStore: store, panel: P0, replayCandles: candles })
      expect(render(Header, headerProps({ quoteSlot: slot }))).toBe(
        render(Header, headerProps({ quote: summarizeQuote(candles, '1m') })),
      )
    }
    const none = h(QuoteReadout, { liveStore: store, panel: P0, replayCandles: [] })
    expect(render(Header, headerProps({ quoteSlot: none }))).toBe(
      render(Header, headerProps({ quote: null })),
    )
  })

  it('the quote markup keeps its classes and order', () => {
    const quote = summarizeQuote(sets[0][1], '1m')
    expect(render(QuoteView, { quote })).toBe(
      '<div class="quote quote--up"><span class="quote__price">105.50</span>' +
        '<span class="quote__change">+4.50 (+4.46%)</span>' +
        '<span class="quote__basis">vs prev close</span></div>',
    )
    expect(render(QuoteView, { quote: null })).toBe(
      '<div class="quote quote--flat"><span class="quote__placeholder" aria-hidden="true"></span></div>',
    )
  })

  it('StatusBar with a LastUpdateClock slot equals StatusBar with the lastUpdate prop', () => {
    const base = { barCount: 500, status: 'live', refreshError: null }
    for (const zone of ['UTC', 'America/New_York']) {
      const name = zone === 'UTC' ? 'UTC' : 'EDT'
      const empty = createLiveStore()
      const none = h(LastUpdateClock, {
        liveStore: empty,
        panel: P0,
        timezone: zone,
        timezoneName: name,
      })
      expect(render(StatusBar, { ...base, timezoneName: name, lastUpdateSlot: none })).toBe(
        render(StatusBar, { ...base, timezoneName: name, lastUpdate: null }),
      )
      const store = createLiveStore()
      store.report('p0', ready([bar(T, 1, 2)], { lastUpdate: T0_MS }), MARKET0)
      const slot = h(LastUpdateClock, {
        liveStore: store,
        panel: P0,
        timezone: zone,
        timezoneName: name,
      })
      const html = render(StatusBar, { ...base, timezoneName: name, lastUpdateSlot: slot })
      expect(html).toBe(
        render(StatusBar, { ...base, timezoneName: name, lastUpdate: formatClock(T0_MS, zone) }),
      )
      expect(html).toContain(`Last update ${formatClock(T0_MS, zone)} ${name}</span>`)
    }
  })
})
