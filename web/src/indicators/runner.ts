import type { Candle } from '../types'
import { emptyOutput, stepInto } from './compute'
import type {
  IndicatorDef,
  IndicatorInputs,
  IndicatorOutput,
  IndicatorPoint,
  IndicatorStepper,
  IndicatorValues,
  PriceSource,
} from './types'

export type RunResult =
  /** Full output: the caller uses series.setData per line. */
  | { kind: 'set'; output: IndicatorOutput }
  /** Only points for replayed bars: the caller calls series.update(point) per point, in order. */
  | { kind: 'update'; output: IndicatorOutput }

/** Index of the first point with `time >= t` (points are ascending by time). */
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

/**
 * Runs one indicator over a candle series and keeps its output current as live ticks arrive.
 *
 * After every call it stores a checkpoint: a clone of the stepper state after the last closed
 * bar (index length - 2; the last bar may still be forming). A tick or an appended bar replays
 * only the bars after the checkpoint, so the result is exactly what a full recompute would give
 * while costing O(tail) instead of O(n).
 */
export class IndicatorRunner {
  readonly def: IndicatorDef
  private readonly inputs: IndicatorInputs
  private readonly source: PriceSource
  private out: IndicatorOutput
  /** Stepper state after bar `cpIndex`; for cpIndex -1 it is a fresh stepper. */
  private checkpoint: IndicatorStepper | null = null
  private cpIndex = -1
  private cpTime: number | null = null
  /** Number of candles at the last call. */
  private length = 0

  constructor(def: IndicatorDef, inputs: IndicatorInputs, source: PriceSource) {
    this.def = def
    this.inputs = inputs
    this.source = source
    this.out = emptyOutput(def)
  }

  /** Full recompute over `candles` (ascending). */
  reset(candles: readonly Candle[]): IndicatorOutput {
    const out = emptyOutput(this.def)
    const stepper = this.def.createStepper(this.inputs, this.source)
    this.replay(stepper, candles, 0, out)
    this.out = out
    return out
  }

  /**
   * Bars candles[from..] changed or were appended; candles[0..from) are the same bars as at the
   * last call. Replays from a checkpoint so only the tail is recomputed (exact, not approximate).
   */
  update(candles: readonly Candle[], from: number): RunResult {
    const full = (): RunResult => ({ kind: 'set', output: this.reset(candles) })
    const n = candles.length
    if (n === 0 || this.checkpoint === null) return full()
    if (!Number.isInteger(from) || from < 0 || from > n) return full()
    const c = this.cpIndex
    if (from < c + 1 || n < this.length) return full()
    if (c >= 0 && candles[c]?.time !== this.cpTime) return full()

    // Nothing to do only when no bars were appended either; replay always starts at c + 1, so a
    // `from` past the previous length still recomputes the new bars.
    if (from === n && n === this.length) return { kind: 'update', output: emptyOutput(this.def) }

    const tail = emptyOutput(this.def)
    this.replay(this.checkpoint.clone(), candles, c + 1, tail)

    // Patch the stored output: drop points at replayed bars, append the new ones.
    const startTime = candles[c + 1].time
    let deleted = false
    for (const line of this.def.lines) {
      const points = this.out[line.key]
      const cut = lowerBound(points, startTime)
      const fresh = tail[line.key]
      if (!deleted && cut < points.length) {
        const kept = new Set(fresh.map((p) => p.time))
        for (let i = cut; i < points.length; i++) {
          if (!kept.has(points[i].time)) {
            deleted = true
            break
          }
        }
      }
      points.length = cut
      for (const p of fresh) points.push(p)
    }
    if (deleted) return { kind: 'set', output: this.out }
    return { kind: 'update', output: tail }
  }

  /** Current full output (kept patched after updates). */
  get output(): IndicatorOutput {
    return this.out
  }

  /** Each line's value at the bar with this time (binary search); {} when none. For the legend. */
  valuesAt(time: number): IndicatorValues {
    const values: IndicatorValues = {}
    for (const line of this.def.lines) {
      const points = this.out[line.key]
      const i = lowerBound(points, time)
      if (i < points.length && points[i].time === time) values[line.key] = points[i].value
    }
    return values
  }

  /**
   * Step `stepper` (positioned just before bar `start`) through candles[start..], appending
   * values to `out`. On the way, store a clone taken after bar length - 2 as the new checkpoint.
   * Callers guarantee start - 1 <= length - 2 whenever candles is non-empty.
   */
  private replay(
    stepper: IndicatorStepper,
    candles: readonly Candle[],
    start: number,
    out: IndicatorOutput,
  ): void {
    const n = candles.length
    this.length = n
    if (n === 0) {
      this.checkpoint = null
      this.cpIndex = -1
      this.cpTime = null
      return
    }
    const newC = n - 2
    stepInto(this.def, stepper, candles.slice(start, newC + 1), out)
    this.checkpoint = stepper.clone()
    this.cpIndex = newC
    this.cpTime = newC >= 0 ? candles[newC].time : null
    stepInto(this.def, stepper, candles.slice(newC + 1), out)
  }
}
