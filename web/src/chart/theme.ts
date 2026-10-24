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

/** TradingView-like dark palette. Keep in sync with the CSS variables in index.css. */
export const colors = {
  background: '#131722',
  grid: '#1c2030',
  border: '#2a2e39',
  text: '#b2b5be',
  mutedText: '#787b86',
  up: '#089981',
  down: '#f23645',
  /** Volume bars use the candle colours at ~45% alpha. */
  volumeUp: '#08998173',
  volumeDown: '#f2364573',
  /** Line and area chart stroke (TradingView blue). */
  line: '#2962ff',
  areaTop: 'rgba(41, 98, 255, 0.28)',
  areaBottom: 'rgba(41, 98, 255, 0)',
  transparent: 'rgba(0, 0, 0, 0)',
  crosshair: '#9598a1',
  crosshairLabel: '#363a45',
  watermark: 'rgba(178, 181, 190, 0.06)',
} as const

export const fallbackFontFamily = "-apple-system, 'Segoe UI', sans-serif"
export const fontFamily = `'IBM Plex Sans', ${fallbackFontFamily}`
/** Font descriptors `document.fonts.load` must resolve before the chart switches to `fontFamily`. */
export const webFonts = ["11px 'IBM Plex Sans'", "600 72px 'IBM Plex Sans'"] as const

export const chartOptions: DeepPartial<TimeChartOptions> = {
  autoSize: true,
  layout: {
    background: { type: ColorType.Solid, color: colors.background },
    textColor: colors.text,
    // Start on system fonts; Chart switches to `fontFamily` once IBM Plex has loaded.
    fontFamily: fallbackFontFamily,
    fontSize: 11,
  },
  grid: {
    vertLines: { color: colors.grid },
    horzLines: { color: colors.grid },
  },
  crosshair: {
    mode: CrosshairMode.Normal,
    vertLine: {
      color: colors.crosshair,
      style: LineStyle.Dashed,
      width: 1,
      labelBackgroundColor: colors.crosshairLabel,
    },
    horzLine: {
      color: colors.crosshair,
      style: LineStyle.Dashed,
      width: 1,
      labelBackgroundColor: colors.crosshairLabel,
    },
  },
  rightPriceScale: { borderColor: colors.border, scaleMargins: { top: 0.12, bottom: 0.24 } },
  timeScale: {
    borderColor: colors.border,
    timeVisible: true,
    secondsVisible: false,
    rightOffset: 6,
    barSpacing: 8,
  },
}

export const candlestickOptions: CandlestickSeriesPartialOptions = {
  upColor: colors.up,
  downColor: colors.down,
  borderVisible: false,
  wickUpColor: colors.up,
  wickDownColor: colors.down,
  // Live price: label on the price scale plus a dotted line across the pane, as on TradingView.
  // An empty priceLineColor (the default) colours both by the last bar's direction.
  lastValueVisible: true,
  priceLineVisible: true,
  priceLineSource: PriceLineSource.LastBar,
  priceLineStyle: LineStyle.Dotted,
  priceLineWidth: 1,
}

/** Live price label + dotted line shared by every main-series type. */
const livePriceLine = {
  lastValueVisible: true,
  priceLineVisible: true,
  priceLineSource: PriceLineSource.LastBar,
  priceLineStyle: LineStyle.Dotted,
  priceLineWidth: 1,
} as const

/**
 * Hollow candles, TradingView style: rising bars are outlined (transparent body, coloured
 * border and wick), falling bars are filled.
 */
export const hollowCandlestickOptions: CandlestickSeriesPartialOptions = {
  ...candlestickOptions,
  upColor: colors.transparent,
  downColor: colors.down,
  borderVisible: true,
  borderUpColor: colors.up,
  borderDownColor: colors.down,
  wickUpColor: colors.up,
  wickDownColor: colors.down,
}

/**
 * Heikin-Ashi draws synthetic bars, so the series' own last-value label would show the HA close.
 * It is hidden; Chart draws a separate price line at the real last close instead
 * (see `realPriceLine` in chartTypes.ts).
 */
export const heikinAshiOptions: CandlestickSeriesPartialOptions = {
  ...candlestickOptions,
  lastValueVisible: false,
  priceLineVisible: false,
}

export const lineOptions: LineSeriesPartialOptions = {
  ...livePriceLine,
  color: colors.line,
  lineWidth: 2,
  crosshairMarkerRadius: 4,
  crosshairMarkerBorderColor: colors.background,
  crosshairMarkerBackgroundColor: colors.line,
}

export const areaOptions: AreaSeriesPartialOptions = {
  ...livePriceLine,
  lineColor: colors.line,
  topColor: colors.areaTop,
  bottomColor: colors.areaBottom,
  lineWidth: 2,
  crosshairMarkerRadius: 4,
  crosshairMarkerBorderColor: colors.background,
  crosshairMarkerBackgroundColor: colors.line,
}

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
): DeepPartial<TextWatermarkOptions> {
  return {
    horzAlign: 'center',
    vertAlign: 'center',
    lines: [{ text, color: colors.watermark, fontSize: 72, fontStyle: '600', fontFamily: family }],
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
export function guideLineOptions(price: number): CreatePriceLineOptions {
  return {
    price,
    color: colors.mutedText,
    lineWidth: 1,
    lineStyle: LineStyle.Dashed,
    lineVisible: true,
    axisLabelVisible: false,
    title: '',
  }
}
