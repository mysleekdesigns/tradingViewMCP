/**
 * Small stateful building blocks for indicator steppers. Each one takes values in time order,
 * returns undefined while warming up, and can be cloned so a live tick can be replayed from a
 * checkpoint (see runner.ts).
 */

/** Last `size` values, oldest first. */
export class RollingWindow {
  readonly size: number
  private readonly buf: number[]

  constructor(size: number, buf: number[] = []) {
    this.size = size
    this.buf = buf
  }

  push(value: number): void {
    this.buf.push(value)
    if (this.buf.length > this.size) this.buf.shift()
  }

  get full(): boolean {
    return this.buf.length === this.size
  }

  get values(): readonly number[] {
    return this.buf
  }

  /** Summed fresh each call, so no floating-point drift builds up over long histories. */
  sum(): number {
    let s = 0
    for (const v of this.buf) s += v
    return s
  }

  mean(): number {
    return this.sum() / this.buf.length
  }

  /** Population standard deviation (divide by N), as TradingView's ta.stdev and Bollinger use. */
  stdev(): number {
    const m = this.mean()
    let ss = 0
    for (const v of this.buf) ss += (v - m) ** 2
    return Math.sqrt(ss / this.buf.length)
  }

  max(): number {
    return Math.max(...this.buf)
  }

  min(): number {
    return Math.min(...this.buf)
  }

  clone(): RollingWindow {
    return new RollingWindow(this.size, [...this.buf])
  }
}

/** Simple moving average: first value after `period` inputs. */
export class Sma {
  private readonly win: RollingWindow

  constructor(period: number, win?: RollingWindow) {
    this.win = win ?? new RollingWindow(period)
  }

  next(value: number): number | undefined {
    this.win.push(value)
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
