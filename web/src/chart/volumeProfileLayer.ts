// Fixed-range volume profiles drawn on the main series as series primitives.
//
// The renderer is ported from TradingView's Lightweight Charts plugin example "volume-profile"
// (https://github.com/tradingview/lightweight-charts/tree/master/plugin-examples/src/plugins/volume-profile),
// Copyright TradingView, Inc., licensed under the Apache License, Version 2.0 (Apache-2.0).
// Changes from the example: one primitive per indicator instance, rows from the indicator's
// `profile()` (up/down volume in two tones, value-area rows brighter), a point-of-control line,
// value-area high/low lines, a range outline, pixel-aligned rows, and hit testing.
//
// The profile itself is computed by the indicator def (`IndicatorDef.profile`); this file only
// draws it. It is recomputed only when the instance's inputs or the candles in its range change
// (candle object identity), so live ticks after a fixed range cost a binary search and an
// identity check (O(1) with the chart's shared `BarLineage`, which knows what the tick kept;
// otherwise a scan of the range); a tick inside an open-ended range adds only the forming bar
// (O(rows)).

import type {
  AutoscaleInfo,
  IChartApi,
  IChartApiBase,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesApi,
  ISeriesPrimitive,
  Logical,
  PrimitiveHoveredItem,
  SeriesAttachedParameter,
  SeriesType,
  Time,
} from 'lightweight-charts'
import { INDICATOR_BY_ID } from '../indicators/registry'
import {
  extendOpenProfile,
  openProfileBase,
  openProfileFits,
  profileRange,
  volumeProfileDef,
  type OpenProfileBase,
} from '../indicators/volumeProfile'
import type {
  IndicatorDef,
  IndicatorId,
  IndicatorInputs,
  VolumeProfile,
  VolumeProfileRow,
} from '../indicators/types'
import type { IndicatorConfig } from '../state/indicators'
import type { Candle } from '../types'
import { BarLineage } from './structureBars'
import type { ChartPalette } from './theme'
import { barSpacingOf, sameViewSignature, viewSignature, type ViewSignature } from './viewSignature'

type RenderTarget = Parameters<IPrimitivePaneRenderer['draw']>[0]
type BitmapScope = Parameters<Parameters<RenderTarget['useBitmapCoordinateSpace']>[0]>[0]

export interface VolumeProfileColors {
  /** Volume of bars that closed at or above their open. */
  up: string
  /** Volume of the other bars. */
  down: string
  /** Point-of-control line. */
  poc: string
  /** Value-area high / low lines. */
  valueArea: string
  /** Range outline and background tint. */
  text: string
}

/** TradingView's fixed-range volume profile look: blue up, amber down, red POC. */
export function volumeProfileColorsFor(
  palette: Pick<ChartPalette, 'text' | 'mutedText'>,
): VolumeProfileColors {
  return {
    up: '#2962ff',
    down: '#fbc02d',
    poc: '#f23645',
    valueArea: palette.mutedText,
    text: palette.text,
  }
}

export const DEFAULT_VOLUME_PROFILE_COLORS: Readonly<VolumeProfileColors> = Object.freeze(
  volumeProfileColorsFor({ text: '#b2b5be', mutedText: '#787b86' }),
)

/** Longest row as a share of the range's pixel width (TradingView draws ~30%). */
export const PROFILE_WIDTH_SHARE = 0.3
/** Longest row is never shorter than this (CSS px), so a narrow range stays readable. */
export const MIN_PROFILE_BAR_PX = 48
/** Row opacity inside / outside the value area. */
export const VALUE_AREA_ALPHA = 0.72
export const OUTSIDE_VALUE_AREA_ALPHA = 0.3
const BOX_FILL_ALPHA = 0.04
const OUTLINE_ALPHA = 0.32
const VA_LINE_ALPHA = 0.75
/** Hit tolerance around the POC line (CSS px), and the shortest hittable row. */
const POC_HIT_PX = 3
const MIN_HIT_BAR_PX = 4

/* ------------------------------------------------------------------------------------------ */
/* Geometry (pure, media / CSS pixels)                                                         */
/* ------------------------------------------------------------------------------------------ */

/** Chart coordinate conversions the geometry needs; LWC or a test fake provides them. */
export interface ProfileCoords {
  /** x of the bar at (or nearest to) `time`; may lie outside the pane. Null without data. */
  xOfTime(time: number): number | null
  /** y of `price` on the main series' scale; may lie outside the pane. Null without data. */
  yOfPrice(price: number): number | null
  barSpacing: number
}

export interface ProfileRowGeometry {
  /** y of the row's high (smaller y) and low edges. */
  top: number
  bottom: number
  /** Up-volume and down-volume lengths from `barX`, in that order. */
  upLength: number
  downLength: number
  inValueArea: boolean
}

export interface ProfileGeometry {
  /** Range edges: half a bar left of the `from` bar, half a bar right of the `to` bar. */
  x0: number
  x1: number
  /** Price extent of the rows (top = highest price). */
  top: number
  bottom: number
  /** Rows start here and grow to the right. */
  barX: number
  /** Length of the row with the most volume. */
  maxBarLength: number
  /** Ascending by price, as in the profile. */
  rows: ProfileRowGeometry[]
  pocY: number | null
  vahY: number | null
  valY: number | null
}

/** Length of the longest row for a range `rangeWidth` px wide. */
export function profileMaxBarLength(rangeWidth: number): number {
  return Math.max(MIN_PROFILE_BAR_PX, PROFILE_WIDTH_SHARE * Math.max(0, rangeWidth))
}

/** Up and down lengths of one row, scaled so `maxVolume` maps to `maxLength`. */
export function rowBarLengths(
  row: Pick<VolumeProfileRow, 'volume' | 'upVolume'>,
  maxVolume: number,
  maxLength: number,
): { up: number; down: number } {
  if (!(maxVolume > 0) || !(row.volume > 0)) return { up: 0, down: 0 }
  const total = (maxLength * row.volume) / maxVolume
  const upShare = Math.min(1, Math.max(0, row.upVolume / row.volume))
  const up = total * upShare
  return { up, down: total - up }
}

/** A row is in the value area when it lies within [val, vah] (relative tolerance for rounding). */
export function isValueAreaRow(
  row: Pick<VolumeProfileRow, 'low' | 'high'>,
  profile: Pick<VolumeProfile, 'vah' | 'val'>,
): boolean {
  const eps = Math.max(Math.abs(profile.vah), Math.abs(profile.val), 1) * 1e-9
  return row.low >= profile.val - eps && row.high <= profile.vah + eps
}

/**
 * Per-profile constants of the geometry: the largest row volume and each row's value-area flag.
 * They depend only on the (immutable) profile, so the primitive computes them once per
 * `setProfile` instead of once per frame.
 */
export interface ProfileStats {
  maxVolume: number
  /** `isValueAreaRow(profile.rows[i], profile)`, by row index. */
  inValueArea: readonly boolean[]
}

export function profileStats(profile: VolumeProfile): ProfileStats {
  let maxVolume = 0
  const inValueArea: boolean[] = []
  for (const r of profile.rows) {
    if (r.volume > maxVolume) maxVolume = r.volume
    inValueArea.push(isValueAreaRow(r, profile))
  }
  return { maxVolume, inValueArea }
}

/** Horizontal extent of a profile: everything `profileOffscreen` needs, before any row. */
export interface ProfileSpan {
  x0: number
  x1: number
  /** Rows start at `x0` (the geometry's `barX`). */
  maxBarLength: number
}

/** Span of a range whose `from` / `to` bars sit at `xFrom` / `xTo`. */
export function profileSpan(xFrom: number, xTo: number, barSpacing: number): ProfileSpan {
  const half = Math.max(0, barSpacing) / 2
  const x0 = Math.min(xFrom, xTo) - half
  const x1 = Math.max(xFrom, xTo) + half
  return { x0, x1, maxBarLength: profileMaxBarLength(x1 - x0) }
}

/** `profileOffscreen` of the geometry this span would give (rows start at `x0`). */
export function spanOffscreen(span: ProfileSpan, width: number): boolean {
  const right = Math.max(span.x1, span.x0 + span.maxBarLength)
  return right < 0 || span.x0 > width
}

/** Rows and price lines of a profile laid out in `span`; null when a price cannot be placed. */
function geometryInSpan(
  profile: VolumeProfile,
  span: ProfileSpan,
  yOfPrice: (price: number) => number | null,
  stats: ProfileStats,
): ProfileGeometry | null {
  const { x0, x1, maxBarLength } = span
  const maxVolume = stats.maxVolume
  const rows: ProfileRowGeometry[] = []
  let top = Number.POSITIVE_INFINITY
  let bottom = Number.NEGATIVE_INFINITY
  const source = profile.rows
  for (let i = 0; i < source.length; i++) {
    const r = source[i]
    const yHigh = yOfPrice(r.high)
    const yLow = yOfPrice(r.low)
    if (yHigh === null || yLow === null) return null
    const { up, down } = rowBarLengths(r, maxVolume, maxBarLength)
    const row: ProfileRowGeometry = {
      top: Math.min(yHigh, yLow),
      bottom: Math.max(yHigh, yLow),
      upLength: up,
      downLength: down,
      inValueArea: stats.inValueArea[i],
    }
    if (row.top < top) top = row.top
    if (row.bottom > bottom) bottom = row.bottom
    rows.push(row)
  }
  return {
    x0,
    x1,
    top,
    bottom,
    barX: x0,
    maxBarLength,
    rows,
    pocY: yOfPrice(profile.poc),
    vahY: yOfPrice(profile.vah),
    valY: yOfPrice(profile.val),
  }
}

/**
 * Screen geometry of a profile, or null when the chart cannot place it (no data). `stats`
 * defaults to `profileStats(profile)`; pass the cached one to skip that pass.
 */
export function profileGeometry(
  profile: VolumeProfile,
  coords: ProfileCoords,
  stats?: ProfileStats,
): ProfileGeometry | null {
  if (profile.rows.length === 0) return null
  const xFrom = coords.xOfTime(profile.from)
  const xTo = coords.xOfTime(profile.to)
  if (xFrom === null || xTo === null) return null
  const span = profileSpan(xFrom, xTo, coords.barSpacing)
  return geometryInSpan(profile, span, coords.yOfPrice, stats ?? profileStats(profile))
}

/** True when no part of the profile (range or its rows) is inside a pane `width` px wide. */
export function profileOffscreen(geom: ProfileGeometry, width: number): boolean {
  const right = Math.max(geom.x1, geom.barX + geom.maxBarLength)
  return right < 0 || geom.x0 > width
}

/** Over the profile body: a row's bar (at least MIN_HIT_BAR_PX long) or the POC line. */
export function hitProfile(geom: ProfileGeometry, x: number, y: number): boolean {
  if (geom.pocY !== null && Math.abs(y - geom.pocY) <= POC_HIT_PX && x >= geom.x0 && x <= geom.x1)
    return true
  if (y < geom.top || y > geom.bottom || x < geom.barX) return false
  for (const r of geom.rows) {
    if (y < r.top || y > r.bottom) continue
    const len = r.upLength + r.downLength
    if (len > 0 && x <= geom.barX + Math.max(MIN_HIT_BAR_PX, len)) return true
  }
  return false
}

/**
 * Bitmap span of the media interval [a, b] at `ratio`: edges rounded to device pixels, so
 * adjacent rows share an edge exactly. With `gap`, spans of 3+ px give up their last pixel as a
 * separator. Never shorter than 1 px.
 */
export function bitmapSpan(
  a: number,
  b: number,
  ratio: number,
  gap = false,
): { position: number; length: number } {
  const p1 = Math.round(Math.min(a, b) * ratio)
  const p2 = Math.round(Math.max(a, b) * ratio)
  const raw = p2 - p1
  const length = Math.max(1, gap && raw >= 3 ? raw - 1 : raw)
  return { position: p1, length }
}

/* ------------------------------------------------------------------------------------------ */
/* Recompute caching (pure)                                                                    */
/* ------------------------------------------------------------------------------------------ */

/**
 * Incremental path for an open-ended range (`to` 0): the closed bars are accumulated once and a
 * tick on the forming (last) bar only adds that bar's share. `volumeProfileOpenOps` implements
 * it for the fixed-range volume profile; tests inject counting wrappers.
 */
export interface OpenProfileOps {
  /** Closed bars `candles[start .. last - 1]` on the grid that also spans `candles[last]`. */
  base(
    candles: readonly Candle[],
    start: number,
    last: number,
    inputs: IndicatorInputs,
  ): OpenProfileBase
  /** `forming` leaves the base's row grid unchanged (its high/low stay inside the span). */
  fits(base: OpenProfileBase, forming: Candle): boolean
  /** The profile with `forming` as the last bar (requires `fits`). */
  extend(base: OpenProfileBase, forming: Candle, inputs: IndicatorInputs): VolumeProfile | null
}

export const volumeProfileOpenOps: OpenProfileOps = {
  base: (candles, start, last, inputs) => openProfileBase(candles, start, last, inputs.rows),
  fits: openProfileFits,
  extend: (base, forming, inputs) => extendOpenProfile(base, forming, inputs.valueArea),
}

/** Last computed profile of one instance and what it was computed from. */
export interface ProfileCache {
  inputsKey: string
  /**
   * The inputs object `inputsKey` was built from. Configs are immutable, so the same object
   * means the same key and the string is not rebuilt per tick.
   */
  inputs?: IndicatorInputs
  candles: readonly Candle[] | null
  /** In-range bars when computed: `candles[start .. start + count - 1]`. */
  start: number
  count: number
  /**
   * `from` is at or after the first loaded bar (or not set). Part of the key: older history
   * loaded before an unloaded `from` can make the profile valid without touching the range.
   */
  anchorLoaded: boolean
  /** Open-ended range with incremental ops: the closed-bar accumulation, else null. */
  base: OpenProfileBase | null
  profile: VolumeProfile | null
}

/** Stable text of an instance's inputs (sorted keys). */
export function inputsKey(inputs: IndicatorInputs): string {
  return Object.keys(inputs)
    .sort()
    .map((k) => `${k}=${inputs[k]}`)
    .join('&')
}

/**
 * The bars a range profile can depend on: `candles[start .. start + count - 1]`, those with
 * `from <= time <= to` (`to` 0 or missing = up to the last bar; `from` 0 or missing = from the
 * first). Two binary searches.
 */
export function rangeOf(
  candles: readonly Candle[],
  inputs: IndicatorInputs,
): { start: number; count: number } {
  return profileRange(candles, inputs.from ?? 0, inputs.to ?? 0)
}

/** `from` set and before the first loaded bar: the range start is not in the history. */
function isAnchorLoaded(candles: readonly Candle[], inputs: IndicatorInputs): boolean {
  const from = inputs.from ?? 0
  return !(from > 0) || (candles.length > 0 && from >= candles[0].time)
}

/**
 * The first `k` in-range bars are the same objects in both arrays. The candle store replaces a
 * bar's object whenever any of its fields changes and keeps identity for unchanged bars, so
 * this catches every edit (a mid-range high, offsetting volume edits) in O(k) pointer checks.
 */
function sameBars(
  a: readonly Candle[],
  aStart: number,
  b: readonly Candle[],
  bStart: number,
  k: number,
  lineage: Pick<BarLineage, 'knownSamePrefix'> | null = null,
): boolean {
  let i = 0
  if (lineage !== null && aStart === bStart) {
    // a[j] === b[j] for j < known: the in-range bars below it need no check.
    const known = lineage.knownSamePrefix(a, b)
    if (known !== null) i = Math.min(k, Math.max(0, known - aStart))
  }
  for (; i < k; i++) if (a[aStart + i] !== b[bStart + i]) return false
  return true
}

/**
 * The profile of `def` for `inputs` over `candles`, reusing `cache` (the same object is
 * returned) when the inputs are unchanged and the candles array is the same. Also reuses the
 * profile when the in-range bars are the same objects (a tick after the range, older history
 * prepended before it). For an open-ended range with `open` ops (default: the registry's
 * volume profile), a tick that replaced only the last bar adds that bar to the cached closed-bar
 * accumulation instead of recomputing the range, unless its high/low moved the row grid.
 */
export function nextProfileCache(
  cache: ProfileCache | null,
  def: Pick<IndicatorDef, 'profile'>,
  inputs: IndicatorInputs,
  candles: readonly Candle[] | null,
  open: OpenProfileOps | null = def.profile === volumeProfileDef.profile
    ? volumeProfileOpenOps
    : null,
  lineage: Pick<BarLineage, 'knownSamePrefix'> | null = null,
): ProfileCache {
  const key = cache !== null && cache.inputs === inputs ? cache.inputsKey : inputsKey(inputs)
  if (cache !== null && cache.inputsKey === key && cache.candles === candles) return cache
  if (candles === null || candles.length === 0 || def.profile === undefined) {
    return {
      inputsKey: key,
      inputs,
      candles,
      start: 0,
      count: 0,
      anchorLoaded: false,
      base: null,
      profile: null,
    }
  }
  const { start, count } = rangeOf(candles, inputs)
  const anchorLoaded = isAnchorLoaded(candles, inputs)
  const prev =
    cache !== null &&
    cache.inputsKey === key &&
    cache.anchorLoaded === anchorLoaded &&
    cache.count === count
      ? cache
      : null
  const prevCandles = prev?.candles ?? null
  if (prev !== null && prevCandles !== null) {
    if (sameBars(prevCandles, prev.start, candles, start, count, lineage)) {
      // Same bars in range: keep the profile, remember the new array for the identity check.
      return { ...prev, inputs, candles, start }
    }
  }

  const openEnded =
    open !== null && (inputs.from ?? 0) > 0 && !((inputs.to ?? 0) > 0) && anchorLoaded && count > 0
  if (!openEnded) {
    return {
      inputsKey: key,
      inputs,
      candles,
      start,
      count,
      anchorLoaded,
      base: null,
      profile: def.profile(candles, inputs),
    }
  }

  const last = start + count - 1
  const forming = candles[last]
  // Tick on the forming bar: every closed bar in range is the same object as before.
  let base =
    prev !== null &&
    prevCandles !== null &&
    prev.base !== null &&
    sameBars(prevCandles, prev.start, candles, start, count - 1, lineage) &&
    open.fits(prev.base, forming)
      ? prev.base
      : null
  if (base === null) base = open.base(candles, start, last, inputs)
  return {
    inputsKey: key,
    inputs,
    candles,
    start,
    count,
    anchorLoaded,
    base,
    profile: open.extend(base, forming, inputs),
  }
}

/* ------------------------------------------------------------------------------------------ */
/* Renderer + primitive                                                                        */
/* ------------------------------------------------------------------------------------------ */

function hLine(scope: BitmapScope, x0: number, x1: number, y: number, width: number): void {
  const ctx = scope.context
  const yb = Math.round(y * scope.verticalPixelRatio) + (width % 2 ? 0.5 : 0)
  ctx.beginPath()
  ctx.moveTo(Math.round(x0 * scope.horizontalPixelRatio), yb)
  ctx.lineTo(Math.round(x1 * scope.horizontalPixelRatio), yb)
  ctx.stroke()
}

/** `setLineDash` copies its argument, so one empty pattern serves every call. */
const NO_DASH: number[] = []
let vaDashRatio = Number.NaN
let vaDash: number[] = NO_DASH

/** The value-area lines' dotted pattern at `ratio` (bitmap px), rebuilt only when it changes. */
export function valueAreaDash(ratio: number): number[] {
  if (ratio !== vaDashRatio) {
    vaDashRatio = ratio
    vaDash = [Math.max(1, Math.round(2 * ratio)), Math.max(2, Math.round(3 * ratio))]
  }
  return vaDash
}

/** Fields per drawn row in `BitmapRows.data`: y, height, left, up end, end, value-area flag. */
const ROW_FIELDS = 6

/**
 * The integer bitmap rects of a geometry's rows at one pixel ratio (rows with no volume left
 * out), computed once per geometry and ratio, so a repaint of an unchanged view only issues
 * canvas calls.
 */
interface BitmapRows {
  hr: number
  vr: number
  count: number
  data: Float64Array
}

const bitmapRowsCache = new WeakMap<ProfileGeometry, BitmapRows>()

function bitmapRowsOf(geom: ProfileGeometry, hr: number, vr: number): BitmapRows {
  const cached = bitmapRowsCache.get(geom)
  if (cached !== undefined && cached.hr === hr && cached.vr === vr) return cached
  const data = new Float64Array(geom.rows.length * ROW_FIELDS)
  let count = 0
  for (const row of geom.rows) {
    const len = row.upLength + row.downLength
    if (!(len > 0)) continue
    const ys = bitmapSpan(row.top, row.bottom, vr, true)
    const left = Math.round(geom.barX * hr)
    const o = count * ROW_FIELDS
    data[o] = ys.position
    data[o + 1] = ys.length
    data[o + 2] = left
    data[o + 3] = Math.round((geom.barX + row.upLength) * hr)
    data[o + 4] = Math.max(left + 1, Math.round((geom.barX + len) * hr))
    data[o + 5] = row.inValueArea ? 1 : 0
    count++
  }
  const rows: BitmapRows = { hr, vr, count, data }
  bitmapRowsCache.set(geom, rows)
  return rows
}

/**
 * Row fills in profile order (paint order is kept: thin rows can share a device pixel, and
 * translucent fills composite in order). A style is set only when it differs from the one set
 * just before, which leaves every fill with the same alpha and colour as setting it per row.
 */
function fillRows(
  ctx: BitmapScope['context'],
  rows: BitmapRows,
  colors: VolumeProfileColors,
): void {
  const d = rows.data
  let alpha = Number.NaN
  let style: string | null = null
  for (let i = 0; i < rows.count; i++) {
    const o = i * ROW_FIELDS
    const y = d[o]
    const h = d[o + 1]
    const left = d[o + 2]
    const upEnd = d[o + 3]
    const end = d[o + 4]
    const a = d[o + 5] === 1 ? VALUE_AREA_ALPHA : OUTSIDE_VALUE_AREA_ALPHA
    if (a !== alpha) {
      ctx.globalAlpha = a
      alpha = a
    }
    if (upEnd > left) {
      if (style !== colors.up) {
        ctx.fillStyle = colors.up
        style = colors.up
      }
      ctx.fillRect(left, y, upEnd - left, h)
    }
    const downStart = Math.max(left, upEnd)
    if (end > downStart) {
      if (style !== colors.down) {
        ctx.fillStyle = colors.down
        style = colors.down
      }
      ctx.fillRect(downStart, y, end - downStart, h)
    }
  }
}

/** Draws one profile in bitmap space; exported for tests with a fake canvas. */
export function drawProfile(
  scope: BitmapScope,
  geom: ProfileGeometry,
  colors: VolumeProfileColors,
): void {
  const ctx = scope.context
  const hr = scope.horizontalPixelRatio
  const vr = scope.verticalPixelRatio
  ctx.save()
  const box = bitmapSpan(geom.x0, geom.x1, hr)
  const boxY = bitmapSpan(geom.top, geom.bottom, vr)

  // Faint range box + outline (the example's translucent box).
  ctx.globalAlpha = BOX_FILL_ALPHA
  ctx.fillStyle = colors.text
  ctx.fillRect(box.position, boxY.position, box.length, boxY.length)
  const outline = Math.max(1, Math.floor(hr))
  ctx.globalAlpha = OUTLINE_ALPHA
  ctx.strokeStyle = colors.text
  ctx.lineWidth = outline
  ctx.setLineDash(NO_DASH)
  const o = outline % 2 ? 0.5 : 0
  ctx.strokeRect(box.position + o, boxY.position + o, box.length - 1, boxY.length - 1)

  // Rows: up volume then down volume, from the range's left edge.
  fillRows(ctx, bitmapRowsOf(geom, hr, vr), colors)

  // Value-area high / low (dotted) and the point of control (solid), across the range.
  const lineWidth = Math.max(1, Math.floor(vr))
  ctx.lineWidth = lineWidth
  ctx.globalAlpha = VA_LINE_ALPHA
  ctx.strokeStyle = colors.valueArea
  ctx.setLineDash(valueAreaDash(hr))
  if (geom.vahY !== null) hLine(scope, geom.x0, geom.x1, geom.vahY, lineWidth)
  if (geom.valY !== null) hLine(scope, geom.x0, geom.x1, geom.valY, lineWidth)
  ctx.setLineDash(NO_DASH)
  if (geom.pocY !== null) {
    ctx.globalAlpha = 1
    ctx.strokeStyle = colors.poc
    hLine(scope, geom.x0, geom.x1, geom.pocY, lineWidth)
  }
  ctx.restore()
}

class ProfileRenderer implements IPrimitivePaneRenderer {
  geom: ProfileGeometry | null = null
  colors: VolumeProfileColors = DEFAULT_VOLUME_PROFILE_COLORS

  draw(target: RenderTarget): void {
    const geom = this.geom
    if (geom === null) return
    target.useBitmapCoordinateSpace((scope) => drawProfile(scope, geom, this.colors))
  }
}

class ProfilePaneView implements IPrimitivePaneView {
  readonly renderer_ = new ProfileRenderer()

  zOrder(): 'normal' {
    return 'normal'
  }

  renderer(): IPrimitivePaneRenderer {
    return this.renderer_
  }
}

type AttachedParam = Pick<SeriesAttachedParameter<Time>, 'chart' | 'series' | 'requestUpdate'>

/**
 * What the last frame's geometry was built from. `sig` covers scroll, bar spacing, pane width,
 * the y of the profile's lowest and highest prices and the primitive's `version`; `xFrom` / `xTo`
 * are the range edges themselves (their bar index can move without the view moving, e.g. a
 * no-trade edge bar snapping to a new neighbour) and `pocY` a third price, so a change of
 * price-scale mode that kept both extremes in place still rebuilds.
 */
interface FrameCache {
  sig: ViewSignature
  xFrom: number
  xTo: number
  pocY: number | null
  geom: ProfileGeometry | null
}

/** One indicator instance's profile on the main series. */
export class VolumeProfilePrimitive implements ISeriesPrimitive<Time> {
  readonly uid: string
  private profile: VolumeProfile | null = null
  /** `profileStats(profile)`, computed on `setProfile`. */
  private stats: ProfileStats | null = null
  private visible = true
  private chart: IChartApiBase<Time> | null = null
  private series: ISeriesApi<SeriesType, Time> | null = null
  private requestUpdate: (() => void) | null = null
  /** Bumped on every change the view signature cannot see: profile, colours, visibility, host. */
  private version = 0
  private frame: FrameCache | null = null
  private builds = 0
  private readonly yOfPrice = (price: number): number | null =>
    this.series === null ? null : this.series.priceToCoordinate(price)
  private readonly paneView = new ProfilePaneView()
  private readonly paneViewList: readonly IPrimitivePaneView[] = [this.paneView]

  constructor(uid: string, colors: VolumeProfileColors) {
    this.uid = uid
    this.paneView.renderer_.colors = colors
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
    this.paneView.renderer_.geom = null
    this.invalidate()
  }

  getProfile(): VolumeProfile | null {
    return this.profile
  }

  setProfile(profile: VolumeProfile | null): void {
    if (profile === this.profile) return
    this.profile = profile
    this.stats = profile !== null && profile.rows.length > 0 ? profileStats(profile) : null
    this.invalidate()
    this.requestUpdate?.()
  }

  setVisible(visible: boolean): void {
    if (visible === this.visible) return
    this.visible = visible
    this.invalidate()
    this.requestUpdate?.()
  }

  setColors(colors: VolumeProfileColors): void {
    this.paneView.renderer_.colors = colors
    this.invalidate()
    this.requestUpdate?.()
  }

  /** The geometry drawn on the last frame (null = nothing drawn). */
  geometry(): ProfileGeometry | null {
    return this.paneView.renderer_.geom
  }

  /** Geometry builds so far (tests: an unchanged view must not rebuild). */
  buildCount(): number {
    return this.builds
  }

  private invalidate(): void {
    this.version++
    this.frame = null
  }

  updateAllViews(): void {
    this.paneView.renderer_.geom = this.currentGeometry()
  }

  /**
   * The geometry for the current view: the last frame's when nothing it depends on moved,
   * else rebuilt. Culled (null) when wholly off-screen, decided from the range's horizontal
   * span before any row is placed.
   */
  private currentGeometry(): ProfileGeometry | null {
    const profile = this.profile
    const stats = this.stats
    const chart = this.chart
    const series = this.series
    if (!this.visible || profile === null || stats === null || chart === null || series === null)
      return null
    const ts = chart.timeScale()
    // Nearest bar: the chart plots traded bars only, so a range edge on a no-trade bar
    // snaps to its neighbour; logicalToCoordinate also places off-screen bars.
    const iFrom = ts.timeToIndex(profile.from as Time, true)
    const iTo = ts.timeToIndex(profile.to as Time, true)
    const xFrom = iFrom === null ? null : ts.logicalToCoordinate(iFrom as unknown as Logical)
    const xTo = iTo === null ? null : ts.logicalToCoordinate(iTo as unknown as Logical)
    if (xFrom === null || xTo === null) {
      this.frame = null
      return null
    }
    const rows = profile.rows
    const sig = viewSignature(
      chart as IChartApi,
      series,
      [rows[0].low, rows[rows.length - 1].high],
      this.version,
    )
    const pocY = series.priceToCoordinate(profile.poc)
    const last = this.frame
    if (
      last !== null &&
      sameViewSignature(last.sig, sig) &&
      last.xFrom === xFrom &&
      last.xTo === xTo &&
      last.pocY === pocY
    )
      return last.geom

    this.builds++
    const span = profileSpan(xFrom, xTo, sig !== null ? sig.barSpacing : barSpacingOf(ts))
    const width = sig !== null ? sig.width : ts.width()
    const geom = spanOffscreen(span, width)
      ? null
      : geometryInSpan(profile, span, this.yOfPrice, stats)
    this.frame = sig === null ? null : { sig, xFrom, xTo, pocY, geom }
    return geom
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return this.paneViewList
  }

  /** Includes the profile's price range while any of its bars is in the visible range. */
  autoscaleInfo(startTimePoint: Logical, endTimePoint: Logical): AutoscaleInfo | null {
    const profile = this.profile
    const chart = this.chart
    if (!this.visible || profile === null || chart === null || profile.rows.length === 0)
      return null
    const ts = chart.timeScale()
    const from = ts.timeToIndex(profile.from as Time, true)
    const to = ts.timeToIndex(profile.to as Time, true)
    if (from === null || to === null) return null
    if (endTimePoint < from || startTimePoint > to) return null
    return {
      priceRange: {
        minValue: profile.rows[0].low,
        maxValue: profile.rows[profile.rows.length - 1].high,
      },
    }
  }

  hitTest(x: number, y: number): PrimitiveHoveredItem | null {
    const geom = this.paneView.renderer_.geom
    if (geom === null || !hitProfile(geom, x, y)) return null
    return {
      externalId: `indicator:${this.uid}`,
      zOrder: 'normal',
      itemType: 'primitive',
      hitTestPriority: 0,
      distance: 0,
    }
  }
}

/* ------------------------------------------------------------------------------------------ */
/* Layer                                                                                       */
/* ------------------------------------------------------------------------------------------ */

/** The part of a series the layer uses (an `ISeriesApi` satisfies it). */
export type PrimitiveHost = Pick<ISeriesApi<SeriesType>, 'attachPrimitive' | 'detachPrimitive'>

interface Entry {
  primitive: VolumeProfilePrimitive
  cache: ProfileCache | null
}

/**
 * Every range indicator (a def with `profile`) drawn on the main series: one primitive per
 * instance. Usage from Chart: `attach(mainSeries)` when the main series is created and
 * `detach(alive)` before it is removed (chart-type switch), `sync(configs, appliedCandles)` when
 * the indicator configs or the candles change, `setColors` on theme change. `lineage` is the
 * chart's shared `BarLineage` (as for market structure and sessions): with it a live tick
 * checks the in-range bars without reading them.
 */
export class VolumeProfileLayer {
  private series: PrimitiveHost | null = null
  private readonly entries = new Map<string, Entry>()
  private colors: VolumeProfileColors
  private readonly lineage: BarLineage

  constructor(
    colors: VolumeProfileColors = DEFAULT_VOLUME_PROFILE_COLORS,
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
   * been removed, so only the references are dropped. Profiles stay cached for the next attach.
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

  sync(
    configs: readonly IndicatorConfig[],
    candles: readonly Candle[] | null,
    defs: ReadonlyMap<IndicatorId, IndicatorDef> = INDICATOR_BY_ID,
  ): void {
    const keep = new Set<string>()
    for (const config of configs) {
      const def = defs.get(config.type)
      if (def?.profile === undefined) continue
      keep.add(config.uid)
      let entry = this.entries.get(config.uid)
      if (entry === undefined) {
        entry = { primitive: new VolumeProfilePrimitive(config.uid, this.colors), cache: null }
        this.entries.set(config.uid, entry)
        this.series?.attachPrimitive(entry.primitive)
      }
      entry.primitive.setVisible(config.visible)
      // Hidden instances draw nothing and are not recomputed until shown again.
      if (!config.visible) continue
      entry.cache = nextProfileCache(
        entry.cache,
        def,
        config.inputs,
        candles,
        def.profile === volumeProfileDef.profile ? volumeProfileOpenOps : null,
        this.lineage,
      )
      entry.primitive.setProfile(entry.cache.profile)
    }
    for (const [uid, entry] of this.entries) {
      if (keep.has(uid)) continue
      this.series?.detachPrimitive(entry.primitive)
      this.entries.delete(uid)
    }
  }

  setColors(colors: VolumeProfileColors): void {
    const c = this.colors
    if (
      c.up === colors.up &&
      c.down === colors.down &&
      c.poc === colors.poc &&
      c.valueArea === colors.valueArea &&
      c.text === colors.text
    )
      return
    this.colors = { ...colors }
    for (const e of this.entries.values()) e.primitive.setColors(this.colors)
  }

  /** Uids with a primitive, in creation order (tests, debugging). */
  uids(): string[] {
    return [...this.entries.keys()]
  }

  /** The primitive of `uid` (tests, debugging). */
  primitiveOf(uid: string): VolumeProfilePrimitive | undefined {
    return this.entries.get(uid)?.primitive
  }
}
