import type { Candle } from '../types'
import type {
  FlipZone,
  IndicatorDef,
  IndicatorInputs,
  IndicatorStepper,
  MarketStructure,
  StructureEvent,
  SwingPoint,
} from './types'

/**
 * Market structure overlay (Phase 16): confirmed fractal swings, BOS / ChoCh events and flip
 * zones, from closed bars only. Visual only, no edge claim. The rules are written out in
 * `web/README.md` ("Market structure overlay") and pinned by the hand-worked fixture in
 * `marketStructure.fixture.ts` plus the rule cases in `marketStructure.rules.fixture.ts`, together
 * the parity source for any future lab feature.
 *
 * Rules (left = L, right = R):
 * - Swing high at bar p: high[p] > high[p-i] for every i in 1..L (strict) and
 *   high[p] >= high[p+j] for every j in 1..R. Needs p >= L. Swing low mirrored
 *   (low[p] < low[p-i] strict, low[p] <= low[p+j]). Of two equal highs within L bars of each
 *   other the earlier bar is the pivot (equal highs further apart can both be pivots). A bar can
 *   be both. Confirmed on the close of bar p+R (confirmTime = its time).
 * - Per bar t, in this order:
 *   1. Every zone created before t that is still active: first retest when retestTime is null and
 *      high[t] >= bottom and low[t] <= top; then invalidation (support: close[t] < bottom,
 *      resistance: close[t] > top). One bar can be both.
 *   2. Breaks against the latest confirmed swing high H, then the latest swing low L (both
 *      confirmed before t). Unbroken H with close[t] > H.price: 'up' event, 'choch' when the state
 *      was 'down', else 'bos'; state 'up'; support zone [max(open, close), high] of H's pivot bar.
 *      Unbroken L with close[t] < L.price: 'down' event, 'choch' when the state was 'up', else
 *      'bos'; state 'down'; resistance zone [low, min(open, close)] of L's pivot bar. A new zone
 *      when `maxZones` are active first ends the oldest active one ('dropped').
 *   3. Swings confirmed at t (pivot t-R): high first, then low; each becomes the new H / L.
 * Exact float comparisons, closes only for breaks, strict comparisons.
 */

export interface MarketStructureParams {
  left: number
  right: number
  maxZones: number
}

export const MARKET_STRUCTURE_DEFAULTS: Readonly<MarketStructureParams> = {
  left: 5,
  right: 5,
  maxZones: 10,
}

/** Bounds for every count input (left, right, maxZones). */
const COUNT_MIN = 1
const COUNT_MAX = 50

export const MARKET_STRUCTURE_SHOW_KEYS = [
  'showSwings',
  'showBos',
  'showChoch',
  'showZones',
] as const

function count(v: number | undefined, fallback: number): number {
  if (v === undefined || !Number.isFinite(v)) return fallback
  return Math.min(COUNT_MAX, Math.max(COUNT_MIN, Math.round(v)))
}

/** Reads left/right/maxZones from complete, clamped inputs. */
export function structureParams(inputs: IndicatorInputs): MarketStructureParams {
  return {
    left: count(inputs.left, MARKET_STRUCTURE_DEFAULTS.left),
    right: count(inputs.right, MARKET_STRUCTURE_DEFAULTS.right),
    maxZones: count(inputs.maxZones, MARKET_STRUCTURE_DEFAULTS.maxZones),
  }
}

/** Compute-relevant key: changes only when left/right/maxZones change (not the show* toggles). */
export function structureParamsKey(p: MarketStructureParams): string {
  return `${p.left}:${p.right}:${p.maxZones}`
}

/** A confirmed swing plus the zone band its pivot bar would give if broken. */
interface SwingRecord {
  point: SwingPoint
  zoneTop: number
  zoneBottom: number
}

export class MarketStructureEngine {
  private readonly left: number
  private readonly right: number
  private readonly maxZones: number
  /** Ring buffer of the last left + right + 1 bars, bar k at k % size. */
  private readonly size: number
  private readonly time: Float64Array
  private readonly open: Float64Array
  private readonly high: Float64Array
  private readonly low: Float64Array
  private readonly close: Float64Array
  private n = 0
  private lastHigh: SwingRecord | null = null
  private lastLow: SwingRecord | null = null
  /** Zones with endTime null, oldest (earliest created) first. */
  private active: FlipZone[] = []
  private readonly res: MarketStructure = { swings: [], events: [], zones: [], state: 'none' }

  constructor(params: MarketStructureParams) {
    this.left = count(params.left, MARKET_STRUCTURE_DEFAULTS.left)
    this.right = count(params.right, MARKET_STRUCTURE_DEFAULTS.right)
    this.maxZones = count(params.maxZones, MARKET_STRUCTURE_DEFAULTS.maxZones)
    this.size = this.left + this.right + 1
    this.time = new Float64Array(this.size)
    this.open = new Float64Array(this.size)
    this.high = new Float64Array(this.size)
    this.low = new Float64Array(this.size)
    this.close = new Float64Array(this.size)
  }

  /** Bars pushed so far. */
  get count(): number {
    return this.n
  }

  /** Live result: arrays are appended and zone/swing records updated in place by push. */
  get result(): MarketStructure {
    return this.res
  }

  /** Process the next CLOSED bar (ascending time). O(left + right + maxZones). */
  push(bar: Candle): void {
    const t = this.n
    const slot = t % this.size
    this.time[slot] = bar.time
    this.open[slot] = bar.open
    this.high[slot] = bar.high
    this.low[slot] = bar.low
    this.close[slot] = bar.close
    this.n = t + 1

    this.updateZones(bar)
    this.checkBreaks(bar)
    this.confirmSwings(t)
  }

  /** Step 1: retest, then invalidation, for every zone created before this bar. */
  private updateZones(bar: Candle): void {
    if (this.active.length === 0) return
    let kept = 0
    for (const z of this.active) {
      if (z.retestTime === null && bar.high >= z.bottom && bar.low <= z.top) {
        z.retestTime = bar.time
      }
      const through = z.side === 'support' ? bar.close < z.bottom : bar.close > z.top
      if (through) {
        z.endTime = bar.time
        z.endReason = 'invalidated'
      } else {
        this.active[kept++] = z
      }
    }
    this.active.length = kept
  }

  /** Step 2: closes through the latest swing high, then the latest swing low. */
  private checkBreaks(bar: Candle): void {
    const h = this.lastHigh
    if (h !== null && h.point.brokenTime === null && bar.close > h.point.price) {
      this.breakSwing(h, 'up', bar.time)
    }
    const l = this.lastLow
    if (l !== null && l.point.brokenTime === null && bar.close < l.point.price) {
      this.breakSwing(l, 'down', bar.time)
    }
  }

  private breakSwing(s: SwingRecord, direction: 'up' | 'down', time: number): void {
    const against = direction === 'up' ? 'down' : 'up'
    const event: StructureEvent = {
      kind: this.res.state === against ? 'choch' : 'bos',
      direction,
      time,
      level: s.point.price,
      swingPivotTime: s.point.pivotTime,
    }
    this.res.events.push(event)
    this.res.state = direction
    s.point.brokenTime = time

    if (this.active.length >= this.maxZones) {
      const oldest = this.active.shift()
      if (oldest !== undefined) {
        oldest.endTime = time
        oldest.endReason = 'dropped'
      }
    }
    const zone: FlipZone = {
      side: direction === 'up' ? 'support' : 'resistance',
      top: s.zoneTop,
      bottom: s.zoneBottom,
      pivotTime: s.point.pivotTime,
      createdTime: time,
      retestTime: null,
      endTime: null,
      endReason: null,
    }
    this.res.zones.push(zone)
    this.active.push(zone)
  }

  /** Step 3: the pivot `right` bars back, if it has `left` bars before it. High first, then low. */
  private confirmSwings(t: number): void {
    const p = t - this.right
    if (p < this.left) return
    const W = this.size
    const ps = p % W
    const ph = this.high[ps]
    const pl = this.low[ps]
    let isHigh = true
    let isLow = true
    for (let i = 1; i <= this.left && (isHigh || isLow); i++) {
      const s = (p - i) % W
      if (!(ph > this.high[s])) isHigh = false
      if (!(pl < this.low[s])) isLow = false
    }
    for (let j = 1; j <= this.right && (isHigh || isLow); j++) {
      const s = (p + j) % W
      if (!(ph >= this.high[s])) isHigh = false
      if (!(pl <= this.low[s])) isLow = false
    }
    if (!isHigh && !isLow) return

    const pivotTime = this.time[ps]
    const confirmTime = this.time[t % W]
    const po = this.open[ps]
    const pc = this.close[ps]
    if (isHigh) {
      const point: SwingPoint = {
        kind: 'high',
        pivotTime,
        confirmTime,
        price: ph,
        brokenTime: null,
      }
      this.res.swings.push(point)
      this.lastHigh = { point, zoneTop: ph, zoneBottom: Math.max(po, pc) }
    }
    if (isLow) {
      const point: SwingPoint = { kind: 'low', pivotTime, confirmTime, price: pl, brokenTime: null }
      this.res.swings.push(point)
      this.lastLow = { point, zoneTop: Math.min(po, pc), zoneBottom: pl }
    }
  }
}

/** Full computation over closed bars (ascending). One O(n (left + right + maxZones)) pass. */
export function computeMarketStructure(
  closed: readonly Candle[],
  params: MarketStructureParams,
): MarketStructure {
  const engine = new MarketStructureEngine(params)
  for (const c of closed) engine.push(c)
  return engine.result
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

function toggle(key: string, label: string) {
  return {
    key,
    label,
    kind: 'toggle' as const,
    default: 1,
    min: 0,
    max: 1,
    step: 1,
    integer: true,
  }
}

/**
 * Market structure (Phase 16): drawn by markers and a primitive from `structure()`; it has no
 * per-bar lines. Visual only: it makes no edge claim.
 */
export const marketStructureDef: IndicatorDef = {
  id: 'mstructure',
  name: 'Market Structure',
  shortName: 'MS',
  description: 'Confirmed swings, BOS / ChoCh and flip zones (visual only)',
  placement: 'overlay',
  inputs: [
    {
      key: 'left',
      label: 'Left bars',
      default: MARKET_STRUCTURE_DEFAULTS.left,
      min: COUNT_MIN,
      max: COUNT_MAX,
      step: 1,
      integer: true,
    },
    {
      key: 'right',
      label: 'Right bars',
      default: MARKET_STRUCTURE_DEFAULTS.right,
      min: COUNT_MIN,
      max: COUNT_MAX,
      step: 1,
      integer: true,
    },
    {
      key: 'maxZones',
      label: 'Max active zones',
      default: MARKET_STRUCTURE_DEFAULTS.maxZones,
      min: COUNT_MIN,
      max: COUNT_MAX,
      step: 1,
      integer: true,
    },
    toggle('showSwings', 'Swings'),
    toggle('showBos', 'BOS'),
    toggle('showChoch', 'ChoCh'),
    toggle('showZones', 'Flip zones'),
  ],
  source: null,
  lines: [],
  guides: [],
  createStepper() {
    return emptyStepper()
  },
  structure(closed: readonly Candle[], inputs: IndicatorInputs) {
    return computeMarketStructure(closed, structureParams(inputs))
  },
}
