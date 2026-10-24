import type { Candle } from '../types'

/** Every indicator the registry knows. */
export type IndicatorId = 'sma' | 'ema' | 'bb' | 'vwap' | 'volma' | 'rsi' | 'macd' | 'stoch'

/**
 * Where an indicator draws:
 * - 'overlay': on the price scale of the main pane (SMA, EMA, Bollinger, VWAP).
 * - 'volume': on the volume overlay scale (id '') of the main pane (Volume MA).
 * - 'pane': in its own pane below the price (RSI, MACD, Stochastic).
 */
export type Placement = 'overlay' | 'volume' | 'pane'

/** Which price an indicator reads from each candle. */
export type PriceSource = 'close' | 'open' | 'high' | 'low' | 'hl2' | 'hlc3' | 'ohlc4'

/** Numeric inputs by key, e.g. `{ period: 20 }`. */
export type IndicatorInputs = Readonly<Record<string, number>>

export interface InputDef {
  key: string
  label: string
  default: number
  min: number
  max: number
  step: number
  /** Whole numbers only (periods). */
  integer: boolean
}

export interface LineDef {
  /** Key in `IndicatorValues` / `IndicatorOutput`, e.g. 'upper', 'signal', 'hist'. */
  key: string
  label: string
  kind: 'line' | 'histogram'
  /** Default colour (#rrggbb); the user can override it in the settings dialog. */
  color: string
  lineWidth: 1 | 2
}

/** Horizontal guide line in an indicator pane, e.g. RSI 30/70. */
export interface GuideDef {
  value: number
  label: string
}

/** The value of each line at one bar. A missing key means that line is still warming up. */
export type IndicatorValues = Partial<Record<string, number>>

/**
 * Consumes candles one at a time in ascending time order and returns the values at each bar.
 * Values at bar t depend only on bars <= t (no look-ahead).
 */
export interface IndicatorStepper {
  next(candle: Candle): IndicatorValues
  /** Independent deep copy: stepping either one never affects the other. */
  clone(): IndicatorStepper
}

export interface IndicatorDef {
  id: IndicatorId
  /** Menu name, e.g. "Bollinger Bands". */
  name: string
  /** Legend prefix, e.g. "BB". */
  shortName: string
  /** One line for the add-indicator menu. */
  description: string
  placement: Placement
  inputs: readonly InputDef[]
  /** Default price source, or null when the indicator reads whole candles (VWAP, Stoch, Vol MA). */
  source: PriceSource | null
  lines: readonly LineDef[]
  /** Horizontal guides drawn in the indicator's pane (pane placement only). */
  guides: readonly GuideDef[]
  /** Fixed pane scale (e.g. RSI 0..100); omitted = autoscale. */
  range?: { min: number; max: number }
  /** `inputs` always holds every key in `this.inputs` (defaults filled in, clamped). */
  createStepper(inputs: IndicatorInputs, source: PriceSource): IndicatorStepper
}

/** One output point. `time` is the candle's own open time in UNIX seconds. */
export interface IndicatorPoint {
  time: number
  value: number
}

/** Points per line key; warm-up bars have no point (never 0 or NaN). */
export type IndicatorOutput = Record<string, IndicatorPoint[]>
