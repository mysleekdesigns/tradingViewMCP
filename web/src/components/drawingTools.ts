// Pure data and helpers for the drawing rail and the drawing style bar.

import type { DrawingKind, LineStyleName, ToolMode } from '../chart/drawings/types'

export interface ToolButtonDef {
  tool: ToolMode
  label: string
  /** Display form, e.g. "Alt+H"; also the `aria-keyshortcuts` value. */
  shortcut: string
}

/** Left-rail order (TradingView style): cursor first, then the drawing tools. */
export const DRAWING_TOOLS: readonly ToolButtonDef[] = [
  { tool: 'cursor', label: 'Cursor', shortcut: 'Escape' },
  { tool: 'hline', label: 'Horizontal line', shortcut: 'Alt+H' },
  { tool: 'hray', label: 'Horizontal ray', shortcut: 'Alt+J' },
  { tool: 'trendline', label: 'Trend line', shortcut: 'Alt+T' },
  { tool: 'fib', label: 'Fib retracement', shortcut: 'Alt+F' },
  { tool: 'rectangle', label: 'Rectangle', shortcut: 'Alt+R' },
]

export const DRAWING_KIND_LABELS: Readonly<Record<DrawingKind, string>> = {
  hline: 'Horizontal line',
  hray: 'Horizontal ray',
  trendline: 'Trend line',
  fib: 'Fib retracement',
  rectangle: 'Rectangle',
}

/** Short label for a shortcut in a tooltip: "Escape" reads "Esc". */
export function shortcutHint(shortcut: string): string {
  return shortcut === 'Escape' ? 'Esc' : shortcut
}

/** Tooltip text, e.g. "Trend line (Alt+T)". */
export function toolTitle(def: ToolButtonDef): string {
  return `${def.label} (${shortcutHint(def.shortcut)})`
}

/** Clicking the active drawing tool again returns to the cursor. */
export function nextTool(current: ToolMode, clicked: ToolMode): ToolMode {
  return clicked === current ? 'cursor' : clicked
}

/** "Remove 3 drawings on BTCUSDT?" */
export function clearAllPrompt(count: number, symbol: string): string {
  return `Remove ${count} ${count === 1 ? 'drawing' : 'drawings'} on ${symbol}?`
}

/** Keyboard event fields the shortcut matcher reads (a DOM KeyboardEvent qualifies). */
export interface ShortcutKey {
  key: string
  /** Physical key, e.g. "KeyH". Used for Alt shortcuts because macOS Option changes `key`. */
  code: string
  altKey: boolean
  ctrlKey: boolean
  metaKey: boolean
  shiftKey: boolean
}

const ALT_CODES: Readonly<Record<string, ToolMode | 'add-alert'>> = {
  KeyH: 'hline',
  KeyJ: 'hray',
  KeyT: 'trendline',
  KeyF: 'fib',
  KeyR: 'rectangle',
  KeyA: 'add-alert',
}

/**
 * Maps a key press to a drawing tool, `'add-alert'` (Alt+A), or null. Escape (no modifiers)
 * maps to the cursor. The caller should skip presses while focus is in a text field.
 */
export function shortcutCommand(e: ShortcutKey): ToolMode | 'add-alert' | null {
  if (e.ctrlKey || e.metaKey) return null
  if (e.key === 'Escape' && !e.altKey && !e.shiftKey) return 'cursor'
  if (!e.altKey || e.shiftKey) return null
  return ALT_CODES[e.code] ?? null
}

/** TradingView-like line palette; the first entry matches DEFAULT_DRAWING_STYLE. */
export const DRAWING_PALETTE: readonly { color: string; name: string }[] = [
  { color: '#2962ff', name: 'Blue' },
  { color: '#00bcd4', name: 'Cyan' },
  { color: '#089981', name: 'Green' },
  { color: '#4caf50', name: 'Light green' },
  { color: '#ffeb3b', name: 'Yellow' },
  { color: '#ff9800', name: 'Orange' },
  { color: '#f23645', name: 'Red' },
  { color: '#e91e63', name: 'Pink' },
  { color: '#9c27b0', name: 'Purple' },
  { color: '#d1d4dc', name: 'Light grey' },
]

export const LINE_WIDTHS: readonly (1 | 2 | 3 | 4)[] = [1, 2, 3, 4]

export const LINE_STYLES: readonly { style: LineStyleName; label: string; dash: string }[] = [
  { style: 'solid', label: 'Solid', dash: '' },
  { style: 'dashed', label: 'Dashed', dash: '6 4' },
  { style: 'dotted', label: 'Dotted', dash: '1.5 3' },
]

/** Palette name for a colour, or its upper-case hex when it is a custom colour. */
export function colorName(color: string): string {
  const c = color.toLowerCase()
  return DRAWING_PALETTE.find((p) => p.color === c)?.name ?? color.toUpperCase()
}

/** Index after an arrow-key move in a grid of `count` items laid out in `columns` columns. */
export function gridMove(
  index: number,
  key: string,
  count: number,
  columns: number,
): number | null {
  if (count <= 0) return null
  const cols = Math.max(1, columns)
  switch (key) {
    case 'ArrowRight':
      return (index + 1) % count
    case 'ArrowLeft':
      return (index - 1 + count) % count
    case 'ArrowDown':
      return cols === 1 ? (index + 1) % count : Math.min(index + cols, count - 1)
    case 'ArrowUp':
      return cols === 1 ? (index - 1 + count) % count : Math.max(index - cols, 0)
    case 'Home':
      return 0
    case 'End':
      return count - 1
    default:
      return null
  }
}
