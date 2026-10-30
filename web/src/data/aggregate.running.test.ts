// RunningBucket must equal aggregateBucket over every bar set so far, bit for bit, after every
// step: forming re-sends (values up or down), new newest bars, out-of-order inserts, replaced
// older bars and exact duplicates, with fractional volumes so summation order would show.
import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { RunningBucket, aggregateBucket } from './aggregate'

function mulberry32(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let x = a
    x = Math.imul(x ^ (x >>> 15), x | 1)
    x ^= x + Math.imul(x ^ (x >>> 7), x | 61)
    return ((x ^ (x >>> 14)) >>> 0) / 4294967296
  }
}

const START = 1_700_000_040
const STEP = 60

function randomBar(time: number, rnd: () => number): Candle {
  const open = 100 + (rnd() - 0.5) * 10
  const close = 100 + (rnd() - 0.5) * 10
  return {
    time,
    open,
    high: Math.max(open, close) + rnd() * 3,
    low: Math.min(open, close) - rnd() * 3,
    close,
    // Fractional and wide-ranging, so a different summation order changes the low bits.
    volume: rnd() < 0.1 ? 0 : rnd() * 10 ** Math.floor(rnd() * 6 - 2),
  }
}

/** Field-by-field Object.is (bit-identical, NaN and -0 included). */
function expectSame(got: Candle | null, want: Candle | null): void {
  if (want === null) {
    expect(got).toBeNull()
    return
  }
  expect(got).not.toBeNull()
  if (got === null) return
  for (const k of ['time', 'open', 'high', 'low', 'close', 'volume'] as const) {
    expect(Object.is(got[k], want[k]), `${k}: ${got[k]} vs ${want[k]}`).toBe(true)
  }
}

describe('RunningBucket', () => {
  it('is null while empty and one bar is its own bucket', () => {
    const rb = new RunningBucket()
    expect(rb.value(START)).toBeNull()
    expect(rb.size).toBe(0)
    rb.set({ time: START + STEP, open: 2, high: 5, low: 1, close: 3, volume: 4 })
    expect(rb.value(START)).toEqual({ time: START, open: 2, high: 5, low: 1, close: 3, volume: 4 })
    expect(rb.size).toBe(1)
  })

  it('a forming re-send replaces the bar: volume and a lower high are not kept twice', () => {
    const rb = new RunningBucket()
    rb.set({ time: START, open: 10, high: 12, low: 9, close: 11, volume: 2 })
    rb.set({ time: START + STEP, open: 11, high: 20, low: 10, close: 19, volume: 5 })
    rb.set({ time: START + STEP, open: 11, high: 13, low: 8, close: 12, volume: 3 })
    expect(rb.value(START)).toEqual({
      time: START,
      open: 10,
      high: 13,
      low: 8,
      close: 12,
      volume: 5,
    })
    expect(rb.size).toBe(2)
  })

  it('an out-of-order bar and a replaced older bar re-fold in time order', () => {
    const rb = new RunningBucket()
    rb.set({ time: START + STEP, open: 11, high: 14, low: 10, close: 13, volume: 1 })
    rb.set({ time: START, open: 10, high: 12, low: 9, close: 11, volume: 2 })
    expect(rb.value(START)).toEqual({
      time: START,
      open: 10,
      high: 14,
      low: 9,
      close: 13,
      volume: 3,
    })
    rb.set({ time: START, open: 7, high: 15, low: 6, close: 8, volume: 4 })
    rb.set({ time: START + 2 * STEP, open: 13, high: 13, low: 12, close: 12.5, volume: 1 })
    expect(rb.value(START)).toEqual({
      time: START,
      open: 7,
      high: 15,
      low: 6,
      close: 12.5,
      volume: 6,
    })
  })

  it('equals aggregateBucket after every step of random update sequences (bit-identical)', () => {
    for (let seed = 1; seed <= 100; seed++) {
      const rnd = mulberry32(seed)
      const rb = new RunningBucket()
      const sent: Candle[] = []
      let newest = -1
      const slots = 2 + Math.floor(rnd() * 60)
      for (let step = 0; step < 150; step++) {
        const r = rnd()
        let idx: number
        if (newest < 0 || (r < 0.15 && newest < slots - 1))
          idx = newest + 1 // next bar
        else if (r < 0.75)
          idx = newest // forming re-send
        else idx = Math.floor(rnd() * (newest + 1)) // older bar, or a gap filled late
        if (rnd() < 0.05 && newest + 2 < slots) idx = newest + 2 // a skipped slot
        if (idx > newest) newest = idx
        const c =
          rnd() < 0.05 && sent.length > 0
            ? sent[Math.floor(rnd() * sent.length)] // exact duplicate of an earlier send
            : randomBar(START + idx * STEP, rnd)
        sent.push(c)
        rb.set(c)
        expectSame(rb.value(START), aggregateBucket(START, sent))
        expect(rb.size).toBe(new Set(sent.map((s) => s.time)).size)
      }
    }
  })

  it('non-finite values fold exactly like aggregateBucket', () => {
    const rb = new RunningBucket()
    const bars: Candle[] = [
      { time: START, open: 1, high: Number.NaN, low: 0.5, close: 1, volume: 1 },
      { time: START + STEP, open: 1, high: 2, low: Number.NaN, close: 1, volume: Infinity },
      { time: START + STEP, open: 1, high: 3, low: -0, close: 2, volume: 2 },
      { time: START + 2 * STEP, open: 2, high: Number.NaN, low: 0, close: Number.NaN, volume: -0 },
    ]
    const sent: Candle[] = []
    for (const b of bars) {
      sent.push(b)
      rb.set(b)
      expectSame(rb.value(START), aggregateBucket(START, sent))
    }
  })
})

/** Median microseconds per call of `fn` over `rounds` rounds of `iters` calls. */
function timeUs(fn: () => void, iters: number, rounds = 7): number {
  for (let i = 0; i < iters; i++) fn()
  const samples: number[] = []
  for (let r = 0; r < rounds; r++) {
    const t0 = performance.now()
    for (let i = 0; i < iters; i++) fn()
    samples.push(((performance.now() - t0) * 1000) / iters)
  }
  samples.sort((a, b) => a - b)
  return samples[Math.floor(samples.length / 2)]
}

describe.skipIf(!import.meta.env.PERF)('phase 19 running bucket timing', () => {
  it('forming base tick on a 60-bar bucket: RunningBucket vs aggregateBucket', () => {
    const rnd = mulberry32(42)
    const base = Array.from({ length: 60 }, (_, i) => randomBar(START + i * STEP, rnd))
    const rb = new RunningBucket()
    for (const c of base) rb.set(c)
    const last = base[base.length - 1]
    let k = 0
    const running = timeUs(() => {
      k++
      rb.set({ ...last, close: last.close + (k % 2 === 0 ? 0.01 : -0.01) })
      rb.value(START)
    }, 20_000)
    const full = timeUs(() => aggregateBucket(START, base), 5_000)
    console.log(
      `running bucket per base tick (60 bars): ${running.toFixed(3)} us; aggregateBucket: ${full.toFixed(2)} us`,
    )
  })
})
