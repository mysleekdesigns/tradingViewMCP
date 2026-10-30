// Phase 19 per-tick timings. Skipped in the normal suite; run with
//   PERF=1 npx vitest run src/perf
// Each case applies forming-bar ticks to a history of N bars and pushes them through the same
// pure steps the chart runs per tick, so growth with N shows the remaining O(n) work.
import { describe, expect, test } from 'vitest'
import type { Candle } from '../types'
import { applyLiveCandle } from '../data/candleStore'
import { plottableCandles } from '../data/plottable'
import { planSeriesSync } from '../chart/seriesSync'
import { aggregateBucket } from '../data/aggregate'
import { parseMiniTickerMessage } from '../data/binanceTicker'

function history(n: number): Candle[] {
  const out: Candle[] = []
  let price = 100
  for (let i = 0; i < n; i++) {
    const open = price
    price = Math.max(1, price + Math.sin(i) * 0.5)
    out.push({
      time: 1_600_000_000 + i * 60,
      open,
      high: Math.max(open, price) + 0.1,
      low: Math.min(open, price) - 0.1,
      close: price,
      volume: 1 + (i % 7),
    })
  }
  return out
}

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

describe.skipIf(!import.meta.env.PERF)('phase 19 per-tick timings', () => {
  test('forming-bar tick: applyLiveCandle + plottableCandles + planSeriesSync', () => {
    const rows: string[] = []
    for (const n of [1_000, 10_000, 150_000]) {
      let raw = history(n)
      let plotted = plottableCandles(raw)
      let k = 0
      const us = timeUs(() => {
        const last = raw[raw.length - 1]
        k++
        const tick: Candle = { ...last, close: last.close + (k % 2 === 0 ? 0.01 : -0.01) }
        const nextRaw = applyLiveCandle(raw, tick)
        const nextPlotted = plottableCandles(nextRaw, plotted)
        expect(planSeriesSync(plotted, nextPlotted).kind).toBe('update')
        raw = nextRaw
        plotted = nextPlotted
      }, 200)
      rows.push(`tick ${n} bars: ${us.toFixed(1)} us`)
    }
    console.log(rows.join('\n'))
  })

  test('aggregated timeframe bucket per base tick (60 base bars)', () => {
    const base = history(60)
    console.log(
      `aggregateBucket 60 bars: ${timeUs(() => aggregateBucket(base[0].time, base), 5_000).toFixed(2)} us`,
    )
  })

  test('whole-market miniTicker message (2000 symbols)', () => {
    const msg = Array.from({ length: 2_000 }, (_, i) => ({
      e: '24hrMiniTicker',
      E: 1_700_000_000_000,
      s: `SYM${i}USDT`,
      c: '1.2345',
      o: '1.2000',
      h: '1.3000',
      l: '1.1000',
      v: '123456.7',
      q: '150000.1',
    }))
    console.log(
      `parseMiniTickerMessage 2000: ${timeUs(() => parseMiniTickerMessage(msg), 200).toFixed(1)} us`,
    )
  })
})
