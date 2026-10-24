import { describe, expect, it } from 'vitest'
import { DEFAULT_DRAWING_STYLE, DRAWING_KINDS } from '../chart/drawings/types'
import {
  DRAWING_KIND_LABELS,
  DRAWING_PALETTE,
  DRAWING_TOOLS,
  clearAllPrompt,
  colorName,
  gridMove,
  nextTool,
  shortcutCommand,
  toolTitle,
  type ShortcutKey,
} from './drawingTools'

const key = (over: Partial<ShortcutKey>): ShortcutKey => ({
  key: '',
  code: '',
  altKey: false,
  ctrlKey: false,
  metaKey: false,
  shiftKey: false,
  ...over,
})

describe('DRAWING_TOOLS', () => {
  it('lists the cursor then every drawing kind exactly once', () => {
    expect(DRAWING_TOOLS[0].tool).toBe('cursor')
    expect(DRAWING_TOOLS.slice(1).map((t) => t.tool)).toEqual([...DRAWING_KINDS])
  })

  it('has a label for every drawing kind', () => {
    for (const k of DRAWING_KINDS) expect(DRAWING_KIND_LABELS[k]).toBeTruthy()
  })

  it('builds tooltip text with the shortcut hint', () => {
    expect(toolTitle(DRAWING_TOOLS[0])).toBe('Cursor (Esc)')
    expect(toolTitle(DRAWING_TOOLS[3])).toBe('Trend line (Alt+T)')
  })
})

describe('nextTool', () => {
  it('selects a different tool', () => {
    expect(nextTool('cursor', 'fib')).toBe('fib')
    expect(nextTool('hline', 'rectangle')).toBe('rectangle')
  })

  it('returns to the cursor when the active tool is clicked again', () => {
    expect(nextTool('trendline', 'trendline')).toBe('cursor')
    expect(nextTool('cursor', 'cursor')).toBe('cursor')
  })
})

describe('clearAllPrompt', () => {
  it('pluralises the count and names the symbol', () => {
    expect(clearAllPrompt(1, 'BTCUSDT')).toBe('Remove 1 drawing on BTCUSDT?')
    expect(clearAllPrompt(3, 'ETHUSDT')).toBe('Remove 3 drawings on ETHUSDT?')
  })
})

describe('shortcutCommand', () => {
  it('maps Alt + physical key to a tool, regardless of the macOS Option character', () => {
    expect(shortcutCommand(key({ key: '˙', code: 'KeyH', altKey: true }))).toBe('hline')
    expect(shortcutCommand(key({ key: 'j', code: 'KeyJ', altKey: true }))).toBe('hray')
    expect(shortcutCommand(key({ key: '†', code: 'KeyT', altKey: true }))).toBe('trendline')
    expect(shortcutCommand(key({ key: 'ƒ', code: 'KeyF', altKey: true }))).toBe('fib')
    expect(shortcutCommand(key({ key: '®', code: 'KeyR', altKey: true }))).toBe('rectangle')
    expect(shortcutCommand(key({ key: 'å', code: 'KeyA', altKey: true }))).toBe('add-alert')
  })

  it('maps plain Escape to the cursor', () => {
    expect(shortcutCommand(key({ key: 'Escape', code: 'Escape' }))).toBe('cursor')
  })

  it('ignores unmodified letters, other modifiers, and unknown keys', () => {
    expect(shortcutCommand(key({ key: 'h', code: 'KeyH' }))).toBeNull()
    expect(shortcutCommand(key({ key: 'h', code: 'KeyH', altKey: true, ctrlKey: true }))).toBeNull()
    expect(shortcutCommand(key({ key: 'h', code: 'KeyH', altKey: true, metaKey: true }))).toBeNull()
    expect(
      shortcutCommand(key({ key: 'H', code: 'KeyH', altKey: true, shiftKey: true })),
    ).toBeNull()
    expect(shortcutCommand(key({ key: 'q', code: 'KeyQ', altKey: true }))).toBeNull()
    expect(shortcutCommand(key({ key: 'Escape', code: 'Escape', metaKey: true }))).toBeNull()
  })
})

describe('palette', () => {
  it('starts with the default drawing colour and has unique lower-case hex entries', () => {
    expect(DRAWING_PALETTE[0].color).toBe(DEFAULT_DRAWING_STYLE.color)
    const colors = DRAWING_PALETTE.map((p) => p.color)
    expect(new Set(colors).size).toBe(colors.length)
    for (const c of colors) expect(c).toMatch(/^#[0-9a-f]{6}$/)
    expect(colors.length).toBeGreaterThanOrEqual(8)
    expect(colors.length).toBeLessThanOrEqual(10)
  })

  it('names palette colours and falls back to hex for custom ones', () => {
    expect(colorName('#2962FF')).toBe('Blue')
    expect(colorName('#123abc')).toBe('#123ABC')
  })
})

describe('gridMove', () => {
  it('moves horizontally with wrap-around', () => {
    expect(gridMove(0, 'ArrowRight', 10, 5)).toBe(1)
    expect(gridMove(9, 'ArrowRight', 10, 5)).toBe(0)
    expect(gridMove(0, 'ArrowLeft', 10, 5)).toBe(9)
  })

  it('moves by a row vertically and clamps at the edges', () => {
    expect(gridMove(2, 'ArrowDown', 10, 5)).toBe(7)
    expect(gridMove(7, 'ArrowDown', 10, 5)).toBe(9)
    expect(gridMove(7, 'ArrowUp', 10, 5)).toBe(2)
    expect(gridMove(2, 'ArrowUp', 10, 5)).toBe(0)
  })

  it('wraps vertically in a single-column list', () => {
    expect(gridMove(3, 'ArrowDown', 4, 1)).toBe(0)
    expect(gridMove(0, 'ArrowUp', 4, 1)).toBe(3)
  })

  it('handles Home/End, other keys, and empty lists', () => {
    expect(gridMove(4, 'Home', 10, 5)).toBe(0)
    expect(gridMove(4, 'End', 10, 5)).toBe(9)
    expect(gridMove(4, 'a', 10, 5)).toBeNull()
    expect(gridMove(0, 'ArrowDown', 0, 1)).toBeNull()
  })
})
