import type { Candle } from '../types'
import { Ema } from './primitives'
import { priceOf } from './sources'
import type { IndicatorDef, IndicatorStepper, PriceSource } from './types'

function emaStepper(ema: Ema, source: PriceSource): IndicatorStepper {
  return {
    next(c: Candle) {
      const v = ema.next(priceOf(c, source))
      return v === undefined ? {} : { ema: v }
    },
    clone() {
      return emaStepper(ema.clone(), source)
    },
  }
}

/**
 * Exponential moving average, TradingView ta.ema convention: seeded with the SMA of the first
 * `period` values (first output at bar period-1), then prev + alpha * (value - prev) with
 * alpha = 2 / (period + 1).
 */
export const emaDef: IndicatorDef = {
  id: 'ema',
  name: 'Exponential Moving Average',
  shortName: 'EMA',
  description: 'Exponential moving average of price',
  placement: 'overlay',
  inputs: [
    { key: 'period', label: 'Length', default: 20, min: 1, max: 500, step: 1, integer: true },
  ],
  source: 'close',
  lines: [{ key: 'ema', label: 'EMA', kind: 'line', color: '#2962ff', lineWidth: 2 }],
  guides: [],
  createStepper(inputs, source) {
    const period = Math.max(1, Math.round(inputs.period))
    return emaStepper(new Ema(period), source)
  },
}
