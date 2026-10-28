import type { HoveredInfo, ISeriesApi, SeriesType } from 'lightweight-charts'
import { describe, expect, it } from 'vitest'
import { drawingObjectId, resolveHover, sameHover, type HoverTarget } from './hover'

// Plain object fakes: the resolver only compares series by identity.
const rsiLine = { name: 'rsi' } as unknown as ISeriesApi<SeriesType>
const macdHist = { name: 'macd-hist' } as unknown as ISeriesApi<SeriesType>
const mainSeries = { name: 'candles' } as unknown as ISeriesApi<SeriesType>

const uids = new Map<ISeriesApi<SeriesType>, string>([
  [rsiLine, 'rsi-1'],
  [macdHist, 'macd-1'],
])
const uidOf = (s: ISeriesApi<SeriesType>) => uids.get(s) ?? null

function info(partial: Partial<HoveredInfo>): { hoveredInfo: HoveredInfo } {
  return {
    hoveredInfo: { type: 'series-line', sourceKind: 'series', objectKind: 'series', ...partial },
  }
}

describe('resolveHover', () => {
  it('nothing hovered is null', () => {
    expect(resolveHover({}, uidOf)).toBeNull()
    expect(resolveHover({ hoveredInfo: undefined }, uidOf)).toBeNull()
  })

  it('an indicator line resolves to its config uid', () => {
    expect(resolveHover(info({ series: rsiLine }), uidOf)).toEqual({
      kind: 'indicator',
      uid: 'rsi-1',
    })
  })

  it('an indicator histogram bar resolves to its config uid', () => {
    expect(
      resolveHover(info({ type: 'series-range', series: macdHist, paneIndex: 2 }), uidOf),
    ).toEqual({ kind: 'indicator', uid: 'macd-1' })
  })

  it('the main series (not an indicator) is null', () => {
    expect(resolveHover(info({ type: 'series-range', series: mainSeries }), uidOf)).toBeNull()
  })

  it('a drawing primitive resolves to the drawing id', () => {
    const hovered = info({
      type: 'primitive',
      sourceKind: 'series-primitive',
      objectKind: 'primitive',
      series: mainSeries,
      objectId: drawingObjectId('abc-123'),
    })
    expect(resolveHover(hovered, uidOf)).toEqual({ kind: 'drawing', id: 'abc-123' })
  })

  it('a drawing id containing the prefix keeps everything after the first prefix', () => {
    const hovered = info({ sourceKind: 'series-primitive', objectId: 'drawing:drawing:x' })
    expect(resolveHover(hovered, uidOf)).toEqual({ kind: 'drawing', id: 'drawing:x' })
  })

  it('a range indicator primitive (volume profile) resolves to its config uid', () => {
    const hovered = info({
      sourceKind: 'series-primitive',
      objectKind: 'primitive',
      objectId: 'indicator:vprofile-1',
    })
    expect(resolveHover(hovered, uidOf)).toEqual({ kind: 'indicator', uid: 'vprofile-1' })
    expect(
      resolveHover(info({ sourceKind: 'series-primitive', objectId: 'indicator:' }), uidOf),
    ).toBeNull()
  })

  it('a primitive over an indicator series is not taken for the indicator', () => {
    // An indicator series' own primitive (sourceKind 'series-primitive') must not highlight it.
    const hovered = info({
      sourceKind: 'series-primitive',
      objectKind: 'primitive',
      series: rsiLine,
    })
    expect(resolveHover(hovered, uidOf)).toBeNull()
  })

  it('other primitives, a tool-mode hit, or non-string ids are null', () => {
    expect(
      resolveHover(info({ sourceKind: 'pane-primitive', objectId: 'watermark' }), uidOf),
    ).toBeNull()
    expect(
      resolveHover(info({ sourceKind: 'series-primitive', objectId: 'drawings' }), uidOf),
    ).toBeNull()
    expect(
      resolveHover(info({ sourceKind: 'series-primitive', objectId: 'drawing:' }), uidOf),
    ).toBeNull()
    expect(resolveHover(info({ sourceKind: 'series-primitive', objectId: 42 }), uidOf)).toBeNull()
  })
})

describe('sameHover', () => {
  const a: HoverTarget = { kind: 'indicator', uid: 'x' }
  it('compares kind and id', () => {
    expect(sameHover(null, null)).toBe(true)
    expect(sameHover(a, null)).toBe(false)
    expect(sameHover(a, { kind: 'indicator', uid: 'x' })).toBe(true)
    expect(sameHover(a, { kind: 'indicator', uid: 'y' })).toBe(false)
    expect(sameHover(a, { kind: 'drawing', id: 'x' })).toBe(false)
    expect(sameHover({ kind: 'drawing', id: 'x' }, { kind: 'drawing', id: 'x' })).toBe(true)
  })
})
