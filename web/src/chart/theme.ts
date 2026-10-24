import {
  ColorType,
  CrosshairMode,
  LineStyle,
  type CandlestickSeriesPartialOptions,
  type DeepPartial,
  type HistogramSeriesPartialOptions,
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
  crosshair: '#9598a1',
  crosshairLabel: '#363a45',
  watermark: 'rgba(178, 181, 190, 0.06)',
} as const

export const fontFamily = "'IBM Plex Sans', -apple-system, 'Segoe UI', sans-serif"

export const chartOptions: DeepPartial<TimeChartOptions> = {
  autoSize: true,
  layout: {
    background: { type: ColorType.Solid, color: colors.background },
    textColor: colors.text,
    fontFamily,
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
}

/** Volume sits on its own overlay price scale (id ''), pinned to the bottom 20% of the pane. */
export const volumeOptions: HistogramSeriesPartialOptions = {
  priceScaleId: '',
  priceFormat: { type: 'volume' },
  lastValueVisible: false,
  priceLineVisible: false,
}

export const volumeScaleMargins = { top: 0.8, bottom: 0 } as const

export function watermarkOptions(text: string): DeepPartial<TextWatermarkOptions> {
  return {
    horzAlign: 'center',
    vertAlign: 'center',
    lines: [{ text, color: colors.watermark, fontSize: 72, fontStyle: '600', fontFamily }],
  }
}
