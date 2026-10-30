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
