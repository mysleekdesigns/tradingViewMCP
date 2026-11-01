import { isValidElement, type ReactElement, type ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Phase 19 wave 5 (W5-C2): every migrated Escape handler, wired in its real component. Each
// component is called as a plain function through a stand-in hook runtime (no DOM in this
// suite); the test finds the element that owns the keys and fires key events at its onKeyDown.
// It pins what each site did before the migration: which keys close, preventDefault or not,
// which keys stop propagating (so chart shortcuts behind it don't fire), where focus returns.
vi.mock('react', async (orig) => ({
  ...(await orig<typeof import('react')>()),
  ...(await import('./testHooks')),
}))
vi.mock('react/compiler-runtime', async () => ({ c: (await import('./testHooks')).c }))

const { hooks, render, resetHooks, unmount } = await import('./testHooks')
const { TimeframePicker } = await import('./TimeframePicker')
const { ChartTypeMenu } = await import('./ChartTypeMenu')
const { LayoutMenu } = await import('./LayoutMenu')
const { DrawingToolbar } = await import('./DrawingToolbar')
const { DrawingStyleBar } = await import('./DrawingStyleBar')
const { AlertsPanel } = await import('./AlertsPanel')
const { PaperTradingPanel } = await import('./PaperTradingPanel')
const { ChartContextMenu } = await import('./ChartContextMenu')
const { IndicatorMenu } = await import('./IndicatorMenu')
const { SymbolSearch } = await import('./SymbolSearch')
const { IndicatorSettings } = await import('./IndicatorSettings')
const { AlertDialog } = await import('./AlertDialog')
const { CHART_TYPES } = await import('../types')
const { INDICATORS } = await import('../indicators/registry')
const { createIndicatorConfig } = await import('../state/indicators')
const { createAccount } = await import('../state/paper')
const { DEFAULT_DRAWING_STYLE } = await import('../chart/drawings/types')

// ---------- tree helpers ----------

type Props = Record<string, unknown> & {
  children?: ReactNode
  ref?: { current: unknown }
  onKeyDown?(e: unknown): void
}
type El = ReactElement<Props>

function elements(node: unknown, out: El[] = []): El[] {
  if (Array.isArray(node)) node.forEach((n) => elements(n, out))
  else if (isValidElement(node)) {
    out.push(node as El)
    elements((node as El).props.children, out)
  }
  return out
}

/** The plain function behind a component (unwraps React.memo). */
function fnOf(type: unknown): (p: unknown) => ReactNode {
  const t = type as { type?: unknown }
  return (typeof type === 'function' ? type : t.type) as (p: unknown) => ReactNode
}

const byName = (els: El[], name: string) =>
  els.filter((e) => typeof e.type !== 'string' && fnOf(e.type)?.name === name)

/** Renders a component element's function with these props; returns its elements. */
function draw(type: unknown, props: unknown): El[] {
  return elements(render(() => fnOf(type)(props)))
}

/** `draw` for a different component than the last one: its own hook slots, no recorded updates. */
function drawChild(type: unknown, props: unknown): El[] {
  unmount()
  resetHooks()
  return draw(type, props)
}

const find = (els: El[], pred: (p: Props, e: El) => boolean): El => {
  const hit = els.find((e) => pred(e.props, e))
  if (!hit) throw new Error('element not found')
  return hit
}

// ---------- events and fakes ----------

let log: string[] = []
interface Key {
  key: string
  shiftKey: boolean
  defaultPrevented: boolean
  preventDefault(): void
  stopPropagation(): void
}
const key = (k: string, shiftKey = false): Key => ({
  key: k,
  shiftKey,
  defaultPrevented: false,
  preventDefault: () => log.push('preventDefault'),
  stopPropagation: () => log.push('stopPropagation'),
})

/** Fires a key and returns what happened, in order. */
function press(el: El, k: string, shiftKey = false): string[] {
  log = []
  el.props.onKeyDown?.(key(k, shiftKey))
  return log
}

const focusable = (name: string) => ({ name, focus: () => log.push(`focus:${name}`) })

let active: unknown = null

beforeEach(() => {
  const noop = () => {}
  vi.stubGlobal('document', {
    get activeElement() {
      return active
    },
    body: {},
    addEventListener: noop,
    removeEventListener: noop,
    getElementById: () => null,
  })
  vi.stubGlobal('window', {
    addEventListener: noop,
    removeEventListener: noop,
    setInterval: () => 1,
    clearInterval: noop,
    innerWidth: 1200,
    innerHeight: 800,
  })
  vi.stubGlobal('requestAnimationFrame', (fn: () => void) => {
    fn()
    return 1
  })
  vi.stubGlobal('HTMLElement', class {})
})

afterEach(() => {
  unmount()
  resetHooks()
  vi.unstubAllGlobals()
  log = []
  active = null
})

/** The first `useState(false)` of the next render starts true (a menu or confirm already open). */
function openFirstFalse(): void {
  let done = false
  hooks.initState = (v) => {
    if (v !== false || done) return v
    done = true
    return true
  }
}

const updates = () => hooks.updates.map((u) => u.value)

// ---------- menus (useMenu) ----------

describe('TimeframePicker', () => {
  function open() {
    openFirstFalse()
    const onChange = vi.fn()
    const els = draw(TimeframePicker, { value: '1h', onChange })
    const trigger = find(els, (p) => p['aria-label'] === 'More timeframes')
    trigger.props.ref!.current = focusable('trigger')
    const panel = find(els, (p) => p.role === 'dialog')
    const list = find(els, (p) => p.role === 'listbox')
    return { panel, list, onChange }
  }

  it('Escape on the panel closes, refocuses the trigger, and goes no further', () => {
    const { panel } = open()
    expect(press(panel, 'Escape')).toEqual(['preventDefault', 'stopPropagation', 'focus:trigger'])
    expect(updates()).toEqual([false])
  })

  it('Tab on the panel is left to the browser (the panel closes on blur instead)', () => {
    const { panel } = open()
    expect(press(panel, 'Tab')).toEqual([])
    expect(updates()).toEqual([])
  })

  it('list arrows, Home and End move the active option with preventDefault', () => {
    const { list } = open()
    for (const k of ['ArrowDown', 'ArrowUp', 'Home', 'End']) {
      expect(press(list, k)).toEqual(['preventDefault'])
    }
    expect(press(list, 'ArrowLeft')).toEqual([])
    expect(hooks.updates).toHaveLength(4)
  })
})

describe('ChartTypeMenu', () => {
  function open() {
    openFirstFalse()
    const onChange = vi.fn()
    const els = draw(ChartTypeMenu, { value: CHART_TYPES[0], onChange })
    const trigger = find(els, (p) => p.title === 'Chart type' && p['aria-haspopup'] === 'listbox')
    trigger.props.ref!.current = focusable('trigger')
    return { list: find(els, (p) => p.role === 'listbox'), onChange }
  }

  it('Escape closes, refocuses the trigger and stops there', () => {
    const { list } = open()
    expect(press(list, 'Escape')).toEqual(['preventDefault', 'stopPropagation', 'focus:trigger'])
    expect(updates()).toEqual([false])
  })

  it('Tab closes without refocus or preventDefault', () => {
    const { list } = open()
    expect(press(list, 'Tab')).toEqual([])
    expect(updates()).toEqual([false])
  })

  it('ArrowDown then Enter picks the next type and refocuses the trigger', () => {
    const { list, onChange } = open()
    expect(press(list, 'ArrowDown')).toEqual(['preventDefault'])
    const next = find(
      draw(ChartTypeMenu, { value: CHART_TYPES[0], onChange }),
      (p) => p.role === 'listbox',
    )
    expect(next.props['aria-activedescendant']).toBe(`id-${CHART_TYPES[1]}`)
    expect(press(next, 'Enter')).toEqual(['preventDefault', 'focus:trigger'])
    expect(onChange).toHaveBeenCalledWith(CHART_TYPES[1])
  })
})

describe('LayoutMenu', () => {
  function open() {
    openFirstFalse()
    const props = {
      kind: '1',
      onKindChange: vi.fn(),
      syncSymbol: false,
      onSyncSymbolChange: vi.fn(),
      syncCrosshair: false,
      onSyncCrosshairChange: vi.fn(),
    }
    const els = draw(LayoutMenu, props)
    find(els, (p) => p.title === 'Chart layout and sync').props.ref!.current = focusable('trigger')
    const menu = find(els, (p) => p.role === 'menu')
    const items = ['tile1', 'tile2', 'tile4', 'sync-symbol', 'sync-crosshair'].map(focusable)
    menu.props.ref!.current = { querySelectorAll: () => items }
    return { menu, items }
  }

  it('Escape closes, refocuses the trigger and stops there', () => {
    const { menu } = open()
    expect(press(menu, 'Escape')).toEqual(['preventDefault', 'stopPropagation', 'focus:trigger'])
    expect(updates()).toEqual([false])
  })

  it('Tab closes without refocus or preventDefault', () => {
    const { menu } = open()
    expect(press(menu, 'Tab')).toEqual([])
    expect(updates()).toEqual([false])
  })

  it('arrows (all four), Home and End move DOM focus over the items, wrapping', () => {
    const { menu, items } = open()
    active = items[0]
    expect(press(menu, 'ArrowRight')).toEqual(['preventDefault', 'focus:tile2'])
    expect(press(menu, 'ArrowUp')).toEqual(['preventDefault', 'focus:sync-crosshair'])
    active = items[4]
    expect(press(menu, 'ArrowDown')).toEqual(['preventDefault', 'focus:tile1'])
    expect(press(menu, 'ArrowLeft')).toEqual(['preventDefault', 'focus:sync-symbol'])
    expect(press(menu, 'Home')).toEqual(['preventDefault', 'focus:tile1'])
    expect(press(menu, 'End')).toEqual(['preventDefault', 'focus:sync-crosshair'])
    expect(press(menu, 'Enter')).toEqual([])
  })
})

// ---------- element handlers (useModalKeys listen: 'element') ----------

describe('DrawingToolbar remove-all confirm', () => {
  it('Escape closes and refocuses the trash button; other keys pass', () => {
    openFirstFalse()
    const els = draw(DrawingToolbar, {
      tool: 'cursor',
      onToolChange: vi.fn(),
      symbol: 'BTCUSDT',
      drawingCount: 2,
      onClearAll: vi.fn(),
    })
    find(els, (p) => p.title === 'Remove all drawings').props.ref!.current = focusable('trash')
    const pop = find(els, (p) => p.role === 'alertdialog')
    expect(press(pop, 'Enter')).toEqual([])
    expect(press(pop, 'Escape')).toEqual(['preventDefault', 'stopPropagation', 'focus:trash'])
    expect(updates()).toEqual([false])
  })
})

describe('DrawingStyleBar popover', () => {
  it('every key stops at the panel; Escape also closes and refocuses its button', () => {
    const els = draw(DrawingStyleBar, {
      drawing: {
        id: 'd1',
        symbol: 'BTCUSDT',
        kind: 'hline',
        anchors: [{ time: 1, price: 1 }],
        style: DEFAULT_DRAWING_STYLE,
      },
      onChange: vi.fn(),
      onDelete: vi.fn(),
    })
    const pop = byName(els, 'StylePopover')[0]
    const onClose = vi.fn(() => log.push('onClose'))
    const inner = drawChild(pop.type, { ...pop.props, open: true, onClose })
    find(inner, (p) => p['aria-haspopup'] === 'listbox').props.ref!.current = focusable('button')
    const panel = find(inner, (p) => p.className === 'draw-pop__panel')
    expect(press(panel, 'Delete')).toEqual(['stopPropagation'])
    expect(press(panel, 'Escape')).toEqual([
      'stopPropagation',
      'preventDefault',
      'onClose',
      'focus:button',
    ])
  })
})

describe('AlertsPanel inline confirms', () => {
  const alert = {
    id: 'a1',
    symbol: 'BTCUSDT',
    condition: 'crossing',
    price: 100,
    frequency: 'once',
    active: true,
    note: '',
    createdAt: 1,
    updatedAt: 1,
    lastTriggeredAt: null,
  }
  const panelProps = (over: Record<string, unknown> = {}) => ({
    alerts: [alert],
    events: [
      {
        id: 1,
        alertId: 'a1',
        symbol: 'BTCUSDT',
        condition: 'crossing',
        level: 100,
        price: 101,
        triggeredAt: 2,
        message: 'm',
      },
    ],
    status: 'ready',
    activeSymbol: 'BTCUSDT',
    notificationPermission: 'granted',
    onRequestPermission: vi.fn(),
    onCreate: vi.fn(),
    onSelectSymbol: vi.fn(),
    onEdit: vi.fn(),
    onToggleActive: vi.fn(),
    onDelete: vi.fn(),
    onClearLog: vi.fn(),
    formatPrice: (_s: string, p: number) => String(p),
    ...over,
  })

  it('delete confirm: Escape cancels and refocuses the delete button; other keys pass', () => {
    const groups = byName(draw(AlertsPanel, panelProps()), 'AlertGroup')
    const group = groups.find((g) => (g.props.alerts as unknown[]).length > 0)!
    const row = byName(drawChild(group.type, group.props), 'AlertRow')[0]
    const setConfirmDelete = vi.fn(() => log.push('setConfirmDelete'))
    const props = { ...row.props, confirmDelete: 'a1', setConfirmDelete }
    // First render (not confirming) holds the delete button and its ref.
    const before = drawChild(row.type, { ...props, confirmDelete: null })
    const del = find(before, (p) => typeof p.title === 'string' && p.title.startsWith('Delete'))
    del.props.ref!.current = focusable('delete')
    const confirm = find(draw(row.type, props), (p) => p.className === 'inline-confirm')
    expect(press(confirm, 'Tab')).toEqual([])
    expect(press(confirm, 'Escape')).toEqual([
      'preventDefault',
      'stopPropagation',
      'setConfirmDelete',
      'focus:delete',
    ])
    expect(setConfirmDelete).toHaveBeenCalledWith(null)
  })

  it('clear-log confirm: Escape closes and refocuses the Clear button; other keys pass', () => {
    hooks.initState = (v) => (v === 'alerts' ? 'log' : v)
    const footer = byName(draw(AlertsPanel, panelProps()), 'ClearLogFooter')[0]
    const setOpen = vi.fn(() => log.push('setOpen'))
    const closed = drawChild(footer.type, { ...footer.props, open: false, setOpen })
    find(closed, (p) => p.type === 'button').props.ref!.current = focusable('clear')
    const confirm = find(draw(footer.type, { ...footer.props, open: true, setOpen }), (p) =>
      String(p.className).startsWith('inline-confirm'),
    )
    expect(press(confirm, 'Enter')).toEqual([])
    expect(press(confirm, 'Escape')).toEqual([
      'preventDefault',
      'stopPropagation',
      'setOpen',
      'focus:clear',
    ])
    expect(setOpen).toHaveBeenCalledWith(false)
  })
})

describe('PaperTradingPanel reset form', () => {
  it('Escape cancels and stops, without preventDefault; other keys pass', () => {
    openFirstFalse()
    const els = draw(PaperTradingPanel, {
      symbol: 'BTCUSDT',
      price: 50_000,
      time: 1_700_000_000,
      scope: 'live',
      paper: {
        scope: 'live',
        account: createAccount(),
        highWater: null,
        place: vi.fn(),
        cancel: vi.fn(),
        onPrice: vi.fn(),
        onBar: vi.fn(),
        reset: vi.fn(),
        restart: vi.fn(),
      },
    })
    const reset = byName(els, 'ResetForm')[0]
    const onCancel = vi.fn(() => log.push('onCancel'))
    const form = find(
      drawChild(reset.type, { ...reset.props, onCancel }),
      (_p, e) => e.type === 'form',
    )
    expect(press(form, 'Enter')).toEqual([])
    expect(press(form, 'Escape')).toEqual(['stopPropagation', 'onCancel'])
  })
})

describe('ChartContextMenu', () => {
  function menu(price: number | null) {
    const onClose = vi.fn(() => log.push('onClose'))
    const els = draw(ChartContextMenu, {
      request: { clientX: 10, clientY: 10, price, time: null, barTime: null, drawingId: null },
      symbol: 'BTCUSDT',
      formatPrice: String,
      alertsAvailable: true,
      onAddAlert: vi.fn(),
      onAddHorizontalLine: vi.fn(),
      onDeleteDrawing: vi.fn(),
      onClose,
    })
    return find(els, (p) => p.role === 'menu')
  }

  it('every key stops at the menu; Escape and Tab close with preventDefault', () => {
    const m = menu(100)
    expect(press(m, 'a')).toEqual(['stopPropagation'])
    expect(press(m, 'Escape')).toEqual(['stopPropagation', 'preventDefault', 'onClose'])
    expect(press(m, 'Tab')).toEqual(['stopPropagation', 'preventDefault', 'onClose'])
    expect(press(m, 'ArrowDown')).toEqual(['stopPropagation', 'preventDefault'])
  })

  it('with no items, arrows do nothing but Escape and Tab still close', () => {
    const m = menu(null)
    expect(press(m, 'ArrowDown')).toEqual(['stopPropagation'])
    expect(press(m, 'Escape')).toEqual(['stopPropagation', 'preventDefault', 'onClose'])
    expect(press(m, 'Tab')).toEqual(['stopPropagation', 'preventDefault', 'onClose'])
  })
})

// ---------- dialogs with a Tab trap ----------

const SEL_MENU = 'button:not([disabled]):not([tabindex="-1"]), input, [tabindex="0"]'

function dialogCase(
  name: string,
  renderDialog: (onClose: () => void) => El[],
  selector: string,
  stopsEveryKey: boolean,
) {
  describe(`${name} (Tab trap)`, () => {
    function setup() {
      const onClose = vi.fn(() => log.push('onClose'))
      const dialog = find(renderDialog(onClose), (p) => p.role === 'dialog' && !!p.onKeyDown)
      const items = ['first', 'middle', 'last'].map(focusable)
      const selectors: string[] = []
      dialog.props.ref!.current = {
        querySelectorAll: (sel: string) => (selectors.push(sel), items),
      }
      return { dialog, items, selectors }
    }
    const stop = stopsEveryKey ? ['stopPropagation'] : []

    it('Escape closes and stops there', () => {
      const { dialog } = setup()
      expect(press(dialog, 'Escape')).toEqual(
        stopsEveryKey
          ? ['stopPropagation', 'preventDefault', 'onClose']
          : ['preventDefault', 'stopPropagation', 'onClose'],
      )
    })

    it(`Tab wraps last → first and Shift+Tab first → last, using its own selector`, () => {
      const { dialog, items, selectors } = setup()
      active = items[2]
      expect(press(dialog, 'Tab')).toEqual([...stop, 'preventDefault', 'focus:first'])
      active = items[0]
      expect(press(dialog, 'Tab', true)).toEqual([...stop, 'preventDefault', 'focus:last'])
      active = items[1]
      expect(press(dialog, 'Tab')).toEqual(stop)
      expect(new Set(selectors)).toEqual(new Set([selector]))
    })

    it(stopsEveryKey ? 'every other key stops here' : 'other keys pass', () => {
      const { dialog } = setup()
      expect(press(dialog, 'Delete')).toEqual(stop)
    })
  })
}

dialogCase(
  'IndicatorMenu',
  (onClose) => draw(IndicatorMenu, { items: [], onAdd: () => null, onClose }),
  SEL_MENU,
  false,
)

dialogCase(
  'SymbolSearch',
  (onClose) =>
    draw(SymbolSearch, {
      initialQuery: '',
      symbols: [],
      error: null,
      onRetry: vi.fn(),
      activeSymbol: 'BTCUSDT',
      source: 'Binance',
      isInWatchlist: () => false,
      onToggleWatchlist: vi.fn(),
      onSelect: vi.fn(),
      onClose,
    }),
  SEL_MENU,
  false,
)

dialogCase(
  'IndicatorSettings',
  (onClose) => {
    const def = INDICATORS[0]
    return draw(IndicatorSettings, {
      def,
      config: createIndicatorConfig(def, []),
      onApply: vi.fn(),
      onClose,
    })
  },
  'button:not([disabled]):not([tabindex="-1"]), input, select, [tabindex="0"]',
  false,
)

dialogCase(
  'AlertDialog',
  (onClose) =>
    draw(AlertDialog, {
      symbol: 'BTCUSDT',
      initialPrice: 100,
      lastPrice: 99,
      formatPrice: String,
      onSubmit: vi.fn(async () => {}),
      onClose,
    }),
  'button:not([disabled]):not([tabindex="-1"]), input:not([readonly]), select, textarea',
  true,
)
