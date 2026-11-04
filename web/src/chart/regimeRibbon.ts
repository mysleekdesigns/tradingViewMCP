// The regime ribbon: a thin strip along the bottom of the main pane coloured by hedgeFund's
// regime of each bar (Bull / Sideways / Bear), labelled by the lab (`POST /lab/regime`).
//
// Filtered, never back-dated: only closed bars are sent (the forming last bar is dropped), and
// the lab labels bar t from closes at or before t, so a new bar never changes an earlier colour.
// Warm-up bars (no label yet) are whitespace. The strip is a constant-height histogram on its
// own overlay price scale pinned to the bottom few percent, so it never moves the price scale.
// On the HMM source each bar's opacity also follows the filtered probability of its state; the
// Markov labels have no probability, so their colours are constant.

import {
  HistogramSeries,
  type AutoscaleInfo,
  type HistogramData,
  type HistogramSeriesPartialOptions,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
  type WhitespaceData,
} from 'lightweight-charts'
import type { RegimeModel, RegimePoint, RegimeState } from '../data/labTypes'
import type { Candle } from '../types'
import { toTime } from './points'
import { withHexAlpha, type ChartPalette } from './theme'
import { upperBoundByTime } from './util/search'

export interface RegimeColors {
  bull: string
  bear: string
  sideways: string
}

/** Bull/Bear follow the user's candle colours; Sideways is the theme's muted grey. */
export const REGIME_ALPHA = 0.55
export const REGIME_SIDEWAYS_ALPHA = 0.35

export function regimeColorsFor(
  palette: Pick<ChartPalette, 'up' | 'down' | 'mutedText'>,
): RegimeColors {
  return {
    bull: withHexAlpha(palette.up, REGIME_ALPHA),
    bear: withHexAlpha(palette.down, REGIME_ALPHA),
    sideways: withHexAlpha(palette.mutedText, REGIME_SIDEWAYS_ALPHA),
  }
}

/** HMM source: the opacity share of a coin-flip bar, faint but still visible. */
export const REGIME_PROB_FLOOR = 0.25

/**
 * HMM source: the share of a state's full opacity for a filtered probability. The linear share
 * runs from 0 at chance (`1 / nStates`) to 1 at certainty and is held at REGIME_PROB_FLOOR where
 * it would fall below that, so the floor covers a band above chance too. A certain bar matches
 * the Markov ribbon's strength.
 */
export function regimeProbStrength(prob: number, nStates: number): number {
  const chance = 1 / nStates
  return Math.min(1, Math.max(REGIME_PROB_FLOOR, (prob - chance) / (1 - chance)))
}

/** '#rrggbbaa' with its alpha scaled by `strength`; any other colour string is returned as is. */
function fadedColor(color: string, strength: number): string {
  if (strength === 1 || !/^#[0-9a-f]{8}$/i.test(color)) return color
  const alpha = Math.round(parseInt(color.slice(7), 16) * strength)
  return `${color.slice(0, 7)}${alpha.toString(16).padStart(2, '0')}`
}

export const REGIME_LABELS: Record<RegimeState, string> = {
  [-1]: 'Bear',
  [0]: 'Sideways',
  [1]: 'Bull',
}

/** The lab's request limit (`MAX_REGIME_BARS` in lab/lab/api.py). */
export const MAX_REGIME_BARS = 20_000

export const REGIME_RIBBON_PRICE_SCALE_ID = 'regime-ribbon'
/** The strip fills the bottom 3% of the main pane. */
export const regimeRibbonScaleMargins = { top: 0.97, bottom: 0 } as const

export const regimeRibbonOptions: HistogramSeriesPartialOptions = {
  priceScaleId: REGIME_RIBBON_PRICE_SCALE_ID,
  base: 0,
  lastValueVisible: false,
  priceLineVisible: false,
  title: '',
  // Each bar is 1 tall on a 0..1 scale of its own; the main price scale never sees it.
  autoscaleInfoProvider: (): AutoscaleInfo => ({ priceRange: { minValue: 0, maxValue: 1 } }),
}

function colorOf(state: RegimeState, colors: RegimeColors): string {
  return state === 1 ? colors.bull : state === -1 ? colors.bear : colors.sideways
}

/**
 * Histogram points for the ribbon: value 1 coloured by state, whitespace for warm-up bars.
 * With `plotted`, points on bars the chart does not plot are dropped (a point at a time the
 * main series lacks would add a bar to the time scale). With `hmmStates` (the HMM source's state
 * count) each bar's opacity follows its `prob`, and a bar without one is whitespace too. Every
 * point depends on its own label alone, so a later point never changes an earlier one.
 */
export function regimeRibbonData(
  points: readonly RegimePoint[],
  colors: RegimeColors,
  plotted?: ReadonlySet<number>,
  hmmStates?: number | null,
): (HistogramData<UTCTimestamp> | WhitespaceData<UTCTimestamp>)[] {
  const out: (HistogramData<UTCTimestamp> | WhitespaceData<UTCTimestamp>)[] = []
  const hmm = hmmStates !== undefined && hmmStates !== null
  for (const p of points) {
    if (plotted !== undefined && !plotted.has(p.time)) continue
    const time = toTime(p.time)
    if (p.state === null) {
      out.push({ time })
    } else if (!hmm) {
      out.push({ time, value: 1, color: colorOf(p.state, colors) })
    } else if (p.prob === null || p.prob === undefined) {
      out.push({ time })
    } else {
      const strength = regimeProbStrength(p.prob, hmmStates)
      out.push({ time, value: 1, color: fadedColor(colorOf(p.state, colors), strength) })
    }
  }
  return out
}

/** The ribbon legend's "bars in regime": the current state and how long it has held. */
export interface RegimeRun {
  state: RegimeState
  /** Consecutive labelled bars ending on the newest labelled bar, that bar included. */
  bars: number
}

/** `regimeRun` over `points[0..end)`, with no copy of the array. */
function runEndingBefore(points: readonly RegimePoint[], end: number): RegimeRun | null {
  let last = end - 1
  // A trailing unlabelled bar is never counted; the run ends on the newest labelled bar.
  while (last >= 0 && points[last].state === null) last--
  if (last < 0) return null
  const state = points[last].state
  if (state === null) return null
  let first = last
  while (first > 0 && points[first - 1].state === state) first--
  return { state, bars: last - first + 1 }
}

/**
 * The newest labelled bar's state and how many consecutive labelled bars end there with that
 * state; null when no bar is labelled. A null-state point breaks the run: it is never counted
 * across and never forward-filled. Filtered like the labels: appending or changing points after
 * bar t leaves the result for `points.slice(0, t + 1)` unchanged.
 */
export function regimeRun(points: readonly RegimePoint[]): RegimeRun | null {
  return runEndingBefore(points, points.length)
}

/**
 * `regimeRun` as of the chart's newest closed bar. `points` may run past it for a moment: after
 * a replay jumps back, the previous labels stay until the new answer lands, and a legend read
 * from them would show bars the replay has not reached yet.
 */
export function regimeRunThrough(
  points: readonly RegimePoint[],
  candles: readonly Candle[],
  formingLast: boolean,
): RegimeRun | null {
  const n = closedCount(candles, formingLast)
  return n === 0 ? null : runEndingBefore(points, upperBoundByTime(points, candles[n - 1].time))
}

/** The legend text, e.g. "Bull · 14 bars". */
export function regimeRunLabel(run: RegimeRun): string {
  return `${REGIME_LABELS[run.state]} · ${run.bars} ${run.bars === 1 ? 'bar' : 'bars'}`
}

/**
 * The bars the ribbon asks the lab about: every loaded bar, no-trade bars included, so the
 * trailing window counts the same rows as the lab's `regime_state` feature (which keeps
 * zero-volume rows). Live (`replayVisible` null) the last bar is still forming; in replay only
 * the bars up to the replay's last visible bar are sent, all closed. Points are drawn on plotted
 * times only (`regimeRibbonData`'s `plotted`).
 */
export function regimeSourceBars(
  raw: readonly Candle[],
  replayVisible: readonly Candle[] | null,
): { candles: readonly Candle[]; formingLast: boolean } {
  if (replayVisible === null) return { candles: raw, formingLast: true }
  if (replayVisible.length === 0) return { candles: [], formingLast: false }
  const cursor = replayVisible[replayVisible.length - 1].time
  // First raw bar after the cursor (raw is ascending).
  const lo = upperBoundByTime(raw, cursor)
  return { candles: lo === raw.length ? raw : raw.slice(0, lo), formingLast: false }
}

/** The closed bars of `candles`: all of them, or all but the last while it is still forming. */
export function closedBarsForRegime(
  candles: readonly Candle[],
  formingLast: boolean,
): readonly Candle[] {
  return formingLast ? candles.slice(0, -1) : candles
}

/** Number of closed bars without copying the array (see `closedBarsForRegime`). */
function closedCount(candles: readonly Candle[], formingLast: boolean): number {
  return formingLast ? Math.max(0, candles.length - 1) : candles.length
}

export interface RegimeQueryInput {
  symbol: string
  timeframe: string
  candles: readonly Candle[]
  /** The last bar is still forming (live data). Default true. */
  formingLast?: boolean
  windowBars?: number
  threshold?: number
  /** Default 'markov'. */
  model?: RegimeModel
}

/**
 * A key that changes only when the closed bars change: a new close, a history prepend, or a
 * corrected last closed bar. Live ticks on the forming bar keep it. Null when nothing is closed.
 * O(1): cheap enough to compute on every render.
 */
export function regimeQueryKey(q: RegimeQueryInput): string | null {
  const n = closedCount(q.candles, q.formingLast ?? true)
  if (n === 0) return null
  const first = q.candles[Math.max(0, n - MAX_REGIME_BARS)]
  const last = q.candles[n - 1]
  return [
    q.symbol,
    q.timeframe,
    q.windowBars ?? '',
    q.threshold ?? '',
    q.model ?? 'markov',
    n,
    first.time,
    last.time,
    last.close,
  ].join('|')
}

/** The newest `MAX_REGIME_BARS` closed bars as request arrays (times in UNIX seconds). */
export function regimeRequestBars(
  candles: readonly Candle[],
  formingLast = true,
): { times: number[]; closes: number[] } {
  const closed = closedBarsForRegime(candles, formingLast).slice(-MAX_REGIME_BARS)
  return { times: closed.map((c) => c.time), closes: closed.map((c) => c.close) }
}

/* ------------------------------------------------------------------------------------------ */
/* Point series sync: update() for a grown tail, setData() otherwise                             */
/* ------------------------------------------------------------------------------------------ */

/** A series that takes a whole data array or one tail point (an LWC series satisfies it). */
export interface PointSeries<P> {
  setData(data: P[]): void
  update(point: P): void
}

/** A histogram / line / whitespace point as the overlays build them. */
type OverlayPoint = { time: number } & object

/** Most trailing points pushed with `update()`; a longer change is one `setData()`. */
export const MAX_TAIL_UPDATES = 2

/** How to move a series from the points it shows (`prev`) to `next`. */
export type PointSyncPlan = { kind: 'none' } | { kind: 'update'; from: number } | { kind: 'set' }

function samePoint(a: OverlayPoint, b: OverlayPoint): boolean {
  const x = a as { time: number; value?: unknown; color?: unknown }
  const y = b as { time: number; value?: unknown; color?: unknown }
  return x.time === y.time && x.value === y.value && x.color === y.color
}

/**
 * 'none' when `next` equals what the series shows; 'update' from index `from` when only the
 * tail changed (at most the last shown point is rewritten at its own time, and at most
 * MAX_TAIL_UPDATES points are pushed, each newer than the one before); 'set' otherwise,
 * including the first data of a series (`prev` null or empty) and any shrink. Points compare
 * by time, value and colour (the fields the overlays build).
 */
export function planPointSync(
  prev: readonly OverlayPoint[] | null,
  next: readonly OverlayPoint[],
): PointSyncPlan {
  if (prev === null || prev.length === 0 || next.length < prev.length) return { kind: 'set' }
  let from = 0
  while (from < prev.length && samePoint(prev[from], next[from])) from++
  if (from === prev.length && next.length === prev.length) return { kind: 'none' }
  // Only the last shown point may change, and only in place (same time): update() at an older
  // time than the series' last point cannot insert, and a newer one would append instead.
  if (from < prev.length - 1) return { kind: 'set' }
  if (from === prev.length - 1 && next[from].time !== prev[from].time) return { kind: 'set' }
  if (next.length - from > MAX_TAIL_UPDATES) return { kind: 'set' }
  for (let i = Math.max(from, 1); i < next.length; i++) {
    if (!(next[i].time > next[i - 1].time)) return { kind: 'set' }
  }
  return { kind: 'update', from }
}

/**
 * Move `series` from `prev` (what it shows, null when unknown) to `next` with the cheapest
 * calls that leave it showing exactly `next`. Returns `next` (what it shows now).
 */
export function syncPoints<P extends OverlayPoint>(
  series: PointSeries<P>,
  prev: readonly P[] | null,
  next: P[],
): readonly P[] {
  const plan = planPointSync(prev, next)
  if (plan.kind === 'set') series.setData(next)
  else if (plan.kind === 'update')
    for (let i = plan.from; i < next.length; i++) series.update(next[i])
  return next
}

type RibbonPoint = HistogramData<UTCTimestamp> | WhitespaceData<UTCTimestamp>

/** The ribbon series on pane 0. `remove` is idempotent; nothing else works after it. */
export class RegimeRibbon {
  private chart: IChartApi | null
  private series: ISeriesApi<'Histogram'> | null
  private colors: RegimeColors
  private points: readonly RegimePoint[] = []
  private plotted: ReadonlySet<number> | undefined
  private hmmStates: number | null = null
  /** What the series shows (null before the first setData). */
  private shown: readonly RibbonPoint[] | null = null

  constructor(chart: IChartApi, colors: RegimeColors) {
    this.chart = chart
    this.colors = colors
    this.series = chart.addSeries(HistogramSeries, regimeRibbonOptions, 0)
    this.series.priceScale().applyOptions({ scaleMargins: regimeRibbonScaleMargins })
  }

  /**
   * Null clears the strip (e.g. the lab is offline or the overlay is loading). `plotted` = the
   * chart's bar times (the chart's shared `BarTimes.set`). When only the newest point changed or
   * was added (a bar close), the series gets `update()`; unchanged points cost no call.
   * `hmmStates` = the HMM source's state count (opacity follows `prob`), null on Markov.
   */
  setPoints(
    points: readonly RegimePoint[] | null,
    plotted?: ReadonlySet<number>,
    hmmStates: number | null = null,
  ): void {
    this.points = points ?? []
    this.plotted = plotted
    this.hmmStates = hmmStates
    this.render()
  }

  setColors(colors: RegimeColors): void {
    if (
      colors.bull === this.colors.bull &&
      colors.bear === this.colors.bear &&
      colors.sideways === this.colors.sideways
    ) {
      return
    }
    this.colors = colors
    // Every coloured point changes: one setData.
    this.render(true)
  }

  remove(): void {
    if (this.chart !== null && this.series !== null) this.chart.removeSeries(this.series)
    this.chart = null
    this.series = null
    this.points = []
    this.shown = null
  }

  /** `full` = always one setData (a recolour); otherwise the cheapest sync (`syncPoints`). */
  private render(full = false): void {
    const series = this.series
    if (series === null) return
    const next = regimeRibbonData(this.points, this.colors, this.plotted, this.hmmStates)
    this.shown = syncPoints<RibbonPoint>(series, full ? null : this.shown, next)
  }
}
