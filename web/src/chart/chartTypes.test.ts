import { describe, expect, it } from 'vitest'
import { CHART_TYPES, type Candle } from '../types'
import {
  mainSeriesData,
  mainSeriesTail,
  realPriceLine,
  seriesKindFor,
  toLinePoint,
  usesRealPriceLine,
} from './chartTypes'
import { heikinAshi } from './heikinAshi'
import {
  areaOptions,
  candlestickOptions,
  colors,
  heikinAshiOptions,
  hollowCandlestickOptions,
  lineOptions,
} from './theme'

const H = 3600
const candles: Candle[] = [
  { time: 0, open: 10, high: 12, low: 9, close: 11, volume: 1 },
  { time: H, open: 11, high: 14, low: 10, close: 13, volume: 2 },
  { time: 2 * H, open: 13, high: 13, low: 8, close: 9, volume: 3 },
]

describe('seriesKindFor', () => {
  it('maps every chart type to a series definition', () => {
    expect(CHART_TYPES.map(seriesKindFor)).toEqual([
      'candlestick',
      'candlestick',
      'line',
      'area',
      'candlestick',
    ])
  })
})

describe('mainSeriesData', () => {
  it('candles and hollow plot real OHLC', () => {
    for (const type of ['candles', 'hollow'] as const) {
      const { points, ha } = mainSeriesData(type, candles)
      expect(ha).toBeNull()
      expect(points[1]).toEqual({ time: H, open: 11, high: 14, low: 10, close: 13 })
    }
  })

  it('line and area plot the close', () => {
    for (const type of ['line', 'area'] as const) {
      expect(mainSeriesData(type, candles).points).toEqual([
        { time: 0, value: 11 },
        { time: H, value: 13 },
        { time: 2 * H, value: 9 },
      ])
    }
    expect(toLinePoint(candles[2])).toEqual({ time: 2 * H, value: 9 })
  })

  it('heikin-ashi plots HA bars and returns them for tail updates', () => {
    const { points, ha } = mainSeriesData('heikin-ashi', candles)
    expect(ha).toEqual(heikinAshi(candles))
    expect(points).toEqual([
      { time: 0, open: 10.5, high: 12, low: 9, close: 10.5 },
      { time: H, open: 10.5, high: 14, low: 10, close: 12 },
      { time: 2 * H, open: 11.25, high: 13, low: 8, close: 10.75 },
    ])
  })

  it('returns empty data for no candles in every mode', () => {
    for (const type of CHART_TYPES) expect(mainSeriesData(type, []).points).toEqual([])
  })
})

describe('mainSeriesTail', () => {
  const ticked: Candle[] = [
    candles[0],
    candles[1],
    { ...candles[2], close: 12, high: 13.5 },
    { time: 3 * H, open: 12, high: 12.5, low: 11, close: 12.2, volume: 1 },
  ]

  it('returns only the points from `from` on, matching a full recompute, in every mode', () => {
    for (const type of CHART_TYPES) {
      const prev = mainSeriesData(type, candles)
      const tail = mainSeriesTail(type, ticked, 2, prev.ha)
      expect(tail.points).toEqual(mainSeriesData(type, ticked).points.slice(2))
      expect(tail.points).toHaveLength(2)
    }
  })

  it('heikin-ashi reuses the unchanged HA prefix', () => {
    const prev = mainSeriesData('heikin-ashi', candles)
    const tail = mainSeriesTail('heikin-ashi', ticked, 2, prev.ha)
    expect(tail.ha?.[0]).toBe(prev.ha?.[0])
    expect(tail.ha?.[1]).toBe(prev.ha?.[1])
  })

  it('heikin-ashi falls back to a full compute without previous HA bars', () => {
    expect(mainSeriesTail('heikin-ashi', ticked, 2, null).points).toEqual(
      mainSeriesData('heikin-ashi', ticked).points.slice(2),
    )
  })
})

describe('price line', () => {
  it('only heikin-ashi needs a separate real-close price line', () => {
    expect(CHART_TYPES.filter(usesRealPriceLine)).toEqual(['heikin-ashi'])
  })

  it('tracks the real last close and direction', () => {
    expect(realPriceLine(candles)).toEqual({ price: 9, color: colors.down, visible: true })
    expect(realPriceLine(candles.slice(0, 2))).toEqual({
      price: 13,
      color: colors.up,
      visible: true,
    })
    const doji = [{ ...candles[0], close: 10 }]
    expect(realPriceLine(doji).color).toBe(colors.up)
  })

  it('uses the given palette for custom candle colours', () => {
    const palette = { up: '#2962ff', down: '#ff9800' }
    expect(realPriceLine(candles, palette).color).toBe('#ff9800')
    expect(realPriceLine(candles.slice(0, 2), palette).color).toBe('#2962ff')
  })

  it('is hidden when there are no candles', () => {
    expect(realPriceLine([]).visible).toBe(false)
  })

  it('HA hides the series label (it would show the HA close); other modes keep it', () => {
    expect(heikinAshiOptions).toMatchObject({ lastValueVisible: false, priceLineVisible: false })
    for (const o of [candlestickOptions, hollowCandlestickOptions, lineOptions, areaOptions]) {
      expect(o).toMatchObject({ lastValueVisible: true, priceLineVisible: true })
    }
  })
})

describe('hollow candle style', () => {
  it('draws rising bars as outlines and falling bars filled', () => {
    expect(hollowCandlestickOptions).toMatchObject({
      upColor: colors.transparent,
      downColor: colors.down,
      borderVisible: true,
      borderUpColor: colors.up,
      borderDownColor: colors.down,
      wickUpColor: colors.up,
      wickDownColor: colors.down,
    })
  })
})
