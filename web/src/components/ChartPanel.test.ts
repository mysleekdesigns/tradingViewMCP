import { createElement, isValidElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { DEFAULT_DRAWING_STYLE } from '../chart/drawings/types'
import type { RegimeHmmInfo, RegimeModel } from '../data/labTypes'
import type { CandleSource } from '../data/timeframeProvider'
import type { DrawingsApi } from '../state/drawings'
import type { RegimeOverlay } from '../state/useRegime'
import { HMM_BELOW_1H_TEXT } from '../state/useRegime'
import { ChartPanel, RegimeChip, type ChartPanelProps } from './ChartPanel'

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
    regimeModel: 'markov',
    onRegimeModel: noop,
    ...over,
  }
}

interface ButtonElement {
  type: 'button'
  props: { 'aria-label': string; onClick: () => void }
}

/** The buttons in a rendered tree (no DOM here), going through function components. */
function findButtons(node: unknown): ButtonElement[] {
  if (Array.isArray(node)) return node.flatMap(findButtons)
  if (!isValidElement<{ children?: unknown }>(node)) return []
  if (node.type === 'button') return [node as unknown as ButtonElement]
  if (typeof node.type === 'function') {
    return findButtons((node.type as (p: unknown) => unknown)(node.props))
  }
  return findButtons(node.props.children)
}

describe('RegimeChip', () => {
  const OVERLAY: RegimeOverlay = { points: null, hmm: null, error: null, loading: false }
  const FIT: RegimeHmmInfo = {
    n_states: 2,
    warmup_bars: 3043,
    bars_needed: 3043,
    bars_have: 5000,
    unavailable: null,
    bear_is_high_vol: null,
  }
  const closed = {
    candles: [1, 2, 3].map((time) => ({
      time,
      open: 1,
      high: 1,
      low: 1,
      close: 1,
      volume: 1,
    })),
    formingLast: false,
  }

  function chip(
    overlay: Partial<RegimeOverlay> = {},
    over: { model?: RegimeModel; hmmUnavailable?: string | null; unavailable?: string | null } = {},
  ): string {
    return renderToStaticMarkup(
      createElement(RegimeChip, {
        overlay: { ...OVERLAY, ...overlay },
        unavailable: over.unavailable ?? null,
        source: closed,
        model: over.model ?? 'hmm',
        hmmUnavailable: over.hmmUnavailable ?? null,
        onModel: noop,
      }),
    )
  }

  it('offers a named Markov / HMM picker with the running model pressed', () => {
    const html = chip({}, { model: 'markov' })
    expect(html).toContain('aria-label="Regime ribbon source"')
    expect(html).toMatch(/aria-label="Markov" aria-pressed="true"/)
    expect(html).toMatch(/aria-label="HMM" aria-pressed="false" aria-disabled="false"/)
    expect(html).not.toContain('aria-disabled="true"')
  })

  it('keeps the Markov label and sideways swatch as they were', () => {
    const html = chip({}, { model: 'markov' })
    expect(html).toContain('Regime 20 · ±5%')
    expect(html).toContain('regime-chip__swatch--side')
    expect(html).not.toContain('Fitting HMM')
  })

  it('disables the HMM option below 1h with the plain reason, and keeps it focusable', () => {
    const html = chip({}, { model: 'markov', hmmUnavailable: HMM_BELOW_1H_TEXT })
    expect(html).toContain(`aria-label="${HMM_BELOW_1H_TEXT}"`)
    expect(html).toContain(`title="${HMM_BELOW_1H_TEXT}"`)
    expect(html).toContain('aria-disabled="true"')
    expect(html).not.toMatch(/\sdisabled(=|\s|>)/)
  })

  it('ignores a click on the disabled option and passes one on the enabled option', () => {
    const picks: RegimeModel[] = []
    const fire = (hmmUnavailable: string | null, label: string) => {
      const buttons = findButtons(
        RegimeChip({
          overlay: OVERLAY,
          unavailable: null,
          source: closed,
          model: 'markov',
          hmmUnavailable,
          onModel: (m) => picks.push(m),
        }),
      )
      buttons.find((b) => b.props['aria-label'] === label)?.props.onClick()
    }
    fire(HMM_BELOW_1H_TEXT, HMM_BELOW_1H_TEXT)
    expect(picks).toEqual([])
    fire(null, 'HMM')
    expect(picks).toEqual(['hmm'])
  })

  it('has no picker on seconds charts, which behave as before', () => {
    const html = chip({}, { model: 'markov', unavailable: 'Regime: not on seconds timeframes' })
    expect(html).toContain('Regime: not on seconds timeframes')
    expect(html).not.toContain('regime-seg')
  })

  it('says the first fit is running while there are no points', () => {
    const html = chip({ loading: true })
    expect(html).toContain('Fitting HMM, first run can take a minute or two')
    expect(html).toContain('regime-chip__spinner')
  })

  it('does not repeat the fitting text during a refresh with points on screen', () => {
    const html = chip({ loading: true, points: [{ time: 3, state: 1, prob: 0.9 }], hmm: FIT })
    expect(html).not.toContain('Fitting HMM')
    expect(html).toContain('Bull · 1 bar')
    expect(html).toContain('HMM 2 states')
  })

  it('replaces the fitting text with the error once the fit fails', () => {
    const html = chip({ error: 'The lab service did not answer in time' })
    expect(html).not.toContain('Fitting HMM')
    expect(html).not.toContain('regime-chip__spinner')
    expect(html).toContain('regime-chip--error')
    expect(html).toContain('The lab service did not answer in time')
  })

  it('says how many bars the HMM needs and has when there are too few', () => {
    const short = { ...FIT, bars_have: 1200 }
    const html = chip({ points: [{ time: 3, state: null, prob: null }], hmm: short })
    expect(html).toContain('HMM needs 3,043 bars, has 1,200')
  })

  it('shows why nothing was fitted, ahead of the bar counts, in the text and the tooltip', () => {
    const reason = 'the BTCUSDT 4h archive ends 2026-03-31 20:00 UTC, the chart starts 2026-06-01'
    const html = chip({
      points: [{ time: 3, state: null, prob: null }],
      hmm: { ...FIT, bars_have: 10, unavailable: reason },
    })
    expect(html).toContain(`>${reason}</span>`)
    expect(html).toContain(`title="No HMM ribbon: ${reason}"`)
    expect(html).not.toContain('HMM needs')
  })

  it('keeps the live region mounted and empty when there is nothing to announce', () => {
    const quiet = chip({ points: [{ time: 3, state: 1, prob: 0.9 }], hmm: FIT })
    expect(quiet).toContain('<span class="regime-chip__note" role="status"></span>')
    const fitting = chip({ loading: true })
    expect(fitting).toContain(
      '<span class="regime-chip__note" role="status">Fitting HMM, first run can take a minute or two</span>',
    )
  })

  it('shows Bear and Bull with no sideways swatch for two states', () => {
    const calm = chip({
      points: [{ time: 3, state: -1, prob: 0.8 }],
      hmm: { ...FIT, bear_is_high_vol: false },
    })
    expect(calm).toContain('role="status">Bear is the calmer state in this fit</span>')
    expect(calm).toContain('not always the more volatile one')
    for (const known of [true, null]) {
      const quiet = chip({
        points: [{ time: 3, state: -1, prob: 0.8 }],
        hmm: { ...FIT, bear_is_high_vol: known },
      })
      expect(quiet).not.toContain('calmer state')
    }
    const html = chip({ points: [{ time: 3, state: -1, prob: 0.8 }], hmm: FIT })
    expect(html).toContain('Bear · 1 bar')
    expect(html).toContain('HMM 2 states')
    expect(html).not.toContain('regime-chip__swatch--side')
  })

  it('keeps the sideways swatch with three states', () => {
    const html = chip({ points: [], hmm: { ...FIT, n_states: 3 } })
    expect(html).toContain('regime-chip__swatch--side')
  })
})

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
