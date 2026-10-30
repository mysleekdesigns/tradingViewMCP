import type { Candle } from '../types'
import { Sma, firstResum } from './primitives'
import { priceOf } from './sources'
import type { IndicatorDef, IndicatorInputs, IndicatorStepper, PriceSource } from './types'

function periodOf(inputs: IndicatorInputs): number {
  return Math.max(1, Math.round(inputs.period))
}

function smaStepper(sma: Sma, source: PriceSource): IndicatorStepper {
  return {
    next(c: Candle) {
      const v = sma.next(priceOf(c, source), c.time)
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
    return smaStepper(new Sma(periodOf(inputs)), source)
  },
  // Bar i reads bars i - period + 1 .. i. Once the window holds only old bars and has had an
  // old bar before it in both runs, the first bar-time re-sum puts both in the same state.
  spliceFrom(candles, count, inputs) {
    const period = periodOf(inputs)
    return firstResum(candles, Math.max(count + period - 1, count + 1), period)
  },
}
