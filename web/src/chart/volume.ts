import type { CandlestickData, HistogramData, UTCTimestamp } from 'lightweight-charts'
import type { Candle } from '../types'

export interface VolumeColors {
  up: string
  down: string
}

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

/**
 * Map candles to histogram points. A bar is "up" when close >= open (a doji counts as up,
 * matching the candlestick series colouring).
 */
export function toVolumeData(candles: readonly Candle[], palette: VolumeColors): HistogramData[] {
  return candles.map((c) => ({
    time: c.time as UTCTimestamp,
    value: c.volume,
    color: c.close >= c.open ? palette.up : palette.down,
  }))
}

/** Map candles (UNIX seconds) to Lightweight Charts candlestick points. */
export function toCandlestickData(candles: readonly Candle[]): CandlestickData[] {
  return candles.map((c) => ({
    time: c.time as UTCTimestamp,
    open: c.open,
    high: c.high,
    low: c.low,
    close: c.close,
  }))
}
