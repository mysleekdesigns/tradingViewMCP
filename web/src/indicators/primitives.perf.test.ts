// Phase 19 wave 3 timings and observed drift. Skipped in the normal suite; run with
//   PERF=1 npx vitest run src/indicators/primitives.perf.test.ts
// "old" = the pre-Phase-19 window (array + shift, re-summed per bar, primitives.testkit.ts) or a
// full runner reset; "new" = the ring-buffer window or the runner's prepend splice.
import { describe, expect, test } from 'vitest'
import type { Candle } from '../types'
import { bollingerDef } from './bollinger'
import { RollingWindow } from './primitives'
import { LegacyRollingWindow, U, prng } from './primitives.testkit'
import { IndicatorRunner } from './runner'
import { smaDef } from './sma'
import type { IndicatorDef, IndicatorInputs } from './types'

function history(n: number, t0 = 1_600_000_000): Candle[] {
  const rand = prng(1)
  const out: Candle[] = []
  let price = 30_000
  for (let i = 0; i < n; i++) {
    const open = price
    price *= 1 + (rand() - 0.5) * 0.004
    out.push({
      time: t0 + i * 60,
      open,
      high: Math.max(open, price) * 1.0005,
      low: Math.min(open, price) * 0.9995,
      close: price,
      volume: 1 + rand() * 10,
    })
  }
  return out
}

/** Median milliseconds of `fn` over `rounds` runs (after one warm-up run). */
function timeMs(fn: () => void, rounds = 5): number {
  fn()
  const samples: number[] = []
  for (let r = 0; r < rounds; r++) {
    const t0 = performance.now()
    fn()
    samples.push(performance.now() - t0)
  }
  samples.sort((a, b) => a - b)
  return samples[Math.floor(samples.length / 2)]
}

describe.skipIf(!import.meta.env.PERF)('phase 19 indicator primitives timing', () => {
  const N = 150_000
  const candles = history(N)
  const closes = candles.map((c) => c.close)

  test('SMA and Bollinger, period 500, over 150k bars: old window vs ring buffer', () => {
    const rows: string[] = []
    const smaOld = timeMs(() => {
      const w = new LegacyRollingWindow(500)
      for (const x of closes) {
        w.push(x)
        if (w.full) w.mean()
      }
    }, 3)
    const smaNew = timeMs(() => {
      const w = new RollingWindow(500, { track: 'sum' })
      for (const x of closes) {
        w.push(x)
        if (w.full) w.mean()
      }
    })
    const bbOld = timeMs(() => {
      const w = new LegacyRollingWindow(500)
      for (const x of closes) {
        w.push(x)
        if (w.full) {
          w.mean()
          w.stdev()
        }
      }
    }, 3)
    const bbNew = timeMs(() => {
      const w = new RollingWindow(500)
      for (const x of closes) {
        w.push(x)
        if (w.full) {
          w.mean()
          w.stdev()
        }
      }
    })
    rows.push(`SMA(500) window over 150k: old ${smaOld.toFixed(1)} ms, new ${smaNew.toFixed(1)} ms`)
    rows.push(`BB(500) window over 150k:  old ${bbOld.toFixed(1)} ms, new ${bbNew.toFixed(1)} ms`)
    for (const [name, def] of [
      ['SMA', smaDef],
      ['BB', bollingerDef],
    ] as const) {
      const inputs: IndicatorInputs = name === 'SMA' ? { period: 500 } : { period: 500, mult: 2 }
      const r = new IndicatorRunner(def, inputs, 'close')
      rows.push(`${name}(500) runner.reset 150k: ${timeMs(() => r.reset(candles)).toFixed(1)} ms`)
    }
    console.log(rows.join('\n'))
  })

  test('1000-bar prepend onto 150k bars: full reset vs head splice', () => {
    const all = history(N + 1_000, 1_600_000_000 - 1_000 * 60)
    const old = all.slice(1_000)
    const rows: string[] = []
    const runs: [string, IndicatorDef, IndicatorInputs][] = [
      ['SMA(20)', smaDef, { period: 20 }],
      ['SMA(500)', smaDef, { period: 500 }],
      ['BB(20)', bollingerDef, { period: 20, mult: 2 }],
      ['BB(500)', bollingerDef, { period: 500, mult: 2 }],
    ]
    for (const [name, def, inputs] of runs) {
      const r = new IndicatorRunner(def, inputs, 'close')
      const reset = timeMs(() => r.reset(all))
      // The prepend alone: each round first resets onto the old history (not timed).
      const samples: number[] = []
      for (let round = 0; round < 6; round++) {
        r.reset(old)
        const t0 = performance.now()
        const kind = r.prepend(all, 1_000).kind
        samples.push(performance.now() - t0)
        expect(kind).toBe('splice')
      }
      samples.sort((a, b) => a - b)
      const splice = samples[Math.floor(samples.length / 2)]
      rows.push(
        `${name}: full reset ${reset.toFixed(1)} ms, prepend splice ${splice.toFixed(2)} ms`,
      )
    }
    console.log(rows.join('\n'))
  })

  test('observed drift on a 150k-bar walk (period 500 and 20), in units of u x max|x|', () => {
    const rows: string[] = []
    for (const scale of [1e-8, 1, 1e5, 1e12]) {
      const xs = history(N).map((c) => (c.close / 30_000) * scale)
      let m = 0
      for (const x of xs) m = Math.max(m, Math.abs(x))
      for (const size of [20, 500]) {
        const w = new RollingWindow(size)
        const o = new LegacyRollingWindow(size)
        let dm = 0
        let ds = 0
        let relSd = 0
        // Stamped with bar times, as the indicator steppers push.
        for (let i = 0; i < xs.length; i++) {
          const x = xs[i]
          w.push(x, 1_600_000_000 + i * 60)
          o.push(x)
          if (!o.full) continue
          dm = Math.max(dm, Math.abs(w.mean() - o.mean()))
          const d = Math.abs(w.stdev() - o.stdev())
          ds = Math.max(ds, d)
          relSd = Math.max(relSd, d / o.stdev())
        }
        rows.push(
          `scale ${scale} N ${size}: max|dmean| ${(dm / (U * m)).toFixed(1)} u.M, ` +
            `max|dsd| ${(ds / (U * m)).toFixed(1)} u.M (max rel to sd ${relSd.toExponential(2)})`,
        )
      }
    }
    console.log(rows.join('\n'))
  })
})
