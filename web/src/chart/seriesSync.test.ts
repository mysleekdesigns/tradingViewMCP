import { LineStyle, PriceLineSource } from 'lightweight-charts'
import { describe, expect, it } from 'vitest'
import { prependOlder } from '../data/candleStore'
import type { Candle } from '../types'
import {
  clickedBarTime,
  isNearLeftEdge,
  newestBarOffscreen,
  LEFT_EDGE_THRESHOLD_BARS,
  MAX_APPENDED_BARS,
  planSeriesSync,
  shiftLogicalRange,
  toCandlestickPoint,
  toVolumePoint,
} from './seriesSync'
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

  it('reports older history prepended with its count', () => {
    const a = base()
    expect(planSeriesSync(a, [bar(-H, 9), ...a])).toEqual({ kind: 'prepend', count: 1 })
    const older = Array.from({ length: 500 }, (_, i) => bar((i - 500) * H, 5))
    expect(planSeriesSync(a, [...older, ...a])).toEqual({ kind: 'prepend', count: 500 })
  })

  it('plans a prepend for the output of prependOlder (identity contract with the controller)', () => {
    const a = base()
    // Overlapping and out-of-order rows, as a page from the exchange may return them.
    const page = [bar(-H, 9), bar(-3 * H, 7), bar(-2 * H, 8), bar(0, 99)]
    expect(planSeriesSync(a, prependOlder(a, page))).toEqual({ kind: 'prepend', count: 3 })
  })

  it('reports a prepend when the tail also ticked or rolled over', () => {
    const a = base()
    const ticked = [bar(-2 * H, 8), bar(-H, 9), a[0], a[1], bar(2 * H, 13)]
    expect(planSeriesSync(a, ticked)).toEqual({ kind: 'prepend', count: 2 })
    const rolled = [bar(-H, 9), ...a, bar(3 * H, 14)]
    expect(planSeriesSync(a, rolled)).toEqual({ kind: 'prepend', count: 1 })
  })

  it('reports a prepend onto a single-bar series whose bar ticked', () => {
    const a = [bar(0, 10)]
    expect(planSeriesSync(a, [bar(-H, 9), bar(0, 11)])).toEqual({ kind: 'prepend', count: 1 })
  })

  it('sets data when prepended bars are not strictly ascending or overlap', () => {
    const a = base()
    expect(planSeriesSync(a, [bar(-H, 9), bar(-2 * H, 8), ...a])).toEqual({ kind: 'set' })
    expect(planSeriesSync(a, [bar(-H, 9), bar(-H, 8), ...a])).toEqual({ kind: 'set' })
  })

  it('sets data when a prepend also changed an existing middle bar', () => {
    const a = base()
    expect(planSeriesSync(a, [bar(-H, 9), a[0], bar(H, 99), a[2]])).toEqual({ kind: 'set' })
  })

  it('sets data when a prepend also appended too many bars', () => {
    const a = base()
    const extra = Array.from({ length: MAX_APPENDED_BARS + 1 }, (_, i) => bar((3 + i) * H, 20 + i))
    expect(planSeriesSync(a, [bar(-H, 9), ...a, ...extra])).toEqual({ kind: 'set' })
  })

  it('sets data when history was replaced by unrelated bars', () => {
    const a = base()
    expect(planSeriesSync(a, [bar(-H, 9), bar(10 * H, 1), bar(11 * H, 2), bar(12 * H, 3)])).toEqual(
      {
        kind: 'set',
      },
    )
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

describe('planSeriesSync under bar replay', () => {
  /** Ten closed hourly bars; replay shows prefixes of this array (same Candle objects). */
  const closed = Array.from({ length: 10 }, (_, i) => bar(i * H, 10 + i))

  it('replay start (a shorter prefix replaces the history) sets data', () => {
    expect(planSeriesSync(closed, closed.slice(0, 6))).toEqual({ kind: 'set' })
  })

  it('step back (prefix shrinks by one) sets data', () => {
    expect(planSeriesSync(closed.slice(0, 6), closed.slice(0, 5))).toEqual({ kind: 'set' })
  })

  it('step forward (prefix grows by one) updates only the new bar', () => {
    expect(planSeriesSync(closed.slice(0, 5), closed.slice(0, 6))).toEqual({
      kind: 'update',
      from: 5,
    })
  })

  it('shrink then regrow plans set, then one update per bar', () => {
    const plans = [8, 5, 6, 7, 8, 9, 10].reduce<{ prev: Candle[]; kinds: string[] }>(
      (acc, k) => {
        const next = closed.slice(0, k)
        acc.kinds.push(planSeriesSync(acc.prev, next).kind)
        return { prev: next, kinds: acc.kinds }
      },
      { prev: closed, kinds: [] },
    ).kinds
    expect(plans).toEqual(['set', 'set', 'update', 'update', 'update', 'update', 'update'])
  })

  it('a jump forward of more than MAX_APPENDED_BARS bars sets data', () => {
    expect(planSeriesSync(closed.slice(0, 2), closed.slice(0, 3 + MAX_APPENDED_BARS))).toEqual({
      kind: 'set',
    })
  })
})

describe('newestBarOffscreen (bar replay keeps the newest bar in view)', () => {
  it('is true only when the newest bar is right of the visible range', () => {
    expect(newestBarOffscreen({ from: 0, to: 8 }, 10)).toBe(true)
    expect(newestBarOffscreen({ from: 0, to: 9 }, 10)).toBe(false)
    // Right offset: empty space after the last bar.
    expect(newestBarOffscreen({ from: 3, to: 15 }, 10)).toBe(false)
  })

  it('is false without a range or bars', () => {
    expect(newestBarOffscreen(null, 10)).toBe(false)
    expect(newestBarOffscreen({ from: -5, to: -1 }, 0)).toBe(false)
  })
})

describe('clickedBarTime (onTimeClick)', () => {
  const click = {
    tool: 'cursor',
    pressConsumed: false,
    hasPoint: true,
    paneIndex: 0,
    barTime: 1_700_000_000 as unknown,
  }

  it('reports the open time of the bar under a cursor-tool click on the main pane', () => {
    expect(clickedBarTime(click)).toBe(1_700_000_000)
    expect(clickedBarTime({ ...click, paneIndex: undefined })).toBe(1_700_000_000)
  })

  it('reports null on the main pane when no bar is under the cursor', () => {
    expect(clickedBarTime({ ...click, barTime: undefined })).toBeNull()
  })

  it('reports nothing for drawing tools, drawing hits, indicator panes, or off-pane clicks', () => {
    expect(clickedBarTime({ ...click, tool: 'trendline' })).toBeUndefined()
    expect(clickedBarTime({ ...click, pressConsumed: true })).toBeUndefined()
    expect(clickedBarTime({ ...click, paneIndex: 1 })).toBeUndefined()
    expect(clickedBarTime({ ...click, hasPoint: false })).toBeUndefined()
  })
})

describe('left edge', () => {
  it('fires within the threshold of the oldest bar, including empty space on the left', () => {
    expect(isNearLeftEdge({ from: LEFT_EDGE_THRESHOLD_BARS - 0.5, to: 200 })).toBe(true)
    expect(isNearLeftEdge({ from: -20, to: 40 })).toBe(true)
    expect(isNearLeftEdge({ from: LEFT_EDGE_THRESHOLD_BARS, to: 200 })).toBe(false)
    expect(isNearLeftEdge({ from: 300, to: 400 })).toBe(false)
  })

  it('does not fire without a visible range', () => {
    expect(isNearLeftEdge(null)).toBe(false)
  })

  it('honours a custom threshold', () => {
    expect(isNearLeftEdge({ from: 8, to: 50 }, 10)).toBe(true)
    expect(isNearLeftEdge({ from: 12, to: 50 }, 10)).toBe(false)
  })

  it('shifts the logical range right by the prepended count so the same bars stay visible', () => {
    expect(shiftLogicalRange({ from: 3.5, to: 120.5 }, 500)).toEqual({ from: 503.5, to: 620.5 })
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
