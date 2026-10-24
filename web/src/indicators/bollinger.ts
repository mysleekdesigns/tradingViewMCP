import type { Candle } from '../types'
import { RollingWindow } from './primitives'
import { priceOf } from './sources'
import type { IndicatorDef, IndicatorStepper, PriceSource } from './types'

function bbStepper(win: RollingWindow, mult: number, source: PriceSource): IndicatorStepper {
  return {
    next(c: Candle) {
      win.push(priceOf(c, source))
      if (!win.full) return {}
      const basis = win.mean()
      const dev = mult * win.stdev()
      return { basis, upper: basis + dev, lower: basis - dev }
    },
    clone() {
      return bbStepper(win.clone(), mult, source)
    },
  }
}

/**
 * Bollinger Bands: basis = SMA(period); upper/lower = basis +/- mult x POPULATION stdev of the
 * same window (divide by N, TradingView ta.stdev / ta.bb convention).
 */
export const bollingerDef: IndicatorDef = {
  id: 'bb',
  name: 'Bollinger Bands',
  shortName: 'BB',
  description: 'Moving average with standard-deviation bands',
  placement: 'overlay',
  inputs: [
    { key: 'period', label: 'Length', default: 20, min: 1, max: 500, step: 1, integer: true },
    { key: 'mult', label: 'StdDev', default: 2, min: 0.1, max: 10, step: 0.1, integer: false },
  ],
  source: 'close',
  lines: [
    { key: 'basis', label: 'Basis', kind: 'line', color: '#ff6d00', lineWidth: 1 },
    { key: 'upper', label: 'Upper', kind: 'line', color: '#2962ff', lineWidth: 1 },
    { key: 'lower', label: 'Lower', kind: 'line', color: '#089981', lineWidth: 1 },
  ],
  guides: [],
  createStepper(inputs, source) {
    const period = Math.max(1, Math.round(inputs.period))
    return bbStepper(new RollingWindow(period), inputs.mult, source)
  },
}
