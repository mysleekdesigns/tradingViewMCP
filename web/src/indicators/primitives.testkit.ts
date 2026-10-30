/**
 * Test-only reference: the pre-Phase-19 RollingWindow (array + shift, everything re-summed per
 * call), kept verbatim so the ring-buffer window can be compared with the numbers the app used
 * to show, and timed against them.
 */
export class LegacyRollingWindow {
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

  sum(): number {
    let s = 0
    for (const v of this.buf) s += v
    return s
  }

  mean(): number {
    return this.sum() / this.buf.length
  }

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

  clone(): LegacyRollingWindow {
    return new LegacyRollingWindow(this.size, [...this.buf])
  }
}

/** Mulberry32: tiny seeded PRNG so random tests are reproducible. */
export function prng(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

/** Unit roundoff of binary64. */
export const U = 2 ** -53
