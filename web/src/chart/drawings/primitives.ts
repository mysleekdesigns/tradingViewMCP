// Lightweight Charts v5 series primitive that renders every drawing of the current symbol.
//
// One instance is attached to the main series (series.attachPrimitive) and re-created when the
// chart type switches the main series. Shapes are projected in updateAllViews(), which LWC calls
// on every redraw (scroll, zoom, data change), so live ticks never rebuild anything here; an
// interaction calls update() → requestUpdate().
//
// The pane view is zOrder 'top', so draw() also runs on every crosshair move (~60 Hz while the
// mouse moves) without an updateAllViews in between. Everything that allocates or formats is
// therefore prepared outside draw(): rgba fills, fib label text, rectangles and handle points in
// updateAllViews (cached per drawing on the style and anchor values they come from), and the
// dash arrays scaled by the bitmap pixel ratio, which only draw() knows, memoised per ratio.
// draw() itself is canvas calls only, in the same order with the same values as before.

import type {
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesPrimitive,
  ISeriesPrimitiveAxisView,
  PrimitiveHoveredItem,
  SeriesAttachedParameter,
  Time,
} from 'lightweight-charts'
import { drawingObjectId } from '../hover'
import { colors, fontFamily, handleColorsFor, handleStroke, type HandleColors } from '../theme'
import type { DrawingsView, HoverHit } from './controller'
import {
  HANDLE_RADIUS,
  fibLabel,
  fibLevelPrices,
  handlePoints,
  normalizeRect,
  withAlpha,
  contrastText,
  type CoordinateAdapter,
  type Point,
  type Rect,
  type Shape,
} from './geometry'
import { ShapeCache, type ShapesFrame } from './shapeCache'
import { FIB_LEVELS, type Drawing, type DrawingStyle, type LineStyleName } from './types'

type RenderTarget = Parameters<IPrimitivePaneRenderer['draw']>[0]
type BitmapScope = Parameters<Parameters<RenderTarget['useBitmapCoordinateSpace']>[0]>[0]

/** What the primitive needs from the interaction layer. DrawingController satisfies it. */
export interface DrawingsSource {
  view(): DrawingsView
  hoverAt(p: { x: number; y: number }): HoverHit | null
  /**
   * view().drawings projected for the current chart view, shared with the source's hit test.
   * Without it the primitive projects through its own cache.
   */
  frame?(): ShapesFrame
}

/**
 * Per-drawing paint resources, rebuilt only when the style or anchor values they come from
 * change. The scaled dash and handle stroke are filled in lazily by draw() (they depend on the
 * pixel ratio and the theme's handle colours) and then reused on every repeat frame.
 */
interface Paint {
  kind: Drawing['kind']
  color: string
  lineWidth: number
  lineStyle: LineStyleName
  /** Dash pattern in CSS px. */
  dash: readonly number[]
  /** `dash` scaled to bitmap px for `dashRatio`. */
  scaledDash: number[]
  dashRatio: number
  /** Rectangle fill. */
  rectFill: string
  /** Fib band fills (alternating) and the faint anchor-to-anchor line. */
  fibFills: readonly [string, string]
  fibTrend: string
  /** Fib labels for every FIB_LEVELS level of anchors priced (fibP0, fibP1). */
  fibP0: number
  fibP1: number
  fibLabels: readonly string[]
  /** handleStroke(color, handleFor), memoised on the handle colours object. */
  handleStroke: string
  handleFor: HandleColors | null
}

interface RenderItem {
  drawing: Drawing
  shape: Shape
  selected: boolean
  /** Under the mouse but not selected: faint handles. */
  hovered: boolean
  paint: Paint
  /** Rectangle bounds (normalised corners), or null for other kinds. */
  rect: Rect | null
  /** Fib label per drawn level (aligned with shape.levels), or null. */
  labels: readonly string[] | null
  /** Handle positions when selected or hovered, else null. */
  handles: readonly Point[] | null
}

/** Opacity of a hovered (not selected) drawing's handles. */
export const HOVER_HANDLE_ALPHA = 0.5

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
/** Opacity of the fib's anchor-to-anchor line. */
const FIB_TREND_ALPHA = 0.5
/** setLineDash copies its argument, so one shared empty pattern serves every solid stroke. */
const NO_DASH: number[] = []

function samePaintStyle(p: Paint, kind: Drawing['kind'], style: DrawingStyle): boolean {
  return (
    p.kind === kind &&
    p.color === style.color &&
    p.lineWidth === style.lineWidth &&
    p.lineStyle === style.lineStyle
  )
}

function makePaint(kind: Drawing['kind'], style: DrawingStyle): Paint {
  const color = style.color
  return {
    kind,
    color,
    lineWidth: style.lineWidth,
    lineStyle: style.lineStyle,
    dash: dashPattern(style.lineStyle, style.lineWidth),
    scaledDash: NO_DASH,
    dashRatio: NaN,
    rectFill: kind === 'rectangle' ? withAlpha(color, RECT_FILL_ALPHA) : '',
    fibFills:
      kind === 'fib'
        ? [withAlpha(color, FIB_FILL_ALPHAS[0]), withAlpha(color, FIB_FILL_ALPHAS[1])]
        : ['', ''],
    fibTrend: kind === 'fib' ? withAlpha(color, FIB_TREND_ALPHA) : '',
    fibP0: NaN,
    fibP1: NaN,
    fibLabels: [],
    handleStroke: '',
    handleFor: null,
  }
}

/** Labels for the levels the fib shape holds (every level unless a price could not be placed). */
function fibLabelsFor(paint: Paint, drawing: Drawing, shape: Shape & { kind: 'fib' }) {
  const p0 = drawing.anchors[0].price
  const p1 = drawing.anchors[1].price
  if (!Object.is(paint.fibP0, p0) || !Object.is(paint.fibP1, p1)) {
    paint.fibLabels = fibLevelPrices(p0, p1).map((lv) => fibLabel(lv.level, lv.price))
    paint.fibP0 = p0
    paint.fibP1 = p1
  }
  if (shape.levels.length === FIB_LEVELS.length) return paint.fibLabels
  return shape.levels.map((lv) => fibLabel(lv.level, lv.price))
}

/** Bitmap-px dash for the item's line style at this pixel ratio (allocated once per ratio). */
function scaledDash(paint: Paint, ratio: number): number[] {
  if (paint.dashRatio !== ratio) {
    paint.scaledDash = paint.dash.length === 0 ? NO_DASH : paint.dash.map((v) => v * ratio)
    paint.dashRatio = ratio
  }
  return paint.scaledDash
}

class DrawingsRenderer implements IPrimitivePaneRenderer {
  private items: readonly RenderItem[] = []
  private width = 0
  /** Selection handle colours; follow the chart theme (see DrawingsPrimitive.setHandleColors). */
  handles: HandleColors = handleColorsFor(colors)
  /** Memo of the fib anchor-line dash and the label font for the last pixel ratios seen. */
  private trendDash: number[] = NO_DASH
  private trendDashRatio = NaN
  private font = ''
  private fontRatio = NaN

  set(items: readonly RenderItem[], width: number): void {
    this.items = items
    this.width = width
  }

  draw(target: RenderTarget): void {
    target.useBitmapCoordinateSpace((scope) => {
      const ctx = scope.context
      const items = this.items
      ctx.save()
      for (let i = 0; i < items.length; i++) this.drawItem(scope, items[i])
      for (let i = 0; i < items.length; i++) {
        const item = items[i]
        if (item.selected) this.drawHandles(scope, item)
        else if (item.hovered) {
          ctx.save()
          ctx.globalAlpha = HOVER_HANDLE_ALPHA
          this.drawHandles(scope, item)
          ctx.restore()
        }
      }
      ctx.restore()
    })
  }

  private drawItem(scope: BitmapScope, item: RenderItem): void {
    const { shape, paint } = item
    const ctx = scope.context
    switch (shape.kind) {
      case 'hline': {
        const w = strokeSetup(scope, paint)
        hLine(scope, 0, this.width, shape.y, w)
        return
      }
      case 'hray': {
        const w = strokeSetup(scope, paint)
        hLine(scope, Math.max(shape.x, -1), Math.max(this.width, shape.x), shape.y, w)
        return
      }
      case 'trendline':
        strokeSetup(scope, paint)
        ctx.lineCap = paint.lineStyle === 'solid' ? 'round' : 'butt'
        segment(scope, shape.x1, shape.y1, shape.x2, shape.y2)
        return
      case 'rectangle': {
        const r = item.rect as Rect
        ctx.fillStyle = paint.rectFill
        fillRect(scope, r.left, r.top, r.right, r.bottom)
        const w = strokeSetup(scope, paint)
        strokeRect(scope, r, w)
        return
      }
      case 'fib':
        this.drawFib(scope, item, shape)
        return
    }
  }

  private drawFib(scope: BitmapScope, item: RenderItem, shape: Shape & { kind: 'fib' }): void {
    const paint = item.paint
    const labels = item.labels as readonly string[]
    const ctx = scope.context
    const left = Math.min(shape.x1, shape.x2)
    const right = Math.max(shape.x1, shape.x2)
    const levels = shape.levels
    const hr = scope.horizontalPixelRatio
    const vr = scope.verticalPixelRatio

    // Faint bands between consecutive levels.
    for (let i = 0; i + 1 < levels.length; i++) {
      const top = Math.min(levels[i].y, levels[i + 1].y)
      const bottom = Math.max(levels[i].y, levels[i + 1].y)
      ctx.fillStyle = paint.fibFills[i % 2]
      fillRect(scope, left, top, right, bottom)
    }

    // Dashed, faint trend line between the two anchors.
    ctx.save()
    ctx.strokeStyle = paint.fibTrend
    ctx.lineWidth = Math.max(1, Math.floor(hr))
    if (this.trendDashRatio !== hr) {
      this.trendDash = [4 * hr, 4 * hr]
      this.trendDashRatio = hr
    }
    ctx.setLineDash(this.trendDash)
    segment(scope, shape.x1, shape.y1, shape.x2, shape.y2)
    ctx.restore()

    const w = strokeSetup(scope, paint)
    for (let i = 0; i < levels.length; i++) hLine(scope, left, right, levels[i].y, w)

    // "0.618 (65,012.50)" just above each level line, left-aligned at the left anchor.
    ctx.setLineDash(NO_DASH)
    if (this.fontRatio !== vr) {
      this.font = `${Math.round(LABEL_FONT_PX * vr)}px ${fontFamily}`
      this.fontRatio = vr
    }
    ctx.font = this.font
    ctx.textAlign = 'left'
    ctx.textBaseline = 'bottom'
    ctx.fillStyle = paint.color
    for (let i = 0; i < levels.length; i++) {
      ctx.fillText(labels[i], Math.round((left + 4) * hr), Math.round((levels[i].y - 3) * vr))
    }
  }

  private drawHandles(scope: BitmapScope, item: RenderItem): void {
    const ctx = scope.context
    const hr = scope.horizontalPixelRatio
    const vr = scope.verticalPixelRatio
    const paint = item.paint
    if (paint.handleFor !== this.handles) {
      paint.handleStroke = handleStroke(paint.color, this.handles)
      paint.handleFor = this.handles
    }
    ctx.setLineDash(NO_DASH)
    ctx.lineWidth = Math.max(1, Math.round(1.5 * hr))
    ctx.strokeStyle = paint.handleStroke
    ctx.fillStyle = this.handles.fill
    const handles = item.handles as readonly Point[]
    for (let i = 0; i < handles.length; i++) {
      const h = handles[i]
      ctx.beginPath()
      ctx.arc(h.x * hr, h.y * vr, HANDLE_RADIUS * hr, 0, Math.PI * 2)
      ctx.fill()
      ctx.stroke()
    }
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
  /** contrastText(textFor), memoised: LWC asks for it on every axis paint. */
  private text_ = ''
  private textFor: string | null = null

  coordinate(): number {
    return this.y
  }
  text(): string {
    return this.label
  }
  textColor(): string {
    if (this.textFor !== this.color) {
      this.text_ = contrastText(this.color)
      this.textFor = this.color
    }
    return this.text_
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
  /** Used only when the source has no frame() of its own. */
  private ownShapes: ShapeCache | null = null
  /** Paint resources per drawing id. */
  private readonly paints = new Map<string, Paint>()
  /** What the current render items were built from; equal inputs reuse them as they are. */
  private built: {
    frame: ShapesFrame
    selectedId: string | null
    hoveredId: string | null
  } | null = null

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

  /** Theme change: handle fill/ring colours, then a redraw. */
  setHandleColors(colors: HandleColors): void {
    this.paneView.renderer_.handles = colors
    this.requestUpdate?.()
  }

  /** Redraw (interaction or drawings changed). */
  update(): void {
    this.requestUpdate?.()
  }

  updateAllViews(): void {
    const { selectedId, hoveredId } = this.source.view()
    const frame = this.currentFrame()
    const { drawings, shapes, width } = frame
    const built = this.built
    if (
      built === null ||
      built.frame !== frame ||
      built.selectedId !== selectedId ||
      built.hoveredId !== hoveredId
    ) {
      this.paneView.renderer_.set(this.buildItems(frame, selectedId, hoveredId), width)
      this.built = { frame, selectedId, hoveredId }
    }
    const labels: { y: number; text: string; color: string }[] = []
    for (let i = 0; i < drawings.length; i++) {
      const shape = shapes[i]
      if (shape && (shape.kind === 'hline' || shape.kind === 'hray')) {
        const drawing = drawings[i]
        labels.push({
          y: shape.y,
          text: this.formatPrice(drawing.anchors[0].price),
          color: drawing.style.color,
        })
      }
    }
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

  /**
   * LWC hit testing: our cursor (crosshair while a tool is active, pointer/grab over drawings)
   * and the drawing under the point as `externalId` 'drawing:<id>', which LWC reports as
   * `hoveredInfo.objectId` ('drawings' when a tool's crosshair is over no drawing).
   */
  hitTest(x: number, y: number): PrimitiveHoveredItem | null {
    const hit = this.source.hoverAt({ x, y })
    if (!hit) return null
    return {
      cursorStyle: hit.cursor,
      externalId: hit.id === null ? 'drawings' : drawingObjectId(hit.id),
      zOrder: 'top',
      distance: 0,
      itemType: 'primitive',
    }
  }

  private currentFrame(): ShapesFrame {
    if (this.source.frame) return this.source.frame()
    this.ownShapes ??= new ShapeCache(this.adapter)
    return this.ownShapes.frame(this.source.view().drawings)
  }

  private buildItems(
    frame: ShapesFrame,
    selectedId: string | null,
    hoveredId: string | null,
  ): RenderItem[] {
    const { drawings, shapes, width } = frame
    const items: RenderItem[] = []
    for (let i = 0; i < drawings.length; i++) {
      const shape = shapes[i]
      if (!shape) continue
      const drawing = drawings[i]
      const selected = drawing.id === selectedId
      const hovered = drawing.id === hoveredId
      const paint = this.paintFor(drawing)
      items.push({
        drawing,
        shape,
        selected,
        hovered,
        paint,
        rect:
          shape.kind === 'rectangle' ? normalizeRect(shape.x1, shape.y1, shape.x2, shape.y2) : null,
        labels: shape.kind === 'fib' ? fibLabelsFor(paint, drawing, shape) : null,
        handles: selected || hovered ? handlePoints(shape, width) : null,
      })
    }
    if (this.paints.size > 2 * drawings.length + 16) {
      const keep = new Set(drawings.map((d) => d.id))
      for (const id of this.paints.keys()) if (!keep.has(id)) this.paints.delete(id)
    }
    return items
  }

  private paintFor(drawing: Drawing): Paint {
    const cached = this.paints.get(drawing.id)
    if (cached && samePaintStyle(cached, drawing.kind, drawing.style)) return cached
    const paint = makePaint(drawing.kind, drawing.style)
    this.paints.set(drawing.id, paint)
    return paint
  }
}

/* ------------------------------------------------------------------------------------------ */
/* Drawing helpers (bitmap space)                                                              */
/* ------------------------------------------------------------------------------------------ */

function strokeSetup(scope: BitmapScope, paint: Paint): number {
  const ctx = scope.context
  const ratio = scope.horizontalPixelRatio
  const width = Math.max(1, Math.floor(paint.lineWidth * ratio))
  ctx.strokeStyle = paint.color
  ctx.lineWidth = width
  ctx.lineCap = 'butt'
  ctx.setLineDash(scaledDash(paint, ratio))
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

function strokeRect(scope: BitmapScope, r: Rect, lineWidth: number) {
  const hr = scope.horizontalPixelRatio
  const vr = scope.verticalPixelRatio
  const offset = lineWidth % 2 ? 0.5 : 0
  const x = Math.round(r.left * hr) + offset
  const y = Math.round(r.top * vr) + offset
  const w = Math.round(r.right * hr) - Math.round(r.left * hr)
  const h = Math.round(r.bottom * vr) - Math.round(r.top * vr)
  scope.context.strokeRect(x, y, w, h)
}
