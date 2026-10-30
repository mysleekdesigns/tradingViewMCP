// Phase 19: the lineage fast paths (candle store, plottable bars, series sync planning,
// BarLineage) must give exactly what the full scans give. The references below are the
// pre-Phase-19 implementations, kept verbatim as oracles.
import { describe, expect, it } from 'vitest'
import { BarLineage } from '../chart/structureBars'
import { MAX_APPENDED_BARS, planSeriesSync, type SeriesSyncPlan } from '../chart/seriesSync'
import type { Candle } from '../types'
import {
  applyLiveCandle,
  knownSamePrefix,
  lineageOf,
  mergeCandles,
  prependOlder,
  recordLineage,
  samePrefixLength,
} from './candleStore'
import { isTradedBar, plottableCandles } from './plottable'

const M = 60

/** Deterministic PRNG (mulberry32), explicit seeds. */
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

function bar(time: number, close: number, volume: number): Candle {
  return { time, open: close - 0.5, high: close + 1, low: close - 1, close, volume }
}

/* ---------------------------------- reference oracles ---------------------------------- */

function refPlottable(raw: readonly Candle[], prev?: readonly Candle[] | null): readonly Candle[] {
  if (raw.every(isTradedBar)) return raw
  const out = raw.filter(isTradedBar)
  if (prev != null && refSameBars(prev, out)) return prev
  return out
}

function refSameBars(a: readonly Candle[], b: readonly Candle[]): boolean {
  if (a.length !== b.length) return false
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false
  return true
}

function refPlan(prev: readonly Candle[] | null, next: readonly Candle[]): SeriesSyncPlan {
  if (prev === next) return { kind: 'none' }
  if (prev === null || prev.length === 0) return { kind: 'set' }
  if (next.length < prev.length) return { kind: 'set' }
  if (next[0].time === prev[0].time) {
    const from = refTailMatch(prev, next, 0)
    if (from === null) return { kind: 'set' }
    if (from >= next.length) return { kind: 'none' }
    return { kind: 'update', from }
  }
  const count = next.findIndex((c) => c.time === prev[0].time)
  if (count <= 0) return { kind: 'set' }
  for (let i = 1; i <= count; i++) {
    if (!(next[i].time > next[i - 1].time)) return { kind: 'set' }
  }
  if (refTailMatch(prev, next, count) === null) return { kind: 'set' }
  return { kind: 'prepend', count }
}

function refTailMatch(
  prev: readonly Candle[],
  next: readonly Candle[],
  offset: number,
): number | null {
  const appended = next.length - offset - prev.length
  if (appended < 0 || appended > MAX_APPENDED_BARS) return null
  const lastIdx = prev.length - 1
  for (let i = 0; i < lastIdx; i++) if (next[offset + i] !== prev[i]) return null
  const last = offset + lastIdx
  if (next[last].time !== prev[lastIdx].time) return null
  for (let i = last + 1; i < next.length; i++) if (!(next[i].time > next[i - 1].time)) return null
  return next[last] === prev[lastIdx] ? last + 1 : last
}

function refPrefix(a: readonly Candle[], b: readonly Candle[]): number {
  const n = Math.min(a.length, b.length)
  let i = 0
  while (i < n && a[i] === b[i]) i++
  return i
}

/* ---------------------------------- random controller ---------------------------------- */

/**
 * One random step on raw bars, like the live controller: forming ticks, rollovers, a late
 * tick on an older bar, identical read-backs, untraded (volume 0) bars, older pages, REST
 * merges and replaced windows.
 */
function step(raw: readonly Candle[], r: () => number, pZero: number): readonly Candle[] {
  const last = raw[raw.length - 1]
  const vol = () => (r() < pZero ? 0 : 1 + Math.floor(r() * 50))
  const x = r()
  if (x < 0.45) return applyLiveCandle(raw, bar(last.time, last.close + r() - 0.5, vol()))
  if (x < 0.6) return applyLiveCandle(raw, bar(last.time + M, last.close, vol()))
  if (x < 0.7) {
    const i = Math.floor(r() * raw.length)
    return applyLiveCandle(raw, bar(raw[i].time, raw[i].close + 1, vol()))
  }
  if (x < 0.75) return applyLiveCandle(raw, { ...last })
  if (x < 0.8) {
    const first = raw[0].time
    const older = Array.from({ length: 1 + Math.floor(r() * 4) }, (_, k) =>
      bar(first - (k + 1) * M, 10, vol()),
    )
    return prependOlder(raw, older)
  }
  if (x < 0.85) return mergeCandles(raw, [bar(last.time, last.close, vol())])
  if (x < 0.88) {
    // A replaced window (gap fill after an outage): unrelated bars.
    const start = last.time - 5 * M
    return Array.from({ length: 8 }, (_, k) => bar(start + k * M, 20 + k, vol()))
  }
  // Several ticks before the chart sees the result (a burst between two renders).
  let out = raw
  const n = 2 + Math.floor(r() * 3)
  for (let k = 0; k < n; k++) {
    const l = out[out.length - 1]
    out = applyLiveCandle(out, bar(l.time + (r() < 0.3 ? M : 0), l.close + r(), vol()))
  }
  return out
}

function initial(n: number, r: () => number, pZero: number): Candle[] {
  return Array.from({ length: n }, (_, i) =>
    bar(1_700_000_000 + i * M, 100 + r(), r() < pZero ? 0 : 1 + i),
  )
}

/** Same verdict as the reference: raw, prev, or a fresh array with the same bars. */
function expectSamePlottable(
  got: readonly Candle[],
  want: readonly Candle[],
  raw: readonly Candle[],
  prev: readonly Candle[] | null,
): void {
  if (want === raw) expect(got).toBe(raw)
  else if (want === prev) expect(got).toBe(prev)
  else {
    expect(got).not.toBe(raw)
    expect(got).not.toBe(prev)
    expect(got.length).toBe(want.length)
    for (let i = 0; i < want.length; i++) expect(got[i]).toBe(want[i])
  }
}

describe('lineage fast paths equal the full scans (randomised)', () => {
  for (const [seed, pZero] of [
    [1, 0],
    [2, 0],
    [3, 0.05],
    [4, 0.3],
    [5, 0.6],
    [6, 1],
  ] as const) {
    it(`seed ${seed}, untraded share ${pZero}`, () => {
      const r = rng(seed)
      let raw: readonly Candle[] = initial(40, r, pZero)
      let plotted: readonly Candle[] | null = null
      let applied: readonly Candle[] | null = null
      const lineage = new BarLineage()
      const history: (readonly Candle[])[] = []
      let fastSteps = 0
      for (let i = 0; i < 1500; i++) {
        const nextRaw = i === 0 ? raw : step(raw, r, pZero)
        const nextPlotted = plottableCandles(nextRaw, plotted)
        expectSamePlottable(nextPlotted, refPlottable(nextRaw, plotted), nextRaw, plotted)
        // The chart sometimes skips a render (it then plans from an older applied array).
        if (r() < 0.85 || applied === null) {
          const plan = planSeriesSync(applied, nextPlotted)
          expect(plan).toEqual(refPlan(applied, nextPlotted))
          const same = applied === null ? null : refPrefix(applied, nextPlotted)
          if (applied !== null) {
            expect(lineage.samePrefixLength(applied, nextPlotted)).toBe(same)
            const known = lineage.knownSamePrefix(applied, nextPlotted)
            if (known !== null) {
              expect(known).toBeLessThanOrEqual(same ?? 0)
              if (applied !== nextPlotted) fastSteps++
            }
          }
          lineage.record(applied, nextPlotted, null)
          applied = nextPlotted
        }
        // Any pair from recent history: exact prefix, and the known bound is a lower bound.
        history.push(nextRaw, nextPlotted)
        if (history.length > 12) history.splice(0, 2)
        const a = history[Math.floor(r() * history.length)]
        const b = history[Math.floor(r() * history.length)]
        expect(samePrefixLength(a, b)).toBe(refPrefix(a, b))
        const known = knownSamePrefix(a, b)
        if (known !== null) expect(known).toBeLessThanOrEqual(refPrefix(a, b))
        expect(planSeriesSync(a, b)).toEqual(refPlan(a, b))
        raw = nextRaw
        plotted = nextPlotted
      }
      // The fast path is really exercised (not only the fallback scans).
      if (pZero < 1) expect(fastSteps).toBeGreaterThan(300)
    })
  }
})

describe('candle store lineage', () => {
  const base = Object.freeze(initial(10, rng(9), 0))

  it('a forming tick keeps every bar but the last; a rollover keeps them all', () => {
    const ticked = applyLiveCandle(base, bar(base[9].time, 1, 1))
    expect(lineageOf(ticked)).toMatchObject({ parent: base, samePrefix: 9 })
    const rolled = applyLiveCandle(base, bar(base[9].time + M, 1, 1))
    expect(lineageOf(rolled)).toMatchObject({ parent: base, samePrefix: 10 })
    const late = applyLiveCandle(base, bar(base[3].time, 1, 1))
    expect(lineageOf(late)).toMatchObject({ parent: base, samePrefix: 3 })
  })

  it('records nothing when nothing changed, and nothing for merges, prepends or new windows', () => {
    expect(applyLiveCandle(base, { ...base[9] })).toBe(base)
    expect(lineageOf(base)).toBeNull()
    expect(lineageOf(mergeCandles(base, [bar(base[9].time, 1, 1)]))).toBeNull()
    expect(lineageOf(prependOlder(base, [bar(base[0].time - M, 1, 1)]))).toBeNull()
    expect(knownSamePrefix(base, base.slice())).toBeNull()
  })

  it('follows a short chain of ticks and gives up after MAX_LINEAGE_HOPS', () => {
    const a = applyLiveCandle(base, bar(base[9].time, 1, 1))
    const b = applyLiveCandle(a, bar(base[9].time + M, 1, 1))
    expect(knownSamePrefix(base, b)).toBe(9)
    expect(knownSamePrefix(a, b)).toBe(10)
    const c = applyLiveCandle(b, bar(base[9].time + M, 2, 1))
    expect(knownSamePrefix(base, c)).toBeNull()
    // The exact prefix is still right without the bound.
    expect(samePrefixLength(base, c)).toBe(9)
  })

  it('ignores an entry whose array changed length after it was recorded (mutation guard)', () => {
    const next = applyLiveCandle(base, bar(base[9].time + M, 1, 1))
    expect(lineageOf(next)).not.toBeNull()
    next.push(bar(base[9].time + 2 * M, 1, 1))
    expect(lineageOf(next)).toBeNull()
    expect(planSeriesSync(base, next)).toEqual(refPlan(base, next))
  })

  it('clamps a recorded prefix to both lengths', () => {
    const next = base.slice(0, 4)
    recordLineage(next, base, 99)
    expect(lineageOf(next)?.samePrefix).toBe(4)
  })

  it('planSeriesSync trusts a recorded prefix instead of reading those bars', () => {
    // A deliberately false record (other's bars are copies) proves the bars are not read; the
    // same pair without the record is a 'set'.
    const other = base.map((c) => ({ ...c }))
    expect(planSeriesSync(base, other)).toEqual({ kind: 'set' })
    const trusted = base.map((c) => ({ ...c }))
    recordLineage(trusted, base, 10)
    expect(planSeriesSync(base, trusted)).toEqual({ kind: 'update', from: 9 })
  })
})

describe('plottableCandles on live ticks', () => {
  it('an untraded forming tick on a thin pair returns prev itself, read from the tail only', () => {
    const raw0 = [bar(0, 1, 1), bar(M, 1, 0), bar(2 * M, 1, 3), bar(3 * M, 1, 0)]
    const p0 = plottableCandles(raw0)
    const raw1 = applyLiveCandle(raw0, bar(3 * M, 1.5, 0))
    expect(plottableCandles(raw1, p0)).toBe(p0)
    // The forming bar trades: one new plotted array with lineage to the previous one.
    const raw2 = applyLiveCandle(raw1, bar(3 * M, 1.5, 2))
    const p2 = plottableCandles(raw2, p0)
    expect(p2.map((c) => c.time)).toEqual([0, 2 * M, 3 * M])
    expect(lineageOf(p2)).toMatchObject({ parent: p0, samePrefix: 2 })
    expect(planSeriesSync(p0, p2)).toEqual({ kind: 'update', from: 2 })
  })

  it('on a liquid pair returns raw itself and plans an update from the last bar', () => {
    const raw0 = initial(50, rng(3), 0)
    const p0 = plottableCandles(raw0)
    expect(p0).toBe(raw0)
    const raw1 = applyLiveCandle(raw0, bar(raw0[49].time, 7, 9))
    expect(plottableCandles(raw1, p0)).toBe(raw1)
    expect(planSeriesSync(p0, raw1)).toEqual({ kind: 'update', from: 49 })
  })
})
