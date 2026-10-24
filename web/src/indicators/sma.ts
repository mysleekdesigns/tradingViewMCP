import type { Candle } from '../types'
import { Sma } from './primitives'
import { priceOf } from './sources'
import type { IndicatorDef, IndicatorStepper, PriceSource } from './types'

function smaStepper(sma: Sma, source: PriceSource): IndicatorStepper {
  return {
    next(c: Candle) {
      const v = sma.next(priceOf(c, source))
      return v === undefined ? {} : { sma: v }
    },
    clone() {
      return smaStepper(sma.clone(), source)
    },
  }
}

/** Simple moving average of the source over the last `period` bars (first value at bar period-1). */
export const smaDef: IndicatorDef = {
  id: 'sma',
  name: 'Moving Average',
  shortName: 'SMA',
  description: 'Simple moving average of price',
  placement: 'overlay',
  inputs: [
    { key: 'period', label: 'Length', default: 20, min: 1, max: 500, step: 1, integer: true },
  ],
  source: 'close',
  lines: [{ key: 'sma', label: 'SMA', kind: 'line', color: '#f7a21b', lineWidth: 2 }],
  guides: [],
  createStepper(inputs, source) {
    const period = Math.max(1, Math.round(inputs.period))
    return smaStepper(new Sma(period), source)
  },
}
