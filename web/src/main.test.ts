import { createElement as h, type ReactElement, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { prerender } from 'react-dom/static'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// main.tsx's only logic besides mounting: reload once when a lazily loaded panel's chunk fails
// to load (the app was rebuilt while this tab stayed open), never in a loop, and never for a
// mere hover/focus preload.
const mounted = vi.hoisted(() => ({ tree: null as unknown, onError: null as (() => void) | null }))
vi.mock('./App.tsx', () => ({ default: () => null }))
vi.mock('react-dom/client', () => ({
  createRoot: () => ({ render: (tree: unknown) => void (mounted.tree = tree) }),
}))
// The rail's hover preload of the tester chunk fails the way Vite's preload helper does after a
// rebuild: it fires `vite:preloadError` while the import is in flight, then rejects.
vi.mock('./components/BacktestPanel', () => {
  mounted.onError?.()
  throw new TypeError('Failed to fetch dynamically imported module')
})
vi.mock('./state/settings', () => ({
  loadSettings: () => ({ theme: 'dark', upColor: '#0f0', downColor: '#f00' }),
  applyThemeToDocument: () => {},
  applyCandleColorsToDocument: () => {},
}))

type Listener = () => void

function storage() {
  const m = new Map<string, string>()
  return {
    getItem: (k: string) => m.get(k) ?? null,
    setItem: (k: string, v: string) => void m.set(k, v),
    raw: m,
  }
}

async function boot(session: unknown) {
  vi.resetModules()
  const listeners = new Map<string, Listener>()
  const reload = vi.fn()
  vi.stubGlobal('window', {
    addEventListener: (type: string, fn: Listener) => void listeners.set(type, fn),
  })
  vi.stubGlobal('document', { getElementById: () => ({}) })
  vi.stubGlobal('location', { reload })
  vi.stubGlobal('sessionStorage', session)
  await import('./main.tsx')
  const onError = listeners.get('vite:preloadError')
  expect(onError).toBeTypeOf('function')
  mounted.onError = onError!
  // The same module instance main.tsx reads `panelLoadInFlight` from.
  const { retryableLazy, LazyLoadError } = await import('./components/lazyLoad')

  /** The user opens a panel whose chunk is gone: Vite fires the event mid-import, then rejects. */
  async function realOpen(): Promise<void> {
    const Panel = retryableLazy('the Strategy Tester', async () => {
      await Promise.resolve()
      onError!()
      throw new TypeError('Failed to fetch dynamically imported module')
    })
    // The load still fails (and LazyBoundary shows it) whether or not the page reloads.
    await expect(prerender(h(Panel, {}))).rejects.toBeInstanceOf(LazyLoadError)
  }
  return { onError: onError!, reload, realOpen }
}

describe('main: lazy chunk load failure', () => {
  beforeEach(() => {
    vi.useFakeTimers({ now: new Date('2026-10-30T12:00:00Z') })
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('reloads once to pick up the new build, then not again within a minute', async () => {
    const session = storage()
    const { realOpen, reload } = await boot(session)
    await realOpen()
    expect(reload).toHaveBeenCalledTimes(1)
    expect(session.raw.get('tv.chunkReloadAt')).toBe(String(Date.now()))
    // The reloaded page fails again (a broken build): leave the error visible, no loop.
    vi.advanceTimersByTime(59_000)
    await realOpen()
    expect(reload).toHaveBeenCalledTimes(1)
    // A later, separate rebuild may reload again.
    vi.advanceTimersByTime(2_000)
    await realOpen()
    expect(reload).toHaveBeenCalledTimes(2)
  })

  it('without sessionStorage (no loop guard) it never reloads', async () => {
    const { realOpen, reload } = await boot({
      getItem: () => {
        throw new Error('denied')
      },
      setItem: () => {},
    })
    await realOpen()
    expect(reload).not.toHaveBeenCalled()
  })

  it('ignores the event while no panel the user opened is loading', async () => {
    const session = storage()
    const { onError, reload } = await boot(session)
    onError()
    expect(reload).not.toHaveBeenCalled()
    // ...and does not spend the once-a-minute reload on it.
    expect(session.raw.has('tv.chunkReloadAt')).toBe(false)
  })

  it('a failed hover or focus preload on the rail never reloads the page', async () => {
    const session = storage()
    const { reload, realOpen } = await boot(session)
    const { SidebarRail } = await import('./components/SidebarRail')
    const tester = railButton(SidebarRail, 'Strategy Tester')
    let fired = 0
    const fire = mounted.onError!
    mounted.onError = () => {
      fired += 1
      fire()
    }
    tester.onPointerEnter()
    tester.onFocus()
    await vi.dynamicImportSettled()
    expect(fired).toBeGreaterThan(0) // the event did fire, as Vite's helper would
    expect(reload).not.toHaveBeenCalled()
    expect(session.raw.has('tv.chunkReloadAt')).toBe(false)
    // The click that follows is a real open: that one reloads.
    await realOpen()
    expect(reload).toHaveBeenCalledTimes(1)
  })
})

/** The rail button's hover and focus handlers. */
function railButton(rail: unknown, label: string) {
  const inner = (rail as { type(p: unknown): ReactElement }).type
  const tree = inner({
    active: null,
    onSelect: () => {},
    alertBadge: 0,
    testerOpen: false,
    onToggleTester: () => {},
    researchOpen: false,
    onToggleResearch: () => {},
  })
  type Props = {
    'aria-label'?: string
    onPointerEnter(): void
    onFocus(): void
    children?: ReactNode
  }
  let found: Props | null = null
  const walk = (node: ReactNode): void => {
    if (Array.isArray(node)) return node.forEach(walk)
    if (node === null || typeof node !== 'object' || !('props' in node)) return
    const props = (node as ReactElement<Props>).props
    if (props['aria-label'] === label) found = props
    walk(props.children)
  }
  walk(tree)
  expect(found).not.toBeNull()
  return found as unknown as Props
}

describe('main: root error boundary', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('mounts App inside it, adding nothing to the page while the app works', async () => {
    await boot(storage())
    const strict = mounted.tree as ReactElement<{ children: ReactElement }>
    const root = strict.props.children as ReactElement<{
      children: ReactElement
      fallback(p: { error: Error; reset(): void }): ReactNode
    }>
    const { ErrorBoundary } = await import('./components/ErrorBoundary')
    const App = (await import('./App.tsx')).default
    expect(root.type).toBe(ErrorBoundary)
    expect(root.props.children.type).toBe(App)
    expect(renderToStaticMarkup(root)).toBe('')
  })

  it('a render error nothing else contains shows a reload card instead of a blank page', async () => {
    const { reload } = await boot(storage())
    const strict = mounted.tree as ReactElement<{ children: ReactElement }>
    const root = strict.props.children as ReactElement<{
      fallback(p: { error: Error; reset(): void }): ReactElement
    }>
    const card = root.props.fallback({ error: new Error('x'), reset: () => {} })
    const html = renderToStaticMarkup(card)
    expect(html).toContain('<div class="state" role="alert"><div class="error-card">')
    expect(html).toContain('Something went wrong')
    expect(html).toContain('>Try again</button>')
    ;(card.props as { onRetry(): void }).onRetry()
    expect(reload).toHaveBeenCalledTimes(1)
  })
})
