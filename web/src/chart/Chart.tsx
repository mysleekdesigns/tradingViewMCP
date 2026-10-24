import { useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from 'react'
import {
  AreaSeries,
  CandlestickSeries,
  createChart,
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
} from 'lightweight-charts'
import { formatUtc } from '../components/quote'
import type { IndicatorConfig } from '../state/indicators'
import type { Candle, ChartType, Interval } from '../types'
import { mainSeriesData, mainSeriesTail, realPriceLine, usesRealPriceLine } from './chartTypes.ts'
import type { HeikinAshiBar } from './heikinAshi.ts'
import { IndicatorLegendRows, PaneLegends } from './IndicatorLegend'
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
  realPriceLineOptions,
  volumeOptions,
  volumeScaleMargins,
  watermarkOptions,
  webFonts,
} from './theme'
import { isNearLeftEdge, planSeriesSync, shiftLogicalRange, toVolumePoint } from './seriesSync'
import { toVolumeData } from './volume'

const volumePalette = { up: colors.volumeUp, down: colors.volumeDown }

type MainSeries = ISeriesApi<'Candlestick' | 'Line' | 'Area'>

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
  /** Ascending, unique times. The last bar may still be forming and changes on live ticks. */
  candles: readonly Candle[]
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
}

const NO_INDICATORS: readonly IndicatorConfig[] = []

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
  indicators = NO_INDICATORS,
  onIndicatorToggleVisible,
  onIndicatorRemove,
  onIndicatorSettings,
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

  // Latest callback without resubscribing to the time scale.
  useEffect(() => {
    onNearLeftEdgeRef.current = onNearLeftEdge
  }, [onNearLeftEdge])

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
      if (isNearLeftEdge(range)) onNearLeftEdgeRef.current?.()
    }
    chart.timeScale().subscribeVisibleLogicalRangeChange(onRangeChange)

    chartRef.current = chart
    volumeSeriesRef.current = volumeSeries
    indicatorManager.attach(chart)
    return () => {
      chart.unsubscribeCrosshairMove(onCrosshairMove)
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(onRangeChange)
      disposed = true
      watermarkRef.current?.detach()
      indicatorManager.detach()
      chartRef.current = null
      chart.remove()
      mainSeriesRef.current = null
      volumeSeriesRef.current = null
      watermarkRef.current = null
      realLineRef.current = null
      appliedRef.current = null
      haRef.current = null
    }
  }, [indicatorManager])

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
    return () => {
      // On unmount the chart may already be removed (with all its series).
      if (chartRef.current === chart) chart.removeSeries(series)
      if (mainSeriesRef.current === series) mainSeriesRef.current = null
      realLineRef.current = null
      haRef.current = null
    }
  }, [chartType])

  // Faint "BTCUSDT, 1h" behind the candles, as on TradingView.
  useEffect(() => {
    watermarkRef.current?.applyOptions(
      watermarkOptions(`${symbol}, ${interval}`, fontsReady ? fontFamily : fallbackFontFamily),
    )
  }, [symbol, interval, fontsReady])

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
        timeScale.setVisibleLogicalRange(shiftLogicalRange(range, plan.count))
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
  }, [candles, indicatorManager])

  // Indicator configs changed: create / remove / recompute / recolour / hide series.
  // Runs after the candles effect on mount, so new indicators start from the applied candles.
  useEffect(() => {
    indicatorManager.sync(indicators, appliedRef.current)
  }, [indicators, indicatorManager])

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
  }, [indicatorSnapshot.paneVersion])

  const legend = useMemo(
    () => buildLegend(candles, selectLegendIndex(candles, hoveredTime)),
    [candles, hoveredTime],
  )

  // Cheap (binary search per line). useSyncExternalStore above re-renders on every store change
  // (live tick, config change), so this always reads the current values.
  const indicatorRows = indicatorManager.legendRows(hoveredTime)
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
