import type { Candle } from '../types'
import type {
  IndicatorDef,
  IndicatorInputs,
  IndicatorStepper,
  VolumeProfile,
  VolumeProfileRow,
} from './types'

/** 2100-01-01 00:00:00 UTC: upper bound for a time input. */
const MAX_TIME = 4_102_444_800

/**
 * Caveat shown with every fixed-range profile: rows (and the POC / value area) use every bar in
 * the range, including bars after any given bar inside it.
 */
export const VOLUME_PROFILE_HINDSIGHT =
  'Rows use every bar in the range, including later ones: hindsight, not a signal'

/**
 * Total height of the profile when every bar in the range trades at one price p: the rows span
 * a tiny band around p (relative to p, so tiny prices keep positive rows) and all volume sits in
 * the middle row (index floor(rows / 2)).
 */
function degenerateHeight(p: number): number {
  return p !== 0 ? Math.abs(p) * 1e-6 : 1e-6
}

function usableVolume(c: Candle): number {
  return Number.isFinite(c.volume) && c.volume > 0 ? c.volume : 0
}

function rowCountOf(rowCount: number): number {
  return Math.max(1, Math.round(Number.isFinite(rowCount) ? rowCount : 1))
}

/** Index of the first candle with time >= t (candles ascending). */
function lowerBound(candles: readonly Candle[], t: number): number {
  let lo = 0
  let hi = candles.length
  while (lo < hi) {
    const mid = (lo + hi) >>> 1
    if (candles[mid].time < t) lo = mid + 1
    else hi = mid
  }
  return lo
}

/**
 * In-range bars `candles[start .. start + count - 1]`: open time in [from, to] (`to` 0 = up to
 * the last candle). Two binary searches, nothing read outside the range.
 */
export function profileRange(
  candles: readonly Candle[],
  from: number,
  to: number,
): { start: number; count: number } {
  const start = from > 0 ? lowerBound(candles, from) : 0
  const end = Number.isFinite(to) && to > 0 ? lowerBound(candles, to + 1) : candles.length
  return { start, count: Math.max(0, end - start) }
}

/** The row grid: `n` equal rows over [lowest low, highest high] (or the single-price band). */
interface Grid {
  readonly minLow: number
  readonly maxHigh: number
  readonly n: number
  readonly lo: number
  readonly hi: number
  readonly h: number
  readonly degenerate: boolean
  readonly mid: number
}

function makeGrid(minLow: number, maxHigh: number, n: number): Grid {
  const degenerate = maxHigh === minLow
  const mid = Math.floor(n / 2)
  if (degenerate) {
    const h = degenerateHeight(minLow) / n
    const lo = minLow - (mid + 0.5) * h
    return { minLow, maxHigh, n, lo, hi: lo + n * h, h, degenerate, mid }
  }
  return { minLow, maxHigh, n, lo: minLow, hi: maxHigh, h: (maxHigh - minLow) / n, degenerate, mid }
}

/** Lower edge of row i (i = n: the top edge). */
function edge(g: Grid, i: number): number {
  return i >= g.n ? g.hi : g.lo + i * g.h
}

/**
 * Row holding price p: row i is [edge(i), edge(i + 1)), the top row includes the top edge.
 * floor() of the rounded quotient can land one row off right at an edge, so the result is
 * settled against the edges exactly as the rows report them.
 */
function rowOf(g: Grid, p: number): number {
  const last = g.n - 1
  let i = Math.floor((p - g.lo) / g.h)
  if (!(i > 0)) i = 0
  else if (i > last) i = last
  if (i < last && p >= edge(g, i + 1)) i += 1
  else if (i > 0 && p < edge(g, i)) i -= 1
  return i
}

/** Add one bar's volume to the rows (uniform over its [low, high]). No allocation. */
function addBar(g: Grid, c: Candle, volume: Float64Array, upVolume: Float64Array): void {
  const v = usableVolume(c)
  if (v === 0) return
  const up = c.close >= c.open
  if (g.degenerate) {
    volume[g.mid] += v
    if (up) upVolume[g.mid] += v
    return
  }
  const bLow = Math.min(c.low, c.high)
  const bHigh = Math.max(c.low, c.high)
  if (bHigh === bLow) {
    const i = rowOf(g, bLow)
    volume[i] += v
    if (up) upVolume[i] += v
    return
  }
  const span = bHigh - bLow
  // One row of slack each side guards against rounding at a row edge; rows the bar does not
  // overlap get nothing.
  const i0 = Math.max(0, rowOf(g, bLow) - 1)
  const i1 = Math.min(g.n - 1, rowOf(g, bHigh) + 1)
  for (let i = i0; i <= i1; i++) {
    const overlap = Math.min(bHigh, edge(g, i + 1)) - Math.max(bLow, edge(g, i))
    if (overlap > 0) {
      const amount = (v * overlap) / span
      volume[i] += amount
      if (up) upVolume[i] += amount
    }
  }
}

/** Rows, POC and value area from accumulated row volumes. */
function finishProfile(
  g: Grid,
  volume: Float64Array,
  upVolume: Float64Array,
  total: number,
  fromTime: number,
  toTime: number,
  valueAreaPct: number,
): VolumeProfile {
  const n = g.n
  const rows: VolumeProfileRow[] = new Array<VolumeProfileRow>(n)
  for (let i = 0; i < n; i++) {
    rows[i] = { low: edge(g, i), high: edge(g, i + 1), volume: volume[i], upVolume: upVolume[i] }
  }

  let pocRow = 0
  for (let i = 1; i < n; i++) if (volume[i] > volume[pocRow]) pocRow = i
  const poc = g.degenerate ? g.minLow : (rows[pocRow].low + rows[pocRow].high) / 2

  const pct = Math.min(100, Math.max(0, Number.isFinite(valueAreaPct) ? valueAreaPct : 70))
  const target = (pct / 100) * total
  let top = pocRow
  let bottom = pocRow
  let included = volume[pocRow]
  while (included < target) {
    const canUp = top + 1 < n
    const canDown = bottom - 1 >= 0
    if (!canUp && !canDown) break
    if (canUp && (!canDown || volume[top + 1] >= volume[bottom - 1])) {
      top += 1
      included += volume[top]
    } else {
      bottom -= 1
      included += volume[bottom]
    }
  }

  return {
    from: fromTime,
    to: toTime,
    rows,
    poc,
    vah: rows[top].high,
    val: rows[bottom].low,
    totalVolume: total,
  }
}

/**
 * Fixed-range volume profile over the bars with `from <= time <= to` (`to` 0 = up to the last
 * candle given). Pure; reads only bars inside the range, so bars after `to` never change it.
 *
 * - Rows: `rows` equal-height buckets spanning [lowest low, highest high] of the range. Row i is
 *   [lo + i*h, lo + (i+1)*h); the top row includes its upper edge.
 * - Each bar's volume is spread uniformly over its [low, high], each row getting the share of the
 *   bar's range it overlaps. A bar with high == low puts all its volume in the row holding that
 *   price (a price exactly on an edge belongs to the row above it). `upVolume` is the same split
 *   for bars with close >= open. Missing, non-finite or negative volume counts as 0.
 * - Single-price range (highest high == lowest low = p): the rows span a band of height
 *   |p| * 1e-6 (1e-6 when p is 0) placed so p is the middle of row floor(rows / 2); all volume is
 *   there and the POC is p.
 * - POC: middle price of the row with the most volume (lowest row on ties).
 * - Value area (TradingView method): start at the POC row and repeatedly add the next row above
 *   or the next row below, whichever has more volume (ties: above), until the included volume is
 *   at least `valueArea`% of the total. VAH = high of the top included row, VAL = low of the
 *   bottom included row.
 *
 * Null when `from` is 0 (not set), `from` is before the first candle given (its bar is not
 * loaded: starting at the first loaded bar would mislabel the range and move as older history
 * loads), no bar is in range, or the range holds no volume.
 *
 * Hindsight: every row uses every bar in the range, including bars after any given bar inside it
 * (VOLUME_PROFILE_HINDSIGHT); it describes the range, it is not a per-bar signal.
 */
export function computeVolumeProfile(
  candles: readonly Candle[],
  from: number,
  to: number,
  rowCount: number,
  valueAreaPct: number,
): VolumeProfile | null {
  if (!Number.isFinite(from) || from <= 0) return null
  if (candles.length === 0 || from < candles[0].time) return null
  const { start, count } = profileRange(candles, from, to)
  if (count === 0) return null
  const end = start + count

  let minLow = Number.POSITIVE_INFINITY
  let maxHigh = Number.NEGATIVE_INFINITY
  let total = 0
  for (let i = start; i < end; i++) {
    const c = candles[i]
    if (c.low < minLow) minLow = c.low
    if (c.high > maxHigh) maxHigh = c.high
    total += usableVolume(c)
  }
  if (!(total > 0)) return null
  if (!Number.isFinite(minLow) || !Number.isFinite(maxHigh)) return null

  const g = makeGrid(minLow, maxHigh, rowCountOf(rowCount))
  const volume = new Float64Array(g.n)
  const upVolume = new Float64Array(g.n)
  for (let i = start; i < end; i++) addBar(g, candles[i], volume, upVolume)
  return finishProfile(
    g,
    volume,
    upVolume,
    total,
    candles[start].time,
    candles[end - 1].time,
    valueAreaPct,
  )
}

/* ------------------------------------------------------------------------------------------ */
/* Open-ended range (to = 0): closed bars accumulated once, the forming bar added per tick      */
/* ------------------------------------------------------------------------------------------ */

/**
 * Accumulation over the closed bars of an open-ended range (every in-range bar but the last),
 * on the row grid that also spans the last bar as it was when this was built. Immutable.
 */
export interface OpenProfileBase {
  readonly grid: Grid
  /** Lowest low / highest high / summed usable volume of the closed bars (sequential order). */
  readonly closedMinLow: number
  readonly closedMaxHigh: number
  readonly closedTotal: number
  /** Open time of the first bar in range (closed or forming). */
  readonly firstTime: number
  readonly volume: Float64Array
  readonly upVolume: Float64Array
}

/** Span of the closed bars plus `forming`, compared exactly as the full pass compares. */
function spanWith(base: OpenProfileBase, forming: Candle): { minLow: number; maxHigh: number } {
  return {
    minLow: forming.low < base.closedMinLow ? forming.low : base.closedMinLow,
    maxHigh: forming.high > base.closedMaxHigh ? forming.high : base.closedMaxHigh,
  }
}

/**
 * Builds the closed-bar accumulation of `candles[start .. last - 1]` on the grid spanning
 * `candles[start .. last]` (`last` = index of the forming bar, `last >= start`). O(range).
 */
export function openProfileBase(
  candles: readonly Candle[],
  start: number,
  last: number,
  rowCount: number,
): OpenProfileBase {
  let closedMinLow = Number.POSITIVE_INFINITY
  let closedMaxHigh = Number.NEGATIVE_INFINITY
  let closedTotal = 0
  for (let i = start; i < last; i++) {
    const c = candles[i]
    if (c.low < closedMinLow) closedMinLow = c.low
    if (c.high > closedMaxHigh) closedMaxHigh = c.high
    closedTotal += usableVolume(c)
  }
  const forming = candles[last]
  const minLow = forming.low < closedMinLow ? forming.low : closedMinLow
  const maxHigh = forming.high > closedMaxHigh ? forming.high : closedMaxHigh
  const grid = makeGrid(minLow, maxHigh, rowCountOf(rowCount))
  const volume = new Float64Array(grid.n)
  const upVolume = new Float64Array(grid.n)
  // An unusable span gives a null profile anyway (see extendOpenProfile); skip the work.
  if (Number.isFinite(minLow) && Number.isFinite(maxHigh)) {
    for (let i = start; i < last; i++) addBar(grid, candles[i], volume, upVolume)
  }
  return {
    grid,
    closedMinLow,
    closedMaxHigh,
    closedTotal,
    firstTime: candles[start].time,
    volume,
    upVolume,
  }
}

/**
 * True when `forming` (a new version of the last bar) leaves the grid unchanged: the closed
 * bars' span widened by it equals the span the base's grid was built for. Otherwise every row
 * edge moves and the base must be rebuilt.
 */
export function openProfileFits(base: OpenProfileBase, forming: Candle): boolean {
  const { minLow, maxHigh } = spanWith(base, forming)
  return Object.is(minLow, base.grid.minLow) && Object.is(maxHigh, base.grid.maxHigh)
}

/**
 * The open-ended profile with `forming` as the last bar: the base's rows plus the forming bar's
 * share, O(rows). Requires `openProfileFits(base, forming)`. Bit-identical to
 * `computeVolumeProfile` over the same bars: the same additions happen in the same order (closed
 * bars in time order, then the forming bar) on the same grid.
 */
export function extendOpenProfile(
  base: OpenProfileBase,
  forming: Candle,
  valueAreaPct: number,
): VolumeProfile | null {
  const total = base.closedTotal + usableVolume(forming)
  if (!(total > 0)) return null
  const g = base.grid
  if (!Number.isFinite(g.minLow) || !Number.isFinite(g.maxHigh)) return null
  const volume = base.volume.slice()
  const upVolume = base.upVolume.slice()
  addBar(g, forming, volume, upVolume)
  return finishProfile(g, volume, upVolume, total, base.firstTime, forming.time, valueAreaPct)
}

const emptyStepper: IndicatorStepper = {
  next() {
    return {}
  },
  clone() {
    return emptyStepper
  },
}

/**
 * Fixed-range volume profile (TradingView "Fixed Range Volume Profile"): defaults 24 rows and a
 * 70% value area. Drawn by a primitive from `profile()`; it has no per-bar lines. The computation
 * is ours; the renderer is ported from TradingView's lightweight-charts plugin example
 * (Apache-2.0), which only draws precomputed rows.
 */
export const volumeProfileDef: IndicatorDef = {
  id: 'vprofile',
  name: 'Volume Profile (Fixed Range)',
  shortName: 'VP',
  description: `Volume traded at each price level over a chosen time range. ${VOLUME_PROFILE_HINDSIGHT}`,
  placement: 'overlay',
  inputs: [
    {
      key: 'from',
      label: 'From',
      kind: 'time',
      default: 0,
      min: 0,
      max: MAX_TIME,
      step: 60,
      integer: true,
    },
    {
      key: 'to',
      label: 'To (latest bar if not set)',
      kind: 'time',
      default: 0,
      min: 0,
      max: MAX_TIME,
      step: 60,
      integer: true,
    },
    { key: 'rows', label: 'Rows', default: 24, min: 6, max: 200, step: 1, integer: true },
    {
      key: 'valueArea',
      label: 'Value area %',
      default: 70,
      min: 50,
      max: 95,
      step: 1,
      integer: true,
    },
  ],
  source: null,
  lines: [],
  guides: [],
  createStepper() {
    // Stateless and immutable, so sharing one instance (and clone returning itself) is safe.
    return emptyStepper
  },
  profile(candles: readonly Candle[], inputs: IndicatorInputs) {
    return computeVolumeProfile(candles, inputs.from, inputs.to, inputs.rows, inputs.valueArea)
  },
}
