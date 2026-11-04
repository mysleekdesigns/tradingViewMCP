import type { Candle } from '../types'
import { Ema, rma } from './primitives'
import type { IndicatorDef, IndicatorInputs, IndicatorStepper, IndicatorValues } from './types'

/**
 * True range of `candle`: max(high - low, |high - prevClose|, |low - prevClose|). With no previous
 * close (`undefined`, the first bar of a series) it is high - low.
 */
export function trueRange(candle: Candle, prevClose: number | undefined): number {
  const hl = candle.high - candle.low
  if (prevClose === undefined) return hl
  return Math.max(hl, Math.abs(candle.high - prevClose), Math.abs(candle.low - prevClose))
}

/**
 * Average True Range, matching TradingView's `ta.atr`: Wilder's RMA (alpha 1/period) of the true
 * range. The first bar's TR is high - low. The first ATR is at bar index period - 1, the SMA of the
 * first `period` true ranges; after that atr = prev + (tr - prev) / period. Default period 14.
 */
class AtrStepper implements IndicatorStepper {
  private readonly avg: Ema
  private prevClose: number | undefined

  constructor(avg: Ema, prevClose: number | undefined) {
    this.avg = avg
    this.prevClose = prevClose
  }

  next(candle: Candle): IndicatorValues {
    const atr = this.avg.next(trueRange(candle, this.prevClose))
    this.prevClose = candle.close
    return atr === undefined ? {} : { atr }
  }

  clone(): AtrStepper {
    return new AtrStepper(this.avg.clone(), this.prevClose)
  }
}

export const atrDef: IndicatorDef = {
  id: 'atr',
  name: 'Average True Range',
  shortName: 'ATR',
  description: "Wilder's average true range: the typical bar size in price units",
  placement: 'pane',
  inputs: [
    { key: 'period', label: 'Length', default: 14, min: 1, max: 500, step: 1, integer: true },
  ],
  source: null,
  lines: [{ key: 'atr', label: 'ATR', kind: 'line', color: '#ef5350', lineWidth: 2 }],
  guides: [],
  // No source parameter: ATR always reads high, low and close. No `spliceFrom`: the Wilder seed
  // stays in the value forever, so a history prepend recomputes everything.
  createStepper(inputs: IndicatorInputs): IndicatorStepper {
    const period = Math.max(1, Math.round(inputs.period ?? 14))
    return new AtrStepper(rma(period), undefined)
  },
}
