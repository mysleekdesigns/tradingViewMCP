import type { Candle } from '../types'
import type { IndicatorDef, IndicatorStepper } from './types'

const DAY = 86400

interface VwapState {
  /** UTC day index (floor(time / 86400)) of the current session, or null before the first bar. */
  day: number | null
  pv: number
  vol: number
}

function vwapStepper(s: VwapState): IndicatorStepper {
  return {
    next(c: Candle) {
      const day = Math.floor(c.time / DAY)
      if (day !== s.day) {
        s.day = day
        s.pv = 0
        s.vol = 0
      }
      const hlc3 = (c.high + c.low + c.close) / 3
      s.pv += hlc3 * c.volume
      s.vol += c.volume
      return s.vol > 0 ? { vwap: s.pv / s.vol } : {}
    },
    clone() {
      return vwapStepper({ ...s })
    },
  }
}

/**
 * Session VWAP anchored to the UTC day: cumulative sum(hlc3 x volume) / sum(volume), reset when
 * floor(time / 86400) changes. On 1d and longer bars each bar is its own session, so the value is
 * that bar's hlc3. No value while the session's cumulative volume is 0.
 */
export const vwapDef: IndicatorDef = {
  id: 'vwap',
  name: 'VWAP',
  shortName: 'VWAP',
  description: 'Volume-weighted average price, reset each UTC day',
  placement: 'overlay',
  inputs: [],
  source: null,
  lines: [{ key: 'vwap', label: 'VWAP', kind: 'line', color: '#ab47bc', lineWidth: 2 }],
  guides: [],
  createStepper() {
    return vwapStepper({ day: null, pv: 0, vol: 0 })
  },
}
