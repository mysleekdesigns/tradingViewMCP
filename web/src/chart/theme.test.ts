import {
  ColorType,
  TickMarkType,
  type TickMarkFormatter,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import { describe, expect, it } from 'vitest'
import { CHART_TYPES } from '../types'
import {
  DEFAULT_APPEARANCE,
  chartOptions,
  chartOptionsFor,
  chartThemeOptionsFor,
  colors,
  guideLineOptions,
  handleColorsFor,
  handleStroke,
  mainSeriesOptionsFor,
  paletteFor,
  watermarkOptions,
  withHexAlpha,
  type ChartAppearance,
} from './theme'

const light: ChartAppearance = { ...DEFAULT_APPEARANCE, theme: 'light' }
const NOV_1_00Z = 1_793_491_200 as UTCTimestamp // 2026-11-01 00:00 UTC = 2026-10-31 20:00 EDT

describe('withHexAlpha', () => {
  it('appends a two-digit alpha to #rrggbb and leaves other strings alone', () => {
    expect(withHexAlpha('#089981', 0x73 / 255)).toBe('#08998173')
    expect(withHexAlpha('#ABCDEF', 1)).toBe('#abcdefff')
    expect(withHexAlpha('#000000', 0)).toBe('#00000000')
    expect(withHexAlpha('rgba(1, 2, 3, 0.5)', 0.4)).toBe('rgba(1, 2, 3, 0.5)')
  })
})

describe('paletteFor', () => {
  it('dark defaults keep the original chart colours', () => {
    expect(paletteFor(DEFAULT_APPEARANCE)).toMatchObject({
      background: '#131722',
      grid: '#1c2030',
      text: '#b2b5be',
      up: '#089981',
      down: '#f23645',
      volumeUp: '#08998173',
      volumeDown: '#f2364573',
    })
    expect(colors).toEqual(paletteFor(DEFAULT_APPEARANCE))
  })

  it('light swaps the chrome to a white TradingView-like palette, same candle colours', () => {
    const p = paletteFor(light)
    expect(p.background).toBe('#ffffff')
    expect(p.grid).toBe('#f0f3fa')
    expect(p.text).toBe('#131722')
    expect(p.up).toBe(colors.up)
    expect(p.down).toBe(colors.down)
    expect(p.background).not.toBe(colors.background)
    expect(p.watermark).not.toBe(colors.watermark)
  })

  it('custom up/down drive candles and ~45% alpha volume', () => {
    const p = paletteFor({ ...light, upColor: '#2962FF', downColor: '#ff9800' })
    expect(p.up).toBe('#2962ff')
    expect(p.down).toBe('#ff9800')
    expect(p.volumeUp).toBe('#2962ff73')
    expect(p.volumeDown).toBe('#ff980073')
    expect(parseInt(p.volumeUp.slice(7), 16) / 255).toBeCloseTo(0.45, 2)
  })

  it('falls back to default candle colours for invalid input', () => {
    const p = paletteFor({ ...DEFAULT_APPEARANCE, upColor: 'green', downColor: '' })
    expect(p.up).toBe(colors.up)
    expect(p.down).toBe(colors.down)
  })
})

describe('chartOptionsFor', () => {
  it('carries the palette and the time-zone formatters', () => {
    const o = chartOptionsFor({ ...light, timezone: 'America/New_York' })
    expect(o.layout?.background).toEqual({ type: ColorType.Solid, color: '#ffffff' })
    expect(o.layout?.textColor).toBe('#131722')
    expect(o.grid?.vertLines?.color).toBe('#f0f3fa')
    expect(o.timeScale?.borderColor).toBe(paletteFor(light).border)
    // DeepPartial<> hides the call signatures; they are plain functions.
    const tick = o.timeScale?.tickMarkFormatter as TickMarkFormatter | undefined
    const time = o.localization?.timeFormatter as ((t: Time) => string) | undefined
    expect(tick?.(NOV_1_00Z, TickMarkType.Time, 'en-US')).toBe('20:00')
    expect(time?.(NOV_1_00Z)).toBe("Sat 31 Oct '26 20:00")
  })

  it('the exported dark defaults format in UTC', () => {
    const tick = chartOptions.timeScale?.tickMarkFormatter as TickMarkFormatter | undefined
    expect(tick?.(NOV_1_00Z, TickMarkType.Month, 'en-US')).toBe('Nov')
    expect(chartOptions.layout?.background).toEqual({
      type: ColorType.Solid,
      color: colors.background,
    })
  })

  it('theme-only options never touch view state or fonts (safe for applyOptions)', () => {
    const o = chartThemeOptionsFor(light)
    expect(o.timeScale).not.toHaveProperty('barSpacing')
    expect(o.timeScale).not.toHaveProperty('rightOffset')
    expect(o.layout).not.toHaveProperty('fontFamily')
    expect(o).not.toHaveProperty('autoSize')
    expect(o.rightPriceScale).not.toHaveProperty('scaleMargins')
    expect(typeof o.localization?.timeFormatter).toBe('function')
    expect(typeof o.timeScale?.tickMarkFormatter).toBe('function')
  })
})

describe('mainSeriesOptionsFor', () => {
  const p = paletteFor({ ...light, upColor: '#2962ff', downColor: '#ff9800' })

  it('candles, hollow and Heikin-Ashi all use the custom colours', () => {
    expect(mainSeriesOptionsFor('candles', p)).toMatchObject({
      upColor: '#2962ff',
      downColor: '#ff9800',
      wickUpColor: '#2962ff',
      wickDownColor: '#ff9800',
    })
    expect(mainSeriesOptionsFor('hollow', p)).toMatchObject({
      upColor: p.transparent,
      borderUpColor: '#2962ff',
      borderDownColor: '#ff9800',
      downColor: '#ff9800',
    })
    expect(mainSeriesOptionsFor('heikin-ashi', p)).toMatchObject({
      upColor: '#2962ff',
      downColor: '#ff9800',
      lastValueVisible: false,
    })
  })

  it('line and area markers are ringed in the theme background', () => {
    expect(mainSeriesOptionsFor('line', p)).toMatchObject({
      crosshairMarkerBorderColor: '#ffffff',
    })
    expect(mainSeriesOptionsFor('area', p)).toMatchObject({
      crosshairMarkerBorderColor: '#ffffff',
    })
  })

  it('covers every chart type', () => {
    for (const type of CHART_TYPES) expect(mainSeriesOptionsFor(type, p)).toBeTypeOf('object')
  })
})

describe('watermark and guides', () => {
  it('follow the palette', () => {
    const p = paletteFor(light)
    expect(watermarkOptions('BTCUSDT, 1h', 'x', p).lines?.[0]?.color).toBe(p.watermark)
    expect(guideLineOptions(70, p).color).toBe(p.mutedText)
    expect(guideLineOptions(70).color).toBe(colors.mutedText)
  })
})

describe('selection handles', () => {
  it('fill with the background and ring in the theme text colour', () => {
    expect(handleColorsFor(paletteFor(light))).toEqual({ fill: '#ffffff', ring: '#131722' })
  })

  it('keep the drawing colour unless it would vanish against the fill', () => {
    const lightHandles = handleColorsFor(paletteFor(light))
    const darkHandles = handleColorsFor(colors)
    expect(handleStroke('#2962ff', lightHandles)).toBe('#2962ff')
    // Light grey / yellow lines on white get the dark ring instead.
    expect(handleStroke('#d1d4dc', lightHandles)).toBe('#131722')
    expect(handleStroke('#ffffff', lightHandles)).toBe('#131722')
    // On dark, light grey is fine; a near-black line gets the light ring.
    expect(handleStroke('#d1d4dc', darkHandles)).toBe('#d1d4dc')
    expect(handleStroke('#131722', darkHandles)).toBe(colors.text)
  })
})
