import { describe, expect, it } from 'vitest'
import {
  evaluateTick,
  formatAlertMessage,
  formatPrice,
  initialState,
  isValidPrice,
  type AlertState,
  type EvalInput,
} from '../src/alerts/evaluate.ts'
import type { AlertCondition, AlertFrequency } from '../src/alerts/types.ts'

const COOLDOWN_MS = 60_000

/** Feeds `prices` one per second from t=0 and returns the indices that fired. */
function run(
  condition: AlertCondition,
  level: number,
  prices: number[],
  frequency: AlertFrequency = 'every',
  opts: { stepMs?: number; cooldownMs?: number; state?: AlertState } = {},
): number[] {
  const input: EvalInput = { condition, level, frequency }
  let state: AlertState | undefined = opts.state
  const fired: number[] = []
  prices.forEach((p, i) => {
    const r = evaluateTick(input, state, p, i * (opts.stepMs ?? 1000), opts.cooldownMs ?? 0)
    state = r.state
    if (r.fired) fired.push(i)
  })
  return fired
}

describe('evaluateTick: crosses_above', () => {
  it('fires when prev < level and cur >= level', () => {
    expect(run('crosses_above', 100, [99, 101])).toEqual([1])
  })
  it('fires on exact equality with the level (cur === level)', () => {
    expect(run('crosses_above', 100, [99, 100])).toEqual([1])
  })
  it('does not fire when prev === level (already at the level)', () => {
    expect(run('crosses_above', 100, [100, 101])).toEqual([])
  })
  it('never fires on the first observation, even above the level', () => {
    expect(run('crosses_above', 100, [150])).toEqual([])
    expect(run('crosses_above', 100, [150, 160])).toEqual([])
  })
  it('does not fire while staying below or moving down', () => {
    expect(run('crosses_above', 100, [90, 95, 99.99, 98])).toEqual([])
    expect(run('crosses_above', 100, [110, 99])).toEqual([])
  })
  it('fires again on each new upward cross (every, no cooldown)', () => {
    expect(run('crosses_above', 100, [99, 101, 99, 101])).toEqual([1, 3])
  })
})

describe('evaluateTick: crosses_below', () => {
  it('fires when prev > level and cur <= level', () => {
    expect(run('crosses_below', 100, [101, 99])).toEqual([1])
  })
  it('fires on exact equality with the level', () => {
    expect(run('crosses_below', 100, [101, 100])).toEqual([1])
  })
  it('does not fire when prev === level', () => {
    expect(run('crosses_below', 100, [100, 99])).toEqual([])
  })
  it('never fires on the first observation', () => {
    expect(run('crosses_below', 100, [50])).toEqual([])
  })
  it('does not fire on an upward move', () => {
    expect(run('crosses_below', 100, [99, 101])).toEqual([])
  })
})

describe('evaluateTick: greater / less (edge-triggered)', () => {
  it('greater fires on the first observation when already true', () => {
    expect(run('greater', 100, [101])).toEqual([0])
  })
  it('greater is strict: equality is not greater, and does re-arm', () => {
    expect(run('greater', 100, [100])).toEqual([])
    expect(run('greater', 100, [101, 100, 101])).toEqual([0, 2])
  })
  it('greater fires once while it stays true, re-arms when false', () => {
    expect(run('greater', 100, [101, 102, 103, 99, 104, 105])).toEqual([0, 4])
  })
  it('less fires on first observation when true and is strict at the level', () => {
    expect(run('less', 100, [99])).toEqual([0])
    expect(run('less', 100, [100])).toEqual([])
    expect(run('less', 100, [99, 98, 100, 97])).toEqual([0, 3])
  })
  it('less does not fire while false', () => {
    expect(run('less', 100, [101, 150, 100])).toEqual([])
  })
})

describe('evaluateTick: frequency and cooldown', () => {
  it('every: a cross inside the cooldown is dropped; after it, the next cross fires', () => {
    // 1 tick per 10 s, cooldown 60 s: fire at t=10 s, crosses at 30 s and 50 s suppressed,
    // the cross at 90 s fires (80 s after the first).
    const prices = [99, 101, 99, 101, 99, 101, 99, 99, 99, 101]
    expect(
      run('crosses_above', 100, prices, 'every', { stepMs: 10_000, cooldownMs: 60_000 }),
    ).toEqual([1, 9])
  })
  it('every: re-fires exactly when the cooldown has elapsed (boundary inclusive)', () => {
    // fire at 0; re-armed at 30 s; true again at 60 s = exactly cooldown -> fires.
    const r = run('greater', 100, [101, 99, 99, 101], 'every', {
      stepMs: 20_000,
      cooldownMs: 60_000,
    })
    expect(r).toEqual([0, 3])
  })
  it('every: a suppressed greater stays armed and fires once the cooldown ends', () => {
    // 0 s fire, 10 s false (re-arm), 20..50 s true but cooling down, 60 s true -> fires.
    const prices = [101, 99, 101, 101, 101, 101, 101, 101]
    expect(run('greater', 100, prices, 'every', { stepMs: 10_000, cooldownMs: 60_000 })).toEqual([
      0, 6,
    ])
  })
  it('once: no cooldown applies (the caller deactivates it after the first fire)', () => {
    const prices = [99, 101, 99, 101]
    expect(run('crosses_above', 100, prices, 'once', { cooldownMs: COOLDOWN_MS })).toEqual([1, 3])
  })
  it('cooldown is seeded from the stored lastTriggeredAt (seconds)', () => {
    // Last trigger at t=0 s; ticks from t=0 every 10 s with cooldown 60 s.
    const state = initialState(0)
    expect(state.lastFiredMs).toBe(0)
    const r = run('greater', 100, [101, 101, 101, 101, 101, 101, 101], 'every', {
      stepMs: 10_000,
      cooldownMs: 60_000,
      state,
    })
    expect(r).toEqual([6])
  })
  it('records lastFiredMs on fire only', () => {
    const input: EvalInput = { condition: 'greater', level: 100, frequency: 'every' }
    const a = evaluateTick(input, undefined, 99, 5_000, COOLDOWN_MS)
    expect(a.state.lastFiredMs).toBeUndefined()
    const b = evaluateTick(input, a.state, 101, 7_000, COOLDOWN_MS)
    expect(b.fired).toBe(true)
    expect(b.state).toEqual({ prev: 101, armed: false, lastFiredMs: 7_000 })
  })
})

describe('evaluateTick: state and invalid prices', () => {
  it('updates prev on every valid tick', () => {
    const input: EvalInput = { condition: 'crosses_above', level: 100, frequency: 'every' }
    const a = evaluateTick(input, undefined, 90, 0, 0)
    expect(a.state.prev).toBe(90)
    const b = evaluateTick(input, a.state, 95, 1, 0)
    expect(b.state.prev).toBe(95)
  })
  it('ignores NaN, Infinity, 0 and negative prices without touching state', () => {
    for (const bad of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY, 0, -5]) {
      expect(run('crosses_above', 100, [99, bad, 101])).toEqual([2])
      expect(run('greater', 100, [bad])).toEqual([])
      const s: AlertState = { prev: 99, armed: true, lastFiredMs: undefined }
      const r = evaluateTick(
        { condition: 'crosses_above', level: 100, frequency: 'once' },
        s,
        bad,
        0,
        0,
      )
      expect(r.fired).toBe(false)
      expect(r.state).toBe(s)
    }
  })
  it('a fresh state (re-arm) forgets the previous price', () => {
    const input: EvalInput = { condition: 'crosses_above', level: 100, frequency: 'every' }
    const a = evaluateTick(input, undefined, 99, 0, 0)
    // Reset: new state, so the next above-level tick is a first observation and cannot cross.
    const b = evaluateTick(input, initialState(), 101, 1, 0)
    expect(a.state.prev).toBe(99)
    expect(b.fired).toBe(false)
  })
  it('isValidPrice', () => {
    expect(isValidPrice(1)).toBe(true)
    expect(isValidPrice(0.00000001)).toBe(true)
    expect(isValidPrice(0)).toBe(false)
    expect(isValidPrice(-1)).toBe(false)
    expect(isValidPrice(Number.NaN)).toBe(false)
    expect(isValidPrice('1')).toBe(false)
  })
})

describe('formatting', () => {
  it('formats prices with en-US grouping, no scientific notation', () => {
    expect(formatPrice(65000)).toBe('65,000')
    expect(formatPrice(65012.5)).toBe('65,012.5')
    expect(formatPrice(1234567.123456789)).toBe('1,234,567.12345679')
    expect(formatPrice(0.5)).toBe('0.5')
    expect(formatPrice(0.000000012345)).toBe('0.000000012345')
    expect(formatPrice(0.00001234567891)).toBe('0.000012345679')
    expect(formatPrice(1e21)).not.toMatch(/e/i)
    expect(formatPrice(1e-9)).not.toMatch(/e/i)
  })
  it('builds the message with lowercase condition labels', () => {
    expect(formatAlertMessage('BTCUSDT', 'crosses_above', 65000, 65012.5)).toBe(
      'BTCUSDT crossing up 65,000 (last 65,012.5)',
    )
    expect(formatAlertMessage('ETHUSDT', 'crosses_below', 3000, 2999.99)).toBe(
      'ETHUSDT crossing down 3,000 (last 2,999.99)',
    )
    expect(formatAlertMessage('SOLUSDT', 'greater', 150, 150.1)).toBe(
      'SOLUSDT greater than 150 (last 150.1)',
    )
    expect(formatAlertMessage('DOGEUSDT', 'less', 0.1, 0.0999)).toBe(
      'DOGEUSDT less than 0.1 (last 0.0999)',
    )
  })
})
