import { describe, expect, it } from 'vitest'
import { shortcutCommand, type ShortcutKey } from '../components/drawingTools'
import { INTERVALS } from '../types'
import {
  appShortcut,
  chartKey,
  isMacPlatform,
  shortcutGroups,
  shortcutRepeats,
  type ChartKeyEvent,
} from './shortcuts'

const key = (over: Partial<ShortcutKey>): ShortcutKey => ({
  key: '',
  code: '',
  altKey: false,
  ctrlKey: false,
  metaKey: false,
  shiftKey: false,
  ...over,
})

describe('appShortcut: Alt+digit picks an interval', () => {
  it('maps Alt+1 … Alt+7 (Digit codes) to INTERVALS in order, whatever e.key is', () => {
    // macOS Option turns "1" into "¡" etc.; the physical code still identifies the key.
    const macKeys = ['¡', '™', '£', '¢', '∞', '§', '¶']
    INTERVALS.forEach((interval, i) => {
      expect(
        appShortcut(key({ key: macKeys[i], code: `Digit${i + 1}`, altKey: true }), '1h'),
      ).toEqual({ kind: 'interval', interval })
    })
  })

  it('accepts the Numpad variants', () => {
    INTERVALS.forEach((interval, i) => {
      expect(
        appShortcut(key({ key: String(i + 1), code: `Numpad${i + 1}`, altKey: true }), '1m'),
      ).toEqual({ kind: 'interval', interval })
    })
  })

  it('ignores digits beyond the interval list, Digit0, and plain digits', () => {
    expect(appShortcut(key({ key: '8', code: 'Digit8', altKey: true }), '1h')).toBeNull()
    expect(appShortcut(key({ key: '9', code: 'Numpad9', altKey: true }), '1h')).toBeNull()
    expect(appShortcut(key({ key: '0', code: 'Digit0', altKey: true }), '1h')).toBeNull()
    // Plain digits open symbol search in App.tsx; they are not interval keys.
    expect(appShortcut(key({ key: '1', code: 'Digit1' }), '1h')).toBeNull()
    expect(
      appShortcut(key({ key: '!', code: 'Digit1', altKey: true, shiftKey: true }), '1h'),
    ).toBeNull()
  })
})

describe('appShortcut: [ and ] step the interval', () => {
  it('steps to the previous and next interval', () => {
    expect(appShortcut(key({ key: '[', code: 'BracketLeft' }), '1h')).toEqual({
      kind: 'interval',
      interval: '15m',
    })
    expect(appShortcut(key({ key: ']', code: 'BracketRight' }), '1h')).toEqual({
      kind: 'interval',
      interval: '4h',
    })
  })

  it('returns null at the ends of the range', () => {
    expect(appShortcut(key({ key: '[', code: 'BracketLeft' }), '1m')).toBeNull()
    expect(appShortcut(key({ key: ']', code: 'BracketRight' }), '1w')).toBeNull()
    expect(appShortcut(key({ key: ']', code: 'BracketRight' }), '1d')).toEqual({
      kind: 'interval',
      interval: '1w',
    })
    expect(appShortcut(key({ key: '[', code: 'BracketLeft' }), '5m')).toEqual({
      kind: 'interval',
      interval: '1m',
    })
  })

  it('walks the whole list one step at a time', () => {
    let cur = INTERVALS[0]
    const seen = [cur]
    for (;;) {
      const s = appShortcut(key({ key: ']', code: 'BracketRight' }), cur)
      if (s === null) break
      expect(s.kind).toBe('interval')
      if (s.kind === 'interval') cur = s.interval
      seen.push(cur)
    }
    expect(seen).toEqual([...INTERVALS])
  })

  it('ignores Ctrl/Meta brackets and keys that do not type a bracket', () => {
    expect(appShortcut(key({ key: ']', code: 'BracketRight', ctrlKey: true }), '1h')).toBeNull()
    expect(appShortcut(key({ key: '[', code: 'BracketLeft', metaKey: true }), '1h')).toBeNull()
    // US layouts: Shift+[ types '{' and Option+[ types '“'; neither is a step.
    expect(appShortcut(key({ key: '{', code: 'BracketLeft', shiftKey: true }), '1h')).toBeNull()
    expect(appShortcut(key({ key: '“', code: 'BracketLeft', altKey: true }), '1h')).toBeNull()
  })

  it("steps on a '[' / ']' typed with Option (German-style mac layouts), never as Alt+digit", () => {
    // German macOS: Option+5 types '[' and Option+6 types ']' (e.code Digit5 / Digit6).
    expect(appShortcut(key({ key: '[', code: 'Digit5', altKey: true }), '1h')).toEqual({
      kind: 'interval',
      interval: '15m',
    })
    expect(appShortcut(key({ key: ']', code: 'Digit6', altKey: true }), '1h')).toEqual({
      kind: 'interval',
      interval: '4h',
    })
    // At the ends it is still not Alt+5 / Alt+6 ('4h' / '1d').
    expect(appShortcut(key({ key: '[', code: 'Digit5', altKey: true }), '1m')).toBeNull()
    expect(appShortcut(key({ key: ']', code: 'Digit6', altKey: true }), '1w')).toBeNull()
    // Layouts that need Option+Shift for a bracket (e.g. French macOS).
    expect(
      appShortcut(key({ key: '[', code: 'Digit5', altKey: true, shiftKey: true }), '1h'),
    ).toEqual({ kind: 'interval', interval: '15m' })
    // A plain Option+5 that types something else is still Alt+5.
    expect(appShortcut(key({ key: '∞', code: 'Digit5', altKey: true }), '1h')).toEqual({
      kind: 'interval',
      interval: INTERVALS[4],
    })
  })
})

describe('appShortcut: screenshot, theme, help', () => {
  it('maps Alt+S (by code) to screenshot', () => {
    expect(appShortcut(key({ key: 'ß', code: 'KeyS', altKey: true }), '1h')).toEqual({
      kind: 'screenshot',
    })
    expect(appShortcut(key({ key: 's', code: 'KeyS' }), '1h')).toBeNull()
    expect(
      appShortcut(key({ key: 'S', code: 'KeyS', altKey: true, shiftKey: true }), '1h'),
    ).toBeNull()
  })

  it('maps Alt+Shift+L to the theme toggle and leaves Alt+L free', () => {
    expect(
      appShortcut(key({ key: 'Ò', code: 'KeyL', altKey: true, shiftKey: true }), '1h'),
    ).toEqual({ kind: 'theme' })
    expect(appShortcut(key({ key: '¬', code: 'KeyL', altKey: true }), '1h')).toBeNull()
    expect(appShortcut(key({ key: 'L', code: 'KeyL', shiftKey: true }), '1h')).toBeNull()
  })

  it("maps '?' (Shift+/) to help, but not with Ctrl/Meta/Alt", () => {
    expect(appShortcut(key({ key: '?', code: 'Slash', shiftKey: true }), '1h')).toEqual({
      kind: 'help',
    })
    expect(
      appShortcut(key({ key: '?', code: 'Slash', shiftKey: true, ctrlKey: true }), '1h'),
    ).toBeNull()
    expect(
      appShortcut(key({ key: '?', code: 'Slash', shiftKey: true, metaKey: true }), '1h'),
    ).toBeNull()
    expect(
      appShortcut(key({ key: '¿', code: 'Slash', shiftKey: true, altKey: true }), '1h'),
    ).toBeNull()
    // Plain "/" is the indicators shortcut in App.tsx.
    expect(appShortcut(key({ key: '/', code: 'Slash' }), '1h')).toBeNull()
  })
})

describe('appShortcut: modifier rejection', () => {
  it('returns null for any Ctrl/Meta combination', () => {
    expect(appShortcut(key({ key: '1', code: 'Digit1', ctrlKey: true }), '1h')).toBeNull()
    expect(appShortcut(key({ key: '1', code: 'Digit1', metaKey: true }), '1h')).toBeNull()
    expect(
      appShortcut(key({ key: '1', code: 'Digit1', altKey: true, ctrlKey: true }), '1h'),
    ).toBeNull()
    expect(appShortcut(key({ key: '[', code: 'BracketLeft', metaKey: true }), '1h')).toBeNull()
    expect(
      appShortcut(key({ key: 's', code: 'KeyS', altKey: true, ctrlKey: true }), '1h'),
    ).toBeNull()
    expect(
      appShortcut(
        key({ key: 'l', code: 'KeyL', altKey: true, shiftKey: true, metaKey: true }),
        '1h',
      ),
    ).toBeNull()
    // Cmd/Ctrl+K stays with symbol search.
    expect(appShortcut(key({ key: 'k', code: 'KeyK', metaKey: true }), '1h')).toBeNull()
    expect(appShortcut(key({ key: 'k', code: 'KeyK', ctrlKey: true }), '1h')).toBeNull()
  })

  it('ignores Escape, Delete, Backspace and plain letters (owned by the chart / search)', () => {
    for (const k of ['Escape', 'Delete', 'Backspace', 'a', 'k']) {
      expect(appShortcut(key({ key: k, code: k }), '1h')).toBeNull()
    }
  })
})

describe('no collision with drawing/alert shortcuts', () => {
  const ALT_CODES = ['KeyH', 'KeyJ', 'KeyT', 'KeyF', 'KeyR', 'KeyA']

  it('appShortcut returns null for every drawingTools Alt code (with or without Shift)', () => {
    for (const code of ALT_CODES) {
      // Sanity: drawingTools does own this code.
      expect(
        shortcutCommand(key({ key: code.slice(3).toLowerCase(), code, altKey: true })),
      ).not.toBeNull()
      expect(
        appShortcut(key({ key: code.slice(3).toLowerCase(), code, altKey: true }), '1h'),
      ).toBeNull()
      expect(
        appShortcut(key({ key: code.slice(3), code, altKey: true, shiftKey: true }), '1h'),
      ).toBeNull()
    }
  })

  it('shortcutCommand returns null for the new app codes', () => {
    expect(shortcutCommand(key({ key: 'ß', code: 'KeyS', altKey: true }))).toBeNull()
    expect(
      shortcutCommand(key({ key: 'Ò', code: 'KeyL', altKey: true, shiftKey: true })),
    ).toBeNull()
    for (let i = 1; i <= INTERVALS.length; i++) {
      expect(shortcutCommand(key({ key: String(i), code: `Digit${i}`, altKey: true }))).toBeNull()
      expect(shortcutCommand(key({ key: String(i), code: `Numpad${i}`, altKey: true }))).toBeNull()
    }
    expect(shortcutCommand(key({ key: '[', code: 'BracketLeft' }))).toBeNull()
    expect(shortcutCommand(key({ key: ']', code: 'BracketRight' }))).toBeNull()
    expect(shortcutCommand(key({ key: '?', code: 'Slash', shiftKey: true }))).toBeNull()
  })
})

describe('shortcutGroups', () => {
  const flat = (isMac: boolean) => shortcutGroups(isMac).flatMap((g) => g.items)
  const seqs = (isMac: boolean) => flat(isMac).flatMap((i) => i.keys.map((k) => k.join(' ')))

  it('lists every interval shortcut, plus [ and ]', () => {
    for (const isMac of [true, false]) {
      const alt = isMac ? '⌥' : 'Alt'
      const s = seqs(isMac)
      INTERVALS.forEach((_, i) => expect(s).toContain(`${alt} ${i + 1}`))
      expect(s).toContain('[')
      expect(s).toContain(']')
    }
    const timeframe = shortcutGroups(false).find((g) => g.title === 'Timeframe')
    expect(timeframe?.items.map((i) => i.label).slice(0, INTERVALS.length)).toEqual([
      '1 minute',
      '5 minutes',
      '15 minutes',
      '1 hour',
      '4 hours',
      '1 day',
      '1 week',
    ])
  })

  it('lists the existing and new app shortcuts', () => {
    const s = seqs(false)
    for (const expected of [
      'Ctrl K',
      'A–Z',
      '/',
      'Alt H',
      'Alt J',
      'Alt T',
      'Alt F',
      'Alt R',
      'Alt A',
      'Esc',
      'Delete',
      'Backspace',
      'Alt S',
      'Alt Shift L',
      '?',
    ]) {
      expect(s).toContain(expected)
    }
    const m = seqs(true)
    for (const expected of ['⌘ K', '⌥ H', '⌥ A', '⌥ S', '⌥ ⇧ L', '⌫', '⌦', 'Esc', '?', '/']) {
      expect(m).toContain(expected)
    }
  })

  it('differs by platform only in key labels', () => {
    const mac = shortcutGroups(true)
    const other = shortcutGroups(false)
    expect(mac).not.toEqual(other)
    expect(mac.map((g) => g.title)).toEqual(other.map((g) => g.title))
    expect(flat(true).map((i) => i.label)).toEqual(flat(false).map((i) => i.label))
    expect(seqs(true).some((k) => /Ctrl|Alt|Shift/.test(k))).toBe(false)
    expect(seqs(false).some((k) => /[⌘⌥⇧]/.test(k))).toBe(false)
  })

  it('has non-empty groups, labels and key sequences', () => {
    for (const g of shortcutGroups(true)) {
      expect(g.title).not.toBe('')
      expect(g.items.length).toBeGreaterThan(0)
      for (const item of g.items) {
        expect(item.label).not.toBe('')
        expect(item.keys.length).toBeGreaterThan(0)
        for (const seq of item.keys) expect(seq.length).toBeGreaterThan(0)
      }
    }
  })
})

describe('isMacPlatform', () => {
  it('detects macOS and iOS from navigator.platform', () => {
    expect(isMacPlatform({ platform: 'MacIntel' })).toBe(true)
    expect(isMacPlatform({ platform: 'iPhone' })).toBe(true)
    expect(isMacPlatform({ platform: 'iPad' })).toBe(true)
  })

  it('rejects Windows and Linux', () => {
    expect(isMacPlatform({ platform: 'Win32' })).toBe(false)
    expect(isMacPlatform({ platform: 'Linux x86_64' })).toBe(false)
    expect(
      isMacPlatform({
        platform: 'Win32',
        userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
      }),
    ).toBe(false)
  })

  it('falls back to the user agent when platform is empty or missing', () => {
    expect(
      isMacPlatform({
        platform: '',
        userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15',
      }),
    ).toBe(true)
    expect(isMacPlatform({ userAgent: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36' })).toBe(
      false,
    )
    expect(isMacPlatform({})).toBe(false)
  })
})

describe('shortcutRepeats: held keys', () => {
  it("lets a held '[' / ']' keep stepping but fires screenshot, theme and help once", () => {
    const step = appShortcut(key({ key: ']', code: 'BracketRight' }), '1h')
    expect(step).not.toBeNull()
    if (step) expect(shortcutRepeats(step)).toBe(true)
    expect(shortcutRepeats({ kind: 'interval', interval: '4h' })).toBe(true)
    expect(shortcutRepeats({ kind: 'screenshot' })).toBe(false)
    expect(shortcutRepeats({ kind: 'theme' })).toBe(false)
    expect(shortcutRepeats({ kind: 'help' })).toBe(false)
  })
})

describe('chartKey: Escape / Delete reach the chart only when nothing modal is open', () => {
  const ev = (over: Partial<ChartKeyEvent>): ChartKeyEvent => ({
    key: '',
    altKey: false,
    ctrlKey: false,
    metaKey: false,
    defaultPrevented: false,
    ...over,
  })

  it('maps Escape, Delete and Backspace', () => {
    expect(chartKey(ev({ key: 'Escape' }), false)).toBe('Escape')
    expect(chartKey(ev({ key: 'Delete' }), false)).toBe('Delete')
    expect(chartKey(ev({ key: 'Backspace' }), false)).toBe('Delete')
    expect(chartKey(ev({ key: 'a' }), false)).toBeNull()
  })

  it('ignores every chart key while a dialog has the keyboard (focus fell to <body>)', () => {
    expect(chartKey(ev({ key: 'Backspace' }), true)).toBeNull()
    expect(chartKey(ev({ key: 'Delete' }), true)).toBeNull()
    expect(chartKey(ev({ key: 'Escape' }), true)).toBeNull()
  })

  it('ignores handled keys and modifier chords', () => {
    expect(chartKey(ev({ key: 'Delete', defaultPrevented: true }), false)).toBeNull()
    expect(chartKey(ev({ key: 'Backspace', metaKey: true }), false)).toBeNull()
    expect(chartKey(ev({ key: 'Backspace', ctrlKey: true }), false)).toBeNull()
    expect(chartKey(ev({ key: 'Escape', altKey: true }), false)).toBeNull()
  })
})
