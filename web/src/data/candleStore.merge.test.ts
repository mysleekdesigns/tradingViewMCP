// Phase 19 (C4/C5): the history merges are binary-search splices of sorted arrays instead of a
// Map + sort. These pin them to the previous implementations (copied verbatim below as oracles)
// on random inputs, sorted or not, with duplicates, element identity included.

import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import {
  BarTimes,
  BarTimesMemo,
  barsKeyOf,
  lineageOf,
  mergeCandles,
  mergeCandlesKeepingUnchanged,
  prependOlder,
  samePrefixLength,
} from './candleStore'

/* ---------------------------- oracles: the pre-Phase-19 code ---------------------------- */

function oldMergeCandles(existing: readonly Candle[], fresh: readonly Candle[]): Candle[] {
  const byTime = new Map<number, Candle>()
  for (const c of existing) byTime.set(c.time, c)
  for (const c of fresh) byTime.set(c.time, c)
  return [...byTime.values()].sort((a, b) => a.time - b.time)
}

function oldPrependOlder(existing: readonly Candle[], older: readonly Candle[]): Candle[] {
  const first = existing.length > 0 ? existing[0].time : Infinity
  const byTime = new Map<number, Candle>()
  for (const c of older) if (c.time < first) byTime.set(c.time, c)
  if (byTime.size === 0) return existing as Candle[]
  const head = [...byTime.values()].sort((a, b) => a.time - b.time)
  return [...head, ...existing]
}

/** `keepUnchangedIdentity` from state/liveCandles.ts (verbatim). */
function oldKeepUnchangedIdentity(prev: readonly Candle[], next: Candle[]): Candle[] {
  const byTime = new Map<number, Candle>()
  for (const c of prev) byTime.set(c.time, c)
  for (let i = 0; i < next.length; i++) {
    const n = next[i]
    const p = byTime.get(n.time)
    if (
      p !== undefined &&
      p !== n &&
      p.open === n.open &&
      p.high === n.high &&
      p.low === n.low &&
      p.close === n.close &&
      p.volume === n.volume
    ) {
      next[i] = p
    }
  }
  return next
}

/* ---------------------------------- random inputs ---------------------------------- */

/** Deterministic PRNG (mulberry32). */
function rng(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

const STEP = 60

/** A bar with values from a tiny set, so equal-field rows (kept identity) are common. */
function bar(time: number, r: () => number): Candle {
  const v = 1 + Math.floor(r() * 2)
  return { time, open: v, high: v, low: v, close: v, volume: Math.floor(r() * 2) }
}

type Shape = 'sorted' | 'unsorted' | 'duplicates'

/** Up to `max` rows with times on a grid starting at `start`, in the given shape. */
function rows(r: () => number, start: number, span: number, max: number, shape: Shape): Candle[] {
  const n = Math.floor(r() * (max + 1))
  const times = new Set<number>()
  for (let i = 0; i < n; i++) times.add(start + Math.floor(r() * span) * STEP)
  let out = [...times].sort((a, b) => a - b).map((t) => bar(t, r))
  if (shape !== 'sorted' && out.length > 0) {
    if (shape === 'duplicates') {
      const extra = 1 + Math.floor(r() * 3)
      for (let i = 0; i < extra; i++) {
        const at = Math.floor(r() * out.length)
        out.splice(at + 1, 0, bar(out[at].time, r))
      }
    }
    if (shape === 'unsorted') out = shuffle(out, r)
  }
  return out
}

function shuffle<T>(arr: T[], r: () => number): T[] {
  const out = arr.slice()
  for (let i = out.length - 1; i > 0; i--) {
    const j = Math.floor(r() * (i + 1))
    ;[out[i], out[j]] = [out[j], out[i]]
  }
  return out
}

/** Freeze rows and the array, so a mutation of an input throws. */
function frozen(arr: Candle[]): readonly Candle[] {
  arr.forEach((x) => Object.freeze(x))
  return Object.freeze(arr)
}

function expectSameElements(got: readonly Candle[], want: readonly Candle[]): void {
  expect(got.length).toBe(want.length)
  for (let i = 0; i < want.length; i++) expect(got[i]).toBe(want[i])
}

const SHAPES: Shape[] = ['sorted', 'unsorted', 'duplicates']
const TRIALS = 600

/* ------------------------------------------ tests ------------------------------------------ */

describe('mergeCandles vs the Map + sort implementation', () => {
  it('returns the same elements (by identity) on random inputs of every shape', () => {
    const r = rng(19_4)
    for (let trial = 0; trial < TRIALS; trial++) {
      const eShape = SHAPES[trial % 3]
      const fShape = SHAPES[Math.floor(trial / 3) % 3]
      const existing = frozen(rows(r, 1000, 40, 30, eShape))
      // Fresh windows: overlapping, inside, before, after, or disjoint from existing.
      const offset = Math.floor(r() * 60) - 20
      const fresh = frozen(rows(r, 1000 + offset * STEP, 1 + Math.floor(r() * 40), 20, fShape))
      const got = mergeCandles(existing, fresh)
      expectSameElements(got, oldMergeCandles(existing, fresh))
      expect(got).not.toBe(existing)
      expect(got).not.toBe(fresh)
    }
  })

  it('handles the empty cases like before', () => {
    const a = frozen([bar(60, rng(1))])
    expectSameElements(mergeCandles([], a), oldMergeCandles([], a))
    expectSameElements(mergeCandles(a, []), oldMergeCandles(a, []))
    expect(mergeCandles(a, [])).not.toBe(a)
    expect(mergeCandles([], [])).toEqual([])
  })
})

describe('prependOlder vs the Map + sort implementation', () => {
  it('returns the same elements, and the same reference when nothing is older', () => {
    const r = rng(19_5)
    for (let trial = 0; trial < TRIALS; trial++) {
      // `existing` is ascending and unique by contract; the page may be in any shape.
      const existing = frozen(rows(r, 1000, 40, 25, 'sorted'))
      const shape = SHAPES[trial % 3]
      const offset = Math.floor(r() * 50) - 40
      const older = frozen(rows(r, 1000 + offset * STEP, 1 + Math.floor(r() * 40), 20, shape))
      const got = prependOlder(existing, older)
      const want = oldPrependOlder(existing, older)
      expectSameElements(got, want)
      if (want === existing) expect(got).toBe(existing)
      else {
        expect(got).not.toBe(existing)
        expect(got).not.toBe(older)
      }
    }
  })

  it('passes an existing array through as it is (old contract: only existing[0] is read)', () => {
    const r = rng(7)
    const existing = frozen([bar(600, r), bar(300, r), bar(900, r)])
    const older = frozen([bar(120, r), bar(60, r)])
    expectSameElements(prependOlder(existing, older), oldPrependOlder(existing, older))
  })
})

describe('mergeCandlesKeepingUnchanged vs mergeCandles + keepUnchangedIdentity', () => {
  it('returns the same elements on random inputs of every shape', () => {
    const r = rng(19_6)
    for (let trial = 0; trial < TRIALS; trial++) {
      const eShape = SHAPES[trial % 3]
      const fShape = SHAPES[Math.floor(trial / 3) % 3]
      const existing = frozen(rows(r, 1000, 40, 30, eShape))
      const offset = Math.floor(r() * 60) - 20
      const fresh = frozen(rows(r, 1000 + offset * STEP, 1 + Math.floor(r() * 40), 20, fShape))
      const got = mergeCandlesKeepingUnchanged(existing, fresh)
      const want = oldKeepUnchangedIdentity(existing, oldMergeCandles(existing, fresh))
      expectSameElements(got, want)
      expect(got).not.toBe(existing)
    }
  })

  it('records the exact identity prefix with an ascending existing array, nothing otherwise', () => {
    const r = rng(19_7)
    for (let trial = 0; trial < TRIALS; trial++) {
      const eShape = SHAPES[trial % 3]
      const existing = frozen(rows(r, 1000, 40, 30, eShape))
      const offset = Math.floor(r() * 60) - 20
      const fresh = frozen(rows(r, 1000 + offset * STEP, 1 + Math.floor(r() * 40), 20, 'sorted'))
      const got = mergeCandlesKeepingUnchanged(existing, fresh)
      const step = lineageOf(got)
      // A shuffled or duplicated array can still come out ascending (few rows): decide by fact.
      const ascending = existing.every((c, i) => i === 0 || c.time > existing[i - 1].time)
      if (ascending && existing.length > 0) {
        let exact = 0
        while (exact < got.length && exact < existing.length && got[exact] === existing[exact])
          exact++
        expect(step).toMatchObject({ parent: existing, samePrefix: exact })
        expect(samePrefixLength(existing, got)).toBe(exact)
      } else {
        expect(step).toBeNull()
      }
    }
  })

  it('a gap fill that repeats the tail with equal values keeps every bar object', () => {
    const r = rng(3)
    const existing = frozen(Array.from({ length: 20 }, (_, i) => bar(1000 + i * STEP, r)))
    const fresh = frozen(existing.slice(15).map((c) => ({ ...c })))
    const got = mergeCandlesKeepingUnchanged(existing, fresh)
    expectSameElements(got, existing)
    expect(lineageOf(got)).toMatchObject({ parent: existing, samePrefix: 20 })
  })
})

describe('barsKeyOf / BarTimes / BarTimesMemo', () => {
  const r = rng(11)
  const bars = (n: number, start = 1000) =>
    Array.from({ length: n }, (_, i) => bar(start + i * STEP, r))

  it('barsKeyOf is count:first:last, empty for no bars', () => {
    expect(barsKeyOf([])).toBe('')
    const b = bars(3)
    expect(barsKeyOf(b)).toBe(`3:${b[0].time}:${b[2].time}`)
  })

  it('builds one times array and one Set per bar set, on first use, shared by every reader', () => {
    let reads = 0
    const base = bars(50)
    // Count element reads: building the times reads each bar once; nothing else reads them.
    const counted = new Proxy(base, {
      get(target, prop, receiver) {
        if (typeof prop === 'string' && /^\d+$/.test(prop)) reads++
        return Reflect.get(target, prop, receiver) as unknown
      },
    })
    const memo = new BarTimesMemo()
    const a = memo.get(counted)
    const keyReads = reads // barsKeyOf reads the first and last bar
    expect(keyReads).toBeLessThanOrEqual(2)
    const set1 = a.set
    const times1 = a.times
    expect(reads - keyReads).toBe(50)
    // A live tick on the forming bar: a new array, same key -> the same object, no re-read.
    const ticked = [...base.slice(0, 49), { ...base[49], close: 9 }]
    const b = memo.get(ticked)
    expect(b).toBe(a)
    expect(b.set).toBe(set1)
    expect(b.times).toBe(times1)
    expect(reads - keyReads).toBe(50)
    expect([...set1]).toEqual(base.map((c) => c.time))
    expect(times1).toEqual(base.map((c) => c.time))
  })

  it('starts a new one when the key changes or on refresh', () => {
    const memo = new BarTimesMemo()
    const base = bars(10)
    const a = memo.get(base)
    const closed = [...base, bar(base[9].time + STEP, r)]
    const b = memo.get(closed)
    expect(b).not.toBe(a)
    expect(b.key).toBe(barsKeyOf(closed))
    expect([...b.set]).toEqual(closed.map((c) => c.time))
    expect(memo.get(closed)).toBe(b)
    expect(memo.get(closed, true)).not.toBe(b)
  })

  it('an empty bar set has an empty Set', () => {
    const t = new BarTimes([])
    expect(t.key).toBe('')
    expect(t.set.size).toBe(0)
    expect(t.times).toEqual([])
  })
})

describe.skipIf(!import.meta.env.PERF)('merge timing (PERF=1)', () => {
  it('a 1000-bar gap fill and a 1000-bar prepend on 150k bars', () => {
    const r = rng(5)
    const n = 150_000
    const existing = Array.from({ length: n }, (_, i) => bar(i * STEP, r))
    const tail = existing.slice(n - 1000).map((c) => ({ ...c }))
    const older = Array.from({ length: 1000 }, (_, i) => bar((i - 1000) * STEP, r))
    const time = (label: string, f: () => unknown) => {
      const t0 = performance.now()
      for (let i = 0; i < 20; i++) f()
      console.log(`${label}: ${((performance.now() - t0) / 20).toFixed(2)} ms`)
    }
    time('old merge + keepUnchanged', () =>
      oldKeepUnchangedIdentity(existing, oldMergeCandles(existing, tail)),
    )
    time('new mergeCandlesKeepingUnchanged', () => mergeCandlesKeepingUnchanged(existing, tail))
    time('old prependOlder', () => oldPrependOlder(existing, older))
    time('new prependOlder', () => prependOlder(existing, older))
  })
})
