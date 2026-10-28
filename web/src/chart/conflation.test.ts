import type { DeepPartial, HorzScaleOptions, TimeChartOptions } from 'lightweight-charts'
import { describe, expect, it } from 'vitest'
import type { Timeframe } from '../types'
import {
  CHART_MAX_BARS,
  CONFLATION_MIN_BARS,
  CONFLATION_PRECOMPUTE_MIN_BARS,
  DEFAULT_MIN_BAR_SPACING,
  FIT_WIDTH_PX,
  MIN_BAR_SPACING_FLOOR,
  conflationChartOptions,
  conflationMinBarSpacing,
  conflationOptions,
  conflationTimeframe,
  sameConflation,
} from './conflation'

/** LWC 5.2.1's conflation factor (`_updateConflationFactor`), for checking what the chart does. */
function lwcConflationFactor(barSpacing: number, dpr: number, thresholdFactor = 1): number {
  const threshold = (1 / dpr) * thresholdFactor
  if (barSpacing >= threshold) return 1
  return Math.min(2 ** Math.floor(Math.log2(threshold / barSpacing)), 512)
}

describe('conflationOptions', () => {
  it('stays off for short native-interval histories, with LWC defaults', () => {
    for (const tf of ['1m', '1h', '1d'] as Timeframe[]) {
      expect(conflationOptions(500, tf)).toEqual({
        enableConflation: false,
        conflationThresholdFactor: 1,
        precomputeConflationOnInit: false,
        precomputeConflationPriority: 'background',
        minBarSpacing: DEFAULT_MIN_BAR_SPACING,
      })
      expect(conflationOptions(CONFLATION_MIN_BARS - 1, tf).enableConflation).toBe(false)
    }
  })

  it('turns on at CONFLATION_MIN_BARS bars on any timeframe', () => {
    for (const tf of ['1m', '4h', '1w'] as Timeframe[]) {
      const o = conflationOptions(CONFLATION_MIN_BARS, tf)
      expect(o.enableConflation).toBe(true)
      expect(o.precomputeConflationOnInit).toBe(false)
    }
  })

  it('is always on for seconds and custom (aggregated) timeframes', () => {
    for (const tf of [
      '1s',
      '5s',
      '30s',
      '2m',
      '3m',
      '90m',
      '10h',
      '2d',
      '3d',
      '2w',
    ] as Timeframe[]) {
      expect(conflationTimeframe(tf)).toBe(true)
      expect(conflationOptions(0, tf).enableConflation).toBe(true)
      expect(conflationOptions(300, tf).enableConflation).toBe(true)
    }
    // The app's native (exchange) intervals; everything else is aggregated.
    for (const tf of ['1m', '5m', '15m', '1h', '4h', '1d', '1w'] as Timeframe[]) {
      expect(conflationTimeframe(tf)).toBe(false)
    }
  })

  it('precomputes only for very long histories', () => {
    expect(
      conflationOptions(CONFLATION_PRECOMPUTE_MIN_BARS - 1, '1m').precomputeConflationOnInit,
    ).toBe(false)
    expect(conflationOptions(CONFLATION_PRECOMPUTE_MIN_BARS, '1m').precomputeConflationOnInit).toBe(
      true,
    )
    expect(conflationOptions(100_000, '1s').precomputeConflationOnInit).toBe(true)
    // Short seconds charts conflate on demand without paying for precompute.
    expect(conflationOptions(5_000, '1s')).toMatchObject({
      enableConflation: true,
      precomputeConflationOnInit: false,
    })
  })

  it('keeps the threshold factor at 1 (candles look wrong when smoothed)', () => {
    for (const n of [0, 1_000, 20_000, 100_000, CHART_MAX_BARS]) {
      expect(conflationOptions(n, '1m').conflationThresholdFactor).toBe(1)
      expect(conflationOptions(n, '5s').conflationThresholdFactor).toBe(1)
    }
  })

  it('orders the thresholds sensibly', () => {
    expect(CONFLATION_MIN_BARS).toBeLessThan(CONFLATION_PRECOMPUTE_MIN_BARS)
    expect(CONFLATION_PRECOMPUTE_MIN_BARS).toBeLessThan(CHART_MAX_BARS)
    // The Phase 14 check is a 1m chart with 100k+ bars: the chart must be able to hold them.
    expect(CHART_MAX_BARS).toBeGreaterThanOrEqual(100_000)
  })

  it('lives under timeScale and type-checks as chart and time scale options', () => {
    const scale: DeepPartial<HorzScaleOptions> = conflationOptions(100_000, '1m')
    const chart: DeepPartial<TimeChartOptions> = conflationChartOptions(100_000, '1m')
    expect(chart).toEqual({ timeScale: scale })
    expect(Object.keys(chart)).toEqual(['timeScale'])
  })
})

describe('conflationMinBarSpacing', () => {
  it('is the LWC default when the history already fits, or for bad counts', () => {
    expect(conflationMinBarSpacing(0)).toBe(DEFAULT_MIN_BAR_SPACING)
    expect(conflationMinBarSpacing(-5)).toBe(DEFAULT_MIN_BAR_SPACING)
    expect(conflationMinBarSpacing(Number.NaN)).toBe(DEFAULT_MIN_BAR_SPACING)
    expect(conflationMinBarSpacing(FIT_WIDTH_PX * 2)).toBe(DEFAULT_MIN_BAR_SPACING)
  })

  it('fits the whole history into FIT_WIDTH_PX, as 0.5 / 2^k', () => {
    for (const n of [2_001, 4_000, 20_000, 100_000, 150_000, 300_000]) {
      const s = conflationMinBarSpacing(n)
      expect(s * n).toBeLessThanOrEqual(FIT_WIDTH_PX)
      // Rounded down to a power of two, so at most 2x tighter than needed.
      expect(s * n * 2).toBeGreaterThan(FIT_WIDTH_PX)
      expect(Number.isInteger(Math.log2(DEFAULT_MIN_BAR_SPACING / s))).toBe(true)
    }
    expect(conflationMinBarSpacing(100_000)).toBe(0.5 / 64)
  })

  it('changes only when the bar count crosses a power of two (few applyOptions calls)', () => {
    const seen = new Set<number>()
    for (let n = 20_000; n <= CHART_MAX_BARS; n += 1_000) seen.add(conflationMinBarSpacing(n))
    expect(seen.size).toBeLessThanOrEqual(4)
  })

  it('never goes below LWC deepest level (512) on a 2x screen', () => {
    expect(conflationMinBarSpacing(10_000_000)).toBe(MIN_BAR_SPACING_FLOOR)
    expect(MIN_BAR_SPACING_FLOOR).toBe(0.5 / 512)
  })

  it('lets LWC actually conflate a 100k-bar 1m chart at full zoom-out, on 1x and 2x screens', () => {
    const o = conflationOptions(100_000, '1m')
    // With LWC's default min spacing nothing is merged on a 2x screen: the reason for the change.
    expect(lwcConflationFactor(DEFAULT_MIN_BAR_SPACING, 2)).toBe(1)
    expect(lwcConflationFactor(o.minBarSpacing, 2)).toBe(64)
    expect(lwcConflationFactor(o.minBarSpacing, 1)).toBe(128)
    // 100k bars drawn as at most ~1.6k merged bars per series.
    expect(100_000 / lwcConflationFactor(o.minBarSpacing, 2)).toBeLessThan(1_600)
  })
})

describe('sameConflation', () => {
  it('detects changes in every field', () => {
    const a = conflationOptions(100_000, '1m')
    expect(sameConflation(a, { ...a })).toBe(true)
    expect(sameConflation(a, conflationOptions(100_500, '1m'))).toBe(true)
    expect(sameConflation(a, { ...a, enableConflation: false })).toBe(false)
    expect(sameConflation(a, { ...a, conflationThresholdFactor: 2 })).toBe(false)
    expect(sameConflation(a, { ...a, precomputeConflationOnInit: false })).toBe(false)
    expect(sameConflation(a, { ...a, precomputeConflationPriority: 'user-visible' })).toBe(false)
    expect(sameConflation(a, { ...a, minBarSpacing: 0.5 })).toBe(false)
  })
})
