import type { Candle } from '../types'
import { trueRange } from './atr'
import { Sma, rma } from './primitives'
import type {
  IndicatorDef,
  IndicatorInputs,
  IndicatorPoint,
  IndicatorStepper,
  KeyLevelDay,
  KeyLevelKey,
  KeyLevels,
  KeyLevelStep,
} from './types'

/**
 * Key levels overlay (Phase 21): previous-day and previous-week highs and lows, the daily ATR, the
 * share of it used today, and the trend bias. Visual only, no edge claim. The definitions are in
 * PLAN.md Phase 21 and pinned by the hand-worked fixture in `keyLevels.fixture.ts`.
 *
 * Rules (all UTC, times are open times in UNIX seconds):
 * - A day is a UTC calendar day; a week runs Monday 00:00 to Sunday.
 * - `daily` holds closed 1d candles. A candle whose open is not a multiple of 86400 is ignored.
 * - Day D reads only the state as of the candle that opened at exactly D - 86400. If there is no
 *   such candle, day D has no step and null atr, sma and bias: a gap, never forward-filled. A
 *   candle with open >= D (the forming 1d candle included) never affects day D.
 * - Week W reads the 7 candles of the previous Monday-to-Sunday week. Any missing day there gives
 *   W no step. A week's step starts on its Monday even when the first closed bar is later that
 *   week, so it may start before the first PDH/PDL step.
 * - Steps exist for every day from the first closed bar's day (or `through`'s day when no bar has
 *   closed) through `through`'s day, so a day has its levels before its first bar closes. They
 *   never come from intraday bars.
 * - ATR is Wilder's over closed daily candles, first value = SMA of the first N true ranges. The
 *   SMA is over closed daily closes. A missing daily candle restarts both warm-ups from the next
 *   candle after the gap, whose true range is high - low (no previous close to read).
 * - Bias is the last closed daily close strictly above or below the SMA; equal gives null.
 * - Range used resets at each UTC midnight and reads closed intraday bars only; a bar counts for
 *   the day it opens in. A zero ATR (a run of flat daily candles) gives no point, like a null one.
 * - The oldest loaded day gives no range used unless its first bar opens at 00:00 UTC: the earlier
 *   part of that day is not loaded, so its high - low would understate the day. A small chart
 *   window on a short interval can make that day today. Later days are whole by construction.
 */

export interface KeyLevelsParams {
  atrPeriod: number
  smaPeriod: number
  showWeekly: boolean
}

export const KEY_LEVEL_LABELS: Record<KeyLevelKey, string> = {
  pdh: 'PDH',
  pdl: 'PDL',
  pwh: 'PWH',
  pwl: 'PWL',
}

const ATR_DEFAULT = 14
const SMA_DEFAULT = 50
const PERIOD_MIN = 1
const PERIOD_MAX = 500

const DAY = 86_400
const WEEK = 7 * DAY

function whole(v: number | undefined, fallback: number): number {
  if (v === undefined || !Number.isFinite(v)) return fallback
  return Math.min(PERIOD_MAX, Math.max(PERIOD_MIN, Math.round(v)))
}

/** Reads the periods and the weekly toggle: rounded, clamped, defaults when missing. */
export function keyLevelsParams(inputs: IndicatorInputs): KeyLevelsParams {
  return {
    atrPeriod: whole(inputs.atrPeriod, ATR_DEFAULT),
    smaPeriod: whole(inputs.smaPeriod, SMA_DEFAULT),
    showWeekly: inputs.showWeekly === 1,
  }
}

/** Compute-relevant key: changes only with these params, not the draw-only toggles. */
export function keyLevelsParamsKey(p: KeyLevelsParams): string {
  return `${p.atrPeriod}:${p.smaPeriod}:${p.showWeekly ? 1 : 0}`
}

function dayOf(time: number): number {
  return Math.floor(time / DAY) * DAY
}

/** Monday 00:00 UTC of the week holding `day`. 1970-01-01 was a Thursday, hence the + 3. */
function mondayOf(day: number): number {
  return day - ((day / DAY + 3) % 7) * DAY
}

/** One closed daily candle and the state as of its close. */
interface ClosedDay {
  high: number
  low: number
  atr: number | null
  sma: number | null
  bias: KeyLevelDay['bias']
}

/** Closed days by open time: one pass, each entry reads only candles at or before its own. */
function closedDays(
  daily: readonly Candle[],
  atrPeriod: number,
  smaPeriod: number,
): Map<number, ClosedDay> {
  const out = new Map<number, ClosedDay>()
  let atr = rma(atrPeriod)
  let sma = new Sma(smaPeriod)
  let prev: Candle | null = null
  for (const c of daily) {
    if (c.time % DAY !== 0) continue
    if (prev !== null && c.time !== prev.time + DAY) {
      atr = rma(atrPeriod)
      sma = new Sma(smaPeriod)
      prev = null
    }
    const a = atr.next(trueRange(c, prev?.close)) ?? null
    const s = sma.next(c.close, c.time) ?? null
    const bias = s === null || c.close === s ? null : c.close > s ? 'above' : 'below'
    out.set(c.time, { high: c.high, low: c.low, atr: a, sma: s, bias })
    prev = c
  }
  return out
}

export class KeyLevelsEngine {
  private readonly weekly: boolean
  private readonly closed: Map<number, ClosedDay>
  /** Days built so far from `firstDay`, and steps.length after each of them. */
  private readonly steps: KeyLevelStep[] = []
  private readonly days: KeyLevelDay[] = []
  private readonly stepEnds: number[] = []
  private readonly rangeUsed: IndicatorPoint[] = []
  private firstDay: number | null = null
  private lastTime = -Infinity
  /** The UTC day of the newest bar: its high, low and ATR. */
  private curDay: number | null = null
  private dayHigh = -Infinity
  private dayLow = Infinity
  private dayAtr: number | null = null

  constructor(daily: readonly Candle[], params: KeyLevelsParams) {
    const { atrPeriod, smaPeriod, showWeekly } = keyLevelsParams({
      atrPeriod: params.atrPeriod,
      smaPeriod: params.smaPeriod,
      showWeekly: params.showWeekly ? 1 : 0,
    })
    this.weekly = showWeekly
    this.closed = closedDays(daily, atrPeriod, smaPeriod)
  }

  /** Next closed intraday bar, ascending; a repeated or older time throws. */
  push(bar: Candle): void {
    const t = bar.time
    if (!(t > this.lastTime)) {
      throw new RangeError(`KeyLevelsEngine: bar ${t} is not after ${this.lastTime}`)
    }
    this.lastTime = t
    const day = dayOf(t)
    this.firstDay ??= day
    if (day !== this.curDay) {
      const partial = this.curDay === null && t !== day
      this.curDay = day
      this.dayHigh = -Infinity
      this.dayLow = Infinity
      this.dayAtr = partial ? null : (this.closed.get(day - DAY)?.atr ?? null)
    }
    if (bar.high > this.dayHigh) this.dayHigh = bar.high
    if (bar.low < this.dayLow) this.dayLow = bar.low
    const atr = this.dayAtr
    if (atr === null || atr <= 0) return
    this.rangeUsed.push({ time: t, value: ((this.dayHigh - this.dayLow) / atr) * 100 })
  }

  /**
   * The output through the newest pushed bar and `through`: fresh arrays each call, and nothing
   * returned before is ever mutated. `through` may move either way between calls.
   */
  result(through: number | null): KeyLevels {
    const wanted = through === null ? null : dayOf(through)
    const first = this.firstDay
    if (first === null) {
      // No closed bar yet, so no first day to build from: a later bar may be on an earlier day.
      const out: KeyLevels = { steps: [], days: [], rangeUsed: [] }
      if (wanted !== null) this.appendDay(wanted, true, out.steps, out.days)
      return out
    }
    const newest = dayOf(this.lastTime)
    const last = wanted === null || wanted < newest ? newest : wanted
    const count = (last - first) / DAY + 1
    for (let i = this.days.length; i < count; i++) {
      this.appendDay(first + i * DAY, i === 0, this.steps, this.days)
      this.stepEnds.push(this.steps.length)
    }
    return {
      steps: this.steps.slice(0, this.stepEnds[count - 1]),
      days: this.days.slice(0, count),
      rangeUsed: this.rangeUsed.slice(),
    }
  }

  /** Day `day`'s entry and steps. `first`: the first day output, which also opens its week. */
  private appendDay(day: number, first: boolean, steps: KeyLevelStep[], days: KeyLevelDay[]): void {
    const monday = mondayOf(day)
    // A week's step starts on its Monday, before this day's steps unless this day is that Monday.
    if (this.weekly && first && day !== monday) this.appendWeek(monday, steps)
    const prev = this.closed.get(day - DAY)
    if (prev !== undefined) {
      steps.push(
        { key: 'pdh', start: day, end: day + DAY, price: prev.high },
        { key: 'pdl', start: day, end: day + DAY, price: prev.low },
      )
    }
    if (this.weekly && day === monday) this.appendWeek(monday, steps)
    days.push({
      day,
      atr: prev?.atr ?? null,
      sma: prev?.sma ?? null,
      bias: prev?.bias ?? null,
    })
  }

  private appendWeek(monday: number, steps: KeyLevelStep[]): void {
    let high = -Infinity
    let low = Infinity
    for (let d = monday - WEEK; d < monday; d += DAY) {
      const c = this.closed.get(d)
      if (c === undefined) return
      if (c.high > high) high = c.high
      if (c.low < low) low = c.low
    }
    steps.push(
      { key: 'pwh', start: monday, end: monday + WEEK, price: high },
      { key: 'pwl', start: monday, end: monday + WEEK, price: low },
    )
  }
}

/** Full computation: the engine run to the end, so incremental equals full by construction. */
export function computeKeyLevels(
  daily: readonly Candle[],
  closed: readonly Candle[],
  through: number | null,
  params: KeyLevelsParams,
): KeyLevels {
  const engine = new KeyLevelsEngine(daily, params)
  for (const c of closed) engine.push(c)
  return engine.result(through)
}

function emptyStepper(): IndicatorStepper {
  return {
    next() {
      return {}
    },
    clone() {
      return emptyStepper()
    },
  }
}

function period(key: string, label: string, def: number) {
  return { key, label, default: def, min: PERIOD_MIN, max: PERIOD_MAX, step: 1, integer: true }
}

function toggle(key: string, label: string, def: 0 | 1) {
  return {
    key,
    label,
    kind: 'toggle' as const,
    default: def,
    min: 0,
    max: 1,
    step: 1,
    integer: true,
  }
}

/**
 * Key levels (Phase 21): drawn by a primitive from `keyLevels()`; it has no per-bar lines. Visual
 * only: it makes no edge claim. `showDaily`, `showRange` and `showBias` change only what is drawn.
 */
export const keyLevelsDef: IndicatorDef = {
  id: 'keylevels',
  name: 'Key Levels',
  shortName: 'KL',
  description:
    'Previous day and week highs and lows, daily ATR range used, trend bias (visual only)',
  placement: 'overlay',
  inputs: [
    period('atrPeriod', 'ATR length', ATR_DEFAULT),
    period('smaPeriod', 'SMA length', SMA_DEFAULT),
    toggle('showDaily', 'Previous day high / low', 1),
    toggle('showWeekly', 'Previous week high / low', 0),
    toggle('showRange', 'Range used', 1),
    toggle('showBias', 'Trend bias', 1),
  ],
  source: null,
  lines: [],
  guides: [],
  createStepper() {
    return emptyStepper()
  },
  keyLevels(daily, closed, through, inputs) {
    return computeKeyLevels(daily, closed, through, keyLevelsParams(inputs))
  },
}
