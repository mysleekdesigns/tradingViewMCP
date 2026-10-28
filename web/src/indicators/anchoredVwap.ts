import type { Candle } from '../types'
import { priceOf } from './sources'
import type { IndicatorDef, IndicatorStepper, PriceSource } from './types'

/** 2100-01-01 00:00:00 UTC: upper bound for a time input. */
const MAX_TIME = 4_102_444_800

interface AvwapState {
  pv: number
  vol: number
  /** The first bar has been seen (the anchor check below is made on it, once). */
  seen: boolean
  /**
   * The first bar opened after the anchor, so the anchor bar is not in the loaded history:
   * no values for the whole series (starting at the first loaded bar would mislabel the line
   * and move it as older pages load). Decided from the first bar only, so no look-ahead.
   */
  unloaded: boolean
}

/** Volume a bar contributes: missing, non-finite or negative volume counts as 0. */
function usableVolume(c: Candle): number {
  return Number.isFinite(c.volume) && c.volume > 0 ? c.volume : 0
}

function avwapStepper(anchor: number, source: PriceSource, s: AvwapState): IndicatorStepper {
  return {
    next(c: Candle) {
      if (!s.seen) {
        s.seen = true
        s.unloaded = anchor > 0 && c.time > anchor
      }
      // Anchor 0 = not set: never a value. Bars opening before the anchor are skipped.
      if (anchor <= 0 || s.unloaded || c.time < anchor) return {}
      const price = priceOf(c, source)
      // A bar with no usable volume or price adds nothing (it never poisons the sums).
      const v = Number.isFinite(price) ? usableVolume(c) : 0
      if (v > 0) {
        s.pv += price * v
        s.vol += v
      }
      return s.vol > 0 ? { avwap: s.pv / s.vol } : {}
    },
    clone() {
      return avwapStepper(anchor, source, { ...s })
    },
  }
}

/**
 * Anchored VWAP: cumulative sum(source x volume) / sum(volume) from the first bar whose open time
 * is at or after `anchor` (UNIX seconds), never reset. An anchor between two bars starts at the
 * next bar. Anchor 0 (not set) gives no values; no value while the cumulative volume is 0.
 * When the first candle given opens after the anchor (the anchor bar is older than the loaded
 * history) there are no values at all. Missing, non-finite or negative volume counts as 0.
 * Default source hlc3, as TradingView's Anchored VWAP.
 */
export const anchoredVwapDef: IndicatorDef = {
  id: 'avwap',
  name: 'Anchored VWAP',
  shortName: 'AVWAP',
  description: 'Volume-weighted average price from a chosen anchor bar onward',
  placement: 'overlay',
  inputs: [
    {
      key: 'anchor',
      label: 'Anchor',
      kind: 'time',
      default: 0,
      min: 0,
      max: MAX_TIME,
      step: 60,
      integer: true,
    },
  ],
  source: 'hlc3',
  lines: [{ key: 'avwap', label: 'AVWAP', kind: 'line', color: '#00bcd4', lineWidth: 2 }],
  guides: [],
  createStepper(inputs, source) {
    const anchor = Number.isFinite(inputs.anchor) ? inputs.anchor : 0
    return avwapStepper(anchor, source, { pv: 0, vol: 0, seen: false, unloaded: false })
  },
}
