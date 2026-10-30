import type { Candle } from '../types'
import { RollingWindow, Sma, firstResum } from './primitives'
import type { IndicatorDef, IndicatorInputs, IndicatorStepper, IndicatorValues } from './types'

const LENGTH = { min: 1, max: 500, step: 1, integer: true } as const

function lengthsOf(inputs: IndicatorInputs) {
  return {
    kLength: Math.max(1, Math.round(inputs.kLength ?? 14)),
    kSmooth: Math.max(1, Math.round(inputs.kSmooth ?? 1)),
    dSmooth: Math.max(1, Math.round(inputs.dSmooth ?? 3)),
  }
}

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
    const k = this.k.next(raw, candle.time)
    if (k === undefined) return {}
    const d = this.d.next(k, candle.time)
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
    const { kLength, kSmooth, dSmooth } = lengthsOf(inputs)
    // The high/low windows only need max/min (exact monotonic deques), no running sums.
    return new StochStepper(
      new RollingWindow(kLength, { track: 'none' }),
      new RollingWindow(kLength, { track: 'none' }),
      new Sma(kSmooth),
      new Sma(dSmooth),
    )
  },
  // raw at bar i reads the kLength-bar high/low windows (buffers and exact deques: the same in
  // both runs from bar count + kLength - 1). %K: once its window holds only those raws and it
  // had a push before in the old run, its first bar-time re-sum syncs it (bar iK). %D likewise
  // once its window holds only %K values from iK on. From there every state is the same.
  spliceFrom(candles, count, inputs) {
    const { kLength, kSmooth, dSmooth } = lengthsOf(inputs)
    const rawFrom = count + kLength - 1
    const iK = firstResum(candles, Math.max(rawFrom + kSmooth - 1, count + kLength), kSmooth)
    if (iK === null) return null
    return firstResum(candles, Math.max(iK + dSmooth - 1, count + kLength + kSmooth - 1), dSmooth)
  },
}
