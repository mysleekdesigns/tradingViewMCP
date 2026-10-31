import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { ResearchIcon, TesterIcon } from './icons'

// The markup each icon rendered while it still lived in BacktestPanel / ResearchPage (captured
// before the move, Phase 19 D1): moving them must not change a single attribute.
const ATTRS =
  'viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.4" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"'
const tester = (size: number) =>
  `<svg width="${size}" height="${size}" ${ATTRS}>` +
  '<path d="M3.5 17.5h15M5 14.5l3.5-4 3 2.5 5-6.5"></path><path d="M14 6.5h2.5V9"></path></svg>'
const research = (size: number) =>
  `<svg width="${size}" height="${size}" ${ATTRS}>` +
  '<path d="M8.5 3h5M9.5 3v5.2L4.6 16.6A1.6 1.6 0 0 0 6 19h10a1.6 1.6 0 0 0 1.4-2.4L12.5 8.2V3"></path>' +
  '<path d="M6.8 14.5 9.5 12l2 1.6 3.6-3.1"></path></svg>'

describe('shared icons (moved out of the lazily loaded panels)', () => {
  it('keep their default sizes: Tester 16, Research 22', () => {
    expect(renderToStaticMarkup(h(TesterIcon))).toBe(tester(16))
    expect(renderToStaticMarkup(h(ResearchIcon))).toBe(research(22))
  })

  it.each([16, 22, 24, 28])('render the same markup as before at size %i', (size) => {
    expect(renderToStaticMarkup(h(TesterIcon, { size }))).toBe(tester(size))
    expect(renderToStaticMarkup(h(ResearchIcon, { size }))).toBe(research(size))
  })
})
