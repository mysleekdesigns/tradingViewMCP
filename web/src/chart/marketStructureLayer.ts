// Market structure overlay (Phase 16) on the main series: visual only, no edge claim.
//
// Per instance of an event indicator (a def with `structure`, i.e. 'mstructure'):
// - a `MarketStructureEngine` fed CLOSED bars only, one at a time: plotted bars at or before
//   `closedThrough` (final values confirmed, see `LiveCandlesSnapshot.closedThrough`), so the
//   forming bar, a partial bar awaiting a gap fill, a provisional close and any bar past a
//   possible hole of missing bars are never pushed or read. Kept incrementally: a new closed
//   bar costs one push, a tick on the forming bar pushes nothing (one O(n) identity scan of
//   the bars), and any pushed bar that is no longer the same object (revised in place, older
//   history, another window) or a `closedThrough` that moved back starts over;
// - a series primitive (zOrder 'bottom': under the candles and under the drawings, which draw at
//   'top') with swing levels and flip zones, culled to the visible time range by binary search;
// - BOS / ChoCh markers on the breaking bar and retest markers on the first retest bar, merged
//   across every visible instance into one channel of the shared `SeriesMarkersMux`, so the
//   backtest's trade markers stay.
//
// Nothing is back-dated: a swing level starts at its confirmation bar (`right` bars after the
// pivot), the pivot dot exists only once the swing is confirmed (it is in the result only from
// then), and every marker sits on a closed bar at or after the bar that produced it.

import type {
  IChartApiBase,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesApi,
  ISeriesPrimitive,
  Logical,
  SeriesAttachedParameter,
  SeriesType,
  Time,
  UTCTimestamp,
} from 'lightweight-charts'
import {
  MarketStructureEngine,
  structureParams,
  structureParamsKey,
} from '../indicators/marketStructure'
import { INDICATOR_BY_ID } from '../indicators/registry'
import type {
  IndicatorDef,
  IndicatorId,
  IndicatorInputs,
  MarketStructure,
} from '../indicators/types'
import type { IndicatorConfig } from '../state/indicators'
import type { Candle } from '../types'
import type { MarkersHandle } from './backtestOverlay'
import type { ChartMarker } from './seriesMarkers'
import { closedBarCount } from './structureBars'
import type { ChartPalette } from './theme'

type RenderTarget = Parameters<IPrimitivePaneRenderer['draw']>[0]
type BitmapScope = Parameters<Parameters<RenderTarget['useBitmapCoordinateSpace']>[0]>[0]

/* ------------------------------------------------------------------------------------------ */
/* Colours and toggles                                                                         */
/* ------------------------------------------------------------------------------------------ */

export interface MarketStructureColors {
  /** Bullish: support zones, levels broken upward, up markers. */
  up: string
  /** Bearish: resistance zones, levels broken downward, down markers. */
  down: string
  /** Unbroken levels and pivot dots. */
  neutral: string
  /** Light background: fills a little fainter, lines a little stronger. */
  light: boolean
}

/** Relative luminance of '#rrggbb' (0 black .. 1 white); other strings count as dark. */
function luminance(hex: string): number {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex)
  if (m === null) return 0
  const [r, g, b] = [m[1], m[2], m[3]].map((h) => parseInt(h, 16) / 255)
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

/** Candle colours for direction, the muted text colour for neutral levels. */
export function marketStructureColorsFor(
  palette: Pick<ChartPalette, 'up' | 'down' | 'mutedText' | 'background'>,
): MarketStructureColors {
  return {
    up: palette.up,
    down: palette.down,
    neutral: palette.mutedText,
    light: luminance(palette.background) > 0.5,
  }
}

export const DEFAULT_MARKET_STRUCTURE_COLORS: Readonly<MarketStructureColors> = Object.freeze(
  marketStructureColorsFor({
    up: '#089981',
    down: '#f23645',
    mutedText: '#787b86',
    background: '#131722',
  }),
)

/** Display toggles (they never change the computation). */
export interface StructureShow {
  swings: boolean
  bos: boolean
  choch: boolean
  zones: boolean
}

/** Toggle inputs are 1 (on) or 0 (off); a missing key counts as on (the default). */
export function structureShow(inputs: IndicatorInputs): StructureShow {
  return {
    swings: (inputs.showSwings ?? 1) === 1,
    bos: (inputs.showBos ?? 1) === 1,
    choch: (inputs.showChoch ?? 1) === 1,
    zones: (inputs.showZones ?? 1) === 1,
  }
}

function sameShow(a: StructureShow, b: StructureShow): boolean {
  return a.swings === b.swings && a.bos === b.bos && a.choch === b.choch && a.zones === b.zones
}

/* ------------------------------------------------------------------------------------------ */
/* Markers (pure)                                                                              */
/* ------------------------------------------------------------------------------------------ */

/** Retest dots are smaller than the BOS / ChoCh arrows (LWC size multiplier, default 1). */
export const RETEST_MARKER_SIZE = 0.6

/**
 * BOS / ChoCh markers at each event's breaking bar (bullish: arrowUp below the bar, bearish:
 * arrowDown above it) and, with the zones toggle, a small circle 'R' at each zone's first retest
 * (support below the bar, resistance above). Sorted by time; at equal times events come first.
 */
export function structureMarkers(
  result: MarketStructure,
  show: StructureShow,
  colors: Pick<MarketStructureColors, 'up' | 'down'>,
): ChartMarker[] {
  const events: ChartMarker[] = []
  if (show.bos || show.choch) {
    for (const e of result.events) {
      if (e.kind === 'bos' ? !show.bos : !show.choch) continue
      const up = e.direction === 'up'
      events.push({
        time: e.time as UTCTimestamp,
        position: up ? 'belowBar' : 'aboveBar',
        shape: up ? 'arrowUp' : 'arrowDown',
        color: up ? colors.up : colors.down,
        text: e.kind === 'bos' ? 'BOS' : 'ChoCh',
      })
    }
  }
  if (!show.zones) return events
  const retests: ChartMarker[] = []
  for (const z of result.zones) {
    if (z.retestTime === null) continue
    const support = z.side === 'support'
    retests.push({
      time: z.retestTime as UTCTimestamp,
      position: support ? 'belowBar' : 'aboveBar',
      shape: 'circle',
      color: support ? colors.up : colors.down,
      text: 'R',
      size: RETEST_MARKER_SIZE,
    })
  }
  if (retests.length === 0) return events
  // Zones are by creation time, retests are not: sort them (stable), then merge.
  retests.sort((a, b) => a.time - b.time)
  return mergeByTime([events, retests])
}

/** Stable merge of time-sorted lists: equal times keep list order. */
export function mergeByTime(lists: readonly (readonly ChartMarker[])[]): ChartMarker[] {
  const nonEmpty = lists.filter((l) => l.length > 0)
  if (nonEmpty.length === 0) return []
  if (nonEmpty.length === 1) return nonEmpty[0].slice()
  const out: ChartMarker[] = []
  const at = nonEmpty.map(() => 0)
  for (;;) {
    let best = -1
    for (let k = 0; k < nonEmpty.length; k++) {
      const i = at[k]
      if (i >= nonEmpty[k].length) continue
      if (best === -1 || nonEmpty[k][i].time < nonEmpty[best][at[best]].time) best = k
    }
    if (best === -1) return out
    out.push(nonEmpty[best][at[best]])
    at[best] += 1
  }
}

function sameMarkers(a: readonly ChartMarker[], b: readonly ChartMarker[]): boolean {
  if (a === b) return true
  if (a.length !== b.length) return false
  for (let i = 0; i < a.length; i++) {
    const x = a[i]
    const y = b[i]
    if (
      x.time !== y.time ||
      x.position !== y.position ||
      x.shape !== y.shape ||
      x.color !== y.color ||
      x.text !== y.text ||
      x.size !== y.size
    )
      return false
  }
  return true
}

/* ------------------------------------------------------------------------------------------ */
/* Time-interval index (pure)                                                                  */
/* ------------------------------------------------------------------------------------------ */

/** Intervals per block of the skip index. */
const BLOCK = 32

/**
 * Time intervals [start, end] appended with non-decreasing starts; `end` may be Infinity (still
 * running) and may later be lowered. `query(from, to)` visits every interval that intersects
 * [from, to] by a binary search on the starts and a backward scan that skips whole blocks whose
 * latest end is before `from`, so a long history costs O(log n + blocks + hits), not O(n).
 */
export class IntervalIndex {
  private readonly starts: number[] = []
  private readonly ends: number[] = []
  private readonly blockMax: number[] = []

  get length(): number {
    return this.starts.length
  }

  push(start: number, end: number): number {
    const i = this.starts.length
    this.starts.push(start)
    this.ends.push(end)
    const b = Math.floor(i / BLOCK)
    if (b === this.blockMax.length) this.blockMax.push(end)
    else if (end > this.blockMax[b]) this.blockMax[b] = end
    return i
  }

  end(i: number): number {
    return this.ends[i]
  }

  setEnd(i: number, end: number): void {
    if (this.ends[i] === end) return
    this.ends[i] = end
    const b = Math.floor(i / BLOCK)
    let max = Number.NEGATIVE_INFINITY
    const stop = Math.min(this.ends.length, (b + 1) * BLOCK)
    for (let k = b * BLOCK; k < stop; k++) if (this.ends[k] > max) max = this.ends[k]
    this.blockMax[b] = max
  }

  /** Indices of the intervals intersecting [from, to], ascending. */
  query(from: number, to: number): number[] {
    const starts = this.starts
    // First index with start > to.
    let lo = 0
    let hi = starts.length
    while (lo < hi) {
      const mid = (lo + hi) >>> 1
      if (starts[mid] <= to) lo = mid + 1
      else hi = mid
    }
    const out: number[] = []
    let i = lo - 1
    while (i >= 0) {
      const b = Math.floor(i / BLOCK)
      if (this.blockMax[b] < from) {
        i = b * BLOCK - 1
        continue
      }
      if (this.ends[i] >= from) out.push(i)
      i -= 1
    }
    out.reverse()
    return out
  }
}

/** First index in [0, n) whose key is >= x (keys ascending). */
function lowerBound(n: number, key: (i: number) => number, x: number): number {
  let lo = 0
  let hi = n
  while (lo < hi) {
    const mid = (lo + hi) >>> 1
    if (key(mid) < x) lo = mid + 1
    else hi = mid
  }
  return lo
}

/**
 * Drawing spans derived from the live result, kept in step with it incrementally:
 * - swing i spans [confirmTime, end]: `brokenTime` when broken, else the confirmation of the
 *   next swing of the same kind (it can no longer break once superseded), else Infinity;
 * - zone i spans [createdTime, endTime ?? Infinity].
 */
export class StructureSpans {
  readonly swings = new IntervalIndex()
  readonly zones = new IntervalIndex()
  /** Latest swing of each kind (the only ones that can still change), or -1. */
  private lastHigh = -1
  private lastLow = -1
  /** Zones still active (end Infinity). */
  private openZones: number[] = []

  /** Catch up with `result` (appended arrays, in-place `brokenTime` / `endTime`). */
  update(result: MarketStructure): void {
    const swings = result.swings
    for (const i of [this.lastHigh, this.lastLow]) {
      if (i < 0) continue
      const b = swings[i].brokenTime
      if (b !== null && this.swings.end(i) === Number.POSITIVE_INFINITY) this.swings.setEnd(i, b)
    }
    for (let i = this.swings.length; i < swings.length; i++) {
      const s = swings[i]
      const prev = s.kind === 'high' ? this.lastHigh : this.lastLow
      if (prev >= 0 && this.swings.end(prev) === Number.POSITIVE_INFINITY) {
        this.swings.setEnd(prev, swings[prev].brokenTime ?? s.confirmTime)
      }
      this.swings.push(s.confirmTime, s.brokenTime ?? Number.POSITIVE_INFINITY)
      if (s.kind === 'high') this.lastHigh = i
      else this.lastLow = i
    }
    const zones = result.zones
    for (let i = this.zones.length; i < zones.length; i++) {
      this.zones.push(zones[i].createdTime, Number.POSITIVE_INFINITY)
      this.openZones.push(i)
    }
    if (this.openZones.length > 0) {
      const still: number[] = []
      for (const i of this.openZones) {
        const end = zones[i].endTime
        if (end === null) still.push(i)
        else this.zones.setEnd(i, end)
      }
      this.openZones = still
    }
  }
}

/* ------------------------------------------------------------------------------------------ */
/* Geometry (pure, media / CSS pixels)                                                         */
/* ------------------------------------------------------------------------------------------ */

/** What one instance's primitive draws: the live result, its spans and the toggles. */
export interface StructureView {
  result: MarketStructure
  spans: StructureSpans
  show: StructureShow
  /** Bumped whenever the result changed (a closed bar was pushed, or a recompute). */
  version: number
}

/** Chart conversions the geometry needs; LWC or a test fake provides them. */
export interface StructureCoords {
  /** Open times of the first and last plotted bar in view, or null without data. */
  visibleRange(): { from: number; to: number } | null
  /** x of the bar at (or nearest to) `time`; may lie outside the pane. */
  xOfTime(time: number): number | null
  /** y of `price` on the main series' scale. */
  yOfPrice(price: number): number | null
  /** Pane width (CSS px): where running levels and zones end. */
  width: number
}

export type SwingLineState = 'active' | 'broken' | 'superseded'

export interface SwingLineGeometry {
  x0: number
  x1: number
  y: number
  state: SwingLineState
  /** Direction of the break for a broken level; null otherwise. */
  broke: 'up' | 'down' | null
}

export interface PivotDotGeometry {
  x: number
  y: number
  kind: 'high' | 'low'
}

export interface ZoneGeometry {
  x0: number
  x1: number
  /** y of the top price (smaller y) and of the bottom price. */
  top: number
  bottom: number
  side: 'support' | 'resistance'
  /** x of the first retest bar, or null (not retested, or retest out of reach). */
  retestX: number | null
  active: boolean
}

export interface StructureGeometry {
  zones: ZoneGeometry[]
  lines: SwingLineGeometry[]
  dots: PivotDotGeometry[]
}

/** Keeps far off-screen ends near the pane so the canvas never sees huge coordinates. */
function clampX(x: number, width: number): number {
  return Math.min(width + 2, Math.max(-2, x))
}

/** The parts of `view` that intersect the visible time range, in pane coordinates. */
export function structureGeometry(
  view: StructureView,
  coords: StructureCoords,
): StructureGeometry | null {
  const range = coords.visibleRange()
  if (range === null) return null
  const { from, to } = range
  const width = coords.width
  const { result, spans, show } = view
  const out: StructureGeometry = { zones: [], lines: [], dots: [] }
  const endX = (end: number): number | null =>
    end === Number.POSITIVE_INFINITY ? width + 2 : coords.xOfTime(end)

  if (show.zones) {
    for (const i of spans.zones.query(from, to)) {
      const z = result.zones[i]
      const x0 = coords.xOfTime(z.createdTime)
      const x1 = endX(spans.zones.end(i))
      const yTop = coords.yOfPrice(z.top)
      const yBottom = coords.yOfPrice(z.bottom)
      if (x0 === null || x1 === null || yTop === null || yBottom === null) continue
      const rx = z.retestTime === null ? null : coords.xOfTime(z.retestTime)
      out.zones.push({
        x0: clampX(x0, width),
        x1: clampX(x1, width),
        top: Math.min(yTop, yBottom),
        bottom: Math.max(yTop, yBottom),
        side: z.side,
        retestX: rx === null ? null : clampX(rx, width),
        active: z.endTime === null,
      })
    }
  }

  if (show.swings) {
    for (const i of spans.swings.query(from, to)) {
      const s = result.swings[i]
      const x0 = coords.xOfTime(s.confirmTime)
      const x1 = endX(spans.swings.end(i))
      const y = coords.yOfPrice(s.price)
      if (x0 === null || x1 === null || y === null) continue
      const broken = s.brokenTime !== null
      out.lines.push({
        x0: clampX(x0, width),
        x1: clampX(x1, width),
        y,
        state: broken
          ? 'broken'
          : spans.swings.end(i) === Number.POSITIVE_INFINITY
            ? 'active'
            : 'superseded',
        broke: broken ? (s.kind === 'high' ? 'up' : 'down') : null,
      })
    }
    // Pivot dots of confirmed swings whose pivot bar is in view (pivot times ascend with
    // confirmation times, so a binary search finds them).
    const swings = result.swings
    const first = lowerBound(swings.length, (i) => swings[i].pivotTime, from)
    for (let i = first; i < swings.length && swings[i].pivotTime <= to; i++) {
      const s = swings[i]
      const x = coords.xOfTime(s.pivotTime)
      const y = coords.yOfPrice(s.price)
      if (x === null || y === null) continue
      out.dots.push({ x, y, kind: s.kind })
    }
  }
  return out
}

/* ------------------------------------------------------------------------------------------ */
/* Renderer + primitive                                                                        */
/* ------------------------------------------------------------------------------------------ */

/** Fill alpha of a zone before / from its first retest, and of its edges (dark theme). */
export const ZONE_FILL_ALPHA = 0.1
export const ZONE_RETESTED_FILL_ALPHA = 0.2
const ZONE_EDGE_ALPHA = 0.55
/** Light theme fills read stronger on white: scale them down. */
const LIGHT_FILL_SCALE = 0.8
const LINE_ALPHA: Record<SwingLineState, number> = { active: 0.9, broken: 0.75, superseded: 0.35 }
const DOT_RADIUS_PX = 2.25

function setDash(ctx: CanvasRenderingContext2D, state: SwingLineState, ratio: number): void {
  if (state === 'active') ctx.setLineDash([Math.round(4 * ratio), Math.round(3 * ratio)])
  else if (state === 'superseded') ctx.setLineDash([Math.round(1 * ratio), Math.round(3 * ratio)])
  else ctx.setLineDash([])
}

/** Draws one instance in bitmap space; exported for tests with a fake canvas. */
export function drawStructure(
  scope: BitmapScope,
  geom: StructureGeometry,
  colors: MarketStructureColors,
): void {
  const ctx = scope.context
  const hr = scope.horizontalPixelRatio
  const vr = scope.verticalPixelRatio
  const fillScale = colors.light ? LIGHT_FILL_SCALE : 1
  const lineWidth = Math.max(1, Math.floor(vr))
  const half = lineWidth % 2 ? 0.5 : 0
  ctx.save()

  for (const z of geom.zones) {
    const color = z.side === 'support' ? colors.up : colors.down
    const left = Math.round(z.x0 * hr)
    const right = Math.max(left + 1, Math.round(z.x1 * hr))
    const top = Math.round(z.top * vr)
    const bottom = Math.max(top + 1, Math.round(z.bottom * vr))
    ctx.fillStyle = color
    const split =
      z.retestX === null ? right : Math.min(right, Math.max(left, Math.round(z.retestX * hr)))
    if (split > left) {
      ctx.globalAlpha = ZONE_FILL_ALPHA * fillScale
      ctx.fillRect(left, top, split - left, bottom - top)
    }
    if (right > split) {
      ctx.globalAlpha = ZONE_RETESTED_FILL_ALPHA * fillScale
      ctx.fillRect(split, top, right - split, bottom - top)
    }
    // Top and bottom edges; a running zone's edges are dashed toward the right edge.
    ctx.globalAlpha = ZONE_EDGE_ALPHA
    ctx.strokeStyle = color
    ctx.lineWidth = lineWidth
    ctx.setLineDash(z.active ? [Math.round(4 * hr), Math.round(3 * hr)] : [])
    ctx.beginPath()
    ctx.moveTo(left, top + half)
    ctx.lineTo(right, top + half)
    ctx.moveTo(left, bottom - lineWidth + half)
    ctx.lineTo(right, bottom - lineWidth + half)
    ctx.stroke()
  }

  ctx.lineWidth = lineWidth
  for (const l of geom.lines) {
    const color = l.broke === 'up' ? colors.up : l.broke === 'down' ? colors.down : colors.neutral
    ctx.globalAlpha = LINE_ALPHA[l.state]
    ctx.strokeStyle = color
    setDash(ctx, l.state, hr)
    const y = Math.round(l.y * vr) + half
    ctx.beginPath()
    ctx.moveTo(Math.round(l.x0 * hr), y)
    ctx.lineTo(Math.max(Math.round(l.x0 * hr) + 1, Math.round(l.x1 * hr)), y)
    ctx.stroke()
  }

  ctx.setLineDash([])
  ctx.globalAlpha = 0.9
  ctx.fillStyle = colors.neutral
  const r = DOT_RADIUS_PX * Math.max(hr, vr)
  for (const d of geom.dots) {
    ctx.beginPath()
    ctx.arc(d.x * hr, d.y * vr, r, 0, 2 * Math.PI)
    ctx.fill()
  }
  ctx.restore()
}

class StructureRenderer implements IPrimitivePaneRenderer {
  geom: StructureGeometry | null = null
  colors: MarketStructureColors = DEFAULT_MARKET_STRUCTURE_COLORS

  draw(target: RenderTarget): void {
    const geom = this.geom
    if (geom === null) return
    target.useBitmapCoordinateSpace((scope) => drawStructure(scope, geom, this.colors))
  }
}

class StructurePaneView implements IPrimitivePaneView {
  readonly renderer_ = new StructureRenderer()

  /** Under the candles and under the drawings (which use 'top'). */
  zOrder(): 'bottom' {
    return 'bottom'
  }

  renderer(): IPrimitivePaneRenderer {
    return this.renderer_
  }
}

type AttachedParam = Pick<SeriesAttachedParameter<Time>, 'chart' | 'series' | 'requestUpdate'>

/**
 * One instance's swing levels and flip zones on the main series. No `autoscaleInfo`, so old
 * zones and levels never stretch the price scale. No hit test: it never takes clicks from the
 * candles or the drawings.
 */
export class MarketStructurePrimitive implements ISeriesPrimitive<Time> {
  readonly uid: string
  private viewData: StructureView | null = null
  private visible = true
  private chart: IChartApiBase<Time> | null = null
  private series: ISeriesApi<SeriesType, Time> | null = null
  private requestUpdate: (() => void) | null = null
  private readonly paneView = new StructurePaneView()
  private readonly paneViewList: readonly IPrimitivePaneView[] = [this.paneView]

  constructor(uid: string, colors: MarketStructureColors) {
    this.uid = uid
    this.paneView.renderer_.colors = colors
  }

  attached(param: AttachedParam): void {
    this.chart = param.chart
    this.series = param.series
    this.requestUpdate = param.requestUpdate
  }

  detached(): void {
    this.chart = null
    this.series = null
    this.requestUpdate = null
    this.paneView.renderer_.geom = null
  }

  /** What the primitive draws (the same object until the result or the toggles change). */
  view(): StructureView | null {
    return this.viewData
  }

  isVisible(): boolean {
    return this.visible
  }

  setView(view: StructureView | null): void {
    if (view === this.viewData) return
    this.viewData = view
    this.requestUpdate?.()
  }

  setVisible(visible: boolean): void {
    if (visible === this.visible) return
    this.visible = visible
    this.requestUpdate?.()
  }

  setColors(colors: MarketStructureColors): void {
    this.paneView.renderer_.colors = colors
    this.requestUpdate?.()
  }

  /** The geometry drawn on the last frame (null = nothing drawn). */
  geometry(): StructureGeometry | null {
    return this.paneView.renderer_.geom
  }

  private coords(): StructureCoords | null {
    const chart = this.chart
    const series = this.series
    if (chart === null || series === null) return null
    const ts = chart.timeScale()
    return {
      visibleRange: () => {
        const r = ts.getVisibleRange()
        if (r === null || typeof r.from !== 'number' || typeof r.to !== 'number') return null
        return { from: r.from, to: r.to }
      },
      // Nearest plotted bar; logicalToCoordinate also places off-screen bars.
      xOfTime: (time) => {
        const index = ts.timeToIndex(time as Time, true)
        return index === null ? null : ts.logicalToCoordinate(index as unknown as Logical)
      },
      yOfPrice: (price) => series.priceToCoordinate(price),
      width: ts.width(),
    }
  }

  updateAllViews(): void {
    const view = this.viewData
    const coords = this.visible && view !== null ? this.coords() : null
    this.paneView.renderer_.geom =
      coords !== null && view !== null ? structureGeometry(view, coords) : null
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return this.paneViewList
  }
}

/* ------------------------------------------------------------------------------------------ */
/* Layer                                                                                       */
/* ------------------------------------------------------------------------------------------ */

/** The part of a series the layer uses (an `ISeriesApi` satisfies it). */
export type PrimitiveHost = Pick<ISeriesApi<SeriesType>, 'attachPrimitive' | 'detachPrimitive'>

interface Entry {
  primitive: MarketStructurePrimitive
  engine: MarketStructureEngine | null
  paramsKey: string
  /**
   * The bars array the engine was last brought up to date with: its first `engine.count`
   * elements are exactly the bars pushed (candles are never mutated, so the same object means
   * the same values).
   */
  synced: readonly Candle[] | null
  spans: StructureSpans
  version: number
  show: StructureShow
  view: StructureView | null
  /** This instance's markers and what they were built from. */
  markers: readonly ChartMarker[]
  markersFrom: { version: number; show: StructureShow; colors: MarketStructureColors } | null
  recomputes: number
  pushed: number
}

/** Length of the common prefix of `a` and `b` by element identity. */
function samePrefixLength(a: readonly Candle[], b: readonly Candle[]): number {
  if (a === b) return a.length
  const n = Math.min(a.length, b.length)
  let i = 0
  while (i < n && a[i] === b[i]) i++
  return i
}

const NO_MARKERS: readonly ChartMarker[] = Object.freeze([])

/**
 * Every market-structure instance drawn on the main series. Usage from Chart: `attach(series)`
 * when the main series is created and `detach(alive)` before it is removed (chart-type switch),
 * `sync(configs, appliedCandles, closedThrough)` when the configs, candles or closedThrough
 * change, `setColors` on theme change. `markers` is the 'structure' channel of the shared
 * markers mux.
 */
export class MarketStructureLayer {
  private series: PrimitiveHost | null = null
  private readonly entries = new Map<string, Entry>()
  private colors: MarketStructureColors
  private readonly markersOut: MarkersHandle | null
  private merged: readonly ChartMarker[] = NO_MARKERS
  /** Uids that contributed to `merged`, in order, and their marker lists (by identity). */
  private mergedFrom: readonly string[] = []
  private mergedLists: readonly (readonly ChartMarker[])[] = []
  /** Merges of several instances' lists so far (tests). */
  private merges = 0
  /** Configs of the last sync (setColors re-publishes with them). */
  private lastConfigs: readonly IndicatorConfig[] = []

  constructor(
    colors: MarketStructureColors = DEFAULT_MARKET_STRUCTURE_COLORS,
    markers: MarkersHandle | null = null,
  ) {
    this.colors = colors
    this.markersOut = markers
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

  /**
   * `candles` are the plotted (real, not Heikin-Ashi) bars. Only those opening at or before
   * `closedThrough` (final values confirmed; null = none) are pushed or read: the forming bar
   * and a partial bar awaiting a gap fill never are. Only bars not yet pushed are processed; a
   * params change, or any pushed bar that is no longer the same object (older history, a bar
   * revised in place anywhere in the pushed history, another window, a replay step back),
   * starts over.
   */
  sync(
    configs: readonly IndicatorConfig[],
    candles: readonly Candle[] | null,
    closedThrough: number | null,
    defs: ReadonlyMap<IndicatorId, IndicatorDef> = INDICATOR_BY_ID,
  ): void {
    const bars = candles ?? []
    const closedLen = closedBarCount(bars, closedThrough)
    // Identity scan per distinct previous array (O(n) pointer compares, once per sync).
    const prefixOf = new Map<readonly Candle[], number>()
    const prefixMatches = (entry: Entry): boolean => {
      const n = entry.engine?.count ?? 0
      if (n === 0) return true
      if (closedLen < n || entry.synced === null) return false
      const prev = entry.synced
      let same = prefixOf.get(prev)
      if (same === undefined) {
        same = samePrefixLength(prev, bars)
        prefixOf.set(prev, same)
      }
      return same >= n
    }
    const keep = new Set<string>()
    for (const config of configs) {
      const def = defs.get(config.type)
      if (def?.structure === undefined) continue
      keep.add(config.uid)
      let entry = this.entries.get(config.uid)
      if (entry === undefined) {
        entry = {
          primitive: new MarketStructurePrimitive(config.uid, this.colors),
          engine: null,
          paramsKey: '',
          synced: null,
          spans: new StructureSpans(),
          version: 0,
          show: structureShow(config.inputs),
          view: null,
          markers: NO_MARKERS,
          markersFrom: null,
          recomputes: 0,
          pushed: 0,
        }
        this.entries.set(config.uid, entry)
        this.series?.attachPrimitive(entry.primitive)
      }
      entry.primitive.setVisible(config.visible)
      // Hidden instances draw nothing, give no markers and are not advanced until shown.
      if (!config.visible) continue

      const params = structureParams(config.inputs)
      const key = structureParamsKey(params)
      if (entry.engine === null || entry.paramsKey !== key || !prefixMatches(entry)) {
        entry.engine = new MarketStructureEngine(params)
        entry.paramsKey = key
        entry.spans = new StructureSpans()
        entry.version += 1
        entry.recomputes += 1
      }
      const engine = entry.engine
      const n = engine.count
      if (closedLen > n) {
        for (let i = n; i < closedLen; i++) engine.push(bars[i])
        entry.pushed += closedLen - n
        entry.version += 1
      }
      // The pushed bars are bars[0 .. count - 1] now.
      entry.synced = bars

      const show = structureShow(config.inputs)
      if (!sameShow(show, entry.show)) entry.show = show
      const v = entry.view
      if (
        v === null ||
        v.version !== entry.version ||
        v.show !== entry.show ||
        v.result !== engine.result
      ) {
        entry.spans.update(engine.result)
        entry.view = {
          result: engine.result,
          spans: entry.spans,
          show: entry.show,
          version: entry.version,
        }
      }
      entry.primitive.setView(entry.view)

      this.refreshMarkers(entry)
    }
    for (const [uid, entry] of this.entries) {
      if (keep.has(uid)) continue
      this.series?.detachPrimitive(entry.primitive)
      this.entries.delete(uid)
    }
    this.lastConfigs = configs
    this.publishMarkers(configs)
  }

  /**
   * Merge the markers of every visible instance (config order) into the channel. When the
   * same instances contribute the same lists (by identity: a list is only rebuilt when its
   * result, toggles or colours changed) nothing is merged or published, so a forming-bar tick
   * costs O(instances).
   */
  private publishMarkers(configs: readonly IndicatorConfig[]): void {
    const lists: (readonly ChartMarker[])[] = []
    const from: string[] = []
    for (const config of configs) {
      const entry = this.entries.get(config.uid)
      if (entry === undefined || !config.visible || entry.markers.length === 0) continue
      lists.push(entry.markers)
      from.push(config.uid)
    }
    const sameFrom =
      from.length === this.mergedFrom.length && from.every((uid, i) => uid === this.mergedFrom[i])
    if (sameFrom && lists.every((l, i) => l === this.mergedLists[i])) return
    this.mergedLists = lists
    if (lists.length > 1) this.merges += 1
    const next =
      lists.length === 0 ? NO_MARKERS : lists.length === 1 ? lists[0] : mergeByTime(lists)
    if (sameFrom && sameMarkers(next, this.merged)) return
    this.mergedFrom = from
    this.merged = next
    if (this.markersOut === null) return
    if (next.length === 0) this.markersOut.detach()
    else this.markersOut.setMarkers(next.slice())
  }

  /** Rebuild `entry`'s markers when its result, toggles or the colours changed. */
  private refreshMarkers(entry: Entry): void {
    const engine = entry.engine
    if (engine === null) return
    const mf = entry.markersFrom
    if (
      mf !== null &&
      mf.version === entry.version &&
      mf.show === entry.show &&
      mf.colors === this.colors
    )
      return
    entry.markers = structureMarkers(engine.result, entry.show, this.colors)
    entry.markersFrom = { version: entry.version, show: entry.show, colors: this.colors }
  }

  setColors(colors: MarketStructureColors): void {
    const c = this.colors
    if (
      c.up === colors.up &&
      c.down === colors.down &&
      c.neutral === colors.neutral &&
      c.light === colors.light
    )
      return
    this.colors = { ...colors }
    for (const e of this.entries.values()) {
      e.primitive.setColors(this.colors)
      if (e.primitive.isVisible()) this.refreshMarkers(e)
    }
    this.publishMarkers(this.lastConfigs)
  }

  /** The merged markers last published (tests, debugging). */
  markers(): readonly ChartMarker[] {
    return this.merged
  }

  /** How many times several instances' markers were merged (tests). */
  mergeCount(): number {
    return this.merges
  }

  /** Uids with a primitive, in creation order (tests, debugging). */
  uids(): string[] {
    return [...this.entries.keys()]
  }

  primitiveOf(uid: string): MarketStructurePrimitive | undefined {
    return this.entries.get(uid)?.primitive
  }

  /** The live result of `uid` (null before its first visible sync). */
  resultOf(uid: string): MarketStructure | null {
    return this.entries.get(uid)?.engine?.result ?? null
  }

  /** Full recomputes and bars pushed so far for `uid` (tests). */
  statsOf(uid: string): { recomputes: number; pushed: number; count: number } | null {
    const e = this.entries.get(uid)
    if (e === undefined) return null
    return { recomputes: e.recomputes, pushed: e.pushed, count: e.engine?.count ?? 0 }
  }
}
