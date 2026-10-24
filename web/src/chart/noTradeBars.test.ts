// Chart behaviour when bars without trades are not plotted (thin Binance.US pairs).

import type { SeriesMarker, UTCTimestamp } from 'lightweight-charts'
import { describe, expect, it } from 'vitest'
import { plottableCandles } from '../data/plottable'
import type { Candle } from '../types'
import { tradeFocusable } from '../components/backtestView'
import type { LabTrade } from '../data/labTypes'
import { buildTradeMarkers, snapMarkersToBars, tradesOnChart } from './backtestOverlay'
import { logicalToTime, timeToLogical, TimeIndex } from './drawings/timeIndex'
import {
  isExpectedRange,
  MAX_AUTO_OLDER_PAGES,
  nextAutoOlder,
  shiftLogicalRange,
  type AutoOlder,
} from './seriesSync'

const M = 60

function bar(time: number, volume: number): Candle {
  return { time, open: 1, high: 1, low: 1, close: 1, volume }
}

describe('nextAutoOlder', () => {
  const at = (start: number | null, pages = 0): AutoOlder => ({ start, pages })

  it('remembers the first state without requesting', () => {
    expect(nextAutoOlder(null, 1000, true)).toEqual({ state: at(1000), request: false })
  })

  it('does nothing while the history start is unchanged (live ticks)', () => {
    const prev = at(1000, 2)
    expect(nextAutoOlder(prev, 1000, true)).toEqual({ state: prev, request: false })
  })

  it('asks for the next page after a page landed while the view is at the left edge', () => {
    expect(nextAutoOlder(at(1000), 400, true)).toEqual({ state: at(400, 1), request: true })
  })

  it('does not ask once the view has left the left edge (a liquid page shifted it)', () => {
    expect(nextAutoOlder(at(1000), 400, false)).toEqual({ state: at(400, 0), request: false })
  })

  it('counts every self-requested page, empty or not, and stops at the cap', () => {
    // A pair that trades about once a day: each page adds a bar or none, and the few plotted
    // bars never fill the view, so it stays "near the left edge" throughout.
    let state = at(1_000_000)
    let requests = 0
    for (let page = 1; page <= 50; page++) {
      const r = nextAutoOlder(state, 1_000_000 - page * 60_000, true)
      state = r.state
      if (r.request) requests += 1
    }
    expect(requests).toBe(MAX_AUTO_OLDER_PAGES)
    expect(state.pages).toBe(MAX_AUTO_OLDER_PAGES)
  })

  it('a user move (pages reset to 0) allows a new run', () => {
    const capped = at(400, MAX_AUTO_OLDER_PAGES)
    expect(nextAutoOlder(capped, 300, true).request).toBe(false)
    expect(nextAutoOlder({ ...capped, pages: 0 }, 300, true).request).toBe(true)
  })

  it('resets without requesting when the history start moves forward (a replaced window)', () => {
    expect(nextAutoOlder(at(1000, 4), 5000, true)).toEqual({ state: at(5000, 0), request: false })
  })
})

describe('isExpectedRange', () => {
  it('matches the range the chart set after a prepend', () => {
    const expected = shiftLogicalRange({ from: 3.5, to: 120.5 }, 40)
    expect(isExpectedRange({ from: 43.5, to: 160.5 }, expected)).toBe(true)
  })

  it('tolerates float rounding from LWC recomputing the range', () => {
    expect(isExpectedRange({ from: 43.500001, to: 160.49999 }, { from: 43.5, to: 160.5 })).toBe(
      true,
    )
  })

  it('does not match a user scroll or zoom, or when nothing is expected', () => {
    expect(isExpectedRange({ from: 41, to: 158 }, { from: 43.5, to: 160.5 })).toBe(false)
    expect(isExpectedRange({ from: 43.5, to: 160.5 }, null)).toBe(false)
    expect(isExpectedRange(null, { from: 43.5, to: 160.5 })).toBe(false)
  })
})

describe('trades on chart count from the first plotted bar', () => {
  const trade = (entry: number, exit: number): LabTrade => ({
    entry_time: entry,
    entry_price: 1,
    exit_time: exit,
    exit_price: 1,
    pnl: 1,
    return_pct: 0.01,
    exit_reason: 'signal',
  })
  // Raw bars from 0; the first traded (plotted) bar is at 5M.
  const plottedTimes = [5 * M, 6 * M, 8 * M, 9 * M]
  const trades = [trade(2 * M, 3 * M), trade(6 * M, 7 * M), trade(8 * M, 9 * M)]

  it('matches the entry markers actually drawn on the plotted bars', () => {
    const drawn = snapMarkersToBars(buildTradeMarkers(trades), plottedTimes).filter(
      (m) => m.shape === 'arrowUp',
    )
    expect(tradesOnChart(trades, plottedTimes[0])).toBe(drawn.length)
    expect(drawn.length).toBe(2)
  })

  it('the raw first bar would over-count trades whose markers are not drawn', () => {
    expect(tradesOnChart(trades, 0)).toBe(3)
  })

  it('a trade before the first plotted bar is not focusable; one after it is', () => {
    expect(tradeFocusable(trades[0], true, plottedTimes[0])).toBe(false)
    expect(tradeFocusable(trades[1], true, plottedTimes[0])).toBe(true)
  })
})

describe('snapMarkersToBars', () => {
  const marker = (time: number, id: string): SeriesMarker<UTCTimestamp> => ({
    id,
    time: time as UTCTimestamp,
    position: 'belowBar',
    shape: 'arrowUp',
    color: '#000',
  })

  it('keeps a marker on a plotted bar as the same object', () => {
    const m = marker(200, 'a')
    expect(snapMarkersToBars([m], [100, 200, 300])[0]).toBe(m)
  })

  it('moves a marker on a bar without trades to the next plotted bar', () => {
    const out = snapMarkersToBars([marker(250, 'a')], [100, 200, 400])
    expect(out.map((m) => [m.id, m.time])).toEqual([['a', 400]])
  })

  it('drops markers before the first or after the last plotted bar', () => {
    const out = snapMarkersToBars(
      [marker(50, 'a'), marker(200, 'b'), marker(500, 'c')],
      [100, 200, 300],
    )
    expect(out.map((m) => m.id)).toEqual(['b'])
  })

  it('stays sorted by time and keeps marker order', () => {
    const out = snapMarkersToBars(
      [marker(100, 'a'), marker(150, 'b'), marker(200, 'c'), marker(250, 'd')],
      [100, 200, 300],
    )
    expect(out.map((m) => [m.id, m.time])).toEqual([
      ['a', 100],
      ['b', 200],
      ['c', 200],
      ['d', 300],
    ])
  })

  it('is empty with no plotted bars', () => {
    expect(snapMarkersToBars([marker(100, 'a')], [])).toEqual([])
  })
})

describe('drawing anchors on a bar that is not plotted', () => {
  // Raw 1m bars 0..5; bars 2 and 3 had no trades, so plotted = 0, 1, 4, 5.
  const raw = [bar(0, 1), bar(M, 1), bar(2 * M, 0), bar(3 * M, 0), bar(4 * M, 1), bar(5 * M, 1)]
  const plotted = plottableCandles(raw)

  it('maps between the neighbouring plotted bars instead of vanishing or jumping', () => {
    // 2M sits a third of the way from bar 1 (1M) to bar 2 (4M).
    expect(timeToLogical(plotted, 2 * M, M)).toBeCloseTo(1 + 1 / 3, 12)
    expect(timeToLogical(plotted, 3 * M, M)).toBeCloseTo(1 + 2 / 3, 12)
  })

  it('keeps anchors on plotted bars on whole bar indices', () => {
    expect(timeToLogical(plotted, M, M)).toBe(1)
    expect(timeToLogical(plotted, 4 * M, M)).toBe(2)
  })

  it('round-trips the hidden time exactly (moving and saving does not drift it)', () => {
    const logical = timeToLogical(plotted, 3 * M, M)
    expect(logical).not.toBeNull()
    expect(logicalToTime(plotted, logical as number, M)).toBe(3 * M)
  })

  it('stays between the same neighbours when those bars are what the chart index holds', () => {
    const index = new TimeIndex(plotted, M)
    const l = index.timeToLogical(2 * M) as number
    expect(l).toBeGreaterThan(1)
    expect(l).toBeLessThan(2)
  })

  it('an anchor on an older hidden bar left of the first plotted bar extrapolates by the interval', () => {
    const thin = plottableCandles([bar(0, 0), bar(M, 0), bar(2 * M, 1), bar(3 * M, 1)])
    expect(timeToLogical(thin, 0, M)).toBe(-2)
  })
})
