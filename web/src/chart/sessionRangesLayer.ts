// Session ranges overlay (Phase 18) on the main series: visual only, no edge claim.
//
// Per instance of a session indicator (a def with `sessions`, i.e. 'sessions'):
// - a `SessionRangesEngine` fed CLOSED bars only, one at a time: plotted (real, never
//   Heikin-Ashi) bars at or before `closedThrough` (final values confirmed, see
//   `LiveCandlesSnapshot.closedThrough`), so the forming bar, a partial bar awaiting a gap fill
//   and any bar past a possible hole are never pushed or read, and a tick on the forming bar can
//   never move a box's high or low. Kept incrementally like the market-structure layer: a new
//   closed bar costs one push, a forming-bar tick pushes nothing (one O(n) identity scan), and a
//   pushed bar that is no longer the same object, a `closedThrough` that moved back or a change
//   of the session hours starts over. The show toggles only change what is drawn, and so does
//   the alignment rule: a session whose hours do not line up with the bar length
//   (`sessionAligned`, e.g. London and New York on 4h) is not drawn. A hidden instance keeps no
//   engine and no reference to the bars; showing it again recomputes from scratch;
// - a series primitive (zOrder 'bottom': under the candles and under the drawings, which draw at
//   'top') with one translucent box per session instance, culled to the visible time range by
//   binary search on the session start.
//
// Shown only on intervals below 1d (a daily bar holds every session, so a box would be the bar).

import type {
  IChartApiBase,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  IRange,
  ISeriesApi,
  ISeriesPrimitive,
  ITimeScaleApi,
  SeriesAttachedParameter,
  SeriesType,
  Time,
  UTCTimestamp,
} from 'lightweight-charts'
import { INDICATOR_BY_ID } from '../indicators/registry'
import {
  SESSION_KEYS,
  SESSION_SHOW_KEYS,
  SessionRangesEngine,
  alignedSessions,
  sessionParams,
  sessionParamsKey,
  type SessionRangesParams,
} from '../indicators/sessionRanges'
import type {
  IndicatorDef,
  IndicatorId,
  IndicatorInputs,
  SessionBox,
  SessionKey,
  SessionRanges,
} from '../indicators/types'
import type { IndicatorConfig } from '../state/indicators'
import type { Candle } from '../types'
import { BarLineage, closedBarCount } from './structureBars'
import { fontFamily, type ChartPalette } from './theme'
import { barSpacingOf, sameViewSignature, viewSignature, type ViewSignature } from './viewSignature'

type RenderTarget = Parameters<IPrimitivePaneRenderer['draw']>[0]
type BitmapScope = Parameters<Parameters<RenderTarget['useBitmapCoordinateSpace']>[0]>[0]

/** Intervals at or above this many seconds (1d) draw no session boxes. */
export const SESSION_MAX_INTERVAL_SECONDS = 86_400
/** A session instance lasts at most 24h (the culling window). */
const MAX_SESSION_SECONDS = 86_400

/* ------------------------------------------------------------------------------------------ */
/* Colours and toggles                                                                         */
/* ------------------------------------------------------------------------------------------ */

export interface SessionRangesColors {
  asia: string
  london: string
  newyork: string
  /** The UTC opening range (neutral, dashed border). */
  opening: string
  /** Light background: deeper hues, slightly fainter fills. */
  light: boolean
}

/** Relative luminance of '#rrggbb' (0 black .. 1 white); other strings count as dark. */
function luminance(hex: string): number {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex)
  if (m === null) return 0
  const [r, g, b] = [m[1], m[2], m[3]].map((h) => parseInt(h, 16) / 255)
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

/** Muted, distinct hues (indigo, green, amber, grey), deeper on a light background. */
const DARK_SESSION_COLORS = {
  asia: '#7986cb',
  london: '#81c784',
  newyork: '#ffb74d',
  opening: '#9598a1',
}
const LIGHT_SESSION_COLORS = {
  asia: '#3f51b5',
  london: '#2e7d32',
  newyork: '#ef6c00',
  opening: '#6a6d78',
}

/** Session colours for the chart background (light or dark by luminance, as market structure). */
export function sessionRangesColorsFor(
  palette: Pick<ChartPalette, 'background'>,
): SessionRangesColors {
  const light = luminance(palette.background) > 0.5
  return { ...(light ? LIGHT_SESSION_COLORS : DARK_SESSION_COLORS), light }
}

export const DEFAULT_SESSION_RANGES_COLORS: Readonly<SessionRangesColors> = Object.freeze(
  sessionRangesColorsFor({ background: '#131722' }),
)

/** Which sessions are drawn (display only; never changes the computation). */
export type SessionShow = Readonly<Record<SessionKey, boolean>>

/** Toggle inputs are 1 (on) or 0 (off); a missing key counts as on (the default). */
export function sessionShow(inputs: IndicatorInputs): SessionShow {
  const out = {} as Record<SessionKey, boolean>
  SESSION_KEYS.forEach((key, i) => {
    out[key] = (inputs[SESSION_SHOW_KEYS[i]] ?? 1) === 1
  })
  return out
}

function sameShow(a: SessionShow, b: SessionShow): boolean {
  return SESSION_KEYS.every((k) => a[k] === b[k])
}

/**
 * Sessions drawn for `inputs` on bars of `intervalSeconds`: toggled on AND aligned with the bar
 * length (`alignedSessions`). Display only; the computation is the same either way.
 */
export function sessionDrawn(inputs: IndicatorInputs, intervalSeconds: number): SessionShow {
  const show = sessionShow(inputs)
  const aligned = alignedSessions(sessionParams(inputs), intervalSeconds)
  const out = {} as Record<SessionKey, boolean>
  for (const k of SESSION_KEYS) out[k] = show[k] && aligned[k]
  return out
}

/** Sessions toggled on but not drawn because their hours do not line up with the bar length. */
export function sessionsHiddenByAlignment(
  inputs: IndicatorInputs,
  intervalSeconds: number,
): SessionKey[] {
  if (intervalSeconds >= SESSION_MAX_INTERVAL_SECONDS) return []
  const show = sessionShow(inputs)
  const drawn = sessionDrawn(inputs, intervalSeconds)
  return SESSION_KEYS.filter((k) => show[k] && !drawn[k])
}

/* ------------------------------------------------------------------------------------------ */
/* Geometry (pure, media / CSS pixels)                                                         */
/* ------------------------------------------------------------------------------------------ */

/** What one instance's primitive draws: the boxes and the toggles. */
export interface SessionView {
  boxes: readonly SessionBox[]
  show: SessionShow
  /** Bumped whenever the boxes changed (a closed bar was pushed, or a recompute). */
  version: number
}

/** Chart conversions the geometry needs; LWC or a test fake provides them. */
export interface SessionCoords {
  /** Open times of the first and last plotted bar in view, or null without data. */
  visibleRange(): { from: number; to: number } | null
  /** x of the centre of the plotted bar opening at `time` (null if no such bar). */
  xOfTime(time: number): number | null
  /** y of `price` on the main series' scale. */
  yOfPrice(price: number): number | null
  /** Distance between neighbouring bar centres (CSS px). */
  barSpacing: number
  /** Pane width (CSS px). */
  width: number
}

export const SESSION_LABELS: Readonly<Record<SessionKey, string>> = Object.freeze({
  asia: 'Asia',
  london: 'London',
  newyork: 'New York',
  opening: 'OR',
})

export const LABEL_FONT_PX = 10
const LABEL_PAD_PX = 4
/** Rough width of one label character at LABEL_FONT_PX (avoids measureText in the pure part). */
const LABEL_CHAR_PX = 6

/** Narrowest box (CSS px) that shows `text` inside it. */
export function labelMinWidth(text: string): number {
  return text.length * LABEL_CHAR_PX + 2 * LABEL_PAD_PX
}

export interface SessionLabelGeometry {
  text: string
  x: number
  y: number
  baseline: 'top' | 'bottom'
}

export interface SessionBoxGeometry {
  session: SessionKey
  x0: number
  x1: number
  /** y of the high (smaller y) and of the low. */
  top: number
  bottom: number
  /** Null when the box is too narrow for its label. */
  label: SessionLabelGeometry | null
}

/** Keeps far off-screen edges near the pane so the canvas never sees huge coordinates. */
function clampX(x: number, width: number): number {
  return Math.min(width + 2, Math.max(-2, x))
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
 * Session labels go in the top-left corner (the opening range's in the bottom-left, so it does
 * not sit on Asia's, which starts at the same bar), inside the box when it is tall enough and
 * just outside it otherwise.
 */
function labelFor(
  session: SessionKey,
  x0: number,
  x1: number,
  top: number,
  bottom: number,
): SessionLabelGeometry | null {
  const text = SESSION_LABELS[session]
  if (x1 - x0 < labelMinWidth(text)) return null
  const x = x0 + LABEL_PAD_PX
  const inside = bottom - top >= LABEL_FONT_PX + 2 * LABEL_PAD_PX
  if (session === 'opening') {
    return inside
      ? { text, x, y: bottom - 3, baseline: 'bottom' }
      : { text, x, y: bottom + 2, baseline: 'top' }
  }
  return inside
    ? { text, x, y: top + 3, baseline: 'top' }
    : { text, x, y: top - 2, baseline: 'bottom' }
}

/**
 * The shown boxes that intersect the visible time range, in pane coordinates: from the left edge
 * of the first bar to the right edge of the last bar in the session so far (bar centre -/+ half
 * the bar spacing), from the high to the low.
 */
export function sessionGeometry(
  view: SessionView,
  coords: SessionCoords,
): SessionBoxGeometry[] | null {
  const range = coords.visibleRange()
  if (range === null) return null
  const { from, to } = range
  const { boxes, show } = view
  const half = coords.barSpacing / 2
  const width = coords.width
  const out: SessionBoxGeometry[] = []
  // A box ends within 24h of its start, so none starting earlier than this can reach `from`.
  const first = lowerBound(boxes.length, (i) => boxes[i].start, from - MAX_SESSION_SECONDS)
  for (let i = first; i < boxes.length && boxes[i].start <= to; i++) {
    const b = boxes[i]
    if (!show[b.session] || b.lastTime < from || b.firstTime > to) continue
    const xa = coords.xOfTime(b.firstTime)
    const xb = coords.xOfTime(b.lastTime)
    const yHigh = coords.yOfPrice(b.high)
    const yLow = coords.yOfPrice(b.low)
    if (xa === null || xb === null || yHigh === null || yLow === null) continue
    const x0 = clampX(xa - half, width)
    const x1 = clampX(xb + half, width)
    const top = Math.min(yHigh, yLow)
    const bottom = Math.max(yHigh, yLow)
    out.push({
      session: b.session,
      x0,
      x1,
      top,
      bottom,
      label: labelFor(b.session, x0, x1, top, bottom),
    })
  }
  return out
}

/* ------------------------------------------------------------------------------------------ */
/* Renderer + primitive                                                                        */
/* ------------------------------------------------------------------------------------------ */

/** Fill and border alpha (dark theme); light backgrounds scale the fill down. */
export const SESSION_FILL_ALPHA = 0.08
export const SESSION_BORDER_ALPHA = 0.55
const LIGHT_FILL_SCALE = 0.8
const LABEL_ALPHA = 0.9

/** Solid borders. `setLineDash` copies its argument, so shared arrays are safe (never mutated). */
const NO_DASH: number[] = []

/** The opening range's dash and the label font for one pair of pixel ratios. */
interface FrameConstants {
  hr: number
  vr: number
  dashed: number[]
  font: string
}

let frameCache: FrameConstants | null = null

/** Built once per pixel ratio pair, so a repeat draw allocates no dash array or font string. */
function frameConstants(hr: number, vr: number): FrameConstants {
  const c = frameCache
  if (c !== null && c.hr === hr && c.vr === vr) return c
  const next: FrameConstants = {
    hr,
    vr,
    dashed: [Math.round(4 * hr), Math.round(3 * hr)],
    font: `${Math.round(LABEL_FONT_PX * vr)}px ${fontFamily}`,
  }
  frameCache = next
  return next
}

/**
 * Draws one instance in bitmap space; exported for tests with a fake canvas. Boxes paint in
 * geometry order (translucent boxes of different sessions overlap, so they are never regrouped
 * by colour); only state sets that would change nothing (the same colour, dash or baseline as
 * the previous box or label) are skipped.
 */
export function drawSessions(
  scope: BitmapScope,
  geom: readonly SessionBoxGeometry[],
  colors: SessionRangesColors,
): void {
  if (geom.length === 0) return
  const ctx = scope.context
  const hr = scope.horizontalPixelRatio
  const vr = scope.verticalPixelRatio
  const frame = frameConstants(hr, vr)
  const lineWidth = Math.max(1, Math.floor(hr))
  const half = lineWidth % 2 ? 0.5 : 0
  const fillAlpha = SESSION_FILL_ALPHA * (colors.light ? LIGHT_FILL_SCALE : 1)
  // What this draw last set (null: not set yet, the inherited value is unknown).
  let fill: string | null = null
  let stroke: string | null = null
  let dash: number[] | null = null
  ctx.save()
  ctx.lineWidth = lineWidth
  for (const g of geom) {
    const color = colors[g.session]
    const left = Math.round(g.x0 * hr)
    const right = Math.max(left + 1, Math.round(g.x1 * hr))
    const top = Math.round(g.top * vr)
    const bottom = Math.max(top + 1, Math.round(g.bottom * vr))
    ctx.globalAlpha = fillAlpha
    if (fill !== color) ctx.fillStyle = fill = color
    ctx.fillRect(left, top, right - left, bottom - top)
    ctx.globalAlpha = SESSION_BORDER_ALPHA
    if (stroke !== color) ctx.strokeStyle = stroke = color
    const boxDash = g.session === 'opening' ? frame.dashed : NO_DASH
    if (dash !== boxDash) ctx.setLineDash((dash = boxDash))
    ctx.strokeRect(
      left + half,
      top + half,
      Math.max(0, right - left - lineWidth),
      Math.max(0, bottom - top - lineWidth),
    )
  }
  // Labels after every box, so a neighbour's fill never covers them.
  if (dash !== NO_DASH) ctx.setLineDash(NO_DASH)
  ctx.globalAlpha = LABEL_ALPHA
  ctx.font = frame.font
  ctx.textAlign = 'left'
  let baseline: SessionLabelGeometry['baseline'] | null = null
  for (const g of geom) {
    const l = g.label
    if (l === null) continue
    const color = colors[g.session]
    if (fill !== color) ctx.fillStyle = fill = color
    if (baseline !== l.baseline) ctx.textBaseline = baseline = l.baseline
    ctx.fillText(l.text, Math.round(l.x * hr), Math.round(l.y * vr))
  }
  ctx.restore()
}

class SessionRenderer implements IPrimitivePaneRenderer {
  geom: SessionBoxGeometry[] | null = null
  colors: SessionRangesColors = DEFAULT_SESSION_RANGES_COLORS

  draw(target: RenderTarget): void {
    const geom = this.geom
    if (geom === null || geom.length === 0) return
    target.useBitmapCoordinateSpace((scope) => drawSessions(scope, geom, this.colors))
  }
}

class SessionPaneView implements IPrimitivePaneView {
  readonly renderer_ = new SessionRenderer()

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
 * Reference prices of the geometry cache's signature: positive (valid on a log scale) and
 * distinct; every price scale mode maps prices through a function fixed by two points.
 */
const REF_PRICES: readonly [number, number] = [1, 2]

/** A visible time range in UNIX seconds. */
type TimeRange = { readonly from: number; readonly to: number }

/** True when the chart's visible range is in UNIX seconds (not null, not business days). */
function isTimestampRange(r: IRange<Time> | null): r is IRange<UTCTimestamp> {
  return r !== null && typeof r.from === 'number' && typeof r.to === 'number'
}

/**
 * `SessionCoords` over the chart, one per primitive (no closures per frame): `updateAllViews`
 * points it at the time scale and series and sets the range, spacing and width it already read.
 */
class ChartSessionCoords implements SessionCoords {
  ts: ITimeScaleApi<Time> | null = null
  series: ISeriesApi<SeriesType, Time> | null = null
  range: TimeRange | null = null
  barSpacing = 0
  width = 0

  visibleRange(): TimeRange | null {
    return this.range
  }

  // Box edges are plotted bars (first / last bar of the session), so the exact lookup works;
  // it also places bars outside the view.
  xOfTime(time: number): number | null {
    return this.ts === null ? null : this.ts.timeToCoordinate(time as Time)
  }

  yOfPrice(price: number): number | null {
    return this.series === null ? null : this.series.priceToCoordinate(price)
  }
}

/**
 * One instance's session boxes on the main series. No `autoscaleInfo` (old boxes never stretch
 * the price scale) and no hit test (it never takes clicks from the candles or the drawings).
 */
export class SessionRangesPrimitive implements ISeriesPrimitive<Time> {
  readonly uid: string
  private viewData: SessionView | null = null
  private visible = true
  private chart: IChartApiBase<Time> | null = null
  private series: ISeriesApi<SeriesType, Time> | null = null
  private requestUpdate: (() => void) | null = null
  private readonly paneView = new SessionPaneView()
  private readonly paneViewList: readonly IPrimitivePaneView[] = [this.paneView]
  /**
   * Bumped whenever what the geometry is built from changes (view, visibility, colours,
   * attach / detach); part of the geometry cache's signature.
   */
  private version = 0
  /** The view signature and time range the current geometry was built for (null = none). */
  private cachedSig: ViewSignature | null = null
  private cachedRange: TimeRange | null = null
  private readonly coordsAdapter = new ChartSessionCoords()
  /** Geometry builds so far (tests: an unchanged view must not rebuild). */
  private builds = 0

  constructor(uid: string, colors: SessionRangesColors) {
    this.uid = uid
    this.paneView.renderer_.colors = colors
  }

  attached(param: AttachedParam): void {
    this.chart = param.chart
    this.series = param.series
    this.requestUpdate = param.requestUpdate
    this.version += 1
  }

  detached(): void {
    this.chart = null
    this.series = null
    this.requestUpdate = null
    this.paneView.renderer_.geom = null
    this.coordsAdapter.ts = null
    this.coordsAdapter.series = null
    this.version += 1
  }

  /** What the primitive draws (the same object until the boxes or the toggles change). */
  view(): SessionView | null {
    return this.viewData
  }

  isVisible(): boolean {
    return this.visible
  }

  setView(view: SessionView | null): void {
    if (view === this.viewData) return
    this.viewData = view
    this.version += 1
    this.requestUpdate?.()
  }

  setVisible(visible: boolean): void {
    if (visible === this.visible) return
    this.visible = visible
    this.version += 1
    this.requestUpdate?.()
  }

  setColors(colors: SessionRangesColors): void {
    this.paneView.renderer_.colors = colors
    this.version += 1
    this.requestUpdate?.()
  }

  colors(): SessionRangesColors {
    return this.paneView.renderer_.colors
  }

  /** The geometry drawn on the last frame (null = nothing drawn). */
  geometry(): SessionBoxGeometry[] | null {
    return this.paneView.renderer_.geom
  }

  /** Geometry builds so far (tests, debugging). */
  buildCount(): number {
    return this.builds
  }

  /**
   * Rebuilds the geometry only when the view moved: a drawing drag, a live tick or a hover
   * change asks every primitive to update (a crosshair-only move repaints without calling
   * this), and an unchanged signature (scroll position, bar
   * spacing, width, two price coordinates, `version`) plus an unchanged visible time range
   * means every coordinate the geometry reads is unchanged too.
   */
  updateAllViews(): void {
    const view = this.viewData
    const chart = this.chart
    const series = this.series
    const renderer = this.paneView.renderer_
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
    const coords = this.coordsAdapter
    coords.ts = ts
    coords.series = series
    coords.range = range
    coords.barSpacing = sig === null ? barSpacingOf(ts) : sig.barSpacing
    coords.width = sig === null ? ts.width() : sig.width
    renderer.geom = sessionGeometry(view, coords)
    this.builds += 1
    this.cachedSig = sig
    this.cachedRange = range
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
  primitive: SessionRangesPrimitive
  engine: SessionRangesEngine | null
  paramsKey: string
  /**
   * Params, their key and the drawn sessions derived from `inputs` and `intervalSeconds`
   * (re-derived only when the config's inputs object or the interval changes, not per tick;
   * IndicatorInputs are immutable, replaced on edit).
   */
  derived: DerivedInputs | null
  /** The bars array the engine was last brought up to date with (its first `count` were pushed). */
  synced: readonly Candle[] | null
  boxes: readonly SessionBox[]
  version: number
  show: SessionShow
  view: SessionView | null
  recomputes: number
  pushed: number
}

const NO_BOXES: readonly SessionBox[] = Object.freeze([])

interface DerivedInputs {
  inputs: IndicatorInputs
  intervalSeconds: number
  params: SessionRangesParams
  key: string
  show: SessionShow
}

/** `entry`'s params/key/drawn sessions, derived once per inputs object and interval. */
function derivedInputs(
  entry: Entry,
  inputs: IndicatorInputs,
  intervalSeconds: number,
): DerivedInputs {
  const d = entry.derived
  if (d !== null && d.inputs === inputs && d.intervalSeconds === intervalSeconds) return d
  const params = sessionParams(inputs)
  const next = {
    inputs,
    intervalSeconds,
    params,
    key: sessionParamsKey(params),
    show: sessionDrawn(inputs, intervalSeconds),
  }
  entry.derived = next
  return next
}

/**
 * Every session-ranges instance drawn on the main series. Usage from Chart: `attach(series)` when
 * the main series is created and `detach(alive)` before it is removed (chart-type switch),
 * `sync(configs, appliedCandles, closedThrough, intervalSeconds)` when the configs, candles,
 * closedThrough or interval change, `setColors` on theme change.
 */
export class SessionRangesLayer {
  private series: PrimitiveHost | null = null
  private readonly entries = new Map<string, Entry>()
  private colors: SessionRangesColors

  /** The chart's record of which bars a tick kept (skips the identity scan on live ticks). */
  private readonly lineage: BarLineage

  constructor(
    colors: SessionRangesColors = DEFAULT_SESSION_RANGES_COLORS,
    lineage: BarLineage = new BarLineage(),
  ) {
    this.colors = colors
    this.lineage = lineage
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
   * `closedThrough` (null = none) are pushed or read: never the forming bar. Only bars not yet
   * pushed are processed; a change of the session hours, or any pushed bar that is no longer the
   * same object (older history, a revised bar, another window, a replay step back), starts over.
   * `intervalSeconds` >= 1d draws nothing and computes nothing; below 1d, sessions whose hours do
   * not line up with it are not drawn (display only, no recompute). A hidden instance drops its
   * engine and bars reference.
   */
  sync(
    configs: readonly IndicatorConfig[],
    candles: readonly Candle[] | null,
    closedThrough: number | null,
    intervalSeconds: number,
    defs: ReadonlyMap<IndicatorId, IndicatorDef> = INDICATOR_BY_ID,
  ): void {
    const bars = candles ?? []
    const intraday = intervalSeconds < SESSION_MAX_INTERVAL_SECONDS
    const closedLen = intraday ? closedBarCount(bars, closedThrough) : 0
    const prefixOf = new Map<readonly Candle[], number>()
    const prefixMatches = (entry: Entry): boolean => {
      const n = entry.engine?.count ?? 0
      if (n === 0) return true
      if (closedLen < n || entry.synced === null) return false
      const prev = entry.synced
      let same = prefixOf.get(prev)
      if (same === undefined) {
        same = this.lineage.samePrefixLength(prev, bars)
        prefixOf.set(prev, same)
      }
      return same >= n
    }
    const keep = new Set<string>()
    for (const config of configs) {
      const def = defs.get(config.type)
      if (def?.sessions === undefined) continue
      keep.add(config.uid)
      let entry = this.entries.get(config.uid)
      if (entry === undefined) {
        entry = {
          primitive: new SessionRangesPrimitive(config.uid, this.colors),
          engine: null,
          paramsKey: '',
          derived: null,
          synced: null,
          boxes: NO_BOXES,
          version: 0,
          show: sessionDrawn(config.inputs, intervalSeconds),
          view: null,
          recomputes: 0,
          pushed: 0,
        }
        this.entries.set(config.uid, entry)
        this.series?.attachPrimitive(entry.primitive)
      }
      entry.primitive.setVisible(config.visible)
      if (!config.visible || !intraday) {
        // Hidden, or 1d and above: nothing drawn, and the engine, the boxes and the bars
        // reference are dropped (no stale history kept across symbol / interval switches).
        // Showing it again (or going back below 1d) recomputes from scratch.
        entry.engine = null
        entry.synced = null
        entry.boxes = NO_BOXES
        entry.view = null
        entry.primitive.setView(null)
        continue
      }

      const derived = derivedInputs(entry, config.inputs, intervalSeconds)
      const key = derived.key
      if (entry.engine === null || entry.paramsKey !== key || !prefixMatches(entry)) {
        entry.engine = new SessionRangesEngine(derived.params)
        entry.paramsKey = key
        entry.boxes = NO_BOXES
        entry.version += 1
        entry.recomputes += 1
      }
      const engine = entry.engine
      const n = engine.count
      if (closedLen > n) {
        for (let i = n; i < closedLen; i++) engine.push(bars[i])
        entry.pushed += closedLen - n
        entry.boxes = engine.result().boxes
        entry.version += 1
      }
      entry.synced = bars

      const show = derived.show
      if (!sameShow(show, entry.show)) entry.show = show
      const v = entry.view
      if (v === null || v.version !== entry.version || v.show !== entry.show) {
        entry.view = { boxes: entry.boxes, show: entry.show, version: entry.version }
      }
      entry.primitive.setView(entry.view)
    }
    for (const [uid, entry] of this.entries) {
      if (keep.has(uid)) continue
      this.series?.detachPrimitive(entry.primitive)
      this.entries.delete(uid)
    }
  }

  setColors(colors: SessionRangesColors): void {
    const c = this.colors
    if (
      c.asia === colors.asia &&
      c.london === colors.london &&
      c.newyork === colors.newyork &&
      c.opening === colors.opening &&
      c.light === colors.light
    )
      return
    this.colors = { ...colors }
    for (const e of this.entries.values()) e.primitive.setColors(this.colors)
  }

  /** Uids with a primitive, in creation order (tests, debugging). */
  uids(): string[] {
    return [...this.entries.keys()]
  }

  primitiveOf(uid: string): SessionRangesPrimitive | undefined {
    return this.entries.get(uid)?.primitive
  }

  /** The current boxes of `uid` (null while hidden, on 1d+, or before its first visible sync). */
  resultOf(uid: string): SessionRanges | null {
    const e = this.entries.get(uid)
    if (e === undefined || e.engine === null) return null
    return { boxes: e.boxes.slice() }
  }

  /** Full recomputes and bars pushed so far for `uid` (tests). */
  statsOf(uid: string): { recomputes: number; pushed: number; count: number } | null {
    const e = this.entries.get(uid)
    if (e === undefined) return null
    return { recomputes: e.recomputes, pushed: e.pushed, count: e.engine?.count ?? 0 }
  }
}
