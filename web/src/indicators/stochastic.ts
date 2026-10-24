import type { Candle } from '../types'
import { RollingWindow, Sma } from './primitives'
import type { IndicatorDef, IndicatorInputs, IndicatorStepper, IndicatorValues } from './types'

const LENGTH = { min: 1, max: 500, step: 1, integer: true } as const

/**
 * Stochastic oscillator (TradingView `ta.stoch` + SMA smoothing):
 * raw = 100 * (close - lowestLow) / (highestHigh - lowestLow) over the last kLength bars,
 * including the current one; %K = SMA(kSmooth) of raw; %D = SMA(dSmooth) of %K.
 * First %K at bar index kLength - 1 + kSmooth - 1; first %D dSmooth - 1 bars later.
 *
 * Convention: when highestHigh == lowestLow (a flat window) raw = 50, the midpoint.
 * TradingView yields na there, which would poison the SMA smoothing for kSmooth bars.
 */
class StochStepper implements IndicatorStepper {
  private readonly highs: RollingWindow
  private readonly lows: RollingWindow
  private readonly k: Sma
  private readonly d: Sma

  constructor(highs: RollingWindow, lows: RollingWindow, k: Sma, d: Sma) {
    this.highs = highs
    this.lows = lows
    this.k = k
    this.d = d
  }

  next(candle: Candle): IndicatorValues {
    this.highs.push(candle.high)
    this.lows.push(candle.low)
    if (!this.highs.full) return {}
    const hh = this.highs.max()
    const ll = this.lows.min()
    const raw = hh === ll ? 50 : (100 * (candle.close - ll)) / (hh - ll)
    const k = this.k.next(raw)
    if (k === undefined) return {}
    const d = this.d.next(k)
    return d === undefined ? { k } : { k, d }
  }

  clone(): StochStepper {
    return new StochStepper(this.highs.clone(), this.lows.clone(), this.k.clone(), this.d.clone())
  }
}

export const stochDef: IndicatorDef = {
  id: 'stoch',
  name: 'Stochastic',
  shortName: 'Stoch',
  description: 'Close relative to the recent high-low range (%K and %D, 0-100)',
  placement: 'pane',
  inputs: [
    { key: 'kLength', label: '%K length', default: 14, ...LENGTH },
    { key: 'kSmooth', label: '%K smoothing', default: 1, ...LENGTH },
    { key: 'dSmooth', label: '%D smoothing', default: 3, ...LENGTH },
  ],
  source: null,
  lines: [
    { key: 'k', label: '%K', kind: 'line', color: '#2962ff', lineWidth: 2 },
    { key: 'd', label: '%D', kind: 'line', color: '#ff6d00', lineWidth: 2 },
  ],
  guides: [
    { value: 80, label: 'Upper' },
    { value: 20, label: 'Lower' },
  ],
  range: { min: 0, max: 100 },
  // No source parameter: Stochastic always reads high, low, and close.
  createStepper(inputs: IndicatorInputs): IndicatorStepper {
    const kLength = Math.max(1, Math.round(inputs.kLength ?? 14))
    const kSmooth = Math.max(1, Math.round(inputs.kSmooth ?? 1))
    const dSmooth = Math.max(1, Math.round(inputs.dSmooth ?? 3))
    return new StochStepper(
      new RollingWindow(kLength),
      new RollingWindow(kLength),
      new Sma(kSmooth),
      new Sma(dSmooth),
    )
  },
}
