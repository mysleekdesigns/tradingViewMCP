import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { DEFAULT_DRAWING_STYLE } from '../chart/drawings/types'
import type { CandleSource } from '../data/timeframeProvider'
import type { DrawingsApi } from '../state/drawings'
import { ChartPanel, type ChartPanelProps } from './ChartPanel'

const noop = () => {}

function props(over: Partial<ChartPanelProps> = {}): ChartPanelProps {
  const drawings: DrawingsApi = {
    items: [],
    status: 'local',
    add: () => true,
    update: () => true,
    remove: noop,
    clear: noop,
  } as unknown as DrawingsApi
  const provider: CandleSource = {
    getCandles: vi.fn(() => new Promise<never>(noop)),
    subscribeCandles: vi.fn(() => noop),
  }
  return {
    panel: { id: 'p0', symbol: 'BTCUSDT', interval: '5s', chartType: 'candles' },
    provider,
    limit: 500,
    errorSource: 'Binance',
    source: 'Binance · local server',
    exchange: 'Binance',
    active: true,
    multi: false,
    onActivate: noop,
    appearance: { theme: 'dark', upColor: '#26a69a', downColor: '#ef5350', timezone: 'UTC' },
    indicators: [],
    onIndicatorToggleVisible: noop,
    onIndicatorRemove: noop,
    onIndicatorSettings: noop,
    drawings,
    tool: 'cursor',
    onToolDone: noop,
    drawingStyle: DEFAULT_DRAWING_STYLE,
    selectedDrawingId: null,
    onSelectDrawing: noop,
    onContextMenu: noop,
    alerts: [],
    backtest: null,
    focusRange: null,
    onFocusDone: noop,
    keyboardBlocked: false,
    crosshairSync: null,
    regimeEnabled: true,
    regimeClient: { regime: vi.fn() },
    ...over,
  }
}

describe('ChartPanel', () => {
  it('shows a loading state, not an unsupported-timeframe error, while the venue is unknown', () => {
    const html = renderToStaticMarkup(createElement(ChartPanel, props({ venuePending: true })))
    expect(html).toContain(
      'Loading BTCUSDT 5s: waiting for the local server to report its exchange',
    )
    expect(html).not.toMatch(/not available/)
  })

  it('names the source while loading once the venue is known', () => {
    const html = renderToStaticMarkup(createElement(ChartPanel, props()))
    expect(html).toContain('Loading BTCUSDT 5s from Binance · local server')
  })

  it('is memoised, so an unchanged panel skips re-rendering on another panel’s tick', () => {
    expect((ChartPanel as unknown as { $$typeof: symbol }).$$typeof).toBe(Symbol.for('react.memo'))
  })
})
