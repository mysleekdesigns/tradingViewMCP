import * as lwc from 'lightweight-charts'
import { describe, expect, it } from 'vitest'

describe('toolchain', () => {
  it('has the Lightweight Charts v5 series API', () => {
    expect(lwc.version().startsWith('5.')).toBe(true)
    expect(lwc.CandlestickSeries).toBeDefined()
    expect(lwc.HistogramSeries).toBeDefined()
  })
})
