import { describe, expect, it } from 'vitest'
import { DrawingController } from './controller'
import { DrawingsPrimitive, dashPattern } from './primitives'
import { barTime, fakeAdapter, yOf } from './testAdapter'
import { DEFAULT_DRAWING_STYLE, type Drawing } from './types'

describe('dashPattern', () => {
  it('is solid, dashed or dotted', () => {
    expect(dashPattern('solid', 2)).toEqual([])
    expect(dashPattern('dashed', 2)).toEqual([6, 4])
    expect(dashPattern('dotted', 2)).toEqual([2, 3])
  })
})

describe('DrawingsPrimitive', () => {
  const hline: Drawing = {
    id: 'h',
    symbol: 'BTCUSDT',
    kind: 'hline',
    anchors: [{ time: barTime(3), price: 150 }],
    style: { ...DEFAULT_DRAWING_STYLE, color: '#ff9800' },
  }
  const trend: Drawing = {
    id: 't',
    symbol: 'BTCUSDT',
    kind: 'trendline',
    anchors: [
      { time: barTime(1), price: 100 },
      { time: barTime(5), price: 110 },
    ],
    style: DEFAULT_DRAWING_STYLE,
  }

  function setup() {
    const adapter = fakeAdapter()
    const controller = new DrawingController(adapter)
    controller.setDrawings([hline, trend])
    const primitive = new DrawingsPrimitive(controller, adapter)
    primitive.updateAllViews()
    return { controller, primitive }
  }

  it('gives hlines and hrays a price-axis label in the line colour', () => {
    const { primitive } = setup()
    const views = primitive.priceAxisViews()
    expect(views).toHaveLength(1)
    expect(views[0].coordinate()).toBe(yOf(150))
    expect(views[0].backColor()).toBe('#ff9800')
    expect(views[0].text()).toBe('150.00')
  })

  it('keeps the axis-view array stable when only positions change', () => {
    const { primitive } = setup()
    const before = primitive.priceAxisViews()
    primitive.updateAllViews()
    expect(primitive.priceAxisViews()).toBe(before)
  })

  it('reports cursors through hitTest for LWC', () => {
    const { primitive, controller } = setup()
    expect(primitive.hitTest(30, yOf(150))?.cursorStyle).toBe('pointer')
    expect(primitive.hitTest(700, 10)).toBeNull()
    controller.setTool('rectangle')
    expect(primitive.hitTest(700, 10)?.cursorStyle).toBe('crosshair')
  })

  it('returns a stable pane view list', () => {
    const { primitive } = setup()
    expect(primitive.paneViews()).toBe(primitive.paneViews())
  })
})
