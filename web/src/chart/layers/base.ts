// Shared plumbing of the chart's series-primitive overlay layers (Phase 19 DRY): the market
// structure, session ranges and volume profile layers draw through the same primitive base,
// renderer, pane view and chart coordinate adapter, and are kept by the same layer host (one
// primitive per indicator instance, attach / detach, keep-set sweep, colour fan-out). The
// drawings primitive shares the render-target types and the bitmap line helper.
//
// Nothing here decides what is drawn: each layer still builds its own geometry and paints it
// with its own draw function, so moving the plumbing here changes no pixel and no cache.

import type {
  IChartApiBase,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  IRange,
  ISeriesApi,
  ISeriesPrimitive,
  ITimeScaleApi,
  Logical,
  PrimitivePaneViewZOrder,
  SeriesAttachedParameter,
  SeriesType,
  Time,
  UTCTimestamp,
} from 'lightweight-charts'
import {
  barSpacingOf,
  sameViewSignature,
  viewSignature,
  type ViewSignature,
} from '../viewSignature'

/* ------------------------------------------------------------------------------------------ */
/* Render-target types and bitmap helpers                                                      */
/* ------------------------------------------------------------------------------------------ */

export type RenderTarget = Parameters<IPrimitivePaneRenderer['draw']>[0]
export type BitmapScope = Parameters<Parameters<RenderTarget['useBitmapCoordinateSpace']>[0]>[0]

/** What `ISeriesPrimitive.attached` receives that the layers use. */
export type AttachedParam = Pick<
  SeriesAttachedParameter<Time>,
  'chart' | 'series' | 'requestUpdate'
>

/** The part of a series a layer uses (an `ISeriesApi` satisfies it). */
export type PrimitiveHost = Pick<ISeriesApi<SeriesType>, 'attachPrimitive' | 'detachPrimitive'>

/** A visible time range in UNIX seconds. */
export type TimeRange = { readonly from: number; readonly to: number }

/** True when the chart's visible range is in UNIX seconds (not null, not business days). */
export function isTimestampRange(r: IRange<Time> | null): r is IRange<UTCTimestamp> {
  return r !== null && typeof r.from === 'number' && typeof r.to === 'number'
}

/** Solid lines. `setLineDash` copies its argument, so one shared empty array is safe. */
export const NO_DASH: number[] = []

/** Keeps far off-screen ends near the pane so the canvas never sees huge coordinates. */
export function clampX(x: number, width: number): number {
  return Math.min(width + 2, Math.max(-2, x))
}

/** Crisp horizontal line from media x0 to x1 at media y, `lineWidth` bitmap px thick. */
export function hLine(
  scope: BitmapScope,
  x0: number,
  x1: number,
  y: number,
  lineWidth: number,
): void {
  const ctx = scope.context
  const offset = lineWidth % 2 ? 0.5 : 0
  const yb = Math.round(y * scope.verticalPixelRatio) + offset
  ctx.beginPath()
  ctx.moveTo(Math.round(x0 * scope.horizontalPixelRatio), yb)
  ctx.lineTo(Math.round(x1 * scope.horizontalPixelRatio), yb)
  ctx.stroke()
}

/* ------------------------------------------------------------------------------------------ */
/* Coordinates                                                                                 */
/* ------------------------------------------------------------------------------------------ */

/**
 * How `xOfTime` finds a bar: 'nearest' snaps to the nearest plotted bar (market structure,
 * volume profile: a level or range edge may sit on a time the chart does not plot), 'exact'
 * needs a plotted bar at that time (session boxes start and end on plotted bars).
 */
export type TimeSnap = 'nearest' | 'exact'

/**
 * The chart conversions a layer's geometry needs, one adapter per primitive (no closures per
 * frame): `updateAllViews` points it at the time scale and series and sets the range and width
 * it already read. `barSpacing` is the view signature's when there is one, else read from the
 * time scale the first time it is asked for.
 */
export class ChartCoords {
  ts: ITimeScaleApi<Time> | null = null
  series: ISeriesApi<SeriesType, Time> | null = null
  range: TimeRange | null = null
  width = 0
  /** The bar spacing already known for this frame; null = read it from `ts` when asked. */
  spacing: number | null = null
  private readonly nearest: boolean

  constructor(snap: TimeSnap) {
    this.nearest = snap === 'nearest'
  }

  visibleRange(): TimeRange | null {
    return this.range
  }

  /** Distance between neighbouring bar centres (CSS px); 0 without a time scale. */
  get barSpacing(): number {
    if (this.spacing === null) this.spacing = this.ts === null ? 0 : barSpacingOf(this.ts)
    return this.spacing
  }

  /** x of the bar at `time` (see `TimeSnap`); also places bars outside the view. */
  xOfTime(time: number): number | null {
    const ts = this.ts
    if (ts === null) return null
    if (!this.nearest) return ts.timeToCoordinate(time as Time)
    // Nearest plotted bar; logicalToCoordinate also places off-screen bars.
    const index = ts.timeToIndex(time as Time, true)
    return index === null ? null : ts.logicalToCoordinate(index as unknown as Logical)
  }

  /** y of `price` on the series' scale (an arrow, so it can be passed as a function). */
  readonly yOfPrice = (price: number): number | null =>
    this.series === null ? null : this.series.priceToCoordinate(price)
}

/* ------------------------------------------------------------------------------------------ */
/* Renderer, pane view, primitive                                                              */
/* ------------------------------------------------------------------------------------------ */

/** Paints one geometry in bitmap space (a layer's exported draw function). */
export type BitmapPaint<G, C> = (scope: BitmapScope, geom: G, colors: C) => void

/** Draws the primitive's current geometry with its colours; nothing while there is none. */
export class LayerRenderer<G, C> implements IPrimitivePaneRenderer {
  geom: G | null = null
  colors: C
  private readonly paint: BitmapPaint<G, C>
  /** Geometry that draws nothing (skips the bitmap scope altogether), or null. */
  private readonly isEmpty: ((geom: G) => boolean) | null

  constructor(colors: C, paint: BitmapPaint<G, C>, isEmpty: ((geom: G) => boolean) | null = null) {
    this.colors = colors
    this.paint = paint
    this.isEmpty = isEmpty
  }

  draw(target: RenderTarget): void {
    const geom = this.geom
    if (geom === null || (this.isEmpty !== null && this.isEmpty(geom))) return
    target.useBitmapCoordinateSpace((scope) => this.paint(scope, geom, this.colors))
  }
}

export class LayerPaneView<G, C> implements IPrimitivePaneView {
  readonly renderer_: LayerRenderer<G, C>
  private readonly z: PrimitivePaneViewZOrder

  constructor(renderer: LayerRenderer<G, C>, zOrder: PrimitivePaneViewZOrder) {
    this.renderer_ = renderer
    this.z = zOrder
  }

  zOrder(): PrimitivePaneViewZOrder {
    return this.z
  }

  renderer(): IPrimitivePaneRenderer {
    return this.renderer_
  }
}

/** How a layer primitive draws: colours, paint function, empty test, z-order, x lookup. */
export interface LayerPrimitiveOptions<G, C> {
  colors: C
  paint: BitmapPaint<G, C>
  isEmpty?: (geom: G) => boolean
  zOrder: PrimitivePaneViewZOrder
  snap: TimeSnap
}

/**
 * One indicator instance's series primitive: its uid, visibility, colours, attachment and one
 * pane view. `version` is bumped whenever what the geometry is built from changes (data,
 * visibility, colours, attach / detach) and is part of the geometry cache's signature.
 */
export abstract class LayerPrimitive<G, C> implements ISeriesPrimitive<Time> {
  readonly uid: string
  protected visible = true
  protected chart: IChartApiBase<Time> | null = null
  protected series: ISeriesApi<SeriesType, Time> | null = null
  private requestUpdate: (() => void) | null = null
  protected readonly renderer_: LayerRenderer<G, C>
  private readonly paneViewList: readonly IPrimitivePaneView[]
  protected readonly coords: ChartCoords
  protected version = 0
  /** Geometry builds so far (tests: an unchanged view must not rebuild). */
  protected builds = 0

  constructor(uid: string, options: LayerPrimitiveOptions<G, C>) {
    this.uid = uid
    this.renderer_ = new LayerRenderer(options.colors, options.paint, options.isEmpty ?? null)
    this.paneViewList = [new LayerPaneView(this.renderer_, options.zOrder)]
    this.coords = new ChartCoords(options.snap)
  }

  attached(param: AttachedParam): void {
    this.chart = param.chart
    this.series = param.series
    this.requestUpdate = param.requestUpdate
    this.invalidate()
  }

  detached(): void {
    this.chart = null
    this.series = null
    this.requestUpdate = null
    this.renderer_.geom = null
    this.coords.ts = null
    this.coords.series = null
    this.invalidate()
  }

  isVisible(): boolean {
    return this.visible
  }

  setVisible(visible: boolean): void {
    if (visible === this.visible) return
    this.visible = visible
    this.changed()
  }

  setColors(colors: C): void {
    this.renderer_.colors = colors
    this.changed()
  }

  colors(): C {
    return this.renderer_.colors
  }

  /** The geometry drawn on the last frame (null = nothing drawn). */
  geometry(): G | null {
    return this.renderer_.geom
  }

  /** Geometry builds so far (tests, debugging). */
  buildCount(): number {
    return this.builds
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return this.paneViewList
  }

  abstract updateAllViews(): void

  /** What the geometry is built from changed: the cached geometry no longer applies. */
  protected invalidate(): void {
    this.version += 1
  }

  /** `invalidate`, then ask the chart for a redraw (when attached). */
  protected changed(): void {
    this.invalidate()
    this.requestUpdate?.()
  }
}

/**
 * Reference prices of the culled layers' signature: positive (valid on a log scale) and
 * distinct; every price scale mode maps prices through a function fixed by two points.
 */
const REF_PRICES: readonly [number, number] = [1, 2]

/**
 * A layer primitive whose geometry is the part of a view inside the visible time range
 * (market structure, session ranges). `updateAllViews` rebuilds it only when the view moved:
 * a drawing drag, a live tick or a hover change asks every primitive to update (a
 * crosshair-only move repaints without calling it), and an unchanged signature (scroll
 * position, bar spacing, width, two price coordinates, `version`) plus an unchanged visible
 * time range means every coordinate the geometry reads is unchanged too.
 */
export abstract class CulledLayerPrimitive<V, G, C> extends LayerPrimitive<G, C> {
  private viewData: V | null = null
  /** The view signature and time range the current geometry was built for (null = none). */
  private cachedSig: ViewSignature | null = null
  private cachedRange: TimeRange | null = null

  /** The geometry of `view` for the frame `coords` describes (null: nothing to draw). */
  protected abstract build(view: V, coords: ChartCoords): G | null

  /** What the primitive draws (the same object until its data or toggles change). */
  view(): V | null {
    return this.viewData
  }

  setView(view: V | null): void {
    if (view === this.viewData) return
    this.viewData = view
    this.changed()
  }

  updateAllViews(): void {
    const view = this.viewData
    const chart = this.chart
    const series = this.series
    const renderer = this.renderer_
    if (!this.visible || view === null || chart === null || series === null) {
      renderer.geom = null
      this.cachedSig = null
      return
    }
    const ts = chart.timeScale()
    const r = ts.getVisibleRange()
    // LWC returns a fresh range object per call; it is kept as is (read-only here).
    if (!isTimestampRange(r)) {
      // No bar in view: nothing to draw, nothing to build.
      renderer.geom = null
      this.cachedSig = null
      return
    }
    const range: TimeRange = r
    const sig = viewSignature(chart, series, REF_PRICES, this.version)
    const prev = this.cachedRange
    if (
      prev !== null &&
      range.from === prev.from &&
      range.to === prev.to &&
      sameViewSignature(sig, this.cachedSig)
    )
      return
    const coords = this.coords
    coords.ts = ts
    coords.series = series
    coords.range = range
    coords.spacing = sig === null ? null : sig.barSpacing
    coords.width = sig === null ? ts.width() : sig.width
    renderer.geom = this.build(view, coords)
    this.builds += 1
    this.cachedSig = sig
    this.cachedRange = range
  }
}

/* ------------------------------------------------------------------------------------------ */
/* Layer host                                                                                  */
/* ------------------------------------------------------------------------------------------ */

/** What the host needs from a primitive (every `LayerPrimitive` satisfies it). */
export interface HostedPrimitive<C> extends ISeriesPrimitive<Time> {
  setColors(colors: C): void
  detached(): void
}

/**
 * The instances of one overlay layer on the main series: one entry (and primitive) per
 * indicator uid, in creation order. Usage from Chart: `attach(series)` when the main series is
 * created and `detach(alive)` before it is removed (chart-type switch), `setColors` on theme
 * change; the layer's own `sync` adds entries with `add` and drops the rest with `sweep`.
 */
export abstract class LayerHost<
  C extends object,
  P extends HostedPrimitive<C>,
  E extends { primitive: P },
> {
  private series: PrimitiveHost | null = null
  protected readonly entries = new Map<string, E>()
  protected colors: C
  /** The colour fields compared by `setColors` (a change of any of them is a new theme). */
  private readonly colorKeys: readonly (keyof C)[]

  constructor(colors: C, colorKeys: readonly (keyof C)[]) {
    this.colors = colors
    this.colorKeys = colorKeys
  }

  /** Attach every primitive to `series` (moving them off a previous one). */
  attach(series: PrimitiveHost): void {
    if (this.series === series) return
    if (this.series !== null) this.detach()
    this.series = series
    for (const e of this.entries.values()) series.attachPrimitive(e.primitive)
  }

  /**
   * Take the primitives off the series. `alive` false = the series (or the chart) has already
   * been removed, so only the references are dropped. Results stay for the next attach.
   */
  detach(alive = true): void {
    const series = this.series
    this.series = null
    if (series === null) return
    for (const e of this.entries.values()) {
      if (alive) series.detachPrimitive(e.primitive)
      else e.primitive.detached()
    }
  }

  /** New colours for every primitive (nothing happens when every compared field is equal). */
  setColors(colors: C): void {
    this.applyColors(colors)
  }

  /** Uids with a primitive, in creation order (tests, debugging). */
  uids(): string[] {
    return [...this.entries.keys()]
  }

  /** The primitive of `uid` (tests, debugging). */
  primitiveOf(uid: string): P | undefined {
    return this.entries.get(uid)?.primitive
  }

  /** Keep `entry` for `uid` and attach its primitive to the current series. */
  protected add(uid: string, entry: E): E {
    this.entries.set(uid, entry)
    this.series?.attachPrimitive(entry.primitive)
    return entry
  }

  /** Drop (and detach) every entry whose uid is not in `keep`. */
  protected sweep(keep: ReadonlySet<string>): void {
    for (const [uid, entry] of this.entries) {
      if (keep.has(uid)) continue
      this.series?.detachPrimitive(entry.primitive)
      this.entries.delete(uid)
    }
  }

  /** Store a copy of `colors` and hand it to every primitive; false when nothing changed. */
  protected applyColors(colors: C): boolean {
    const c = this.colors
    if (this.colorKeys.every((k) => c[k] === colors[k])) return false
    this.colors = { ...colors }
    for (const e of this.entries.values()) e.primitive.setColors(this.colors)
    return true
  }
}
