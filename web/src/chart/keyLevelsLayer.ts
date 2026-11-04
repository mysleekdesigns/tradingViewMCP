// Key levels overlay (Phase 21) on the main series: visual only, no edge claim.
//
// Per instance of a key-levels indicator (a def with `keyLevels`, i.e. 'keylevels'):
// - a `KeyLevelsEngine` fed CLOSED bars only, one at a time: plotted (real, never Heikin-Ashi) bars
//   at or before `closedThrough`, so a tick on the forming bar pushes nothing and moves nothing.
//   `through`, the newest plotted bar, only says which UTC day the steps run to (in replay it is
//   the cursor bar, so no day past the cursor exists). A changed daily array, a change of the
//   params, a pushed bar that is no longer the same object or a `closedThrough` that moved back
//   starts over. The show toggles only change what is drawn. A hidden instance keeps no engine;
// - a series primitive with one thin horizontal segment per step, culled to the visible time range
//   by binary search, at zOrder 'bottom' (under the candles and under the drawings), and its right
//   edge labels at 'normal' (a label under the newest candles could not be read; the drawings
//   draw at 'top', so they stay above the labels too).
//
// A step only covers plotted bars of its own day or week: a day with no daily candle has no step,
// a day with no plotted bar draws nothing, and nothing ever bridges either kind of gap.
//
// Shown only on intervals below 1d.

import type { IPrimitivePaneView } from 'lightweight-charts'
import { INDICATOR_BY_ID } from '../indicators/registry'
import {
  KEY_LEVEL_LABELS,
  KeyLevelsEngine,
  keyLevelsParams,
  keyLevelsParamsKey,
  type KeyLevelsParams,
} from '../indicators/keyLevels'
import type {
  IndicatorDef,
  IndicatorId,
  IndicatorInputs,
  KeyLevelDay,
  KeyLevelKey,
  KeyLevels,
  KeyLevelStep,
} from '../indicators/types'
import type { IndicatorConfig } from '../state/indicators'
import type { Candle } from '../types'
import {
  CulledLayerPrimitive,
  LayerHost,
  LayerPaneView,
  LayerRenderer,
  NO_DASH,
  clampX,
  type BitmapScope,
  type ChartCoords,
} from './layers/base'
import { BarLineage, closedBarCount, pushNewBars, type BarEngine } from './structureBars'
import { fontFamily, withHexAlpha, type ChartPalette } from './theme'
import { isLightBackground } from './util/color'
import { lastIndexAtOrBeforeTime, lowerBound, lowerBoundByTime } from './util/search'

/** Intervals at or above this many seconds (1d) draw no levels and load no daily candles. */
export const KEY_LEVELS_MAX_INTERVAL_SECONDS = 86_400

const DAY = 86_400
/** A step lasts at most a week (the culling window). */
const MAX_STEP_SECONDS = 7 * DAY

/** Daily candles are needed: a visible key-levels instance below 1d, with the venue known. */
export function keyLevelsDailyEnabled(
  configs: readonly IndicatorConfig[],
  intervalSeconds: number,
  venuePending: boolean,
  defs: ReadonlyMap<IndicatorId, IndicatorDef> = INDICATOR_BY_ID,
): boolean {
  if (venuePending || intervalSeconds >= KEY_LEVELS_MAX_INTERVAL_SECONDS) return false
  return configs.some((c) => c.visible && defs.get(c.type)?.keyLevels !== undefined)
}

/* ------------------------------------------------------------------------------------------ */
/* Colours and toggles                                                                         */
/* ------------------------------------------------------------------------------------------ */

export interface KeyLevelsColors {
  /** PDH / PDL: the chart's text colour, drawn thin and translucent. */
  daily: string
  /** PWH / PWL: the muted text colour, dashed. */
  weekly: string
  /** Behind a label, so it reads over the candles. */
  plate: string
}

/** Neutral palette colours: levels are not signals, so never the up / down hues. */
export function keyLevelsColorsFor(
  palette: Pick<ChartPalette, 'background' | 'text' | 'mutedText'>,
): KeyLevelsColors {
  const alpha = isLightBackground(palette.background) ? 0.8 : 0.72
  return {
    daily: palette.text,
    weekly: palette.mutedText,
    plate: withHexAlpha(palette.background, alpha),
  }
}

export const DEFAULT_KEY_LEVELS_COLORS: Readonly<KeyLevelsColors> = Object.freeze(
  keyLevelsColorsFor({ background: '#131722', text: '#b2b5be', mutedText: '#787b86' }),
)

/** What is drawn and shown (display only, except that `weekly` is also a compute param). */
export interface KeyLevelsShow {
  daily: boolean
  weekly: boolean
  range: boolean
  bias: boolean
}

/** Toggle inputs are 1 (on) or 0 (off); a missing key takes the def's default. */
export function keyLevelsShow(inputs: IndicatorInputs): KeyLevelsShow {
  return {
    daily: (inputs.showDaily ?? 1) === 1,
    weekly: inputs.showWeekly === 1,
    range: (inputs.showRange ?? 1) === 1,
    bias: (inputs.showBias ?? 1) === 1,
  }
}

function keyShown(key: KeyLevelKey, show: KeyLevelsShow): boolean {
  return key === 'pdh' || key === 'pdl' ? show.daily : show.weekly
}

/* ------------------------------------------------------------------------------------------ */
/* Geometry (pure, media / CSS pixels)                                                         */
/* ------------------------------------------------------------------------------------------ */

/** What one instance's primitive draws. The same object until a closed bar or a new bar. */
export interface KeyLevelsView {
  steps: readonly KeyLevelStep[]
  /** Plotted bars: only their open times are read, so an older copy of the array is fine. */
  bars: readonly Candle[]
  show: KeyLevelsShow
  version: number
}

/** Chart conversions the geometry needs; LWC or a test fake provides them. */
export interface KeyLevelCoords {
  /** Open times of the first and last plotted bar in view, or null without data. */
  visibleRange(): { from: number; to: number } | null
  /** x of the centre of the plotted bar opening at `time` (null if no such bar). */
  xOfTime(time: number): number | null
  /** y of `price` on the main series' scale. */
  yOfPrice(price: number): number | null
  /** Distance between neighbouring bar centres (CSS px). */
  barSpacing: number
  /** Pane width (CSS px), which excludes the price scale. */
  width: number
}

export interface KeyLevelSegment {
  key: KeyLevelKey
  x0: number
  x1: number
  y: number
}

export interface KeyLevelLabel {
  key: KeyLevelKey
  text: string
  /** Right edge of the plate; inside the pane, so never under the price scale. */
  x: number
  /** Vertical centre of the plate. */
  y: number
  width: number
}

export interface KeyLevelsGeometry {
  segments: KeyLevelSegment[]
  labels: KeyLevelLabel[]
}

const KEYS: readonly KeyLevelKey[] = ['pdh', 'pdl', 'pwh', 'pwl']

export const LABEL_FONT_PX = 10
/** Plate height, which is also the closest two label centres may be. */
export const LABEL_HEIGHT_PX = 14
const LABEL_RIGHT_PX = 4
const LABEL_PAD_PX = 3
/** Rough width of one label character at LABEL_FONT_PX (no measureText in the pure part). */
const LABEL_CHAR_PX = 6

/** Thousands separators on the integer part only: "81196.01" -> "81,196.01". */
export function groupThousands(price: string): string {
  const dot = price.indexOf('.')
  const whole = dot < 0 ? price : price.slice(0, dot)
  const rest = dot < 0 ? '' : price.slice(dot)
  return whole.replace(/\B(?=(\d{3})+$)/g, ',') + rest
}

/**
 * Pushes labels apart, top to bottom, so no two plates touch: order by y, then each label moves
 * down just enough to clear the one above. Sorts `labels` in place.
 */
export function spreadLabels(labels: KeyLevelLabel[]): void {
  labels.sort((a, b) => a.y - b.y)
  for (let i = 1; i < labels.length; i++) {
    const min = labels[i - 1].y + LABEL_HEIGHT_PX
    if (labels[i].y < min) labels[i].y = min
  }
}

/**
 * The shown steps in the visible time range, in pane coordinates: from the left edge of the step's
 * first plotted bar to the right edge of its last one (a step with no plotted bar draws nothing),
 * plus a label at the pane's right edge for each key's step that covers the newest visible bar. On
 * a gap there is no such step and no label: an older level at the right edge would read as today's.
 */
export function keyLevelGeometry(
  view: KeyLevelsView,
  coords: KeyLevelCoords,
  format: (price: number) => string,
): KeyLevelsGeometry | null {
  const range = coords.visibleRange()
  if (range === null) return null
  const { from, to } = range
  const { steps, bars, show } = view
  const half = coords.barSpacing / 2
  const width = coords.width
  const segments: KeyLevelSegment[] = []
  const latest: Partial<Record<KeyLevelKey, KeyLevelStep>> = {}
  // A step ends within a week of its start, so none starting earlier than this reaches `from`.
  const first = lowerBound(steps.length, (i) => steps[i].start, from - MAX_STEP_SECONDS)
  for (let i = first; i < steps.length && steps[i].start <= to; i++) {
    const s = steps[i]
    if (s.end <= from || !keyShown(s.key, show)) continue
    const a = lowerBoundByTime(bars, s.start)
    const b = lowerBoundByTime(bars, s.end) - 1
    if (a > b) continue
    const xa = coords.xOfTime(bars[a].time)
    const xb = coords.xOfTime(bars[b].time)
    const y = coords.yOfPrice(s.price)
    if (xa === null || xb === null || y === null) continue
    segments.push({ key: s.key, x0: clampX(xa - half, width), x1: clampX(xb + half, width), y })
    latest[s.key] = s
  }
  const labels: KeyLevelLabel[] = []
  const newest = bars[lowerBoundByTime(bars, to + 1) - 1]?.time
  for (const key of KEYS) {
    const s = latest[key]
    if (s === undefined || newest === undefined || newest < s.start || newest >= s.end) continue
    const y = coords.yOfPrice(s.price)
    if (y === null) continue
    const text = `${KEY_LEVEL_LABELS[key]} ${format(s.price)}`
    labels.push({
      key,
      text,
      x: width - LABEL_RIGHT_PX,
      y,
      width: text.length * LABEL_CHAR_PX + 2 * LABEL_PAD_PX,
    })
  }
  spreadLabels(labels)
  return { segments, labels }
}

/* ------------------------------------------------------------------------------------------ */
/* Renderers + primitive                                                                       */
/* ------------------------------------------------------------------------------------------ */

/** Line opacity: thin and quiet, the candles stay the subject. */
export const DAILY_LINE_ALPHA = 0.6
export const WEEKLY_LINE_ALPHA = 0.8

/** One path per family: every daily segment, then every weekly one. */
function strokeFamily(
  scope: BitmapScope,
  segments: readonly KeyLevelSegment[],
  daily: boolean,
  offset: number,
): void {
  const ctx = scope.context
  const hr = scope.horizontalPixelRatio
  const vr = scope.verticalPixelRatio
  ctx.beginPath()
  for (const s of segments) {
    if ((s.key === 'pdh' || s.key === 'pdl') !== daily) continue
    const y = Math.round(s.y * vr) + offset
    ctx.moveTo(Math.round(s.x0 * hr), y)
    ctx.lineTo(Math.round(s.x1 * hr), y)
  }
  ctx.stroke()
}

/** Draws the level lines in bitmap space; exported for tests with a fake canvas. */
export function drawKeyLevelLines(
  scope: BitmapScope,
  geom: KeyLevelsGeometry,
  colors: KeyLevelsColors,
): void {
  const { segments } = geom
  if (segments.length === 0) return
  const ctx = scope.context
  const hr = scope.horizontalPixelRatio
  const lineWidth = Math.max(1, Math.floor(hr))
  const offset = lineWidth % 2 ? 0.5 : 0
  ctx.save()
  ctx.lineWidth = lineWidth
  ctx.setLineDash(NO_DASH)
  ctx.globalAlpha = DAILY_LINE_ALPHA
  ctx.strokeStyle = colors.daily
  strokeFamily(scope, segments, true, offset)
  ctx.setLineDash([Math.round(5 * hr), Math.round(4 * hr)])
  ctx.globalAlpha = WEEKLY_LINE_ALPHA
  ctx.strokeStyle = colors.weekly
  strokeFamily(scope, segments, false, offset)
  ctx.restore()
}

/** Draws the right-edge labels in bitmap space; exported for tests with a fake canvas. */
export function drawKeyLevelLabels(
  scope: BitmapScope,
  geom: KeyLevelsGeometry,
  colors: KeyLevelsColors,
): void {
  const { labels } = geom
  if (labels.length === 0) return
  const ctx = scope.context
  const hr = scope.horizontalPixelRatio
  const vr = scope.verticalPixelRatio
  const height = Math.round(LABEL_HEIGHT_PX * vr) - Math.round(vr)
  ctx.save()
  ctx.globalAlpha = 1
  ctx.font = `${Math.round(LABEL_FONT_PX * vr)}px ${fontFamily}`
  ctx.textAlign = 'right'
  ctx.textBaseline = 'middle'
  for (const l of labels) {
    const right = Math.round(l.x * hr)
    const width = Math.round(l.width * hr)
    const y = Math.round(l.y * vr)
    ctx.fillStyle = colors.plate
    ctx.fillRect(right - width, y - Math.round(height / 2), width, height)
    ctx.fillStyle = l.key === 'pdh' || l.key === 'pdl' ? colors.daily : colors.weekly
    ctx.fillText(l.text, right - Math.round(LABEL_PAD_PX * hr), y)
  }
  ctx.restore()
}

function noLines(geom: KeyLevelsGeometry): boolean {
  return geom.segments.length === 0
}

function noLabels(geom: KeyLevelsGeometry): boolean {
  return geom.labels.length === 0
}

/**
 * One instance's levels on the main series. Two pane views over one geometry: the lines under the
 * candles and the labels over them. No `autoscaleInfo` (an old level never stretches the price
 * scale) and no hit test (it never takes clicks from the candles or the drawings). The geometry is
 * cached behind the view signature (see `CulledLayerPrimitive`).
 */
export class KeyLevelsPrimitive extends CulledLayerPrimitive<
  KeyLevelsView,
  KeyLevelsGeometry,
  KeyLevelsColors
> {
  private readonly labelRenderer: LayerRenderer<KeyLevelsGeometry, KeyLevelsColors>
  private readonly views: readonly IPrimitivePaneView[]

  constructor(uid: string, colors: KeyLevelsColors) {
    super(uid, {
      colors,
      paint: drawKeyLevelLines,
      isEmpty: noLines,
      zOrder: 'bottom',
      snap: 'exact',
    })
    this.labelRenderer = new LayerRenderer(colors, drawKeyLevelLabels, noLabels)
    this.views = [...super.paneViews(), new LayerPaneView(this.labelRenderer, 'normal')]
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return this.views
  }

  override detached(): void {
    super.detached()
    this.labelRenderer.geom = null
  }

  override setColors(colors: KeyLevelsColors): void {
    this.labelRenderer.colors = colors
    super.setColors(colors)
  }

  override updateAllViews(): void {
    super.updateAllViews()
    this.labelRenderer.geom = this.renderer_.geom
  }

  protected build(view: KeyLevelsView, coords: ChartCoords): KeyLevelsGeometry | null {
    const series = this.series
    if (series === null) return null
    const formatter = series.priceFormatter()
    return keyLevelGeometry(view, coords, (p) => groupThousands(formatter.format(p)))
  }
}

/* ------------------------------------------------------------------------------------------ */
/* Legend reads                                                                                */
/* ------------------------------------------------------------------------------------------ */

/** What the legend shows for one bar. */
export interface KeyLevelsLegend {
  /** Percent of the daily ATR used by that bar's UTC day so far, or null when unknown. */
  rangeUsed: number | null
  bias: KeyLevelDay['bias']
  smaPeriod: number
  show: KeyLevelsShow
}

function dayStart(time: number): number {
  return Math.floor(time / DAY) * DAY
}

/**
 * Range used and bias as of bar `time`. Range used is the newest closed point at or before it, of
 * its own UTC day only (a forming bar reads the closed bars of its day; a day with no closed bar
 * yet, or no ATR, has none). Bias is its UTC day's entry.
 */
export function keyLevelsAt(
  levels: KeyLevels,
  time: number,
): Pick<KeyLevelsLegend, 'rangeUsed' | 'bias'> {
  const day = dayStart(time)
  const point = levels.rangeUsed[lastIndexAtOrBeforeTime(levels.rangeUsed, time)]
  const days = levels.days
  const entry = days[lowerBound(days.length, (i) => days[i].day, day)]
  return {
    rangeUsed: point !== undefined && point.time >= day ? point.value : null,
    bias: entry !== undefined && entry.day === day ? entry.bias : null,
  }
}

/* ------------------------------------------------------------------------------------------ */
/* Layer                                                                                       */
/* ------------------------------------------------------------------------------------------ */

/** `KeyLevelsEngine` with the bar count `pushNewBars` reads. */
class CountedEngine implements BarEngine {
  count = 0
  readonly engine: KeyLevelsEngine

  constructor(daily: readonly Candle[], params: KeyLevelsParams) {
    this.engine = new KeyLevelsEngine(daily, params)
  }

  push(bar: Candle): void {
    this.engine.push(bar)
    this.count += 1
  }
}

interface DerivedInputs {
  inputs: IndicatorInputs
  params: KeyLevelsParams
  key: string
  show: KeyLevelsShow
}

interface Entry {
  primitive: KeyLevelsPrimitive
  engine: CountedEngine | null
  paramsKey: string
  /** Derived from `inputs` once per inputs object (immutable, replaced on edit), not per tick. */
  derived: DerivedInputs | null
  daily: readonly Candle[] | null
  /** The bars array the engine was last brought up to date with (its first `count` were pushed). */
  synced: readonly Candle[] | null
  levels: KeyLevels | null
  /** UTC day `levels` runs through. */
  throughDay: number | null
  /** Length and newest time of the bars the view holds (a new bar lengthens the last step). */
  barCount: number
  lastTime: number | null
  version: number
  view: KeyLevelsView | null
  recomputes: number
  pushed: number
}

/** The colour fields whose change is a new theme for the layer. */
const COLOR_KEYS: readonly (keyof KeyLevelsColors)[] = ['daily', 'weekly', 'plate']

function derivedInputs(entry: Entry, inputs: IndicatorInputs): DerivedInputs {
  const d = entry.derived
  if (d !== null && d.inputs === inputs) return d
  const params = keyLevelsParams(inputs)
  const next = { inputs, params, key: keyLevelsParamsKey(params), show: keyLevelsShow(inputs) }
  entry.derived = next
  return next
}

/** Nothing to draw or read: no engine and no reference to the bars or the daily candles. */
function clearEntry(entry: Entry): void {
  entry.engine = null
  entry.daily = null
  entry.synced = null
  entry.levels = null
  entry.throughDay = null
  entry.barCount = 0
  entry.lastTime = null
  entry.view = null
  entry.primitive.setView(null)
}

/** Day `day`'s levels need the candle of the day before, which `daily` does not reach. */
function missingLastDay(daily: readonly Candle[], day: number): boolean {
  const newest = daily[daily.length - 1]
  return newest === undefined || newest.time < day - DAY
}

/**
 * Every key-levels instance drawn on the main series. Usage from Chart: `attach(series)` when the
 * main series is created and `detach(alive)` before it is removed, `sync(...)` when the configs,
 * candles, closedThrough, interval or daily candles change, `setColors` on theme change. The
 * indicator legend reads `legendAt` and `dailyFailed`; `legendVersion` moves when they may answer
 * differently, which a forming-bar tick never does.
 */
export class KeyLevelsLayer extends LayerHost<KeyLevelsColors, KeyLevelsPrimitive, Entry> {
  /** The chart's record of which bars a tick kept (skips the identity scan on live ticks). */
  private readonly lineage: BarLineage
  private failed = false
  private legendV = 0

  constructor(
    colors: KeyLevelsColors = DEFAULT_KEY_LEVELS_COLORS,
    lineage: BarLineage = new BarLineage(),
  ) {
    super(colors, COLOR_KEYS)
    this.lineage = lineage
  }

  /**
   * `candles` are the plotted (real, not Heikin-Ashi) bars, the last of which may be forming. Only
   * those at or before `closedThrough` (null = none) are pushed. `daily` holds closed 1d candles
   * (null = none known yet). Good candles beside `dailyError` keep drawing; it is flagged when there
   * are none, or when they stop short of the previous day of the newest bar. `intervalSeconds` >= 1d
   * draws nothing and computes nothing.
   */
  sync(
    configs: readonly IndicatorConfig[],
    candles: readonly Candle[] | null,
    closedThrough: number | null,
    intervalSeconds: number,
    daily: readonly Candle[] | null,
    dailyError: string | null = null,
    defs: ReadonlyMap<IndicatorId, IndicatorDef> = INDICATOR_BY_ID,
  ): void {
    const bars = candles ?? []
    const intraday = intervalSeconds < KEY_LEVELS_MAX_INTERVAL_SECONDS
    const closedLen = intraday ? closedBarCount(bars, closedThrough) : 0
    const prefix = this.lineage.prefixChecks(bars, closedLen)
    const newest = bars.length > 0 ? bars[bars.length - 1].time : null
    const throughDay = newest === null ? null : dayStart(newest)
    const failed =
      intraday &&
      dailyError !== null &&
      (daily === null || (throughDay !== null && missingLastDay(daily, throughDay)))
    if (failed !== this.failed) {
      this.failed = failed
      this.legendV += 1
    }
    const keep = new Set<string>()
    for (const config of configs) {
      const def = defs.get(config.type)
      if (def?.keyLevels === undefined) continue
      keep.add(config.uid)
      let entry = this.entries.get(config.uid)
      if (entry === undefined) {
        entry = this.add(config.uid, {
          primitive: new KeyLevelsPrimitive(config.uid, this.colors),
          engine: null,
          paramsKey: '',
          derived: null,
          daily: null,
          synced: null,
          levels: null,
          throughDay: null,
          barCount: 0,
          lastTime: null,
          version: 0,
          view: null,
          recomputes: 0,
          pushed: 0,
        })
        this.legendV += 1
      }
      entry.primitive.setVisible(config.visible)
      if (!config.visible || !intraday || daily === null) {
        if (entry.levels !== null) this.legendV += 1
        clearEntry(entry)
        continue
      }

      const derived = derivedInputs(entry, config.inputs)
      let changed = false
      if (
        entry.engine === null ||
        entry.paramsKey !== derived.key ||
        entry.daily !== daily ||
        !prefix.keeps(entry.synced, entry.engine.count)
      ) {
        entry.engine = new CountedEngine(daily, derived.params)
        entry.paramsKey = derived.key
        entry.daily = daily
        entry.recomputes += 1
        changed = true
      }
      const added = pushNewBars(entry.engine, bars, closedLen)
      entry.pushed += added
      entry.synced = bars
      if (changed || added > 0 || throughDay !== entry.throughDay) {
        entry.levels = entry.engine.engine.result(newest)
        entry.throughDay = throughDay
        entry.version += 1
        this.legendV += 1
      } else if (bars.length !== entry.barCount || newest !== entry.lastTime) {
        entry.version += 1
      }
      entry.barCount = bars.length
      entry.lastTime = newest

      const v = entry.view
      if (v === null || v.version !== entry.version || v.show !== derived.show) {
        if (v !== null && v.show !== derived.show) this.legendV += 1
        const steps = entry.levels?.steps ?? []
        entry.view = { steps, bars, show: derived.show, version: entry.version }
      }
      entry.primitive.setView(entry.view)
    }
    this.sweep(keep)
  }

  /** Moves when `legendAt` or `dailyFailed` may answer differently. */
  legendVersion(): number {
    return this.legendV
  }

  /** The daily candles failed, or stop short of the newest bar's previous day: the legend notes it. */
  dailyFailed(): boolean {
    return this.failed
  }

  /** The legend of `uid` at bar `time` (null while hidden, loading or on 1d and above). */
  legendAt(uid: string, time: number | undefined): KeyLevelsLegend | null {
    const e = this.entries.get(uid)
    if (e === undefined || e.levels === null || e.derived === null) return null
    const at = time === undefined ? { rangeUsed: null, bias: null } : keyLevelsAt(e.levels, time)
    return { ...at, smaPeriod: e.derived.params.smaPeriod, show: e.derived.show }
  }

  /** The current levels of `uid` (null while hidden, on 1d+, or before its first daily load). */
  resultOf(uid: string): KeyLevels | null {
    return this.entries.get(uid)?.levels ?? null
  }

  /** Full recomputes and bars pushed so far for `uid` (tests). */
  statsOf(uid: string): { recomputes: number; pushed: number; count: number } | null {
    const e = this.entries.get(uid)
    if (e === undefined) return null
    return { recomputes: e.recomputes, pushed: e.pushed, count: e.engine?.count ?? 0 }
  }
}
