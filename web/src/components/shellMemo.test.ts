import { createElement as h, type ComponentType } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { MarketDataProvider } from '../data/provider'
import { formatClock } from '../state/appSettings'
import { AlertsPanel } from './AlertsPanel'
import { ChartTypeMenu } from './ChartTypeMenu'
import { DrawingToolbar } from './DrawingToolbar'
import { Header } from './Header'
import { LayoutMenu } from './LayoutMenu'
import { SidebarRail } from './SidebarRail'
import { StatusBar } from './StatusBar'
import { TimeframePicker } from './TimeframePicker'
import { Watchlist } from './Watchlist'

const MEMO = Symbol.for('react.memo')
const noop = () => {}

interface MemoLike {
  $$typeof: symbol
  compare: unknown
  type: { name: string }
}

describe('shell components are memoised (Phase 19, A2/A6)', () => {
  const cases: [string, unknown][] = [
    ['Header', Header],
    ['Watchlist', Watchlist],
    ['StatusBar', StatusBar],
    ['SidebarRail', SidebarRail],
    ['DrawingToolbar', DrawingToolbar],
    ['AlertsPanel', AlertsPanel],
    ['TimeframePicker', TimeframePicker],
    ['ChartTypeMenu', ChartTypeMenu],
    ['LayoutMenu', LayoutMenu],
  ]
  it.each(cases)('%s is React.memo with shallow props and a named inner function', (name, c) => {
    const m = c as MemoLike
    expect(m.$$typeof).toBe(MEMO)
    // null compare = React's default shallow equality; a custom compare could hide prop changes.
    expect(m.compare).toBeNull()
    // Named inner function keeps the component name in React DevTools and the Profiler.
    expect(m.type.name).toBe(name)
  })
})

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
    quote: null,
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

const render = (c: unknown, props: Record<string, unknown>) =>
  renderToStaticMarkup(h(c as ComponentType<Record<string, unknown>>, props))

describe('Header with hoisted static icons', () => {
  it('renders the same markup on repeated renders (shared icon elements are reusable)', () => {
    const a = render(Header, headerProps())
    const b = render(Header, headerProps())
    expect(a).toBe(b)
    // The hoisted elements still render: logo, search glass and the watchlist glyph.
    expect(a).toContain('fill="var(--accent)"')
    expect(a).toContain('class="symbol-btn__glass"')
    expect(a).toContain('d="M11 3v12M12.8 6.5h1.2M12.8 9h1.2M12.8 11.5h1.2"')
  })

  it('swaps the theme glyph: sun in dark, moon in light', () => {
    const SUN = 'M9 1.8v1.8'
    const MOON = 'M14.8 11.2A6 6 0 0 1 6.8 3.2'
    const dark = render(Header, headerProps({ theme: 'dark' }))
    const light = render(Header, headerProps({ theme: 'light' }))
    expect(dark).toContain(SUN)
    expect(dark).not.toContain(MOON)
    expect(light).toContain(MOON)
    expect(light).not.toContain(SUN)
  })

  it('shows the funding button only with a toggle handler', () => {
    expect(render(Header, headerProps())).not.toContain('Funding and open interest panes')
    expect(render(Header, headerProps({ onToggleDerivatives: noop }))).toContain(
      'Funding and open interest panes',
    )
  })
})

describe('Watchlist rows with shared stable handlers', () => {
  const info = new Map([
    [
      'BTCUSDT',
      {
        symbol: 'BTCUSDT',
        baseAsset: 'BTC',
        quoteAsset: 'USDT',
        status: 'TRADING',
        tickSize: 0.01,
      },
    ],
  ])
  const base = {
    provider: {} as MarketDataProvider,
    onSelect: noop,
    onRemove: noop,
    onMove: noop,
    onAdd: noop,
  }

  it('renders one draggable row per symbol, the active one tabbable', () => {
    const html = render(Watchlist, {
      ...base,
      items: ['BTCUSDT', 'ETHUSDT', 'FOOUSDT'],
      activeSymbol: 'ETHUSDT',
      symbolInfo: info,
    })
    expect(html.match(/<li [^>]*draggable="true"/g)?.length).toBe(3)
    expect(html.match(/tabindex="0"/g)?.length).toBe(1)
    expect(html).toMatch(/tabindex="0"[^>]*aria-current="true"[^>]*aria-label="ETHUSDT, /)
    expect(html).toContain('aria-label="FOOUSDT, not listed on this exchange"')
    expect(html).toContain('<span class="watchlist__base">BTC</span>')
    expect(html).toContain('aria-label="Remove BTCUSDT from watchlist"')
  })

  it('falls back to the first row as the tab stop when the active symbol is not listed', () => {
    const html = render(Watchlist, {
      ...base,
      items: ['BTCUSDT', 'ETHUSDT'],
      activeSymbol: 'XRPUSDT',
      symbolInfo: null,
    })
    expect(html).toMatch(/tabindex="0"[^>]*aria-label="BTCUSDT, price loading"/)
    expect(html).not.toContain('aria-current')
  })
})

describe('StatusBar clock formatter cache (formatClock, one Intl formatter per zone)', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  const reference = (ms: number, timeZone: string) =>
    new Intl.DateTimeFormat('en-GB', {
      timeZone,
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hourCycle: 'h23',
    }).format(new Date(ms))

  it('builds no new Intl.DateTimeFormat after the first call for a zone', () => {
    const ms = Date.UTC(2026, 9, 30, 14, 3, 7)
    // A zone no other test here uses, so the first call is the cold one.
    const zone = 'Pacific/Chatham'
    const spy = vi.spyOn(Intl, 'DateTimeFormat')
    const first = formatClock(ms, zone)
    const cold = spy.mock.calls.length
    expect(cold).toBeGreaterThan(0)
    expect(cold).toBeLessThanOrEqual(2) // zone validity probe + the parts formatter
    for (let i = 1; i <= 50; i++) formatClock(ms + i * 1000, zone)
    expect(spy.mock.calls.length).toBe(cold)
    // A different zone gets its own formatter; going back to the first one reuses its cache.
    formatClock(ms, 'Asia/Kolkata')
    const afterSecondZone = spy.mock.calls.length
    expect(afterSecondZone).toBeGreaterThan(cold)
    expect(formatClock(ms, zone)).toBe(first)
    expect(spy.mock.calls.length).toBe(afterSecondZone)
  })

  it('cached output matches a freshly built formatter', () => {
    const ms = Date.UTC(2026, 2, 8, 6, 59, 59) // around a US DST switch
    for (const zone of ['UTC', 'America/New_York', 'Asia/Tokyo', 'Pacific/Chatham']) {
      for (const offset of [0, 1_000, 3_600_000, 86_399_000]) {
        expect(formatClock(ms + offset, zone)).toBe(reference(ms + offset, zone))
      }
    }
  })
})
