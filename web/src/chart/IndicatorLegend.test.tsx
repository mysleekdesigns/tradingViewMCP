import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { IndicatorLegendRows, PaneLegends } from './IndicatorLegend'
import type { IndicatorLegendRow } from './indicatorSeries'

function row(uid: string, pane: number, visible = true): IndicatorLegendRow {
  return {
    uid,
    title: `T ${uid}`,
    placement: pane === 0 ? 'overlay' : 'pane',
    pane,
    visible,
    values: [{ key: 'v', label: 'V', text: '1.00', color: '#2962ff' }],
  }
}

/** Class attribute of each legend row, in document order. */
function rowClasses(html: string): string[] {
  return [...html.matchAll(/<div class="(ind-legend__row[^"]*)"/g)].map((m) => m[1])
}

describe('indicator legend highlight', () => {
  it('main legend: only the hovered indicator row gets the highlighted class', () => {
    const html = renderToStaticMarkup(
      <IndicatorLegendRows rows={[row('a', 0), row('b', 0)]} highlightedUid="b" />,
    )
    expect(rowClasses(html)).toEqual([
      'ind-legend__row',
      'ind-legend__row ind-legend__row--highlighted',
    ])
    expect(html.match(/data-highlighted="true"/g)).toHaveLength(1)
  })

  it('pane legends: the hovered pane indicator row is highlighted', () => {
    const html = renderToStaticMarkup(
      <PaneLegends rows={[row('r', 1), row('m', 2)]} paneTops={[0, 300, 420]} highlightedUid="r" />,
    )
    expect(rowClasses(html)).toEqual([
      'ind-legend__row ind-legend__row--highlighted',
      'ind-legend__row',
    ])
  })

  it('no highlight by default or for null / unknown uids; hidden rows keep their class', () => {
    const rows = [row('a', 0), row('h', 0, false)]
    for (const highlightedUid of [undefined, null, 'zzz']) {
      const html = renderToStaticMarkup(
        <IndicatorLegendRows rows={rows} highlightedUid={highlightedUid} />,
      )
      expect(rowClasses(html)).toEqual([
        'ind-legend__row',
        'ind-legend__row ind-legend__row--hidden',
      ])
      expect(html).not.toContain('data-highlighted')
    }
  })

  it('highlighting changes classes only: same markup structure, so no layout shift', () => {
    const plain = renderToStaticMarkup(<IndicatorLegendRows rows={[row('a', 0)]} />)
    const lit = renderToStaticMarkup(
      <IndicatorLegendRows rows={[row('a', 0)]} highlightedUid="a" />,
    )
    const strip = (h: string) =>
      h.replace(/ ind-legend__row--highlighted/g, '').replace(/ data-highlighted="true"/g, '')
    expect(strip(lit)).toBe(plain)
  })
})

describe('indicator legend note and hint', () => {
  const NOTE =
    'Anchor is before the loaded history. Scroll left to load older bars, if the market has them.'

  it('renders the note as small muted text inside the row, as an accessible note', () => {
    const html = renderToStaticMarkup(
      <IndicatorLegendRows rows={[{ ...row('a', 0), note: NOTE }]} />,
    )
    expect(rowClasses(html)).toEqual(['ind-legend__row ind-legend__row--note'])
    // Inside the row, after the values, before the action buttons; full text as tooltip too.
    const noteAt = html.indexOf('class="ind-legend__note"')
    expect(noteAt).toBeGreaterThan(html.indexOf('ind-legend__values'))
    expect(noteAt).toBeLessThan(html.indexOf('ind-legend__actions'))
    expect(html).toContain(
      `<span class="ind-legend__note" role="note" title="${NOTE}">${NOTE}</span>`,
    )
  })

  it('a hidden row shows no note; rows without a note are unchanged', () => {
    const hidden = renderToStaticMarkup(
      <IndicatorLegendRows rows={[{ ...row('a', 0, false), note: NOTE }]} />,
    )
    expect(hidden).not.toContain('ind-legend__note')
    expect(rowClasses(hidden)).toEqual(['ind-legend__row ind-legend__row--hidden'])
    // Another row's markup is the same whether or not its neighbour has a note.
    const plain = renderToStaticMarkup(<IndicatorLegendRows rows={[row('a', 0), row('b', 0)]} />)
    const noted = renderToStaticMarkup(
      <IndicatorLegendRows rows={[{ ...row('a', 0), note: NOTE }, row('b', 0)]} />,
    )
    const second = (h: string) => h.slice(h.lastIndexOf('<div class="ind-legend__row'))
    expect(second(noted)).toBe(second(plain))
  })

  it('puts the hint on the row as a tooltip, and no title attribute without one', () => {
    const hint = 'Rows use every bar in the range, including later ones: hindsight, not a signal'
    const html = renderToStaticMarkup(<IndicatorLegendRows rows={[{ ...row('v', 0), hint }]} />)
    expect(html).toContain(`<div class="ind-legend__row" title="${hint}">`)
    const plain = renderToStaticMarkup(<IndicatorLegendRows rows={[row('v', 0)]} />)
    expect(plain).toMatch(/^<div class="ind-legend__row">/)
  })
})
