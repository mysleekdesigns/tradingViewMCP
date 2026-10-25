import { describe, expect, it } from 'vitest'
import indexHtml from '../../index.html?raw'
import type { PrefsStorage } from './prefs'
import {
  SETTINGS_KEY,
  THEME_CHROME_COLORS,
  TIMEZONE_CHOICES,
  applyCandleColorsToDocument,
  applyThemeToDocument,
  defaultSettings,
  loadSettings,
  parseSettings,
  saveSettings,
  type Settings,
} from './settings'
import { isValidTimezone } from '../chart/timeFormat'

function memoryStorage(initial: Record<string, string> = {}): PrefsStorage & {
  data: Record<string, string>
} {
  const data = { ...initial }
  return {
    data,
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => {
      data[k] = v
    },
  }
}

const throwing: PrefsStorage = {
  getItem: () => {
    throw new Error('SecurityError')
  },
  setItem: () => {
    throw new Error('QuotaExceededError')
  },
}

const custom: Settings = {
  theme: 'light',
  provider: 'coinbase',
  upColor: '#2962ff',
  downColor: '#ff9800',
  timezone: 'America/New_York',
}

describe('defaultSettings', () => {
  it('is dark, UTC, TradingView candle colours, on the local server (no VITE_DATA_SOURCE)', () => {
    expect(defaultSettings()).toEqual({
      theme: 'dark',
      provider: 'server',
      upColor: '#089981',
      downColor: '#f23645',
      timezone: 'UTC',
    })
  })

  it('returns a fresh object each call', () => {
    const a = defaultSettings()
    a.theme = 'light'
    expect(defaultSettings().theme).toBe('dark')
  })
})

describe('parseSettings', () => {
  it('keeps a fully valid value', () => {
    expect(parseSettings(custom)).toEqual(custom)
  })

  it('falls back to defaults for non-objects', () => {
    for (const raw of [null, undefined, 42, 'light', [custom]]) {
      expect(parseSettings(raw)).toEqual(defaultSettings())
    }
  })

  it('replaces each bad field on its own, keeping the good ones', () => {
    const d = defaultSettings()
    expect(parseSettings({ ...custom, theme: 'sepia' })).toEqual({ ...custom, theme: d.theme })
    expect(parseSettings({ ...custom, provider: 'kraken' })).toEqual({
      ...custom,
      provider: d.provider,
    })
    expect(parseSettings({ ...custom, upColor: 'green' })).toEqual({
      ...custom,
      upColor: d.upColor,
    })
    expect(parseSettings({ ...custom, downColor: '#ff00' })).toEqual({
      ...custom,
      downColor: d.downColor,
    })
    expect(parseSettings({ ...custom, timezone: 42 })).toEqual({ ...custom, timezone: 'UTC' })
  })

  it("accepts 'UTC', 'local' and IANA zones; an invalid zone becomes 'UTC'", () => {
    expect(parseSettings({ timezone: 'local' }).timezone).toBe('local')
    expect(parseSettings({ timezone: 'UTC' }).timezone).toBe('UTC')
    expect(parseSettings({ timezone: 'Asia/Tokyo' }).timezone).toBe('Asia/Tokyo')
    expect(parseSettings({ timezone: 'Mars/Base' }).timezone).toBe('UTC')
    expect(parseSettings({ timezone: '' }).timezone).toBe('UTC')
  })

  it('rejects non-#rrggbb colours (named, rgb(), short hex, alpha) and lower-cases hex', () => {
    const d = defaultSettings()
    for (const bad of ['red', 'rgb(1, 2, 3)', '#abc', '#08998180', '089981', 7]) {
      expect(parseSettings({ upColor: bad }).upColor).toBe(d.upColor)
    }
    expect(parseSettings({ upColor: '#ABCDEF' }).upColor).toBe('#abcdef')
  })
})

describe('loadSettings / saveSettings', () => {
  it('returns defaults when nothing is stored, or storage is missing', () => {
    expect(loadSettings(memoryStorage())).toEqual(defaultSettings())
    expect(loadSettings(null)).toEqual(defaultSettings())
  })

  it('round-trips through storage under SETTINGS_KEY', () => {
    const s = memoryStorage()
    saveSettings(custom, s)
    expect(Object.keys(s.data)).toEqual([SETTINGS_KEY])
    expect(SETTINGS_KEY).toBe('tv.settings.v1')
    expect(loadSettings(s)).toEqual(custom)
  })

  it('cleans values before saving', () => {
    const s = memoryStorage()
    saveSettings({ ...custom, upColor: '#ABCDEF', timezone: 'Nowhere/City' }, s)
    expect(JSON.parse(s.data[SETTINGS_KEY])).toEqual({
      ...custom,
      upColor: '#abcdef',
      timezone: 'UTC',
    })
  })

  it('survives corrupt JSON and partly stale data', () => {
    expect(loadSettings(memoryStorage({ [SETTINGS_KEY]: '{not json' }))).toEqual(defaultSettings())
    const stale = memoryStorage({
      [SETTINGS_KEY]: JSON.stringify({ theme: 'light', provider: 'ftx', extra: true }),
    })
    expect(loadSettings(stale)).toEqual({ ...defaultSettings(), theme: 'light' })
  })

  it('never throws when storage throws', () => {
    expect(loadSettings(throwing)).toEqual(defaultSettings())
    expect(() => saveSettings(custom, throwing)).not.toThrow()
  })
})

/** Just enough of a Document for the theme helpers. */
function fakeDoc() {
  const props = new Map<string, string>()
  const documentElement = {
    dataset: {} as Record<string, string>,
    style: {
      colorScheme: '',
      setProperty: (k: string, v: string) => void props.set(k, v),
      removeProperty: (k: string) => {
        props.delete(k)
        return ''
      },
    },
  }
  return { doc: { documentElement } as unknown as Document, documentElement, props }
}

describe('applyThemeToDocument', () => {
  it('sets data-theme and color-scheme on the root element', () => {
    const { doc, documentElement } = fakeDoc()
    applyThemeToDocument('light', doc)
    expect(documentElement.dataset.theme).toBe('light')
    expect(documentElement.style.colorScheme).toBe('light')
    applyThemeToDocument('dark', doc)
    expect(documentElement.dataset.theme).toBe('dark')
    expect(documentElement.style.colorScheme).toBe('dark')
  })

  it('is a no-op without a document (Node)', () => {
    expect(() => applyThemeToDocument('light', undefined)).not.toThrow()
  })

  it("sets <meta name=theme-color> to the theme's chrome colour", () => {
    const { documentElement } = fakeDoc()
    const attrs = new Map<string, string>([['content', '#0f121a']])
    const meta = { setAttribute: (k: string, v: string) => attrs.set(k, v) }
    const selectors: string[] = []
    const doc = {
      documentElement,
      querySelector: (sel: string) => {
        selectors.push(sel)
        return meta
      },
    } as unknown as Document
    applyThemeToDocument('light', doc)
    expect(attrs.get('content')).toBe(THEME_CHROME_COLORS.light)
    expect(THEME_CHROME_COLORS.light).toBe('#f0f3fa')
    applyThemeToDocument('dark', doc)
    expect(attrs.get('content')).toBe('#0f121a')
    expect(selectors[0]).toBe('meta[name="theme-color"]')
  })

  it('index.html repeats SETTINGS_KEY and the chrome colours', () => {
    const html = indexHtml
    expect(html).toContain(`'${SETTINGS_KEY}'`)
    expect(html).toContain(THEME_CHROME_COLORS.dark)
    expect(html).toContain(THEME_CHROME_COLORS.light)
  })
})

describe('applyCandleColorsToDocument', () => {
  it('overrides --up/--down for custom colours and clears them for defaults', () => {
    const { doc, props } = fakeDoc()
    applyCandleColorsToDocument('#2962ff', '#ff9800', doc)
    expect(props.get('--up')).toBe('#2962ff')
    expect(props.get('--down')).toBe('#ff9800')
    const d = defaultSettings()
    applyCandleColorsToDocument(d.upColor, d.downColor, doc)
    expect(props.has('--up')).toBe(false)
    expect(props.has('--down')).toBe(false)
  })
})

describe('TIMEZONE_CHOICES', () => {
  it('starts with UTC and Local, has unique values, and every zone is valid', () => {
    expect(TIMEZONE_CHOICES.slice(0, 2).map((c) => c.value)).toEqual(['UTC', 'local'])
    const values = TIMEZONE_CHOICES.map((c) => c.value)
    expect(new Set(values).size).toBe(values.length)
    for (const v of values.slice(2)) expect(isValidTimezone(v)).toBe(true)
    expect(values).toContain('America/New_York')
  })
})
