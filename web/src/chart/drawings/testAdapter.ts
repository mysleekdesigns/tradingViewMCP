// Linear fake of the chart's coordinate system for the pure drawing tests:
// bars every 60 s from T0, 10 px per bar starting at x = 0, and y = (200 - price) * 2.

import type { CoordinateAdapter } from './geometry'
import { TimeIndex } from './timeIndex'

export const T0 = 1_700_000_000
export const STEP = 60
export const BAR_PX = 10
export const PANE_WIDTH = 800

export function makeBars(count: number): { time: number }[] {
  return Array.from({ length: count }, (_, i) => ({ time: T0 + i * STEP }))
}

export function fakeAdapter(barCount = 100): CoordinateAdapter {
  const index = new TimeIndex(makeBars(barCount), STEP)
  return {
    xToLogical: (x) => x / BAR_PX,
    logicalToX: (l) => l * BAR_PX,
    timeToLogical: (t) => index.timeToLogical(t),
    logicalToTime: (l) => index.logicalToTime(l),
    priceToY: (p) => (200 - p) * 2,
    yToPrice: (y) => 200 - y / 2,
    paneWidth: () => PANE_WIDTH,
  }
}

/** Time of bar i (extrapolated past the data). */
export const barTime = (i: number) => T0 + i * STEP
/** Pane y of a price. */
export const yOf = (price: number) => (200 - price) * 2
