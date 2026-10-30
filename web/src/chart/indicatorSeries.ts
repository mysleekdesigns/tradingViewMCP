import {
  HistogramSeries,
  LineSeries,
  type AutoscaleInfo,
  type HistogramData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type LineData,
  type LineWidth,
  type SeriesType,
  type UTCTimestamp,
} from 'lightweight-charts'
import { INDICATOR_BY_ID } from '../indicators/registry'
import { IndicatorRunner } from '../indicators/runner'
import { VOLUME_PROFILE_HINDSIGHT } from '../indicators/volumeProfile'
import type {
  IndicatorDef,
  IndicatorId,
  IndicatorOutput,
  IndicatorPoint,
  IndicatorValues,
  LineDef,
  Placement,
  SessionKey,
} from '../indicators/types'
import type { IndicatorConfig } from '../state/indicators'
import type { Candle } from '../types'
import { formatPrice, formatVolume, selectLegendIndex } from './legend'
import type { SeriesSyncPlan } from './seriesSync'
import { SESSION_MAX_INTERVAL_SECONDS, sessionsHiddenByAlignment } from './sessionRangesLayer'
import {
  colors,
  guideLineOptions,
  histogramColors,
  indicatorHistogramBase,
  indicatorLineBase,
  indicatorPaneScaleMargins,
  indicatorPaneStretch,
  mainPaneStretch,
  type ChartPalette,
} from './theme'

/** The theme colours indicator series use (their line colours are the user's and stay). */
export type IndicatorPalette = Pick<ChartPalette, 'background' | 'mutedText'>

type Defs = ReadonlyMap<IndicatorId, IndicatorDef>

/* ------------------------------------------------------------------------------------------ */
/* Pure helpers                                                                                */
/* ------------------------------------------------------------------------------------------ */

/** What has to happen to the chart to go from `prev` to `next` indicator configs. */
export interface IndicatorDiff {
  /** New uids (or a uid whose type changed), in `next` order. */
  create: string[]
  /** uids that disappeared (or whose type changed). */
  remove: string[]
  /** Same uid, new inputs or source: new runner and full recompute. */
  recompute: string[]
  /** Same uid, at least one line colour changed: applyOptions only, no recompute. */
  recolor: string[]
  /** Same uid, `visible` flipped. */
  visibility: string[]
  /**
   * The ordered list of pane-indicator uids changed (add / remove / reorder / type change).
   * The manager then tears down and rebuilds every pane indicator's series so panes 1..n
   * follow `next` order.
   */
  rebuildPanes: boolean
}

function sameNumbers(a: Readonly<Record<string, number>>, b: Readonly<Record<string, number>>) {
  const ka = Object.keys(a)
  if (ka.length !== Object.keys(b).length) return false
  return ka.every((k) => Object.is(a[k], b[k]))
}

function sameStrings(a: Readonly<Record<string, string>>, b: Readonly<Record<string, string>>) {
  const ka = Object.keys(a)
  if (ka.length !== Object.keys(b).length) return false
  return ka.every((k) => a[k]?.toLowerCase() === b[k]?.toLowerCase())
}

/**
 * True when an indicator gets its own pane: pane placement and at least one line. A def with no
 * lines (a range indicator such as the fixed-range volume profile, drawn by a primitive) never
 * creates series, so it must not take a pane index either.
 */
export function ownsPane(def: IndicatorDef | undefined): boolean {
  return def !== undefined && def.placement === 'pane' && def.lines.length > 0
}

/** uids of indicators with their own pane, in order. Unknown types are skipped. */
export function paneIndicatorUids(
  configs: readonly IndicatorConfig[],
  defs: Defs = INDICATOR_BY_ID,
) {
  return configs.filter((c) => ownsPane(defs.get(c.type))).map((c) => c.uid)
}

export function diffIndicators(
  prev: readonly IndicatorConfig[],
  next: readonly IndicatorConfig[],
  defs: Defs = INDICATOR_BY_ID,
): IndicatorDiff {
  const diff: IndicatorDiff = {
    create: [],
    remove: [],
    recompute: [],
    recolor: [],
    visibility: [],
    rebuildPanes: false,
  }
  const before = new Map(prev.map((c) => [c.uid, c]))
  const after = new Set(next.map((c) => c.uid))
  for (const c of prev) if (!after.has(c.uid)) diff.remove.push(c.uid)
  let typeChangedInPane = false
  for (const c of next) {
    if (!defs.has(c.type)) continue
    const old = before.get(c.uid)
    if (old === undefined) {
      diff.create.push(c.uid)
      continue
    }
    if (old.type !== c.type) {
      diff.remove.push(c.uid)
      diff.create.push(c.uid)
      if (ownsPane(defs.get(old.type)) || ownsPane(defs.get(c.type))) {
        typeChangedInPane = true
      }
      continue
    }
    if (old.source !== c.source || !sameNumbers(old.inputs, c.inputs)) diff.recompute.push(c.uid)
    if (!sameStrings(old.colors, c.colors)) diff.recolor.push(c.uid)
    if (old.visible !== c.visible) diff.visibility.push(c.uid)
  }
  const a = paneIndicatorUids(prev, defs)
  const b = paneIndicatorUids(next, defs)
  diff.rebuildPanes = typeChangedInPane || a.length !== b.length || a.some((uid, i) => uid !== b[i])
  return diff
}

/**
 * Pane index per uid: overlay and volume indicators share the price pane (0); every pane
 * indicator with lines gets its own pane 1..n in config order (a lineless one stays on 0).
 */
export function assignPanes(
  configs: readonly IndicatorConfig[],
  defs: Defs = INDICATOR_BY_ID,
): Map<string, number> {
  const panes = new Map<string, number>()
  let next = 1
  for (const c of configs) {
    const def = defs.get(c.type)
    if (def === undefined) continue
    panes.set(c.uid, ownsPane(def) ? next++ : 0)
  }
  return panes
}

/**
 * TradingView MACD histogram colour. Rising means value >= previous bar's value (the first bar
 * counts as rising). `positive` is the user's histogram colour and replaces the strong positive
 * colour; with a custom colour the faded positive colour is that colour at 50% alpha.
 */
export function histogramColor(
  value: number,
  prev: number | undefined,
  positive: string = histogramColors.positiveRising,
): string {
  const rising = prev === undefined || value >= prev
  if (value >= 0) {
    if (rising) return positive
    return positive.toLowerCase() === histogramColors.positiveRising
      ? histogramColors.positiveFalling
      : `${positive.slice(0, 7)}80`
  }
  return rising ? histogramColors.negativeRising : histogramColors.negativeFalling
}

/** Input numbers without trailing zeros or float noise: 2 -> "2", 0.5 -> "0.5". */
export function formatInputNumber(n: number): string {
  if (!Number.isFinite(n)) return String(n)
  return String(Number(n.toPrecision(12)))
}

/** A time input as compact UTC, e.g. 1_700_000_040 -> "2023-11-14 22:14". */
export function formatTimeInput(t: number): string {
  return new Date(t * 1000).toISOString().slice(0, 16).replace('T', ' ')
}

/**
 * Legend title, e.g. "EMA 20 close", "BB 20 2 close", "MACD 12 26 9 close", "VWAP",
 * "AVWAP 2023-11-14 22:14 hlc3". Time inputs show as UTC when set (> 0) and are left out when
 * not set, so two anchored instances can be told apart.
 */
export function indicatorTitle(
  def: IndicatorDef,
  config: Pick<IndicatorConfig, 'inputs' | 'source'>,
): string {
  const parts = [def.shortName]
  for (const input of def.inputs) {
    // On/off toggles (market structure "Show" parts) are not part of the title.
    if (input.kind === 'toggle') continue
    const v = config.inputs[input.key] ?? input.default
    if (input.kind === 'time') {
      if (Number.isFinite(v) && v > 0) parts.push(formatTimeInput(v))
      continue
    }
    parts.push(formatInputNumber(v))
  }
  if (def.source !== null) parts.push(config.source)
  return parts.join(' ')
}

/**
 * Legend tooltip for event indicators (market structure): a drawing aid, not a signal, and never
 * back-dated (a swing or event shows only once the bar that confirms it has closed).
 */
export const MARKET_STRUCTURE_HINT =
  'Visual only, no edge claim. Swings and events appear only after the bar that confirms them closes'

/**
 * Legend tooltip for session indicators (session ranges): a drawing aid, not a signal; boxes use
 * closed bars only and are drawn only on intervals below 1D. A finished box is the whole
 * session's range, which was only known once its last bar closed (hindsight on history).
 */
export const SESSION_RANGES_HINT =
  'Visual only, no edge claim. UTC session high/low from closed bars only; shown below 1D. ' +
  "A finished box shows the whole session's range, known only once its last bar closed"

const SESSION_NOTE_NAMES: Readonly<Record<SessionKey, string>> = {
  asia: 'Asia',
  london: 'London',
  newyork: 'New York',
  opening: 'opening range',
}

/** Legend note for session ranges on 1D and above, where nothing is drawn. */
export const SESSION_DAILY_NOTE = 'Not shown on 1D and above'

/**
 * Legend note when session ranges draws less than it could: nothing on 1D and above
 * (`SESSION_DAILY_NOTE`), or toggled-on sessions hidden because their hours do not line up with
 * the bar length (e.g. London, New York and the 1h opening range on 4h). Undefined when nothing
 * is hidden, or the interval is unknown.
 */
export function sessionAlignmentNote(
  def: IndicatorDef,
  config: Pick<IndicatorConfig, 'inputs'>,
  intervalSeconds: number | undefined,
): string | undefined {
  if (def.sessions === undefined || intervalSeconds === undefined) return undefined
  if (intervalSeconds >= SESSION_MAX_INTERVAL_SECONDS) return SESSION_DAILY_NOTE
  const hidden = sessionsHiddenByAlignment(config.inputs, intervalSeconds)
  if (hidden.length === 0) return undefined
  const names = hidden.map((k) => SESSION_NOTE_NAMES[k])
  const list =
    names.length === 1
      ? names[0]
      : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
  return `${list} hidden: hours don't line up with the bar length`
}

export const ANCHOR_NOT_SET_NOTE = 'Set the anchor in Settings'
export const ANCHOR_NOT_LOADED_NOTE =
  'Anchor is before the loaded history. Scroll left to load older bars, if the market has them.'

/**
 * Why an anchored indicator (a def with a 'time' input; its first one is the anchor, e.g.
 * AVWAP `anchor`, VP `from`) draws nothing: the anchor is not set, or it is older than the first
 * plotted bar (`firstTime`), so its bar is not loaded. Undefined otherwise.
 */
export function anchorNote(
  def: IndicatorDef,
  config: Pick<IndicatorConfig, 'inputs'>,
  firstTime: number | undefined,
): string | undefined {
  const input = def.inputs.find((i) => i.kind === 'time')
  if (input === undefined) return undefined
  const anchor = config.inputs[input.key] ?? input.default
  if (!(anchor > 0)) return ANCHOR_NOT_SET_NOTE
  if (firstTime !== undefined && anchor < firstTime) return ANCHOR_NOT_LOADED_NOTE
  return undefined
}

/** Volume MA values read like volume (1.23K); everything else like a price. */
export function formatIndicatorValue(value: number | undefined, placement: Placement): string {
  if (value === undefined) return '—'
  return placement === 'volume' ? formatVolume(value) : formatPrice(value)
}

/** Price levels drawn as guides in a pane: the def's guides plus a zero line under histograms. */
export function paneGuideValues(def: IndicatorDef): number[] {
  if (def.placement !== 'pane') return []
  const values = def.guides.map((g) => g.value)
  if (def.lines.some((l) => l.kind === 'histogram') && !values.includes(0)) values.push(0)
  return values
}

/** Colour of one line: the user's choice, else the def default. */
export function lineColor(config: Pick<IndicatorConfig, 'colors'>, line: LineDef): string {
  return config.colors[line.key] ?? line.color
}

/**
 * Price format for indicator series. Volume MA formats like volume. Unbounded pane indicators
 * (MACD) take the precision `formatPrice` uses for the last close, so low-priced coins do not
 * show 0.00 everywhere. Bounded ones (RSI, Stoch) and overlays keep the default (2 decimals;
 * overlays share the main price scale).
 */
export type IndicatorPriceFormat =
  { type: 'volume' } | { type: 'price'; precision: number; minMove: number }

export function indicatorPriceFormat(
  def: IndicatorDef,
  lastClose: number | undefined,
): IndicatorPriceFormat | undefined {
  if (def.placement === 'volume') return { type: 'volume' }
  if (def.placement !== 'pane' || def.range !== undefined || lastClose === undefined) {
    return undefined
  }
  const abs = Math.abs(lastClose)
  const precision = abs >= 1 || abs === 0 ? 2 : abs >= 0.01 ? 4 : 8
  return { type: 'price', precision, minMove: 10 ** -precision }
}

export interface IndicatorLegendValue {
  key: string
  label: string
  text: string
  color: string
}

export interface IndicatorLegendRow {
  uid: string
  title: string
  placement: Placement
  /** 0 for overlay/volume rows (main legend), 1..n for pane indicators. */
  pane: number
  visible: boolean
  values: IndicatorLegendValue[]
  /** Why the indicator draws nothing (anchor not set / not loaded); shown inside the row. */
  note?: string
  /**
   * Tooltip for the row (range indicators: the hindsight caveat; market structure and session
   * ranges: visual only).
   */
  hint?: string
}

/**
 * One legend row. `values` are the line values at the shown bar; `prevValues` those at the bar
 * before (only used to colour a histogram value like its bar). `firstTime` is the open time of
 * the first plotted bar (for the anchor note); undefined = no bars yet. `intervalSeconds` is the
 * bar length (for the session-alignment note); undefined = unknown, no note.
 */
export function buildLegendRow(
  def: IndicatorDef,
  config: IndicatorConfig,
  pane: number,
  values: IndicatorValues,
  prevValues: IndicatorValues = {},
  firstTime?: number,
  intervalSeconds?: number,
): IndicatorLegendRow {
  const row: IndicatorLegendRow = {
    uid: config.uid,
    title: indicatorTitle(def, config),
    placement: def.placement,
    pane,
    visible: config.visible,
    values: def.lines.map((line) => {
      const v = values[line.key]
      const base = lineColor(config, line)
      return {
        key: line.key,
        label: line.label,
        text: formatIndicatorValue(v, def.placement),
        color:
          line.kind === 'histogram' && v !== undefined
            ? histogramColor(v, prevValues[line.key], base)
            : base,
      }
    }),
  }
  const note =
    anchorNote(def, config, firstTime) ?? sessionAlignmentNote(def, config, intervalSeconds)
  if (note !== undefined) row.note = note
  if (def.profile !== undefined) row.hint = VOLUME_PROFILE_HINDSIGHT
  else if (def.structure !== undefined) row.hint = MARKET_STRUCTURE_HINT
  else if (def.sessions !== undefined) row.hint = SESSION_RANGES_HINT
  return row
}

/* ------------------------------------------------------------------------------------------ */
/* Series manager                                                                              */
/* ------------------------------------------------------------------------------------------ */

type IndicatorSeries = ISeriesApi<'Line'> | ISeriesApi<'Histogram'>

interface LineSlot {
  line: LineDef
  series: IndicatorSeries
}

interface Entry {
  config: IndicatorConfig
  def: IndicatorDef
  runner: IndicatorRunner
  pane: number
  /** Empty until the series are built. Histograms first so they draw below the lines. */
  slots: LineSlot[]
  /** Series built (a def without lines builds none, so `slots` alone cannot tell). */
  built: boolean
  guides: IPriceLine[]
}

export interface IndicatorSnapshot {
  /**
   * `getSnapshot`: bumps whenever data, configs, or panes change (legend values).
   * `getLayoutSnapshot`: the same, except a live tick's tail update ('update' plan).
   */
  version: number
  /** Bumps when indicator panes were added, removed, or rebuilt (legend positions). */
  paneVersion: number
}

function lowerBound(points: readonly IndicatorPoint[], t: number): number {
  let lo = 0
  let hi = points.length
  while (lo < hi) {
    const mid = (lo + hi) >>> 1
    if (points[mid].time < t) lo = mid + 1
    else hi = mid
  }
  return lo
}

function toLineData(p: IndicatorPoint): LineData {
  return { time: p.time as UTCTimestamp, value: p.value }
}

function toHistogramData(points: readonly IndicatorPoint[], positive: string): HistogramData[] {
  return points.map((p, i) => ({
    time: p.time as UTCTimestamp,
    value: p.value,
    color: histogramColor(p.value, i > 0 ? points[i - 1].value : undefined, positive),
  }))
}

/**
 * Owns the Lightweight Charts series of every active indicator.
 *
 * Indicators always compute on the real candles, whatever the chart type (Heikin-Ashi draws
 * synthetic bars, but RSI/EMA/... read the exchange's OHLCV, as TradingView does by default).
 *
 * Usage from Chart: `attach(chart)` after createChart, `sync(configs, appliedCandles)` when the
 * configs change, `applyPlan(plan, candles)` in the candles effect (same plan as the main
 * series), `detach()` before chart.remove(). It is also an external store for
 * `useSyncExternalStore`, in two grains:
 * - `subscribe` / `getSnapshot`: every change, including a live tick's tail update. The legend
 *   rows read this, so they always show the values of the bar just applied.
 * - `subscribeLayout` / `getLayoutSnapshot`: everything except tail updates (configs, panes,
 *   highlight, full data loads). The Chart component reads this, so a forming-bar tick does not
 *   re-render the whole chart a second time.
 */
export class IndicatorSeriesManager {
  private chart: IChartApi | null = null
  private readonly defs: Defs
  private readonly entries = new Map<string, Entry>()
  private configs: readonly IndicatorConfig[] = []
  private candles: readonly Candle[] | null = null
  private snapshot: IndicatorSnapshot = { version: 0, paneVersion: 0 }
  private readonly listeners = new Set<() => void>()
  private layoutSnapshot: IndicatorSnapshot = { version: 0, paneVersion: 0 }
  private readonly layoutListeners = new Set<() => void>()
  /** Config uid of every built indicator series (kept in step with `slots`). */
  private readonly uidBySeries = new Map<unknown, string>()
  private palette: IndicatorPalette = colors
  /** Indicator under the mouse (LWC hit testing): its lines draw one pixel wider. */
  private highlighted: string | null = null
  private legendMemo: {
    snapshot: IndicatorSnapshot
    candles: readonly Candle[] | null
    configs: readonly IndicatorConfig[]
    hoveredTime: number | undefined
    intervalSeconds: number | undefined
    rows: IndicatorLegendRow[]
  } | null = null

  constructor(defs: Defs = INDICATOR_BY_ID) {
    this.defs = defs
  }

  attach(chart: IChartApi): void {
    this.chart = chart
    this.highlighted = null
    this.entries.clear()
    this.legendMemo = null
    this.uidBySeries.clear()
    this.configs = []
    this.candles = null
    this.emit(true)
  }

  /** Drop every reference without touching the chart (it is about to be removed). */
  detach(): void {
    this.chart = null
    this.highlighted = null
    this.entries.clear()
    this.legendMemo = null
    this.uidBySeries.clear()
    this.configs = []
    this.candles = null
    this.emit(true)
  }

  /** Bring the series in line with `configs`. `candles` = the candles the chart currently shows. */
  sync(configs: readonly IndicatorConfig[], candles: readonly Candle[] | null): void {
    const chart = this.chart
    if (!chart) return
    this.candles = candles
    const diff = diffIndicators(this.configs, configs, this.defs)
    const byUid = new Map(configs.map((c) => [c.uid, c]))
    // Pane sizes the user may have dragged; kept across pane rebuilds below.
    const stretch = this.captureStretch(chart)

    for (const uid of diff.remove) {
      const entry = this.entries.get(uid)
      if (entry) this.destroySeries(entry)
      this.entries.delete(uid)
      this.legendMemo = null
      if (uid === this.highlighted && !byUid.has(uid)) this.highlighted = null
    }
    if (diff.rebuildPanes) {
      for (const entry of this.entries.values()) {
        if (ownsPane(entry.def)) this.destroySeries(entry)
      }
    }
    if (diff.remove.length > 0 || diff.rebuildPanes) this.removeEmptyPanes()

    for (const uid of diff.create) {
      const config = byUid.get(uid)
      const def = config && this.defs.get(config.type)
      if (!config || !def) continue
      const runner = new IndicatorRunner(def, config.inputs, config.source)
      if (candles) runner.reset(candles)
      this.entries.set(uid, {
        config,
        def,
        runner,
        pane: 0,
        slots: [],
        built: false,
        guides: [],
      })
    }
    const recomputed = new Set(diff.recompute)
    for (const uid of diff.recompute) {
      const entry = this.entries.get(uid)
      const config = byUid.get(uid)
      if (!entry || !config) continue
      entry.runner = new IndicatorRunner(entry.def, config.inputs, config.source)
      if (candles) entry.runner.reset(candles)
    }

    // Build missing series in config order so panes 1..n follow the legend order.
    const panes = assignPanes(configs, this.defs)
    const built = new Set<string>()
    let addedPane = false
    for (const config of configs) {
      const entry = this.entries.get(config.uid)
      if (!entry) continue
      entry.config = config
      entry.pane = panes.get(config.uid) ?? 0
      if (!entry.built) {
        this.buildSeries(chart, entry)
        this.setAll(entry, entry.runner.output)
        built.add(config.uid)
        if (ownsPane(entry.def)) addedPane = true
      } else if (recomputed.has(config.uid)) {
        this.setAll(entry, entry.runner.output)
      }
    }
    for (const uid of diff.recolor) {
      const entry = this.entries.get(uid)
      if (!entry || built.has(uid)) continue
      for (const { line, series } of entry.slots) {
        const color = lineColor(entry.config, line)
        series.applyOptions(
          line.kind === 'line' ? { color, crosshairMarkerBackgroundColor: color } : { color },
        )
      }
      // Histogram bars carry per-point colours derived from the chosen colour.
      if (!recomputed.has(uid)) this.setAll(entry, entry.runner.output, 'histogram')
    }
    for (const uid of diff.visibility) {
      const entry = this.entries.get(uid)
      if (!entry || built.has(uid)) continue
      for (const { series } of entry.slots) series.applyOptions({ visible: entry.config.visible })
    }
    if (addedPane) this.applyPaneStretch(chart, stretch)

    this.configs = configs
    this.emit(diff.rebuildPanes || diff.remove.length > 0 || addedPane)
  }

  /**
   * Theme change: crosshair-marker borders and guide lines follow the palette; user-chosen line
   * colours are untouched. Applies to existing series at once and to every series built later.
   */
  setPalette(palette: IndicatorPalette): void {
    if (
      palette.background === this.palette.background &&
      palette.mutedText === this.palette.mutedText
    )
      return
    this.palette = { background: palette.background, mutedText: palette.mutedText }
    if (!this.chart) return
    for (const entry of this.entries.values()) {
      for (const { line, series } of entry.slots) {
        if (line.kind === 'line')
          series.applyOptions({ crosshairMarkerBorderColor: palette.background })
      }
      for (const guide of entry.guides) guide.applyOptions({ color: palette.mutedText })
    }
  }

  /** Apply the main series' sync plan to every indicator. Live ticks only call series.update(). */
  applyPlan(plan: SeriesSyncPlan, candles: readonly Candle[]): void {
    if (!this.chart) return
    if (plan.kind === 'set' || plan.kind === 'prepend') {
      const lastClose = candles.at(-1)?.close
      for (const entry of this.entries.values()) {
        const format = indicatorPriceFormat(entry.def, lastClose)
        if (format && entry.def.placement === 'pane') {
          for (const { series } of entry.slots) series.applyOptions({ priceFormat: format })
        }
        this.setAll(entry, entry.runner.reset(candles))
      }
    } else if (plan.kind === 'update') {
      for (const entry of this.entries.values()) {
        const result = entry.runner.update(candles, plan.from)
        if (result.kind === 'set') this.setAll(entry, result.output)
        else this.updateTail(entry, result.output)
      }
    }
    this.candles = candles
    // A tail update changes legend values only: the layout snapshot (Chart) stays put.
    if (plan.kind === 'update') this.emitValues()
    else if (plan.kind !== 'none') this.emit(false)
  }

  /**
   * Config uid of the indicator that owns `series`, or null (main series, volume, other).
   * O(1): a map kept in step with the built series (crosshair moves call this).
   */
  uidOfSeries(series: ISeriesApi<SeriesType>): string | null {
    return this.uidBySeries.get(series) ?? null
  }

  /**
   * Emphasise the indicator under the mouse: its line series draw one pixel wider (histograms
   * keep their bars). `null` clears it. Restores the def's exact widths on un-hover, and series
   * re-created later (pane rebuild, type change) pick the state up. Unknown uids are ignored.
   */
  setHighlighted(uid: string | null): void {
    if (uid === this.highlighted) return
    if (uid !== null && !this.entries.has(uid)) return
    const previous = this.highlighted
    this.highlighted = uid
    if (previous !== null) this.applyLineWidths(previous)
    if (uid !== null) this.applyLineWidths(uid)
    this.emit(false)
  }

  /** The highlighted indicator's uid, or null. */
  highlightedUid(): string | null {
    return this.highlighted
  }

  /** Pane index of an indicator (0 = price pane), or -1 if unknown. */
  paneIndexOf(uid: string): number {
    return this.entries.get(uid)?.pane ?? -1
  }

  valuesAt(uid: string, time: number): IndicatorValues {
    return this.entries.get(uid)?.runner.valuesAt(time) ?? {}
  }

  /**
   * Legend rows in config order, with values at the hovered bar (or the last bar).
   * `intervalSeconds` (the bar length) enables the session-alignment note.
   */
  legendRows(hoveredTime: number | undefined, intervalSeconds?: number): IndicatorLegendRow[] {
    // The main legend and the pane legends both ask per render: build the rows once per state.
    const memo = this.legendMemo
    if (
      memo !== null &&
      memo.snapshot === this.snapshot &&
      memo.candles === this.candles &&
      memo.configs === this.configs &&
      memo.hoveredTime === hoveredTime &&
      memo.intervalSeconds === intervalSeconds
    ) {
      return memo.rows
    }
    const rows = this.buildLegendRows(hoveredTime, intervalSeconds)
    this.legendMemo = {
      snapshot: this.snapshot,
      candles: this.candles,
      configs: this.configs,
      hoveredTime,
      intervalSeconds,
      rows,
    }
    return rows
  }

  private buildLegendRows(
    hoveredTime: number | undefined,
    intervalSeconds: number | undefined,
  ): IndicatorLegendRow[] {
    const candles = this.candles ?? []
    const index = selectLegendIndex(candles, hoveredTime)
    const time = index >= 0 ? candles[index].time : undefined
    const prevTime = index > 0 ? candles[index - 1].time : undefined
    const rows: IndicatorLegendRow[] = []
    for (const config of this.configs) {
      const entry = this.entries.get(config.uid)
      if (!entry) continue
      const values = time === undefined ? {} : entry.runner.valuesAt(time)
      const prev = prevTime === undefined ? {} : entry.runner.valuesAt(prevTime)
      rows.push(
        buildLegendRow(
          entry.def,
          entry.config,
          entry.pane,
          values,
          prev,
          candles[0]?.time,
          intervalSeconds,
        ),
      )
    }
    return rows
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  getSnapshot = (): IndicatorSnapshot => this.snapshot

  /** Like `subscribe`, but not notified on a live tick's tail update. */
  subscribeLayout = (listener: () => void): (() => void) => {
    this.layoutListeners.add(listener)
    return () => {
      this.layoutListeners.delete(listener)
    }
  }

  /** Like `getSnapshot`, but unchanged by a live tick's tail update. */
  getLayoutSnapshot = (): IndicatorSnapshot => this.layoutSnapshot

  private emit(paneChanged: boolean): void {
    this.snapshot = {
      version: this.snapshot.version + 1,
      paneVersion: this.snapshot.paneVersion + (paneChanged ? 1 : 0),
    }
    this.layoutSnapshot = {
      version: this.layoutSnapshot.version + 1,
      paneVersion: this.layoutSnapshot.paneVersion + (paneChanged ? 1 : 0),
    }
    for (const listener of this.listeners) listener()
    for (const listener of this.layoutListeners) listener()
  }

  /** Values changed (tail update): only the full-grain store moves. */
  private emitValues(): void {
    this.snapshot = { version: this.snapshot.version + 1, paneVersion: this.snapshot.paneVersion }
    for (const listener of this.listeners) listener()
  }

  private buildSeries(chart: IChartApi, entry: Entry): void {
    const { def, config, pane } = entry
    const range = def.range
    const autoscale =
      range === undefined
        ? undefined
        : (): AutoscaleInfo => ({ priceRange: { minValue: range.min, maxValue: range.max } })
    const priceFormat = indicatorPriceFormat(def, this.candles?.at(-1)?.close)
    const priceScaleId = def.placement === 'volume' ? '' : 'right'
    const ordered = [
      ...def.lines.filter((l) => l.kind === 'histogram'),
      ...def.lines.filter((l) => l.kind !== 'histogram'),
    ]
    for (const line of ordered) {
      const common = {
        color: lineColor(config, line),
        visible: config.visible,
        priceScaleId,
        ...(priceFormat ? { priceFormat } : {}),
        ...(autoscale ? { autoscaleInfoProvider: autoscale } : {}),
      }
      const series: IndicatorSeries =
        line.kind === 'histogram'
          ? chart.addSeries(HistogramSeries, { ...indicatorHistogramBase, ...common }, pane)
          : chart.addSeries(
              LineSeries,
              {
                ...indicatorLineBase,
                crosshairMarkerBorderColor: this.palette.background,
                ...common,
                lineWidth: this.lineWidthFor(config.uid, line),
                crosshairMarkerBackgroundColor: lineColor(config, line),
              },
              pane,
            )
      if (def.placement === 'pane') {
        series.priceScale().applyOptions({ scaleMargins: indicatorPaneScaleMargins })
      }
      entry.slots.push({ line, series })
      this.uidBySeries.set(series, config.uid)
    }
    entry.built = true
    const host = entry.slots[0]?.series
    if (host) {
      for (const value of paneGuideValues(def)) {
        entry.guides.push(host.createPriceLine(guideLineOptions(value, this.palette)))
      }
    }
  }

  private destroySeries(entry: Entry): void {
    const chart = this.chart
    if (chart) for (const { series } of entry.slots) chart.removeSeries(series)
    for (const { series } of entry.slots) this.uidBySeries.delete(series)
    entry.slots = []
    entry.built = false
    entry.guides = []
  }

  /** Line width of `line` for indicator `uid`: the def's width, +1 while it is highlighted. */
  private lineWidthFor(uid: string, line: LineDef): LineWidth {
    return (uid === this.highlighted ? line.lineWidth + 1 : line.lineWidth) as LineWidth
  }

  private applyLineWidths(uid: string): void {
    const entry = this.entries.get(uid)
    if (!entry || !this.chart) return
    for (const { line, series } of entry.slots) {
      if (line.kind === 'line') series.applyOptions({ lineWidth: this.lineWidthFor(uid, line) })
    }
  }

  /** LWC drops empty panes by itself; this makes sure no empty indicator pane is left behind. */
  private removeEmptyPanes(): void {
    const chart = this.chart
    if (!chart) return
    const panes = chart.panes()
    for (let i = panes.length - 1; i >= 1; i--) {
      if (panes[i].getSeries().length === 0) chart.removePane(i)
    }
  }

  /** Current stretch factor of the main pane (key '') and of each pane indicator, by uid. */
  private captureStretch(chart: IChartApi): Map<string, number> {
    const panes = chart.panes()
    const out = new Map<string, number>()
    if (panes.length < 2) return out
    out.set('', panes[0].getStretchFactor())
    for (const [uid, entry] of this.entries) {
      const pane = ownsPane(entry.def) && entry.slots.length > 0 && panes[entry.pane]
      if (pane) out.set(uid, pane.getStretchFactor())
    }
    return out
  }

  /** Defaults for new panes; panes that existed before keep the size the user gave them. */
  private applyPaneStretch(chart: IChartApi, saved: ReadonlyMap<string, number>): void {
    const panes = chart.panes()
    panes[0]?.setStretchFactor(saved.get('') ?? mainPaneStretch)
    for (const [uid, entry] of this.entries) {
      if (!ownsPane(entry.def) || entry.pane === 0) continue
      panes[entry.pane]?.setStretchFactor(saved.get(uid) ?? indicatorPaneStretch)
    }
  }

  private setAll(entry: Entry, output: IndicatorOutput, only?: 'histogram'): void {
    for (const { line, series } of entry.slots) {
      const points = output[line.key] ?? []
      if (line.kind === 'histogram') {
        ;(series as ISeriesApi<'Histogram'>).setData(
          toHistogramData(points, lineColor(entry.config, line)),
        )
      } else if (only === undefined) {
        ;(series as ISeriesApi<'Line'>).setData(points.map(toLineData))
      }
    }
  }

  private updateTail(entry: Entry, tail: IndicatorOutput): void {
    const full = entry.runner.output
    for (const { line, series } of entry.slots) {
      const points = tail[line.key] ?? []
      if (line.kind === 'histogram') {
        const all = full[line.key] ?? []
        const positive = lineColor(entry.config, line)
        for (const p of points) {
          const i = lowerBound(all, p.time)
          const prev = i > 0 ? all[i - 1].value : undefined
          ;(series as ISeriesApi<'Histogram'>).update({
            time: p.time as UTCTimestamp,
            value: p.value,
            color: histogramColor(p.value, prev, positive),
          })
        }
      } else {
        for (const p of points) (series as ISeriesApi<'Line'>).update(toLineData(p))
      }
    }
  }
}
