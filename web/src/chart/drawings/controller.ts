// Drawing interaction state machine. Pure: it is fed pointer and key events in main-pane CSS
// pixels plus a CoordinateAdapter, and reports results through callbacks. Chart.tsx wires DOM
// events to it and decides (from the return values) whether Lightweight Charts sees an event.
//
// States:
// - idle: cursor mode or a tool waiting for its first click.
// - creating: a 2-anchor drawing whose second anchor follows the pointer. Either the button is
//   still held (press-drag-release) or it was released without moving (click, move, click).
// - dragging: moving a drawing's body or one of its handles in cursor mode.

import {
  HIT_TOLERANCE,
  hitTestBody,
  hitTestHandles,
  moveHandle,
  pointToAnchor,
  shiftAnchors,
  xToSnappedTime,
  type CoordinateAdapter,
  type HitPart,
  type Point,
} from './geometry'
import { ShapeCache, type ShapesFrame } from './shapeCache'
import {
  ANCHOR_COUNT,
  DEFAULT_DRAWING_STYLE,
  type Anchor,
  type Drawing,
  type DrawingKind,
  type DrawingPatch,
  type DrawingStyle,
  type ToolMode,
} from './types'

/** Pointer travel (px) that turns a press into a drag. */
export const DRAG_THRESHOLD = 3

export interface ControllerCallbacks {
  /**
   * A new drawing was finished. Return `false` when the store rejected it: the controller drops
   * it at once (no ghost drawing) and does not select it. `true` / no return value = accepted.
   */
  onCreate?: (drawing: Drawing) => boolean | void
  onSelect?: (id: string | null) => void
  /** A creation finished, or Escape cancelled the tool: the app should go back to 'cursor'. */
  onToolDone?: () => void
  /**
   * A move / handle drag ended with a change (once per drag). Return `false` when the store
   * rejected the patch: the drawing snaps back to its committed anchors at once.
   */
  onChange?: (id: string, patch: DrawingPatch) => boolean | void
  onDelete?: (id: string) => void
  /** True while creating or dragging: the chart must not pan or zoom. */
  onBusyChange?: (busy: boolean) => void
  /** What view() returns changed: redraw. */
  onRender?: () => void
}

export interface PointerInput extends Point {
  /** MouseEvent.button; 0 = primary. Defaults to 0. */
  button?: number
}

export interface ContextMenuHit {
  price: number | null
  time: number | null
  drawingId: string | null
}

/**
 * `time` when it is a plotted bar's time (between the first and last plotted bar inclusive),
 * else null. Snapped context-menu times extrapolate past the data (future whitespace, or before
 * the first loaded bar); bar-anchored indicators started there would never draw.
 */
export function plottedBarTime(
  time: number | null,
  bars: readonly { time: number }[] | null,
): number | null {
  if (time === null || bars === null || bars.length === 0) return null
  return time >= bars[0].time && time <= bars[bars.length - 1].time ? time : null
}

export interface DrawingsView {
  /** Committed drawings with any in-progress move applied, plus the creation preview last. */
  drawings: readonly Drawing[]
  /** Drawing that shows handles (the selection, or the preview while creating). */
  selectedId: string | null
  /**
   * Drawing under the mouse (from LWC hit testing) that shows faint handles: set only in cursor
   * mode while idle, when it exists and is not the selection.
   */
  hoveredId: string | null
}

/** What the pointer is over: the cursor to show and the drawing id behind it, if any. */
export interface HoverHit {
  cursor: CursorStyle
  /** The drawing under the point (or being dragged); null for a tool's crosshair over nothing. */
  id: string | null
}

export type CursorStyle = 'crosshair' | 'pointer' | 'grab' | 'grabbing' | 'move'

interface CreatingState {
  kind: 'creating'
  tool: DrawingKind
  id: string
  anchors: [Anchor, Anchor]
  /** Button still held since the first press. */
  pressed: boolean
  dragged: boolean
  down: Point
}

interface DraggingState {
  kind: 'dragging'
  id: string
  part: HitPart
  down: Point
  downLogical: number | null
  downPrice: number | null
  original: Drawing
  anchors: Anchor[]
  moved: boolean
}

type State = { kind: 'idle' } | CreatingState | DraggingState

export interface ControllerOptions {
  newId?: () => string
  symbol?: string
  style?: DrawingStyle
  tool?: ToolMode
}

function defaultId(): string {
  return crypto.randomUUID()
}

export class DrawingController {
  private readonly adapter: CoordinateAdapter
  private callbacks: ControllerCallbacks
  private readonly newId: () => string

  private base: readonly Drawing[] = []
  private selected: string | null = null
  private hovered: string | null = null
  private toolMode: ToolMode
  private symbol: string
  private style: DrawingStyle
  private state: State = { kind: 'idle' }
  /** The pointer-up of a press that already finished something; swallow it. */
  private swallowUp = false
  private busyFlag = false
  /**
   * Committed locally but not yet echoed back through setDrawings (a moved drawing's anchors,
   * a just-created drawing). Shown until the next setDrawings so nothing flickers back.
   */
  private pendingMove: { id: string; anchors: Anchor[] } | null = null
  private pendingCreated: Drawing | null = null
  private viewCache: DrawingsView | null = null
  /** Projections for the current view, shared with the renderer through frame(). */
  private readonly shapes: ShapeCache

  constructor(
    adapter: CoordinateAdapter,
    callbacks: ControllerCallbacks = {},
    options: ControllerOptions = {},
  ) {
    this.adapter = adapter
    this.callbacks = callbacks
    this.newId = options.newId ?? defaultId
    this.symbol = options.symbol ?? ''
    this.style = options.style ?? DEFAULT_DRAWING_STYLE
    this.toolMode = options.tool ?? 'cursor'
    this.shapes = new ShapeCache(adapter)
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Inputs from props                                                                         */
  /* ---------------------------------------------------------------------------------------- */

  setCallbacks(callbacks: ControllerCallbacks): void {
    this.callbacks = callbacks
  }

  setDrawings(drawings: readonly Drawing[]): void {
    if (drawings === this.base && !this.pendingMove && !this.pendingCreated) return
    this.base = drawings
    this.pendingMove = null
    this.pendingCreated = null
    if (this.state.kind === 'dragging') {
      const id = this.state.id
      if (!drawings.some((d) => d.id === id)) this.setState({ kind: 'idle' })
    }
    this.changed()
  }

  setSelected(id: string | null): void {
    if (id === this.selected) return
    this.selected = id
    this.changed()
  }

  setTool(tool: ToolMode): void {
    if (tool === this.toolMode) return
    this.toolMode = tool
    if (this.state.kind === 'creating') {
      this.setState({ kind: 'idle' })
      this.changed()
    } else if (this.hovered !== null) {
      // Hover handles only show in cursor mode.
      this.changed()
    }
  }

  /**
   * Drawing under the mouse, from LWC hit testing (`resolveHover` in Chart). A hovered drawing
   * that is not selected shows its anchor handles faintly, as in TradingView.
   */
  setHovered(id: string | null): void {
    if (id === this.hovered) return
    this.hovered = id
    this.changed()
  }

  setSymbol(symbol: string): void {
    this.symbol = symbol
  }

  setStyle(style: DrawingStyle | undefined): void {
    this.style = style ?? DEFAULT_DRAWING_STYLE
    if (this.state.kind === 'creating') this.changed()
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Queries                                                                                   */
  /* ---------------------------------------------------------------------------------------- */

  get tool(): ToolMode {
    return this.toolMode
  }

  get selectedId(): string | null {
    return this.selected
  }

  /** Last id given to setHovered (whether or not it currently shows handles). */
  get hoveredId(): string | null {
    return this.hovered
  }

  /** Creating or dragging (or holding a press that created something). */
  get busy(): boolean {
    return this.busyFlag
  }

  /** True while the primary button is held in an interaction the controller owns. */
  get pressed(): boolean {
    return (
      this.swallowUp ||
      this.state.kind === 'dragging' ||
      (this.state.kind === 'creating' && this.state.pressed)
    )
  }

  view(): DrawingsView {
    if (this.viewCache) return this.viewCache
    const s = this.state
    let drawings: Drawing[] = this.base.map((d) => {
      if (s.kind === 'dragging' && s.id === d.id) return { ...d, anchors: s.anchors }
      if (this.pendingMove && this.pendingMove.id === d.id)
        return { ...d, anchors: this.pendingMove.anchors }
      return d
    })
    let selectedId = this.selected
    if (this.pendingCreated && !drawings.some((d) => d.id === this.pendingCreated?.id)) {
      drawings.push(this.pendingCreated)
    }
    if (s.kind === 'creating') {
      drawings = [...drawings, this.previewDrawing(s)]
      selectedId = s.id
    }
    const hovered = this.hovered
    const hoveredId =
      hovered !== null &&
      s.kind === 'idle' &&
      this.toolMode === 'cursor' &&
      hovered !== selectedId &&
      drawings.some((d) => d.id === hovered)
        ? hovered
        : null
    this.viewCache = { drawings, selectedId, hoveredId }
    return this.viewCache
  }

  /**
   * view().drawings projected for the chart's current view. The renderer builds its frame from
   * this in updateAllViews, and hitTest reuses the same shapes while the view is unchanged.
   */
  frame(): ShapesFrame {
    return this.shapes.frame(this.view().drawings)
  }

  /** Cursor to show at p when no button is held. */
  cursorAt(p: Point): CursorStyle | null {
    return this.hoverAt(p)?.cursor ?? null
  }

  /**
   * Cursor and drawing at p (null = nothing of ours there). Feeds the primitive's hitTest, so
   * LWC reports the drawing as `hoveredInfo.objectId`. While a tool is active the crosshair
   * covers the whole pane and no drawing is reported.
   */
  hoverAt(p: Point): HoverHit | null {
    const s = this.state
    if (s.kind === 'dragging') {
      return { cursor: s.part.part === 'handle' ? 'grabbing' : 'move', id: s.id }
    }
    if (this.toolMode !== 'cursor' || s.kind === 'creating')
      return { cursor: 'crosshair', id: null }
    const hit = this.hitTest(p)
    if (!hit) return null
    return { cursor: hit.part.part === 'handle' ? 'grab' : 'pointer', id: hit.id }
  }

  /** Topmost drawing at p: the selected drawing's handles first, then the nearest stroke/fill. */
  hitTest(p: Point): { id: string; part: HitPart; distance: number } | null {
    // The frame's shapes: projected once per view (the selected drawing included), not per move.
    const { drawings, shapes, width } = this.frame()
    const previewId = this.state.kind === 'creating' ? this.state.id : null
    if (this.selected) {
      const selected = this.selected
      const i = drawings.findIndex((d) => d.id === selected)
      const shape = i >= 0 ? shapes[i] : null
      const h = shape ? hitTestHandles(shape, p, width) : null
      if (h) return { id: drawings[i].id, part: h, distance: h.distance }
    }
    let best: { id: string; part: HitPart; distance: number } | null = null
    // Later drawings draw on top, so walk backwards and only replace on a strictly closer hit.
    for (let i = drawings.length - 1; i >= 0; i--) {
      const d = drawings[i]
      if (d.id === previewId) continue
      const shape = shapes[i]
      if (!shape) continue
      const distance = hitTestBody(shape, p, width, HIT_TOLERANCE)
      if (distance !== null && (best === null || distance < best.distance)) {
        best = { id: d.id, part: { part: 'body' }, distance }
      }
    }
    return best
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Pointer events                                                                            */
  /* ---------------------------------------------------------------------------------------- */

  /** Returns true when the controller consumed the press (the chart must not pan). */
  pointerDown(p: PointerInput): boolean {
    if ((p.button ?? 0) !== 0) return false
    const s = this.state

    // Second click of click-move-click creation.
    if (s.kind === 'creating' && !s.pressed) {
      const a = validAnchor(pointToAnchor(this.adapter, p)) ?? s.anchors[1]
      this.swallowUp = true
      this.finishCreate(s, a)
      return true
    }
    if (s.kind !== 'idle') return true

    if (this.toolMode !== 'cursor') {
      const tool = this.toolMode
      const a = pointToAnchor(this.adapter, p)
      if (!a) return false
      if (ANCHOR_COUNT[tool] === 1) {
        this.swallowUp = true
        this.updateBusy()
        this.emitCreate(tool, [a])
        return true
      }
      this.setState({
        kind: 'creating',
        tool,
        id: this.newId(),
        anchors: [a, { ...a }],
        pressed: true,
        dragged: false,
        down: { x: p.x, y: p.y },
      })
      this.changed()
      return true
    }

    // Cursor mode.
    const hit = this.hitTest(p)
    if (!hit) {
      if (this.selected !== null) {
        this.selected = null
        this.changed()
        this.callbacks.onSelect?.(null)
      }
      return false
    }
    const drawing = this.view().drawings.find((d) => d.id === hit.id)
    if (!drawing) return false
    if (this.selected !== hit.id) {
      this.selected = hit.id
      this.changed()
      this.callbacks.onSelect?.(hit.id)
    }
    this.pendingMove = null
    this.setState({
      kind: 'dragging',
      id: hit.id,
      part: hit.part,
      down: { x: p.x, y: p.y },
      downLogical: this.adapter.xToLogical(p.x),
      downPrice: this.adapter.yToPrice(p.y),
      original: drawing,
      anchors: drawing.anchors,
      moved: false,
    })
    return true
  }

  /** Returns true while the controller owns the pointer (creating or dragging). */
  pointerMove(p: Point): boolean {
    const s = this.state
    if (s.kind === 'creating') {
      if (s.pressed && Math.hypot(p.x - s.down.x, p.y - s.down.y) > DRAG_THRESHOLD) {
        s.dragged = true
      }
      // A pointer below price 0 keeps the last valid second anchor.
      const a = validAnchor(pointToAnchor(this.adapter, p))
      if (a) {
        s.anchors = [s.anchors[0], a]
        this.changed()
      }
      return true
    }
    if (s.kind === 'dragging') {
      if (!s.moved && Math.hypot(p.x - s.down.x, p.y - s.down.y) <= DRAG_THRESHOLD) return true
      s.moved = true
      const next = this.dragAnchors(s, p)
      if (next) {
        s.anchors = next
        this.changed()
      }
      return true
    }
    return this.swallowUp
  }

  /** Returns true when the release belonged to the controller. */
  pointerUp(p: Point): boolean {
    if (this.swallowUp) {
      this.swallowUp = false
      this.updateBusy()
      return true
    }
    const s = this.state
    if (s.kind === 'creating' && s.pressed) {
      if (s.dragged) {
        this.finishCreate(s, validAnchor(pointToAnchor(this.adapter, p)) ?? s.anchors[1])
      } else {
        // A click: keep following the pointer until the second click.
        s.pressed = false
      }
      return true
    }
    if (s.kind === 'dragging') {
      if (s.moved) {
        const next = this.dragAnchors(s, p) ?? s.anchors
        const changed = !sameAnchors(next, s.original.anchors)
        const pending = changed && this.callbacks.onChange ? { id: s.id, anchors: next } : null
        this.pendingMove = pending
        this.setState({ kind: 'idle' })
        this.changed()
        if (changed) {
          const accepted = this.callbacks.onChange?.(s.id, { anchors: next.map((a) => ({ ...a })) })
          // Rejected: show the committed drawing again (unless setDrawings already replaced it).
          if (accepted === false && pending !== null && this.pendingMove === pending) {
            this.pendingMove = null
            this.changed()
          }
        }
      } else {
        this.setState({ kind: 'idle' })
        this.changed()
      }
      return true
    }
    return false
  }

  /** The browser took the pointer away (touch scroll, lost capture): abandon the interaction. */
  pointerCancel(): void {
    this.swallowUp = false
    if (this.state.kind !== 'idle') {
      this.setState({ kind: 'idle' })
      this.changed()
    } else {
      this.updateBusy()
    }
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Keys and context menu                                                                     */
  /* ---------------------------------------------------------------------------------------- */

  /** Escape / Delete / Backspace. Returns true when the key was handled. */
  keyDown(key: string): boolean {
    const s = this.state
    if (key === 'Escape') {
      if (s.kind === 'creating') {
        this.swallowUp = false
        this.setState({ kind: 'idle' })
        this.changed()
        this.callbacks.onToolDone?.()
        return true
      }
      if (s.kind === 'dragging') {
        this.setState({ kind: 'idle' })
        this.changed()
        return true
      }
      if (this.toolMode !== 'cursor') {
        this.callbacks.onToolDone?.()
        return true
      }
      if (this.selected !== null) {
        this.selected = null
        this.changed()
        this.callbacks.onSelect?.(null)
        return true
      }
      return false
    }
    if (key === 'Delete' || key === 'Backspace') {
      if (s.kind !== 'idle' || this.selected === null) return false
      const id = this.selected
      if (!this.view().drawings.some((d) => d.id === id)) return false
      this.selected = null
      this.changed()
      this.callbacks.onDelete?.(id)
      this.callbacks.onSelect?.(null)
      return true
    }
    return false
  }

  /**
   * Right-click at p. Cancels an in-progress creation (returns null: no menu) or drag; otherwise
   * selects the drawing under the cursor and returns the price / snapped time there.
   */
  contextMenu(p: Point): ContextMenuHit | null {
    const s = this.state
    if (s.kind === 'creating') {
      this.swallowUp = false
      this.setState({ kind: 'idle' })
      this.changed()
      this.callbacks.onToolDone?.()
      return null
    }
    if (s.kind === 'dragging') {
      this.setState({ kind: 'idle' })
      this.changed()
    }
    const rawPrice = this.adapter.yToPrice(p.y)
    const price = rawPrice !== null && Number.isFinite(rawPrice) ? rawPrice : null
    const time = xToSnappedTime(this.adapter, p.x)
    const hit = this.toolMode === 'cursor' ? this.hitTest(p) : null
    const drawingId = hit?.id ?? null
    if (drawingId !== null && drawingId !== this.selected) {
      this.selected = drawingId
      this.changed()
      this.callbacks.onSelect?.(drawingId)
    }
    return { price, time, drawingId }
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Internals                                                                                 */
  /* ---------------------------------------------------------------------------------------- */

  private previewDrawing(s: CreatingState): Drawing {
    return {
      id: s.id,
      symbol: this.symbol,
      kind: s.tool,
      anchors: s.anchors,
      style: this.style,
    }
  }

  private finishCreate(s: CreatingState, second: Anchor): void {
    this.setState({ kind: 'idle' })
    this.emitCreate(s.tool, [s.anchors[0], second], s.id)
  }

  private emitCreate(kind: DrawingKind, anchors: Anchor[], id: string = this.newId()): void {
    const drawing: Drawing = {
      id,
      symbol: this.symbol,
      kind,
      anchors: anchors.map((a) => ({ ...a })),
      style: { ...this.style },
    }
    const previous = this.selected
    if (this.callbacks.onCreate) this.pendingCreated = drawing
    this.selected = id
    this.changed()
    const accepted = this.callbacks.onCreate?.(drawing)
    if (accepted === false) {
      // Rejected (e.g. the drawing limit, or a price <= 0): no ghost drawing, nothing selected
      // that does not exist. The app never heard of this id, so it is not told to select it.
      if (this.pendingCreated === drawing) this.pendingCreated = null
      if (this.selected === id) this.selected = previous
      this.changed()
    } else {
      this.callbacks.onSelect?.(id)
    }
    this.callbacks.onToolDone?.()
  }

  private dragAnchors(s: DraggingState, p: Point): Anchor[] | null {
    if (s.part.part === 'handle') {
      const a = validAnchor(pointToAnchor(this.adapter, p))
      return a ? moveHandle(s.original, s.part.index, a) : null
    }
    const logical = this.adapter.xToLogical(p.x)
    const price = this.adapter.yToPrice(p.y)
    if (logical === null || price === null || s.downLogical === null || s.downPrice === null) {
      return null
    }
    const barDelta = Math.round(logical - s.downLogical)
    const next = shiftAnchors(s.original.anchors, barDelta, price - s.downPrice, this.adapter)
    // A move that would take any anchor to price <= 0 is not applied (the store rejects such
    // drawings); the drawing stays at its last valid position.
    return next.every((a) => validPrice(a.price)) ? next : null
  }

  private setState(next: State): void {
    this.state = next
    this.updateBusy()
  }

  private updateBusy(): void {
    const busy = this.state.kind !== 'idle' || this.swallowUp
    if (busy === this.busyFlag) return
    this.busyFlag = busy
    this.callbacks.onBusyChange?.(busy)
  }

  private changed(): void {
    this.viewCache = null
    this.callbacks.onRender?.()
  }
}

function validPrice(price: number): boolean {
  return Number.isFinite(price) && price > 0
}

/** The anchor, or null when its price is one a drawing cannot hold (not finite or <= 0). */
function validAnchor(a: Anchor | null): Anchor | null {
  return a !== null && validPrice(a.price) ? a : null
}

function sameAnchors(a: readonly Anchor[], b: readonly Anchor[]): boolean {
  return a.length === b.length && a.every((x, i) => x.time === b[i].time && x.price === b[i].price)
}
