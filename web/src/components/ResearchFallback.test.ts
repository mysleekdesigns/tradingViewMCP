import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { ResearchFallback } from './ResearchFallback'
import { ResearchFallback as ReExported } from './ResearchPage'

// Phase 19 wave 4 review: Research's render-error panel moved out of ResearchPage.tsx (into the
// main chunk) with its markup unchanged. This is the markup the ResearchPage.tsx version rendered
// at 816ba6b, byte for byte.
const HEAD_MARKUP =
  '<section class="research" aria-labelledby="_R_0_-title"><header class="research__head">' +
  '<span class="research__head-icon" aria-hidden="true">' +
  '<svg width="16" height="16" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M8.5 3h5M9.5 3v5.2L4.6 16.6A1.6 1.6 0 0 0 6 19h10a1.6 1.6 0 0 0 1.4-2.4L12.5 8.2V3">' +
  '</path><path d="M6.8 14.5 9.5 12l2 1.6 3.6-3.1"></path></svg></span>' +
  '<h2 id="_R_0_-title" class="research__title">Research</h2>' +
  '<span class="research__spacer"></span>' +
  '<button type="button" class="research__back" title="Back to the chart (Esc)">' +
  '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M2.5 13.5h11M4 11l3-3.5 2.5 2L13 5"></path></svg>Back to chart</button>' +
  '</header><div class="research__body">' +
  '<div class="research__state research__state--bad" role="alert">' +
  '<span class="research__state-icon" aria-hidden="true">' +
  '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M8 2.5 14 13H2L8 2.5ZM8 6.5v3M8 11.3v.01"></path></svg></span>' +
  '<p class="research__state-title">Research view failed to render</p>' +
  '<p class="research__state-text">Part of the lab&#x27;s answer could not be shown. The chart and the Strategy Tester are unaffected.</p>' +
  '<p class="research__state-text research__mono research__fallback-detail">&lt;b&gt;bad&lt;/b&gt; payload</p>' +
  '<button type="button" class="research__btn research__btn--primary">Reload</button></div>' +
  '</div></section>'

const props = () => ({
  error: new Error('<b>bad</b> payload'),
  onReload: vi.fn(),
  onClose: vi.fn(),
})

describe('ResearchFallback (static, main chunk)', () => {
  it('renders exactly the markup it had inside ResearchPage', () => {
    expect(renderToStaticMarkup(createElement(ResearchFallback, props()))).toBe(HEAD_MARKUP)
  })

  it('ResearchPage re-exports the same component, not a copy', () => {
    expect(ReExported).toBe(ResearchFallback)
  })
})
