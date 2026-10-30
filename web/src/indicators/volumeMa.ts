import type { Candle } from '../types'
import { Sma, firstResum } from './primitives'
import type { IndicatorDef, IndicatorInputs, IndicatorStepper } from './types'

function periodOf(inputs: IndicatorInputs): number {
  return Math.max(1, Math.round(inputs.period))
}

function volMaStepper(sma: Sma): IndicatorStepper {
  return {
    next(c: Candle) {
      const v = sma.next(c.volume, c.time)
      return v === undefined ? {} : { ma: v }
    },
    clone() {
      return volMaStepper(sma.clone())
    },
  }
}

/** Simple moving average of bar volume, drawn on the volume scale. */
export const volumeMaDef: IndicatorDef = {
  id: 'volma',
  name: 'Volume Moving Average',
  shortName: 'Vol MA',
  description: 'Simple moving average of volume',
  placement: 'volume',
  inputs: [
    { key: 'period', label: 'Length', default: 20, min: 1, max: 500, step: 1, integer: true },
  ],
  source: null,
  lines: [{ key: 'ma', label: 'MA', kind: 'line', color: '#ff9800', lineWidth: 1 }],
  guides: [],
  createStepper(inputs) {
    return volMaStepper(new Sma(periodOf(inputs)))
  },
  // Bar i reads bars i - period + 1 .. i. Once the window holds only old bars and has had an
  // old bar before it in both runs, the first bar-time re-sum puts both in the same state.
  spliceFrom(candles, count, inputs) {
    const period = periodOf(inputs)
    return firstResum(candles, Math.max(count + period - 1, count + 1), period)
  },
}
