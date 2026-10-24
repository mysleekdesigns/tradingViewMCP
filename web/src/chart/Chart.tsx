import { useEffect, useMemo, useRef, useState } from 'react'
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
import type { Candle, ChartType, Interval } from '../types'
import { mainSeriesData, mainSeriesTail, realPriceLine, usesRealPriceLine } from './chartTypes.ts'
import type { HeikinAshiBar } from './heikinAshi.ts'
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
}

export function Chart({
  candles,
  symbol,
  interval,
  source,
  chartType,
  onNearLeftEdge,
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
    return () => {
      chart.unsubscribeCrosshairMove(onCrosshairMove)
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(onRangeChange)
      disposed = true
      watermarkRef.current?.detach()
      chartRef.current = null
      chart.remove()
      mainSeriesRef.current = null
      volumeSeriesRef.current = null
      watermarkRef.current = null
      realLineRef.current = null
      appliedRef.current = null
      haRef.current = null
    }
  }, [])

  // (Re)create the main series for the chart type. On mount the data effect below fills it;
  // on a type switch the candles already shown are re-applied here. Volume is untouched.
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    chartTypeRef.current = chartType
    const series = addMainSeries(chart, chartType)
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
    }
    if (plan.kind !== 'none') syncRealPriceLine(realLineRef.current, candles)
    appliedRef.current = candles
  }, [candles])

  const legend = useMemo(
    () => buildLegend(candles, selectLegendIndex(candles, hoveredTime)),
    [candles, hoveredTime],
  )

  return (
    <div className="chart">
      <div ref={containerRef} className="chart__canvas" />
      <Legend symbol={symbol} interval={interval} source={source} values={legend} />
    </div>
  )
}

interface LegendProps {
  symbol: string
  interval: Interval
  source: string
  values: LegendValues | null
}

function Legend({ symbol, interval, source, values }: LegendProps) {
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
