// Which plotted bars the market-structure overlay (Phase 16) may use: only bars whose final
// values are confirmed. Pure, so the wiring in ChartPanel is tested.

import type { LiveCandlesSnapshot } from '../state/liveCandles'
import type { Candle } from '../types'

/**
 * The `closedThrough` time the chart's market-structure layer gets (it uses plotted bars with
 * time <= it, never the forming bar or a bar whose final values are not confirmed yet):
 * - nothing loaded (live load not ready) → null: no structure;
 * - live → the controller's `closedThrough` (see `LiveCandlesSnapshot.closedThrough`);
 * - bar replay (`replayBars` set) → the last replay bar (replay shows closed bars only), but
 *   never past the live `closedThrough` of the same stream, so a partial bar that replay can
 *   still see while a gap fill is pending is not used either. No replay bars → null.
 */
export function structureClosedThrough(
  live: Pick<LiveCandlesSnapshot, 'load' | 'closedThrough'>,
  replayBars: readonly Candle[] | null,
): number | null {
  if (live.load.kind !== 'ready') return null
  const through = live.closedThrough
  if (replayBars === null) return through
  if (replayBars.length === 0 || through === null) return null
  return Math.min(replayBars[replayBars.length - 1].time, through)
}

/** How many leading bars of ascending `bars` open at or before `closedThrough` (0 for null). */
export function closedBarCount(bars: readonly Candle[], closedThrough: number | null): number {
  if (closedThrough === null) return 0
  let lo = 0
  let hi = bars.length
  while (lo < hi) {
    const mid = (lo + hi) >>> 1
    if (bars[mid].time <= closedThrough) lo = mid + 1
    else hi = mid
  }
  return lo
}
