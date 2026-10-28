import { describe, expect, it } from 'vitest'
import { DrawingController } from './controller'
import { DrawingsPrimitive, HOVER_HANDLE_ALPHA, dashPattern } from './primitives'
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

  it('names the drawing under the point as the hit-test externalId (hoveredInfo.objectId)', () => {
    const { primitive, controller } = setup()
    expect(primitive.hitTest(30, yOf(150))).toMatchObject({
      externalId: 'drawing:h',
      zOrder: 'top',
      cursorStyle: 'pointer',
    })
    // Trendline from bar 1 (x 10, y 200) to bar 5 (x 50, y 180): midpoint x 30, y 190.
    expect(primitive.hitTest(30, 190)?.externalId).toBe('drawing:t')
    // A tool's crosshair over empty space names no drawing.
    controller.setTool('rectangle')
    expect(primitive.hitTest(700, 10)).toMatchObject({
      externalId: 'drawings',
      cursorStyle: 'crosshair',
    })
  })

  it('returns a stable pane view list', () => {
    const { primitive } = setup()
    expect(primitive.paneViews()).toBe(primitive.paneViews())
  })

  it('draws selection handles in the theme colours set by setHandleColors', () => {
    const { controller, primitive } = setup()
    let redraws = 0
    primitive.attached({
      requestUpdate: () => redraws++,
      series: { priceFormatter: () => ({ format: (p: number) => p.toFixed(2) }) },
    } as unknown as Parameters<DrawingsPrimitive['attached']>[0])
    controller.setSelected('t')
    primitive.setHandleColors({ fill: '#ffffff', ring: '#131722' })
    expect(redraws).toBe(1)
    primitive.updateAllViews()

    const arcs: { fill: unknown; stroke: unknown }[] = []
    const ctx = new Proxy(
      { fillStyle: '' as unknown, strokeStyle: '' as unknown },
      {
        get(target, key) {
          if (key in target) return target[key as keyof typeof target]
          if (key === 'fill')
            return () => arcs.push({ fill: target.fillStyle, stroke: target.strokeStyle })
          return () => undefined
        },
        set(target, key, value) {
          ;(target as Record<string | symbol, unknown>)[key] = value
          return true
        },
      },
    )
    const scope = { context: ctx, horizontalPixelRatio: 1, verticalPixelRatio: 1 }
    const target = { useBitmapCoordinateSpace: (f: (s: typeof scope) => void) => f(scope) }
    const renderer = primitive.paneViews()[0].renderer()
    renderer?.draw(target as unknown as Parameters<NonNullable<typeof renderer>['draw']>[0])

    // Two anchor handles on the selected trendline, filled with the light background and
    // stroked in its (blue) line colour, which stays visible on white.
    const handles = arcs.filter((a) => a.fill === '#ffffff')
    expect(handles).toHaveLength(2)
    for (const h of handles) expect(h.stroke).toBe(DEFAULT_DRAWING_STYLE.color)
  })

  it("draws a hovered, unselected drawing's handles faintly; none without hover", () => {
    const { controller, primitive } = setup()

    function drawnHandles() {
      const arcs: { alpha: number }[] = []
      const state = { fillStyle: '' as unknown, strokeStyle: '' as unknown, globalAlpha: 1 }
      const stack: number[] = []
      const ctx = new Proxy(state, {
        get(target, key) {
          if (key in target) return target[key as keyof typeof target]
          if (key === 'save') return () => stack.push(target.globalAlpha)
          if (key === 'restore') return () => (target.globalAlpha = stack.pop() ?? 1)
          if (key === 'arc') return () => arcs.push({ alpha: target.globalAlpha })
          return () => undefined
        },
        set(target, key, value) {
          ;(target as Record<string | symbol, unknown>)[key] = value
          return true
        },
      })
      const scope = { context: ctx, horizontalPixelRatio: 1, verticalPixelRatio: 1 }
      const target = { useBitmapCoordinateSpace: (f: (s: typeof scope) => void) => f(scope) }
      primitive.updateAllViews()
      const renderer = primitive.paneViews()[0].renderer()
      renderer?.draw(target as unknown as Parameters<NonNullable<typeof renderer>['draw']>[0])
      return arcs
    }

    expect(drawnHandles()).toEqual([])
    controller.setHovered('t')
    // Two faint handles on the hovered trendline.
    expect(drawnHandles()).toEqual([{ alpha: HOVER_HANDLE_ALPHA }, { alpha: HOVER_HANDLE_ALPHA }])
    // Selected: full-strength handles, and the hover does not add a second faint set.
    controller.setSelected('t')
    expect(drawnHandles()).toEqual([{ alpha: 1 }, { alpha: 1 }])
    // Hover another drawing while one is selected: both sets, each at its own strength.
    controller.setHovered('h')
    const both = drawnHandles()
    expect(both.filter((a) => a.alpha === 1)).toHaveLength(2)
    expect(both.filter((a) => a.alpha === HOVER_HANDLE_ALPHA).length).toBeGreaterThan(0)
  })
})
