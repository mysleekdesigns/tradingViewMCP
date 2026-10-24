import { LineStyle, PriceLineSource } from 'lightweight-charts'
import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { MAX_APPENDED_BARS, planSeriesSync, toCandlestickPoint, toVolumePoint } from './seriesSync'
import { candlestickOptions, volumeOptions } from './theme'
import { toCandlestickData, toVolumeData } from './volume'

const H = 3600

function bar(time: number, close: number, open = close - 1): Candle {
  return {
    time,
    open,
    high: Math.max(open, close) + 1,
    low: Math.min(open, close) - 1,
    close,
    volume: 7,
  }
}

const base = (): Candle[] => [bar(0, 10), bar(H, 11), bar(2 * H, 12)]

describe('planSeriesSync', () => {
  it('sets data on the first render', () => {
    expect(planSeriesSync(null, base())).toEqual({ kind: 'set' })
  })

  it('sets data when the previous array was empty', () => {
    expect(planSeriesSync([], base())).toEqual({ kind: 'set' })
  })

  it('does nothing for the same array', () => {
    const a = base()
    expect(planSeriesSync(a, a)).toEqual({ kind: 'none' })
  })

  it('does nothing for a copy with identical elements', () => {
    const a = base()
    expect(planSeriesSync(a, a.slice())).toEqual({ kind: 'none' })
  })

  it('updates only the last bar when it is replaced with the same time', () => {
    const a = base()
    const b = [...a.slice(0, 2), bar(2 * H, 13)]
    expect(planSeriesSync(a, b)).toEqual({ kind: 'update', from: 2 })
  })

  it('updates last and appended bar when a tick rolls over', () => {
    const a = base()
    const b = [...a.slice(0, 2), bar(2 * H, 13), bar(3 * H, 14)]
    expect(planSeriesSync(a, b)).toEqual({ kind: 'update', from: 2 })
  })

  it('starts after the old last bar when it kept its identity', () => {
    const a = base()
    const b = [...a, bar(3 * H, 14)]
    expect(planSeriesSync(a, b)).toEqual({ kind: 'update', from: 3 })
  })

  it('allows up to MAX_APPENDED_BARS appended bars', () => {
    const a = base()
    const extra = Array.from({ length: MAX_APPENDED_BARS }, (_, i) => bar((3 + i) * H, 20 + i))
    expect(planSeriesSync(a, [...a, ...extra])).toEqual({ kind: 'update', from: 3 })
  })

  it('sets data when more than MAX_APPENDED_BARS bars are appended', () => {
    const a = base()
    const extra = Array.from({ length: MAX_APPENDED_BARS + 1 }, (_, i) => bar((3 + i) * H, 20 + i))
    expect(planSeriesSync(a, [...a, ...extra])).toEqual({ kind: 'set' })
  })

  it('sets data when an earlier bar changed identity', () => {
    const a = base()
    const b = [a[0], bar(H, 99), a[2]]
    expect(planSeriesSync(a, b)).toEqual({ kind: 'set' })
  })

  it('sets data when bars were removed', () => {
    const a = base()
    expect(planSeriesSync(a, a.slice(0, 2))).toEqual({ kind: 'set' })
  })

  it('sets data when older history was prepended', () => {
    const a = base()
    expect(planSeriesSync(a, [bar(-H, 9), ...a])).toEqual({ kind: 'set' })
  })

  it('sets data when the last bar was replaced by one with a different time', () => {
    const a = base()
    const b = [a[0], a[1], bar(2 * H + 60, 12)]
    expect(planSeriesSync(a, b)).toEqual({ kind: 'set' })
  })

  it('sets data when appended bars are not strictly ascending', () => {
    const a = base()
    expect(planSeriesSync(a, [...a, bar(4 * H, 1), bar(3 * H, 2)])).toEqual({ kind: 'set' })
    expect(planSeriesSync(a, [...a, bar(2 * H, 1)])).toEqual({ kind: 'set' })
  })
})

describe('single-candle mappers', () => {
  const palette = { up: 'U', down: 'D' }

  it('match the array mappers element for element', () => {
    const candles = [bar(0, 10, 9), bar(H, 9, 10), bar(2 * H, 10, 10)]
    expect(candles.map(toCandlestickPoint)).toEqual(toCandlestickData(candles))
    expect(candles.map((c) => toVolumePoint(c, palette))).toEqual(toVolumeData(candles, palette))
  })

  it('colours a doji volume bar as up', () => {
    expect(toVolumePoint(bar(0, 10, 10), palette).color).toBe('U')
  })
})

describe('live price line options', () => {
  it('shows the last price label and a dotted line from the last bar', () => {
    expect(candlestickOptions).toMatchObject({
      lastValueVisible: true,
      priceLineVisible: true,
      priceLineSource: PriceLineSource.LastBar,
      priceLineStyle: LineStyle.Dotted,
      priceLineWidth: 1,
    })
    // '' lets Lightweight Charts colour the line and label by the last bar's direction.
    expect(candlestickOptions.priceLineColor ?? '').toBe('')
  })

  it('keeps the volume series free of its own price line and label', () => {
    expect(volumeOptions).toMatchObject({ lastValueVisible: false, priceLineVisible: false })
  })
})
