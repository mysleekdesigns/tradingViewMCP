import { createElement as h, Suspense, type ComponentType, type ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { prerender } from 'react-dom/static'
import { describe, expect, it, vi } from 'vitest'

// Stand-ins for the five heavy modules: each echoes the name of the export it stands for and the
// props it received, so the tests prove which named export a lazy wrapper loads.
vi.mock('./BacktestPanel', () => ({ BacktestPanel: stub('BacktestPanel') }))
vi.mock('./ResearchPage', () => ({ ResearchPage: stub('ResearchPage') }))
vi.mock('./PaperTradingPanel', () => ({ PaperTradingPanel: stub('PaperTradingPanel') }))
vi.mock('./SeasonalsView', () => ({ SeasonalsView: stub('SeasonalsView') }))
vi.mock('./SettingsDialog', () => ({ SettingsDialog: stub('SettingsDialog') }))

function stub(name: string) {
  return function Stub(props: Record<string, unknown>) {
    return h('p', { 'data-real': name }, JSON.stringify(props))
  }
}

const lazy = await import('./lazyPanels')
const { PANEL_LOADING_DELAY_MS, PanelLoading, PanelLoadingView } = lazy

async function settled(el: ReactElement): Promise<string> {
  const { prelude } = await prerender(el)
  // Drop React's Suspense boundary markers (<!--$-->…<!--/$-->).
  return (await new Response(prelude).text()).replace(/<!--\/?\$-->/g, '')
}

describe('lazy panels (Phase 19, D1)', () => {
  const cases: [string, ComponentType<never>][] = [
    ['BacktestPanel', lazy.BacktestPanel as ComponentType<never>],
    ['ResearchPage', lazy.ResearchPage as ComponentType<never>],
    ['PaperTradingPanel', lazy.PaperTradingPanel as ComponentType<never>],
    ['SeasonalsView', lazy.SeasonalsView as ComponentType<never>],
    ['SettingsDialog', lazy.SettingsDialog as ComponentType<never>],
  ]

  it.each(cases)(
    '%s is React.lazy and resolves to that named export, props intact',
    async (name, C) => {
      // A retryable wrapper (lazyLoad.ts): its current attempt is the React.lazy.
      const attempt = (C as unknown as { current: { $$typeof: symbol } }).current
      expect(attempt.$$typeof).toBe(Symbol.for('react.lazy'))
      const html = await settled(
        h(Suspense, { fallback: 'loading' }, h(C, { symbol: 'BTCUSDT', n: 3 } as never)),
      )
      expect(html).toBe(
        `<p data-real="${name}">{&quot;symbol&quot;:&quot;BTCUSDT&quot;,&quot;n&quot;:3}</p>`,
      )
    },
  )

  it("Research's render-error panel is no longer lazy: it must render without the chunk", () => {
    expect('ResearchFallback' in lazy).toBe(false)
  })

  it('shows the Suspense fallback, not the panel, until the code has loaded', async () => {
    // Fresh wrappers that have not loaded yet: a synchronous render can only show the fallback.
    vi.resetModules()
    const fresh = await import('./lazyPanels')
    const html = renderToStaticMarkup(
      h(
        Suspense,
        { fallback: h(fresh.PanelLoading, { variant: 'dock', label: 'Loading Strategy Tester…' }) },
        h(fresh.BacktestPanel as ComponentType<never>),
      ),
    )
    expect(html).toBe(
      '<section class="tester lazy-panel" aria-busy="true" aria-label="Loading Strategy Tester…"></section>',
    )
  })
})

describe('PanelLoading', () => {
  it('waits a moment before showing anything, so a fast load never flashes', () => {
    expect(PANEL_LOADING_DELAY_MS).toBeGreaterThanOrEqual(150)
    expect(PANEL_LOADING_DELAY_MS).toBeLessThanOrEqual(300)
    // Effects do not run in a static render: this is the placeholder's first frame.
    for (const variant of ['overlay', 'sidebar', 'dialog'] as const) {
      expect(renderToStaticMarkup(h(PanelLoading, { variant, label: 'Loading…' }))).toBe('')
    }
  })

  it('holds the dock frame from the first frame, so the chart does not jump when it loads', () => {
    expect(
      renderToStaticMarkup(
        h(PanelLoadingView, { variant: 'dock', label: 'Loading Strategy Tester…', shown: false }),
      ),
    ).toBe(
      '<section class="tester lazy-panel" aria-busy="true" aria-label="Loading Strategy Tester…"></section>',
    )
  })

  const status =
    '<div class="lazy-panel__status" role="status">' +
    '<span class="lazy-panel__spinner" aria-hidden="true"></span>Loading Research…</div>'

  it('after the delay: spinner and caption in the slot of the panel being opened', () => {
    const view = (variant: 'dock' | 'overlay' | 'sidebar' | 'dialog') =>
      renderToStaticMarkup(
        h(PanelLoadingView, { variant, label: 'Loading Research…', shown: true }),
      )
    expect(view('overlay')).toBe(`<div class="lazy-panel lazy-panel--overlay">${status}</div>`)
    expect(view('sidebar')).toBe(`<div class="lazy-panel">${status}</div>`)
    expect(view('dock')).toBe(
      `<section class="tester lazy-panel" aria-busy="true" aria-label="Loading Research…">${status}</section>`,
    )
    expect(view('dialog')).toBe(
      '<div class="modal-backdrop modal-backdrop--center">' +
        `<div class="lazy-panel lazy-panel--card">${status}</div></div>`,
    )
  })

  it('a dialog placeholder closes on a backdrop click, like the real dialog, not on its card', () => {
    const onClose = vi.fn()
    const el = PanelLoadingView({
      variant: 'dialog',
      label: 'Loading Settings…',
      onClose,
      shown: true,
    })
    const backdrop = el as ReactElement<{
      onPointerDown(e: { target: unknown; currentTarget: unknown }): void
    }>
    const self = {}
    backdrop.props.onPointerDown({ target: {}, currentTarget: self })
    expect(onClose).not.toHaveBeenCalled()
    backdrop.props.onPointerDown({ target: self, currentTarget: self })
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
