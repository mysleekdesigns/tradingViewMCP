import { createElement as h, Fragment, Suspense, type ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { prerender } from 'react-dom/static'
import { describe, expect, it, vi } from 'vitest'
import { LazyLoadError, retryableLazy } from './lazyLoad'
import {
  AlertDialog,
  IndicatorSettings,
  LazyBoundary,
  LazyLoadFailed,
  ShortcutsDialog,
  type LazyBoundaryProps,
  type PanelLoadingVariant,
} from './lazyPanels'

// Phase 19 wave 4 review: a lazily loaded panel whose code fails to arrive must not unmount the
// app. This suite has no DOM, so the boundary's lifecycle is driven by hand as React would
// (getDerivedStateFromError, render, componentDidCatch), like ErrorBoundary.test.ts.

vi.mock('./IndicatorSettings', () => ({ IndicatorSettings: () => null }))
vi.mock('./AlertDialog', () => ({ AlertDialog: () => null }))
vi.mock('./ShortcutsDialog', () => ({ ShortcutsDialog: () => null }))

const loadError = () => new LazyLoadError('the Strategy Tester', new TypeError('Failed to fetch'))

function boundary(over: Partial<LazyBoundaryProps> = {}) {
  const props: LazyBoundaryProps = {
    variant: 'dock',
    name: 'the Strategy Tester',
    retry: vi.fn(),
    onClose: vi.fn(),
    children: h('p', null, 'panel'),
    ...over,
  }
  const b = new LazyBoundary(props)
  // setState needs a mounted updater; apply what it would set instead.
  b.setState = ((u: unknown) => {
    const patch = typeof u === 'function' ? (u as (s: typeof b.state) => object)(b.state) : u
    b.state = { ...b.state, ...(patch as object) }
  }) as typeof b.setState
  return { b, props }
}

function fail(b: LazyBoundary, value: unknown) {
  b.state = { ...b.state, ...LazyBoundary.getDerivedStateFromError(value) }
}

const ICON =
  '<svg class="lazy-panel__error-icon" width="20" height="20" viewBox="0 0 24 24" aria-hidden="true">' +
  '<circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" stroke-width="1.5"></circle>' +
  '<path d="M12 7v6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"></path>' +
  '<circle cx="12" cy="16.5" r="1.1" fill="currentColor"></circle></svg>'

/** The message block; ids come from useId (`_R_0_` for a root render). */
function body(role: boolean) {
  return (
    `<div class="lazy-panel__error"${role ? ' role="alert"' : ''}>${ICON}` +
    '<p id="_R_0_-title" class="lazy-panel__error-title">Couldn&#x27;t load the Strategy Tester</p>' +
    '<p id="_R_0_-text" class="lazy-panel__error-text">Its code did not download. Try again, or ' +
    'reload the page if the app was just updated.</p>' +
    '<div class="lazy-panel__actions">' +
    '<button type="button" class="btn btn--primary">Retry</button>' +
    '<button type="button" class="btn btn--ghost">Close</button></div></div>'
  )
}

const failedView = (variant: PanelLoadingVariant) =>
  renderToStaticMarkup(
    h(LazyLoadFailed, {
      variant,
      name: 'the Strategy Tester',
      onRetry: () => {},
      onClose: () => {},
    }),
  )

describe('LazyLoadFailed: "Couldn\'t load" in the slot the placeholder held', () => {
  it('dock: inside the dock frame, so the chart keeps its height', () => {
    expect(failedView('dock')).toBe(
      `<section class="tester lazy-panel" aria-labelledby="_R_0_-title">${body(true)}</section>`,
    )
  })

  it('overlay (Research) and sidebar (paper trading): the loading placeholder boxes', () => {
    expect(failedView('overlay')).toBe(
      `<div class="lazy-panel lazy-panel--overlay">${body(true)}</div>`,
    )
    expect(failedView('sidebar')).toBe(`<div class="lazy-panel">${body(true)}</div>`)
  })

  it('dialog: an alert dialog card over the shared backdrop', () => {
    expect(failedView('dialog')).toBe(
      '<div class="modal-backdrop modal-backdrop--center">' +
        '<div class="lazy-panel lazy-panel--card" role="alertdialog" aria-modal="true" ' +
        `aria-labelledby="_R_0_-title" aria-describedby="_R_0_-text">${body(false)}</div></div>`,
    )
  })
})

describe('LazyBoundary', () => {
  it('renders its children (keyed by attempt) while nothing failed', () => {
    const { b } = boundary()
    const out = b.render() as ReactElement<{ children: unknown }>
    expect(out.type).toBe(Fragment)
    expect(out.key).toBe('0')
    expect(renderToStaticMarkup(out)).toBe('<p>panel</p>')
  })

  it('a load failure shows LazyLoadFailed in the same slot, with the panel name', () => {
    const { b } = boundary({ variant: 'overlay', name: 'Research' })
    fail(b, loadError())
    const out = b.render() as ReactElement<{ variant: string; name: string }>
    expect(out.type).toBe(LazyLoadFailed)
    expect(out.props.variant).toBe('overlay')
    expect(out.props.name).toBe('Research')
  })

  it('contains the failure: the chart next to it still renders', () => {
    class Failed extends LazyBoundary {
      state = { caught: { value: loadError() as unknown }, attempt: 0 }
    }
    const props: LazyBoundaryProps = {
      variant: 'dock',
      name: 'the Strategy Tester',
      retry() {},
      onClose() {},
      children: h('p', null, 'x'),
    }
    const html = renderToStaticMarkup(
      h('div', { className: 'app' }, h('p', null, 'chart'), h(Failed, props)),
    )
    expect(html).toMatch(/^<div class="app"><p>chart<\/p><section class="tester lazy-panel"/)
    expect(html).toContain('Couldn&#x27;t load the Strategy Tester')
  })

  it('any other error is not a load failure: rethrown to the next boundary up', () => {
    const { b } = boundary()
    const renderError = new Error('bad payload')
    fail(b, renderError)
    expect(() => b.render()).toThrow(renderError)
    fail(b, 'a thrown string')
    expect(() => b.render()).toThrow('a thrown string')
  })

  it('Retry drops the failed attempt and mounts the children afresh', () => {
    const { b, props } = boundary()
    fail(b, loadError())
    b.retry()
    expect(props.retry).toHaveBeenCalledTimes(1)
    expect(props.onClose).not.toHaveBeenCalled()
    expect(b.state.caught).toBeNull()
    const out = b.render() as ReactElement
    // A new key: nothing of the failed attempt's subtree is reused.
    expect(out.key).toBe('1')
    expect(renderToStaticMarkup(out)).toBe('<p>panel</p>')
  })

  it("Close calls the panel's own close handler", () => {
    const { b, props } = boundary()
    fail(b, loadError())
    b.close()
    expect(props.onClose).toHaveBeenCalledTimes(1)
    // Dropping the failed attempt happens on unmount, however the panel closes.
    expect(props.retry).not.toHaveBeenCalled()
  })

  it('unmounted while failed (Close, rail toggle, sidebar switch): the next mount loads again', async () => {
    let calls = 0
    const Panel = retryableLazy('the Strategy Tester', async () => {
      calls += 1
      if (calls === 1) throw new TypeError('Failed to fetch dynamically imported module')
      return () => h('p', null, 'panel')
    })
    const settle = async () => {
      const { prelude } = await prerender(h(Panel, {}))
      return (await new Response(prelude).text()).replace(/<!--\/?\$-->/g, '')
    }
    const err = await settle().catch((e: unknown) => e)
    expect(err).toBeInstanceOf(LazyLoadError)
    const { b } = boundary({ retry: Panel.retry })
    fail(b, err)
    b.componentWillUnmount()
    // Reopened: a fresh download, which now succeeds.
    expect(await settle()).toBe('<p>panel</p>')
    expect(calls).toBe(2)
  })

  it('unmounted after a successful load, or after a non-load error: no retry', () => {
    const ok = boundary()
    ok.b.componentWillUnmount()
    expect(ok.props.retry).not.toHaveBeenCalled()
    const other = boundary()
    fail(other.b, new Error('render'))
    other.b.componentWillUnmount()
    expect(other.props.retry).not.toHaveBeenCalled()
  })

  it('logs load failures with their cause; other errors are left to the boundary above', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { b } = boundary()
    const err = loadError()
    b.componentDidCatch(err, { componentStack: '\n at X' })
    expect(spy).toHaveBeenCalledWith("Couldn't load the Strategy Tester", err.cause, '\n at X')
    spy.mockClear()
    b.componentDidCatch(new Error('render'), { componentStack: '' })
    expect(spy).not.toHaveBeenCalled()
    spy.mockRestore()
  })
})

describe('lazy dialogs: Suspense, then LazyBoundary, then the dialog', () => {
  it.each([
    ['IndicatorSettings', IndicatorSettings, 'indicator settings'],
    ['AlertDialog', AlertDialog, 'the alert dialog'],
    ['ShortcutsDialog', ShortcutsDialog, 'keyboard shortcuts'],
  ] as const)('%s fails in the dialog slot and closes with its own onClose', (_n, W, name) => {
    const onClose = vi.fn()
    const el = (W as unknown as (p: object) => ReactElement)({ onClose })
    expect(el.type).toBe(Suspense)
    const inner = (el.props as { children: ReactElement<LazyBoundaryProps> }).children
    expect(inner.type).toBe(LazyBoundary)
    expect(inner.props.variant).toBe('dialog')
    expect(inner.props.name).toBe(name)
    expect(inner.props.onClose).toBe(onClose)
    expect(inner.props.retry).toBeTypeOf('function')
  })
})
