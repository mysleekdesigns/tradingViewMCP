// Pure drawing geometry: projecting anchors to pane pixels, hit-testing, Fibonacci levels,
// rectangle normalisation and anchor-drag maths. Pixels are the main pane's CSS ("media")
// pixels: x from the pane's left edge, y from its top edge.

import { FIB_LEVELS, type Anchor, type Drawing } from './types'

/** Converts between anchors and pane pixels. Implemented on top of the chart in chartAdapter.ts. */
export interface CoordinateAdapter {
  /** Pane x → fractional logical bar index; null when the time scale is empty. */
  xToLogical(x: number): number | null
  /** Fractional logical bar index → pane x; null when the time scale is empty. */
  logicalToX(logical: number): number | null
  /** Anchor time (UNIX s) → fractional logical index; null with no bars. */
  timeToLogical(time: number): number | null
  /** Fractional logical index → time (UNIX s); exact inverse of timeToLogical. */
  logicalToTime(logical: number): number | null
  priceToY(price: number): number | null
  yToPrice(y: number): number | null
  /** Width of the main pane in CSS px (where full-width lines end). */
  paneWidth(): number
  /**
   * Changes whenever the time <-> logical mapping may have changed (bars or bar step replaced,
   * chart or series rebound). Optional: without it, projections are never cached (shapeCache.ts).
   */
  viewVersion?(): number
}

/** Stroke hit tolerance in px. */
export const HIT_TOLERANCE = 6
/** Handle radius when drawn, and its (larger) hit radius, in px. */
export const HANDLE_RADIUS = 5
export const HANDLE_HIT_RADIUS = 8
/** Inset of the hline handle from the pane edges when its anchor is off-screen. */
const HLINE_HANDLE_INSET = 24

export interface Point {
  x: number
  y: number
}

export interface FibLevel {
  level: number
  price: number
}

export interface FibLevelPx extends FibLevel {
  y: number
}

/** A drawing projected onto the pane. Coordinates may lie outside the visible area. */
export type Shape =
  | { kind: 'hline'; y: number; /** Anchor x; where the handle sits. */ ax: number }
  | { kind: 'hray'; x: number; y: number }
  | { kind: 'trendline'; x1: number; y1: number; x2: number; y2: number }
  | { kind: 'fib'; x1: number; y1: number; x2: number; y2: number; levels: FibLevelPx[] }
  | { kind: 'rectangle'; x1: number; y1: number; x2: number; y2: number }

export type HitPart = { part: 'body' } | { part: 'handle'; index: number }

export type Hit = HitPart & {
  /** Distance from the pointer to the hit feature, px (0 inside a fill). */
  distance: number
}

/* ------------------------------------------------------------------------------------------ */
/* Basic maths                                                                                 */
/* ------------------------------------------------------------------------------------------ */

/** Shortest distance from P to the segment AB (a point if A = B). */
export function distanceToSegment(
  px: number,
  py: number,
  ax: number,
  ay: number,
  bx: number,
  by: number,
): number {
  const dx = bx - ax
  const dy = by - ay
  const len2 = dx * dx + dy * dy
  let t = len2 === 0 ? 0 : ((px - ax) * dx + (py - ay) * dy) / len2
  t = Math.max(0, Math.min(1, t))
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy))
}

export interface Rect {
  left: number
  top: number
  right: number
  bottom: number
}

/** Rectangle spanned by two opposite corners, whichever way round they were drawn. */
export function normalizeRect(x1: number, y1: number, x2: number, y2: number): Rect {
  return {
    left: Math.min(x1, x2),
    right: Math.max(x1, x2),
    top: Math.min(y1, y2),
    bottom: Math.max(y1, y2),
  }
}

/** Price of each FIB_LEVELS level: anchors[0] is level 0, anchors[1] is level 1. */
export function fibLevelPrices(price0: number, price1: number): FibLevel[] {
  return FIB_LEVELS.map((level) => ({ level, price: price0 + (price1 - price0) * level }))
}

/* ------------------------------------------------------------------------------------------ */
/* Projection                                                                                  */
/* ------------------------------------------------------------------------------------------ */

export function timeToX(adapter: CoordinateAdapter, time: number): number | null {
  const logical = adapter.timeToLogical(time)
  return logical === null ? null : adapter.logicalToX(logical)
}

/** Pane x → open time of the nearest bar (extrapolated beyond the data), or null. */
export function xToSnappedTime(adapter: CoordinateAdapter, x: number): number | null {
  const logical = adapter.xToLogical(x)
  if (logical === null || !Number.isFinite(logical)) return null
  return adapter.logicalToTime(Math.round(logical))
}

/** Pane point → bar-snapped anchor (free price), or null when the chart has no data. */
export function pointToAnchor(adapter: CoordinateAdapter, p: Point): Anchor | null {
  const time = xToSnappedTime(adapter, p.x)
  const price = adapter.yToPrice(p.y)
  if (time === null || price === null || !Number.isFinite(price)) return null
  return { time, price }
}

function anchorToPoint(adapter: CoordinateAdapter, a: Anchor | undefined): Point | null {
  if (!a) return null
  const x = timeToX(adapter, a.time)
  const y = adapter.priceToY(a.price)
  return x === null || y === null ? null : { x, y }
}

/** Project a drawing onto the pane, or null when it cannot be placed (no data yet). */
export function projectDrawing(d: Drawing, adapter: CoordinateAdapter): Shape | null {
  const a = anchorToPoint(adapter, d.anchors[0])
  if (!a) return null
  switch (d.kind) {
    case 'hline':
      return { kind: 'hline', y: a.y, ax: a.x }
    case 'hray':
      return { kind: 'hray', x: a.x, y: a.y }
    case 'trendline':
    case 'rectangle': {
      const b = anchorToPoint(adapter, d.anchors[1])
      if (!b) return null
      return { kind: d.kind, x1: a.x, y1: a.y, x2: b.x, y2: b.y }
    }
    case 'fib': {
      const b = anchorToPoint(adapter, d.anchors[1])
      if (!b) return null
      const levels: FibLevelPx[] = []
      for (const lv of fibLevelPrices(d.anchors[0].price, d.anchors[1].price)) {
        const y = adapter.priceToY(lv.price)
        if (y !== null) levels.push({ ...lv, y })
      }
      return { kind: 'fib', x1: a.x, y1: a.y, x2: b.x, y2: b.y, levels }
    }
  }
}

/* ------------------------------------------------------------------------------------------ */
/* Handles                                                                                     */
/* ------------------------------------------------------------------------------------------ */

/**
 * Handle positions of a selected drawing. Index i < anchors.length is anchors[i]. Rectangles
 * also get the two other corners: 2 = (anchor0.time, anchor1.price), 3 = (anchor1.time,
 * anchor0.price). The hline handle sits at its anchor x, kept inside the pane.
 */
export function handlePoints(shape: Shape, paneWidth: number): Point[] {
  switch (shape.kind) {
    case 'hline': {
      const inset = Math.min(HLINE_HANDLE_INSET, paneWidth / 2)
      const x = Math.max(inset, Math.min(paneWidth - inset, shape.ax))
      return [{ x, y: shape.y }]
    }
    case 'hray':
      return [{ x: shape.x, y: shape.y }]
    case 'trendline':
    case 'fib':
      return [
        { x: shape.x1, y: shape.y1 },
        { x: shape.x2, y: shape.y2 },
      ]
    case 'rectangle':
      return [
        { x: shape.x1, y: shape.y1 },
        { x: shape.x2, y: shape.y2 },
        { x: shape.x1, y: shape.y2 },
        { x: shape.x2, y: shape.y1 },
      ]
  }
}

/** Nearest handle within HANDLE_HIT_RADIUS of p, or null. */
export function hitTestHandles(
  shape: Shape,
  p: Point,
  paneWidth: number,
  radius: number = HANDLE_HIT_RADIUS,
): Hit | null {
  let best: Hit | null = null
  const handles = handlePoints(shape, paneWidth)
  for (let index = 0; index < handles.length; index++) {
    const h = handles[index]
    const distance = Math.hypot(p.x - h.x, p.y - h.y)
    if (distance <= radius && (best === null || distance < best.distance)) {
      best = { part: 'handle', index, distance }
    }
  }
  return best
}

/* ------------------------------------------------------------------------------------------ */
/* Body hit-testing                                                                            */
/* ------------------------------------------------------------------------------------------ */

/**
 * Distance from p to the drawing's body if within `tol`, else null.
 * - hline: anywhere across the pane width, within tol vertically.
 * - hray: only from its start x rightwards.
 * - trendline: distance to the segment.
 * - fib: any level line across the anchors' x range, or inside the area between levels 0 and 1.
 * - rectangle: the border band or the fill.
 */
export function hitTestBody(
  shape: Shape,
  p: Point,
  paneWidth: number,
  tol: number = HIT_TOLERANCE,
): number | null {
  const within = (d: number) => (d <= tol ? d : null)
  switch (shape.kind) {
    case 'hline':
      if (p.x < 0 || p.x > paneWidth) return null
      return within(Math.abs(p.y - shape.y))
    case 'hray':
      if (p.x < shape.x - tol) return null
      return within(distanceToSegment(p.x, p.y, shape.x, shape.y, Math.max(shape.x, p.x), shape.y))
    case 'trendline':
      return within(distanceToSegment(p.x, p.y, shape.x1, shape.y1, shape.x2, shape.y2))
    case 'fib': {
      const left = Math.min(shape.x1, shape.x2)
      const right = Math.max(shape.x1, shape.x2)
      let best: number | null = null
      for (const lv of shape.levels) {
        const d = distanceToSegment(p.x, p.y, left, lv.y, right, lv.y)
        if (d <= tol && (best === null || d < best)) best = d
      }
      if (best !== null) return best
      const r = normalizeRect(left, shape.y1, right, shape.y2)
      return insideRect(r, p) ? tol : null
    }
    case 'rectangle': {
      const r = normalizeRect(shape.x1, shape.y1, shape.x2, shape.y2)
      const edge = distanceToRectEdge(r, p)
      if (edge <= tol) return edge
      // The fill counts too, but a nearby stroke of another drawing should win.
      return insideRect(r, p) ? tol : null
    }
  }
}

function insideRect(r: Rect, p: Point): boolean {
  return p.x >= r.left && p.x <= r.right && p.y >= r.top && p.y <= r.bottom
}

function distanceToRectEdge(r: Rect, p: Point): number {
  return Math.min(
    distanceToSegment(p.x, p.y, r.left, r.top, r.right, r.top),
    distanceToSegment(p.x, p.y, r.left, r.bottom, r.right, r.bottom),
    distanceToSegment(p.x, p.y, r.left, r.top, r.left, r.bottom),
    distanceToSegment(p.x, p.y, r.right, r.top, r.right, r.bottom),
  )
}

/* ------------------------------------------------------------------------------------------ */
/* Anchor maths for drags                                                                      */
/* ------------------------------------------------------------------------------------------ */

/**
 * Anchors after dragging handle `index` of a drawing to `to`. Rectangle handles 2 and 3 are
 * the derived corners (see handlePoints) and move one coordinate of each anchor.
 */
export function moveHandle(d: Drawing, index: number, to: Anchor): Anchor[] {
  const anchors = d.anchors.map((a) => ({ ...a }))
  if (index < anchors.length) {
    anchors[index] = { ...to }
  } else if (d.kind === 'rectangle' && anchors.length === 2) {
    if (index === 2) {
      anchors[0].time = to.time
      anchors[1].price = to.price
    } else if (index === 3) {
      anchors[1].time = to.time
      anchors[0].price = to.price
    }
  }
  return anchors
}

/**
 * Anchors after moving the whole drawing by `barDelta` whole bars and `priceDelta`.
 * Times shift in logical-index space so off-bar anchors keep their place between bars.
 */
export function shiftAnchors(
  anchors: readonly Anchor[],
  barDelta: number,
  priceDelta: number,
  adapter: Pick<CoordinateAdapter, 'timeToLogical' | 'logicalToTime'>,
): Anchor[] {
  return anchors.map((a) => {
    let time = a.time
    if (barDelta !== 0) {
      const logical = adapter.timeToLogical(a.time)
      const shifted = logical === null ? null : adapter.logicalToTime(logical + barDelta)
      if (shifted !== null) time = shifted
    }
    return { time, price: a.price + priceDelta }
  })
}

/* ------------------------------------------------------------------------------------------ */
/* Colours / formatting helpers used by the renderer                                           */
/* ------------------------------------------------------------------------------------------ */

/** `#rrggbb` (or `#rgb`) → `rgba(r, g, b, alpha)`; other strings are returned unchanged. */
export function withAlpha(color: string, alpha: number): string {
  let hex = color.trim()
  if (/^#[0-9a-f]{3}$/i.test(hex)) hex = '#' + [...hex.slice(1)].map((c) => c + c).join('')
  if (!/^#[0-9a-f]{6}$/i.test(hex)) return color
  const r = parseInt(hex.slice(1, 3), 16)
  const g = parseInt(hex.slice(3, 5), 16)
  const b = parseInt(hex.slice(5, 7), 16)
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

/** Readable label text on a `#rrggbb` background: dark on light colours, white otherwise. */
export function contrastText(color: string): string {
  const m = /^#([0-9a-f]{6})$/i.exec(color.trim())
  if (!m) return '#ffffff'
  const v = parseInt(m[1], 16)
  const lum = (0.299 * ((v >> 16) & 255) + 0.587 * ((v >> 8) & 255) + 0.114 * (v & 255)) / 255
  return lum > 0.6 ? '#131722' : '#ffffff'
}

/** Fib label price: legend precision plus thousands separators, e.g. 65012.5 → "65,012.50". */
export function formatLevelPrice(price: number): string {
  if (!Number.isFinite(price)) return '—'
  const abs = Math.abs(price)
  const decimals = abs >= 1 || abs === 0 ? 2 : abs >= 0.01 ? 4 : 8
  return price.toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })
}

/** "0.618 (65,012.50)" */
export function fibLabel(level: number, price: number): string {
  return `${level} (${formatLevelPrice(price)})`
}
