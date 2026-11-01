import { afterEach, describe, expect, it, vi } from 'vitest'

// Phase 19 wave 5 (W5-C2): `useModalKeys` gained element handlers so the hand-rolled Escape
// handlers could go. These tests replay every old handler (copied verbatim below as reference
// implementations) and the new rules over the same keys, flags and focus layouts, and require
// identical calls: preventDefault, stopPropagation, close, and which element gets focus.
// No DOM here: React's hooks are stubbed and `document` is a fake.
const effects = vi.hoisted(() => [] as (() => void | (() => void))[])
// Refs persist across "renders" by call order, like React's; `rerender()` restarts the order.
const refs = vi.hoisted(() => ({ slots: [] as { current: unknown }[], next: 0 }))
vi.mock('react', async (orig) => {
  const actual = await orig<typeof import('react')>()
  return {
    ...actual,
    useRef: (current: unknown) => (refs.slots[refs.next++] ??= { current }),
    useEffect: (fn: () => void | (() => void)) => void effects.push(fn),
    useCallback: <T>(fn: T) => fn,
  }
})

const { elementKeyRules, handleModalKey, useModalKeys } = await import('./useModalKeys')
type Rules = import('./useModalKeys').ModalKeyRules

// ---------- fakes ----------

interface FakeEl {
  name: string
  focus(): void
}
interface FakeRoot extends FakeEl {
  selectors: string[]
  querySelectorAll(sel: string): FakeEl[]
  contains(n: unknown): boolean
}
interface Ev {
  key: string
  shiftKey: boolean
  defaultPrevented: boolean
  isComposing: boolean
  preventDefault(): void
  stopPropagation(): void
}

let log: string[] = []
const el = (name: string): FakeEl => ({ name, focus: () => log.push(`focus:${name}`) })

function fakeRoot(count: number): { root: FakeRoot; items: FakeEl[] } {
  const items = Array.from({ length: count }, (_, i) => el(`item${i}`))
  const root: FakeRoot = {
    ...el('root'),
    selectors: [],
    querySelectorAll(sel) {
      this.selectors.push(sel)
      return items
    },
    contains: (n) => n === root || items.includes(n as FakeEl),
  }
  return { root, items }
}

function ev(key: string, over: Partial<Omit<Ev, 'preventDefault' | 'stopPropagation'>> = {}): Ev {
  return {
    key,
    shiftKey: false,
    defaultPrevented: false,
    isComposing: false,
    preventDefault: () => log.push('preventDefault'),
    stopPropagation: () => log.push('stopPropagation'),
    ...over,
  }
}

let active: unknown = null
function stubDocument(listeners?: ((e: Ev) => void)[]): void {
  vi.stubGlobal('document', {
    get activeElement() {
      return active
    },
    addEventListener: (type: string, fn: (e: Ev) => void) => {
      if (type === 'keydown') listeners?.push(fn)
    },
    removeEventListener: (_type: string, fn: (e: Ev) => void) => {
      const i = listeners?.indexOf(fn) ?? -1
      if (i >= 0) listeners?.splice(i, 1)
    },
  })
}

function rerender(): void {
  refs.next = 0
  effects.length = 0
}

afterEach(() => {
  vi.unstubAllGlobals()
  refs.slots = []
  refs.next = 0
  effects.length = 0
  log = []
  active = null
})

// ---------- the old handlers, verbatim apart from types ----------

type Old = (e: Ev, root: FakeRoot | null, close: () => void) => void

/** TimeframePicker panel, ChartTypeMenu / LayoutMenu Escape case, DrawingToolbar, AlertsPanel x2. */
const oldEscapeOnly: Old = (e, _root, close) => {
  if (e.key === 'Escape') {
    e.preventDefault()
    e.stopPropagation()
    close()
  }
}

/** DrawingStyleBar popover panel. */
const oldStopAll: Old = (e, _root, close) => {
  e.stopPropagation()
  if (e.key !== 'Escape') return
  e.preventDefault()
  close()
}

/** PaperTradingPanel reset form. */
const oldPaperReset: Old = (e, _root, onCancel) => {
  if (e.key === 'Escape') {
    e.stopPropagation()
    onCancel()
  }
}

/** ChartContextMenu, its Escape/Tab part (count > 0 and count === 0 alike). */
const oldContextMenu: Old = (e, _root, onClose) => {
  e.stopPropagation()
  if (e.key === 'Escape' || e.key === 'Tab') {
    e.preventDefault()
    onClose()
  }
}

/** IndicatorMenu, SymbolSearch, IndicatorSettings (each with its own selector). */
const oldDialog =
  (selector: string): Old =>
  (e, dialog, onClose) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      onClose()
      return
    }
    if (e.key !== 'Tab' || !dialog) return
    const focusables = Array.from(dialog.querySelectorAll(selector))
    if (focusables.length === 0) return
    const first = focusables[0]
    const last = focusables[focusables.length - 1]
    if (e.shiftKey && document.activeElement === (first as unknown)) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && document.activeElement === (last as unknown)) {
      e.preventDefault()
      first.focus()
    }
  }

/** AlertDialog. */
const oldAlertDialog =
  (selector: string): Old =>
  (e, dialog, onClose) => {
    e.stopPropagation()
    if (e.key === 'Escape') {
      e.preventDefault()
      onClose()
      return
    }
    if (e.key !== 'Tab' || !dialog) return
    const focusables = Array.from(dialog.querySelectorAll(selector))
    if (focusables.length === 0) return
    const first = focusables[0]
    const last = focusables[focusables.length - 1]
    if (e.shiftKey && document.activeElement === (first as unknown)) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && document.activeElement === (last as unknown)) {
      e.preventDefault()
      first.focus()
    }
  }

const DOC_FOCUSABLE =
  'button:not([disabled]):not([tabindex="-1"]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex="0"]'

/** The document listener `useModalKeys` had before this change (Settings, Shortcuts, Seasonals). */
const oldDocument: Old = (e, root, onClose) => {
  if (!root) return
  e.stopPropagation()
  if (e.defaultPrevented || e.isComposing) return
  if (e.key === 'Escape') {
    e.preventDefault()
    onClose()
    return
  }
  if (e.key !== 'Tab') return
  const focusables = Array.from(root.querySelectorAll(DOC_FOCUSABLE))
  const act = document.activeElement
  if (focusables.length === 0) {
    e.preventDefault()
    root.focus()
    return
  }
  const first = focusables[0]
  const last = focusables[focusables.length - 1]
  if (!root.contains(act)) {
    e.preventDefault()
    ;(e.shiftKey ? last : first).focus()
  } else if (e.shiftKey && act === (first as unknown)) {
    e.preventDefault()
    last.focus()
  } else if (!e.shiftKey && act === (last as unknown)) {
    e.preventDefault()
    first.focus()
  }
}

// ---------- the matrix ----------

const SEL_MENU = 'button:not([disabled]):not([tabindex="-1"]), input, [tabindex="0"]'
const SEL_SETTINGS = 'button:not([disabled]):not([tabindex="-1"]), input, select, [tabindex="0"]'
const SEL_ALERT =
  'button:not([disabled]):not([tabindex="-1"]), input:not([readonly]), select, textarea'

const SITES: [string, Old, Rules][] = [
  ['TimeframePicker / ChartTypeMenu / LayoutMenu Escape', oldEscapeOnly, elementKeyRules()],
  ['DrawingToolbar confirm', oldEscapeOnly, elementKeyRules()],
  ['AlertsPanel delete confirm', oldEscapeOnly, elementKeyRules()],
  ['AlertsPanel clear-log confirm', oldEscapeOnly, elementKeyRules()],
  ['DrawingStyleBar popover', oldStopAll, elementKeyRules({ stop: 'all' })],
  ['PaperTradingPanel reset form', oldPaperReset, elementKeyRules({ preventDefault: false })],
  ['ChartContextMenu', oldContextMenu, elementKeyRules({ stop: 'all', tabCloses: true })],
  ['IndicatorMenu', oldDialog(SEL_MENU), elementKeyRules({ trap: SEL_MENU })],
  ['SymbolSearch', oldDialog(SEL_MENU), elementKeyRules({ trap: SEL_MENU })],
  ['IndicatorSettings', oldDialog(SEL_SETTINGS), elementKeyRules({ trap: SEL_SETTINGS })],
  ['AlertDialog', oldAlertDialog(SEL_ALERT), elementKeyRules({ stop: 'all', trap: SEL_ALERT })],
]

const KEYS = ['Escape', 'Tab', 'Enter', ' ', 'a', 'ArrowDown', 'Delete']
const FLAGS = [false, true]
/** Focus layouts: how many focusables, and where focus is. */
const LAYOUTS: [number, 'first' | 'middle' | 'last' | 'outside' | 'root'][] = [
  [0, 'outside'],
  [0, 'root'],
  [1, 'first'],
  [3, 'first'],
  [3, 'middle'],
  [3, 'last'],
  [3, 'outside'],
]

function run(handler: Old, e: Ev, count: number, at: string, withRoot: boolean) {
  log = []
  const { root, items } = fakeRoot(count)
  active =
    at === 'first'
      ? items[0]
      : at === 'last'
        ? items[items.length - 1]
        : at === 'middle'
          ? items[1]
          : at === 'root'
            ? root
            : el('outside')
  handler(e, withRoot ? root : null, () => log.push('close'))
  return { log: [...log], selectors: root.selectors }
}

function* cases() {
  for (const key of KEYS)
    for (const shiftKey of FLAGS)
      for (const defaultPrevented of FLAGS)
        for (const isComposing of FLAGS)
          for (const [count, at] of LAYOUTS)
            for (const withRoot of FLAGS)
              yield { key, shiftKey, defaultPrevented, isComposing, count, at, withRoot }
}

describe('handleModalKey matches every hand-rolled handler it replaced', () => {
  it.each(SITES)('%s', (_name, old, rules) => {
    stubDocument()
    let n = 0
    for (const c of cases()) {
      const flags = { shiftKey: c.shiftKey, defaultPrevented: c.defaultPrevented }
      const want = run(
        old,
        ev(c.key, { ...flags, isComposing: c.isComposing }),
        c.count,
        c.at,
        c.withRoot,
      )
      const got = run(
        (e, root, close) => void handleModalKey(e, root as unknown as HTMLElement, close, rules),
        ev(c.key, { ...flags, isComposing: c.isComposing }),
        c.count,
        c.at,
        c.withRoot,
      )
      expect({ c, ...got }).toEqual({ c, ...want })
      n++
    }
    expect(n).toBe(KEYS.length * 2 * 2 * 2 * LAYOUTS.length * 2)
  })

  it('the old document listener too (stop all, skip handled keys, strict trap)', () => {
    const rules: Rules = {
      stop: 'all',
      skipHandled: true,
      preventDefault: true,
      tabCloses: false,
      trap: DOC_FOCUSABLE,
      strictTrap: true,
    }
    stubDocument()
    for (const c of cases()) {
      const make = () =>
        ev(c.key, {
          shiftKey: c.shiftKey,
          defaultPrevented: c.defaultPrevented,
          isComposing: c.isComposing,
        })
      const want = run(oldDocument, make(), c.count, c.at, c.withRoot)
      const got = run(
        (e, root, close) => {
          if (root) handleModalKey(e, root as unknown as HTMLElement, close, rules)
        },
        make(),
        c.count,
        c.at,
        c.withRoot,
      )
      expect({ c, ...got }).toEqual({ c, ...want })
    }
  })

  it('returns true only when the key closed', () => {
    stubDocument()
    const rules = elementKeyRules({ tabCloses: true })
    expect(handleModalKey(ev('Escape'), null, () => {}, rules)).toBe(true)
    expect(handleModalKey(ev('Tab'), null, () => {}, rules)).toBe(true)
    expect(handleModalKey(ev('Enter'), null, () => {}, rules)).toBe(false)
    expect(handleModalKey(ev('Tab'), null, () => {}, elementKeyRules())).toBe(false)
  })
})

describe('useModalKeys', () => {
  it('default: one document keydown listener while mounted, using the latest onClose', () => {
    const listeners: ((e: Ev) => void)[] = []
    stubDocument(listeners)
    const { root } = fakeRoot(2)
    const ref = { current: root as unknown as HTMLElement }
    const first = vi.fn()
    expect(useModalKeys(ref, first)).toBeUndefined()
    const cleanups = effects.map((fn) => fn())
    expect(listeners).toHaveLength(1)

    // A re-render with a new onClose: its ref-sync effect runs, the listener stays the same.
    const second = vi.fn()
    rerender()
    useModalKeys(ref, second)
    effects[0]()
    const esc = ev('Escape')
    listeners[0](esc)
    expect(first).not.toHaveBeenCalled()
    expect(second).toHaveBeenCalledTimes(1)
    expect(log).toEqual(['stopPropagation', 'preventDefault'])

    // Escape already handled inside the dialog: stopped, not closed again.
    log = []
    listeners[0](ev('Escape', { defaultPrevented: true }))
    expect(second).toHaveBeenCalledTimes(1)
    expect(log).toEqual(['stopPropagation'])

    for (const c of cleanups) if (typeof c === 'function') c()
    expect(listeners).toEqual([])
  })

  it("listen: 'element' adds no listener and returns this render's handler", () => {
    const listeners: ((e: Ev) => void)[] = []
    stubDocument(listeners)
    const { root, items } = fakeRoot(3)
    const ref = { current: root as unknown as HTMLElement }
    const onClose = vi.fn()
    const handler = useModalKeys(ref, onClose, { listen: 'element', trap: SEL_MENU })
    for (const fn of effects) fn()
    expect(listeners).toEqual([])

    active = items[2]
    expect(handler(ev('Tab'))).toBe(false)
    expect(log).toEqual(['preventDefault', 'focus:item0'])
    expect(root.selectors).toEqual([SEL_MENU])

    log = []
    expect(handler(ev('Escape'))).toBe(true)
    expect(log).toEqual(['preventDefault', 'stopPropagation'])
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('an element handler with a null ref still closes on Escape (no trap)', () => {
    stubDocument()
    const onClose = vi.fn()
    const handler = useModalKeys(null, onClose, { listen: 'element', trap: SEL_MENU })
    handler(ev('Tab'))
    expect(log).toEqual([])
    handler(ev('Escape'))
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
