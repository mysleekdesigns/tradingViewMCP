// CoordinateAdapter backed by the live chart: x ↔ logical through the time scale, time ↔
// logical through a TimeIndex over the applied candles, price ↔ y through the main series.
// The series reference is swapped when the chart type replaces the main series.

import type { IChartApi, ISeriesApi, Logical, SeriesType } from 'lightweight-charts'
import type { CoordinateAdapter } from './geometry'
import { TimeIndex } from './timeIndex'

export class ChartCoordinateAdapter implements CoordinateAdapter {
  private chart: IChartApi | null = null
  private series: ISeriesApi<SeriesType> | null = null
  readonly index = new TimeIndex()
  /** Bumped on every (re)bind of the chart or series. */
  private bindings = 0

  /** Bind (or, with null, release) the chart. Release only if `chart` is still the bound one. */
  setChart(chart: IChartApi | null, previous?: IChartApi): void {
    if (chart === null && previous !== undefined && this.chart !== previous) return
    this.chart = chart
    this.bindings++
  }

  /** Bind (or, with null, release) the main series; same release rule as setChart. */
  setSeries(series: ISeriesApi<SeriesType> | null, previous?: ISeriesApi<SeriesType>): void {
    if (series === null && previous !== undefined && this.series !== previous) return
    this.series = series
    this.bindings++
  }

  xToLogical(x: number): number | null {
    if (!this.chart || this.index.length === 0) return null
    return this.chart.timeScale().coordinateToLogical(x)
  }

  logicalToX(logical: number): number | null {
    if (!this.chart || this.index.length === 0) return null
    return this.chart.timeScale().logicalToCoordinate(logical as Logical)
  }

  timeToLogical(time: number): number | null {
    return this.index.timeToLogical(time)
  }

  logicalToTime(logical: number): number | null {
    return this.index.logicalToTime(logical)
  }

  priceToY(price: number): number | null {
    return this.series?.priceToCoordinate(price) ?? null
  }

  yToPrice(y: number): number | null {
    return this.series?.coordinateToPrice(y) ?? null
  }

  paneWidth(): number {
    return this.chart?.timeScale().width() ?? 0
  }

  /** Sum of two counters that only increase, so any rebind or index change increases it. */
  viewVersion(): number {
    return this.bindings + this.index.version()
  }
}
