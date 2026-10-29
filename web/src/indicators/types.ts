import type { Candle } from '../types'

/** Every indicator the registry knows. */
export type IndicatorId =
  | 'sma'
  | 'ema'
  | 'bb'
  | 'vwap'
  | 'avwap'
  | 'vprofile'
  | 'mstructure'
  | 'volma'
  | 'rsi'
  | 'macd'
  | 'stoch'

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
  /**
   * 'time': a bar open time in UNIX seconds, edited as a UTC date and time; 0 means "not set".
   * 'toggle': an on/off switch stored as 1 / 0 (integer, min 0, max 1), edited as a checkbox.
   * Omitted = 'number'.
   */
  kind?: 'number' | 'time' | 'toggle'
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
  /**
   * Range indicators (fixed-range volume profile): one result over a time range, drawn by a
   * primitive instead of per-bar lines (`lines` is empty, the stepper returns {}). Reads only
   * bars inside the range, so bars after it never change the result. Null when the range is
   * not set or holds no volume.
   */
  profile?(candles: readonly Candle[], inputs: IndicatorInputs): VolumeProfile | null
  /**
   * Event indicators (market structure): swings, BOS/ChoCh events and flip zones, drawn by
   * markers and a primitive instead of per-bar lines (`lines` is empty, the stepper returns {}).
   * `closed` holds closed bars only (never the forming bar), ascending. Everything dated at bar t
   * uses only bars <= t.
   */
  structure?(closed: readonly Candle[], inputs: IndicatorInputs): MarketStructure
}

/** Trend state from closes through confirmed swings; 'none' until the first break. */
export type StructureState = 'none' | 'up' | 'down'

/** A confirmed fractal pivot. Known only from `confirmTime` (pivot bar + `right` bars). */
export interface SwingPoint {
  kind: 'high' | 'low'
  /** Open time (UNIX s) of the pivot bar. */
  pivotTime: number
  /** Open time of the bar whose close confirms it (`right` bars after the pivot). */
  confirmTime: number
  /** The pivot bar's high (kind 'high') or low (kind 'low'). */
  price: number
  /** Open time of the bar whose close broke it, or null while unbroken. */
  brokenTime: number | null
}

/** A close through the last confirmed swing. */
export interface StructureEvent {
  /** 'bos' continues the structure (or starts it from 'none'); 'choch' flips it. */
  kind: 'bos' | 'choch'
  /** 'up' = close above a swing high, 'down' = close below a swing low. */
  direction: 'up' | 'down'
  /** Open time of the breaking bar (the bar whose close went through `level`). */
  time: number
  /** The broken swing's price. */
  level: number
  /** Pivot time of the broken swing. */
  swingPivotTime: number
}

/** A broken swing turned into a zone: broken high -> support, broken low -> resistance. */
export interface FlipZone {
  side: 'support' | 'resistance'
  /** Band edges: the pivot bar's wick extreme and its body edge on that side. top >= bottom. */
  top: number
  bottom: number
  /** Pivot time of the swing the zone comes from. */
  pivotTime: number
  /** Open time of the breaking bar (the zone exists from its close). */
  createdTime: number
  /** Open time of the first later bar that traded into the zone, or null. */
  retestTime: number | null
  /** Open time of the bar that ended the zone, or null while active. */
  endTime: number | null
  /** 'invalidated': a close through the far side; 'dropped': pushed out by `maxZones`. */
  endReason: 'invalidated' | 'dropped' | null
}

/** Market structure over a run of closed bars. Arrays are ascending by their main time. */
export interface MarketStructure {
  /** By confirmTime. */
  swings: SwingPoint[]
  /** By time. */
  events: StructureEvent[]
  /** By createdTime. */
  zones: FlipZone[]
  /** Structure after the last processed bar. */
  state: StructureState
}

/** One price bucket of a volume profile; `low` inclusive, `high` exclusive (the top row inclusive). */
export interface VolumeProfileRow {
  low: number
  high: number
  volume: number
  /** Volume of bars that closed at or above their open. */
  upVolume: number
}

/** Fixed-range volume profile over the bars with `from <= time <= to`. */
export interface VolumeProfile {
  /** Open time (UNIX s) of the first and last bar used. */
  from: number
  to: number
  /** Ascending by price, equal heights, covering [lowest low, highest high] of the range. */
  rows: VolumeProfileRow[]
  /** Point of control: middle price of the row with the most volume (lowest such row on ties). */
  poc: number
  /** Value area high / low: top of the highest and bottom of the lowest row in the value area. */
  vah: number
  val: number
  totalVolume: number
}

/** One output point. `time` is the candle's own open time in UNIX seconds. */
export interface IndicatorPoint {
  time: number
  value: number
}

/** Points per line key; warm-up bars have no point (never 0 or NaN). */
export type IndicatorOutput = Record<string, IndicatorPoint[]>
