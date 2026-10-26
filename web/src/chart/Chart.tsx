import {
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type CSSProperties,
  type ReactNode,
} from 'react'
import {
  AreaSeries,
  CandlestickSeries,
  createChart,
  createSeriesMarkers,
  createTextWatermark,
  HistogramSeries,
  LineSeries,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ITextWatermarkPluginApi,
  type LogicalRange,
  type MouseEventParams,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import '../components/Backtest.css'
import type { RegimePoint } from '../data/labTypes'
import { isSecondsTimeframe, timeframeLabel, timeframeSeconds } from '../data/timeframes'
import { getIndicatorDef } from '../indicators/registry'
import { chartAcceptsEscape, chartOwnsKeyTarget } from '../state/chartTools'
import { chartKey } from '../state/shortcuts'
import type { IndicatorConfig } from '../state/indicators'
import type { Candle, ChartType, Timeframe } from '../types'
import { mainSeriesData, mainSeriesTail, realPriceLine, usesRealPriceLine } from './chartTypes.ts'
import type { HeikinAshiBar } from './heikinAshi.ts'
import { IndicatorLegendRows, PaneLegends } from './IndicatorLegend'
import {
  BacktestOverlay,
  planFocus,
  type BacktestChartData,
  type FocusPlan,
  type OverlayChart,
} from './backtestOverlay'
import { IndicatorSeriesManager } from './indicatorSeries'
import { buildLegend, selectLegendIndex, type LegendValues } from './legend.ts'
import {
  DEFAULT_APPEARANCE,
  alertLineOptions,
  chartOptionsFor,
  chartThemeOptionsFor,
  fallbackFontFamily,
  fontFamily,
  handleColorsFor,
  mainSeriesOptionsFor,
  paletteFor,
  realPriceLineOptions,
  timeScaleOptionsFor,
  volumeOptions,
  volumeScaleMargins,
  watermarkOptions,
  webFonts,
  type ChartAppearance,
  type ChartPalette,
} from './theme'
import { bindCrosshairSync, type CrosshairSync } from './crosshairSync'
import { RegimeRibbon, regimeColorsFor } from './regimeRibbon'
import { composeScreenshot } from './screenshot'
import { formatDateTime, timezoneLabel } from './timeFormat'
import {
  clickedBarTime,
  isNearLeftEdge,
  isExpectedRange,
  newestBarOffscreen,
  nextAutoOlder,
  planSeriesSync,
  shiftLogicalRange,
  toVolumePoint,
  type AutoOlder,
} from './seriesSync'
import { toVolumeData } from './volume'
import { AlertLinesManager } from './drawings/alertLines'
import { ChartCoordinateAdapter } from './drawings/chartAdapter'
import { DrawingController, type PointerInput } from './drawings/controller'
import { xToSnappedTime } from './drawings/geometry'
import { DrawingsPrimitive } from './drawings/primitives'
import type {
  AlertLine,
  ChartContextMenuRequest,
  Drawing,
  DrawingPatch,
  DrawingStyle,
  ToolMode,
} from './drawings/types'

/** Volume bar colours of a palette. */
function volumePaletteOf(p: ChartPalette) {
  return { up: p.volumeUp, down: p.volumeDown }
}

/** What the app can ask of a mounted chart (see the `onApi` prop). */
export interface ChartApi {
  /**
   * The chart as one canvas: a header strip (symbol · interval · venue, last bar, capture time)
   * above `chart.takeScreenshot()` with drawings and primitives, without the crosshair.
   */
  screenshot(): HTMLCanvasElement
}

type MainSeries = ISeriesApi<'Candlestick' | 'Line' | 'Area'>

/** The overlay's view of the chart: equity lines in their own pane. */
function backtestChartAdapter(chart: IChartApi): OverlayChart<ISeriesApi<'Line'>> {
  return {
    addLine: (options, paneIndex) => chart.addSeries(LineSeries, options, paneIndex),
    removeSeries: (series) => chart.removeSeries(series),
    paneCount: () => chart.panes().length,
  }
}

/** Identity of the pane indicators (uid + type, in order); a change can rebuild their panes. */
function paneIndicatorKey(indicators: readonly IndicatorConfig[]): string {
  return indicators
    .filter((c) => getIndicatorDef(c.type)?.placement === 'pane')
    .map((c) => `${c.uid}:${c.type}`)
    .join('|')
}

function addMainSeries(chart: IChartApi, type: ChartType, palette: ChartPalette): MainSeries {
  switch (type) {
    case 'candles':
    case 'hollow':
    case 'heikin-ashi':
      return chart.addSeries(CandlestickSeries, mainSeriesOptionsFor(type, palette))
    case 'line':
      return chart.addSeries(LineSeries, mainSeriesOptionsFor(type, palette))
    case 'area':
      return chart.addSeries(AreaSeries, mainSeriesOptionsFor(type, palette))
  }
}

/** Recolour the main series in place (theme / candle colour change). */
function applyMainSeriesOptions(series: MainSeries, type: ChartType, palette: ChartPalette): void {
  // The series kind always matches `type` (it was created for it), so the options fit.
  ;(series as ISeriesApi<'Candlestick'>).applyOptions(
    mainSeriesOptionsFor(type, palette) as Parameters<ISeriesApi<'Candlestick'>['applyOptions']>[0],
  )
}

/** Move a real-close price line to the last candle (Heikin-Ashi mode only). */
function syncRealPriceLine(
  line: IPriceLine | null,
  candles: readonly Candle[],
  palette: ChartPalette,
): void {
  if (!line) return
  const { price, color, visible } = realPriceLine(candles, palette)
  line.applyOptions({
    price,
    color,
    axisLabelColor: color,
    lineVisible: visible,
    axisLabelVisible: visible,
  })
}

interface ChartProps {
  /**
   * The bars to plot: ascending, unique times, traded bars only (`plottableCandles`; bars
   * without trades are left out, as on TradingView). The last bar may still be forming and
   * changes on live ticks. Series, volume, indicators, legend, drawings, and the backtest
   * overlay all use these.
   */
  candles: readonly Candle[]
  /**
   * Open time of the oldest loaded bar including bars without trades (the pagination cursor).
   * After each page lands, the chart asks for the next one itself while the view is still at the
   * left edge, a few pages in a row (see `nextAutoOlder`); a user scroll starts a new run.
   */
  historyStart?: number | null
  symbol: string
  /** Native or aggregated timeframe; seconds timeframes show seconds on the time axis. */
  interval: Timeframe
  /** Data source shown in the legend, e.g. "Binance · local server". */
  source: string
  /** How the main series draws. Switching replaces the main series; volume stays. */
  chartType: ChartType
  /**
   * Fired when the visible range comes within LEFT_EDGE_THRESHOLD_BARS of the oldest loaded bar.
   * May fire repeatedly while the user stays there; the caller dedupes.
   */
  onNearLeftEdge?: () => void
  /**
   * Active indicators in display order. Pane indicators get panes 1..n in this order.
   * They always compute on the real candles, in every chart type (Heikin-Ashi included).
   */
  indicators?: readonly IndicatorConfig[]
  onIndicatorToggleVisible?: (uid: string) => void
  onIndicatorRemove?: (uid: string) => void
  onIndicatorSettings?: (uid: string) => void
  /** Drawings of the current symbol. */
  drawings?: readonly Drawing[]
  /** What a left press does. Default 'cursor'. */
  tool?: ToolMode
  selectedDrawingId?: string | null
  onSelectDrawing?: (id: string | null) => void
  /**
   * New drawing finished. The chart builds it: id = crypto.randomUUID(), symbol = props.symbol,
   * style = props.drawingStyle ?? DEFAULT_DRAWING_STYLE. Return `false` when the store rejects
   * it: the chart drops it at once and does not select it (onToolDone still fires).
   */
  onDrawingCreate?: (drawing: Drawing) => boolean | void
  drawingStyle?: DrawingStyle
  /** After a create completes, or when Escape cancels a tool: the app resets the tool to 'cursor'. */
  onToolDone?: () => void
  /**
   * A move or anchor drag ended (fired once on pointer-up, not per move). Return `false` when
   * the store rejects the patch: the drawing snaps back to its committed position.
   */
  onDrawingChange?: (id: string, patch: DrawingPatch) => boolean | void
  /**
   * Delete/Backspace with a drawing selected. Only when nothing is focused or focus is inside
   * the chart, and never from an editable or a [role=dialog|alertdialog|menu|listbox].
   */
  onDrawingDelete?: (id: string) => void
  /**
   * Right-click on the main pane (the native menu is suppressed). If a drawing is under the
   * cursor it is selected and its id passed.
   */
  onContextMenu?: (req: ChartContextMenuRequest) => void
  /** Active alerts for this symbol: dashed price lines with an axis label. */
  alertLines?: readonly AlertLine[]
  /**
   * A lab backtest for this symbol and interval: trade markers on the main series and an equity
   * pane below the indicator panes. null/undefined removes both.
   */
  backtest?: BacktestChartData | null
  /** Scroll/zoom the time scale to [from, to] (UNIX seconds) once per new `key`. */
  focusRange?: { key: number; from: number; to: number } | null
  /**
   * A focus request was handled: 'apply' scrolled the chart; 'before-data' / 'after-data' /
   * 'no-data' skipped it because the range lies outside the loaded bars. The caller should
   * clear the request so a remounted chart does not apply it again.
   */
  onFocusDone?: (key: number, result: FocusPlan['kind']) => void
  /**
   * Theme, candle colours and display time zone (default DEFAULT_APPEARANCE: dark, UTC).
   * Changes recolour the chart in place with applyOptions; the chart, series, drawings and
   * indicators are not recreated, and the user's scroll/zoom is kept.
   */
  appearance?: ChartAppearance
  /** Called with the chart's API once the chart exists, and with null on cleanup. */
  onApi?: (api: ChartApi | null) => void
  /**
   * True while a dialog, menu or the Research view has the keyboard: the chart ignores
   * Escape/Delete/Backspace, even when focus has fallen to <body> under the modal.
   */
  keyboardBlocked?: boolean
  /**
   * Multi-chart crosshair sync group (null/undefined = not synced). Hovering this chart moves
   * the other members' crosshairs to the same time, and theirs move this one's (the legend
   * follows). Needs `crosshairSyncId`.
   */
  crosshairSync?: CrosshairSync | null
  /** This chart's id inside `crosshairSync` (unique per chart). */
  crosshairSyncId?: string
  /** Regime labels for the plotted (closed) bars; null/undefined removes the ribbon. Colours follow `appearance`. */
  regime?: readonly RegimePoint[] | null
  /** Left click on the main pane with tool 'cursor' and no drawing hit: the open time (UNIX s) of the plotted bar under the cursor, or null when there is none. Used for picking the bar-replay start. */
  onTimeClick?: (time: number | null) => void
  /** True while bar replay drives `candles`: the chart must not auto-request older history (`onNearLeftEdge`/auto-older), and should keep the right edge in view as bars step forward. */
  replayActive?: boolean
}

const NO_INDICATORS: readonly IndicatorConfig[] = []
const NO_DRAWINGS: readonly Drawing[] = []
const NO_ALERT_LINES: readonly AlertLine[] = []

interface PanePoint extends PointerInput {
  /** Inside the main pane (not on an axis or an indicator pane). */
  inside: boolean
}

/** Viewport coordinates → main-pane CSS px (the space LWC primitives draw in). */
function toPanePoint(chart: IChartApi, clientX: number, clientY: number): PanePoint | null {
  const pane = chart.panes()[0]
  const el = pane?.getHTMLElement()
  if (!pane || !el) return null
  const rect = el.getBoundingClientRect()
  const leftScale = chart.priceScale('left')
  const leftWidth = leftScale.options().visible ? leftScale.width() : 0
  const x = clientX - rect.left - leftWidth
  const y = clientY - rect.top
  const inside = x >= 0 && y >= 0 && x < chart.timeScale().width() && y < pane.getHeight()
  return { x, y, inside }
}

/** Same tops (within half a pixel) → keep the old array so React skips the re-render. */
function sameTops(a: readonly (number | null)[], b: readonly (number | null)[]): boolean {
  if (a.length !== b.length) return false
  return a.every((t, i) => {
    const u = b[i]
    return t === null || u === null ? t === u : Math.abs(t - u) < 0.5
  })
}

export function Chart({
  candles,
  symbol,
  interval,
  source,
  chartType,
  onNearLeftEdge,
  historyStart = null,
  indicators = NO_INDICATORS,
  onIndicatorToggleVisible,
  onIndicatorRemove,
  onIndicatorSettings,
  drawings = NO_DRAWINGS,
  tool = 'cursor',
  selectedDrawingId,
  onSelectDrawing,
  onDrawingCreate,
  drawingStyle,
  onToolDone,
  onDrawingChange,
  onDrawingDelete,
  onContextMenu,
  alertLines = NO_ALERT_LINES,
  backtest = null,
  focusRange = null,
  onFocusDone,
  appearance = DEFAULT_APPEARANCE,
  onApi,
  keyboardBlocked = false,
  crosshairSync = null,
  crosshairSyncId,
  regime = null,
  onTimeClick,
  replayActive = false,
}: ChartProps) {
  // Callers may pass a fresh object per render; only the fields matter.
  const { theme, upColor, downColor, timezone } = appearance
  const palette = useMemo(
    () => paletteFor({ theme, upColor, downColor, timezone }),
    [theme, upColor, downColor, timezone],
  )
  /** Latest palette / appearance for effects that create series or read colours later. */
  const paletteRef = useRef(palette)
  const appearanceRef = useRef<ChartAppearance>({ theme, upColor, downColor, timezone })
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const mainSeriesRef = useRef<MainSeries | null>(null)
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  const watermarkRef = useRef<ITextWatermarkPluginApi<Time> | null>(null)
  /** Heikin-Ashi mode: price line at the real last close (the series' own label is hidden). */
  const realLineRef = useRef<IPriceLine | null>(null)
  /** Candles currently in the series; lets live ticks use update() instead of setData(). */
  const appliedRef = useRef<readonly Candle[] | null>(null)
  /**
   * Candles being pushed into the series by the data effect (null otherwise). LWC fires the
   * re-applied crosshair move synchronously inside setData()/update(), before `appliedRef` is
   * advanced, so crosshair sync reads this first. `appliedRef` itself keeps its old ordering:
   * the range-change handler, which can also fire inside setData(), relies on it being null
   * until the first push has finished.
   */
  const pushingRef = useRef<readonly Candle[] | null>(null)
  /** HA bars for `appliedRef` (Heikin-Ashi mode only), reused for tail updates. */
  const haRef = useRef<HeikinAshiBar[] | null>(null)
  const chartTypeRef = useRef<ChartType>(chartType)
  const onNearLeftEdgeRef = useRef(onNearLeftEdge)
  /** Pages the chart requested by itself since the user last moved the view (nextAutoOlder). */
  const autoOlderRef = useRef<AutoOlder | null>(null)
  /** Range the chart set after a prepend; its (late) range event is not a user move. */
  const expectedRangeRef = useRef<{ from: number; to: number } | null>(null)
  const [hoveredTime, setHoveredTime] = useState<number | undefined>(undefined)
  const [fontsReady, setFontsReady] = useState(false)
  /** Series of every indicator; also the store the indicator legend reads from. */
  const [indicatorManager] = useState(() => new IndicatorSeriesManager())
  const indicatorSnapshot = useSyncExternalStore(
    indicatorManager.subscribe,
    indicatorManager.getSnapshot,
  )
  /** Top of each pane (px from the chart's top edge), for the pane legends. */
  const [paneTops, setPaneTops] = useState<readonly (number | null)[]>([])
  /** Drawings: pixel <-> anchor mapping, interaction state machine, and renderer. */
  const [coords] = useState(() => new ChartCoordinateAdapter())
  const [drawingController] = useState(() => new DrawingController(coords))
  const [alertLinesManager] = useState(() => new AlertLinesManager(alertLineOptions))
  const drawingsPrimitiveRef = useRef<DrawingsPrimitive | null>(null)
  /** Backtest trade markers + equity pane; re-attached to each new main series. */
  const [backtestOverlay] = useState(
    () => new BacktestOverlay<MainSeries>((series) => createSeriesMarkers(series, [])),
  )
  const backtestVersion = useSyncExternalStore(
    backtestOverlay.subscribe,
    backtestOverlay.getSnapshot,
  )
  /** Pane-indicator identity the equity pane was last placed after. */
  const paneKeyRef = useRef<string | null>(null)
  const onContextMenuRef = useRef(onContextMenu)
  const onFocusDoneRef = useRef(onFocusDone)
  /** Read by the window keydown listener, so a change does not re-bind it. */
  const keyboardBlockedRef = useRef(keyboardBlocked)
  /** Bar length for crosshair sync, read at event time. */
  const stepRef = useRef(timeframeSeconds(interval))
  /** Read by the range listener, the data effect and auto-paging (see `replayActive`). */
  const replayActiveRef = useRef(replayActive)
  const onTimeClickRef = useRef(onTimeClick)
  const toolRef = useRef<ToolMode>(tool)
  /** The current press was taken by the drawing controller (a drawing hit, or a drawing tool). */
  const pressConsumedRef = useRef(false)
  /** Regime strip at the bottom of the main pane; exists while `regime` is non-null. */
  const ribbonRef = useRef<RegimeRibbon | null>(null)

  // Latest drawing callbacks, without re-binding DOM listeners.
  useEffect(() => {
    onContextMenuRef.current = onContextMenu
    drawingController.setCallbacks({
      onCreate: onDrawingCreate,
      onSelect: onSelectDrawing,
      onToolDone,
      onChange: onDrawingChange,
      onDelete: onDrawingDelete,
      // No panning or zooming while a drawing is being created or dragged.
      onBusyChange: (busy) =>
        chartRef.current?.applyOptions({ handleScroll: !busy, handleScale: !busy }),
      onRender: () => drawingsPrimitiveRef.current?.update(),
    })
  }, [
    drawingController,
    onContextMenu,
    onDrawingCreate,
    onSelectDrawing,
    onToolDone,
    onDrawingChange,
    onDrawingDelete,
  ])

  // Latest callbacks without resubscribing to the time scale / re-running the focus effect.
  useEffect(() => {
    onNearLeftEdgeRef.current = onNearLeftEdge
  }, [onNearLeftEdge])
  useEffect(() => {
    onFocusDoneRef.current = onFocusDone
  }, [onFocusDone])
  useEffect(() => {
    keyboardBlockedRef.current = keyboardBlocked
  }, [keyboardBlocked])
  // Declared before the data effect, so a render that both starts replay and swaps the candles
  // already sees replay on.
  useEffect(() => {
    replayActiveRef.current = replayActive
  }, [replayActive])
  useEffect(() => {
    onTimeClickRef.current = onTimeClick
  }, [onTimeClick])

  // Create the chart once per mount. StrictMode mounts twice; cleanup removes the first chart.
  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const chart = createChart(container, chartOptionsFor(appearanceRef.current))
    // Volume first so the main series (added by the chart-type effect) draws over it.
    const volumeSeries = chart.addSeries(HistogramSeries, volumeOptions)
    volumeSeries.priceScale().applyOptions({ scaleMargins: volumeScaleMargins })
    watermarkRef.current = createTextWatermark(
      chart.panes()[0],
      watermarkOptions('', fallbackFontFamily, paletteRef.current),
    )

    // LWC re-measures text only when the font string changes, so the chart starts on system
    // fonts and switches to IBM Plex once it has loaded. If it never loads, it stays on them.
    let disposed = false
    const fonts = document.fonts
    if (fonts) {
      void fonts.ready
        .then(() => Promise.all(webFonts.map((font) => fonts.load(font))))
        .then((loaded) => {
          if (disposed || !loaded.every((faces) => faces.length > 0)) return
          chart.applyOptions({ layout: { fontFamily } })
          setFontsReady(true)
        })
        .catch(() => {})
    }

    const onCrosshairMove = (param: MouseEventParams) => {
      const series = mainSeriesRef.current
      const bar = param.time !== undefined && series ? param.seriesData.get(series) : undefined
      // Candle times are UTCTimestamp numbers; anything else means "not over a bar".
      const time = bar && typeof bar.time === 'number' ? bar.time : undefined
      setHoveredTime(time)
    }
    chart.subscribeCrosshairMove(onCrosshairMove)

    const onRangeChange = (range: LogicalRange | null) => {
      if (!appliedRef.current?.length) return
      // The chart's own shift after a prepend: neither a user move nor a reason to load. The
      // auto-paging effect below decides whether another page follows.
      if (isExpectedRange(range, expectedRangeRef.current)) return
      expectedRangeRef.current = null
      // The view moved (scroll, zoom, a new live bar, new data): a new auto-paging run.
      if (autoOlderRef.current) autoOlderRef.current = { ...autoOlderRef.current, pages: 0 }
      // Bar replay shows a fixed prefix of the loaded bars: no older history while it runs.
      if (isNearLeftEdge(range) && !replayActiveRef.current) onNearLeftEdgeRef.current?.()
    }
    chart.timeScale().subscribeVisibleLogicalRangeChange(onRangeChange)

    // A plain click on the main pane (cursor tool, no drawing under it) reports its bar's time.
    const onClick = (param: MouseEventParams) => {
      const handler = onTimeClickRef.current
      if (!handler) return
      const series = mainSeriesRef.current
      const bar = param.time !== undefined && series ? param.seriesData.get(series) : undefined
      const time = clickedBarTime({
        tool: toolRef.current,
        pressConsumed: pressConsumedRef.current,
        hasPoint: param.point !== undefined,
        paneIndex: param.paneIndex,
        barTime: bar?.time,
      })
      if (time !== undefined) handler(time)
    }
    chart.subscribeClick(onClick)

    chartRef.current = chart
    volumeSeriesRef.current = volumeSeries
    coords.setChart(chart)
    indicatorManager.attach(chart)
    backtestOverlay.attachChart(backtestChartAdapter(chart))
    return () => {
      chart.unsubscribeCrosshairMove(onCrosshairMove)
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(onRangeChange)
      chart.unsubscribeClick(onClick)
      disposed = true
      watermarkRef.current?.detach()
      indicatorManager.detach()
      // chart.remove() below takes the equity series and markers with it.
      backtestOverlay.detachChart(false)
      paneKeyRef.current = null
      chartRef.current = null
      coords.setChart(null, chart)
      chart.remove()
      mainSeriesRef.current = null
      volumeSeriesRef.current = null
      watermarkRef.current = null
      realLineRef.current = null
      appliedRef.current = null
      haRef.current = null
      autoOlderRef.current = null
      expectedRangeRef.current = null
    }
  }, [indicatorManager, coords, backtestOverlay])

  // (Re)create the main series for the chart type. On mount the data effect below fills it;
  // on a type switch the candles already shown are re-applied here. Volume is untouched.
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    chartTypeRef.current = chartType
    const series = addMainSeries(chart, chartType, paletteRef.current)
    // Right above volume (order 0), below indicator overlays, as on TradingView.
    series.setSeriesOrder(1)
    mainSeriesRef.current = series
    if (usesRealPriceLine(chartType))
      realLineRef.current = series.createPriceLine(realPriceLineOptions)
    const applied = appliedRef.current
    if (applied) {
      const { points, ha } = mainSeriesData(chartType, applied)
      series.setData(points)
      haRef.current = ha
      syncRealPriceLine(realLineRef.current, applied, paletteRef.current)
    }
    // Drawings and alert lines live on the main series, so they move to the new one.
    coords.setSeries(series)
    const primitive = new DrawingsPrimitive(drawingController, coords)
    primitive.setHandleColors(handleColorsFor(paletteRef.current))
    series.attachPrimitive(primitive)
    drawingsPrimitiveRef.current = primitive
    alertLinesManager.attach(series)
    backtestOverlay.attachSeries(series)
    return () => {
      // On unmount the chart may already be removed (with all its series).
      const alive = chartRef.current === chart
      alertLinesManager.detach(alive)
      backtestOverlay.detachSeries(alive)
      if (alive) series.detachPrimitive(primitive)
      if (drawingsPrimitiveRef.current === primitive) drawingsPrimitiveRef.current = null
      coords.setSeries(null, series)
      if (alive) chart.removeSeries(series)
      if (mainSeriesRef.current === series) mainSeriesRef.current = null
      realLineRef.current = null
      haRef.current = null
    }
  }, [chartType, coords, drawingController, alertLinesManager, backtestOverlay])

  // Faint "BTCUSDT, 1h" behind the candles, as on TradingView.
  useEffect(() => {
    watermarkRef.current?.applyOptions(
      watermarkOptions(
        `${symbol}, ${timeframeLabel(interval)}`,
        fontsReady ? fontFamily : fallbackFontFamily,
        palette,
      ),
    )
  }, [symbol, interval, fontsReady, palette])

  // Bar length for extrapolating drawing anchors past either end of the data, and seconds on
  // the time axis for seconds timeframes (applied in place; the chart is not recreated).
  useEffect(() => {
    const step = timeframeSeconds(interval)
    coords.index.setStep(step)
    stepRef.current = step
    chartRef.current?.applyOptions(timeScaleOptionsFor(interval))
  }, [coords, interval])

  // Multi-chart crosshair sync. Declared after the creation effect, so the chart exists; the
  // series and bars are read at event time (a chart-type switch replaces the series).
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !crosshairSync || crosshairSyncId === undefined) return
    return bindCrosshairSync(crosshairSync, crosshairSyncId, chart, {
      series: () => mainSeriesRef.current,
      bars: () => pushingRef.current ?? appliedRef.current,
      step: () => stepRef.current,
      onShow: setHoveredTime,
    })
  }, [crosshairSync, crosshairSyncId])

  // Push data whenever the candles change. Runs after the creation effects on mount.
  // Live ticks only touch the tail, so they go through update(), which keeps the user's
  // scroll and zoom. Older history prepended uses setData() and shifts the visible range by the
  // number of new bars so the same bars stay on screen; anything else uses setData().
  useEffect(() => {
    const chart = chartRef.current
    const mainSeries = mainSeriesRef.current
    const volumeSeries = volumeSeriesRef.current
    if (!chart || !mainSeries || !volumeSeries) return
    const type = chartTypeRef.current
    const plan = planSeriesSync(appliedRef.current, candles)
    pushingRef.current = candles
    try {
      if (plan.kind === 'set' || plan.kind === 'prepend') {
        const timeScale = chart.timeScale()
        const range = plan.kind === 'prepend' ? timeScale.getVisibleLogicalRange() : null
        const { points, ha } = mainSeriesData(type, candles)
        mainSeries.setData(points)
        volumeSeries.setData(toVolumeData(candles, volumePaletteOf(paletteRef.current)))
        haRef.current = ha
        indicatorManager.applyPlan(plan, candles)
        if (plan.kind === 'prepend' && range) {
          const shifted = shiftLogicalRange(range, plan.count)
          expectedRangeRef.current = shifted
          timeScale.setVisibleLogicalRange(shifted)
        }
      } else if (plan.kind === 'update') {
        const { points, ha } = mainSeriesTail(type, candles, plan.from, haRef.current)
        for (const point of points) mainSeries.update(point)
        for (let i = plan.from; i < candles.length; i++) {
          volumeSeries.update(toVolumePoint(candles[i], volumePaletteOf(paletteRef.current)))
        }
        haRef.current = ha
        indicatorManager.applyPlan(plan, candles)
      }
      if (plan.kind !== 'none') syncRealPriceLine(realLineRef.current, candles, paletteRef.current)
      // Bar replay: a step forward that lands right of the view scrolls the newest bar back in.
      // (A step back or the replay start is a setData, which keeps the view on the last bar.)
      if (replayActiveRef.current && plan.kind !== 'none') {
        const timeScale = chart.timeScale()
        if (newestBarOffscreen(timeScale.getVisibleLogicalRange(), candles.length))
          timeScale.scrollToRealTime()
      }
    } finally {
      pushingRef.current = null
    }
    appliedRef.current = candles
    // O(1): the index keeps a reference; drawings re-project on LWC's next redraw.
    coords.index.setBars(candles)
    // Cheap per tick; re-filters markers/equity only when the bar set changed.
    backtestOverlay.setBars(candles)
  }, [candles, indicatorManager, coords, backtestOverlay])

  // Theme / candle colours / time zone. Everything is recoloured in place (applyOptions, one
  // volume setData), so the chart, its series, drawings, indicators and the view all survive.
  // Runs after the creation and data effects, so on mount it only re-applies the same values.
  useEffect(() => {
    paletteRef.current = palette
    const next: ChartAppearance = { theme, upColor, downColor, timezone }
    appearanceRef.current = next
    const chart = chartRef.current
    if (!chart) return
    chart.applyOptions(chartThemeOptionsFor(next))
    const series = mainSeriesRef.current
    if (series) applyMainSeriesOptions(series, chartTypeRef.current, palette)
    const applied = appliedRef.current
    if (applied) {
      volumeSeriesRef.current?.setData(toVolumeData(applied, volumePaletteOf(palette)))
      syncRealPriceLine(realLineRef.current, applied, palette)
    }
    drawingsPrimitiveRef.current?.setHandleColors(handleColorsFor(palette))
    ribbonRef.current?.setColors(regimeColorsFor(palette))
    indicatorManager.setPalette(palette)
  }, [palette, theme, upColor, downColor, timezone, indicatorManager])

  // Latest header values for screenshots, read when the user exports (not per render).
  const shotRef = useRef({ symbol, interval, source, candles })
  useEffect(() => {
    shotRef.current = { symbol, interval, source, candles }
  }, [symbol, interval, source, candles])
  const [api] = useState<ChartApi>(() => ({
    screenshot() {
      const chart = chartRef.current
      if (!chart) throw new Error('The chart is not ready yet')
      // addTopLayer = true keeps drawings and other primitives; the crosshair is left out.
      const canvas = chart.takeScreenshot(true, false)
      const { symbol: sym, interval: iv, source: src, candles: bars } = shotRef.current
      const tz = appearanceRef.current.timezone
      const last = buildLegend(bars, bars.length - 1)
      const nowSec = Date.now() / 1000
      const captured = `${formatDateTime(nowSec, tz)} ${timezoneLabel(tz, nowSec)}`
      const subtitle = last
        ? `O ${last.open}  H ${last.high}  L ${last.low}  C ${last.close}  ${last.change}  ·  Vol ${last.volume}  ·  ${captured}`
        : captured
      return composeScreenshot(canvas, {
        title: `${sym} · ${timeframeLabel(iv)} · ${src}`,
        subtitle,
        palette: paletteRef.current,
      })
    },
  }))
  // Declared after the creation effect, so the chart exists when the API is handed out.
  useEffect(() => {
    if (!onApi || !chartRef.current) return
    onApi(api)
    return () => onApi(null)
  }, [onApi, api])

  // A page landed (the raw start moved back). On a pair that rarely trades it adds few or no
  // plotted bars, so the view can still sit at the left edge: ask for the next page, at most
  // MAX_AUTO_OLDER_PAGES in a row until the user moves the view. Runs after the candles effect,
  // so the visible range already reflects this render's bars.
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    // Never while bar replay runs: it shows a fixed prefix of the loaded bars.
    const near =
      !replayActiveRef.current && isNearLeftEdge(chart.timeScale().getVisibleLogicalRange())
    const { state, request } = nextAutoOlder(autoOlderRef.current, historyStart, near)
    autoOlderRef.current = state
    if (request) onNearLeftEdgeRef.current?.()
  }, [historyStart])

  // Drawing props → controller (each triggers a primitive redraw, never a setData).
  useEffect(() => {
    drawingController.setDrawings(drawings)
  }, [drawingController, drawings])
  useEffect(() => {
    if (selectedDrawingId !== undefined) drawingController.setSelected(selectedDrawingId)
  }, [drawingController, selectedDrawingId])
  useEffect(() => {
    toolRef.current = tool
    drawingController.setTool(tool)
    // A drawing tool owns touch gestures on the chart; cursor mode leaves them to LWC.
    const container = containerRef.current
    if (container) container.style.touchAction = tool === 'cursor' ? '' : 'none'
  }, [drawingController, tool])
  useEffect(() => {
    drawingController.setSymbol(symbol)
  }, [drawingController, symbol])
  useEffect(() => {
    drawingController.setStyle(drawingStyle)
  }, [drawingController, drawingStyle])
  useEffect(() => {
    alertLinesManager.sync(alertLines)
  }, [alertLinesManager, alertLines])

  // Pointer, context-menu and key events for drawings. Capture-phase listeners on the container
  // see presses before LWC; a press the controller consumes is stopped (and its default
  // prevented, which also suppresses the compatibility mouse events LWC pans with). Presses on
  // empty chart in cursor mode pass through untouched.
  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    let captured: number | null = null

    const local = (e: MouseEvent): PanePoint | null => {
      const chart = chartRef.current
      return chart ? toPanePoint(chart, e.clientX, e.clientY) : null
    }
    const setCursor = (cursor: string | null) => {
      const el = chartRef.current?.chartElement()
      if (!el) return
      if (cursor) el.style.setProperty('cursor', cursor)
      else el.style.removeProperty('cursor')
    }
    // Mouse events are suppressed while we hold the press, so drive the crosshair ourselves.
    const followCrosshair = (p: PanePoint) => {
      const chart = chartRef.current
      const series = mainSeriesRef.current
      if (!chart || !series) return
      const price = series.coordinateToPrice(p.y)
      const time = xToSnappedTime(coords, p.x)
      if (price === null || time === null) return
      chart.setCrosshairPosition(price, time as UTCTimestamp, series)
    }

    const onPointerDown = (e: PointerEvent) => {
      // Ctrl-click is a right-click on macOS; leave it to the contextmenu handler.
      if (!e.isPrimary || e.ctrlKey) return
      pressConsumedRef.current = false
      const p = local(e)
      if (!p || !p.inside) return
      if (!drawingController.pointerDown({ x: p.x, y: p.y, button: e.button })) return
      pressConsumedRef.current = true
      e.stopPropagation()
      e.preventDefault()
      // preventDefault also stops the press from moving focus. Blur whatever had it (e.g. a
      // toolbar button) as a normal click on the chart would, so Delete reaches the chart.
      const active = document.activeElement
      if (active instanceof HTMLElement && active !== document.body && !container.contains(active))
        active.blur()
      try {
        container.setPointerCapture(e.pointerId)
        captured = e.pointerId
      } catch {
        captured = null
      }
      setCursor(drawingController.cursorAt(p))
    }
    const onPointerMove = (e: PointerEvent) => {
      if (!e.isPrimary || !drawingController.busy) return
      const p = local(e)
      if (!p) return
      const pressed = drawingController.pressed
      if (drawingController.pointerMove(p) && pressed) {
        followCrosshair(p)
        setCursor(drawingController.cursorAt(p))
      }
    }
    const onPointerUp = (e: PointerEvent) => {
      if (!e.isPrimary || !drawingController.busy) return
      const p = local(e)
      const handled = p ? drawingController.pointerUp(p) : false
      if (!p) drawingController.pointerCancel()
      if (captured !== null) {
        if (container.hasPointerCapture(captured)) container.releasePointerCapture(captured)
        captured = null
      }
      if (handled && p) setCursor(drawingController.cursorAt(p))
    }
    const onPointerCancel = (e: PointerEvent) => {
      if (!e.isPrimary) return
      drawingController.pointerCancel()
      captured = null
      setCursor(null)
    }
    const onContextMenuEvent = (e: MouseEvent) => {
      const p = local(e)
      if (!p || !p.inside) return
      const hit = drawingController.contextMenu(p)
      const handler = onContextMenuRef.current
      if (hit === null) {
        // Right-click cancelled a drawing in progress.
        e.preventDefault()
        return
      }
      if (!handler) return
      e.preventDefault()
      handler({ clientX: e.clientX, clientY: e.clientY, ...hit })
    }
    const onKeyDown = (e: KeyboardEvent) => {
      const key = chartKey(e, keyboardBlockedRef.current)
      if (key === null) return
      const accepted =
        key === 'Escape'
          ? chartAcceptsEscape(e.target)
          : chartOwnsKeyTarget(e.target, document.body, container)
      if (!accepted) return
      if (drawingController.keyDown(e.key)) e.preventDefault()
    }

    container.addEventListener('pointerdown', onPointerDown, true)
    container.addEventListener('pointermove', onPointerMove, true)
    window.addEventListener('pointerup', onPointerUp, true)
    window.addEventListener('pointercancel', onPointerCancel, true)
    container.addEventListener('contextmenu', onContextMenuEvent)
    window.addEventListener('keydown', onKeyDown)
    return () => {
      container.removeEventListener('pointerdown', onPointerDown, true)
      container.removeEventListener('pointermove', onPointerMove, true)
      window.removeEventListener('pointerup', onPointerUp, true)
      window.removeEventListener('pointercancel', onPointerCancel, true)
      container.removeEventListener('contextmenu', onContextMenuEvent)
      window.removeEventListener('keydown', onKeyDown)
      drawingController.pointerCancel()
    }
  }, [drawingController, coords])

  // Indicator configs changed: create / remove / recompute / recolour / hide series.
  // Runs after the candles effect on mount, so new indicators start from the applied candles.
  // The equity pane is taken out while pane indicators change, then re-added as the last pane,
  // so it never shares a pane with an indicator.
  useEffect(() => {
    const key = paneIndicatorKey(indicators)
    const panesChange = key !== paneKeyRef.current
    if (panesChange) backtestOverlay.suspendPane()
    indicatorManager.sync(indicators, appliedRef.current)
    if (panesChange) backtestOverlay.resumePane()
    paneKeyRef.current = key
  }, [indicators, indicatorManager, backtestOverlay])

  // Backtest overlay. Declared after the indicator effect so on mount the equity pane lands
  // below the indicator panes. Cleanup clears it (StrictMode re-runs re-add it).
  useEffect(() => {
    backtestOverlay.setData(backtest)
    return () => backtestOverlay.setData(null)
  }, [backtest, backtestOverlay])

  // Regime ribbon: created while `regime` is non-null, removed when it becomes null.
  const hasRegime = regime !== null
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !hasRegime) return
    const ribbon = new RegimeRibbon(chart, regimeColorsFor(paletteRef.current))
    ribbonRef.current = ribbon
    return () => {
      if (ribbonRef.current === ribbon) ribbonRef.current = null
      // On unmount the chart is already removed, with the ribbon series.
      if (chartRef.current === chart) ribbon.remove()
    }
  }, [hasRegime])

  // Ribbon points, filtered to the plotted bars (a point at a time the main series lacks would
  // add a bar to the time scale). Re-applied when the labels or the bar set change; live ticks
  // on the forming bar keep the key, so they cost nothing.
  const barsKey =
    candles.length === 0
      ? ''
      : `${candles.length}:${candles[0].time}:${candles[candles.length - 1].time}`
  useEffect(() => {
    const ribbon = ribbonRef.current
    if (!ribbon || regime === null) return
    const plotted = new Set((appliedRef.current ?? []).map((c) => c.time))
    ribbon.setPoints(regime, plotted)
  }, [regime, hasRegime, barsKey])

  // One-shot scroll/zoom to a time range (e.g. the backtest window or one trade).
  const focusKey = focusRange?.key ?? null
  const focusFrom = focusRange?.from ?? null
  const focusTo = focusRange?.to ?? null
  // The request is reported back once handled so the caller can drop it (a remounted chart
  // must not replay an old request).
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || focusKey === null || focusFrom === null || focusTo === null) return
    const bars = appliedRef.current
    const first = bars && bars.length > 0 ? bars[0].time : null
    const last = bars && bars.length > 0 ? bars[bars.length - 1].time : null
    // Clamped to the loaded bars: LWC needs both ends inside the data to map times to bars.
    const plan = planFocus(focusFrom, focusTo, first, last)
    if (plan.kind === 'apply') {
      try {
        chart.timeScale().setVisibleRange({
          from: plan.from as UTCTimestamp,
          to: plan.to as UTCTimestamp,
        })
      } catch {
        // A range LWC cannot map (e.g. while data is being replaced) is simply ignored.
      }
    }
    onFocusDoneRef.current?.(focusKey, plan.kind)
  }, [focusKey, focusFrom, focusTo])

  // Measure where each pane starts so pane legends sit at their pane's top-left corner.
  // Re-measured when panes change, the chart resizes, or the user drags a pane separator.
  useEffect(() => {
    const chart = chartRef.current
    const container = containerRef.current
    if (!chart || !container) return
    let frame = 0
    let retries = 0
    const observer = new ResizeObserver(() => measure())
    const observed = new Set<Element>()
    let disposed = false
    function measure() {
      // A ResizeObserver callback can run while a retry frame is pending; drop that frame.
      if (frame) cancelAnimationFrame(frame)
      frame = 0
      if (disposed || !chart || !container) return
      const base = container.getBoundingClientRect().top
      let missing = false
      const tops = chart.panes().map((pane) => {
        const el = pane.getHTMLElement()
        if (!el) {
          missing = true
          return null
        }
        if (!observed.has(el)) {
          observed.add(el)
          observer.observe(el)
        }
        return el.getBoundingClientRect().top - base
      })
      setPaneTops((prev) => (sameTops(prev, tops) ? prev : tops))
      // Pane widgets are created on LWC's next frame; try again until they exist.
      if (missing && retries++ < 10) frame = requestAnimationFrame(measure)
    }
    observer.observe(container)
    frame = requestAnimationFrame(measure)
    return () => {
      disposed = true
      if (frame) cancelAnimationFrame(frame)
      observer.disconnect()
    }
  }, [indicatorSnapshot.paneVersion, backtestVersion])

  const legend = useMemo(
    () => buildLegend(candles, selectLegendIndex(candles, hoveredTime)),
    [candles, hoveredTime],
  )

  // Cheap (binary search per line). useSyncExternalStore above re-renders on every store change
  // (live tick, config change), so this always reads the current values.
  const indicatorRows = indicatorManager.legendRows(hoveredTime)
  const equityPane = backtestOverlay.equityPaneIndex()
  const equityTop = equityPane !== null ? paneTops[equityPane] : null
  const equityLegend =
    equityTop !== null && equityTop !== undefined ? backtestOverlay.legend(hoveredTime) : []
  const legendActions = {
    onToggleVisible: onIndicatorToggleVisible,
    onRemove: onIndicatorRemove,
    onSettings: onIndicatorSettings,
  }

  return (
    <div className="chart" style={candleColorVars(palette)}>
      <div ref={containerRef} className="chart__canvas" />
      <Legend
        symbol={symbol}
        interval={interval}
        source={source}
        values={legend}
        timezone={timezone}
      >
        <IndicatorLegendRows rows={indicatorRows.filter((r) => r.pane === 0)} {...legendActions} />
      </Legend>
      <PaneLegends
        rows={indicatorRows.filter((r) => r.pane > 0)}
        paneTops={paneTops}
        {...legendActions}
      />
      {equityLegend.length > 0 && typeof equityTop === 'number' && (
        <div className="bt-pane-legend" style={{ top: equityTop + 6 }} aria-live="off">
          <span className="bt-pane-legend__title">Equity</span>
          {equityLegend.map((item) => (
            <span key={item.id} className="bt-pane-legend__item">
              <span
                className="bt-pane-legend__swatch"
                style={{ background: item.color }}
                aria-hidden="true"
              />
              {item.title}
              <span className="bt-pane-legend__value" style={{ color: item.color }}>
                {item.text}
              </span>
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

/** The legend (and indicator legends) colour changes with the user's candle colours. */
function candleColorVars(p: ChartPalette): CSSProperties {
  return { '--up': p.up, '--down': p.down } as CSSProperties
}

interface LegendProps {
  symbol: string
  interval: Timeframe
  source: string
  values: LegendValues | null
  /** Display zone for the bar time ('UTC' | 'local' | IANA). */
  timezone: string
  /** Overlay indicator rows, under the OHLC and volume rows. */
  children?: ReactNode
}

function Legend({ symbol, interval, source, values, timezone, children }: LegendProps) {
  return (
    <div className="chart-legend" aria-live="off">
      <div className="chart-legend__row">
        <span className="chart-legend__title">
          {symbol} · {timeframeLabel(interval)} · {source}
        </span>
        {values && (
          <span className={`chart-legend__values chart-legend__values--${values.direction}`}>
            <Field label="O" value={values.open} />
            <Field label="H" value={values.high} />
            <Field label="L" value={values.low} />
            <Field label="C" value={values.close} />
            <span className="chart-legend__change">{values.change}</span>
          </span>
        )}
      </div>
      {values && (
        <div className="chart-legend__row chart-legend__row--secondary">
          <Field label="Vol" value={values.volume} />
          <span className="chart-legend__time">
            {formatDateTime(values.time, timezone, { seconds: isSecondsTimeframe(interval) })}{' '}
            {timezoneLabel(timezone, values.time)}
          </span>
        </div>
      )}
      {children}
    </div>
  )
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <span className="chart-legend__field">
      <span className="chart-legend__label">{label}</span>
      <span className="chart-legend__value">{value}</span>
    </span>
  )
}
