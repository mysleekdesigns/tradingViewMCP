import { describe, expect, it } from 'vitest'
import {
  contrastText,
  distanceToSegment,
  fibLabel,
  fibLevelPrices,
  handlePoints,
  hitTestBody,
  hitTestHandles,
  moveHandle,
  normalizeRect,
  pointToAnchor,
  projectDrawing,
  shiftAnchors,
  withAlpha,
  type Shape,
} from './geometry'
import { PANE_WIDTH, barTime, fakeAdapter, yOf } from './testAdapter'
import { DEFAULT_DRAWING_STYLE, FIB_LEVELS, type Drawing } from './types'

const W = PANE_WIDTH

function drawing(kind: Drawing['kind'], anchors: Drawing['anchors']): Drawing {
  return { id: kind, symbol: 'BTCUSDT', kind, anchors, style: DEFAULT_DRAWING_STYLE }
}

describe('distanceToSegment', () => {
  it('is the perpendicular distance inside the segment', () => {
    expect(distanceToSegment(5, 3, 0, 0, 10, 0)).toBe(3)
  })
  it('is the distance to the nearest end outside it', () => {
    expect(distanceToSegment(13, 4, 0, 0, 10, 0)).toBe(5)
    expect(distanceToSegment(-3, -4, 0, 0, 10, 0)).toBe(5)
  })
  it('treats a zero-length segment as a point', () => {
    expect(distanceToSegment(3, 4, 0, 0, 0, 0)).toBe(5)
  })
})

describe('normalizeRect', () => {
  it('orders corners drawn in any direction', () => {
    const expected = { left: 10, right: 50, top: 20, bottom: 80 }
    expect(normalizeRect(10, 20, 50, 80)).toEqual(expected)
    expect(normalizeRect(50, 80, 10, 20)).toEqual(expected)
    expect(normalizeRect(50, 20, 10, 80)).toEqual(expected)
  })
})

describe('fibLevelPrices', () => {
  it('runs from anchor 0 (level 0) to anchor 1 (level 1) on an up move', () => {
    const levels = fibLevelPrices(100, 200)
    expect(levels.map((l) => l.level)).toEqual(FIB_LEVELS)
    // Hand-computed: 100 + 100 * level. 1e-9 tolerance covers float rounding only.
    const expected = [100, 123.6, 138.2, 150, 161.8, 178.6, 200]
    levels.forEach((l, i) => expect(l.price).toBeCloseTo(expected[i], 9))
  })

  it('runs downwards when anchor 1 is below anchor 0', () => {
    const levels = fibLevelPrices(200, 100)
    expect(levels[0].price).toBe(200)
    expect(levels[3].price).toBe(150)
    expect(levels[4].price).toBeCloseTo(138.2, 9)
    expect(levels[6].price).toBe(100)
  })
})

describe('projectDrawing', () => {
  const adapter = fakeAdapter()

  it('places anchors through time → logical → x and price → y', () => {
    const shape = projectDrawing(
      drawing('trendline', [
        { time: barTime(2), price: 150 },
        { time: barTime(10), price: 100 },
      ]),
      adapter,
    )
    expect(shape).toEqual({ kind: 'trendline', x1: 20, y1: 100, x2: 100, y2: 200 })
  })

  it('places anchors past the last bar', () => {
    const shape = projectDrawing(drawing('hray', [{ time: barTime(120), price: 150 }]), adapter)
    expect(shape).toEqual({ kind: 'hray', x: 1200, y: 100 })
  })

  it('computes a y for every fib level', () => {
    const shape = projectDrawing(
      drawing('fib', [
        { time: barTime(0), price: 100 },
        { time: barTime(10), price: 200 },
      ]),
      adapter,
    )
    expect(shape?.kind).toBe('fib')
    if (shape?.kind !== 'fib') return
    expect(shape.levels).toHaveLength(FIB_LEVELS.length)
    expect(shape.levels[3]).toEqual({ level: 0.5, price: 150, y: yOf(150) })
  })

  it('returns null when the chart has no bars', () => {
    expect(projectDrawing(drawing('hline', [{ time: 0, price: 1 }]), fakeAdapter(0))).toBeNull()
  })
})

describe('pointToAnchor', () => {
  it('snaps time to the nearest bar and keeps the price free', () => {
    expect(pointToAnchor(fakeAdapter(), { x: 34, y: 101 })).toEqual({
      time: barTime(3),
      price: 149.5,
    })
  })
  it('snaps to extrapolated bars past the end', () => {
    expect(pointToAnchor(fakeAdapter(), { x: 1_048, y: 0 })?.time).toBe(barTime(105))
  })
})

describe('hitTestBody', () => {
  it('hline: hits anywhere across the pane width within tolerance', () => {
    const s: Shape = { kind: 'hline', y: 100, ax: 300 }
    expect(hitTestBody(s, { x: 5, y: 104 }, W)).toBe(4)
    expect(hitTestBody(s, { x: 790, y: 96 }, W)).toBe(4)
    expect(hitTestBody(s, { x: 400, y: 107 }, W)).toBeNull()
  })

  it('hray: hits only from its start rightwards', () => {
    const s: Shape = { kind: 'hray', x: 300, y: 100 }
    expect(hitTestBody(s, { x: 700, y: 102 }, W)).toBe(2)
    expect(hitTestBody(s, { x: 300, y: 100 }, W)).toBe(0)
    expect(hitTestBody(s, { x: 200, y: 100 }, W)).toBeNull()
  })

  it('trendline: hits near the segment only', () => {
    const s: Shape = { kind: 'trendline', x1: 0, y1: 0, x2: 100, y2: 100 }
    expect(hitTestBody(s, { x: 50, y: 54 }, W)).toBeCloseTo(Math.SQRT2 * 2, 9)
    expect(hitTestBody(s, { x: 50, y: 70 }, W)).toBeNull()
    expect(hitTestBody(s, { x: 150, y: 150 }, W)).toBeNull()
  })

  it('fib: hits a level line within the anchors x range, or the area inside', () => {
    const s = projectDrawing(
      drawing('fib', [
        { time: barTime(10), price: 100 },
        { time: barTime(30), price: 200 },
      ]),
      fakeAdapter(),
    ) as Shape
    // 0.5 level at price 150 → y 100.
    expect(hitTestBody(s, { x: 200, y: 102 }, W)).toBe(2)
    // Inside the area between levels, away from any line: counts with the tolerance distance.
    expect(hitTestBody(s, { x: 200, y: 112 }, W)).toBe(6)
    // Right of the x range.
    expect(hitTestBody(s, { x: 400, y: 100 }, W)).toBeNull()
  })

  it('rectangle: hits the border band and the fill, not outside', () => {
    const s: Shape = { kind: 'rectangle', x1: 100, y1: 100, x2: 200, y2: 50 }
    expect(hitTestBody(s, { x: 150, y: 52 }, W)).toBe(2)
    expect(hitTestBody(s, { x: 96, y: 75 }, W)).toBe(4)
    expect(hitTestBody(s, { x: 150, y: 75 }, W)).toBe(6)
    expect(hitTestBody(s, { x: 150, y: 120 }, W)).toBeNull()
  })
})

describe('handles', () => {
  it('rectangle has both anchors and the two derived corners', () => {
    const s: Shape = { kind: 'rectangle', x1: 10, y1: 20, x2: 30, y2: 40 }
    expect(handlePoints(s, W)).toEqual([
      { x: 10, y: 20 },
      { x: 30, y: 40 },
      { x: 10, y: 40 },
      { x: 30, y: 20 },
    ])
  })

  it('hline handle stays inside the pane when its anchor is off-screen', () => {
    expect(handlePoints({ kind: 'hline', y: 50, ax: -500 }, W)).toEqual([{ x: 24, y: 50 }])
    expect(handlePoints({ kind: 'hline', y: 50, ax: 5_000 }, W)).toEqual([{ x: 776, y: 50 }])
  })

  it('hit-tests the nearest handle within its radius', () => {
    const s: Shape = { kind: 'trendline', x1: 0, y1: 0, x2: 10, y2: 0 }
    expect(hitTestHandles(s, { x: 9, y: 1 }, W)).toEqual({
      part: 'handle',
      index: 1,
      distance: Math.SQRT2,
    })
    expect(hitTestHandles(s, { x: 30, y: 0 }, W)).toBeNull()
  })
})

describe('anchor maths', () => {
  const rect = drawing('rectangle', [
    { time: 100, price: 10 },
    { time: 200, price: 20 },
  ])

  it('moves the dragged anchor', () => {
    expect(moveHandle(rect, 1, { time: 300, price: 30 })).toEqual([
      { time: 100, price: 10 },
      { time: 300, price: 30 },
    ])
  })

  it('moves one coordinate of each anchor for a derived rectangle corner', () => {
    expect(moveHandle(rect, 2, { time: 50, price: 25 })).toEqual([
      { time: 50, price: 10 },
      { time: 200, price: 25 },
    ])
    expect(moveHandle(rect, 3, { time: 250, price: 5 })).toEqual([
      { time: 100, price: 5 },
      { time: 250, price: 20 },
    ])
  })

  it('does not mutate the original drawing', () => {
    moveHandle(rect, 0, { time: 1, price: 1 })
    expect(rect.anchors[0]).toEqual({ time: 100, price: 10 })
  })

  it('shifts by whole bars in logical space and keeps off-bar anchors between bars', () => {
    const adapter = fakeAdapter()
    const shifted = shiftAnchors(
      [
        { time: barTime(5), price: 100 },
        { time: barTime(7) + 30, price: 110 },
      ],
      3,
      -5,
      adapter,
    )
    expect(shifted).toEqual([
      { time: barTime(8), price: 95 },
      { time: barTime(10) + 30, price: 105 },
    ])
  })

  it('leaves time untouched for a zero bar delta', () => {
    const anchors = [{ time: 123, price: 1 }]
    expect(shiftAnchors(anchors, 0, 2, fakeAdapter())).toEqual([{ time: 123, price: 3 }])
  })
})

describe('render helpers', () => {
  it('withAlpha converts hex colours', () => {
    expect(withAlpha('#2962ff', 0.2)).toBe('rgba(41, 98, 255, 0.2)')
    expect(withAlpha('#fff', 0.5)).toBe('rgba(255, 255, 255, 0.5)')
    expect(withAlpha('red', 0.5)).toBe('red')
  })

  it('contrastText picks dark text on light colours', () => {
    expect(contrastText('#ffeb3b')).toBe('#131722')
    expect(contrastText('#2962ff')).toBe('#ffffff')
  })

  it('fibLabel formats level and price with thousands separators', () => {
    expect(fibLabel(0.618, 65012.5)).toBe('0.618 (65,012.50)')
    expect(fibLabel(1, 0.012345)).toBe('1 (0.0123)')
  })
})
