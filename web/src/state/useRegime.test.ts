import { describe, expect, it } from 'vitest'
import {
  REGIME_DEBOUNCE_MS,
  REGIME_MAX_GAP_MS,
  regimeRequestDelay,
  regimeSupported,
} from './useRegime'

describe('regimeSupported', () => {
  it('is off on seconds timeframes and on for minutes and longer', () => {
    expect(regimeSupported('1s')).toBe(false)
    expect(regimeSupported('5s')).toBe(false)
    expect(regimeSupported('30s')).toBe(false)
    expect(regimeSupported('1m')).toBe(true)
    expect(regimeSupported('4h')).toBe(true)
    expect(regimeSupported('1d')).toBe(true)
  })
})

describe('regimeRequestDelay', () => {
  const now = 1_700_000_000_000
  const H = 3600
  /** The previous request: `ago` ms back, newest closed bar at `newest`. */
  const last = (ago: number, newest: number) => ({ at: now - ago, newest })
  const T = 1_700_000_000

  it('waits only the debounce for the first request, also after re-enabling', () => {
    expect(regimeRequestDelay(null, now, T, 60)).toBe(REGIME_DEBOUNCE_MS)
    expect(regimeRequestDelay(null, now, T, H)).toBe(REGIME_DEBOUNCE_MS)
  })

  it('spaces a bar close at least one bar after the previous request', () => {
    // 1m bars, a new bar closed, last request 10 s ago: wait the remaining 50 s.
    expect(regimeRequestDelay(last(10_000, T), now, T + 60, 60)).toBe(50_000)
    // 1h bars cap at a minute: 20 s ago -> 40 s.
    expect(REGIME_MAX_GAP_MS).toBe(60_000)
    expect(regimeRequestDelay(last(20_000, T), now, T + H, H)).toBe(40_000)
    // A bar has passed since: only the debounce.
    expect(regimeRequestDelay(last(61_000, T), now, T + 60, 60)).toBe(REGIME_DEBOUNCE_MS)
  })

  it('does not throttle a history prepend or a corrected close (newest bar unchanged)', () => {
    // 1h chart, loadOlder 5 s after the last request: the newest closed bar is the same.
    expect(regimeRequestDelay(last(5_000, T), now, T, H)).toBe(REGIME_DEBOUNCE_MS)
    // A replaced window whose newest bar is older does not wait either.
    expect(regimeRequestDelay(last(5_000, T), now, T - H, H)).toBe(REGIME_DEBOUNCE_MS)
  })

  it('never goes below the debounce, also without a bar length or a closed bar', () => {
    expect(regimeRequestDelay(last(100, T), now, T + 60)).toBe(REGIME_DEBOUNCE_MS)
    expect(regimeRequestDelay(last(100, T), now, T + 60, Number.NaN)).toBe(REGIME_DEBOUNCE_MS)
    expect(regimeRequestDelay(last(100, T), now, null, 60)).toBe(REGIME_DEBOUNCE_MS)
  })
})
