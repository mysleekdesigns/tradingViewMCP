import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { IChartApi, UTCTimestamp } from 'lightweight-charts'
import type {
  AccessibilityCommand,
  AccessibilityOptions,
  AnySeries,
  KeyBindings,
} from '@tradingview/lwc-plugin-accessibility'
import { shortcutCommand, type ShortcutKey } from '../components/drawingTools'
import { appShortcut, chartKey } from '../state/shortcuts'
import { replayKeyAction } from '../state/replay'
import {
  A11Y_HELP_KEY,
  A11Y_KEY_BINDINGS,
  A11Y_MESSAGES,
  A11Y_TABLE_KEY,
  accessibilityOptions,
  A11Y_OVERLAY_SELECTOR,
  attachChartAccessibility,
  isAccessibilityOverlay,
  seriesLabelFor,
} from './chartAccessibility'

const controller = {
  plugins: [],
  detach: vi.fn(),
  focus: vi.fn(),
  refresh: vi.fn(),
  applyOptions: vi.fn(),
}
const addAccessibilityPlugin = vi.fn<
  (chart: IChartApi, opts?: AccessibilityOptions) => typeof controller
>(() => controller)

vi.mock(import('@tradingview/lwc-plugin-accessibility'), async (importOriginal) => ({
  ...(await importOriginal()),
  addAccessibilityPlugin: (chart: IChartApi, opts?: AccessibilityOptions) =>
    addAccessibilityPlugin(chart, opts),
}))

const fakeChart = {} as IChartApi
const fmt = {
  title: 'BTCUSDT 1h',
  formatPrice: (p: number) => `$${p.toFixed(2)}`,
  formatTime: (t: number) => `t=${t}`,
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('attachChartAccessibility', () => {
  it('attaches the plugin once to the chart with the app options', () => {
    attachChartAccessibility(fakeChart, fmt)
    expect(addAccessibilityPlugin).toHaveBeenCalledTimes(1)
    const [chart, opts] = addAccessibilityPlugin.mock.calls[0]
    expect(chart).toBe(fakeChart)
    expect(opts).toMatchObject({
      chartTitle: 'BTCUSDT 1h',
      keyBindings: A11Y_KEY_BINDINGS,
      messages: A11Y_MESSAGES,
      dataUpdates: { mode: 'none' },
      syncCrosshair: true,
      focusOnPointerDown: false,
    })
  })

  it('setTitle renames the panes once per change and is a no-op for the same title', () => {
    const a11y = attachChartAccessibility(fakeChart, fmt)
    a11y.setTitle('BTCUSDT 1h')
    expect(controller.applyOptions).not.toHaveBeenCalled()
    a11y.setTitle('ETHUSDT 4h')
    a11y.setTitle('ETHUSDT 4h')
    expect(controller.applyOptions).toHaveBeenCalledTimes(1)
    expect(controller.applyOptions).toHaveBeenCalledWith({ chartTitle: 'ETHUSDT 4h' })
  })

  it('detach is idempotent and setTitle does nothing afterwards', () => {
    const a11y = attachChartAccessibility(fakeChart, fmt)
    a11y.detach()
    a11y.detach()
    expect(controller.detach).toHaveBeenCalledTimes(1)
    a11y.setTitle('ETHUSDT 4h')
    expect(controller.applyOptions).not.toHaveBeenCalled()
  })
})

describe('formatters', () => {
  it('passes prices through and hands UNIX seconds to formatTime', () => {
    const opts = accessibilityOptions(fmt)
    expect(opts.priceFormatter?.(64250.5)).toBe('$64250.50')
    expect(opts.timeFormatter?.(1_700_000_000 as UTCTimestamp)).toBe('t=1700000000')
    expect(opts.timeFormatter?.('2024-01-02')).toBe('t=1704153600')
    expect(opts.timeFormatter?.({ year: 2024, month: 1, day: 2 })).toBe('t=1704153600')
    expect(opts.timeFormatter?.('not a date')).toBe('not a date')
  })

  it('names series by title, then "Price" for candles/bars, then "Series N"', () => {
    const s = (title: string, type: string) =>
      ({ options: () => ({ title }), seriesType: () => type }) as unknown as AnySeries
    expect(seriesLabelFor(s('EMA 20', 'Line'), 3)).toBe('EMA 20')
    expect(seriesLabelFor(s('', 'Candlestick'), 0)).toBe('Price')
    expect(seriesLabelFor(s('', 'Bar'), 0)).toBe('Price')
    expect(seriesLabelFor(s('', 'Histogram'), 1)).toBe('Series 2')
  })
})

describe('spoken messages name the rebound keys', () => {
  it('pane label and help say comma / period, never H, T or Escape', () => {
    const label = A11Y_MESSAGES.paneLabel?.({
      title: 'BTCUSDT 1h',
      paneIndex: 1,
      paneCount: 2,
      seriesCount: 1,
      seriesLabel: 'RSI 14',
    })
    expect(label).toBe('BTCUSDT 1h. Pane 2 of 2. RSI 14. Press comma for keyboard help.')
    const help = A11Y_MESSAGES.help?.({ multiSeries: true, pageStep: 10 }) ?? ''
    expect(help).toContain('Period shows the data as a table')
    expect(help).toContain('Up and down arrows')
    expect(help).not.toMatch(/\b[HT]\b|Escape/)
    expect(A11Y_MESSAGES.tableClose).toBe('Press period to close.')
    const rows = A11Y_MESSAGES.shortcuts?.({ multiSeries: false, pageStep: 10 }) ?? []
    expect(rows.map((r) => r.keys)).toEqual(expect.arrayContaining([A11Y_HELP_KEY, A11Y_TABLE_KEY]))
    expect(rows.some((r) => /^(H|T|Esc)$/.test(r.keys))).toBe(false)
  })
})

// ---- Key conflicts -------------------------------------------------------------------------
//
// The plugin's v1.0.0 key lookup, as documented in its typings and implemented in
// dist/accessibility.js (`keyCommands`, `codeCommands`, `commandForKey`, `_handleKeyDown`):
// Alt/Ctrl/Meta chords are ignored; Shift is ignored unless the key is one printable character;
// custom bindings by key, then by code, win (null removes); then the built-in key map, then the
// built-in code map.
const BUILT_IN_KEYS: Readonly<Record<string, AccessibilityCommand>> = {
  ArrowRight: 'nextPoint',
  ArrowLeft: 'previousPoint',
  ArrowUp: 'previousSeries',
  ArrowDown: 'nextSeries',
  PageUp: 'pageForward',
  PageDown: 'pageBack',
  Home: 'firstPoint',
  End: 'lastPoint',
  '+': 'zoomIn',
  '=': 'zoomIn',
  '-': 'zoomOut',
  _: 'zoomOut',
  Enter: 'summary',
  ' ': 'summary',
  h: 'help',
  H: 'help',
  t: 'viewAsTable',
  T: 'viewAsTable',
  Escape: 'closePanels',
}
const BUILT_IN_CODES: Readonly<Record<string, AccessibilityCommand>> = {
  KeyH: 'help',
  KeyT: 'viewAsTable',
}
const own = (m: object, k: string) => Object.prototype.hasOwnProperty.call(m, k)

function pluginCommand(e: ShortcutKey, bindings: KeyBindings): AccessibilityCommand | null {
  if (e.altKey || e.ctrlKey || e.metaKey) return null
  if (e.shiftKey && e.key.length !== 1) return null
  if (own(bindings, e.key)) return bindings[e.key]
  if (own(bindings, e.code)) return bindings[e.code]
  if (own(BUILT_IN_KEYS, e.key)) return BUILT_IN_KEYS[e.key]
  return own(BUILT_IN_CODES, e.code) ? BUILT_IN_CODES[e.code] : null
}

/** App.tsx's tail: '/' opens the indicator menu, A–Z / 0–9 open symbol search. */
function appTail(e: ShortcutKey): boolean {
  if (e.metaKey || e.ctrlKey || e.altKey) return false
  return e.key === '/' || /^[a-z0-9]$/i.test(e.key)
}

/** Whether any app keyboard handler acts on this press (the chart layer is not a text field). */
function appActsOn(e: ShortcutKey): boolean {
  const cmd = shortcutCommand(e)
  return (
    (cmd !== null && cmd !== 'cursor') ||
    (e.key === 'Escape' && !e.altKey && !e.shiftKey && !e.ctrlKey && !e.metaKey) ||
    ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === 'k') ||
    appShortcut(e, '1h') !== null ||
    chartKey({ ...e, defaultPrevented: false }, false) !== null ||
    replayKeyAction(e, 'active') !== null ||
    replayKeyAction(e, 'picking') !== null ||
    appTail(e)
  )
}

const press = (over: Partial<ShortcutKey>): ShortcutKey => ({
  key: '',
  code: '',
  altKey: false,
  ctrlKey: false,
  metaKey: false,
  shiftKey: false,
  ...over,
})

function allPresses(): ShortcutKey[] {
  const base: { key: string; code: string }[] = []
  for (let c = 0; c < 26; c++) {
    const lower = String.fromCharCode(97 + c)
    const code = `Key${lower.toUpperCase()}`
    base.push({ key: lower, code }, { key: lower.toUpperCase(), code })
  }
  for (let d = 0; d <= 9; d++) base.push({ key: String(d), code: `Digit${d}` })
  for (const [key, code] of [
    ['[', 'BracketLeft'],
    [']', 'BracketRight'],
    ['/', 'Slash'],
    ['?', 'Slash'],
    [',', 'Comma'],
    ['.', 'Period'],
    ['+', 'Equal'],
    ['=', 'Equal'],
    ['-', 'Minus'],
    ['_', 'Minus'],
    [' ', 'Space'],
    // Non-Latin layouts: the physical H and T keys type other letters.
    ['р', 'KeyH'],
    ['е', 'KeyT'],
  ])
    base.push({ key, code })
  for (const key of [
    'Escape',
    'Delete',
    'Backspace',
    'Enter',
    'ArrowLeft',
    'ArrowRight',
    'ArrowUp',
    'ArrowDown',
    'PageUp',
    'PageDown',
    'Home',
    'End',
  ])
    base.push({ key, code: key })
  const out: ShortcutKey[] = []
  for (const b of base)
    for (const altKey of [false, true])
      for (const shiftKey of [false, true])
        for (const mod of ['none', 'ctrl', 'meta'] as const)
          out.push(
            press({ ...b, altKey, shiftKey, ctrlKey: mod === 'ctrl', metaKey: mod === 'meta' }),
          )
  return out
}

describe('A11Y_KEY_BINDINGS vs the app shortcuts', () => {
  it('the plugin defaults would collide (so the rebinding is needed)', () => {
    const collide = allPresses().filter((e) => appActsOn(e) && pluginCommand(e, {}) !== null)
    const keys = new Set(collide.map((e) => e.key))
    // Not the Cyrillic 'р' / 'е' on the physical H / T keys: type-to-search only takes Latin
    // letters and digits, so the app ignores them.
    expect(keys).toEqual(new Set(['h', 'H', 't', 'T', 'Escape']))
  })

  it('the physical H / T fallbacks are removed too, so the spoken key names stay true', () => {
    // A non-Latin layout would otherwise still open help / the table from KeyH / KeyT, while
    // the help text names comma and period.
    expect(pluginCommand(press({ key: 'р', code: 'KeyH' }), {})).toBe('help')
    expect(pluginCommand(press({ key: 'е', code: 'KeyT' }), {})).toBe('viewAsTable')
    expect(pluginCommand(press({ key: 'р', code: 'KeyH' }), A11Y_KEY_BINDINGS)).toBeNull()
    expect(pluginCommand(press({ key: 'е', code: 'KeyT' }), A11Y_KEY_BINDINGS)).toBeNull()
  })

  it('no press the app acts on reaches a plugin command', () => {
    const collide = allPresses().filter(
      (e) => appActsOn(e) && pluginCommand(e, A11Y_KEY_BINDINGS) !== null,
    )
    expect(collide.map((e) => JSON.stringify(e))).toEqual([])
  })

  it('help and table keep a key, and navigation keeps the built-ins', () => {
    const cmd = (key: string, code = key) => pluginCommand(press({ key, code }), A11Y_KEY_BINDINGS)
    expect(cmd(A11Y_HELP_KEY, 'Comma')).toBe('help')
    expect(cmd(A11Y_TABLE_KEY, 'Period')).toBe('viewAsTable')
    expect(cmd('ArrowRight')).toBe('nextPoint')
    expect(cmd('ArrowUp')).toBe('previousSeries')
    expect(cmd('PageUp')).toBe('pageForward')
    expect(cmd('End')).toBe('lastPoint')
    expect(cmd('+', 'Equal')).toBe('zoomIn')
    expect(cmd('Enter')).toBe('summary')
    expect(cmd('Escape')).toBeNull()
    expect(cmd('t', 'KeyT')).toBeNull()
    expect(cmd('р', 'KeyH')).toBeNull()
    // Shift+arrows stay with bar replay.
    expect(
      pluginCommand(
        press({ key: 'ArrowRight', code: 'ArrowRight', shiftKey: true }),
        A11Y_KEY_BINDINGS,
      ),
    ).toBeNull()
  })

  it('the bindings only name real plugin commands', () => {
    const commands = new Set<AccessibilityCommand | null>([...Object.values(BUILT_IN_KEYS), null])
    for (const v of Object.values(A11Y_KEY_BINDINGS)) expect(commands.has(v)).toBe(true)
  })
})

describe('accessibility overlay hit test', () => {
  /** A fake element: its `closest` finds a plugin panel when `inside`. */
  const el = (inside: boolean) => ({
    closest(selector: string) {
      expect(selector).toBe(A11Y_OVERLAY_SELECTOR)
      return inside ? {} : null
    },
  })

  it('targets inside the data table or shortcuts panel are the overlay', () => {
    expect(isAccessibilityOverlay(el(true) as unknown as EventTarget)).toBe(true)
  })

  it('chart canvases, other elements and non-elements are not', () => {
    expect(isAccessibilityOverlay(el(false) as unknown as EventTarget)).toBe(false)
    expect(isAccessibilityOverlay(null)).toBe(false)
    // A text node or window has no `closest`.
    expect(isAccessibilityOverlay({} as EventTarget)).toBe(false)
  })

  it('names the plugin panels that take pointer input', () => {
    expect(A11Y_OVERLAY_SELECTOR.split(',').map((s) => s.trim())).toEqual([
      '.lw-chart-a11y-data-table',
      '.lw-chart-a11y-shortcuts-panel',
    ])
  })
})
