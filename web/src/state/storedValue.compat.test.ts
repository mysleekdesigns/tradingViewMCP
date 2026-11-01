// Phase 19 Wave 5, no-behaviour-change check: every load/save pair moved onto storedValue is
// compared with its hand-written predecessor (copied verbatim from 48b862c below) over the
// same stored texts and storages: same values read, same bytes written, same keys, nothing
// thrown.
import { describe, expect, it } from 'vitest'
import type { DrawingStyle } from '../chart/drawings/types'
import { parseDrawingStyle } from '../data/drawingsApi'
import {
  DOCK_DEFAULT_HEIGHT,
  DOCK_STORAGE_KEY,
  loadDockPrefs,
  loadPlanText,
  PLAN_STORAGE_KEY,
  saveDockPrefs,
  savePlanText,
  type DockPrefs,
} from '../components/backtestView'
import {
  loadResearchTab,
  RESEARCH_TAB_KEY,
  RESEARCH_TABS,
  resetResearchTab,
  saveResearchTab,
  type ResearchTab,
} from '../components/researchView'
import type { SidebarPanel } from '../components/SidebarRail'
import {
  DRAWING_STYLE_KEY,
  loadDrawingStyle,
  loadSidebarPanel,
  saveDrawingStyle,
  saveSidebarPanel,
  SIDEBAR_PANEL_KEY,
} from './chartTools'
import { DERIVATIVES_PREF_KEY, loadDerivativesOn, saveDerivativesOn } from './derivativesPref'
import { DEFAULT_PREFS, loadPrefs, parsePrefs, PREFS_KEY, savePrefs, type Prefs } from './prefs'
import { loadRegimeOn, REGIME_PREF_KEY, saveRegimeOn } from './regimePref'
import {
  defaultSettings,
  loadSettings,
  parseSettings,
  saveSettings,
  SETTINGS_KEY,
  type Settings,
} from './settings'
import {
  DEFAULT_WATCHLIST,
  loadWatchlist,
  parseWatchlist,
  saveWatchlist,
  serializeWatchlist,
  WATCHLIST_STORAGE_KEY,
} from './watchlist'

type S = Pick<Storage, 'getItem' | 'setItem'>

// ---- The predecessors, verbatim apart from the storage parameter having no default ----

const legacy = {
  loadPrefs(storage: S | null): Prefs {
    if (!storage) return { ...DEFAULT_PREFS }
    try {
      const text = storage.getItem(PREFS_KEY)
      if (text === null) return { ...DEFAULT_PREFS }
      return parsePrefs(JSON.parse(text))
    } catch {
      return { ...DEFAULT_PREFS }
    }
  },
  savePrefs(prefs: Prefs, storage: S | null): void {
    if (!storage) return
    try {
      const clean = parsePrefs(prefs)
      storage.setItem(PREFS_KEY, JSON.stringify(clean))
    } catch {
      // ignore
    }
  },
  loadBool(key: string, storage: S | null): boolean {
    if (!storage) return false
    try {
      const raw = storage.getItem(key)
      return raw !== null && (JSON.parse(raw) as unknown) === true
    } catch {
      return false
    }
  },
  saveBool(key: string, on: boolean, storage: S | null): void {
    if (!storage) return
    try {
      storage.setItem(key, JSON.stringify(on))
    } catch {
      // ignore
    }
  },
  loadSettings(storage: S | null): Settings {
    if (!storage) return defaultSettings()
    try {
      const text = storage.getItem(SETTINGS_KEY)
      if (text === null) return defaultSettings()
      return parseSettings(JSON.parse(text))
    } catch {
      return defaultSettings()
    }
  },
  saveSettings(s: Settings, storage: S | null): void {
    if (!storage) return
    try {
      storage.setItem(SETTINGS_KEY, JSON.stringify(parseSettings(s)))
    } catch {
      // ignore
    }
  },
  readJson(storage: S | null, key: string): unknown {
    if (!storage) return undefined
    try {
      const text = storage.getItem(key)
      return text === null ? undefined : (JSON.parse(text) as unknown)
    } catch {
      return undefined
    }
  },
  writeJson(storage: S | null, key: string, value: unknown): void {
    if (!storage) return
    try {
      storage.setItem(key, JSON.stringify(value))
    } catch {
      // ignore
    }
  },
  loadDrawingStyle(fallback: DrawingStyle, storage: S | null): DrawingStyle {
    return parseDrawingStyle(legacy.readJson(storage, DRAWING_STYLE_KEY)) ?? { ...fallback }
  },
  saveDrawingStyle(style: DrawingStyle, storage: S | null): void {
    const clean = parseDrawingStyle(style)
    if (clean !== null) legacy.writeJson(storage, DRAWING_STYLE_KEY, clean)
  },
  loadSidebarPanel(storage: S | null): SidebarPanel | null {
    const raw = legacy.readJson(storage, SIDEBAR_PANEL_KEY)
    if (raw === 'watchlist' || raw === 'alerts' || raw === 'paper') return raw
    if (raw === 'none') return null
    return 'watchlist'
  },
  saveSidebarPanel(panel: SidebarPanel | null, storage: S | null): void {
    legacy.writeJson(storage, SIDEBAR_PANEL_KEY, panel ?? 'none')
  },
  loadWatchlist(storage: S | null): readonly string[] {
    if (storage === null) return DEFAULT_WATCHLIST
    try {
      return parseWatchlist(storage.getItem(WATCHLIST_STORAGE_KEY)) ?? DEFAULT_WATCHLIST
    } catch {
      return DEFAULT_WATCHLIST
    }
  },
  saveWatchlist(items: readonly string[], storage: S | null): void {
    if (storage === null) return
    try {
      storage.setItem(WATCHLIST_STORAGE_KEY, serializeWatchlist(items))
    } catch {
      // ignore
    }
  },
  loadResearchTab(storage: S | null): ResearchTab {
    try {
      const v = storage?.getItem(RESEARCH_TAB_KEY)
      return RESEARCH_TABS.some((t) => t.id === v) ? (v as ResearchTab) : 'runs'
    } catch {
      return 'runs'
    }
  },
  saveResearchTab(tab: ResearchTab, storage: S | null): void {
    try {
      storage?.setItem(RESEARCH_TAB_KEY, tab)
    } catch {
      // ignore
    }
  },
  loadPlanText(storage: S | null): { text: string; edited: boolean } | null {
    try {
      const raw = storage?.getItem(PLAN_STORAGE_KEY)
      if (!raw) return null
      const v: unknown = JSON.parse(raw)
      if (typeof v !== 'object' || v === null) return null
      const { text, edited } = v as { text?: unknown; edited?: unknown }
      if (typeof text !== 'string' || typeof edited !== 'boolean') return null
      return { text: text.slice(0, 100_000), edited }
    } catch {
      return null
    }
  },
  savePlanText(text: string, edited: boolean, storage: S | null): void {
    try {
      storage?.setItem(PLAN_STORAGE_KEY, JSON.stringify({ text, edited }))
    } catch {
      // ignore
    }
  },
  loadDockPrefs(storage: S | null): DockPrefs {
    try {
      const raw = storage?.getItem(DOCK_STORAGE_KEY)
      if (raw) {
        const v = JSON.parse(raw) as { open?: unknown; height?: unknown }
        return {
          open: v.open === true,
          height:
            typeof v.height === 'number' && Number.isFinite(v.height)
              ? v.height
              : DOCK_DEFAULT_HEIGHT,
        }
      }
    } catch {
      // ignore
    }
    return { open: false, height: DOCK_DEFAULT_HEIGHT }
  },
  saveDockPrefs(prefs: DockPrefs, storage: S | null): void {
    try {
      storage?.setItem(DOCK_STORAGE_KEY, JSON.stringify(prefs))
    } catch {
      // ignore
    }
  },
}

// ---- Storages ----

function memory(key: string, text: string | null): S & { writes: [string, string][] } {
  const writes: [string, string][] = []
  return {
    writes,
    getItem: (k) => (k === key ? text : null),
    setItem: (k, v) => void writes.push([k, v]),
  }
}

const throwing: S = {
  getItem: () => {
    throw new Error('SecurityError')
  },
  setItem: () => {
    throw new Error('QuotaExceededError')
  },
}

/** Texts every key is read from: missing, empty, junk, JSON of each shape. */
const COMMON_TEXTS: (string | null)[] = [
  null,
  '',
  '{oops',
  'null',
  'true',
  'false',
  '0',
  '5',
  '"x"',
  '[]',
  '{}',
  'undefined',
]

function storagesFor(key: string, texts: (string | null)[]): (S | null)[] {
  return [null, throwing, ...[...COMMON_TEXTS, ...texts].map((t) => memory(key, t))]
}

function sameLoads<T>(
  key: string,
  texts: (string | null)[],
  next: (s: S | null) => T,
  old: (s: S | null) => T,
) {
  for (const s of storagesFor(key, texts)) expect(next(s)).toEqual(old(s))
}

function sameWrites<V>(
  key: string,
  values: V[],
  next: (v: V, s: S | null) => void,
  old: (v: V, s: S | null) => void,
) {
  for (const v of values) {
    const a = memory(key, null)
    const b = memory(key, null)
    next(v, a)
    old(v, b)
    expect(a.writes).toEqual(b.writes)
    expect(() => next(v, throwing)).not.toThrow()
    expect(() => next(v, null)).not.toThrow()
  }
}

describe('storedValue adoption: same reads, same bytes', () => {
  it('prefs (tv.prefs.v1)', () => {
    sameLoads(
      PREFS_KEY,
      [
        '{"symbol":"ethusdt","interval":"60s","chartType":"line"}',
        '{"symbol":"!!","interval":"9z","chartType":"x"}',
        JSON.stringify(DEFAULT_PREFS),
      ],
      loadPrefs,
      legacy.loadPrefs,
    )
    sameWrites(
      PREFS_KEY,
      [
        { symbol: 'ETHUSDT', interval: '5m', chartType: 'line' } as Prefs,
        { symbol: ' solusdt ', interval: '60s', chartType: 'nope' } as unknown as Prefs,
      ],
      savePrefs,
      legacy.savePrefs,
    )
  })

  it('regime and derivatives switches', () => {
    for (const [key, load, save] of [
      [REGIME_PREF_KEY, loadRegimeOn, saveRegimeOn],
      [DERIVATIVES_PREF_KEY, loadDerivativesOn, saveDerivativesOn],
    ] as const) {
      sameLoads(key, ['1', '"true"'], load, (s) => legacy.loadBool(key, s))
      sameWrites(key, [true, false], save, (v, s) => legacy.saveBool(key, v, s))
    }
  })

  it('settings (tv.settings.v1)', () => {
    sameLoads(
      SETTINGS_KEY,
      [
        '{"theme":"light","provider":"coinbase","upColor":"#ABCDEF","downColor":"bad","timezone":"Europe/London"}',
        '{"theme":"x","timezone":"Not/AZone"}',
      ],
      loadSettings,
      legacy.loadSettings,
    )
    sameWrites(
      SETTINGS_KEY,
      [
        defaultSettings(),
        { ...defaultSettings(), theme: 'light', upColor: '#AABBCC', timezone: 'local' },
        { ...defaultSettings(), downColor: 'red' },
      ],
      saveSettings,
      legacy.saveSettings,
    )
  })

  it('drawing style and sidebar panel', () => {
    const fallback: DrawingStyle = { color: '#2962ff', lineWidth: 2, lineStyle: 'solid' }
    sameLoads(
      DRAWING_STYLE_KEY,
      [
        '{"color":"#ff0000","lineWidth":3,"lineStyle":"dashed"}',
        '{"color":"#ff0000","lineWidth":7,"lineStyle":"dashed"}',
      ],
      (s) => loadDrawingStyle(fallback, s),
      (s) => legacy.loadDrawingStyle(fallback, s),
    )
    sameWrites(
      DRAWING_STYLE_KEY,
      [
        { color: '#ff0000', lineWidth: 3, lineStyle: 'dashed' } as DrawingStyle,
        { color: 'red', lineWidth: 1, lineStyle: 'solid' } as DrawingStyle,
      ],
      saveDrawingStyle,
      legacy.saveDrawingStyle,
    )
    sameLoads(
      SIDEBAR_PANEL_KEY,
      ['"watchlist"', '"alerts"', '"paper"', '"none"', 'watchlist'],
      loadSidebarPanel,
      legacy.loadSidebarPanel,
    )
    sameWrites(
      SIDEBAR_PANEL_KEY,
      ['watchlist', 'alerts', 'paper', null] as (SidebarPanel | null)[],
      saveSidebarPanel,
      legacy.saveSidebarPanel,
    )
  })

  it('watchlist (tv.watchlist.v1), the default list by reference', () => {
    sameLoads(
      WATCHLIST_STORAGE_KEY,
      ['["BTCUSDT","ETHUSDT"]', '["btcusdt"]', '["A","A"]', '["BTCUSDT",1]'],
      loadWatchlist,
      legacy.loadWatchlist,
    )
    expect(loadWatchlist(memory(WATCHLIST_STORAGE_KEY, '{oops'))).toBe(DEFAULT_WATCHLIST)
    expect(loadWatchlist(null)).toBe(DEFAULT_WATCHLIST)
    sameWrites(
      WATCHLIST_STORAGE_KEY,
      [[], ['BTCUSDT', 'ETHUSDT']],
      saveWatchlist,
      legacy.saveWatchlist,
    )
  })

  it('research tab (plain text) and its reset', () => {
    sameLoads(
      RESEARCH_TAB_KEY,
      [...RESEARCH_TABS.map((t) => t.id), '"journal"', 'Journal'],
      loadResearchTab,
      legacy.loadResearchTab,
    )
    sameWrites(
      RESEARCH_TAB_KEY,
      RESEARCH_TABS.map((t) => t.id),
      saveResearchTab,
      legacy.saveResearchTab,
    )
    const a = memory(RESEARCH_TAB_KEY, null)
    resetResearchTab(a)
    expect(a.writes).toEqual([[RESEARCH_TAB_KEY, 'runs']])
  })

  it('tester plan text and dock prefs', () => {
    sameLoads(
      PLAN_STORAGE_KEY,
      [
        '{"text":"abc","edited":true}',
        '{"text":"abc","edited":"yes"}',
        `{"text":"${'x'.repeat(100_010)}","edited":false}`,
      ],
      loadPlanText,
      legacy.loadPlanText,
    )
    sameWrites(
      PLAN_STORAGE_KEY,
      [
        ['{"kind":"strategy"}', true],
        ['', false],
      ] as [string, boolean][],
      ([t, e], s) => savePlanText(t, e, s),
      ([t, e], s) => legacy.savePlanText(t, e, s),
    )
    sameLoads(
      DOCK_STORAGE_KEY,
      ['{"open":true,"height":420}', '{"open":1,"height":"9"}', '{"height":1e999}'],
      loadDockPrefs,
      legacy.loadDockPrefs,
    )
    sameWrites(
      DOCK_STORAGE_KEY,
      [
        { open: true, height: 300 },
        { open: false, height: 340.5 },
      ],
      saveDockPrefs,
      legacy.saveDockPrefs,
    )
  })
})
