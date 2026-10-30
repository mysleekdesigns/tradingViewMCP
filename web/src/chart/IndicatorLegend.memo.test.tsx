import type { ComponentType, MemoExoticComponent } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { IndicatorLegendRows, PaneLegends } from './IndicatorLegend'
import { partitionLegendRows, type IndicatorLegendRow } from './indicatorSeries'

// Phase 19 (B5): the legend components are memo()'d. Wrapping must not change a single byte of
// markup, so each case renders the memo'd export and the wrapped function side by side.

const rows: IndicatorLegendRow[] = [
  {
    uid: 'e',
    title: 'EMA 20',
    placement: 'overlay',
    pane: 0,
    visible: true,
    values: [{ key: 'ema', label: 'EMA', text: '101.25', color: '#2962ff' }],
  },
  {
    uid: 'h',
    title: 'SMA 50',
    placement: 'overlay',
    pane: 0,
    visible: false,
    values: [{ key: 'sma', label: 'SMA', text: '99.00', color: '#ff9800' }],
    note: 'n',
    hint: 'hint',
  },
  {
    uid: 'r',
    title: 'RSI 14',
    placement: 'pane',
    pane: 1,
    visible: true,
    values: [{ key: 'rsi', label: 'RSI', text: '55.10', color: '#7e57c2' }],
    note: 'Anchor not set',
  },
  {
    uid: 'm',
    title: 'MACD 12 26 9',
    placement: 'pane',
    pane: 2,
    visible: true,
    values: [
      { key: 'hist', label: 'Hist', text: '0.12', color: '#26a69a' },
      { key: 'macd', label: 'MACD', text: '1.02', color: '#2962ff' },
    ],
  },
]

const noop = () => {}

function inner<P extends object>(component: unknown): ComponentType<P> {
  return (component as MemoExoticComponent<ComponentType<P>>).type
}

describe('memo()d indicator legend components', () => {
  it('both are memo components', () => {
    const memoTag = Symbol.for('react.memo')
    expect((IndicatorLegendRows as unknown as { $$typeof: symbol }).$$typeof).toBe(memoTag)
    expect((PaneLegends as unknown as { $$typeof: symbol }).$$typeof).toBe(memoTag)
  })

  it('main legend rows: identical markup to the unwrapped component', () => {
    const Plain = inner<Parameters<typeof IndicatorLegendRows>[0]>(IndicatorLegendRows)
    const { main } = partitionLegendRows(rows)
    for (const highlightedUid of [undefined, null, 'e', 'h', 'zzz']) {
      const props = { rows: main, highlightedUid, onToggleVisible: noop, onRemove: noop }
      const memoed = renderToStaticMarkup(<IndicatorLegendRows {...props} />)
      expect(memoed).toBe(renderToStaticMarkup(<Plain {...props} />))
      expect(memoed).toContain('EMA 20')
    }
  })

  it('pane legends: identical markup to the unwrapped component, unmeasured panes still hidden', () => {
    const Plain = inner<Parameters<typeof PaneLegends>[0]>(PaneLegends)
    const { panes } = partitionLegendRows(rows)
    for (const paneTops of [[0, 300, 420], [0, 300, null], [], [0]]) {
      for (const highlightedUid of [undefined, 'r', 'm']) {
        const props = { rows: panes, paneTops, highlightedUid, onSettings: noop }
        const memoed = renderToStaticMarkup(<PaneLegends {...props} />)
        expect(memoed).toBe(renderToStaticMarkup(<Plain {...props} />))
      }
    }
    expect(renderToStaticMarkup(<PaneLegends rows={panes} paneTops={[0, 300, null]} />)).toContain(
      'top:306px',
    )
    expect(renderToStaticMarkup(<PaneLegends rows={panes} paneTops={[0]} />)).toBe('')
  })

  it('partitioned rows render the same markup as the old inline filters', () => {
    const { main, panes } = partitionLegendRows(rows)
    expect(renderToStaticMarkup(<IndicatorLegendRows rows={main} highlightedUid="e" />)).toBe(
      renderToStaticMarkup(
        <IndicatorLegendRows rows={rows.filter((r) => r.pane === 0)} highlightedUid="e" />,
      ),
    )
    const tops = [0, 300, 420]
    expect(renderToStaticMarkup(<PaneLegends rows={panes} paneTops={tops} />)).toBe(
      renderToStaticMarkup(<PaneLegends rows={rows.filter((r) => r.pane > 0)} paneTops={tops} />),
    )
  })
})
