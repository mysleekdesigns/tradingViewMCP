// Data conflation (Lightweight Charts 5.1+): when bars are drawn closer together than the
// screen can resolve, the chart merges neighbours into one drawn bar (power-of-2 groups, up to
// 512). It is render-only: series data, indicator values, the crosshair and the legend still
// see every bar. Indicators compute on the real candles in IndicatorRunner, never on what the
// chart draws (conflation.indicators.test.ts pins that).
//
// All of these are TIME SCALE options (`HorzScaleOptions` in LWC 5.2.1's typings, next to
// `ignoreWhitespaceIndices`), so they go to the chart as `{ timeScale: { ... } }`:
// `chart.applyOptions(conflationChartOptions(candles.length, interval))` or
// `chart.timeScale().applyOptions(conflationOptions(candles.length, interval))`.
//
// How LWC 5.2.1 decides (lightweight-charts.development.mjs, `_updateConflationFactor`):
// factor = 2^floor(log2(threshold / barSpacing)) with threshold = conflationThresholdFactor /
// devicePixelRatio, and nothing is merged while barSpacing >= threshold. The chart can never
// zoom out past `minBarSpacing`, whose default (0.5 px) is exactly the threshold on a 2x
// (retina) screen: with the default, conflation would never engage there, and a 1200 px chart
// could show only ~2,400 bars at full zoom-out. So turning conflation on also lowers
// `minBarSpacing`, far enough for the whole history to fit in FIT_WIDTH_PX.
import type { DeepPartial, HorzScaleOptions, TimeChartOptions } from 'lightweight-charts'
import type { Timeframe } from '../types'
import { isNativeInterval, isSecondsTimeframe } from '../data/timeframes'

/**
 * Bars at which conflation turns on for native intervals. 20,000 bars at LWC's default
 * minimum spacing (0.5 px) need 10,000 px, so below this the chart cannot zoom out far enough
 * for merging to matter, and LWC's own docs recommend conflation from ~10k points. It is also
 * the old chart history cap (MAX_HISTORY_BARS): every history the chart could hold before
 * Phase 14 renders exactly as it did.
 */
export const CONFLATION_MIN_BARS = 20_000

/**
 * Bars at which conflation levels are precomputed after each `setData` (LWC: +100–500 ms load
 * in a background task, +20–50% memory, 10–100x faster zoom-out). Worth it only for histories
 * well past the turn-on point; below this, on-demand merging is fast enough.
 */
export const CONFLATION_PRECOMPUTE_MIN_BARS = 50_000

/**
 * Most bars (raw, traded or not) scroll-back paging grows a chart's history to. 150,000 1m bars
 * is ~104 days; with conflation on, a full zoom-out draws at most a few thousand merged bars per
 * series. Memory: ~150k candles plus their LWC copies (candles, volume, each indicator line)
 * stays in the tens of MB per chart. Separate from the Strategy Tester's MAX_HISTORY_BARS
 * (components/backtestView.ts), which caps backtest history loads.
 */
export const CHART_MAX_BARS = 150_000

/** LWC's default `minBarSpacing` (px), used whenever conflation is off. */
export const DEFAULT_MIN_BAR_SPACING = 0.5

/** With conflation on, a full zoom-out fits the whole history into about this many px. */
export const FIT_WIDTH_PX = 1_000

/**
 * Smallest `minBarSpacing` ever set: 0.5 / 512, i.e. LWC's deepest conflation level (512 bars
 * per drawn bar) on a 2x screen. Fitting even more bars per pixel would draw nothing new.
 */
export const MIN_BAR_SPACING_FLOOR = DEFAULT_MIN_BAR_SPACING / 512

/** The time scale options this module sets (all keys always present, so off really resets). */
export interface ConflationOptions {
  enableConflation: boolean
  /**
   * 1.0 = merge only when the screen cannot show the detail. Higher values smooth harder and
   * make candles look wrong (LWC recommends > 1 only for line/area series), so it stays 1.
   */
  conflationThresholdFactor: number
  precomputeConflationOnInit: boolean
  precomputeConflationPriority: HorzScaleOptions['precomputeConflationPriority']
  /** See the header comment: without lowering it, conflation cannot engage on 2x screens. */
  minBarSpacing: number
}

/** Seconds timeframes and custom (aggregated) ones: always conflation-capable. */
export function conflationTimeframe(timeframe: Timeframe): boolean {
  return isSecondsTimeframe(timeframe) || !isNativeInterval(timeframe)
}

/**
 * Smallest bar spacing that still fits `barCount` bars into FIT_WIDTH_PX, rounded DOWN to
 * 0.5 / 2^k so it only changes when the bar count crosses a power of two (a few applyOptions
 * calls per history, not one per page or live bar). Clamped to [MIN_BAR_SPACING_FLOOR, 0.5].
 */
export function conflationMinBarSpacing(barCount: number): number {
  if (!Number.isFinite(barCount) || barCount <= 0) return DEFAULT_MIN_BAR_SPACING
  const fit = FIT_WIDTH_PX / barCount
  if (fit >= DEFAULT_MIN_BAR_SPACING) return DEFAULT_MIN_BAR_SPACING
  const k = Math.ceil(Math.log2(DEFAULT_MIN_BAR_SPACING / fit))
  return Math.max(MIN_BAR_SPACING_FLOOR, DEFAULT_MIN_BAR_SPACING / 2 ** k)
}

/**
 * Time scale conflation options for a chart holding `barCount` candles on `timeframe`.
 * On for long histories (>= CONFLATION_MIN_BARS) and for seconds/custom timeframes; precompute
 * only for very long histories (>= CONFLATION_PRECOMPUTE_MIN_BARS). Pure; cheap enough to call
 * on every render, but apply it only when the result changes (`sameConflation`).
 */
export function conflationOptions(barCount: number, timeframe: Timeframe): ConflationOptions {
  const on = barCount >= CONFLATION_MIN_BARS || conflationTimeframe(timeframe)
  return {
    enableConflation: on,
    conflationThresholdFactor: 1,
    precomputeConflationOnInit: on && barCount >= CONFLATION_PRECOMPUTE_MIN_BARS,
    precomputeConflationPriority: 'background',
    minBarSpacing: on ? conflationMinBarSpacing(barCount) : DEFAULT_MIN_BAR_SPACING,
  }
}

/** The same options wrapped for `chart.applyOptions` (they live under `timeScale`). */
export function conflationChartOptions(
  barCount: number,
  timeframe: Timeframe,
): DeepPartial<TimeChartOptions> {
  return { timeScale: conflationOptions(barCount, timeframe) }
}

/** True when applying `b` after `a` would change nothing. */
export function sameConflation(a: ConflationOptions, b: ConflationOptions): boolean {
  return (
    a.enableConflation === b.enableConflation &&
    a.conflationThresholdFactor === b.conflationThresholdFactor &&
    a.precomputeConflationOnInit === b.precomputeConflationOnInit &&
    a.precomputeConflationPriority === b.precomputeConflationPriority &&
    a.minBarSpacing === b.minBarSpacing
  )
}
