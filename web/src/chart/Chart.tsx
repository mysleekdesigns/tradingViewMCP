import { useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from 'react'
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
import { formatUtc } from '../components/quote'
import '../components/Backtest.css'
import { getIndicatorDef } from '../indicators/registry'
import { chartAcceptsEscape, chartOwnsKeyTarget } from '../state/chartTools'
import type { IndicatorConfig } from '../state/indicators'
import type { Candle, ChartType, Interval } from '../types'
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
  areaOptions,
  candlestickOptions,
  chartOptions,
  colors,
  fallbackFontFamily,
  fontFamily,
  heikinAshiOptions,
  hollowCandlestickOptions,
  lineOptions,
  alertLineOptions,
  realPriceLineOptions,
  volumeOptions,
  volumeScaleMargins,
  watermarkOptions,
  webFonts,
} from './theme'
import {
  isNearLeftEdge,
  isExpectedRange,
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
import { INTERVAL_SECONDS } from './drawings/timeIndex'
import type {
  AlertLine,
  ChartContextMenuRequest,
  Drawing,
  DrawingPatch,
  DrawingStyle,
  ToolMode,
} from './drawings/types'

const volumePalette = { up: colors.volumeUp, down: colors.volumeDown }

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

function addMainSeries(chart: IChartApi, type: ChartType): MainSeries {
  switch (type) {
    case 'candles':
      return chart.addSeries(CandlestickSeries, candlestickOptions)
    case 'hollow':
      return chart.addSeries(CandlestickSeries, hollowCandlestickOptions)
    case 'heikin-ashi':
      return chart.addSeries(CandlestickSeries, heikinAshiOptions)
    case 'line':
      return chart.addSeries(LineSeries, lineOptions)
    case 'area':
      return chart.addSeries(AreaSeries, areaOptions)
  }
}

/** Move a real-close price line to the last candle (Heikin-Ashi mode only). */
function syncRealPriceLine(line: IPriceLine | null, candles: readonly Candle[]): void {
  if (!line) return
  const { price, color, visible } = realPriceLine(candles)
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
  interval: Interval
  /** Data source shown in the legend, e.g. "Binance.US". */
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
}: ChartProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const mainSeriesRef = useRef<MainSeries | null>(null)
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  const watermarkRef = useRef<ITextWatermarkPluginApi<Time> | null>(null)
  /** Heikin-Ashi mode: price line at the real last close (the series' own label is hidden). */
  const realLineRef = useRef<IPriceLine | null>(null)
  /** Candles currently in the series; lets live ticks use update() instead of setData(). */
  const appliedRef = useRef<readonly Candle[] | null>(null)
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

  // Create the chart once per mount. StrictMode mounts twice; cleanup removes the first chart.
  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const chart = createChart(container, chartOptions)
    // Volume first so the main series (added by the chart-type effect) draws over it.
    const volumeSeries = chart.addSeries(HistogramSeries, volumeOptions)
    volumeSeries.priceScale().applyOptions({ scaleMargins: volumeScaleMargins })
    watermarkRef.current = createTextWatermark(
      chart.panes()[0],
      watermarkOptions('', fallbackFontFamily),
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
      if (isNearLeftEdge(range)) onNearLeftEdgeRef.current?.()
    }
    chart.timeScale().subscribeVisibleLogicalRangeChange(onRangeChange)

    chartRef.current = chart
    volumeSeriesRef.current = volumeSeries
    coords.setChart(chart)
    indicatorManager.attach(chart)
    backtestOverlay.attachChart(backtestChartAdapter(chart))
    return () => {
      chart.unsubscribeCrosshairMove(onCrosshairMove)
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(onRangeChange)
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
    const series = addMainSeries(chart, chartType)
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
      syncRealPriceLine(realLineRef.current, applied)
    }
    // Drawings and alert lines live on the main series, so they move to the new one.
    coords.setSeries(series)
    const primitive = new DrawingsPrimitive(drawingController, coords)
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
      watermarkOptions(`${symbol}, ${interval}`, fontsReady ? fontFamily : fallbackFontFamily),
    )
  }, [symbol, interval, fontsReady])

  // Bar length for extrapolating drawing anchors past either end of the data.
  useEffect(() => {
    coords.index.setStep(INTERVAL_SECONDS[interval])
  }, [coords, interval])

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
    if (plan.kind === 'set' || plan.kind === 'prepend') {
      const timeScale = chart.timeScale()
      const range = plan.kind === 'prepend' ? timeScale.getVisibleLogicalRange() : null
      const { points, ha } = mainSeriesData(type, candles)
      mainSeries.setData(points)
      volumeSeries.setData(toVolumeData(candles, volumePalette))
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
        volumeSeries.update(toVolumePoint(candles[i], volumePalette))
      }
      haRef.current = ha
      indicatorManager.applyPlan(plan, candles)
    }
    if (plan.kind !== 'none') syncRealPriceLine(realLineRef.current, candles)
    appliedRef.current = candles
    // O(1): the index keeps a reference; drawings re-project on LWC's next redraw.
    coords.index.setBars(candles)
    // Cheap per tick; re-filters markers/equity only when the bar set changed.
    backtestOverlay.setBars(candles)
  }, [candles, indicatorManager, coords, backtestOverlay])

  // A page landed (the raw start moved back). On a pair that rarely trades it adds few or no
  // plotted bars, so the view can still sit at the left edge: ask for the next page, at most
  // MAX_AUTO_OLDER_PAGES in a row until the user moves the view. Runs after the candles effect,
  // so the visible range already reflects this render's bars.
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const near = isNearLeftEdge(chart.timeScale().getVisibleLogicalRange())
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
      const p = local(e)
      if (!p || !p.inside) return
      if (!drawingController.pointerDown({ x: p.x, y: p.y, button: e.button })) return
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
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return
      if (e.key !== 'Escape' && e.key !== 'Delete' && e.key !== 'Backspace') return
      const accepted =
        e.key === 'Escape'
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
    <div className="chart">
      <div ref={containerRef} className="chart__canvas" />
      <Legend symbol={symbol} interval={interval} source={source} values={legend}>
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

interface LegendProps {
  symbol: string
  interval: Interval
  source: string
  values: LegendValues | null
  /** Overlay indicator rows, under the OHLC and volume rows. */
  children?: ReactNode
}

function Legend({ symbol, interval, source, values, children }: LegendProps) {
  return (
    <div className="chart-legend" aria-live="off">
      <div className="chart-legend__row">
        <span className="chart-legend__title">
          {symbol} · {interval} · {source}
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
          <span className="chart-legend__time">{formatUtc(values.time)} UTC</span>
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
