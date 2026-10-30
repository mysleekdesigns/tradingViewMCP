// The renderer paints exactly what the pre-cache renderer painted. `legacyDraw` below is the
// renderer as it was before Phase 19 (projection and string work inside draw()), kept verbatim
// as an oracle: for every scene, pixel ratio, theme, and after scroll/zoom/drag, both must issue
// the same canvas calls with the same values in the same order.

import { describe, expect, it } from 'vitest'
import { DrawingController } from './controller'
import {
  HANDLE_RADIUS,
  fibLabel,
  handlePoints,
  normalizeRect,
  projectDrawing,
  withAlpha,
  type CoordinateAdapter,
  type Shape,
} from './geometry'
import { DrawingsPrimitive, HOVER_HANDLE_ALPHA, dashPattern } from './primitives'
import {
  PIXEL_RATIOS,
  SCENES,
  SCENE_DRAWINGS,
  paint,
  recordingCanvas,
  sceneSetup,
  type RecordingCanvas,
} from './recordingCanvas'
import { fontFamily, handleStroke, type HandleColors } from '../theme'
import { STEP, makeBars, movableAdapter } from './testAdapter'
import type { Drawing, DrawingStyle } from './types'

/* ---------------------------------- legacy oracle (verbatim) ---------------------------------- */

interface LegacyItem {
  drawing: Drawing
  shape: Shape
  selected: boolean
  hovered: boolean
}
interface LegacyScope {
  context: CanvasRenderingContext2D
  horizontalPixelRatio: number
  verticalPixelRatio: number
}

function legacyItems(controller: DrawingController, adapter: CoordinateAdapter): LegacyItem[] {
  const { drawings, selectedId, hoveredId } = controller.view()
  const items: LegacyItem[] = []
  for (const drawing of drawings) {
    const shape = projectDrawing(drawing, adapter)
    if (!shape) continue
    items.push({
      drawing,
      shape,
      selected: drawing.id === selectedId,
      hovered: drawing.id === hoveredId,
    })
  }
  return items
}

function legacyDraw(
  scope: LegacyScope,
  items: readonly LegacyItem[],
  width: number,
  handles: HandleColors,
): void {
  const ctx = scope.context
  ctx.save()
  for (const item of items) drawItem(scope, item, width)
  for (const item of items) {
    if (item.selected) drawHandles(scope, item, width, handles)
    else if (item.hovered) {
      ctx.save()
      ctx.globalAlpha = HOVER_HANDLE_ALPHA
      drawHandles(scope, item, width, handles)
      ctx.restore()
    }
  }
  ctx.restore()
}

function strokeSetup(scope: LegacyScope, style: DrawingStyle, color: string = style.color): number {
  const ctx = scope.context
  const ratio = scope.horizontalPixelRatio
  const width = Math.max(1, Math.floor(style.lineWidth * ratio))
  ctx.strokeStyle = color
  ctx.lineWidth = width
  ctx.lineCap = 'butt'
  ctx.setLineDash(dashPattern(style.lineStyle, style.lineWidth).map((v) => v * ratio))
  return width
}

function hLine(scope: LegacyScope, x0: number, x1: number, y: number, lineWidth: number): void {
  const ctx = scope.context
  const offset = lineWidth % 2 ? 0.5 : 0
  const yb = Math.round(y * scope.verticalPixelRatio) + offset
  ctx.beginPath()
  ctx.moveTo(Math.round(x0 * scope.horizontalPixelRatio), yb)
  ctx.lineTo(Math.round(x1 * scope.horizontalPixelRatio), yb)
  ctx.stroke()
}

function segment(scope: LegacyScope, x1: number, y1: number, x2: number, y2: number): void {
  const ctx = scope.context
  const hr = scope.horizontalPixelRatio
  const vr = scope.verticalPixelRatio
  ctx.beginPath()
  ctx.moveTo(x1 * hr, y1 * vr)
  ctx.lineTo(x2 * hr, y2 * vr)
  ctx.stroke()
}

function fillRect(scope: LegacyScope, left: number, top: number, right: number, bottom: number) {
  const hr = scope.horizontalPixelRatio
  const vr = scope.verticalPixelRatio
  const x = Math.round(left * hr)
  const y = Math.round(top * vr)
  scope.context.fillRect(x, y, Math.round(right * hr) - x, Math.round(bottom * vr) - y)
}

function strokeRect(scope: LegacyScope, r: ReturnType<typeof normalizeRect>, lineWidth: number) {
  const hr = scope.horizontalPixelRatio
  const vr = scope.verticalPixelRatio
  const offset = lineWidth % 2 ? 0.5 : 0
  const x = Math.round(r.left * hr) + offset
  const y = Math.round(r.top * vr) + offset
  const w = Math.round(r.right * hr) - Math.round(r.left * hr)
  const h = Math.round(r.bottom * vr) - Math.round(r.top * vr)
  scope.context.strokeRect(x, y, w, h)
}

function drawItem(scope: LegacyScope, item: LegacyItem, paneWidth: number): void {
  const { shape, drawing } = item
  const style = drawing.style
  const ctx = scope.context
  switch (shape.kind) {
    case 'hline': {
      const w = strokeSetup(scope, style)
      hLine(scope, 0, paneWidth, shape.y, w)
      return
    }
    case 'hray': {
      const w = strokeSetup(scope, style)
      hLine(scope, Math.max(shape.x, -1), Math.max(paneWidth, shape.x), shape.y, w)
      return
    }
    case 'trendline':
      strokeSetup(scope, style)
      ctx.lineCap = style.lineStyle === 'solid' ? 'round' : 'butt'
      segment(scope, shape.x1, shape.y1, shape.x2, shape.y2)
      return
    case 'rectangle': {
      const r = normalizeRect(shape.x1, shape.y1, shape.x2, shape.y2)
      ctx.fillStyle = withAlpha(style.color, 0.16)
      fillRect(scope, r.left, r.top, r.right, r.bottom)
      const w = strokeSetup(scope, style)
      strokeRect(scope, r, w)
      return
    }
    case 'fib':
      drawFib(scope, item)
      return
  }
}

function drawFib(scope: LegacyScope, item: LegacyItem): void {
  const shape = item.shape
  if (shape.kind !== 'fib') return
  const style = item.drawing.style
  const ctx = scope.context
  const left = Math.min(shape.x1, shape.x2)
  const right = Math.max(shape.x1, shape.x2)
  const levels = shape.levels
  const FIB_FILL_ALPHAS = [0.06, 0.11] as const

  for (let i = 0; i + 1 < levels.length; i++) {
    const top = Math.min(levels[i].y, levels[i + 1].y)
    const bottom = Math.max(levels[i].y, levels[i + 1].y)
    ctx.fillStyle = withAlpha(style.color, FIB_FILL_ALPHAS[i % 2])
    fillRect(scope, left, top, right, bottom)
  }

  ctx.save()
  ctx.strokeStyle = withAlpha(style.color, 0.5)
  ctx.lineWidth = Math.max(1, Math.floor(scope.horizontalPixelRatio))
  ctx.setLineDash([4 * scope.horizontalPixelRatio, 4 * scope.horizontalPixelRatio])
  segment(scope, shape.x1, shape.y1, shape.x2, shape.y2)
  ctx.restore()

  const w = strokeSetup(scope, style)
  for (const lv of levels) hLine(scope, left, right, lv.y, w)

  const hr = scope.horizontalPixelRatio
  const vr = scope.verticalPixelRatio
  ctx.setLineDash([])
  ctx.font = `${Math.round(11 * vr)}px ${fontFamily}`
  ctx.textAlign = 'left'
  ctx.textBaseline = 'bottom'
  ctx.fillStyle = style.color
  for (const lv of levels) {
    ctx.fillText(
      fibLabel(lv.level, lv.price),
      Math.round((left + 4) * hr),
      Math.round((lv.y - 3) * vr),
    )
  }
}

function drawHandles(
  scope: LegacyScope,
  item: LegacyItem,
  paneWidth: number,
  handles: HandleColors,
): void {
  const ctx = scope.context
  const hr = scope.horizontalPixelRatio
  const vr = scope.verticalPixelRatio
  ctx.setLineDash([])
  ctx.lineWidth = Math.max(1, Math.round(1.5 * hr))
  ctx.strokeStyle = handleStroke(item.drawing.style.color, handles)
  ctx.fillStyle = handles.fill
  for (const h of handlePoints(item.shape, paneWidth)) {
    ctx.beginPath()
    ctx.arc(h.x * hr, h.y * vr, HANDLE_RADIUS * hr, 0, Math.PI * 2)
    ctx.fill()
    ctx.stroke()
  }
}

/* --------------------------------------------- tests --------------------------------------------- */

function legacyRecording(
  controller: DrawingController,
  adapter: CoordinateAdapter,
  handles: HandleColors,
  hr: number,
  vr: number = hr,
): RecordingCanvas {
  const canvas = recordingCanvas()
  const scope = { context: canvas.ctx, horizontalPixelRatio: hr, verticalPixelRatio: vr }
  legacyDraw(scope, legacyItems(controller, adapter), adapter.paneWidth(), handles)
  return canvas
}

describe('drawings renderer equals the pre-cache renderer', () => {
  it('for every scene, theme and pixel ratio', () => {
    expect(SCENES.length).toBeGreaterThanOrEqual(14)
    for (const scene of SCENES) {
      const { controller, primitive } = sceneSetup(scene)
      for (const [hr, vr] of PIXEL_RATIOS) {
        const canvas = recordingCanvas()
        paint(primitive, canvas, hr, vr)
        const { adapter } = movableAdapter()
        const expected = legacyRecording(controller, adapter, scene.handles, hr, vr)
        expect(canvas.ops, `${scene.name} @${hr}x${vr}`).toEqual(expected.ops)
      }
    }
  })

  it('covers every drawing kind, handles, labels and hover alpha', () => {
    const { primitive } = sceneSetup(SCENES.find((s) => s.selected === 'x1')!)
    const canvas = recordingCanvas()
    paint(primitive, canvas, 2)
    const names = new Set(canvas.ops.map((op) => `${op[0]}:${op[1]}`))
    for (const n of [
      'call:fillRect',
      'call:strokeRect',
      'call:fillText',
      'call:arc',
      'set:globalAlpha',
      'call:setLineDash',
    ]) {
      expect(names.has(n), n).toBe(true)
    }
    const kinds = new Set(SCENE_DRAWINGS.map((d) => d.kind))
    expect([...kinds].sort()).toEqual(['fib', 'hline', 'hray', 'rectangle', 'trendline'])
  })

  it('a repeat paint issues the same calls as the first', () => {
    for (const scene of SCENES) {
      const { primitive } = sceneSetup(scene)
      const first = recordingCanvas()
      paint(primitive, first, 1.5)
      const again = recordingCanvas()
      paint(primitive, again, 1.5)
      expect(again.ops).toEqual(first.ops)
    }
  })

  it('after scroll, zoom, price-scale change, new bars, a theme change and during a drag', () => {
    const m = movableAdapter()
    const controller = new DrawingController(m.adapter)
    controller.setDrawings(SCENE_DRAWINGS)
    controller.setSelected('f1')
    controller.setHovered('t2')
    const primitive = new DrawingsPrimitive(controller, m.adapter)
    let handles = SCENES[0].handles
    primitive.setHandleColors(handles)

    const check = (label: string) => {
      primitive.updateAllViews()
      for (const [hr, vr] of PIXEL_RATIOS) {
        const canvas = recordingCanvas()
        paint(primitive, canvas, hr, vr)
        expect(canvas.ops, `${label} @${hr}x${vr}`).toEqual(
          legacyRecording(controller, m.adapter, handles, hr, vr).ops,
        )
      }
    }

    check('initial')
    m.view.offset = 7.5
    check('scrolled')
    m.view.spacing = 6.25
    check('zoomed')
    m.view.scale = 3.1
    m.view.base = 260
    check('price scale')
    m.view.width = 640
    check('narrower pane')
    // A gap in the bars moves off-bar anchors without any scroll.
    m.setBars(makeBars(100).map((b, i) => ({ time: i < 15 ? b.time : b.time + 3 * STEP })))
    check('new bars')
    handles = SCENES[1].handles
    primitive.setHandleColors(handles)
    check('theme')

    // Drag the fib's body: its anchors change on every move.
    const f1 = SCENE_DRAWINGS.find((d) => d.id === 'f1')!
    const shape = projectDrawing(f1, m.adapter)!
    expect(shape.kind).toBe('fib')
    if (shape.kind !== 'fib') return
    const start = { x: (shape.x1 + shape.x2) / 2, y: shape.levels[3].y }
    expect(controller.hitTest(start)?.id).toBe('f1')
    expect(controller.pointerDown(start)).toBe(true)
    for (let k = 1; k <= 6; k++) {
      controller.pointerMove({ x: start.x + 13 * k, y: start.y - 7 * k })
      check(`drag move ${k}`)
    }
    controller.pointerUp({ x: start.x + 78, y: start.y - 42 })
    check('after drag')
  })
})
