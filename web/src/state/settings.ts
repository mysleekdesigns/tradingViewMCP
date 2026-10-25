// User settings (Settings dialog): theme, default data provider, candle colours, time zone.
// Persisted to localStorage like prefs.ts; every access is wrapped so a disabled or full storage
// never breaks the app.

import { isValidTimezone } from '../chart/timeFormat'
import { isProviderId, type ProviderId } from '../data/providerIds'
import type { PrefsStorage } from './prefs'

export type ThemeName = 'dark' | 'light'

export const THEMES: readonly ThemeName[] = ['dark', 'light']

export interface Settings {
  theme: ThemeName
  provider: ProviderId
  /** '#rrggbb' */
  upColor: string
  /** '#rrggbb' */
  downColor: string
  /** 'UTC' | 'local' | an IANA zone accepted by Intl.DateTimeFormat */
  timezone: string
}

// index.html's inline theme script repeats this key (and THEME_CHROME_COLORS below) to paint
// the saved theme before the bundle loads. Change them there too.
export const SETTINGS_KEY = 'tv.settings.v1'

/** TradingView's default candle colours (match --up / --down in index.css). */
export const DEFAULT_UP_COLOR = '#089981'
export const DEFAULT_DOWN_COLOR = '#f23645'

export function defaultSettings(): Settings {
  return {
    theme: 'dark',
    provider: import.meta.env.VITE_DATA_SOURCE === 'direct' ? 'binance' : 'server',
    upColor: DEFAULT_UP_COLOR,
    downColor: DEFAULT_DOWN_COLOR,
    timezone: 'UTC',
  }
}

const HEX_COLOR_RE = /^#[0-9a-f]{6}$/i

export function isHexColor(v: unknown): v is string {
  return typeof v === 'string' && HEX_COLOR_RE.test(v)
}

export function isThemeName(v: unknown): v is ThemeName {
  return v === 'dark' || v === 'light'
}

function isTimezoneSetting(v: unknown): v is string {
  return typeof v === 'string' && (v === 'local' || (v.length <= 64 && isValidTimezone(v)))
}

/**
 * Validates an untrusted value field by field. Each bad or missing field falls back to its
 * default on its own, so one stale field does not wipe the others. Colours are lower-cased.
 */
export function parseSettings(raw: unknown): Settings {
  const d = defaultSettings()
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return d
  const r = raw as Record<string, unknown>
  return {
    theme: isThemeName(r.theme) ? r.theme : d.theme,
    provider: isProviderId(r.provider) ? r.provider : d.provider,
    upColor: isHexColor(r.upColor) ? r.upColor.toLowerCase() : d.upColor,
    downColor: isHexColor(r.downColor) ? r.downColor.toLowerCase() : d.downColor,
    timezone: isTimezoneSetting(r.timezone) ? r.timezone : d.timezone,
  }
}

function defaultStorage(): PrefsStorage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    // Accessing localStorage itself throws when storage is disabled.
    return null
  }
}

export function loadSettings(storage: PrefsStorage | null = defaultStorage()): Settings {
  if (!storage) return defaultSettings()
  try {
    const text = storage.getItem(SETTINGS_KEY)
    if (text === null) return defaultSettings()
    return parseSettings(JSON.parse(text))
  } catch {
    return defaultSettings()
  }
}

/** Best effort: a full or disabled storage must never break the app. */
export function saveSettings(s: Settings, storage: PrefsStorage | null = defaultStorage()): void {
  if (!storage) return
  try {
    storage.setItem(SETTINGS_KEY, JSON.stringify(parseSettings(s)))
  } catch {
    // Quota exceeded or storage disabled: keep running without persistence.
  }
}

/**
 * The `--chrome` token of each theme (index.css): the browser UI colour in
 * `<meta name="theme-color">`. index.html's inline script repeats these values.
 */
export const THEME_CHROME_COLORS: Readonly<Record<ThemeName, string>> = {
  dark: '#0f121a',
  light: '#f0f3fa',
}

/**
 * Switch the CSS token block (`:root[data-theme='light']` in index.css), native controls, and
 * the browser UI colour (`<meta name="theme-color">`).
 */
export function applyThemeToDocument(
  theme: ThemeName,
  doc: Document | undefined = globalDoc(),
): void {
  if (!doc) return
  const root = doc.documentElement
  root.dataset.theme = theme
  root.style.colorScheme = theme
  const meta =
    typeof doc.querySelector === 'function'
      ? doc.querySelector<HTMLMetaElement>('meta[name="theme-color"]')
      : null
  meta?.setAttribute('content', THEME_CHROME_COLORS[theme])
}

/**
 * Optional extra for the integrator: the user's candle colours as the --up / --down CSS tokens,
 * so the legend, watchlist changes and up/down tints follow Settings. Default colours remove
 * the inline override so the theme blocks in index.css apply.
 */
export function applyCandleColorsToDocument(
  upColor: string,
  downColor: string,
  doc: Document | undefined = globalDoc(),
): void {
  if (!doc) return
  const style = doc.documentElement.style
  if (isHexColor(upColor) && upColor.toLowerCase() !== DEFAULT_UP_COLOR)
    style.setProperty('--up', upColor)
  else style.removeProperty('--up')
  if (isHexColor(downColor) && downColor.toLowerCase() !== DEFAULT_DOWN_COLOR)
    style.setProperty('--down', downColor)
  else style.removeProperty('--down')
}

function globalDoc(): Document | undefined {
  return typeof document === 'undefined' ? undefined : document
}

/** Time zones offered in Settings: UTC, the browser's zone, and common market/trader zones. */
export const TIMEZONE_CHOICES: readonly { value: string; label: string }[] = [
  { value: 'UTC', label: 'UTC' },
  { value: 'local', label: 'Local (browser)' },
  { value: 'America/Los_Angeles', label: 'Los Angeles' },
  { value: 'America/Denver', label: 'Denver' },
  { value: 'America/Chicago', label: 'Chicago' },
  { value: 'America/New_York', label: 'New York' },
  { value: 'America/Toronto', label: 'Toronto' },
  { value: 'America/Sao_Paulo', label: 'São Paulo' },
  { value: 'Europe/London', label: 'London' },
  { value: 'Europe/Berlin', label: 'Berlin' },
  { value: 'Europe/Paris', label: 'Paris' },
  { value: 'Europe/Zurich', label: 'Zurich' },
  { value: 'Europe/Istanbul', label: 'Istanbul' },
  { value: 'Europe/Moscow', label: 'Moscow' },
  { value: 'Asia/Dubai', label: 'Dubai' },
  { value: 'Asia/Kolkata', label: 'Kolkata' },
  { value: 'Asia/Singapore', label: 'Singapore' },
  { value: 'Asia/Hong_Kong', label: 'Hong Kong' },
  { value: 'Asia/Shanghai', label: 'Shanghai' },
  { value: 'Asia/Seoul', label: 'Seoul' },
  { value: 'Asia/Tokyo', label: 'Tokyo' },
  { value: 'Australia/Sydney', label: 'Sydney' },
  { value: 'Pacific/Auckland', label: 'Auckland' },
]
