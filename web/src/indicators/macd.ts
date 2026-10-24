import type { Candle } from '../types'
import { Ema } from './primitives'
import { priceOf } from './sources'
import type {
  IndicatorDef,
  IndicatorInputs,
  IndicatorStepper,
  IndicatorValues,
  PriceSource,
} from './types'

const LENGTH = { min: 1, max: 500, step: 1, integer: true } as const

/**
 * MACD (TradingView `ta.macd`): macd = EMA(fast) - EMA(slow) of src, both SMA-seeded EMAs;
 * signal = SMA-seeded EMA(signal) of the macd values; hist = macd - signal.
 * macd first at bar index max(fast, slow) - 1; signal and hist `signal - 1` bars later.
 * fast > slow is allowed and not swapped (the line just changes sign).
 */
class MacdStepper implements IndicatorStepper {
  private readonly source: PriceSource
  private readonly fast: Ema
  private readonly slow: Ema
  private readonly signal: Ema

  constructor(source: PriceSource, fast: Ema, slow: Ema, signal: Ema) {
    this.source = source
    this.fast = fast
    this.slow = slow
    this.signal = signal
  }

  next(candle: Candle): IndicatorValues {
    const src = priceOf(candle, this.source)
    const f = this.fast.next(src)
    const s = this.slow.next(src)
    if (f === undefined || s === undefined) return {}
    const macd = f - s
    const signal = this.signal.next(macd)
    if (signal === undefined) return { macd }
    return { macd, signal, hist: macd - signal }
  }

  clone(): MacdStepper {
    return new MacdStepper(this.source, this.fast.clone(), this.slow.clone(), this.signal.clone())
  }
}

export const macdDef: IndicatorDef = {
  id: 'macd',
  name: 'MACD',
  shortName: 'MACD',
  description: 'Moving average convergence/divergence with signal line and histogram',
  placement: 'pane',
  inputs: [
    { key: 'fast', label: 'Fast length', default: 12, ...LENGTH },
    { key: 'slow', label: 'Slow length', default: 26, ...LENGTH },
    { key: 'signal', label: 'Signal smoothing', default: 9, ...LENGTH },
  ],
  source: 'close',
  lines: [
    { key: 'hist', label: 'Histogram', kind: 'histogram', color: '#26a69a', lineWidth: 1 },
    { key: 'macd', label: 'MACD', kind: 'line', color: '#2962ff', lineWidth: 2 },
    { key: 'signal', label: 'Signal', kind: 'line', color: '#ff6d00', lineWidth: 2 },
  ],
  guides: [],
  createStepper(inputs: IndicatorInputs, source: PriceSource): IndicatorStepper {
    const fast = Math.max(1, Math.round(inputs.fast ?? 12))
    const slow = Math.max(1, Math.round(inputs.slow ?? 26))
    const signal = Math.max(1, Math.round(inputs.signal ?? 9))
    return new MacdStepper(source, new Ema(fast), new Ema(slow), new Ema(signal))
  },
}
