import { useEffect, useMemo, useRef, useState } from 'react'
import {
  CandlestickSeries,
  createChart,
  HistogramSeries,
  type ISeriesApi,
  type MouseEventParams,
} from 'lightweight-charts'
import type { Candle, Interval } from '../types'
import { buildLegend, selectLegendIndex, type LegendValues } from './legend.ts'
import {
  candlestickOptions,
  chartOptions,
  colors,
  volumeOptions,
  volumeScaleMargins,
} from './theme'
import { toCandlestickData, toVolumeData } from './volume'

interface ChartProps {
  candles: Candle[]
  symbol: string
  interval: Interval
}

export function Chart({ candles, symbol, interval }: ChartProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const candleSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  const [hoveredTime, setHoveredTime] = useState<number | undefined>(undefined)

  // Create the chart once per mount. StrictMode mounts twice; cleanup removes the first chart.
  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const chart = createChart(container, chartOptions)
    const candleSeries = chart.addSeries(CandlestickSeries, candlestickOptions)
    const volumeSeries = chart.addSeries(HistogramSeries, volumeOptions)
    volumeSeries.priceScale().applyOptions({ scaleMargins: volumeScaleMargins })

    const onCrosshairMove = (param: MouseEventParams) => {
      const bar = param.time !== undefined ? param.seriesData.get(candleSeries) : undefined
      // Candle times are UTCTimestamp numbers; anything else means "not over a bar".
      const time = bar && typeof bar.time === 'number' ? bar.time : undefined
      setHoveredTime(time)
    }
    chart.subscribeCrosshairMove(onCrosshairMove)

    candleSeriesRef.current = candleSeries
    volumeSeriesRef.current = volumeSeries
    return () => {
      chart.unsubscribeCrosshairMove(onCrosshairMove)
      chart.remove()
      candleSeriesRef.current = null
      volumeSeriesRef.current = null
    }
  }, [])

  // Push data whenever the candles change. Runs after the creation effect on mount.
  useEffect(() => {
    const candleSeries = candleSeriesRef.current
    const volumeSeries = volumeSeriesRef.current
    if (!candleSeries || !volumeSeries) return
    candleSeries.setData(toCandlestickData(candles))
    volumeSeries.setData(toVolumeData(candles, { up: colors.volumeUp, down: colors.volumeDown }))
  }, [candles])

  const legend = useMemo(
    () => buildLegend(candles, selectLegendIndex(candles, hoveredTime)),
    [candles, hoveredTime],
  )

  return (
    <div className="chart">
      <div ref={containerRef} className="chart__canvas" />
      <Legend symbol={symbol} interval={interval} values={legend} />
    </div>
  )
}

interface LegendProps {
  symbol: string
  interval: Interval
  values: LegendValues | null
}

function Legend({ symbol, interval, values }: LegendProps) {
  return (
    <div className="chart-legend" aria-live="off">
      <span className="chart-legend__title">
        {symbol} · {interval}
      </span>
      {values && (
        <span className={`chart-legend__values chart-legend__values--${values.direction}`}>
          <Field label="O" value={values.open} />
          <Field label="H" value={values.high} />
          <Field label="L" value={values.low} />
          <Field label="C" value={values.close} />
          <span className="chart-legend__change">{values.change}</span>
          <Field label="V" value={values.volume} />
        </span>
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
