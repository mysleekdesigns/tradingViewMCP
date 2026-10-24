import { describe, expect, it } from 'vitest'
import {
  INTERVAL_SECONDS,
  TimeIndex,
  logicalToTime,
  snapLogicalToTime,
  timeToLogical,
} from './timeIndex'

// Hourly bars with a missing bar (a gap) between index 2 and 3.
const H = 3_600
const bars = [{ time: 0 }, { time: H }, { time: 2 * H }, { time: 4 * H }, { time: 5 * H }]

describe('timeToLogical', () => {
  it('maps exact bar times to integer indices', () => {
    bars.forEach((b, i) => expect(timeToLogical(bars, b.time, H)).toBe(i))
  })

  it('interpolates times between two bars', () => {
    expect(timeToLogical(bars, H / 2, H)).toBe(0.5)
    // Across the gap: 3H sits halfway between index 2 (2H) and index 3 (4H).
    expect(timeToLogical(bars, 3 * H, H)).toBe(2.5)
    // A 15m anchor on 1h bars.
    expect(timeToLogical(bars, H + 900, H)).toBe(1.25)
  })

  it('extrapolates past the last bar with the interval length', () => {
    expect(timeToLogical(bars, 7 * H, H)).toBe(6)
    expect(timeToLogical(bars, 5 * H + 1_800, H)).toBe(4.5)
  })

  it('extrapolates before the first bar with the interval length', () => {
    expect(timeToLogical(bars, -2 * H, H)).toBe(-2)
    expect(timeToLogical(bars, -H / 4, H)).toBe(-0.25)
  })

  it('returns null for an empty series', () => {
    expect(timeToLogical([], 123, H)).toBeNull()
  })

  it('handles a one-bar series by extrapolating both ways', () => {
    const one = [{ time: 1_000 }]
    expect(timeToLogical(one, 1_000, 60)).toBe(0)
    expect(timeToLogical(one, 1_120, 60)).toBe(2)
    expect(timeToLogical(one, 940, 60)).toBe(-1)
  })

  it('rejects a non-positive step', () => {
    expect(timeToLogical(bars, 0, 0)).toBeNull()
  })
})

describe('logicalToTime', () => {
  it('maps integer indices to bar open times', () => {
    bars.forEach((b, i) => expect(logicalToTime(bars, i, H)).toBe(b.time))
  })

  it('is the inverse of timeToLogical between bars, across gaps and outside the data', () => {
    for (const t of [H / 2, 3 * H, H + 900, 7 * H, -2 * H, -900, 5 * H + 60]) {
      const l = timeToLogical(bars, t, H)
      expect(l).not.toBeNull()
      expect(logicalToTime(bars, l as number, H)).toBe(t)
    }
  })

  it('extrapolates bar times beyond either end', () => {
    expect(logicalToTime(bars, 6, H)).toBe(7 * H)
    expect(logicalToTime(bars, -3, H)).toBe(-3 * H)
  })

  it('returns null for an empty series', () => {
    expect(logicalToTime([], 0, H)).toBeNull()
  })

  it('works on a one-bar series', () => {
    const one = [{ time: 1_000 }]
    expect(logicalToTime(one, 0, 60)).toBe(1_000)
    expect(logicalToTime(one, 3, 60)).toBe(1_180)
    expect(logicalToTime(one, -1, 60)).toBe(940)
  })

  it('rounds to whole seconds', () => {
    const t = logicalToTime([{ time: 0 }, { time: 7 }], 1 / 3, 7)
    expect(Number.isInteger(t)).toBe(true)
  })
})

describe('snapLogicalToTime', () => {
  it('snaps to the nearest bar open time', () => {
    expect(snapLogicalToTime(bars, 1.4, H)).toBe(H)
    expect(snapLogicalToTime(bars, 2.6, H)).toBe(4 * H)
  })

  it('snaps to extrapolated bar times past the end', () => {
    expect(snapLogicalToTime(bars, 6.3, H)).toBe(7 * H)
  })

  it('returns null for non-finite input', () => {
    expect(snapLogicalToTime(bars, Number.NaN, H)).toBeNull()
  })
})

describe('TimeIndex', () => {
  it('tracks swapped bar arrays and steps without copying', () => {
    const index = new TimeIndex()
    expect(index.timeToLogical(0)).toBeNull()
    index.setBars(bars)
    index.setStep(H)
    expect(index.length).toBe(5)
    expect(index.timeToLogical(3 * H)).toBe(2.5)
    expect(index.snapLogicalToTime(0.6)).toBe(H)
    index.setStep(INTERVAL_SECONDS['1m'])
    expect(index.timeToLogical(5 * H + 120)).toBe(6)
  })

  it('has a step for every interval', () => {
    expect(INTERVAL_SECONDS['1h']).toBe(3_600)
    expect(INTERVAL_SECONDS['1w']).toBe(7 * 86_400)
  })
})

describe('cross-interval anchors', () => {
  // Anchors drawn on one interval and shown on another. Times are UNIX seconds from 0.
  const M15 = INTERVAL_SECONDS['15m']
  const H1 = INTERVAL_SECONDS['1h']
  // 15m bars 00:00 .. 01:45 (8 bars, indices 0..7).
  const bars15 = Array.from({ length: 8 }, (_, i) => ({ time: i * M15 }))
  // 1h bars 00:00 .. 03:00 (4 bars, indices 0..3).
  const bars1h = Array.from({ length: 4 }, (_, i) => ({ time: i * H1 }))

  it('maps 1h-bar anchors onto the matching 15m bars', () => {
    // 00:00 is 15m bar 0, 01:00 is 15m bar 4.
    expect(timeToLogical(bars15, 0, M15)).toBe(0)
    expect(timeToLogical(bars15, H1, M15)).toBe(4)
    expect(logicalToTime(bars15, 0, M15)).toBe(0)
    expect(logicalToTime(bars15, 4, M15)).toBe(H1)
  })

  it('maps 15m anchors onto fractional indices between 1h bars', () => {
    // 00:15 → 0.25, 00:45 → 0.75, 02:15 → 2.25 on hourly bars.
    expect(timeToLogical(bars1h, M15, H1)).toBe(0.25)
    expect(timeToLogical(bars1h, 3 * M15, H1)).toBe(0.75)
    expect(timeToLogical(bars1h, 2 * H1 + M15, H1)).toBe(2.25)
    expect(logicalToTime(bars1h, 0.25, H1)).toBe(M15)
    expect(logicalToTime(bars1h, 0.75, H1)).toBe(3 * M15)
    expect(logicalToTime(bars1h, 2.25, H1)).toBe(2 * H1 + M15)
  })

  it('extrapolates a 1h anchor past the last 15m bar with the 15m step', () => {
    // Last 15m bar is 01:45 (index 7); 02:00 is one 15m step later, 03:00 five.
    expect(timeToLogical(bars15, 2 * H1, M15)).toBe(8)
    expect(timeToLogical(bars15, 3 * H1, M15)).toBe(12)
    expect(logicalToTime(bars15, 8, M15)).toBe(2 * H1)
    expect(logicalToTime(bars15, 12, M15)).toBe(3 * H1)
  })

  it('extrapolates a 15m anchor past the last 1h bar with the 1h step', () => {
    // Last 1h bar is 03:00 (index 3); 03:15 is a quarter bar later, 04:30 one and a half.
    expect(timeToLogical(bars1h, 3 * H1 + M15, H1)).toBe(3.25)
    expect(timeToLogical(bars1h, 4 * H1 + 2 * M15, H1)).toBe(4.5)
    expect(logicalToTime(bars1h, 3.25, H1)).toBe(3 * H1 + M15)
    expect(logicalToTime(bars1h, 4.5, H1)).toBe(4 * H1 + 2 * M15)
  })

  it('snaps a 15m time on hourly bars to the nearest hour', () => {
    expect(snapLogicalToTime(bars1h, timeToLogical(bars1h, 3 * M15, H1) as number, H1)).toBe(H1)
    expect(snapLogicalToTime(bars1h, timeToLogical(bars1h, M15, H1) as number, H1)).toBe(0)
  })
})
