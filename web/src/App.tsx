import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type CSSProperties,
  type ErrorInfo,
} from 'react'
import { buildChartData, tradesOnChart, tradesZoomRange } from './chart/backtestOverlay'
import type { ChartApi } from './chart/Chart'
import { createCrosshairSync } from './chart/crosshairSync'
import { downloadCanvas, screenshotFilename } from './chart/screenshot'
import type { ChartAppearance } from './chart/theme'
import { timezoneLabel } from './chart/timeFormat'
import {
  DEFAULT_DRAWING_STYLE,
  type ChartContextMenuRequest,
  type Drawing,
  type DrawingPatch,
  type DrawingStyle,
  type ToolMode,
} from './chart/drawings/types'
import { AlertDialog, type AlertDialogValues } from './components/AlertDialog'
import { AlertsPanel } from './components/AlertsPanel'
import { AlertToasts, type AlertToast } from './components/AlertToasts'
import { BacktestPanel, type BacktestChartLink } from './components/BacktestPanel'
import {
  clampDockHeight,
  describeLabError,
  HISTORY_RETRY_MS,
  autoHistoryKey,
  autoHistoryStep,
  historyLoadFailed,
  historyLoadingText,
  historyLoadReducer,
  initialAutoHistory,
  historyPlan,
  initialHistoryLoad,
  initTesterState,
  isChartInterval,
  loadDockPrefs,
  loadPlanText,
  historyRoom,
  reportMatchesChart,
  saveDockPrefs,
  savePlanText,
  testerReducer,
  type DockPrefs,
} from './components/backtestView'
import type { FocusPlan } from './chart/backtestOverlay'
import { ChartContextMenu } from './components/ChartContextMenu'
import { ChartPanel, type PanelMarket } from './components/ChartPanel'
import { DrawingStyleBar } from './components/DrawingStyleBar'
import { DrawingToolbar } from './components/DrawingToolbar'
import { shortcutCommand } from './components/drawingTools'
import { Header } from './components/Header'
import { IndicatorMenu } from './components/IndicatorMenu'
import { IndicatorSettings } from './components/IndicatorSettings'
import { LayoutMenu } from './components/LayoutMenu'
import { PaperTradingPanel } from './components/PaperTradingPanel'
import { ReplayBar } from './components/ReplayBar'
import { summarizeQuote } from './components/quote'
import { ErrorBoundary } from './components/ErrorBoundary'
import { ResearchFallback, ResearchPage } from './components/ResearchPage'
import {
  loadPlanRestore,
  planRestoreAfterLoad,
  planRestoreValid,
  resetResearchTab,
  savePlanRestore,
  type CandidateRunRequest,
  type PlanRestore,
} from './components/researchView'
import { SidebarRail, type SidebarPanel } from './components/SidebarRail'
import { StatusBar } from './components/StatusBar'
import { SeasonalsView } from './components/SeasonalsView'
import { SettingsDialog } from './components/SettingsDialog'
import { ShortcutsDialog } from './components/ShortcutsDialog'
import { SymbolSearch } from './components/SymbolSearch'
import { Watchlist } from './components/Watchlist'
import type { Alert, AlertEvent, NotifyPermission } from './data/alertTypes'
import { createAlertsClient } from './data/alertsApi'
import { BinanceProvider, createBinanceSecondsSource } from './data/binance'
import { createDrawingsClient } from './data/drawingsApi'
import { createLabClient } from './data/labApi'
import { createResearchClient } from './data/researchApi'
import type { MarketDataProvider } from './data/provider'
import { createProvider } from './data/providers'
import { createTimeframeProvider, type TimeframeProvider } from './data/timeframeProvider'
import { isSecondsTimeframe, timeframeLabel, timeframeSeconds } from './data/timeframes'
import { venueLabels, type VenueInfo } from './data/venue'
import { getIndicatorDef } from './indicators/registry'
import type { IndicatorId } from './indicators/types'
import { useAlerts } from './state/alerts'
import {
  alertsUnavailableReason,
  fallbackSymbol,
  formatClock,
  labOnChart,
  labOverlayBlockedNotice,
  providerNeedsReload,
  providerProfile,
  symbolFallbackNotice,
  toggledTheme,
} from './state/appSettings'
import {
  buildHLineDrawing,
  dismissToast,
  formatPriceByTick,
  loadDrawingStyle,
  loadSidebarPanel,
  pushToast,
  saveDrawingStyle,
  saveSidebarPanel,
} from './state/chartTools'
import { useDrawings, type DrawingsApi } from './state/drawings'
import { anchorInputs, useIndicators } from './state/indicators'
import {
  notificationPermission,
  requestNotificationPermission,
  showBrowserNotification,
} from './state/notify'
import {
  assignSlots,
  layoutReducer,
  loadLayout,
  panelCount,
  pickPanel,
  sanitizeTimeframes,
  saveLayout,
  type LayoutKind,
  type PanelState,
} from './state/layout'
import {
  LIVE_OFFLINE_MESSAGE,
  liveFeedUpdates,
  liveQuotesOf,
  paperMarks,
  priceStamp,
  REPLAY_MARK_OFF,
  replayFeedStep,
  replayPaperPause,
  type LiveFeedSeen,
  type LiveQuote,
  type PriceStamp,
  type ReplayMark,
} from './state/paperFeeds'
import { savePrefs } from './state/prefs'
import { loadRegimeOn, saveRegimeOn } from './state/regimePref'
import {
  derivativesVenueBlocked,
  loadDerivativesOn,
  saveDerivativesOn,
} from './state/derivativesPref'
import { replayKeyAction } from './state/replay'
import {
  applyCandleColorsToDocument,
  applyThemeToDocument,
  loadSettings,
  saveSettings,
  type Settings,
} from './state/settings'
import { appShortcut, isMacPlatform, shortcutRepeats } from './state/shortcuts'
import { initialSnapshot } from './state/liveCandles'
import { secondsSourceKind } from './state/timeframeSources'
import type { UseLiveCandles } from './state/useLiveCandles'
import { usePaperAccount } from './state/usePaperAccount'
import { useReplay } from './state/useReplay'
import { useSymbols } from './state/useSymbols'
import { useWatchlist } from './state/watchlist'
import type { Candle, ChartType, Timeframe } from './types'

// The data provider comes from Settings, read once: it is fixed for the page's lifetime, and
// changing it in Settings saves and reloads. Default: the local server (cache + fan-out);
// `VITE_DATA_SOURCE=direct` defaults to Binance's market-data-only hosts from the browser
// (see defaultSettings). Coinbase is a second direct provider.
const PROVIDER_ID = loadSettings().provider
const PROFILE = providerProfile(PROVIDER_ID)
const DIRECT = PROFILE.direct
const provider: MarketDataProvider = createProvider(PROVIDER_ID)
// Every chart timeframe (seconds, custom) on top of the provider: native Intervals pass
// straight through, the rest are aggregated (createTimeframeProvider). Direct Binance has its
// own 1s klines; Coinbase has none.
const TIMEFRAMES: TimeframeProvider = createTimeframeProvider(provider, {
  seconds:
    secondsSourceKind(PROVIDER_ID, null) === 'provider' && provider instanceof BinanceProvider
      ? provider.secondsSource()
      : null,
})
// The local server has no 1s cache. When it serves Binance global, seconds charts fetch Binance's
// public 1s klines from the browser (data-api / data-stream.binance.vision), bypassing the
// server's cache and fan-out; every other timeframe still goes through the server. Built up
// front, used only once the server reports venue 'binance' (see timeframesFor).
const SERVER_SECONDS_TIMEFRAMES: TimeframeProvider | null =
  PROVIDER_ID === 'server'
    ? createTimeframeProvider(provider, { seconds: createBinanceSecondsSource() })
    : null

/** The timeframe provider for the venue the data comes from (null while it is unknown). */
function timeframesFor(venueId: string | null): TimeframeProvider {
  return secondsSourceKind(PROVIDER_ID, venueId) === 'binance-direct' &&
    SERVER_SECONDS_TIMEFRAMES !== null
    ? SERVER_SECONDS_TIMEFRAMES
    : TIMEFRAMES
}
const IS_MAC = isMacPlatform()
const needsReload = (next: Settings): boolean => providerNeedsReload(PROVIDER_ID, next)
// Module-level so the hooks see stable clients. Direct providers have no local server: drawings
// stay in localStorage and alerts are unavailable.
const drawingsClient = DIRECT ? null : createDrawingsClient()
const alertsClient = DIRECT ? null : createAlertsClient()
// The Python lab (backtests + ledger) is its own service behind the `/lab` proxy, in both modes.
const labClient = createLabClient()
// Phase 8 research runs, ideas, journal, candidates: read-only endpoints of the same lab service.
const researchClient = createResearchClient()

/** The Research view threw while rendering: log it, and reopen it on Runs next time. */
function onResearchError(error: Error, info: ErrorInfo): void {
  resetResearchTab()
  console.error('The Research view failed to render', error, info.componentStack)
}

/**
 * Source name inside chart error messages. Stable for the app's lifetime, so learning the
 * server's venue later does not restart the candle stream. Labels use `venueLabels` instead.
 */
const ERROR_SOURCE = PROFILE.errorSource
/** Retry interval while the server has not reported its venue yet. */
const VENUE_RETRY_MS = 5000
const LIMIT = 500
/** How long a small error notice (e.g. a failed alert delete) stays up. */
const NOTICE_MS = 6000
/** Candle colours reach the chart at most this often while a colour picker is dragged. */
const COLOR_APPLY_MS = 100
/** Settings are written to localStorage this long after the last change (trailing). */
const SETTINGS_SAVE_MS = 400

/**
 * `value`, updated at most once per `ms` (leading and trailing): the first change shows at once,
 * a burst of changes (a colour picker fires `input` continuously) shows its latest value at the
 * next slot, and the last value always lands. Compare by value (strings), not identity.
 */
function useThrottledValue<T>(value: T, ms: number): T {
  const [shown, setShown] = useState(value)
  const lastRef = useRef(0)
  useEffect(() => {
    if (Object.is(value, shown)) return
    // The slot is fixed by the last apply, so a stream of changes cannot postpone it.
    const wait = Math.max(0, lastRef.current + ms - Date.now())
    const timer = window.setTimeout(() => {
      lastRef.current = Date.now()
      setShown(value)
    }, wait)
    return () => window.clearTimeout(timer)
  }, [value, shown, ms])
  return shown
}

/** True when a keystroke should go to the focused control rather than open the search. */
function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  if (target.isContentEditable) return true
  const tag = target.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true
  // Listboxes/menus/dialogs (and confirms) handle their own keys (e.g. the chart type menu).
  return (
    target.closest('[role="listbox"], [role="menu"], [role="dialog"], [role="alertdialog"]') !==
    null
  )
}

/** A store call answered `false` (rejected). `true` or no answer counts as accepted. */
function isRejected(result: boolean | void): boolean {
  return result === false
}

function errorText(err: unknown, fallback: string): string {
  return err instanceof Error && err.message.trim() !== '' ? err.message : fallback
}

/** Live state of a panel that has not reported yet (or is not mounted). */
const IDLE_LIVE: UseLiveCandles = { ...initialSnapshot, reload: () => {}, loadOlder: () => {} }

/** Same keys, same values (Object.is): a panel's re-render without new live data. */
function sameLive(a: UseLiveCandles, b: UseLiveCandles): boolean {
  const keys = Object.keys(a) as (keyof UseLiveCandles)[]
  return keys.length === Object.keys(b).length && keys.every((k) => Object.is(a[k], b[k]))
}

/** A panel's last live report and the market it belongs to. */
interface PanelLive extends PanelMarket {
  live: UseLiveCandles
  /** When its latest price last changed (paper trading ticks and order times). */
  stamp: PriceStamp | null
}

/**
 * The panel's live state, or IDLE_LIVE while the stored report is still the previous market's
 * (the render right after a symbol or timeframe change): those candles are another market's.
 */
function panelLive(
  lives: Readonly<Record<string, PanelLive>>,
  panel: Pick<PanelState, 'id' | 'symbol' | 'interval'>,
): UseLiveCandles {
  const e = lives[panel.id]
  return e !== undefined && e.symbol === panel.symbol && e.interval === panel.interval
    ? e.live
    : IDLE_LIVE
}

const NO_BARS: readonly Candle[] = []

/** The bar replay session: the panel it runs on and the market it started on. */
interface ReplaySession extends PanelMarket {
  panelId: string
}

/** Every loaded bar, traded or not (pagination cursor, history planning, the quote). */
function rawCandles(live: UseLiveCandles): readonly Candle[] | null {
  return live.load.kind === 'ready' ? live.load.candles : null
}

/**
 * The last traded price. The last candle may still be forming; it is a display/default value
 * only. A bar without trades carries the previous close, so the raw last close is it.
 */
function lastCloseOf(live: UseLiveCandles): number | null {
  const candles = rawCandles(live)
  return candles !== null && candles.length > 0 ? candles[candles.length - 1].close : null
}

/** Open time of the last plotted bar. */
function lastBarTimeOf(live: UseLiveCandles): number | null {
  const bars = live.load.kind === 'ready' ? live.load.bars : null
  return bars !== null && bars.length > 0 ? bars[bars.length - 1].time : null
}

/**
 * One drawings store per distinct symbol in `symbols`, so panels on the same symbol share a
 * store and never overwrite each other. Hooks need a fixed count, so there are MAX_PANELS
 * slots; an unused slot is '' with no client (an empty, inert local store). A slot keeps its
 * symbol while it is still needed (assignSlots), so switching one panel leaves the others'
 * stores running. Returns the store for a symbol (any symbol in `symbolKey`).
 */
function useDrawingStores(
  /** Space-separated symbols of the visible panels. */
  symbolKey: string,
): (symbol: string) => DrawingsApi {
  const symbols = symbolKey.split(' ')
  const [slotState, setSlotState] = useState(() => assignSlots([], symbols))
  // A changed assignment re-renders at once, before anything commits (React's "state from
  // changed data" pattern); this render already uses it.
  const slots = assignSlots(slotState, symbols)
  if (slots !== slotState) setSlotState(slots)
  const s0 = useDrawings(slots[0], slots[0] === '' ? null : drawingsClient)
  const s1 = useDrawings(slots[1], slots[1] === '' ? null : drawingsClient)
  const s2 = useDrawings(slots[2], slots[2] === '' ? null : drawingsClient)
  const s3 = useDrawings(slots[3], slots[3] === '' ? null : drawingsClient)
  return useCallback(
    (symbol: string) => {
      const i = slots.indexOf(symbol)
      return i === 1 ? s1 : i === 2 ? s2 : i === 3 ? s3 : s0
    },
    [slots, s0, s1, s2, s3],
  )
}

interface SearchState {
  initialQuery: string
}

interface ContextMenuState {
  /** Fresh per request so the menu re-measures and refocuses. */
  key: number
  /** The panel that was right-clicked. */
  panelId: string
  request: ChartContextMenuRequest
}

/** The selected drawing and the panel it was selected on. */
interface DrawingSelection {
  panelId: string
  id: string
}

/** A time range the chart should scroll to; a fresh key per request. */
interface FocusRequest {
  key: number
  from: number
  to: number
  /** Only applied to a chart on this market (a symbol switch remounts the chart). */
  symbol: string
  interval: Timeframe
}

interface AlertDialogState {
  /** Fresh per open so the form starts from `initialPrice` / `editing`. */
  key: number
  /**
   * Fixed when the dialog opens (the edited alert's symbol, else the chart's). The chart symbol
   * can change underneath an open dialog (a toast click), and the alert must not follow it.
   */
  symbol: string
  initialPrice: number
  editing?: Alert
}

function App() {
  // ---- Layout: 1, 2 or 4 chart panels; the active one takes the toolbar and keyboard ----
  // Panel 0 starts from the single-chart prefs (`tv.prefs.v1`) the first time.
  const [layout, dispatchLayout] = useReducer(layoutReducer, null, () => loadLayout())
  // Memoized / plain reads of the reducer state, so React Compiler sees them as immutable and
  // keeps the memoization below.
  const layoutPanels = layout.panels
  const visible = useMemo(
    () => layoutPanels.slice(0, panelCount(layout.kind)),
    [layoutPanels, layout.kind],
  )
  const multi = visible.length > 1
  const activeIndex = layout.active
  const activeP = layout.panels[activeIndex]
  // The active panel's market: what the header, search, watchlist and shortcuts act on.
  const { symbol, interval, chartType } = activeP
  const [crosshairSync] = useState(createCrosshairSync)

  const [search, setSearch] = useState<SearchState | null>(null)
  const [panel, setPanel] = useState<SidebarPanel | null>(loadSidebarPanel)
  const [indicatorMenuOpen, setIndicatorMenuOpen] = useState(false)
  /** Instance whose settings dialog is open. */
  const [settingsUid, setSettingsUid] = useState<string | null>(null)

  const [tool, setTool] = useState<ToolMode>('cursor')
  const [selection, setSelection] = useState<DrawingSelection | null>(null)
  /** The last style picked in the style bar; new drawings start with it. */
  const [drawingStyle, setDrawingStyle] = useState<DrawingStyle>(() =>
    loadDrawingStyle(DEFAULT_DRAWING_STYLE),
  )
  const [contextMenu, setContextMenu] = useState<ContextMenuState | null>(null)
  const [alertDialog, setAlertDialog] = useState<AlertDialogState | null>(null)
  const [toasts, setToasts] = useState<readonly AlertToast[]>([])
  const [notice, setNotice] = useState<string | null>(null)
  const [permission, setPermission] = useState<NotifyPermission>(notificationPermission)
  const keySeq = useRef(0)

  // ---- Settings (theme, colours, time zone; the provider applies on reload) ----
  const [settings, setSettings] = useState<Settings>(loadSettings)
  const [settingsOpen, setSettingsOpen] = useState(false)
  /** Keyboard shortcuts / Seasonals dialogs: a fresh key per open, null when closed. */
  const [shortcutsKey, setShortcutsKey] = useState<number | null>(null)
  const [seasonalsKey, setSeasonalsKey] = useState<number | null>(null)
  const { theme, timezone } = settings
  // The dialog shows every colour-picker step at once; the chart, the document tokens and the
  // backtest markers follow at most every COLOR_APPLY_MS (each step re-sets the volume series).
  const upColor = useThrottledValue(settings.upColor, COLOR_APPLY_MS)
  const downColor = useThrottledValue(settings.downColor, COLOR_APPLY_MS)
  // Persisted on a trailing debounce, and flushed when the page goes away.
  const settingsRef = useRef(settings)
  const saveTimerRef = useRef<number | null>(null)
  useEffect(() => {
    settingsRef.current = settings
    const timer = window.setTimeout(() => saveSettings(settings), SETTINGS_SAVE_MS)
    saveTimerRef.current = timer
    return () => window.clearTimeout(timer)
  }, [settings])
  useEffect(() => {
    const flush = () => saveSettings(settingsRef.current)
    window.addEventListener('pagehide', flush)
    return () => {
      window.removeEventListener('pagehide', flush)
      flush()
    }
  }, [])
  useEffect(() => {
    applyThemeToDocument(theme)
  }, [theme])
  useEffect(() => {
    applyCandleColorsToDocument(upColor, downColor)
  }, [upColor, downColor])
  const appearance = useMemo<ChartAppearance>(
    () => ({ theme, upColor, downColor, timezone }),
    [theme, upColor, downColor, timezone],
  )
  const changeSettings = useCallback((next: Settings) => {
    if (needsReload(next)) {
      // A new provider means new symbols, streams and server features: start the page over.
      // The ref first, so the pagehide flush writes `next` rather than the old settings, and
      // drop any pending debounced save of the old settings.
      settingsRef.current = next
      if (saveTimerRef.current !== null) window.clearTimeout(saveTimerRef.current)
      saveSettings(next)
      window.location.reload()
      return
    }
    setSettings(next)
  }, [])
  const toggleTheme = useCallback(
    () => setSettings((s) => ({ ...s, theme: toggledTheme(s.theme) })),
    [],
  )
  const openSettingsDialog = useCallback(() => setSettingsOpen(true), [])
  const closeSettingsDialog = useCallback(() => setSettingsOpen(false), [])
  const openShortcuts = useCallback(() => {
    keySeq.current += 1
    setShortcutsKey(keySeq.current)
  }, [])
  const closeShortcuts = useCallback(() => setShortcutsKey(null), [])
  const openSeasonals = useCallback(() => {
    keySeq.current += 1
    setSeasonalsKey(keySeq.current)
  }, [])
  const closeSeasonals = useCallback(() => setSeasonalsKey(null), [])

  /** Each mounted chart's API (screenshots), by panel id. */
  const chartApisRef = useRef(new Map<string, ChartApi>())
  const onChartApi = useCallback((panelId: string, api: ChartApi | null) => {
    if (api === null) chartApisRef.current.delete(panelId)
    else chartApisRef.current.set(panelId, api)
  }, [])

  // Each panel owns its live data (ChartPanel calls useLiveCandles) and reports it here after
  // every render; only a real change is stored, so a re-render cannot loop.
  const [lives, setLives] = useState<Readonly<Record<string, PanelLive>>>({})
  const livesRef = useRef(lives)
  const onPanelLive = useCallback(
    (panelId: string, live: UseLiveCandles | null, market?: PanelMarket) => {
      const prev = livesRef.current
      const cur = prev[panelId]
      if (live === null || market === undefined) {
        if (cur === undefined) return
      } else if (
        cur !== undefined &&
        cur.symbol === market.symbol &&
        cur.interval === market.interval &&
        sameLive(cur.live, live)
      ) {
        return
      }
      const next = { ...prev }
      if (live === null || market === undefined) delete next[panelId]
      else {
        // Stamped with the wall-clock time the last bar's close changed, never on a history
        // load or refresh that leaves it as it was (see priceStamp).
        const stamp = priceStamp(cur?.stamp ?? null, market.symbol, rawCandles(live), Date.now())
        next[panelId] = { live, symbol: market.symbol, interval: market.interval, stamp }
      }
      livesRef.current = next
      setLives(next)
    },
    [],
  )
  const liveOf = useCallback(
    (panelId: string): UseLiveCandles => {
      const p = layoutPanels.find((x) => x.id === panelId)
      return p === undefined ? IDLE_LIVE : panelLive(lives, p)
    },
    [lives, layoutPanels],
  )

  // Strategy Tester: dock layout, panel state (kept here so a run survives closing the dock).
  const [dock, setDock] = useState<DockPrefs>(loadDockPrefs)
  const [tester, dispatchTester] = useReducer(testerReducer, null, () =>
    initTesterState(loadPlanText()),
  )
  const [historyLoad, dispatchHistory] = useReducer(historyLoadReducer, initialHistoryLoad)
  const historyTarget = historyLoad.target
  const [focusRange, setFocusRange] = useState<FocusRequest | null>(null)
  /** The Research view covers the chart area; the chart stays mounted underneath. */
  const [researchOpen, setResearchOpen] = useState(false)
  /** A candidate's plan replaced the user's own tester plan; offered back until dismissed. */
  const [planRestore, setPlanRestore] = useState<PlanRestore | null>(loadPlanRestore)
  const appRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    saveLayout(layout)
  }, [layout])
  // Panel 0 is the single-chart app's chart: keep its prefs current for older readers.
  const first = layout.panels[0]
  useEffect(() => {
    savePrefs({ symbol: first.symbol, interval: first.interval, chartType: first.chartType })
  }, [first.symbol, first.interval, first.chartType])

  useEffect(() => {
    saveSidebarPanel(panel)
  }, [panel])

  useEffect(() => {
    saveDockPrefs(dock)
  }, [dock])

  useEffect(() => {
    savePlanText(tester.planText, tester.planEdited)
  }, [tester.planText, tester.planEdited])

  useEffect(() => {
    if (notice === null) return
    const t = setTimeout(() => setNotice(null), NOTICE_MS)
    return () => clearTimeout(t)
  }, [notice])

  // The venue the chart's data comes from: fixed in direct mode, reported by the server else.
  const [venue, setVenue] = useState<VenueInfo | null>(PROFILE.initialVenue)
  useEffect(() => {
    if (venue !== null || provider.getVenue === undefined) return
    const ctrl = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    const ask = (): void => {
      provider.getVenue?.({ signal: ctrl.signal }).then(
        (v) => {
          if (!ctrl.signal.aborted) setVenue(v)
        },
        () => {
          if (!ctrl.signal.aborted) timer = setTimeout(ask, VENUE_RETRY_MS)
        },
      )
    }
    ask()
    return () => {
      ctrl.abort()
      if (timer !== undefined) clearTimeout(timer)
    }
  }, [venue])
  const { exchange: EXCHANGE, source: SOURCE } = venueLabels(venue, DIRECT)
  // Lab backtests are Binance global spot data: drawn only when the chart's data really is
  // (the server may run on Binance.US; Coinbase is another market). Unknown venue: not drawn.
  const labAllowed = labOnChart(venue)
  const labBlockedNotice = labAllowed ? null : labOverlayBlockedNotice(venue, DIRECT)

  // ---- Timeframes: seconds need Binance 1s klines (see timeframesFor) ----
  const timeframes = timeframesFor(venue?.id ?? null)
  const supportsTimeframe = timeframes.supports
  // Once the venue is known, a saved timeframe it cannot serve (e.g. 5s after switching to
  // Coinbase) falls back to 1m. Checked once; adjusted during render like the symbol check.
  const [timeframesChecked, setTimeframesChecked] = useState(false)
  if (!timeframesChecked && venue !== null) {
    setTimeframesChecked(true)
    const fixed = sanitizeTimeframes(layout, supportsTimeframe, '1m')
    if (fixed !== layout) {
      const changed = fixed.panels.flatMap((p, index) =>
        p.interval !== layout.panels[index].interval
          ? [{ index, from: layout.panels[index].interval, to: p.interval }]
          : [],
      )
      changed.forEach((c) =>
        dispatchLayout({ type: 'set-interval', index: c.index, interval: c.to }),
      )
      if (changed.length > 0) {
        setNotice(
          `${timeframeLabel(changed[0].from)} charts are not available from ${venue.name}; showing ${timeframeLabel(changed[0].to)} instead.`,
        )
      }
    }
  }

  // ---- Regime ribbon (all panels; remembered across visits) ----
  const [regimeOn, setRegimeOn] = useState(loadRegimeOn)
  useEffect(() => {
    saveRegimeOn(regimeOn)
  }, [regimeOn])
  const toggleRegime = useCallback(() => setRegimeOn((on) => !on), [])

  // ---- Funding / OI panes (all panels; remembered; Binance global spot charts only) ----
  const [derivativesOn, setDerivativesOn] = useState(loadDerivativesOn)
  useEffect(() => {
    saveDerivativesOn(derivativesOn)
  }, [derivativesOn])
  const toggleDerivatives = useCallback(() => setDerivativesOn((on) => !on), [])
  const derivativesBlocked = derivativesVenueBlocked(venue)

  // Memoised so React Compiler treats the result as immutable (it is never mutated).
  const live = useMemo(() => panelLive(lives, activeP), [lives, activeP])
  const { load } = live
  const symbolList = useSymbols(provider)
  const watchlist = useWatchlist()
  // One indicator list for every panel (v1): adding, editing or removing an indicator applies
  // to all charts in the layout.
  const indicators = useIndicators()

  // Drawings: one store per distinct visible symbol (see useDrawingStores).
  const drawingsOf = useDrawingStores(visible.map((p) => p.symbol).join(' '))
  const drawings = drawingsOf(symbol)
  const panelById = useCallback(
    (panelId: string) => layoutPanels.find((p) => p.id === panelId),
    [layoutPanels],
  )

  const onTriggered = useCallback((event: AlertEvent) => {
    setToasts((ts) => pushToast(ts, { key: String(event.id), event }))
    if (notificationPermission() === 'granted') showBrowserNotification(event)
  }, [])
  const alerts = useAlerts({ client: alertsClient, onTriggered })
  const { markSeen } = alerts

  // An open alerts panel counts as seen, including triggers that arrive while it is open.
  useEffect(() => {
    if (panel === 'alerts' && alerts.unseen > 0) markSeen()
  }, [panel, alerts.unseen, markSeen])

  // A removed indicator closes its settings dialog (the uid simply stops resolving).
  const settingsConfig =
    settingsUid !== null ? indicators.items.find((c) => c.uid === settingsUid) : undefined
  const settingsDef = settingsConfig ? getIndicatorDef(settingsConfig.type) : undefined

  // ---- Bar replay: one session at a time, on the panel it was started on ----
  // The panel plots `replay.visible` (closed bars up to the cursor) while its live stream keeps
  // loading underneath. A symbol or timeframe change on that panel, a layout that hides it, Esc,
  // or Exit ends the session.
  const [replaySession, setReplaySession] = useState<ReplaySession | null>(null)
  const replayCell =
    replaySession !== null ? visible.find((p) => p.id === replaySession.panelId) : undefined
  const replayValid =
    replaySession !== null &&
    replayCell !== undefined &&
    replayCell.symbol === replaySession.symbol &&
    replayCell.interval === replaySession.interval
  const replayPanel = replayValid ? replayCell : activeP
  const replayLive = useMemo(() => panelLive(lives, replayPanel), [lives, replayPanel])
  const replaySource = replayLive.load.kind === 'ready' ? replayLive.load.bars : NO_BARS
  // Called unconditionally; idle (off) until a session starts.
  const replay = useReplay(replaySource)
  const {
    exit: exitReplay,
    startPicking: startReplayPicking,
    pickAt: replayPickAt,
    stepForward: replayStepForward,
    stepBack: replayStepBack,
    togglePlay: replayTogglePlay,
    cancelPicking: replayCancelPicking,
  } = replay
  if (replaySession !== null && !replayValid) {
    // Adjusted during render (React's "state from changed data" pattern), like the symbol check.
    setReplaySession(null)
    exitReplay()
  }
  const replayPanelId = replayValid ? replaySession.panelId : null
  const replayStatus = replay.state.status
  /**
   * The replay panel shows replayed history (active, or re-picking a start from a running
   * replay): no backtest overlay, and the quote and paper trading follow the replay.
   */
  const replayShown = replayPanelId !== null && replay.visible !== null
  const replayBlockedReason =
    load.kind === 'ready' && load.bars.length > 1
      ? null
      : 'Bar replay needs the chart’s bars to load first'

  const closeReplay = useCallback(() => {
    exitReplay()
    setReplaySession(null)
  }, [exitReplay])
  const toggleReplay = useCallback(() => {
    if (replaySession !== null) {
      closeReplay()
      return
    }
    setReplaySession({ panelId: activeP.id, symbol, interval })
    // Pick mode: the next left click on that chart picks the start bar (cursor tool only).
    setTool('cursor')
    startReplayPicking()
  }, [replaySession, closeReplay, activeP.id, symbol, interval, startReplayPicking])
  const pickReplayBar = useCallback(
    (_panelId: string, time: number | null) => {
      if (time !== null) replayPickAt(time)
    },
    [replayPickAt],
  )

  // The active panel's bars: the quote, the status bar, and alerts at the last price.
  const activeCandles = load.kind === 'ready' ? load.candles : null
  // What the chart plots: bars without trades are left out, as on TradingView.
  const bars = load.kind === 'ready' ? load.bars : null
  const lastClose = lastCloseOf(live)
  // In replay the header quote is the replayed bar's, never the hidden live price.
  const quoteCandles = replayShown && replayPanelId === activeP.id ? replay.visible : activeCandles
  const quote = useMemo(
    () => (quoteCandles ? summarizeQuote(quoteCandles, interval) : null),
    [quoteCandles, interval],
  )

  // ---- Paper trading: a live account (saved) and a replay account (memory only) ----
  const paperLive = usePaperAccount('live')
  const paperReplay = usePaperAccount('replay')
  const { onPrice: paperLiveTick } = paperLive
  const { onBar: paperReplayBar, restart: paperReplayRestart } = paperReplay
  // The latest price of every live chart on screen, stamped with the wall-clock time it last
  // changed (never a bar's open time: a limit only fills on a tick after it was placed).
  // Connecting, reconnecting and offline panels may show a stale price: they feed nothing.
  const liveQuotes = useMemo<LiveQuote[]>(
    () =>
      liveQuotesOf(
        visible.flatMap((p) => {
          const e = lives[p.id]
          return e !== undefined && e.symbol === p.symbol && e.interval === p.interval
            ? [{ panelId: p.id, symbol: p.symbol, status: e.live.status, stamp: e.stamp }]
            : []
        }),
      ),
    [visible, lives],
  )
  // Live ticks fill the live account's limits, on every chart on screen (replay or not: the
  // live stream keeps running underneath a replay). Each tick is fed once.
  const liveFeedRef = useRef<LiveFeedSeen>(new Map())
  useEffect(() => {
    const { fresh, seen } = liveFeedUpdates(liveFeedRef.current, liveQuotes)
    liveFeedRef.current = seen
    for (const q of fresh) paperLiveTick(q.symbol, q.price, q.time)
  }, [liveQuotes, paperLiveTick])
  // Replay steps feed the replay account every revealed bar, oldest first (a 10-bar skip fills
  // limits like ten single steps); bars at or before its high-water mark were fed already and
  // are dropped. A new replay starts a fresh account whose high-water mark is the start bar.
  const replayMarkRef = useRef<ReplayMark>(REPLAY_MARK_OFF)
  const replayCursor = replay.state.cursorTime
  const replayVisible = replay.visible
  const replaySymbol = replayValid ? replaySession.symbol : null
  useEffect(() => {
    const next: ReplayMark = { status: replayStatus, cursorTime: replayCursor }
    const step = replayFeedStep(replayMarkRef.current, next, replayVisible)
    replayMarkRef.current = next
    if (step.kind === 'reset') paperReplayRestart(step.at)
    else if (step.kind === 'bars' && replaySymbol !== null) {
      for (const b of step.bars) paperReplayBar(replaySymbol, b)
    }
  }, [replayStatus, replayCursor, replayVisible, replaySymbol, paperReplayBar, paperReplayRestart])
  const paperMarksForActive = useMemo(() => paperMarks(liveQuotes, symbol), [liveQuotes, symbol])
  // The paper panel stays on the replay account whenever the active chart shows replayed bars,
  // also while re-picking a start bar (then trading is paused: replayPaperPause).
  const activePaperReplay = replayShown && replayPanelId === activeP.id
  const replayBar = activePaperReplay ? (replay.visible?.at(-1) ?? null) : null
  const paperPaused = activePaperReplay ? replayPaperPause(replayStatus) : null
  const paperPrice = activePaperReplay ? (replayBar?.close ?? null) : lastClose
  // Orders are stamped with the moment they are placed: the replayed bar's time (only later
  // bars can fill them), or the wall-clock time the active chart's price last changed.
  const activeEntry = lives[activeP.id]
  const activeStamp =
    activeEntry !== undefined && activeEntry.symbol === symbol && activeEntry.interval === interval
      ? activeEntry.stamp
      : null
  const paperTime = activePaperReplay ? (replayBar?.time ?? null) : (activeStamp?.at ?? null)
  // A chart that is not live may show a stale price: no market orders at it, and no mark.
  const paperOffline = !activePaperReplay && live.status !== 'live' ? LIVE_OFFLINE_MESSAGE : null
  // ---- Strategy Tester ↔ chart ----

  const report = tester.report
  // The panel the report is drawn on: the active panel when it shows the report's market,
  // else the first panel that does. Never on a chart whose prices are another venue's
  // (Coinbase, Binance.US): see labAllowed.
  const btIndex =
    labAllowed && report !== null
      ? pickPanel(layout, (p) => reportMatchesChart(report, p.symbol, p.interval))
      : -1
  const reportOnMarket = btIndex >= 0
  // Without a match the tester compares against (and switches) the active panel.
  const btPanel = btIndex >= 0 ? layout.panels[btIndex] : activeP
  const btSymbol = btPanel.symbol
  const btInterval = btPanel.interval
  const btLive = useMemo(() => panelLive(lives, btPanel), [lives, btPanel])
  const candles = btLive.load.kind === 'ready' ? btLive.load.candles : null
  const chartData = useMemo(
    () => (report ? buildChartData(report, { win: upColor, loss: downColor }) : null),
    [report, upColor, downColor],
  )
  const backtestDrawn = tester.shownOnChart && reportOnMarket
  // Raw first bar: a page of bars without trades still moves the history back.
  const firstBarTime = candles !== null && candles.length > 0 ? candles[0].time : null
  // Live ticks replace `candles` but rarely change its length; plans depend on the length only.
  const candleCount = candles?.length ?? null
  const history = useMemo(
    () =>
      backtestDrawn && report && candleCount !== null
        ? historyPlan(firstBarTime, report.window, btInterval, historyRoom(candleCount))
        : null,
    [backtestDrawn, report, candleCount, firstBarTime, btInterval],
  )
  // Active only for the market it was started on, until the window start is loaded.
  const historyActive =
    historyTarget !== null &&
    backtestDrawn &&
    historyTarget.symbol === btSymbol &&
    historyTarget.interval === btInterval &&
    firstBarTime !== null &&
    firstBarTime > historyTarget.target &&
    !btLive.historyExhausted
  const historyFailed = backtestDrawn && historyLoadFailed(historyLoad, btSymbol, btInterval)
  // The last load on this market got the first bar to its target (it may have stopped short of
  // the bar cap because of exchange gaps).
  const historyReached =
    historyTarget !== null &&
    historyTarget.symbol === btSymbol &&
    historyTarget.interval === btInterval &&
    firstBarTime !== null &&
    firstBarTime <= historyTarget.target
  const { loadOlder, loadingOlder, retryBlocked } = btLive

  // Count pages of the auto-load: a page that ends without moving the first bar back failed.
  // The reducer gives up after a few consecutive failures instead of spinning forever.
  const pageStartRef = useRef<number | null | undefined>(undefined)
  useEffect(() => {
    if (!historyActive) {
      pageStartRef.current = undefined
      return
    }
    if (loadingOlder) {
      if (pageStartRef.current === undefined) {
        pageStartRef.current = firstBarTime
        dispatchHistory({ type: 'request' })
      }
      return
    }
    const start = pageStartRef.current
    if (start === undefined) return
    pageStartRef.current = undefined
    dispatchHistory({ type: 'page-done', progressed: firstBarTime !== start })
  }, [historyActive, loadingOlder, firstBarTime])

  // Ask for the next page now and keep asking on an interval: loadOlder is a no-op during its
  // cooldown after a failed page, so a single retry could land inside it and never re-arm.
  // A Retry-After block pauses the loop; it re-arms when the block lifts. The user asked for
  // this history, so it may page past the chart's scroll-back cap in one go (historyPlan
  // bounds the target by historyRoom: one load's budget, never past CHART_MAX_BARS).
  useEffect(() => {
    if (!historyActive || loadingOlder || retryBlocked) return
    loadOlder(true)
    const id = window.setInterval(() => {
      dispatchHistory({ type: 'tick' })
      loadOlder(true)
    }, HISTORY_RETRY_MS)
    return () => window.clearInterval(id)
  }, [historyActive, loadingOlder, retryBlocked, firstBarTime, loadOlder])

  // While loading, count the bars left to the target the load was started with.
  const historyTargetTime = historyActive ? historyTarget.target : null
  const historyLeft = useMemo(
    () =>
      historyTargetTime !== null && history && report
        ? {
            ...history,
            bars: historyPlan(
              firstBarTime,
              { ...report.window, start: historyTargetTime },
              btInterval,
            ).bars,
          }
        : history,
    [historyTargetTime, history, report, firstBarTime, btInterval],
  )
  // The chart draws trade markers on plotted bars only, so the Strategy Tester counts and
  // links trades from the first PLOTTED bar (history planning above stays on raw bars).
  const btBars = btLive.load.kind === 'ready' ? btLive.load.bars : null
  const plottedFirstTime = btBars !== null && btBars.length > 0 ? btBars[0].time : null
  // Runs over every trade, so only when the report or the first plotted bar changes.
  const onChartCount = useMemo(
    () => (report && backtestDrawn ? tradesOnChart(report.trades, plottedFirstTime) : 0),
    [report, backtestDrawn, plottedFirstTime],
  )
  const historyExhausted = btLive.historyExhausted
  // Stable between live ticks so the memoised dock does not re-render on every tick.
  const testerLink = useMemo<BacktestChartLink>(
    () => ({
      drawn: backtestDrawn,
      matches: reportOnMarket,
      canSwitch: report !== null && isChartInterval(report.interval),
      tradesOnChart: onChartCount,
      firstBarTime: plottedFirstTime,
      history: historyLeft,
      historyLoading: historyActive,
      historyExhausted,
      historyFailed,
    }),
    [
      backtestDrawn,
      reportOnMarket,
      report,
      onChartCount,
      plottedFirstTime,
      historyLeft,
      historyActive,
      historyExhausted,
      historyFailed,
    ],
  )

  const byName = symbolList.byName
  const info = byName?.get(symbol)
  const description = info ? `${info.baseAsset} / ${info.quoteAsset}` : ''

  const formatSymbolPrice = useCallback(
    (sym: string, price: number) => formatPriceByTick(price, byName?.get(sym)?.tickSize),
    [byName],
  )

  const alertsAvailable = alerts.status === 'ready'
  const alertDisabledReason = alertsUnavailableReason(PROVIDER_ID, alerts.status)

  // The selected drawing, on a panel that is still shown.
  const selectedPanel = selection !== null ? panelById(selection.panelId) : undefined
  const selectedDrawing =
    selection !== null && selectedPanel !== undefined && visible.includes(selectedPanel)
      ? drawingsOf(selectedPanel.symbol).items.find((d) => d.id === selection.id)
      : undefined

  const openSearch = useCallback((initialQuery = '') => setSearch({ initialQuery }), [])
  const openIndicatorMenu = useCallback(() => setIndicatorMenuOpen(true), [])
  const closeIndicatorMenu = useCallback(() => setIndicatorMenuOpen(false), [])
  // Time-anchored indicators (Anchored VWAP, volume profile) start at `time`; from the menu that
  // is the first bar in view of the active chart, so they show something straight away.
  const addIndicator = useCallback(
    (type: IndicatorId, time?: number | null) => {
      const def = getIndicatorDef(type)
      if (!def) return null
      const anchor =
        time !== undefined
          ? time
          : (chartApisRef.current.get(activeP.id)?.firstVisibleTime() ?? null)
      const uid = indicators.add(type, anchorInputs(def, anchor))
      // The indicator menu reports the limit itself; the chart's right-click menu does not.
      if (uid === null && time !== undefined) {
        setNotice('Indicator limit reached. Remove one to add another.')
      }
      return uid
    },
    [indicators, activeP.id],
  )
  const closeSettings = useCallback(() => setSettingsUid(null), [])
  const closeSearch = useCallback(() => setSearch(null), [])

  // Symbol, interval and chart type changes act on the active panel (symbol sync applies the
  // symbol to every panel; see layoutReducer).
  const selectSymbol = useCallback(
    (next: string) => {
      dispatchLayout({ type: 'set-symbol', index: activeIndex, symbol: next })
      // A pending scroll request belongs to the old chart.
      setFocusRange(null)
      setSearch(null)
      // Drawing state belongs to the old symbol's chart (this also hides the style bar).
      setTool('cursor')
      setSelection(null)
      setContextMenu(null)
    },
    [activeIndex],
  )

  const changeInterval = useCallback(
    (next: Timeframe) => {
      dispatchLayout({ type: 'set-interval', index: activeIndex, interval: next })
      setFocusRange(null)
    },
    [activeIndex],
  )

  const changeChartType = useCallback(
    (next: ChartType) =>
      dispatchLayout({ type: 'set-chart-type', index: activeIndex, chartType: next }),
    [activeIndex],
  )

  /** Make panel `index` active. The selection belonged to the previous panel. */
  const activateIndex = useCallback(
    (index: number) => {
      if (index === activeIndex) return
      dispatchLayout({ type: 'set-active', index })
      setSelection(null)
    },
    [activeIndex],
  )
  const activatePanel = useCallback(
    (panelId: string) => activateIndex(layoutPanels.findIndex((p) => p.id === panelId)),
    [activateIndex, layoutPanels],
  )
  const changeLayout = useCallback((kind: LayoutKind) => {
    dispatchLayout({ type: 'set-layout', kind })
    setSelection(null)
    setContextMenu(null)
  }, [])
  const setSyncSymbol = useCallback(
    (on: boolean) => dispatchLayout({ type: 'set-sync-symbol', on }),
    [],
  )
  const setSyncCrosshair = useCallback(
    (on: boolean) => dispatchLayout({ type: 'set-sync-crosshair', on }),
    [],
  )

  const showBacktestOnChart = useCallback(() => {
    if (report === null) return
    if (labBlockedNotice !== null) {
      setNotice(labBlockedNotice)
      return
    }
    const match = pickPanel(layout, (p) => reportMatchesChart(report, p.symbol, p.interval))
    if (match >= 0) activateIndex(match)
    else {
      if (report.symbol !== symbol) selectSymbol(report.symbol)
      if (report.interval !== interval && isChartInterval(report.interval)) {
        changeInterval(report.interval)
      }
    }
    dispatchTester({ type: 'show-on-chart', shown: true })
  }, [
    report,
    layout,
    symbol,
    interval,
    selectSymbol,
    changeInterval,
    activateIndex,
    labBlockedNotice,
  ])

  const clearBacktestFromChart = useCallback(() => {
    dispatchTester({ type: 'show-on-chart', shown: false })
    dispatchHistory({ type: 'stop' })
  }, [])

  const loadBacktestHistory = useCallback(() => {
    // An unreachable window lies beyond the chart's bar limit: fetching would never reach it.
    if (history === null || !history.needed || history.unreachable) return
    dispatchHistory({
      type: 'start',
      target: { symbol: btSymbol, interval: btInterval, target: history.target },
    })
  }, [history, btSymbol, btInterval])

  const stopBacktestHistory = useCallback(() => dispatchHistory({ type: 'stop' }), [])

  const focusChartRange = useCallback(
    (from: number, to: number) => {
      keySeq.current += 1
      setFocusRange({ key: keySeq.current, from, to, symbol: btSymbol, interval: btInterval })
    },
    [btSymbol, btInterval],
  )

  // Once per report and market: load the history back to the design window start (same path
  // as "Load full history"), then zoom to the most recent trades. Stopping it, a failed load,
  // or an unreachable window ends it without a zoom; the same report never starts again.
  const autoHistoryRef = useRef(initialAutoHistory)
  const autoKey = backtestDrawn && report !== null ? autoHistoryKey(report) : null
  useEffect(() => {
    const { state, command } = autoHistoryStep(autoHistoryRef.current, {
      key: autoKey,
      plan: history,
      loading: historyActive,
      failed: historyFailed,
      exhausted: historyExhausted,
      reachedTarget: historyReached,
    })
    autoHistoryRef.current = state
    if (command === 'start' && history !== null) {
      dispatchHistory({
        type: 'start',
        target: { symbol: btSymbol, interval: btInterval, target: history.target },
      })
    } else if (command === 'zoom' && report !== null) {
      const r = tradesZoomRange(report.trades, report.window, timeframeSeconds(btInterval))
      focusChartRange(r.from, r.to)
    }
  }, [
    autoKey,
    history,
    historyActive,
    historyFailed,
    historyExhausted,
    historyReached,
    report,
    btSymbol,
    btInterval,
    focusChartRange,
  ])

  // The chart handled a request: drop it so a remounted chart does not replay it.
  const focusDone = useCallback((key: number, result: FocusPlan['kind']) => {
    setFocusRange((cur) => (cur?.key === key ? null : cur))
    if (result === 'before-data') {
      setNotice(
        'That part of the backtest is older than the bars on the chart. Use “Load full history” in the Strategy Tester first.',
      )
    } else if (result === 'after-data') {
      setNotice('That part of the backtest is newer than the bars on the chart.')
    }
  }, [])

  // ---- Research → Strategy Tester ----

  // A candidate click: put its plan in the Strategy Tester, switch the chart to its market, run
  // it (design window, like the Run button), and show the chart with the trades and equity.
  const testerRunning = tester.run.kind === 'running'
  const testerRunSeq = tester.runSeq
  const testerPlanText = tester.planText
  const testerPlanEdited = tester.planEdited

  // Put a candidate's plan in the editor. The user's own edited plan is never lost: it is kept
  // and offered back with "Restore previous plan" (no blocking confirm).
  const loadPlanIntoTester = useCallback(
    (req: CandidateRunRequest) => {
      setPlanRestore((pending) =>
        planRestoreAfterLoad(
          { planText: testerPlanText, planEdited: testerPlanEdited },
          pending,
          req.planText,
        ),
      )
      dispatchTester({ type: 'edit', text: req.planText })
    },
    [testerPlanText, testerPlanEdited],
  )
  const restoreValid = planRestoreValid(
    { planText: testerPlanText, planEdited: testerPlanEdited },
    planRestore,
  )
  const restorePreviousPlan = useCallback(() => {
    if (planRestore === null) return
    dispatchTester({ type: 'edit', text: planRestore.previous })
    setPlanRestore(null)
  }, [planRestore])
  const dismissPlanRestore = useCallback(() => setPlanRestore(null), [])
  // Kept across a page reload while the offer still applies; a lapsed offer is dropped.
  const storedPlanRestore = restoreValid ? planRestore : null
  useEffect(() => {
    savePlanRestore(storedPlanRestore)
  }, [storedPlanRestore])

  const showCandidate = useCallback(
    (req: CandidateRunRequest) => {
      if (testerRunning) {
        setNotice('A Strategy Tester backtest is already running. Try again when it finishes.')
        return
      }
      loadPlanIntoTester(req)
      if (labBlockedNotice !== null) {
        // The run still goes ahead in the Strategy Tester; the chart keeps its own market.
        setNotice(labBlockedNotice)
      } else {
        // A panel already on the candidate's market takes it; else the active panel switches.
        const match = pickPanel(
          layout,
          (p) =>
            p.symbol === req.symbol &&
            (req.chartInterval === null || p.interval === req.chartInterval),
        )
        if (match >= 0) activateIndex(match)
        else {
          if (req.symbol !== symbol) selectSymbol(req.symbol)
          if (req.chartInterval !== null && req.chartInterval !== interval) {
            changeInterval(req.chartInterval)
          }
        }
      }
      setDock((d) => (d.open ? d : { ...d, open: true }))
      setResearchOpen(false)
      const id = testerRunSeq
      dispatchTester({ type: 'run-start', startedAt: Date.now() })
      void labClient.backtest(req.plan).then(
        (r) => dispatchTester({ type: 'run-done', id, report: r }),
        (err: unknown) =>
          dispatchTester({
            type: 'run-fail',
            id,
            text: req.planText,
            error: describeLabError(err),
          }),
      )
    },
    [
      testerRunning,
      testerRunSeq,
      layout,
      symbol,
      interval,
      selectSymbol,
      changeInterval,
      activateIndex,
      loadPlanIntoTester,
      labBlockedNotice,
    ],
  )

  const loadCandidatePlan = useCallback(
    (req: CandidateRunRequest) => {
      loadPlanIntoTester(req)
      setDock((d) => (d.open ? d : { ...d, open: true }))
      setResearchOpen(false)
    },
    [loadPlanIntoTester],
  )

  const closeResearch = useCallback(() => setResearchOpen(false), [])

  // While Research covers them, the chart, the tester dock and the sidebar leave the tab order
  // (`inert`). The dock is BacktestPanel's own root, so it is marked through the DOM.
  const dockOpen = dock.open
  useEffect(() => {
    if (!researchOpen || !dockOpen) return
    const dockEl = appRef.current?.querySelector<HTMLElement>(':scope > .tester') ?? null
    dockEl?.setAttribute('inert', '')
    return () => dockEl?.removeAttribute('inert')
  }, [researchOpen, dockOpen])

  // When Research closes (Esc, Back, a candidate click, the rail), focus would fall to <body>
  // with the unmounted view: hand it to the rail's Research button instead.
  const wasResearchOpen = useRef(false)
  useEffect(() => {
    const was = wasResearchOpen.current
    wasResearchOpen.current = researchOpen
    if (!was || researchOpen) return
    const active = document.activeElement
    if (active !== null && active !== document.body && active.isConnected) return
    appRef.current?.querySelector<HTMLElement>('.side-rail button[aria-label="Research"]')?.focus()
  }, [researchOpen])
  const toggleResearch = useCallback(() => {
    // Chart keys (Esc, Delete) must not act on a drawing hidden under the Research view.
    setTool('cursor')
    setSelection(null)
    setContextMenu(null)
    setResearchOpen((o) => !o)
  }, [])

  const resizeDock = useCallback((height: number) => {
    setDock((d) => ({ ...d, height: clampDockHeight(height, window.innerHeight) }))
  }, [])
  const closeDock = useCallback(() => setDock((d) => ({ ...d, open: false })), [])
  const toggleDock = useCallback(() => setDock((d) => ({ ...d, open: !d.open })), [])

  // The persisted symbol may not exist on this provider (e.g. SUIUSDT after switching to
  // Coinbase). Checked once, when the provider's symbol list first arrives, so it cannot loop.
  // Adjusted during render (React's "state from changed data" pattern), not in an effect.
  const listedSymbols = symbolList.symbols
  const [symbolsChecked, setSymbolsChecked] = useState(false)
  if (!symbolsChecked && listedSymbols !== null) {
    setSymbolsChecked(true)
    const listed = listedSymbols.map((s) => s.symbol)
    // Every panel, hidden ones included; the notice names the first replaced symbol.
    const swaps = [...new Set(layout.panels.map((p) => p.symbol))]
      .map((from) => ({ from, to: fallbackSymbol(from, listed, PROFILE.defaultSymbol) }))
      .filter((x): x is { from: string; to: string } => x.to !== null)
    if (swaps.length > 0) {
      setNotice(symbolFallbackNotice(swaps[0].from, swaps[0].to, EXCHANGE))
      swaps.forEach((x) => dispatchLayout({ type: 'replace-symbol', from: x.from, to: x.to }))
      setFocusRange(null)
      setTool('cursor')
      setSelection(null)
      setContextMenu(null)
    }
  }

  // ---- Screenshot ----

  const saveScreenshot = useCallback(() => {
    const api = chartApisRef.current.get(activeP.id) ?? null
    if (api === null) {
      setNotice('There is no chart to save yet. Try again once the candles have loaded.')
      return
    }
    let canvas: HTMLCanvasElement
    try {
      canvas = api.screenshot()
    } catch (err) {
      console.warn('Chart screenshot failed', err)
      setNotice(`Could not capture the chart: ${errorText(err, 'unknown error')}`)
      return
    }
    downloadCanvas(canvas, screenshotFilename(symbol, interval, Date.now() / 1000)).catch(
      (err: unknown) => {
        console.warn('Saving the chart image failed', err)
        setNotice(`Could not save the chart image: ${errorText(err, 'unknown error')}`)
      },
    )
  }, [activeP.id, symbol, interval])

  const toggleWatchlistItem = useCallback(
    (s: string) => (watchlist.has(s) ? watchlist.remove(s) : watchlist.add(s)),
    [watchlist],
  )

  // ---- Drawings ----

  // The drawing tool is shared: it acts on the panel where the pointer interaction happens.
  // Selection and the context menu remember their panel; Delete reaches the active panel only.
  const resetTool = useCallback(() => setTool('cursor'), [])
  const openContextMenu = useCallback((panelId: string, request: ChartContextMenuRequest) => {
    keySeq.current += 1
    setContextMenu({ key: keySeq.current, panelId, request })
  }, [])
  const closeContextMenu = useCallback(() => setContextMenu(null), [])
  const selectDrawing = useCallback(
    (panelId: string, id: string | null) =>
      setSelection((cur) =>
        id !== null ? { panelId, id } : cur?.panelId === panelId ? null : cur,
      ),
    [],
  )

  /** Remove a drawing of `sym` (and drop the selection when it was that drawing). */
  const deleteDrawing = useCallback(
    (sym: string, id: string) => {
      drawingsOf(sym).remove(id)
      setSelection((cur) => (cur?.id === id ? null : cur))
    },
    [drawingsOf],
  )
  const clearDrawings = useCallback(() => {
    drawings.clear()
    setSelection(null)
  }, [drawings])

  const changeSelectedStyle = useCallback(
    (sym: string, id: string, patch: DrawingPatch) => {
      // A rejected patch changes nothing, so it does not become the default style either.
      if (isRejected(drawingsOf(sym).update(id, patch))) return
      if (patch.style) {
        setDrawingStyle(patch.style)
        saveDrawingStyle(patch.style)
      }
    },
    [drawingsOf],
  )

  const addHorizontalLine = useCallback(
    (panelId: string, price: number, time: number | null) => {
      const target = panelById(panelId)
      if (target === undefined) return
      const drawing: Drawing | null = buildHLineDrawing({
        id: crypto.randomUUID(),
        symbol: target.symbol,
        price,
        time,
        lastCandleTime: lastBarTimeOf(liveOf(panelId)),
        style: drawingStyle,
        nowSeconds: Date.now() / 1000,
      })
      if (drawing === null) return
      // A rejected drawing (e.g. the per-symbol limit) never exists, so it is not selected.
      if (isRejected(drawingsOf(target.symbol).add(drawing))) return
      setSelection({ panelId, id: drawing.id })
    },
    [panelById, liveOf, drawingStyle, drawingsOf],
  )

  // ---- Alerts ----

  const openAlertDialog = useCallback(
    (initialPrice: number, editing?: Alert, forSymbol?: string) => {
      keySeq.current += 1
      setAlertDialog({
        key: keySeq.current,
        symbol: editing?.symbol ?? forSymbol ?? symbol,
        initialPrice,
        editing,
      })
    },
    [symbol],
  )
  const openAlertAtLast = useCallback(
    () => openAlertDialog(lastClose ?? Number.NaN),
    [openAlertDialog, lastClose],
  )
  const closeAlertDialog = useCallback(() => setAlertDialog(null), [])

  /** Fire-and-forget alert calls: a failure shows a short notice instead of going unhandled. */
  const runAlertAction = useCallback((p: Promise<unknown>, what: string) => {
    p.catch((err: unknown) => {
      console.warn(`${what} failed`, err)
      setNotice(`${what} failed: ${errorText(err, 'the local server did not respond')}`)
    })
  }, [])

  const submitAlert = useCallback(
    async (values: AlertDialogValues) => {
      if (alertDialog === null) return
      const { editing } = alertDialog
      if (editing) await alerts.update(editing.id, values)
      else await alerts.create({ symbol: alertDialog.symbol, ...values })
    },
    [alertDialog, alerts],
  )

  const requestPermission = useCallback(() => {
    void requestNotificationPermission().then(setPermission)
  }, [])

  const openToast = useCallback(
    (event: AlertEvent) => {
      selectSymbol(event.symbol)
      setPanel('alerts')
    },
    [selectSymbol],
  )
  const dismiss = useCallback((key: string) => setToasts((ts) => dismissToast(ts, key)), [])

  // ---- Keyboard ----

  // Ctrl/Cmd+K opens search; typing a letter or digit outside an input opens it prefilled.
  // "/" outside an input opens the indicators picker (TradingView shortcut).
  // Alt+H/J/T/F/R pick a drawing tool and Alt+A opens "Create alert". Escape and Delete belong
  // to the chart (cancel tool / delete the selected drawing).
  const dialogOpen =
    search !== null ||
    indicatorMenuOpen ||
    settingsConfig !== undefined ||
    alertDialog !== null ||
    contextMenu !== null ||
    settingsOpen ||
    shortcutsKey !== null ||
    seasonalsKey !== null ||
    // The Research view takes the keyboard (Esc closes it); chart shortcuts wait.
    researchOpen
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (dialogOpen || e.isComposing || e.defaultPrevented) return
      // Bar replay: Shift+→ / Shift+← step, Shift+↓ play/pause, Esc cancels a pick or exits.
      // A drawing tool or selected drawing claims Esc first (the chart cancels / deselects).
      const escClaimed = e.key === 'Escape' && (tool !== 'cursor' || selection !== null)
      if (replaySession !== null && !escClaimed && !isTypingTarget(e.target)) {
        const action = replayKeyAction(e, replayStatus)
        if (action !== null) {
          e.preventDefault()
          if (action === 'step-forward') replayStepForward()
          else if (action === 'step-back') replayStepBack()
          else if (action === 'toggle-play') replayTogglePlay()
          else if (action === 'cancel-pick') replayCancelPicking()
          else closeReplay()
          return
        }
        // The bar's idle state (no replay picked yet): Esc closes it.
        if (
          e.key === 'Escape' &&
          replayStatus === 'off' &&
          !e.repeat &&
          !e.shiftKey &&
          !e.ctrlKey &&
          !e.metaKey &&
          !e.altKey
        ) {
          e.preventDefault()
          closeReplay()
          return
        }
      }
      const command = isTypingTarget(e.target) ? null : shortcutCommand(e)
      if (command === 'add-alert') {
        e.preventDefault()
        if (alertsAvailable) openAlertAtLast()
        else if (alertDisabledReason !== null) setNotice(alertDisabledReason)
        return
      }
      if (command !== null && command !== 'cursor') {
        e.preventDefault()
        setTool(command)
        return
      }
      if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        openSearch()
        return
      }
      // Alt+1…7 / [ ] timeframes, Alt+S screenshot, Alt+Shift+L theme, ? help (state/shortcuts).
      const app = isTypingTarget(e.target)
        ? null
        : appShortcut(e, interval, { supports: supportsTimeframe })
      if (app !== null) {
        e.preventDefault()
        // A held key: '[' / ']' keep stepping; screenshot, theme and help fire once per press.
        if (e.repeat && !shortcutRepeats(app)) return
        if (app.kind === 'interval') {
          if (app.interval !== interval) changeInterval(app.interval)
        } else if (app.kind === 'screenshot') saveScreenshot()
        else if (app.kind === 'theme') toggleTheme()
        else openShortcuts()
        return
      }
      if (e.metaKey || e.ctrlKey || e.altKey) return
      if (isTypingTarget(e.target)) return
      if (e.key === '/') {
        e.preventDefault()
        openIndicatorMenu()
        return
      }
      if (!/^[a-z0-9]$/i.test(e.key)) return
      e.preventDefault()
      openSearch(e.key.toUpperCase())
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [
    dialogOpen,
    openSearch,
    openIndicatorMenu,
    alertsAvailable,
    alertDisabledReason,
    openAlertAtLast,
    interval,
    changeInterval,
    saveScreenshot,
    toggleTheme,
    openShortcuts,
    supportsTimeframe,
    tool,
    selection,
    replaySession,
    replayStatus,
    replayStepForward,
    replayStepBack,
    replayTogglePlay,
    replayCancelPicking,
    closeReplay,
  ])

  const contextPanel = contextMenu !== null ? panelById(contextMenu.panelId) : undefined
  // The dialog's market may be on another panel than the active one (or on none).
  const alertPanel =
    alertDialog !== null ? pickPanel(layout, (p) => p.symbol === alertDialog.symbol) : -1
  const alertDialogLastPrice =
    alertPanel >= 0 ? lastCloseOf(liveOf(layout.panels[alertPanel].id)) : null

  // ---- Elements handed to the memoised ChartPanels: stable between ticks of other panels ----
  const historyChip = useMemo(
    () =>
      historyActive && report !== null ? (
        // Visual only: the Strategy Tester's live region announces start/end/failure.
        <div className="history-chip">
          <span className="history-hint__spinner" aria-hidden="true" />
          <span className="history-chip__text" aria-hidden="true">
            {historyLoadingText(report.window, onChartCount, report.trades.length)}
          </span>
          <button
            type="button"
            className="history-chip__stop"
            aria-label="Stop loading backtest history"
            onClick={stopBacktestHistory}
          >
            Stop
          </button>
        </div>
      ) : undefined,
    [historyActive, report, onChartCount, stopBacktestHistory],
  )
  // useReplay returns a fresh object per render; rebuilt from its parts so the bar (and the
  // replay panel) only re-render when the replay itself changes.
  const {
    state: replayState,
    visible: replayVisibleBars,
    currentBar: replayCurrentBar,
    atEnd: replayAtEnd,
    barsAhead: replayBarsAhead,
    revision: replayRevision,
    startBarsBack: replayStartBarsBack,
    play: replayPlay,
    pause: replayPause,
    setSpeed: replaySetSpeed,
  } = replay
  const replayBarEl = useMemo(
    () => (
      <ReplayBar
        replay={{
          state: replayState,
          visible: replayVisibleBars,
          currentBar: replayCurrentBar,
          atEnd: replayAtEnd,
          barsAhead: replayBarsAhead,
          revision: replayRevision,
          startPicking: startReplayPicking,
          cancelPicking: replayCancelPicking,
          pickAt: replayPickAt,
          startBarsBack: replayStartBarsBack,
          stepForward: replayStepForward,
          stepBack: replayStepBack,
          play: replayPlay,
          pause: replayPause,
          togglePlay: replayTogglePlay,
          setSpeed: replaySetSpeed,
          exit: exitReplay,
        }}
        timeZone={timezone}
        onClose={closeReplay}
      />
    ),
    [
      replayState,
      replayVisibleBars,
      replayCurrentBar,
      replayAtEnd,
      replayBarsAhead,
      replayRevision,
      startReplayPicking,
      replayCancelPicking,
      replayPickAt,
      replayStartBarsBack,
      replayStepForward,
      replayStepBack,
      replayPlay,
      replayPause,
      replayTogglePlay,
      replaySetSpeed,
      exitReplay,
      timezone,
      closeReplay,
    ],
  )
  // The selected drawing's style bar, on the panel the selection belongs to.
  const stylePanelSymbol = selectedDrawing !== undefined ? selectedPanel?.symbol : undefined
  const styleBarEl = useMemo(
    () =>
      selectedDrawing !== undefined && stylePanelSymbol !== undefined ? (
        <DrawingStyleBar
          key={selectedDrawing.id}
          drawing={selectedDrawing}
          onChange={(patch) => changeSelectedStyle(stylePanelSymbol, selectedDrawing.id, patch)}
          onDelete={() => deleteDrawing(stylePanelSymbol, selectedDrawing.id)}
        />
      ) : undefined,
    [selectedDrawing, stylePanelSymbol, changeSelectedStyle, deleteDrawing],
  )
  // Seconds charts need the venue (Binance 1s klines or none): until the server reports it,
  // they wait in a loading state instead of failing on a provider without seconds.
  const venuePending = venue === null && provider.getVenue !== undefined

  const sidebarOpen = panel !== null
  const dockHeight = clampDockHeight(dock.height, window.innerHeight)
  const appClass = `app${sidebarOpen ? ' app--with-sidebar' : ''}${
    dock.open ? ' app--with-tester' : ''
  }`
  const appStyle = dock.open ? ({ '--tester-h': `${dockHeight}px` } as CSSProperties) : undefined

  return (
    <div ref={appRef} className={appClass} style={appStyle}>
      <Header
        symbol={symbol}
        description={description}
        interval={interval}
        onIntervalChange={changeInterval}
        supportsTimeframe={supportsTimeframe}
        chartType={chartType}
        onChartTypeChange={changeChartType}
        onOpenIndicators={openIndicatorMenu}
        onOpenSearch={() => openSearch()}
        onOpenAlert={openAlertAtLast}
        alertDisabledReason={alertDisabledReason}
        replayOn={replaySession !== null}
        onToggleReplay={toggleReplay}
        replayDisabledReason={replayBlockedReason}
        regimeOn={regimeOn}
        onToggleRegime={toggleRegime}
        derivativesOn={derivativesOn}
        onToggleDerivatives={toggleDerivatives}
        watchlistOpen={panel === 'watchlist'}
        onToggleWatchlist={() => setPanel((p) => (p === 'watchlist' ? null : 'watchlist'))}
        source={SOURCE}
        quote={quote}
        status={live.status}
        loading={load.kind === 'loading' || live.refreshing}
        onRefresh={live.reload}
        refreshDisabled={live.retryBlocked}
        onOpenSeasonals={openSeasonals}
        onScreenshot={saveScreenshot}
        theme={theme}
        onToggleTheme={toggleTheme}
        onOpenShortcuts={openShortcuts}
        onOpenSettings={openSettingsDialog}
        isMac={IS_MAC}
        layoutControl={
          <LayoutMenu
            kind={layout.kind}
            onKindChange={changeLayout}
            syncSymbol={layout.syncSymbol}
            onSyncSymbolChange={setSyncSymbol}
            syncCrosshair={layout.syncCrosshair}
            onSyncCrosshairChange={setSyncCrosshair}
          />
        }
      />
      <DrawingToolbar
        tool={tool}
        onToolChange={setTool}
        symbol={symbol}
        drawingCount={drawings.items.length}
        onClearAll={clearDrawings}
      />
      <main className="workspace" inert={researchOpen}>
        <div className={`layout-grid layout-grid--${layout.kind}`}>
          {visible.map((cell, i) => {
            const isBt = i === btIndex
            const panelDrawings = drawingsOf(cell.symbol)
            const isReplay = cell.id === replayPanelId
            // Replayed history never shows the backtest: its markers and equity run past the cursor.
            const cellReplaying = isReplay && replayShown
            return (
              <ChartPanel
                key={cell.id}
                panel={cell}
                // Seconds go to the venue-aware provider; every other timeframe keeps one stable
                // source, so learning the server's venue never restarts those streams.
                provider={isSecondsTimeframe(cell.interval) ? timeframes : TIMEFRAMES}
                venuePending={venuePending && isSecondsTimeframe(cell.interval)}
                limit={LIMIT}
                errorSource={ERROR_SOURCE}
                source={SOURCE}
                exchange={EXCHANGE}
                active={i === activeIndex}
                multi={multi}
                onActivate={activatePanel}
                onLive={onPanelLive}
                onApi={onChartApi}
                appearance={appearance}
                indicators={indicators.items}
                onIndicatorToggleVisible={indicators.toggleVisible}
                onIndicatorRemove={indicators.remove}
                onIndicatorSettings={setSettingsUid}
                drawings={panelDrawings}
                tool={tool}
                onToolDone={resetTool}
                drawingStyle={drawingStyle}
                selectedDrawingId={selection?.panelId === cell.id ? selection.id : null}
                onSelectDrawing={selectDrawing}
                onContextMenu={openContextMenu}
                alerts={alerts.alerts}
                backtest={isBt && backtestDrawn && !cellReplaying ? chartData : null}
                focusRange={
                  isBt &&
                  !cellReplaying &&
                  focusRange?.symbol === cell.symbol &&
                  focusRange.interval === cell.interval
                    ? focusRange
                    : null
                }
                onFocusDone={focusDone}
                keyboardBlocked={dialogOpen}
                crosshairSync={multi && layout.syncCrosshair ? crosshairSync : null}
                candlesOverride={isReplay ? replay.visible : null}
                replaySession={isReplay}
                onTimeClick={isReplay && replayStatus === 'picking' ? pickReplayBar : undefined}
                regimeEnabled={regimeOn}
                regimeClient={labClient}
                derivativesEnabled={derivativesOn}
                derivativesClient={labClient}
                derivativesBlocked={derivativesBlocked}
                statusChip={isBt ? historyChip : undefined}
                replayBar={isReplay ? replayBarEl : undefined}
              >
                {selection?.panelId === cell.id ? styleBarEl : undefined}
              </ChartPanel>
            )
          })}
        </div>
      </main>
      {sidebarOpen && (
        <div className="sidebar" inert={researchOpen}>
          {panel === 'watchlist' ? (
            <Watchlist
              provider={provider}
              items={watchlist.items}
              activeSymbol={symbol}
              symbolInfo={byName}
              onSelect={selectSymbol}
              onRemove={watchlist.remove}
              onMove={watchlist.move}
              onAdd={() => openSearch()}
            />
          ) : panel === 'paper' ? (
            <PaperTradingPanel
              symbol={symbol}
              price={paperPrice}
              time={paperTime}
              scope={activePaperReplay ? 'replay' : 'live'}
              paper={activePaperReplay ? paperReplay : paperLive}
              marks={activePaperReplay ? undefined : paperMarksForActive}
              pausedReason={paperPaused}
              quoteAsset={info?.quoteAsset ?? null}
              offlineReason={paperOffline}
              formatPrice={formatSymbolPrice}
              onSelectSymbol={selectSymbol}
            />
          ) : (
            <AlertsPanel
              alerts={alerts.alerts}
              events={alerts.events}
              status={alerts.status}
              activeSymbol={symbol}
              notificationPermission={permission}
              onRequestPermission={requestPermission}
              onCreate={openAlertAtLast}
              onSelectSymbol={selectSymbol}
              onEdit={(a) => openAlertDialog(a.price, a)}
              onToggleActive={(a) =>
                runAlertAction(
                  alerts.update(a.id, { active: !a.active }),
                  a.active ? 'Pausing the alert' : 'Resuming the alert',
                )
              }
              onDelete={(a) => runAlertAction(alerts.remove(a.id), 'Deleting the alert')}
              onClearLog={() => runAlertAction(alerts.clearHistory(), 'Clearing the alert log')}
              formatPrice={formatSymbolPrice}
            />
          )}
        </div>
      )}
      {dock.open && (
        <BacktestPanel
          client={labClient}
          state={tester}
          dispatch={dispatchTester}
          chartSymbol={btSymbol}
          chartInterval={btInterval}
          link={testerLink}
          onShowOnChart={showBacktestOnChart}
          onClearFromChart={clearBacktestFromChart}
          onLoadHistory={loadBacktestHistory}
          onStopHistory={stopBacktestHistory}
          onFocusRange={focusChartRange}
          onClose={closeDock}
          height={dockHeight}
          onResize={resizeDock}
          formatPrice={formatSymbolPrice}
          chartVenue={venue}
        />
      )}
      {researchOpen && (
        <ErrorBoundary
          onError={onResearchError}
          fallback={({ error, reset }) => (
            <ResearchFallback
              error={error}
              onReload={() => {
                // Reopen on Runs: the saved tab may be the one that threw.
                resetResearchTab()
                reset()
              }}
              onClose={closeResearch}
            />
          )}
        >
          <ResearchPage
            client={researchClient}
            labClient={labClient}
            onClose={closeResearch}
            onShowCandidate={showCandidate}
            onLoadPlan={loadCandidatePlan}
            testerBusy={testerRunning}
          />
        </ErrorBoundary>
      )}
      <SidebarRail
        active={panel}
        onSelect={setPanel}
        alertBadge={alerts.unseen}
        testerOpen={dock.open}
        onToggleTester={toggleDock}
        researchOpen={researchOpen}
        onToggleResearch={toggleResearch}
      />
      <StatusBar
        barCount={bars ? bars.length : null}
        status={live.status}
        lastUpdate={live.lastUpdate !== null ? formatClock(live.lastUpdate, timezone) : null}
        timezoneName={timezoneLabel(timezone)}
        refreshError={load.kind === 'ready' ? live.refreshError : null}
      />
      {contextMenu !== null && contextPanel !== undefined && (
        <ChartContextMenu
          key={contextMenu.key}
          request={contextMenu.request}
          symbol={contextPanel.symbol}
          formatPrice={(price) => formatSymbolPrice(contextPanel.symbol, price)}
          alertsAvailable={alertsAvailable}
          onAddAlert={(price) => openAlertDialog(price, undefined, contextPanel.symbol)}
          onAddHorizontalLine={(price) =>
            addHorizontalLine(contextPanel.id, price, contextMenu.request.time)
          }
          onDeleteDrawing={(id) => deleteDrawing(contextPanel.symbol, id)}
          onAnchoredVwap={(time) => addIndicator('avwap', time)}
          onVolumeProfile={(time) => addIndicator('vprofile', time)}
          onClose={closeContextMenu}
        />
      )}
      {alertDialog !== null && (
        <AlertDialog
          key={alertDialog.key}
          symbol={alertDialog.symbol}
          initialPrice={alertDialog.initialPrice}
          lastPrice={alertDialogLastPrice}
          editing={alertDialog.editing}
          formatPrice={(price) => formatSymbolPrice(alertDialog.symbol, price)}
          onSubmit={submitAlert}
          onClose={closeAlertDialog}
        />
      )}
      <AlertToasts toasts={toasts} onDismiss={dismiss} onOpen={openToast} />
      {notice !== null && (
        <div className="app-notice" role="status">
          <span className="app-notice__text">{notice}</span>
          <button
            type="button"
            className="app-notice__close"
            aria-label="Dismiss"
            onClick={() => setNotice(null)}
          >
            ×
          </button>
        </div>
      )}
      {restoreValid && planRestore !== null && (
        <div className="app-notice app-notice--restore" role="status">
          <span className="app-notice__text">
            A research candidate&apos;s plan replaced your Strategy Tester plan.
          </span>
          <button type="button" className="app-notice__action" onClick={restorePreviousPlan}>
            Restore previous plan
          </button>
          <button
            type="button"
            className="app-notice__close"
            aria-label="Dismiss (keep the candidate's plan)"
            title="Keep the candidate's plan"
            onClick={dismissPlanRestore}
          >
            ×
          </button>
        </div>
      )}
      {search !== null && (
        <SymbolSearch
          initialQuery={search.initialQuery}
          symbols={symbolList.symbols}
          error={symbolList.error}
          onRetry={symbolList.retry}
          activeSymbol={symbol}
          source={SOURCE}
          isInWatchlist={watchlist.has}
          onToggleWatchlist={toggleWatchlistItem}
          onSelect={selectSymbol}
          onClose={closeSearch}
        />
      )}
      {indicatorMenuOpen && (
        <IndicatorMenu items={indicators.items} onAdd={addIndicator} onClose={closeIndicatorMenu} />
      )}
      {settingsOpen && (
        <SettingsDialog
          settings={settings}
          onChange={changeSettings}
          onClose={closeSettingsDialog}
          providerNeedsReload={needsReload}
        />
      )}
      {shortcutsKey !== null && (
        <ShortcutsDialog key={shortcutsKey} isMac={IS_MAC} onClose={closeShortcuts} />
      )}
      {seasonalsKey !== null && (
        <SeasonalsView
          key={seasonalsKey}
          symbol={symbol}
          provider={provider}
          venueName={EXCHANGE}
          onClose={closeSeasonals}
        />
      )}
      {settingsConfig && settingsDef && (
        <IndicatorSettings
          key={settingsConfig.uid}
          def={settingsDef}
          config={settingsConfig}
          onApply={(patch) => indicators.update(settingsConfig.uid, patch)}
          onClose={closeSettings}
          secondsCharts={visible.some((p) => isSecondsTimeframe(p.interval))}
        />
      )}
    </div>
  )
}

export default App
