// Phase 19 (C7): candles <-> hollow <-> Heikin-Ashi switch on the same CandlestickSeries
// (applyOptions + setData) instead of removing it and re-attaching its primitives. A fake series
// merges options the way LWC does; the switched series must end with exactly the options, data
// and price lines a fresh series for the new type gets, and keep its primitives.

import type { CandlestickData, CreatePriceLineOptions } from 'lightweight-charts'
import { describe, expect, it } from 'vitest'
import type { Candle, ChartType } from '../types'
import {
  mainSeriesData,
  sameSeriesKind,
  seriesKindFor,
  switchCandleTypeInPlace,
  usesRealPriceLine,
} from './chartTypes'
import {
  DEFAULT_APPEARANCE,
  mainSeriesOptionsFor,
  paletteFor,
  realPriceLineOptions,
  type ChartPalette,
} from './theme'

interface FakeLine {
  options: CreatePriceLineOptions
}

/** Options merge like LWC's applyOptions (a shallow merge is exact for these flat options). */
class FakeCandleSeries {
  options: Record<string, unknown>
  data: CandlestickData[] = []
  lines: FakeLine[] = []
  primitives: object[] = []
  constructor(options: object) {
    this.options = { ...options }
  }
  applyOptions(o: object) {
    this.options = { ...this.options, ...o }
  }
  setData(d: CandlestickData[]) {
    this.data = d
  }
  createPriceLine(options: CreatePriceLineOptions): FakeLine {
    const line = { options }
    this.lines.push(line)
    return line
  }
  removePriceLine(line: FakeLine) {
    this.lines = this.lines.filter((l) => l !== line)
  }
  attachPrimitive(p: object) {
    this.primitives.push(p)
  }
}

/** What the chart-type effect builds for `type` on a new series (addMainSeries + data + line). */
function fresh(type: ChartType, palette: ChartPalette, candles: readonly Candle[] | null) {
  const series = new FakeCandleSeries(mainSeriesOptionsFor(type, palette))
  const line = usesRealPriceLine(type) ? series.createPriceLine(realPriceLineOptions) : null
  if (candles) series.setData(mainSeriesData(type, candles).points as CandlestickData[])
  return { series, line }
}

const CANDLE_TYPES: ChartType[] = ['candles', 'hollow', 'heikin-ashi']

const candles: Candle[] = Array.from({ length: 30 }, (_, i) => {
  const open = 100 + Math.sin(i) * 5
  const close = open + Math.cos(i * 1.7) * 4
  return {
    time: 1_700_000_000 + i * 60,
    open,
    high: Math.max(open, close) + 1,
    low: Math.min(open, close) - 1,
    close,
    volume: 10 + i,
  }
})

const palettes: ChartPalette[] = [
  paletteFor(DEFAULT_APPEARANCE),
  paletteFor({ ...DEFAULT_APPEARANCE, theme: 'light', upColor: '#2962ff', downColor: '#ff9800' }),
]

describe('sameSeriesKind', () => {
  it('groups the three candle types, and line / area apart', () => {
    const all: ChartType[] = ['candles', 'hollow', 'heikin-ashi', 'line', 'area']
    for (const a of all)
      for (const b of all) expect(sameSeriesKind(a, b)).toBe(seriesKindFor(a) === seriesKindFor(b))
    expect(sameSeriesKind('candles', 'heikin-ashi')).toBe(true)
    expect(sameSeriesKind('hollow', 'line')).toBe(false)
  })
})

describe('switchCandleTypeInPlace', () => {
  it('every candle type sets the same option keys (so applyOptions leaves nothing stale)', () => {
    for (const p of palettes) {
      const keys = CANDLE_TYPES.map((t) => Object.keys(mainSeriesOptionsFor(t, p)).sort())
      expect(keys[1]).toEqual(keys[0])
      expect(keys[2]).toEqual(keys[0])
    }
  })

  it('ends with the options, data and price lines of a fresh series, keeping the series', () => {
    for (const palette of palettes) {
      for (const from of CANDLE_TYPES) {
        for (const to of CANDLE_TYPES) {
          if (from === to) continue
          for (const bars of [candles, null]) {
            const { series, line } = fresh(from, palette, bars)
            const primitive = { name: 'drawings' }
            series.attachPrimitive(primitive)
            const alert = series.createPriceLine({ price: 1, title: 'Alert' })
            const result = switchCandleTypeInPlace(series, to, palette, bars, line)
            const want = fresh(to, palette, bars)
            expect(series.options).toEqual(want.series.options)
            expect(series.data).toEqual(want.series.data)
            expect(series.primitives).toEqual([primitive])
            expect(series.lines.includes(alert)).toBe(true)
            // The real-close line exists exactly on Heikin-Ashi, created only when missing.
            expect(result.realLine !== null).toBe(usesRealPriceLine(to))
            expect(result.createdRealLine).toBe(usesRealPriceLine(to) && line === null)
            if (result.realLine !== null) {
              expect(series.lines.includes(result.realLine)).toBe(true)
              expect(result.realLine.options).toEqual(realPriceLineOptions)
            }
            expect(series.lines.filter((l) => l !== alert)).toHaveLength(
              usesRealPriceLine(to) ? 1 : 0,
            )
            // Heikin-Ashi bars for the tail updates, as the fresh path stores them.
            expect(result.ha).toEqual(bars ? mainSeriesData(to, bars).ha : null)
          }
        }
      }
    }
  })
})
