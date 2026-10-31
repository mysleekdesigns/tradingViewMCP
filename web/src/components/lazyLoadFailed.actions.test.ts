import type { ReactElement } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

// Phase 19 wave 4 review: the "Couldn't load" view's buttons and keys, called as a plain
// function (no DOM in this suite). Its only hooks (useId, useRef, useEffect) are stubbed, and the
// effects it registers are kept so a test can run them; the markup is in lazyBoundary.test.ts.
const effects = vi.hoisted(() => [] as (() => void | (() => void))[])
vi.mock('react', async (orig) => {
  const actual = await orig<typeof import('react')>()
  return {
    ...actual,
    useId: () => 'id',
    useRef: () => ({ current: null }),
    useEffect: (fn: () => void | (() => void)) => void effects.push(fn),
  }
})

const { LazyLoadFailed } = await import('./lazyPanels')
type PanelLoadingVariant = 'dock' | 'overlay' | 'sidebar' | 'dialog'

describe('LazyLoadFailed actions', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  type Clickable = ReactElement<{
    onClick?(): void
    onPointerDown?(e: unknown): void
    onKeyDown?(e: unknown): void
    children?: unknown
  }>

  /** Every element in a rendered tree (function components are not expanded). */
  function elements(node: unknown, out: Clickable[] = []): Clickable[] {
    if (Array.isArray(node)) node.forEach((n) => elements(n, out))
    else if (node !== null && typeof node === 'object' && 'props' in node) {
      out.push(node as Clickable)
      elements((node as Clickable).props.children, out)
    }
    return out
  }

  function call(variant: PanelLoadingVariant) {
    const onRetry = vi.fn()
    const onClose = vi.fn()
    const tree = LazyLoadFailed({ variant, name: 'x', onRetry, onClose })
    const els = elements(tree)
    const button = (text: string) =>
      els.find((e) => e.type === 'button' && e.props.children === text)!
    return { tree: tree as Clickable, els, button, onRetry, onClose }
  }

  it.each(['dock', 'overlay', 'sidebar', 'dialog'] as const)(
    '%s: Retry retries, Close closes',
    (variant) => {
      const { button, onRetry, onClose } = call(variant)
      button('Retry').props.onClick?.()
      expect(onRetry).toHaveBeenCalledTimes(1)
      expect(onClose).not.toHaveBeenCalled()
      button('Close').props.onClick?.()
      expect(onClose).toHaveBeenCalledTimes(1)
    },
  )

  it('dialog: a backdrop click closes, a click on the card does not', () => {
    const { tree, onClose } = call('dialog')
    const self = {}
    tree.props.onPointerDown?.({ target: {}, currentTarget: self })
    expect(onClose).not.toHaveBeenCalled()
    tree.props.onPointerDown?.({ target: self, currentTarget: self })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  type Key = {
    key: string
    defaultPrevented: boolean
    isComposing: boolean
    target: unknown
    preventDefault(): void
    stopPropagation(): void
  }
  const keyEvent = (key: string, over: Partial<Key> = {}): Key => ({
    key,
    defaultPrevented: false,
    isComposing: false,
    target: {},
    preventDefault: vi.fn(),
    stopPropagation: vi.fn(),
    ...over,
  })

  /** Render, commit the effects, and return the window keydown listeners they added. */
  function mount(variant: PanelLoadingVariant) {
    effects.length = 0
    const listeners: ((e: Key) => void)[] = []
    vi.stubGlobal('window', {
      addEventListener: (type: string, fn: (e: Key) => void) => {
        if (type === 'keydown') listeners.push(fn)
      },
      removeEventListener: () => {},
    })
    // The hook skips keys from inside a dialog (`target instanceof Element`); none of the fake
    // targets here is one.
    vi.stubGlobal('Element', class {})
    const view = call(variant)
    for (const fn of effects) fn()
    return { ...view, listeners }
  }

  it.each(['dialog', 'overlay'] as const)(
    '%s: Escape closes wherever focus is, even on <body> after a background click',
    (variant) => {
      const { listeners, onClose } = mount(variant)
      expect(listeners).toHaveLength(1)
      const press = (e: Key) => listeners.forEach((fn) => fn(e))
      press(keyEvent('Enter'))
      press(keyEvent('Escape', { defaultPrevented: true }))
      expect(onClose).not.toHaveBeenCalled()
      const esc = keyEvent('Escape', { target: { tagName: 'BODY' } })
      press(esc)
      expect(esc.preventDefault).toHaveBeenCalled()
      expect(onClose).toHaveBeenCalledTimes(1)
    },
  )

  it('dialog: Escape from inside the card closes it there and goes no further', () => {
    const { els, onClose } = mount('dialog')
    const card = els.find((e) => e.props.onKeyDown !== undefined)!
    const key = (k: string, defaultPrevented = false) => {
      const e = keyEvent(k, { defaultPrevented })
      card.props.onKeyDown?.(e)
      return e
    }
    expect(key('Enter').stopPropagation).not.toHaveBeenCalled()
    expect(key('Escape', true).stopPropagation).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
    const esc = key('Escape')
    expect(esc.preventDefault).toHaveBeenCalled()
    expect(esc.stopPropagation).toHaveBeenCalled()
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it.each(['dock', 'sidebar'] as const)(
    '%s: not modal, so Escape is left to the app (no listener, no handler)',
    (variant) => {
      const { els, listeners } = mount(variant)
      expect(listeners).toEqual([])
      expect(els.some((e) => e.props.onKeyDown !== undefined)).toBe(false)
    },
  )
})
