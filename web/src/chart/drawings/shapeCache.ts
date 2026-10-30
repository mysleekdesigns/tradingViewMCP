// Projected shapes for the current chart view, shared by the renderer (updateAllViews) and the
// controller's hit test, so a mouse move hit-tests the shapes the frame already holds instead of
// re-projecting every drawing (two binary searches and four coordinate conversions each, plus
// seven more for a fib).
//
// A projection depends on a drawing's kind and anchors and on the adapter's mapping. The mapping
// is fingerprinted by a FrameKey: the x of logical 0 and 1 (time scale is affine in the logical
// index: scroll and zoom move them), the y of two reference prices (every price scale mode maps
// prices through a monotone function fixed by its range, height and base value, so any change
// moves at least one), the pane width, and the adapter's viewVersion (bars or bar step changed,
// or the chart / series was rebound: anything time -> logical depends on). An adapter without
// viewVersion is never cached: every call projects afresh, exactly as before.
//
// Per drawing, an entry keeps the last shape with the kind and anchors it was built from, so a
// drag re-projects only the dragged drawing. Shapes are shared and must not be mutated.

import { projectDrawing, type CoordinateAdapter, type Shape } from './geometry'
import type { Drawing } from './types'

/** The current drawings projected for the current view (`shapes[i]` is `drawings[i]`). */
export interface ShapesFrame {
  readonly drawings: readonly Drawing[]
  readonly shapes: readonly (Shape | null)[]
  /** Pane width in CSS px when the shapes were projected. */
  readonly width: number
}

interface FrameKey {
  x0: number
  x1: number
  y0: number
  y1: number
  width: number
  version: number
}

interface Entry {
  kind: Drawing['kind']
  count: number
  t0: number
  p0: number
  t1: number
  p1: number
  /** Key generation the shape was projected under. */
  generation: number
  shape: Shape | null
}

function sameKey(a: FrameKey, b: FrameKey): boolean {
  return (
    a.x0 === b.x0 &&
    a.x1 === b.x1 &&
    a.y0 === b.y0 &&
    a.y1 === b.y1 &&
    a.width === b.width &&
    a.version === b.version
  )
}

/** Two distinct positive prices spanning the drawings' anchors (a log scale needs > 0). */
function referencePrices(drawings: readonly Drawing[]): [number, number] {
  let lo = Infinity
  let hi = -Infinity
  for (const d of drawings) {
    for (const a of d.anchors) {
      if (Number.isFinite(a.price) && a.price > 0) {
        if (a.price < lo) lo = a.price
        if (a.price > hi) hi = a.price
      }
    }
  }
  if (lo === Infinity) return [1, 2]
  return lo < hi ? [lo, hi] : [lo, lo * 2]
}

export class ShapeCache {
  private readonly adapter: CoordinateAdapter
  private readonly entries = new Map<string, Entry>()
  private refs: [number, number] = [1, 2]
  private key: FrameKey | null = null
  private generation = 0
  private last: ShapesFrame | null = null

  constructor(adapter: CoordinateAdapter) {
    this.adapter = adapter
  }

  /**
   * Shapes of `drawings` for the current view. Returns the previous frame object itself when
   * the drawings array and the view are both unchanged.
   */
  frame(drawings: readonly Drawing[]): ShapesFrame {
    const previousKey = this.key
    const key = this.currentKey(drawings)
    if (key === null) {
      this.key = null
      this.last = null
      this.entries.clear()
      return {
        drawings,
        shapes: drawings.map((d) => projectDrawing(d, this.adapter)),
        width: this.adapter.paneWidth(),
      }
    }
    const last = this.last
    // currentKey returns the previous key object itself only when the view is unchanged.
    if (last !== null && last.drawings === drawings && key === previousKey) return last
    const shapes = drawings.map((d) => this.shapeOf(d))
    if (this.entries.size > 2 * drawings.length + 16) this.prune(drawings)
    this.last = { drawings, shapes, width: key.width }
    return this.last
  }

  /**
   * The key for the current view; `this.key` itself (same object) when the view is unchanged.
   * A change starts a new generation, so every entry re-projects on its next use.
   */
  private currentKey(drawings: readonly Drawing[]): FrameKey | null {
    const version = this.adapter.viewVersion?.()
    if (version === undefined) return null
    let key = this.measure(this.refs, version)
    if (key !== null && this.key !== null && sameKey(key, this.key)) return this.key
    // A new view: re-pick the reference prices around the drawings and start a generation.
    this.refs = referencePrices(drawings)
    key = this.measure(this.refs, version)
    this.generation++
    this.key = key
    return key
  }

  private measure(refs: readonly [number, number], version: number): FrameKey | null {
    const a = this.adapter
    const x0 = a.logicalToX(0)
    const x1 = a.logicalToX(1)
    const y0 = a.priceToY(refs[0])
    const y1 = a.priceToY(refs[1])
    if (x0 === null || x1 === null || y0 === null || y1 === null) return null
    return { x0, x1, y0, y1, width: a.paneWidth(), version }
  }

  private shapeOf(d: Drawing): Shape | null {
    const a0 = d.anchors[0]
    const a1 = d.anchors[1]
    const count = d.anchors.length
    const t0 = a0 ? a0.time : NaN
    const p0 = a0 ? a0.price : NaN
    const t1 = a1 ? a1.time : NaN
    const p1 = a1 ? a1.price : NaN
    const e = this.entries.get(d.id)
    if (
      e !== undefined &&
      e.generation === this.generation &&
      e.kind === d.kind &&
      e.count === count &&
      Object.is(e.t0, t0) &&
      Object.is(e.p0, p0) &&
      Object.is(e.t1, t1) &&
      Object.is(e.p1, p1)
    ) {
      return e.shape
    }
    const shape = projectDrawing(d, this.adapter)
    if (e === undefined) {
      this.entries.set(d.id, {
        kind: d.kind,
        count,
        t0,
        p0,
        t1,
        p1,
        generation: this.generation,
        shape,
      })
    } else {
      e.kind = d.kind
      e.count = count
      e.t0 = t0
      e.p0 = p0
      e.t1 = t1
      e.p1 = p1
      e.generation = this.generation
      e.shape = shape
    }
    return shape
  }

  private prune(drawings: readonly Drawing[]): void {
    const keep = new Set(drawings.map((d) => d.id))
    for (const id of this.entries.keys()) if (!keep.has(id)) this.entries.delete(id)
  }
}
