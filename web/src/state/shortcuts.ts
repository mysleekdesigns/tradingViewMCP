// App-level keyboard shortcuts (timeframes, screenshot, theme, help) and the list the
// "Keyboard shortcuts" dialog shows. Pure: no DOM access beyond the optional navigator.
//
// Drawing tools (Alt+H/J/T/F/R), Alt+A (alert), Esc and Delete/Backspace are matched elsewhere
// (components/drawingTools.ts `shortcutCommand` and the chart's drawing controller). The codes
// used here (Digit1-7, Numpad1-7, KeyS, Shift+KeyL, '[', ']', '?') never overlap with them.

import { DRAWING_TOOLS, type ShortcutKey } from '../components/drawingTools'
import { INTERVALS, type Interval } from '../types'

export type AppShortcut =
  | { kind: 'interval'; interval: Interval }
  | { kind: 'screenshot' }
  | { kind: 'help' }
  | { kind: 'theme' }

/** Physical code → index into INTERVALS. By code because macOS Option changes `e.key`. */
function digitIndex(code: string): number | null {
  const m = /^(?:Digit|Numpad)([1-9])$/.exec(code)
  if (!m) return null
  const i = Number(m[1]) - 1
  return i < INTERVALS.length ? i : null
}

/**
 * Maps a key press to an app shortcut or null. The caller skips presses in text fields, while a
 * dialog is open, and when e.isComposing/defaultPrevented. Rules:
 * - Alt+1 … Alt+7 (by e.code Digit1..Digit7 or Numpad1..Numpad7, because macOS Option changes e.key) → INTERVALS[i]
 * - e.key '[' / ']' → the previous / next interval relative to `current`, null at the ends. Checked
 *   before the Alt rules, with Alt/Shift allowed: German-style macOS layouts type '[' as Option+5
 *   (e.code Digit5), which must step the interval, never act as Alt+5
 * - Alt+S (e.code KeyS) → screenshot
 * - Alt+Shift+L (e.code KeyL) → theme toggle   (Alt+L alone stays free)
 * - '?' (Shift+/ , e.key '?') with no Ctrl/Meta/Alt → help
 * - Any Ctrl/Meta combination → null (leave browser/OS shortcuts and the existing Cmd/Ctrl+K alone)
 * Must not collide with the existing Alt+H/J/T/F/R/A drawing/alert shortcuts in drawingTools.ts.
 */
export function appShortcut(e: ShortcutKey, current: Interval): AppShortcut | null {
  if (e.ctrlKey || e.metaKey) return null

  // By the character typed, whatever modifiers the layout needs for it (see above).
  if (e.key === '[' || e.key === ']') {
    const i = INTERVALS.indexOf(current)
    if (i < 0) return null
    const j = e.key === '[' ? i - 1 : i + 1
    return j >= 0 && j < INTERVALS.length ? { kind: 'interval', interval: INTERVALS[j] } : null
  }

  if (e.altKey) {
    if (e.shiftKey) return e.code === 'KeyL' ? { kind: 'theme' } : null
    if (e.code === 'KeyS') return { kind: 'screenshot' }
    const i = digitIndex(e.code)
    return i === null ? null : { kind: 'interval', interval: INTERVALS[i] }
  }

  if (e.key === '?') return { kind: 'help' }

  return null
}

/**
 * Whether a held key (e.repeat) may fire `s` again. Stepping the timeframe with a held '[' / ']'
 * walks the list (and Alt+digit is idempotent); a held Alt+S, Alt+Shift+L or '?' would save a
 * stream of screenshots, flicker the theme, or reopen help, so those fire once per press.
 */
export function shortcutRepeats(s: AppShortcut): boolean {
  return s.kind === 'interval'
}

/** The parts of a keydown the chart's Escape/Delete handler reads. */
export interface ChartKeyEvent {
  key: string
  altKey: boolean
  ctrlKey: boolean
  metaKey: boolean
  defaultPrevented: boolean
}

/**
 * The chart's own keys: Escape (cancel the tool / deselect) and Delete/Backspace (remove the
 * selected drawing), or null. `blocked` is true while any dialog, menu or the Research view is
 * open: a key typed there (or after focus fell to <body> under a modal) must never act on a
 * drawing behind it. The caller still checks the key's target (chartTools).
 */
export function chartKey(e: ChartKeyEvent, blocked: boolean): 'Escape' | 'Delete' | null {
  if (blocked || e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return null
  if (e.key === 'Escape') return 'Escape'
  return e.key === 'Delete' || e.key === 'Backspace' ? 'Delete' : null
}

/** One row of the shortcuts dialog. `keys` lists alternatives; each is a key sequence. */
export interface ShortcutItem {
  keys: string[][]
  label: string
}

export interface ShortcutGroup {
  title: string
  items: ShortcutItem[]
}

const INTERVAL_NAMES: Readonly<Record<Interval, string>> = {
  '1m': '1 minute',
  '5m': '5 minutes',
  '15m': '15 minutes',
  '1h': '1 hour',
  '4h': '4 hours',
  '1d': '1 day',
  '1w': '1 week',
}

interface Glyphs {
  alt: string
  shift: string
  mod: string
  backspace: string
  del: string
}

const MAC: Glyphs = { alt: '⌥', shift: '⇧', mod: '⌘', backspace: '⌫', del: '⌦' }
const OTHER: Glyphs = {
  alt: 'Alt',
  shift: 'Shift',
  mod: 'Ctrl',
  backspace: 'Backspace',
  del: 'Delete',
}

/** "Alt+H" (DRAWING_TOOLS display form) → the letter after the modifier. */
function altLetter(shortcut: string): string | null {
  const m = /^Alt\+(.)$/.exec(shortcut)
  return m ? m[1] : null
}

/**
 * Every shortcut in the app, grouped for the help dialog, with macOS (⌥ ⇧ ⌘) or other-platform
 * (Alt Shift Ctrl) key labels.
 */
export function shortcutGroups(isMac: boolean): ShortcutGroup[] {
  const g = isMac ? MAC : OTHER

  const timeframe: ShortcutGroup = {
    title: 'Timeframe',
    items: [
      ...INTERVALS.map((interval, i) => ({
        keys: [[g.alt, String(i + 1)]],
        label: INTERVAL_NAMES[interval],
      })),
      { keys: [['[']], label: 'Previous timeframe' },
      { keys: [[']']], label: 'Next timeframe' },
    ],
  }

  const tools = DRAWING_TOOLS.flatMap((t) => {
    const letter = altLetter(t.shortcut)
    return letter ? [{ keys: [[g.alt, letter]], label: t.label }] : []
  })

  const drawing: ShortcutGroup = {
    title: 'Drawing',
    items: [
      ...tools,
      { keys: [['Esc']], label: 'Cancel drawing or deselect' },
      { keys: [[g.backspace], [g.del]], label: 'Remove selected drawing' },
    ],
  }

  const chart: ShortcutGroup = {
    title: 'Chart',
    items: [
      { keys: [[g.mod, 'K']], label: 'Search symbols' },
      { keys: [['A–Z'], ['0–9']], label: 'Type to search symbols' },
      { keys: [['/']], label: 'Add indicator' },
      { keys: [[g.alt, 'A']], label: 'Create alert at last price' },
      { keys: [[g.alt, 'S']], label: 'Save chart as PNG' },
    ],
  }

  const general: ShortcutGroup = {
    title: 'General',
    items: [
      { keys: [[g.alt, g.shift, 'L']], label: 'Toggle light / dark theme' },
      { keys: [['?']], label: 'Show keyboard shortcuts' },
      { keys: [['Esc']], label: 'Close dialog or menu' },
    ],
  }

  return [timeframe, chart, drawing, general]
}

/** Whether key labels should use macOS glyphs. Defaults to the global navigator. */
export function isMacPlatform(nav?: { platform?: string; userAgent?: string }): boolean {
  const n = nav ?? (typeof navigator === 'undefined' ? undefined : navigator)
  if (!n) return false
  const platform = n.platform ?? ''
  if (platform !== '') return /^(Mac|iPhone|iPad|iPod)/i.test(platform)
  return /Macintosh|Mac OS X|iPhone|iPad|iPod/i.test(n.userAgent ?? '')
}
