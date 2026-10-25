import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { applyLiveCandle, mergeCandles, prependOlder } from './candleStore'

const c = (time: number, close = time): Candle => ({
  time,
  open: close,
  high: close,
  low: close,
  close,
  volume: 1,
})

function frozen(arr: Candle[]): readonly Candle[] {
  arr.forEach((x) => Object.freeze(x))
  return Object.freeze(arr)
}

describe('applyLiveCandle', () => {
  it('returns [c] for empty input', () => {
    const live = c(100)
    expect(applyLiveCandle([], live)).toEqual([live])
  })

  it('replaces the last candle on equal time, keeping other identities', () => {
    const input = frozen([c(60), c(120), c(180)])
    const live = c(180, 999)
    const out = applyLiveCandle(input, live)
    expect(out).not.toBe(input)
    expect(out).toHaveLength(3)
    expect(out[2]).toBe(live)
    expect(out[0]).toBe(input[0])
    expect(out[1]).toBe(input[1])
    expect(input[2].close).toBe(180)
  })

  it('appends a newer candle', () => {
    const input = frozen([c(60), c(120)])
    const live = c(180)
    const out = applyLiveCandle(input, live)
    expect(out.map((x) => x.time)).toEqual([60, 120, 180])
    expect(out[0]).toBe(input[0])
    expect(out[1]).toBe(input[1])
    expect(input).toHaveLength(2)
  })

  it('replaces an older same-time element', () => {
    const input = frozen([c(60), c(120), c(180), c(240)])
    const live = c(120, 5)
    const out = applyLiveCandle(input, live)
    expect(out).not.toBe(input)
    expect(out[1]).toBe(live)
    expect(out[0]).toBe(input[0])
    expect(out[2]).toBe(input[2])
    expect(out[3]).toBe(input[3])
  })

  it('returns the same reference for an older time not present', () => {
    const input = frozen([c(60), c(180), c(240)])
    expect(applyLiveCandle(input, c(120))).toBe(input)
    expect(applyLiveCandle(input, c(0))).toBe(input)
  })

  it('returns the same reference when the same-time bar already has identical fields', () => {
    const input = frozen([c(60), c(120), c(180)])
    // A fresh object with equal fields (a repeated read-back or a matching gap row).
    expect(applyLiveCandle(input, { ...c(180) })).toBe(input)
    expect(applyLiveCandle(input, { ...c(120) })).toBe(input)
    // Any differing field still copies and replaces.
    for (const field of ['open', 'high', 'low', 'close', 'volume'] as const) {
      const changed = { ...c(120), [field]: 999 }
      const out = applyLiveCandle(input, changed)
      expect(out).not.toBe(input)
      expect(out[1]).toBe(changed)
    }
  })
})

describe('mergeCandles', () => {
  it('unions by time, fresh wins, ascending and unique', () => {
    const existing = frozen([c(60), c(120), c(180)])
    const fresh = frozen([c(240), c(180, 7), c(300)])
    const out = mergeCandles(existing, fresh)
    expect(out.map((x) => x.time)).toEqual([60, 120, 180, 240, 300])
    expect(out[2]).toBe(fresh[1])
    expect(out[0]).toBe(existing[0])
    expect(out[1]).toBe(existing[1])
  })

  it('handles empty inputs and fresh-before-existing', () => {
    const a = frozen([c(60)])
    expect(mergeCandles([], a)).toEqual([a[0]])
    expect(mergeCandles(a, [])[0]).toBe(a[0])
    expect(mergeCandles([], [])).toEqual([])
    const out = mergeCandles(frozen([c(300), c(360)]), frozen([c(60), c(120)]))
    expect(out.map((x) => x.time)).toEqual([60, 120, 300, 360])
  })

  it('dedupes duplicates within fresh (later wins)', () => {
    const fresh = frozen([c(60, 1), c(60, 2)])
    const out = mergeCandles([], fresh)
    expect(out).toHaveLength(1)
    expect(out[0]).toBe(fresh[1])
  })
})

describe('prependOlder', () => {
  it('prepends only rows strictly older than the first bar, keeping existing identity', () => {
    const existing = frozen([c(300), c(360), c(420)])
    const older = frozen([c(120), c(180), c(240), c(300, 9), c(360, 9)])
    const out = prependOlder(existing, older)
    expect(out.map((x) => x.time)).toEqual([120, 180, 240, 300, 360, 420])
    expect(out[0]).toBe(older[0])
    expect(out[3]).toBe(existing[0])
    expect(out[4]).toBe(existing[1])
    expect(out[5]).toBe(existing[2])
    expect(existing).toHaveLength(3)
  })

  it('returns the same reference when no row is older', () => {
    const existing = frozen([c(300), c(360)])
    expect(prependOlder(existing, [])).toBe(existing)
    expect(prependOlder(existing, frozen([c(300), c(360), c(420)]))).toBe(existing)
  })

  it('sorts and dedupes the older page (later duplicate wins)', () => {
    const older = frozen([c(180), c(60, 1), c(120), c(60, 2)])
    const out = prependOlder(frozen([c(300)]), older)
    expect(out.map((x) => x.time)).toEqual([60, 120, 180, 300])
    expect(out[0]).toBe(older[3])
  })

  it('with empty existing returns the sorted older rows', () => {
    const out = prependOlder([], frozen([c(120), c(60)]))
    expect(out.map((x) => x.time)).toEqual([60, 120])
  })
})
