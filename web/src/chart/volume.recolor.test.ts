// Phase 19 (C6): the appearance effect re-sets the volume series only when the up/down volume
// colours change, not on a theme-only or time-zone-only change.

import type { HistogramData } from 'lightweight-charts'
import { describe, expect, it } from 'vitest'
import type { Candle } from '../types'
import { DEFAULT_APPEARANCE, paletteFor, type ChartAppearance } from './theme'
import { recolorVolume, toVolumeData, volumeColorsOf, type VolumeColors } from './volume'

class FakeVolume {
  calls = 0
  data: HistogramData[] = []
  setData(d: HistogramData[]) {
    this.calls++
    this.data = d
  }
}

const candles: Candle[] = [
  { time: 60, open: 1, high: 2, low: 0.5, close: 1.5, volume: 10 },
  { time: 120, open: 1.5, high: 1.6, low: 1, close: 1.1, volume: 7 },
  { time: 180, open: 1.1, high: 1.1, low: 1.1, close: 1.1, volume: 3 },
]

/** The appearance effect's volume step, run over a sequence of appearances. */
function run(steps: ChartAppearance[]) {
  const series = new FakeVolume()
  let shown: Readonly<VolumeColors> | null = null
  const callsAfter: number[] = []
  for (const a of steps) {
    shown = recolorVolume(series, candles, shown, volumeColorsOf(paletteFor(a)))
    callsAfter.push(series.calls)
    // Whatever was skipped, the series shows exactly what a full re-set would.
    expect(series.data).toEqual(toVolumeData(candles, volumeColorsOf(paletteFor(a))))
  }
  return callsAfter
}

describe('recolorVolume', () => {
  const base = DEFAULT_APPEARANCE

  it('sets the data when nothing is known yet', () => {
    expect(run([base])).toEqual([1])
  })

  it('skips a theme-only and a time-zone-only change', () => {
    const light = { ...base, theme: 'light' as const }
    // Same candle colours in both themes -> same volume colours.
    expect(volumeColorsOf(paletteFor(light))).toEqual(volumeColorsOf(paletteFor(base)))
    expect(run([base, light, base])).toEqual([1, 1, 1])
    expect(
      run([base, { ...base, timezone: 'Asia/Tokyo' }, { ...base, timezone: 'local' }]),
    ).toEqual([1, 1, 1])
  })

  it('re-sets on an up or down colour change', () => {
    expect(
      run([
        base,
        { ...base, upColor: '#2962ff' },
        { ...base, upColor: '#2962ff', downColor: '#ff9800' },
        { ...base, upColor: '#2962ff', downColor: '#ff9800', theme: 'light' },
      ]),
    ).toEqual([1, 2, 3, 3])
  })
})
