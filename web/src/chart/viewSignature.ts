// A cheap fingerprint of everything an overlay layer's pixel geometry depends on, so
// `updateAllViews` can return the geometry it built last time when nothing moved (a drawing drag,
// a live tick or a hover change asks every primitive to update; a crosshair-only move repaints
// the top canvas without calling `updateAllViews`).
//
// Horizontal: the x of logical index 0 (scroll position) plus the bar spacing and pane width.
// A prepend shifts which bar is at logical 0, so it changes x0 as well. Vertical: the y of two
// reference prices; every price scale mode (normal, log, percentage, indexed-to-100) maps
// prices through a monotone function fixed by the scale's range, height and base value, so any
// change to those moves at least one of the two. `version` is the layer's own counter: bump it
// whenever the data the geometry is built from, the colours or the show toggles change.

import type {
  IChartApi,
  ISeriesApi,
  ITimeScaleApi,
  Logical,
  SeriesType,
  Time,
} from 'lightweight-charts'

export interface ViewSignature {
  readonly x0: number
  readonly barSpacing: number
  readonly width: number
  readonly y0: number
  readonly y1: number
  readonly version: number
}

/**
 * Pixel distance between two adjacent bars, from two `logicalToCoordinate` calls (no options
 * clone, unlike `timeScale().options().barSpacing`). 0 when the time scale has no coordinates.
 */
export function barSpacingOf(ts: ITimeScaleApi<Time>): number {
  const c0 = ts.logicalToCoordinate(0 as Logical)
  const c1 = ts.logicalToCoordinate(1 as Logical)
  return c0 !== null && c1 !== null ? Math.abs(c1 - c0) : 0
}

/**
 * The current signature, or null when the chart cannot place logical 0 or either reference
 * price (then always rebuild). `refPrices` should be two distinct finite prices (positive, so
 * they are valid on a log scale); the layer's own price extent is a good choice, but any fixed
 * pair works.
 */
export function viewSignature(
  chart: IChartApi,
  series: ISeriesApi<SeriesType>,
  refPrices: readonly [number, number],
  version: number,
): ViewSignature | null {
  const ts = chart.timeScale()
  const x0 = ts.logicalToCoordinate(0 as Logical)
  const x1 = ts.logicalToCoordinate(1 as Logical)
  const y0 = series.priceToCoordinate(refPrices[0])
  const y1 = series.priceToCoordinate(refPrices[1])
  if (x0 === null || x1 === null || y0 === null || y1 === null) return null
  return { x0, barSpacing: Math.abs(x1 - x0), width: ts.width(), y0, y1, version }
}

/** True when both signatures exist and are equal field by field. */
export function sameViewSignature(a: ViewSignature | null, b: ViewSignature | null): boolean {
  return (
    a !== null &&
    b !== null &&
    a.x0 === b.x0 &&
    a.barSpacing === b.barSpacing &&
    a.width === b.width &&
    a.y0 === b.y0 &&
    a.y1 === b.y1 &&
    a.version === b.version
  )
}
