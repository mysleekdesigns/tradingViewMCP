import type { Candle } from '../types'
import { Ema, rma } from './primitives'
import { priceOf } from './sources'
import type {
  IndicatorDef,
  IndicatorInputs,
  IndicatorStepper,
  IndicatorValues,
  PriceSource,
} from './types'

/**
 * Wilder's RSI, matching TradingView's `ta.rsi`:
 * - change = src - previous src (the first bar has no change, so it only primes `prev`);
 * - gains = max(change, 0), losses = max(-change, 0), each smoothed with Wilder's RMA
 *   (alpha 1/period), seeded with the SMA of the first `period` values;
 * - first value at bar index `period` (it needs `period` changes);
 * - avgLoss == 0 -> 100 (includes the flat case 0/0), else avgGain == 0 -> 0,
 *   else 100 - 100 / (1 + avgGain / avgLoss).
 */
class RsiStepper implements IndicatorStepper {
  private readonly source: PriceSource
  private readonly gain: Ema
  private readonly loss: Ema
  private prev: number | undefined

  constructor(source: PriceSource, gain: Ema, loss: Ema, prev: number | undefined) {
    this.source = source
    this.gain = gain
    this.loss = loss
    this.prev = prev
  }

  next(candle: Candle): IndicatorValues {
    const src = priceOf(candle, this.source)
    const prev = this.prev
    this.prev = src
    if (prev === undefined) return {}
    const change = src - prev
    const avgGain = this.gain.next(Math.max(change, 0))
    const avgLoss = this.loss.next(Math.max(-change, 0))
    if (avgGain === undefined || avgLoss === undefined) return {}
    if (avgLoss === 0) return { rsi: 100 }
    if (avgGain === 0) return { rsi: 0 }
    return { rsi: 100 - 100 / (1 + avgGain / avgLoss) }
  }

  clone(): RsiStepper {
    return new RsiStepper(this.source, this.gain.clone(), this.loss.clone(), this.prev)
  }
}

export const rsiDef: IndicatorDef = {
  id: 'rsi',
  name: 'Relative Strength Index',
  shortName: 'RSI',
  description: "Wilder's momentum oscillator (0-100) with 30/70 levels",
  placement: 'pane',
  inputs: [
    { key: 'period', label: 'Length', default: 14, min: 1, max: 500, step: 1, integer: true },
  ],
  source: 'close',
  lines: [{ key: 'rsi', label: 'RSI', kind: 'line', color: '#7e57c2', lineWidth: 2 }],
  guides: [
    { value: 70, label: 'Overbought' },
    { value: 30, label: 'Oversold' },
  ],
  range: { min: 0, max: 100 },
  createStepper(inputs: IndicatorInputs, source: PriceSource): IndicatorStepper {
    const period = Math.max(1, Math.round(inputs.period ?? 14))
    return new RsiStepper(source, rma(period), rma(period), undefined)
  },
}
