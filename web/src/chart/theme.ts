import {
  ColorType,
  CrosshairMode,
  type CandlestickSeriesPartialOptions,
  type DeepPartial,
  type HistogramSeriesPartialOptions,
  type TimeChartOptions,
} from 'lightweight-charts'

/** TradingView-like dark palette. */
export const colors = {
  background: '#131722',
  grid: '#1e222d',
  border: '#2a2e39',
  text: '#d1d4dc',
  mutedText: '#787b86',
  up: '#26a69a',
  down: '#ef5350',
  /** Volume bars use the candle colours at ~50% alpha. */
  volumeUp: '#26a69a80',
  volumeDown: '#ef535080',
  crosshair: '#758696',
} as const

export const chartOptions: DeepPartial<TimeChartOptions> = {
  autoSize: true,
  layout: {
    background: { type: ColorType.Solid, color: colors.background },
    textColor: colors.text,
    fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
  },
  grid: {
    vertLines: { color: colors.grid },
    horzLines: { color: colors.grid },
  },
  crosshair: {
    mode: CrosshairMode.Normal,
    vertLine: { color: colors.crosshair, labelBackgroundColor: colors.border },
    horzLine: { color: colors.crosshair, labelBackgroundColor: colors.border },
  },
  rightPriceScale: { borderColor: colors.border },
  timeScale: { borderColor: colors.border, timeVisible: true, secondsVisible: false },
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
