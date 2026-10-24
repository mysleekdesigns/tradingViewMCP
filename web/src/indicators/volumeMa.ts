import type { Candle } from '../types'
import { Sma } from './primitives'
import type { IndicatorDef, IndicatorStepper } from './types'

function volMaStepper(sma: Sma): IndicatorStepper {
  return {
    next(c: Candle) {
      const v = sma.next(c.volume)
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
    const period = Math.max(1, Math.round(inputs.period))
    return volMaStepper(new Sma(period))
  },
}
