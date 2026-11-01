import type { HistogramData } from 'lightweight-charts'
import type { Candle } from '../types'
import { toVolumeData, type VolumeColors } from './points'

// The candle → point mappers live in points.ts; re-exported here for existing importers.
export { toCandlestickData, toVolumeData, type VolumeColors } from './points'

/** The volume fields of a chart palette (`ChartPalette` satisfies it). */
export interface VolumePaletteSource {
  volumeUp: string
  volumeDown: string
}

const volumeColorsCache = new WeakMap<VolumePaletteSource, Readonly<VolumeColors>>()

/**
 * Volume bar colours of a palette, one shared object per palette object (no allocation per
 * volume point on a live tick). Re-derived if the palette's fields no longer match the cached
 * pair, so an edited palette can never draw stale colours. Treat the result as read-only.
 */
export function volumeColorsOf(p: VolumePaletteSource): Readonly<VolumeColors> {
  const cached = volumeColorsCache.get(p)
  if (cached !== undefined && cached.up === p.volumeUp && cached.down === p.volumeDown)
    return cached
  const colors = Object.freeze({ up: p.volumeUp, down: p.volumeDown })
  volumeColorsCache.set(p, colors)
  return colors
}

/** The parts of the volume series a recolour uses (an LWC histogram series fits). */
export interface VolumeSeriesTarget {
  setData(data: HistogramData[]): void
}

/**
 * Recolour the volume series for `next` only when its up/down colours differ from the colours
 * it shows (`shown`, null = unknown): a theme or time-zone change that keeps the candle colours
 * costs no re-map and no `setData`. Returns the colours the series shows afterwards.
 */
export function recolorVolume(
  series: VolumeSeriesTarget,
  candles: readonly Candle[],
  shown: Readonly<VolumeColors> | null,
  next: Readonly<VolumeColors>,
): Readonly<VolumeColors> {
  if (shown !== null && shown.up === next.up && shown.down === next.down) return shown
  series.setData(toVolumeData(candles, next))
  return next
}
