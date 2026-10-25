import {
  ColorType,
  CrosshairMode,
  LineStyle,
  PriceLineSource,
  type AreaSeriesPartialOptions,
  type CandlestickSeriesPartialOptions,
  type CreatePriceLineOptions,
  type DeepPartial,
  type HistogramSeriesPartialOptions,
  type LineSeriesPartialOptions,
  type TextWatermarkOptions,
  type TimeChartOptions,
} from 'lightweight-charts'
import type { ThemeName } from '../state/settings'
import type { ChartType } from '../types'
import { makeTickMarkFormatter, makeTimeFormatter } from './timeFormat'

/** Every colour the chart draws with (series, scales, crosshair, watermark). */
export interface ChartPalette {
  background: string
  grid: string
  border: string
  text: string
  mutedText: string
  up: string
  down: string
  /** Volume bars use the candle colours at ~45% alpha. */
  volumeUp: string
  volumeDown: string
  /** Line and area chart stroke (TradingView blue). */
  line: string
  areaTop: string
  areaBottom: string
  transparent: string
  crosshair: string
  crosshairLabel: string
  watermark: string
}

/** What Settings controls about the chart's look. Bars stay UTC-aligned in every zone. */
export interface ChartAppearance {
  theme: ThemeName
  /** '#rrggbb' */
  upColor: string
  /** '#rrggbb' */
  downColor: string
  /** 'UTC' | 'local' | an IANA zone */
  timezone: string
}

export const DEFAULT_APPEARANCE: Readonly<ChartAppearance> = Object.freeze({
  theme: 'dark',
  upColor: '#089981',
  downColor: '#f23645',
  timezone: 'UTC',
})

/** Volume bars: candle colour at 0x73 / 255 ≈ 45% alpha. */
export const VOLUME_ALPHA = 0x73 / 255

/** '#rrggbb' + alpha → '#rrggbbaa'. Any other colour string is returned unchanged. */
export function withHexAlpha(color: string, alpha: number): string {
  if (!/^#[0-9a-f]{6}$/i.test(color)) return color
  const a = Math.round(Math.min(1, Math.max(0, alpha)) * 255)
  return `${color.toLowerCase()}${a.toString(16).padStart(2, '0')}`
}

type BasePalette = Omit<ChartPalette, 'up' | 'down' | 'volumeUp' | 'volumeDown'>

/** Keep in sync with the CSS tokens in index.css (`:root` and `:root[data-theme='light']`). */
const BASE_PALETTES: Readonly<Record<ThemeName, BasePalette>> = {
  dark: {
    background: '#131722',
    grid: '#1c2030',
    border: '#2a2e39',
    text: '#b2b5be',
    mutedText: '#787b86',
    line: '#2962ff',
    areaTop: 'rgba(41, 98, 255, 0.28)',
    areaBottom: 'rgba(41, 98, 255, 0)',
    transparent: 'rgba(0, 0, 0, 0)',
    crosshair: '#9598a1',
    crosshairLabel: '#363a45',
    watermark: 'rgba(178, 181, 190, 0.06)',
  },
  light: {
    background: '#ffffff',
    grid: '#f0f3fa',
    border: '#e0e3eb',
    text: '#131722',
    mutedText: '#787b86',
    line: '#2962ff',
    areaTop: 'rgba(41, 98, 255, 0.22)',
    areaBottom: 'rgba(41, 98, 255, 0)',
    transparent: 'rgba(0, 0, 0, 0)',
    crosshair: '#9598a1',
    crosshairLabel: '#131722',
    watermark: 'rgba(19, 23, 34, 0.06)',
  },
}

const HEX6 = /^#[0-9a-f]{6}$/i

/** The chart palette for a theme and candle colours (invalid colours fall back to defaults). */
export function paletteFor(a: ChartAppearance): ChartPalette {
  const base = BASE_PALETTES[a.theme] ?? BASE_PALETTES.dark
  const up = HEX6.test(a.upColor) ? a.upColor.toLowerCase() : DEFAULT_APPEARANCE.upColor
  const down = HEX6.test(a.downColor) ? a.downColor.toLowerCase() : DEFAULT_APPEARANCE.downColor
  return {
    ...base,
    up,
    down,
    volumeUp: withHexAlpha(up, VOLUME_ALPHA),
    volumeDown: withHexAlpha(down, VOLUME_ALPHA),
  }
}

/** The dark default palette. Kept for code that does not follow Settings. */
export const colors: Readonly<ChartPalette> = Object.freeze(paletteFor(DEFAULT_APPEARANCE))

export const fallbackFontFamily = "-apple-system, 'Segoe UI', sans-serif"
export const fontFamily = `'IBM Plex Sans', ${fallbackFontFamily}`
/** Font descriptors `document.fonts.load` must resolve before the chart switches to `fontFamily`. */
export const webFonts = ["11px 'IBM Plex Sans'", "600 72px 'IBM Plex Sans'"] as const

/**
 * Only the options an appearance change touches: colours and time formatting. Safe for
 * `chart.applyOptions` at any time (no bar spacing, offsets, or fonts that would reset the view).
 */
export function chartThemeOptionsFor(a: ChartAppearance): DeepPartial<TimeChartOptions> {
  const p = paletteFor(a)
  return {
    layout: {
      background: { type: ColorType.Solid, color: p.background },
      textColor: p.text,
    },
    grid: {
      vertLines: { color: p.grid },
      horzLines: { color: p.grid },
    },
    crosshair: {
      vertLine: { color: p.crosshair, labelBackgroundColor: p.crosshairLabel },
      horzLine: { color: p.crosshair, labelBackgroundColor: p.crosshairLabel },
    },
    rightPriceScale: { borderColor: p.border },
    timeScale: { borderColor: p.border, tickMarkFormatter: makeTickMarkFormatter(a.timezone) },
    localization: { timeFormatter: makeTimeFormatter(a.timezone) },
  }
}

/** Full options for `createChart`: layout, scales, crosshair, and the appearance. */
export function chartOptionsFor(a: ChartAppearance): DeepPartial<TimeChartOptions> {
  const p = paletteFor(a)
  return {
    autoSize: true,
    layout: {
      background: { type: ColorType.Solid, color: p.background },
      textColor: p.text,
      // Start on system fonts; Chart switches to `fontFamily` once IBM Plex has loaded.
      fontFamily: fallbackFontFamily,
      fontSize: 11,
    },
    grid: {
      vertLines: { color: p.grid },
      horzLines: { color: p.grid },
    },
    crosshair: {
      mode: CrosshairMode.Normal,
      vertLine: {
        color: p.crosshair,
        style: LineStyle.Dashed,
        width: 1,
        labelBackgroundColor: p.crosshairLabel,
      },
      horzLine: {
        color: p.crosshair,
        style: LineStyle.Dashed,
        width: 1,
        labelBackgroundColor: p.crosshairLabel,
      },
    },
    rightPriceScale: { borderColor: p.border, scaleMargins: { top: 0.12, bottom: 0.24 } },
    timeScale: {
      borderColor: p.border,
      timeVisible: true,
      secondsVisible: false,
      rightOffset: 6,
      barSpacing: 8,
      tickMarkFormatter: makeTickMarkFormatter(a.timezone),
    },
    localization: { timeFormatter: makeTimeFormatter(a.timezone) },
  }
}

/** Dark, UTC defaults. */
export const chartOptions: DeepPartial<TimeChartOptions> = chartOptionsFor(DEFAULT_APPEARANCE)

/** Live price label + dotted line shared by every main-series type. */
const livePriceLine = {
  lastValueVisible: true,
  priceLineVisible: true,
  priceLineSource: PriceLineSource.LastBar,
  priceLineStyle: LineStyle.Dotted,
  priceLineWidth: 1,
} as const

export function candlestickOptionsFor(p: ChartPalette): CandlestickSeriesPartialOptions {
  return {
    upColor: p.up,
    downColor: p.down,
    borderVisible: false,
    // Kept in step with the body colours so a switch from hollow never leaves stale borders.
    borderUpColor: p.up,
    borderDownColor: p.down,
    wickUpColor: p.up,
    wickDownColor: p.down,
    // Live price: label on the price scale plus a dotted line across the pane, as on TradingView.
    // An empty priceLineColor (the default) colours both by the last bar's direction.
    ...livePriceLine,
  }
}

/**
 * Hollow candles, TradingView style: rising bars are outlined (transparent body, coloured
 * border and wick), falling bars are filled.
 */
export function hollowCandlestickOptionsFor(p: ChartPalette): CandlestickSeriesPartialOptions {
  return {
    ...candlestickOptionsFor(p),
    upColor: p.transparent,
    downColor: p.down,
    borderVisible: true,
    borderUpColor: p.up,
    borderDownColor: p.down,
    wickUpColor: p.up,
    wickDownColor: p.down,
  }
}

/**
 * Heikin-Ashi draws synthetic bars, so the series' own last-value label would show the HA close.
 * It is hidden; Chart draws a separate price line at the real last close instead
 * (see `realPriceLine` in chartTypes.ts).
 */
export function heikinAshiOptionsFor(p: ChartPalette): CandlestickSeriesPartialOptions {
  return { ...candlestickOptionsFor(p), lastValueVisible: false, priceLineVisible: false }
}

export function lineOptionsFor(p: ChartPalette): LineSeriesPartialOptions {
  return {
    ...livePriceLine,
    color: p.line,
    lineWidth: 2,
    crosshairMarkerRadius: 4,
    crosshairMarkerBorderColor: p.background,
    crosshairMarkerBackgroundColor: p.line,
  }
}

export function areaOptionsFor(p: ChartPalette): AreaSeriesPartialOptions {
  return {
    ...livePriceLine,
    lineColor: p.line,
    topColor: p.areaTop,
    bottomColor: p.areaBottom,
    lineWidth: 2,
    crosshairMarkerRadius: 4,
    crosshairMarkerBorderColor: p.background,
    crosshairMarkerBackgroundColor: p.line,
  }
}

/** Options for the main series of a chart type (what `addSeries` and an appearance change use). */
export function mainSeriesOptionsFor(
  type: ChartType,
  p: ChartPalette,
): CandlestickSeriesPartialOptions | LineSeriesPartialOptions | AreaSeriesPartialOptions {
  switch (type) {
    case 'candles':
      return candlestickOptionsFor(p)
    case 'hollow':
      return hollowCandlestickOptionsFor(p)
    case 'heikin-ashi':
      return heikinAshiOptionsFor(p)
    case 'line':
      return lineOptionsFor(p)
    case 'area':
      return areaOptionsFor(p)
  }
}

/* Dark defaults, kept for existing imports. */
export const candlestickOptions: CandlestickSeriesPartialOptions = candlestickOptionsFor(colors)
export const hollowCandlestickOptions: CandlestickSeriesPartialOptions =
  hollowCandlestickOptionsFor(colors)
export const heikinAshiOptions: CandlestickSeriesPartialOptions = heikinAshiOptionsFor(colors)
export const lineOptions: LineSeriesPartialOptions = lineOptionsFor(colors)
export const areaOptions: AreaSeriesPartialOptions = areaOptionsFor(colors)

/** Base options for the real-close price line used in Heikin-Ashi mode (price/colour set per tick). */
export const realPriceLineOptions: CreatePriceLineOptions = {
  price: 0,
  color: colors.up,
  lineWidth: 1,
  lineStyle: LineStyle.Dotted,
  lineVisible: false,
  axisLabelVisible: false,
  title: '',
}

/** Volume sits on its own overlay price scale (id ''), pinned to the bottom 20% of the pane. */
export const volumeOptions: HistogramSeriesPartialOptions = {
  priceScaleId: '',
  priceFormat: { type: 'volume' },
  lastValueVisible: false,
  priceLineVisible: false,
}

export const volumeScaleMargins = { top: 0.8, bottom: 0 } as const

export function watermarkOptions(
  text: string,
  family: string = fontFamily,
  palette: Pick<ChartPalette, 'watermark'> = colors,
): DeepPartial<TextWatermarkOptions> {
  return {
    horzAlign: 'center',
    vertAlign: 'center',
    lines: [{ text, color: palette.watermark, fontSize: 72, fontStyle: '600', fontFamily: family }],
  }
}

/* ---------- Indicators ---------- */

/** MACD-style histogram colours (TradingView defaults). */
export const histogramColors = {
  /** value >= 0 and rising (>= previous bar). Replaced by the user's histogram colour. */
  positiveRising: '#26a69a',
  positiveFalling: '#b2dfdb',
  negativeFalling: '#ff5252',
  negativeRising: '#ffcdd2',
} as const

/** Shared by every indicator line: small coloured label on the scale, no price line. */
export const indicatorLineBase: LineSeriesPartialOptions = {
  lastValueVisible: true,
  priceLineVisible: false,
  crosshairMarkerRadius: 3,
  crosshairMarkerBorderColor: colors.background,
  title: '',
}

export const indicatorHistogramBase: HistogramSeriesPartialOptions = {
  lastValueVisible: true,
  priceLineVisible: false,
  title: '',
}

/** Indicator panes keep a little air above and below the plotted values. */
export const indicatorPaneScaleMargins = { top: 0.1, bottom: 0.08 } as const

/**
 * Relative pane heights: the price pane keeps stretch 1 and each indicator pane gets this,
 * so one oscillator takes ~23% of the height, two take ~19% each, three ~16% each.
 */
export const mainPaneStretch = 1
export const indicatorPaneStretch = 0.3

/** Dashed, muted guide lines (RSI 70/30, Stoch 80/20, MACD zero line); no axis label. */
export function guideLineOptions(
  price: number,
  palette: Pick<ChartPalette, 'mutedText'> = colors,
): CreatePriceLineOptions {
  return {
    price,
    color: palette.mutedText,
    lineWidth: 1,
    lineStyle: LineStyle.Dashed,
    lineVisible: true,
    axisLabelVisible: false,
    title: '',
  }
}

/* ---------- Drawings and alerts ---------- */

export const drawingColors = {
  /** Inside of the round anchor handles on a selected drawing (dark default; see handleColorsFor). */
  handleFill: colors.background,
  /** Active alert price lines (TradingView's alert orange). */
  alertLine: '#f7a600',
} as const

/** Selection handles: filled with the chart background, ringed in the theme's text colour. */
export interface HandleColors {
  fill: string
  ring: string
}

export function handleColorsFor(p: Pick<ChartPalette, 'background' | 'text'>): HandleColors {
  return { fill: p.background, ring: p.text }
}

/** Perceived brightness of '#rrggbb' in [0, 1]; null for anything else. */
export function hexLuminance(color: string): number | null {
  const m = /^#([0-9a-f]{6})/i.exec(color.trim())
  if (!m) return null
  const v = parseInt(m[1], 16)
  return (0.299 * ((v >> 16) & 255) + 0.587 * ((v >> 8) & 255) + 0.114 * (v & 255)) / 255
}

/**
 * Stroke for a selection handle: the drawing's colour, unless it would vanish against the handle
 * fill (e.g. a light-grey line on the light theme), then the theme's ring colour.
 */
export function handleStroke(drawingColor: string, handles: HandleColors): string {
  const a = hexLuminance(drawingColor)
  const b = hexLuminance(handles.fill)
  if (a === null || b === null) return drawingColor
  return Math.abs(a - b) < 0.2 ? handles.ring : drawingColor
}

/** Dashed alert price line with an axis label, e.g. "Alert". */
export function alertLineOptions(price: number, title: string): CreatePriceLineOptions {
  return {
    price,
    color: drawingColors.alertLine,
    lineWidth: 1,
    lineStyle: LineStyle.Dashed,
    lineVisible: true,
    axisLabelVisible: true,
    axisLabelColor: drawingColors.alertLine,
    axisLabelTextColor: '#131722',
    title,
  }
}
