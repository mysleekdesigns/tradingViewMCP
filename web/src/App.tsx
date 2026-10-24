import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type CSSProperties,
} from 'react'
import { buildChartData, tradesOnChart, tradesZoomRange } from './chart/backtestOverlay'
import { INTERVAL_SECONDS } from './chart/drawings/timeIndex'
import { Chart } from './chart/Chart'
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
  MAX_HISTORY_BARS,
  reportMatchesChart,
  saveDockPrefs,
  savePlanText,
  testerReducer,
  type DockPrefs,
} from './components/backtestView'
import type { FocusPlan } from './chart/backtestOverlay'
import { ChartContextMenu } from './components/ChartContextMenu'
import { DrawingStyleBar } from './components/DrawingStyleBar'
import { DrawingToolbar } from './components/DrawingToolbar'
import { shortcutCommand } from './components/drawingTools'
import { Header } from './components/Header'
import { IndicatorMenu } from './components/IndicatorMenu'
import { IndicatorSettings } from './components/IndicatorSettings'
import { summarizeQuote, formatUtcClock } from './components/quote'
import { SidebarRail, type SidebarPanel } from './components/SidebarRail'
import { StatusBar } from './components/StatusBar'
import { NoTradesView } from './components/NoTradesView'
import { ErrorView, LoadingView } from './components/StatusViews'
import { SymbolSearch } from './components/SymbolSearch'
import { Watchlist } from './components/Watchlist'
import type { Alert, AlertEvent, NotifyPermission } from './data/alertTypes'
import { createAlertsClient } from './data/alertsApi'
import { BinanceProvider } from './data/binance'
import { createDrawingsClient } from './data/drawingsApi'
import { createLabClient } from './data/labApi'
import type { MarketDataProvider } from './data/provider'
import { ServerProvider } from './data/server'
import { DIRECT_VENUE, VENUE_NAME, venueLabels, type VenueInfo } from './data/venue'
import { getIndicatorDef } from './indicators/registry'
import { useAlerts } from './state/alerts'
import {
  alertLinesFor,
  buildHLineDrawing,
  dismissToast,
  formatPriceByTick,
  loadDrawingStyle,
  loadSidebarPanel,
  pushToast,
  saveDrawingStyle,
  saveSidebarPanel,
} from './state/chartTools'
import { useDrawings } from './state/drawings'
import { useIndicators } from './state/indicators'
import {
  notificationPermission,
  requestNotificationPermission,
  showBrowserNotification,
} from './state/notify'
import { loadPrefs, savePrefs } from './state/prefs'
import { atHistoryLimit } from './state/liveCandles'
import { useLiveCandles } from './state/useLiveCandles'
import { useSymbols } from './state/useSymbols'
import { useWatchlist } from './state/watchlist'
import type { ChartType, Interval } from './types'

// Default: the local server (cache + fan-out). `VITE_DATA_SOURCE=direct` talks to Binance's
// market-data-only hosts (data-api / data-stream.binance.vision) from the browser.
const DIRECT = import.meta.env.VITE_DATA_SOURCE === 'direct'
const provider: MarketDataProvider = DIRECT ? new BinanceProvider() : new ServerProvider()
// Module-level so the hooks see stable clients. Direct mode has no local server: drawings stay
// in localStorage and alerts are unavailable.
const drawingsClient = DIRECT ? null : createDrawingsClient()
const alertsClient = DIRECT ? null : createAlertsClient()
// The Python lab (backtests + ledger) is its own service behind the `/lab` proxy, in both modes.
const labClient = createLabClient()

/**
 * Source name inside chart error messages. Stable for the app's lifetime, so learning the
 * server's venue later does not restart the candle stream. Labels use `venueLabels` instead.
 */
const ERROR_SOURCE = DIRECT ? VENUE_NAME : 'the local server'
/** Retry interval while the server has not reported its venue yet. */
const VENUE_RETRY_MS = 5000
const LIMIT = 500
/** How long a small error notice (e.g. a failed alert delete) stays up. */
const NOTICE_MS = 6000

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

interface SearchState {
  initialQuery: string
}

interface ContextMenuState {
  /** Fresh per request so the menu re-measures and refocuses. */
  key: number
  request: ChartContextMenuRequest
}

/** A time range the chart should scroll to; a fresh key per request. */
interface FocusRequest {
  key: number
  from: number
  to: number
  /** Only applied to the chart it was made for (a symbol switch remounts the chart). */
  symbol: string
  interval: Interval
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
  const [initial] = useState(loadPrefs)
  const [symbol, setSymbol] = useState(initial.symbol)
  const [interval, setTimeframe] = useState<Interval>(initial.interval)
  const [chartType, setChartType] = useState<ChartType>(initial.chartType)
  const [search, setSearch] = useState<SearchState | null>(null)
  const [panel, setPanel] = useState<SidebarPanel | null>(loadSidebarPanel)
  const [indicatorMenuOpen, setIndicatorMenuOpen] = useState(false)
  /** Instance whose settings dialog is open. */
  const [settingsUid, setSettingsUid] = useState<string | null>(null)

  const [tool, setTool] = useState<ToolMode>('cursor')
  const [selectedDrawingId, setSelectedDrawingId] = useState<string | null>(null)
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

  // Strategy Tester: dock layout, panel state (kept here so a run survives closing the dock).
  const [dock, setDock] = useState<DockPrefs>(loadDockPrefs)
  const [tester, dispatchTester] = useReducer(testerReducer, null, () =>
    initTesterState(loadPlanText()),
  )
  const [historyLoad, dispatchHistory] = useReducer(historyLoadReducer, initialHistoryLoad)
  const historyTarget = historyLoad.target
  const [focusRange, setFocusRange] = useState<FocusRequest | null>(null)

  useEffect(() => {
    savePrefs({ symbol, interval, chartType })
  }, [symbol, interval, chartType])

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
  const [venue, setVenue] = useState<VenueInfo | null>(DIRECT ? DIRECT_VENUE : null)
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

  const live = useLiveCandles(provider, symbol, interval, LIMIT, ERROR_SOURCE, MAX_HISTORY_BARS)
  const { load } = live
  const symbolList = useSymbols(provider)
  const watchlist = useWatchlist()
  const indicators = useIndicators()
  const drawings = useDrawings(symbol, drawingsClient)

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

  // Every loaded bar, traded or not: the pagination cursor, history planning, and the quote.
  const candles = load.kind === 'ready' ? load.candles : null
  // What the chart plots: bars without trades are left out, as on TradingView.
  const bars = load.kind === 'ready' ? load.bars : null
  // The last candle may still be forming; it is a display/default value only. A bar without
  // trades carries the previous close, so the raw last close is the last traded price.
  const lastClose =
    candles !== null && candles.length > 0 ? candles[candles.length - 1].close : null
  const lastCandleTime = bars !== null && bars.length > 0 ? bars[bars.length - 1].time : null
  const quote = useMemo(
    () => (candles ? summarizeQuote(candles, interval) : null),
    [candles, interval],
  )
  // ---- Strategy Tester ↔ chart ----

  const report = tester.report
  const reportOnMarket = report !== null && reportMatchesChart(report, symbol, interval)
  const chartData = useMemo(() => (report ? buildChartData(report) : null), [report])
  const backtestDrawn = tester.shownOnChart && reportOnMarket
  // Raw first bar: a page of bars without trades still moves the history back.
  const firstBarTime = candles !== null && candles.length > 0 ? candles[0].time : null
  // Live ticks replace `candles` but rarely change its length; plans depend on the length only.
  const candleCount = candles?.length ?? null
  const history = useMemo(
    () =>
      backtestDrawn && report && candleCount !== null
        ? historyPlan(
            firstBarTime,
            report.window,
            interval,
            Math.max(0, MAX_HISTORY_BARS - candleCount),
          )
        : null,
    [backtestDrawn, report, candleCount, firstBarTime, interval],
  )
  // Active only for the market it was started on, until the window start is loaded.
  const historyActive =
    historyTarget !== null &&
    backtestDrawn &&
    historyTarget.symbol === symbol &&
    historyTarget.interval === interval &&
    firstBarTime !== null &&
    firstBarTime > historyTarget.target &&
    !live.historyExhausted
  const historyFailed = backtestDrawn && historyLoadFailed(historyLoad, symbol, interval)
  const { loadOlder, loadingOlder, retryBlocked } = live

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
  // this history, so it may go past the bar limit that caps scrolling back (historyPlan
  // already bounds the target).
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
              interval,
            ).bars,
          }
        : history,
    [historyTargetTime, history, report, firstBarTime, interval],
  )
  // The chart draws trade markers on plotted bars only, so the Strategy Tester counts and
  // links trades from the first PLOTTED bar (history planning above stays on raw bars).
  const plottedFirstTime = bars !== null && bars.length > 0 ? bars[0].time : null
  // Runs over every trade, so only when the report or the first plotted bar changes.
  const onChartCount = useMemo(
    () => (report && backtestDrawn ? tradesOnChart(report.trades, plottedFirstTime) : 0),
    [report, backtestDrawn, plottedFirstTime],
  )
  const historyExhausted = live.historyExhausted
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
  const formatCurrentPrice = useCallback(
    (price: number) => formatSymbolPrice(symbol, price),
    [formatSymbolPrice, symbol],
  )

  const alertsAvailable = alerts.status === 'ready'
  const alertDisabledReason = alertsAvailable
    ? null
    : alerts.status === 'loading'
      ? 'Connecting to the local server…'
      : DIRECT
        ? 'Alerts need the local server (not available in direct mode)'
        : 'Alerts need the local server, which cannot be reached'

  const activeAlertLines = useMemo(
    () => alertLinesFor(alerts.alerts, symbol),
    [alerts.alerts, symbol],
  )

  const selectedDrawing =
    selectedDrawingId !== null ? drawings.items.find((d) => d.id === selectedDrawingId) : undefined

  const openSearch = useCallback((initialQuery = '') => setSearch({ initialQuery }), [])
  const openIndicatorMenu = useCallback(() => setIndicatorMenuOpen(true), [])
  const closeIndicatorMenu = useCallback(() => setIndicatorMenuOpen(false), [])
  const closeSettings = useCallback(() => setSettingsUid(null), [])
  const closeSearch = useCallback(() => setSearch(null), [])

  const selectSymbol = useCallback((next: string) => {
    setSymbol(next)
    // A pending scroll request belongs to the old chart.
    setFocusRange(null)
    setSearch(null)
    // Drawing state belongs to the old symbol's chart (this also hides the style bar).
    setTool('cursor')
    setSelectedDrawingId(null)
    setContextMenu(null)
  }, [])

  const changeInterval = useCallback((next: Interval) => {
    setTimeframe(next)
    setFocusRange(null)
  }, [])

  const showBacktestOnChart = useCallback(() => {
    if (report === null) return
    if (report.symbol !== symbol) selectSymbol(report.symbol)
    if (report.interval !== interval && isChartInterval(report.interval)) {
      changeInterval(report.interval)
    }
    dispatchTester({ type: 'show-on-chart', shown: true })
  }, [report, symbol, interval, selectSymbol, changeInterval])

  const clearBacktestFromChart = useCallback(() => {
    dispatchTester({ type: 'show-on-chart', shown: false })
    dispatchHistory({ type: 'stop' })
  }, [])

  const loadBacktestHistory = useCallback(() => {
    // An unreachable window lies beyond the chart's bar limit: fetching would never reach it.
    if (history === null || !history.needed || history.unreachable) return
    dispatchHistory({ type: 'start', target: { symbol, interval, target: history.target } })
  }, [history, symbol, interval])

  const stopBacktestHistory = useCallback(() => dispatchHistory({ type: 'stop' }), [])

  const focusChartRange = useCallback(
    (from: number, to: number) => {
      keySeq.current += 1
      setFocusRange({ key: keySeq.current, from, to, symbol, interval })
    },
    [symbol, interval],
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
    })
    autoHistoryRef.current = state
    if (command === 'start' && history !== null) {
      dispatchHistory({ type: 'start', target: { symbol, interval, target: history.target } })
    } else if (command === 'zoom' && report !== null) {
      const r = tradesZoomRange(report.trades, report.window, INTERVAL_SECONDS[interval])
      focusChartRange(r.from, r.to)
    }
  }, [
    autoKey,
    history,
    historyActive,
    historyFailed,
    historyExhausted,
    report,
    symbol,
    interval,
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

  const resizeDock = useCallback((height: number) => {
    setDock((d) => ({ ...d, height: clampDockHeight(height, window.innerHeight) }))
  }, [])
  const closeDock = useCallback(() => setDock((d) => ({ ...d, open: false })), [])
  const toggleDock = useCallback(() => setDock((d) => ({ ...d, open: !d.open })), [])

  const toggleWatchlistItem = useCallback(
    (s: string) => (watchlist.has(s) ? watchlist.remove(s) : watchlist.add(s)),
    [watchlist],
  )

  // ---- Drawings ----

  const resetTool = useCallback(() => setTool('cursor'), [])
  const openContextMenu = useCallback((request: ChartContextMenuRequest) => {
    keySeq.current += 1
    setContextMenu({ key: keySeq.current, request })
  }, [])
  const closeContextMenu = useCallback(() => setContextMenu(null), [])

  const { add: addDrawing, update: updateDrawing, remove: removeDrawing } = drawings
  const deleteDrawing = useCallback(
    (id: string) => {
      removeDrawing(id)
      setSelectedDrawingId((cur) => (cur === id ? null : cur))
    },
    [removeDrawing],
  )
  const clearDrawings = useCallback(() => {
    drawings.clear()
    setSelectedDrawingId(null)
  }, [drawings])

  const changeSelectedStyle = useCallback(
    (id: string, patch: DrawingPatch) => {
      // A rejected patch changes nothing, so it does not become the default style either.
      if (isRejected(updateDrawing(id, patch))) return
      if (patch.style) {
        setDrawingStyle(patch.style)
        saveDrawingStyle(patch.style)
      }
    },
    [updateDrawing],
  )

  const addHorizontalLine = useCallback(
    (price: number, time: number | null) => {
      const drawing: Drawing | null = buildHLineDrawing({
        id: crypto.randomUUID(),
        symbol,
        price,
        time,
        lastCandleTime,
        style: drawingStyle,
        nowSeconds: Date.now() / 1000,
      })
      if (drawing === null) return
      // A rejected drawing (e.g. the per-symbol limit) never exists, so it is not selected.
      if (isRejected(addDrawing(drawing))) return
      setSelectedDrawingId(drawing.id)
    },
    [symbol, lastCandleTime, drawingStyle, addDrawing],
  )

  // ---- Alerts ----

  const openAlertDialog = useCallback(
    (initialPrice: number, editing?: Alert) => {
      keySeq.current += 1
      setAlertDialog({
        key: keySeq.current,
        symbol: editing?.symbol ?? symbol,
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
    contextMenu !== null
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (dialogOpen || e.isComposing || e.defaultPrevented) return
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
  ])

  const sidebarOpen = panel !== null
  const dockHeight = clampDockHeight(dock.height, window.innerHeight)
  const appClass = `app${sidebarOpen ? ' app--with-sidebar' : ''}${
    dock.open ? ' app--with-tester' : ''
  }`
  const appStyle = dock.open ? ({ '--tester-h': `${dockHeight}px` } as CSSProperties) : undefined

  return (
    <div className={appClass} style={appStyle}>
      <Header
        symbol={symbol}
        description={description}
        interval={interval}
        onIntervalChange={changeInterval}
        chartType={chartType}
        onChartTypeChange={setChartType}
        onOpenIndicators={openIndicatorMenu}
        onOpenSearch={() => openSearch()}
        onOpenAlert={openAlertAtLast}
        alertDisabledReason={alertDisabledReason}
        watchlistOpen={panel === 'watchlist'}
        onToggleWatchlist={() => setPanel((p) => (p === 'watchlist' ? null : 'watchlist'))}
        source={SOURCE}
        quote={quote}
        status={live.status}
        loading={load.kind === 'loading' || live.refreshing}
        onRefresh={live.reload}
        refreshDisabled={live.retryBlocked}
      />
      <DrawingToolbar
        tool={tool}
        onToolChange={setTool}
        symbol={symbol}
        drawingCount={drawings.items.length}
        onClearAll={clearDrawings}
      />
      <main className="workspace">
        {load.kind === 'ready' && load.bars.length > 0 && (
          <Chart
            candles={load.bars}
            historyStart={firstBarTime}
            symbol={symbol}
            interval={interval}
            source={SOURCE}
            chartType={chartType}
            onNearLeftEdge={live.loadOlder}
            indicators={indicators.items}
            onIndicatorToggleVisible={indicators.toggleVisible}
            onIndicatorRemove={indicators.remove}
            onIndicatorSettings={setSettingsUid}
            drawings={drawings.items}
            tool={tool}
            selectedDrawingId={selectedDrawingId}
            onSelectDrawing={setSelectedDrawingId}
            onDrawingCreate={addDrawing}
            drawingStyle={drawingStyle}
            onToolDone={resetTool}
            onDrawingChange={updateDrawing}
            onDrawingDelete={removeDrawing}
            onContextMenu={openContextMenu}
            alertLines={activeAlertLines}
            backtest={backtestDrawn ? chartData : null}
            focusRange={
              focusRange?.symbol === symbol && focusRange.interval === interval ? focusRange : null
            }
            onFocusDone={focusDone}
          />
        )}
        {load.kind === 'ready' && load.bars.length === 0 && (
          <NoTradesView
            symbol={symbol}
            interval={interval}
            exchange={EXCHANGE}
            raw={load.candles}
            maxBars={MAX_HISTORY_BARS}
            loadingOlder={live.loadingOlder}
            historyExhausted={live.historyExhausted}
            atLimit={atHistoryLimit(load.candles.length, MAX_HISTORY_BARS)}
            retryBlocked={live.retryBlocked}
            olderCooldown={live.olderCooldown}
            offline={live.status === 'offline'}
            onLoadOlder={live.loadOlder}
          />
        )}
        {load.kind === 'ready' && load.bars.length > 0 && selectedDrawing && (
          <DrawingStyleBar
            key={selectedDrawing.id}
            drawing={selectedDrawing}
            onChange={(patch) => changeSelectedStyle(selectedDrawing.id, patch)}
            onDelete={() => deleteDrawing(selectedDrawing.id)}
          />
        )}
        {historyActive && report !== null ? (
          <div className="history-chip" role="status">
            <span className="history-hint__spinner" aria-hidden="true" />
            <span className="history-chip__text">
              {historyLoadingText(report.window, onChartCount, report.trades.length)}
            </span>
            <button type="button" className="history-chip__stop" onClick={stopBacktestHistory}>
              Stop
            </button>
          </div>
        ) : (
          load.kind === 'ready' &&
          load.bars.length > 0 &&
          live.loadingOlder && (
            <div className="history-hint" role="status">
              <span className="history-hint__spinner" aria-hidden="true" />
              Loading older bars…
            </div>
          )
        )}
        {load.kind === 'loading' && (
          <LoadingView label={`Loading ${symbol} ${interval} from ${SOURCE}…`} />
        )}
        {load.kind === 'error' && (
          <ErrorView
            title={load.title}
            message={load.message}
            onRetry={live.reload}
            retryDisabled={live.retryBlocked}
          />
        )}
      </main>
      {sidebarOpen && (
        <div className="sidebar">
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
          chartSymbol={symbol}
          chartInterval={interval}
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
      <SidebarRail
        active={panel}
        onSelect={setPanel}
        alertBadge={alerts.unseen}
        testerOpen={dock.open}
        onToggleTester={toggleDock}
      />
      <StatusBar
        barCount={bars ? bars.length : null}
        status={live.status}
        lastUpdate={live.lastUpdate !== null ? formatUtcClock(new Date(live.lastUpdate)) : null}
        refreshError={load.kind === 'ready' ? live.refreshError : null}
      />
      {contextMenu !== null && (
        <ChartContextMenu
          key={contextMenu.key}
          request={contextMenu.request}
          symbol={symbol}
          formatPrice={formatCurrentPrice}
          alertsAvailable={alertsAvailable}
          onAddAlert={(price) => openAlertDialog(price)}
          onAddHorizontalLine={(price) => addHorizontalLine(price, contextMenu.request.time)}
          onDeleteDrawing={deleteDrawing}
          onClose={closeContextMenu}
        />
      )}
      {alertDialog !== null && (
        <AlertDialog
          key={alertDialog.key}
          symbol={alertDialog.symbol}
          initialPrice={alertDialog.initialPrice}
          lastPrice={alertDialog.symbol === symbol ? lastClose : null}
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
        <IndicatorMenu
          items={indicators.items}
          onAdd={indicators.add}
          onClose={closeIndicatorMenu}
        />
      )}
      {settingsConfig && settingsDef && (
        <IndicatorSettings
          key={settingsConfig.uid}
          def={settingsDef}
          config={settingsConfig}
          onApply={(patch) => indicators.update(settingsConfig.uid, patch)}
          onClose={closeSettings}
        />
      )}
    </div>
  )
}

export default App
