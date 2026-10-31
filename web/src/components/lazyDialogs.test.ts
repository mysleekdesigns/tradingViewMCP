import { createElement as h, Suspense, type ComponentType, type ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { prerender } from 'react-dom/static'
import { describe, expect, it, vi } from 'vitest'

// Phase 19 wave 4 (D4 follow-up): the three rarely opened dialogs load on first open. Stand-ins
// echo the export they stand for and the props they received.
vi.mock('./IndicatorSettings', () => ({ IndicatorSettings: stub('IndicatorSettings') }))
vi.mock('./AlertDialog', () => ({ AlertDialog: stub('AlertDialog') }))
vi.mock('./ShortcutsDialog', () => ({ ShortcutsDialog: stub('ShortcutsDialog') }))

function stub(name: string) {
  return function Stub(props: Record<string, unknown>) {
    const shown = Object.fromEntries(Object.entries(props).filter(([, v]) => !isFn(v)))
    return h('p', { 'data-real': name }, JSON.stringify(shown))
  }
}
const isFn = (v: unknown) => typeof v === 'function'

const lazy = await import('./lazyPanels')

async function settled(el: ReactElement): Promise<string> {
  const { prelude } = await prerender(el)
  return (await new Response(prelude).text()).replace(/<!--\/?\$-->/g, '')
}

type Props = Record<string, unknown>
type Wrapper = ComponentType<Props>
const DIALOGS: [string, string, Record<string, unknown>][] = [
  ['IndicatorSettings', 'Loading indicator settings…', { secondsCharts: true }],
  ['AlertDialog', 'Loading alert…', { symbol: 'BTCUSDT', initialPrice: 100 }],
  ['ShortcutsDialog', 'Loading shortcuts…', { isMac: true }],
]
const wrapperOf = (m: Record<string, unknown>, name: string) => m[name] as Wrapper

describe('lazy dialogs', () => {
  it.each(DIALOGS)('%s loads that named export with its props intact', async (name, _l, props) => {
    const onClose = vi.fn()
    const html = await settled(h(wrapperOf(lazy, name), { ...props, onClose }))
    expect(html).toBe(`<p data-real="${name}">${escape(JSON.stringify(props))}</p>`)
    expect(onClose).not.toHaveBeenCalled()
  })

  it.each(DIALOGS)(
    '%s has its own Suspense boundary with the dialog placeholder',
    (name, label, props) => {
      const onClose = vi.fn()
      const call = wrapperOf(lazy, name) as (p: Props) => ReactElement
      const el = call({ ...props, onClose })
      expect(el.type).toBe(Suspense)
      const fallback = (el.props as { fallback: ReactElement }).fallback
      expect(fallback.type).toBe(lazy.PanelLoading)
      // Same placeholder as Settings: the dialog slot, and a backdrop click closes.
      expect(fallback.props).toEqual({ variant: 'dialog', label, onClose })
    },
  )

  it.each(DIALOGS)(
    '%s shows nothing on its first frame while the code loads',
    async (name, _l, props) => {
      vi.resetModules()
      const fresh = (await import('./lazyPanels')) as unknown as Record<string, unknown>
      const html = renderToStaticMarkup(h(wrapperOf(fresh, name), { ...props, onClose: () => {} }))
      expect(html).toBe('')
    },
  )
})

function escape(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;')
}
