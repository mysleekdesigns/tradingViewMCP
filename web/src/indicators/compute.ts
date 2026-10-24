import type { Candle } from '../types'
import type {
  IndicatorDef,
  IndicatorInputs,
  IndicatorOutput,
  IndicatorStepper,
  PriceSource,
} from './types'

/** Empty output with one (empty) array per line of `def`. */
export function emptyOutput(def: IndicatorDef): IndicatorOutput {
  const out: IndicatorOutput = {}
  for (const line of def.lines) out[line.key] = []
  return out
}

/** Step `stepper` through `candles`, appending each defined value to `out` at the candle's time. */
export function stepInto(
  def: IndicatorDef,
  stepper: IndicatorStepper,
  candles: readonly Candle[],
  out: IndicatorOutput,
): void {
  for (const c of candles) {
    const values = stepper.next(c)
    for (const line of def.lines) {
      const v = values[line.key]
      if (v !== undefined && Number.isFinite(v)) out[line.key].push({ time: c.time, value: v })
    }
  }
}

/** Full computation over `candles` (ascending). Inputs must already be complete and clamped. */
export function computeIndicator(
  def: IndicatorDef,
  candles: readonly Candle[],
  inputs: IndicatorInputs,
  source: PriceSource = def.source ?? 'close',
): IndicatorOutput {
  const out = emptyOutput(def)
  stepInto(def, def.createStepper(inputs, source), candles, out)
  return out
}

/** Every input of `def` at its default. */
export function defaultInputs(def: IndicatorDef): Record<string, number> {
  const inputs: Record<string, number> = {}
  for (const i of def.inputs) inputs[i.key] = i.default
  return inputs
}
