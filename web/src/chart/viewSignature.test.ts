import type { IChartApi, ISeriesApi, SeriesType } from 'lightweight-charts'
import { describe, expect, it } from 'vitest'
import { barSpacingOf, sameViewSignature, viewSignature } from './viewSignature'

function fakes(state: { x0: number | null; spacing: number; width: number; scale: number }) {
  const ts = {
    logicalToCoordinate: (l: number) => (state.x0 === null ? null : state.x0 + l * state.spacing),
    width: () => state.width,
  }
  const chart = { timeScale: () => ts } as unknown as IChartApi
  const series = {
    priceToCoordinate: (p: number) => 500 - p * state.scale,
  } as unknown as ISeriesApi<SeriesType>
  return { chart, series, ts }
}

describe('viewSignature', () => {
  it('bar spacing comes from two logical coordinates', () => {
    const { ts } = fakes({ x0: 10, spacing: 6, width: 800, scale: 1 })
    expect(barSpacingOf(ts as never)).toBe(6)
    const none = fakes({ x0: null, spacing: 6, width: 800, scale: 1 })
    expect(barSpacingOf(none.ts as never)).toBe(0)
  })

  it('an unchanged view gives an equal signature; scroll, zoom, width, price scale and version each change it', () => {
    const state = { x0: 10 as number | null, spacing: 6, width: 800, scale: 1 }
    const { chart, series } = fakes(state)
    const sig = () => viewSignature(chart, series, [100, 200], 1)
    const a = sig()
    expect(sameViewSignature(a, sig())).toBe(true)
    for (const change of [
      () => (state.x0 = 11),
      () => (state.spacing = 7),
      () => (state.width = 801),
      () => (state.scale = 1.5),
    ]) {
      const before = sig()
      change()
      expect(sameViewSignature(before, sig())).toBe(false)
    }
    const b = sig()
    expect(sameViewSignature(b, viewSignature(chart, series, [100, 200], 2))).toBe(false)
  })

  it('no coordinates means no signature, and null never matches', () => {
    const { chart, series } = fakes({ x0: null, spacing: 6, width: 800, scale: 1 })
    expect(viewSignature(chart, series, [100, 200], 0)).toBeNull()
    expect(sameViewSignature(null, null)).toBe(false)
  })
})
