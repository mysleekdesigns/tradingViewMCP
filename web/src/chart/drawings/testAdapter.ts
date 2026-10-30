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
    // The mapping above never changes, so projections may be cached.
    viewVersion: () => 0,
  }
}

/** Time of bar i (extrapolated past the data). */
export const barTime = (i: number) => T0 + i * STEP
/** Pane y of a price. */
export const yOf = (price: number) => (200 - price) * 2

/**
 * A fake chart whose scroll (`offset`, in bars), zoom (`spacing`, px per bar), price scale
 * (`base`, `scale`), width and bars can change. Mutate `view`, or call `setBars` (which bumps
 * the viewVersion as the real adapter does), then let the code under test re-read it.
 */
export function movableAdapter(barCount = 100) {
  const view = { offset: 0, spacing: BAR_PX, base: 200, scale: 2, width: PANE_WIDTH }
  let index = new TimeIndex(makeBars(barCount), STEP)
  let version = 0
  const adapter: CoordinateAdapter = {
    xToLogical: (x) => x / view.spacing + view.offset,
    logicalToX: (l) => (l - view.offset) * view.spacing,
    timeToLogical: (t) => index.timeToLogical(t),
    logicalToTime: (l) => index.logicalToTime(l),
    priceToY: (p) => (view.base - p) * view.scale,
    yToPrice: (y) => view.base - y / view.scale,
    paneWidth: () => view.width,
    viewVersion: () => version,
  }
  return {
    adapter,
    view,
    setBars(bars: readonly { time: number }[]) {
      index = new TimeIndex(bars, STEP)
      version++
    },
  }
}
