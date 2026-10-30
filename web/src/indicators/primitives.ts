/**
 * Small stateful building blocks for indicator steppers. Each one takes values in time order,
 * returns undefined while warming up, and can be cloned so a live tick can be replayed from a
 * checkpoint (see runner.ts).
 */

/** Unit roundoff of IEEE-754 binary64 with round-to-nearest. */
const U = 2 ** -53

/**
 * Relative accuracy RollingWindow certifies for its variance: after every push the proven
 * error bound of the running sums is at most VARIANCE_REL_TOL x variance (plus the noise floor
 * of the plain two-pass formula); otherwise the window re-sums at once. See RollingWindow.
 */
export const VARIANCE_REL_TOL = 1e-10

/**
 * Relative accuracy RollingWindow certifies for its mean (tracks 'sum' and 'squares'): after
 * every push the proven error bound of mean() is at most MEAN_REL_TOL x |mean| (plus one
 * rounding); otherwise the window re-sums at once. Same order as VARIANCE_REL_TOL (stdev is then
 * good to ~5e-11), far below what a chart shows, and only crossed when a value tens of thousands
 * of times the current mean has just left the window (a volume spike, a crash). See RollingWindow.
 */
export const MEAN_REL_TOL = 1e-10

/**
 * What a RollingWindow keeps running sums for:
 * - 'none': only the values (max/min, or mean/stdev summed fresh per call as before);
 * - 'sum': a running sum, so mean() is O(1);
 * - 'squares': running sum and sum of squares, so mean() and stdev() are O(1).
 */
export type WindowTrack = 'none' | 'sum' | 'squares'

/**
 * Whether a window of `size` re-sums at the push stamped `time` after one stamped `prevTime`:
 * when the step crosses a multiple of size x (time - prevTime). On evenly spaced bars that is
 * exactly once every `size` bars. It depends only on the two bar times, never on where the
 * stream started, so a run over a longer history (older bars prepended) re-sums at the same
 * bars as the shorter run, and from such a bar on both hold the same state bit for bit.
 */
export function resumesAt(prevTime: number, time: number, size: number): boolean {
  if (size <= 1) return true
  const step = time - prevTime
  if (!(step > 0)) return false
  const block = size * step
  return Math.floor(time / block) !== Math.floor(prevTime / block)
}

/**
 * First index i >= max(from, 1) of `bars` at which a window of `size` re-sums by bar time
 * (`resumesAt` from bar i - 1 to bar i), or null when there is none. Indicators use it in
 * `spliceFrom`: past such a bar, a window that holds the same bars holds the same sums.
 */
export function firstResum(
  bars: readonly { time: number }[],
  from: number,
  size: number,
): number | null {
  for (let i = Math.max(1, from); i < bars.length; i++) {
    if (resumesAt(bars[i - 1].time, bars[i].time, size)) return i
  }
  return null
}

/** a comes strictly before b in the max order (NaN never reaches it; -0 < +0 like Math.max). */
function above(a: number, b: number): boolean {
  return a > b || (a === 0 && b === 0 && Object.is(b, -0) && !Object.is(a, -0))
}

/** a comes strictly before b in the min order (-0 < +0 like Math.min). */
function below(a: number, b: number): boolean {
  return a < b || (a === 0 && b === 0 && Object.is(a, -0) && !Object.is(b, -0))
}

/**
 * Monotonic deque over the window's values: front = the window maximum (or minimum) in O(1)
 * amortised per push, exact. Holds no NaN (the window counts those). `seqs` are push numbers.
 */
class MonoDeque {
  private readonly better: (a: number, b: number) => boolean
  private vals: number[]
  private seqs: number[]
  private head: number

  constructor(better: (a: number, b: number) => boolean, vals: number[] = [], seqs: number[] = []) {
    this.better = better
    this.vals = vals
    this.seqs = seqs
    this.head = 0
  }

  push(value: number, seq: number): void {
    // Drop values this one dominates; equal values keep the newest (it leaves last).
    while (this.vals.length > this.head && !this.better(this.vals[this.vals.length - 1], value)) {
      this.vals.pop()
      this.seqs.pop()
    }
    this.vals.push(value)
    this.seqs.push(seq)
  }

  /** Forget values pushed before `firstSeq` (they left the window). */
  expire(firstSeq: number): void {
    while (this.head < this.vals.length && this.seqs[this.head] < firstSeq) this.head++
    if (this.head > 64 && this.head * 2 > this.vals.length) {
      this.vals = this.vals.slice(this.head)
      this.seqs = this.seqs.slice(this.head)
      this.head = 0
    }
  }

  /** Front value, or `empty` when the deque holds nothing. */
  front(empty: number): number {
    return this.head < this.vals.length ? this.vals[this.head] : empty
  }

  clone(): MonoDeque {
    return new MonoDeque(this.better, this.vals.slice(this.head), this.seqs.slice(this.head))
  }
}

/**
 * Last `size` values, oldest first, in a ring buffer.
 *
 * Mean and population stdev come from running sums of d = x - k (k = the window mean at the
 * last re-sum), so each push is O(1) instead of O(size). The sums are rebuilt from the buffer
 * ("re-sum", amortised one extra pass per value) when the window first fills and then
 * periodically, which bounds the floating-point drift:
 * - pushes stamped with a bar time (the indicator steppers): at the bars `resumesAt` picks
 *   (every `size` bars on evenly spaced bars), and at the latest after 2 x size pushes (gaps);
 * - unstamped pushes: every `size` pushes.
 * Bar-time re-sums make the state a function of the bars since the last re-sum only, which is
 * what lets the runner splice a history prepend bit for bit (see `firstResum`).
 *
 * - mean: |mean() - exact mean| <= u|mean| + (5.03 size + 9) u D, where u = 2^-53 and
 *   D = max |x - k| over the window and the values pushed since the last re-sum (D <= 2 max|x|
 *   over the last 3 size values). The previous fresh left-to-right sum was within
 *   (size - 1) u max|x| + u|mean|, so the two agree within (12 size + 24) u max|x|.
 *   That bound is relative to the largest recent value, not to the mean: after a spike leaves
 *   (or a crash from 60k to 1e-4) it would allow the mean only a few correct digits. So every
 *   push also tracks a proven bound B on |mean() - exact mean| - 1.01 u |mean()|:
 *     B = 1.01 (es + u size dmax + u |s|) / size,
 *   es = the running bound on |s - sum of the stored d| (the sums' roundings), u size dmax =
 *   the roundings of the stored d = fl(x - k) themselves (|fl(y) - y| <= 1.0000001 u |fl(y)|,
 *   dmax = max |d| over the window at the last re-sum and every d pushed since), u |s| = s / size.
 *   When B > MEAN_REL_TOL x |mean()| it re-sums immediately. Hence after every push
 *     |mean() - exact mean| <= (MEAN_REL_TOL + 1.01 u) |mean()|,
 *   unless the bound right after a re-sum, at most (1.03 size + 2) u max|x - k|, is above that
 *   (a window whose mean is ~0 next to its values, which needs mixed signs: no indicator input,
 *   prices, volumes and %K, is). Such a window re-sums on every push until it is not: O(size)
 *   per push like the pre-Phase-19 window, never less accurate. For non-negative windows the
 *   old fresh sum was within 1.01 size u |mean| + u |mean|, so the two agree within
 *   (1.01 MEAN_REL_TOL + (1.01 size + 3) u) |mean|.
 *   The guard reads only k, s, es and dmax, which a re-sum rebuilds from the window's values
 *   alone, so it keeps the prepend splice exact: two runs that re-sum at the same bar (by bar
 *   time) hold the same state from there on and take the same guard decisions after it.
 * - stdev ('squares'): every push also tracks a proven bound E on the variance error. When
 *   E > VARIANCE_REL_TOL x variance + (size u |k|)^2 it re-sums immediately, so
 *   |stdev() - exact stdev of the d values| <= VARIANCE_REL_TOL stdev() + size u |k|; the last
 *   term is the noise floor of the two-pass formula (its mean is off by up to size u max|x|).
 *   d = x - k is exact by Sterbenz's lemma while k/2 <= x <= 2k, and within u |x - k| otherwise.
 *   Holds for size up to 10^5 (indicator periods stop at 500).
 *
 * Non-finite values follow the old arithmetic: mean() is NaN when the window holds a NaN or
 * both infinities, +-Infinity when it holds one sign of infinity; stdev() is NaN with any
 * non-finite value; max()/min() match Math.max/Math.min (NaN wins, -0 < +0). A value leaving
 * the window takes its effect with it, as before.
 *
 * Partial windows (still warming up) are summed fresh per call, exactly as before.
 */
export class RollingWindow {
  readonly size: number
  readonly track: WindowTrack
  private buf: Float64Array
  /** Next write slot; the oldest value once the window is full. */
  private head = 0
  private count = 0
  /** Values pushed so far (push numbers for the extrema deques). */
  private pushes = 0
  private nan = 0
  private posInf = 0
  private negInf = 0
  /** Running sums over the finite values: s = sum(d), q = sum(d^2), d = x - k. */
  private k = 0
  private s = 0
  private q = 0
  /** Proven bounds on |s - exact sum of d| and |q - exact sum of fl(d^2)|. */
  private es = 0
  private eq = 0
  /** Max |d| over the window at the last re-sum and every d pushed since (for the mean bound). */
  private dmax = 0
  /** Pushes since the last re-sum. */
  private since = 0
  /** Bar time of the last stamped push, or null. */
  private lastTime: number | null = null
  private maxQ: MonoDeque | null = null
  private minQ: MonoDeque | null = null

  constructor(size: number, options: { track?: WindowTrack } = {}) {
    this.size = size
    this.track = options.track ?? 'squares'
    this.buf = new Float64Array(Math.max(1, size))
  }

  /** Add `value`; `time` (the bar's open time, ascending) schedules re-sums by bar time. */
  push(value: number, time?: number): void {
    const n = this.size
    const atBar = time !== undefined && this.lastTime !== null && resumesAt(this.lastTime, time, n)
    if (time !== undefined) this.lastTime = time
    const evicting = this.count === n
    const old = evicting ? this.buf[this.head] : 0
    this.buf[this.head] = value
    this.head = this.head + 1 === n ? 0 : this.head + 1
    this.pushes++
    if (evicting) this.tally(old, -1)
    else this.count++
    this.tally(value, 1)
    if (this.maxQ) this.pushExtreme(this.maxQ, value)
    if (this.minQ) this.pushExtreme(this.minQ, value)

    if (this.track === 'none' || this.count < n) return
    const cap = time === undefined || n === 1 ? n : 2 * n
    if (!evicting || atBar || ++this.since >= cap) {
      this.resum()
      return
    }
    const k = this.k
    const dn = Number.isFinite(value) ? value - k : 0
    // fl(old - k) with the same k is bit-identical to the d added when `old` came in, so the
    // sums stay sums of exactly the stored d values; only the additions round.
    const dOld = Number.isFinite(old) ? old - k : 0
    const s0 = this.s
    this.s = s0 + dn - dOld
    // Two roundings: |err| <= u|s0 + dn| + u|t - dOld| <= (2 + u) u (|s0| + |dn| + |dOld|).
    const adn = Math.abs(dn)
    this.es += 2.01 * U * (Math.abs(s0) + adn + Math.abs(dOld))
    if (adn > this.dmax) this.dmax = adn
    const squares = this.track === 'squares'
    if (squares) {
      const q0 = this.q
      const sqNew = dn * dn
      const sqOld = dOld * dOld
      this.q = q0 + sqNew - sqOld
      this.eq += 2.01 * U * (Math.abs(q0) + sqNew + sqOld)
    }
    // Non-finite windows answer from the counts; the sums cover the finite values and are
    // checked again on the first push after the last non-finite value leaves.
    if (!this.finite()) return
    // Written as !(bound <= tol) so a NaN bound (sums overflowing near 1e308) re-sums too.
    if (
      !(this.meanBound() <= MEAN_REL_TOL * Math.abs(k + this.s / n)) ||
      (squares && this.varianceBound() > VARIANCE_REL_TOL * this.variance() + this.floor())
    )
      this.resum()
  }

  get full(): boolean {
    return this.count === this.size
  }

  /** The window's values, oldest first (a copy). */
  get values(): readonly number[] {
    const out: number[] = []
    this.each((v) => out.push(v))
    return out
  }

  /** Plain left-to-right sum, oldest first (the pre-Phase-19 arithmetic). O(size). */
  sum(): number {
    let s = 0
    this.each((v) => (s += v))
    return s
  }

  mean(): number {
    if (this.track === 'none' || this.count < this.size) return this.sum() / this.count
    if (this.nan > 0 || (this.posInf > 0 && this.negInf > 0)) return NaN
    if (this.posInf > 0) return Infinity
    if (this.negInf > 0) return -Infinity
    return this.k + this.s / this.size
  }

  /** Population standard deviation (divide by N), as TradingView's ta.stdev and Bollinger use. */
  stdev(): number {
    if (this.track !== 'squares' || this.count < this.size) {
      // Two-pass over the buffer (the pre-Phase-19 arithmetic).
      const m = this.sum() / this.count
      let ss = 0
      this.each((v) => (ss += (v - m) ** 2))
      return Math.sqrt(ss / this.count)
    }
    if (!this.finite()) return NaN
    return Math.sqrt(this.variance())
  }

  max(): number {
    if (this.nan > 0) return NaN
    this.maxQ ??= this.buildExtreme(above)
    return this.maxQ.front(-Infinity)
  }

  min(): number {
    if (this.nan > 0) return NaN
    this.minQ ??= this.buildExtreme(below)
    return this.minQ.front(Infinity)
  }

  clone(): RollingWindow {
    const c = new RollingWindow(this.size, { track: this.track })
    c.buf = this.buf.slice()
    c.head = this.head
    c.count = this.count
    c.pushes = this.pushes
    c.nan = this.nan
    c.posInf = this.posInf
    c.negInf = this.negInf
    c.k = this.k
    c.s = this.s
    c.q = this.q
    c.es = this.es
    c.eq = this.eq
    c.dmax = this.dmax
    c.since = this.since
    c.lastTime = this.lastTime
    c.maxQ = this.maxQ ? this.maxQ.clone() : null
    c.minQ = this.minQ ? this.minQ.clone() : null
    return c
  }

  /** Visit the values oldest first. */
  private each(fn: (v: number) => void): void {
    const n = this.size
    const start = this.count < n ? 0 : this.head
    for (let i = 0; i < this.count; i++) {
      const j = start + i
      fn(this.buf[j < n ? j : j - n])
    }
  }

  private tally(v: number, sign: 1 | -1): void {
    if (Number.isNaN(v)) this.nan += sign
    else if (v === Infinity) this.posInf += sign
    else if (v === -Infinity) this.negInf += sign
  }

  private finite(): boolean {
    return this.nan === 0 && this.posInf === 0 && this.negInf === 0
  }

  /** Rebuild k, s and q from the buffer, with their summation-error bounds. O(size). */
  private resum(): void {
    let sum = 0
    let finite = 0
    this.each((v) => {
      if (Number.isFinite(v)) {
        sum += v
        finite++
      }
    })
    // With every value finite, k is exactly the plain mean the window used to return.
    let k = finite > 0 ? sum / finite : 0
    if (!Number.isFinite(k)) k = 0
    let s = 0
    let abs = 0
    let dmax = 0
    let q = 0
    const squares = this.track === 'squares'
    this.each((v) => {
      if (!Number.isFinite(v)) return
      const d = v - k
      const ad = Math.abs(d)
      s += d
      abs += ad
      if (ad > dmax) dmax = ad
      if (squares) q += d * d
    })
    // Recursive summation of m terms: |err| <= gamma_(m-1) sum|t| < 1.01 m u sum|t| (m << 10^13).
    this.k = k
    this.s = s
    this.q = q
    this.es = 1.01 * finite * U * abs
    this.eq = 1.01 * finite * U * q
    this.dmax = dmax
    this.since = 0
  }

  /**
   * Proven bound on |mean() - exact mean of the window| apart from the final rounding of
   * k + s / size (1.01 u |mean()|): the sums' roundings (es), the roundings of the stored
   * d = fl(x - k) (at most u |d| each, size of them, each |d| <= dmax), and that of s / size.
   */
  private meanBound(): number {
    const n = this.size
    return (1.01 * (this.es + U * (n * this.dmax + Math.abs(this.s)))) / n
  }

  /** Variance from the running sums, clamped at 0 (rounding can push it slightly below). */
  private variance(): number {
    const m1 = this.s / this.size
    const v = this.q / this.size - m1 * m1
    return v > 0 ? v : 0
  }

  /**
   * Proven bound on |variance() - exact variance of the stored d values|: the tracked sum errors
   * plus u q (each fl(d^2)), the three roundings of q / n - m1^2, and the error of m1 = s / n.
   */
  private varianceBound(): number {
    const n = this.size
    const m1 = Math.abs(this.s / n)
    const a = (this.es + U * Math.abs(this.s)) / n
    return 1.01 * ((this.eq + 3 * U * Math.abs(this.q)) / n + a * (2 * m1 + a) + 2 * U * m1 * m1)
  }

  /** Variance noise floor of the plain two-pass formula: (size u |k|)^2. */
  private floor(): number {
    const f = this.size * U * this.k
    return f * f
  }

  private pushExtreme(dq: MonoDeque, value: number): void {
    if (!Number.isNaN(value)) dq.push(value, this.pushes)
    dq.expire(this.pushes - this.count + 1)
  }

  private buildExtreme(better: (a: number, b: number) => boolean): MonoDeque {
    const dq = new MonoDeque(better)
    let seq = this.pushes - this.count
    this.each((v) => {
      seq++
      if (!Number.isNaN(v)) dq.push(v, seq)
    })
    return dq
  }
}

/** Simple moving average: first value after `period` inputs. */
export class Sma {
  private readonly win: RollingWindow

  constructor(period: number, win?: RollingWindow) {
    this.win = win ?? new RollingWindow(period, { track: 'sum' })
  }

  /** `time` = the bar's open time: re-sums by bar time (see RollingWindow). */
  next(value: number, time?: number): number | undefined {
    this.win.push(value, time)
    return this.win.full ? this.win.mean() : undefined
  }

  clone(): Sma {
    return new Sma(this.win.size, this.win.clone())
  }
}

/**
 * Exponential smoother seeded with the SMA of the first `period` inputs (TradingView ta.ema /
 * ta.rma convention): first value after `period` inputs, then
 * `prev + alpha * (value - prev)`. Default alpha is 2 / (period + 1) (EMA); pass 1 / period for
 * Wilder's RMA (see `rma`).
 */
export class Ema {
  readonly period: number
  readonly alpha: number
  private seed: number[] = []
  private prev: number | undefined = undefined

  constructor(period: number, alpha: number = 2 / (period + 1)) {
    this.period = period
    this.alpha = alpha
  }

  next(value: number): number | undefined {
    if (this.prev === undefined) {
      this.seed.push(value)
      if (this.seed.length < this.period) return undefined
      let s = 0
      for (const v of this.seed) s += v
      this.prev = s / this.period
      this.seed = []
      return this.prev
    }
    this.prev = this.prev + this.alpha * (value - this.prev)
    return this.prev
  }

  clone(): Ema {
    const copy = new Ema(this.period, this.alpha)
    copy.seed = [...this.seed]
    copy.prev = this.prev
    return copy
  }
}

/** Wilder's moving average (RMA / SMMA), alpha = 1 / period, SMA-seeded. Used by RSI and ATR. */
export function rma(period: number): Ema {
  return new Ema(period, 1 / period)
}
