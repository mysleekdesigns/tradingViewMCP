// Lightweight Charts v5 series primitive that renders every drawing of the current symbol.
//
// One instance is attached to the main series (series.attachPrimitive) and re-created when the
// chart type switches the main series. Shapes are projected in updateAllViews(), which LWC calls
// on every redraw (scroll, zoom, data change), so live ticks never rebuild anything here; an
// interaction calls update() → requestUpdate().

import type {
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesPrimitive,
  ISeriesPrimitiveAxisView,
  PrimitiveHoveredItem,
  SeriesAttachedParameter,
  Time,
} from 'lightweight-charts'
import { drawingColors, fontFamily } from '../theme'
import type { CursorStyle, DrawingsView } from './controller'
import {
  HANDLE_RADIUS,
  fibLabel,
  handlePoints,
  normalizeRect,
  projectDrawing,
  withAlpha,
  contrastText,
  type CoordinateAdapter,
  type Shape,
} from './geometry'
import type { Drawing, DrawingStyle, LineStyleName } from './types'

type RenderTarget = Parameters<IPrimitivePaneRenderer['draw']>[0]
type BitmapScope = Parameters<Parameters<RenderTarget['useBitmapCoordinateSpace']>[0]>[0]

/** What the primitive needs from the interaction layer. DrawingController satisfies it. */
export interface DrawingsSource {
  view(): DrawingsView
  cursorAt(p: { x: number; y: number }): CursorStyle | null
}

interface RenderItem {
  drawing: Drawing
  shape: Shape
  selected: boolean
}

/** Dash pattern in CSS px for a line style at a given width. */
export function dashPattern(style: LineStyleName, width: number): number[] {
  switch (style) {
    case 'solid':
      return []
    case 'dashed':
      return [Math.max(4, width * 3), Math.max(3, width * 2)]
    case 'dotted':
      return [Math.max(1, width), Math.max(2, width * 1.5)]
  }
}

const LABEL_FONT_PX = 11
const RECT_FILL_ALPHA = 0.16
/** Fib bands alternate between these two fill alphas, like TradingView's faint level fills. */
const FIB_FILL_ALPHAS = [0.06, 0.11] as const

class DrawingsRenderer implements IPrimitivePaneRenderer {
  private items: readonly RenderItem[] = []
  private width = 0

  set(items: readonly RenderItem[], width: number): void {
    this.items = items
    this.width = width
  }

  draw(target: RenderTarget): void {
    target.useBitmapCoordinateSpace((scope) => {
      const ctx = scope.context
      ctx.save()
      for (const item of this.items) drawItem(scope, item, this.width)
      for (const item of this.items) if (item.selected) drawHandles(scope, item, this.width)
      ctx.restore()
    })
  }
}

class DrawingsPaneView implements IPrimitivePaneView {
  readonly renderer_ = new DrawingsRenderer()

  zOrder(): 'top' {
    return 'top'
  }

  renderer(): IPrimitivePaneRenderer {
    return this.renderer_
  }
}

/** Price-axis label for an hline / hray, in the line colour. */
class PriceLabelView implements ISeriesPrimitiveAxisView {
  y = 0
  label = ''
  color = '#2962ff'

  coordinate(): number {
    return this.y
  }
  text(): string {
    return this.label
  }
  textColor(): string {
    return contrastText(this.color)
  }
  backColor(): string {
    return this.color
  }
  visible(): boolean {
    return true
  }
  tickVisible(): boolean {
    return true
  }
}

export class DrawingsPrimitive implements ISeriesPrimitive<Time> {
  private readonly source: DrawingsSource
  private readonly adapter: CoordinateAdapter
  private readonly paneView = new DrawingsPaneView()
  private readonly paneViewList: readonly IPrimitivePaneView[] = [this.paneView]
  private labelViews: PriceLabelView[] = []
  private requestUpdate: (() => void) | null = null
  private formatPrice: (price: number) => string = (p) => p.toFixed(2)

  constructor(source: DrawingsSource, adapter: CoordinateAdapter) {
    this.source = source
    this.adapter = adapter
  }

  attached(param: SeriesAttachedParameter<Time>): void {
    this.requestUpdate = param.requestUpdate
    const series = param.series
    this.formatPrice = (p) => series.priceFormatter().format(p)
  }

  detached(): void {
    this.requestUpdate = null
  }

  /** Redraw (interaction or drawings changed). */
  update(): void {
    this.requestUpdate?.()
  }

  updateAllViews(): void {
    const { drawings, selectedId } = this.source.view()
    const width = this.adapter.paneWidth()
    const items: RenderItem[] = []
    const labels: { y: number; text: string; color: string }[] = []
    for (const drawing of drawings) {
      const shape = projectDrawing(drawing, this.adapter)
      if (!shape) continue
      items.push({ drawing, shape, selected: drawing.id === selectedId })
      if (shape.kind === 'hline' || shape.kind === 'hray') {
        labels.push({
          y: shape.y,
          text: this.formatPrice(drawing.anchors[0].price),
          color: drawing.style.color,
        })
      }
    }
    this.paneView.renderer_.set(items, width)
    // LWC caches axis views by array identity: keep the array unless the count changes.
    if (labels.length !== this.labelViews.length) {
      this.labelViews = labels.map(() => new PriceLabelView())
    }
    labels.forEach((l, i) => {
      const v = this.labelViews[i]
      v.y = l.y
      v.label = l.text
      v.color = l.color
    })
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return this.paneViewList
  }

  priceAxisViews(): readonly ISeriesPrimitiveAxisView[] {
    return this.labelViews
  }

  /** Lets LWC show our cursor: crosshair while a tool is active, pointer/grab over drawings. */
  hitTest(x: number, y: number): PrimitiveHoveredItem | null {
    const cursor = this.source.cursorAt({ x, y })
    if (!cursor) return null
    return {
      cursorStyle: cursor,
      externalId: 'drawings',
      zOrder: 'top',
      distance: 0,
      itemType: 'primitive',
    }
  }
}

/* ------------------------------------------------------------------------------------------ */
/* Drawing helpers (bitmap space)                                                              */
/* ------------------------------------------------------------------------------------------ */

function strokeSetup(scope: BitmapScope, style: DrawingStyle, color: string = style.color): number {
  const ctx = scope.context
  const ratio = scope.horizontalPixelRatio
  const width = Math.max(1, Math.floor(style.lineWidth * ratio))
  ctx.strokeStyle = color
  ctx.lineWidth = width
  ctx.lineCap = 'butt'
  ctx.setLineDash(dashPattern(style.lineStyle, style.lineWidth).map((v) => v * ratio))
  return width
}

/** Crisp horizontal line from media x0 to x1 at media y. */
function hLine(scope: BitmapScope, x0: number, x1: number, y: number, lineWidth: number): void {
  const ctx = scope.context
  const offset = lineWidth % 2 ? 0.5 : 0
  const yb = Math.round(y * scope.verticalPixelRatio) + offset
  ctx.beginPath()
  ctx.moveTo(Math.round(x0 * scope.horizontalPixelRatio), yb)
  ctx.lineTo(Math.round(x1 * scope.horizontalPixelRatio), yb)
  ctx.stroke()
}

function segment(scope: BitmapScope, x1: number, y1: number, x2: number, y2: number): void {
  const ctx = scope.context
  const hr = scope.horizontalPixelRatio
  const vr = scope.verticalPixelRatio
  ctx.beginPath()
  ctx.moveTo(x1 * hr, y1 * vr)
  ctx.lineTo(x2 * hr, y2 * vr)
  ctx.stroke()
}

function fillRect(scope: BitmapScope, left: number, top: number, right: number, bottom: number) {
  const hr = scope.horizontalPixelRatio
  const vr = scope.verticalPixelRatio
  const x = Math.round(left * hr)
  const y = Math.round(top * vr)
  scope.context.fillRect(x, y, Math.round(right * hr) - x, Math.round(bottom * vr) - y)
}

function strokeRect(scope: BitmapScope, r: ReturnType<typeof normalizeRect>, lineWidth: number) {
  const hr = scope.horizontalPixelRatio
  const vr = scope.verticalPixelRatio
  const offset = lineWidth % 2 ? 0.5 : 0
  const x = Math.round(r.left * hr) + offset
  const y = Math.round(r.top * vr) + offset
  const w = Math.round(r.right * hr) - Math.round(r.left * hr)
  const h = Math.round(r.bottom * vr) - Math.round(r.top * vr)
  scope.context.strokeRect(x, y, w, h)
}

function drawItem(scope: BitmapScope, item: RenderItem, paneWidth: number): void {
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
      ctx.fillStyle = withAlpha(style.color, RECT_FILL_ALPHA)
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

function drawFib(scope: BitmapScope, item: RenderItem): void {
  const shape = item.shape
  if (shape.kind !== 'fib') return
  const style = item.drawing.style
  const ctx = scope.context
  const left = Math.min(shape.x1, shape.x2)
  const right = Math.max(shape.x1, shape.x2)
  const levels = shape.levels

  // Faint bands between consecutive levels.
  for (let i = 0; i + 1 < levels.length; i++) {
    const top = Math.min(levels[i].y, levels[i + 1].y)
    const bottom = Math.max(levels[i].y, levels[i + 1].y)
    ctx.fillStyle = withAlpha(style.color, FIB_FILL_ALPHAS[i % 2])
    fillRect(scope, left, top, right, bottom)
  }

  // Dashed, faint trend line between the two anchors.
  ctx.save()
  ctx.strokeStyle = withAlpha(style.color, 0.5)
  ctx.lineWidth = Math.max(1, Math.floor(scope.horizontalPixelRatio))
  ctx.setLineDash([4 * scope.horizontalPixelRatio, 4 * scope.horizontalPixelRatio])
  segment(scope, shape.x1, shape.y1, shape.x2, shape.y2)
  ctx.restore()

  const w = strokeSetup(scope, style)
  for (const lv of levels) hLine(scope, left, right, lv.y, w)

  // "0.618 (65,012.50)" just above each level line, left-aligned at the left anchor.
  const hr = scope.horizontalPixelRatio
  const vr = scope.verticalPixelRatio
  ctx.setLineDash([])
  ctx.font = `${Math.round(LABEL_FONT_PX * vr)}px ${fontFamily}`
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

function drawHandles(scope: BitmapScope, item: RenderItem, paneWidth: number): void {
  const ctx = scope.context
  const hr = scope.horizontalPixelRatio
  const vr = scope.verticalPixelRatio
  ctx.setLineDash([])
  ctx.lineWidth = Math.max(1, Math.round(1.5 * hr))
  ctx.strokeStyle = item.drawing.style.color
  ctx.fillStyle = drawingColors.handleFill
  for (const h of handlePoints(item.shape, paneWidth)) {
    ctx.beginPath()
    ctx.arc(h.x * hr, h.y * vr, HANDLE_RADIUS * hr, 0, Math.PI * 2)
    ctx.fill()
    ctx.stroke()
  }
}
