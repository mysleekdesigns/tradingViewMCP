// The one luminance (Rec. 601) must leave every light/dark decision of the four copies it
// replaced unchanged for every colour that can reach each call site:
// - overlay light flag (market structure, session ranges; Rec. 709 > 0.5 before): only the two
//   theme backgrounds reach it (`paletteFor` takes the base palette of 'dark' or 'light');
// - handle stroke (`handleStroke`, Rec. 601 |a - b| < 0.2 before) and price-label text
//   (`contrastText`, Rec. 601 > 0.6 before): any '#rrggbb' (palette swatches, the colour
//   picker, stored drawings validated as '#rrggbb'), against the two theme handle fills.

import { describe, expect, it } from 'vitest'
import { DRAWING_PALETTE } from '../../components/drawingTools'
import { contrastText } from '../drawings/geometry'
import { DEFAULT_DRAWING_STYLE } from '../drawings/types'
import { marketStructureColorsFor } from '../marketStructureLayer'
import { sessionRangesColorsFor } from '../sessionRangesLayer'
import { DEFAULT_APPEARANCE, handleColorsFor, handleStroke, paletteFor } from '../theme'
import { LIGHT_BACKGROUND_LUMINANCE, isLightBackground, luminance, parseHex6 } from './color'

/* The four copies as they were before Phase 19 wave 5 (verbatim apart from the names). */

/** marketStructureLayer.ts / sessionRangesLayer.ts (Rec. 709). */
function oldOverlayLuminance(hex: string): number {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex)
  if (m === null) return 0
  const [r, g, b] = [m[1], m[2], m[3]].map((h) => parseInt(h, 16) / 255)
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

/** theme.ts `hexLuminance` (Rec. 601). */
function oldHexLuminance(color: string): number | null {
  const m = /^#([0-9a-f]{6})/i.exec(color.trim())
  if (!m) return null
  const v = parseInt(m[1], 16)
  return (0.299 * ((v >> 16) & 255) + 0.587 * ((v >> 8) & 255) + 0.114 * (v & 255)) / 255
}

function oldHandleStroke(drawingColor: string, handles: { fill: string; ring: string }): string {
  const a = oldHexLuminance(drawingColor)
  const b = oldHexLuminance(handles.fill)
  if (a === null || b === null) return drawingColor
  return Math.abs(a - b) < 0.2 ? handles.ring : drawingColor
}

/** drawings/geometry.ts `contrastText` (Rec. 601). */
function oldContrastText(color: string): string {
  const m = /^#([0-9a-f]{6})$/i.exec(color.trim())
  if (!m) return '#ffffff'
  const v = parseInt(m[1], 16)
  const lum = (0.299 * ((v >> 16) & 255) + 0.587 * ((v >> 8) & 255) + 0.114 * (v & 255)) / 255
  return lum > 0.6 ? '#131722' : '#ffffff'
}

const hex = (v: number): string => '#' + v.toString(16).padStart(6, '0')

const DARK = paletteFor(DEFAULT_APPEARANCE)
const LIGHT = paletteFor({ ...DEFAULT_APPEARANCE, theme: 'light' })
const HANDLES = [handleColorsFor(DARK), handleColorsFor(LIGHT)]
/** Every colour a drawing can start with or pick from a swatch (plus upper case, as stored). */
const SWATCHES = [
  DEFAULT_DRAWING_STYLE.color,
  ...DRAWING_PALETTE.map((p) => p.color),
  ...DRAWING_PALETTE.map((p) => p.color.toUpperCase()),
]

describe('luminance (Rec. 601)', () => {
  it('known values', () => {
    expect(luminance('#000000')).toBe(0)
    expect(luminance('#ffffff')).toBe(1)
    expect(luminance('#ff0000')).toBeCloseTo(0.299, 12)
    expect(luminance('#00ff00')).toBeCloseTo(0.587, 12)
    expect(luminance('#0000ff')).toBeCloseTo(0.114, 12)
    expect(luminance('#131722')).toBeCloseTo((0.299 * 19 + 0.587 * 23 + 0.114 * 34) / 255, 12)
    expect(luminance('#FFFFFF')).toBe(1)
    expect(luminance(' #ffffff ')).toBe(1)
  })

  it("anything that is not exactly '#rrggbb' has none", () => {
    for (const s of ['', '#fff', '#ffffff80', 'ffffff', 'rgba(0, 0, 0, 0)', '#gggggg', 'white']) {
      expect(luminance(s)).toBeNull()
      expect(parseHex6(s)).toBeNull()
      expect(isLightBackground(s)).toBe(false)
    }
    expect(parseHex6('#2962ff')).toBe(0x2962ff)
  })

  it('isLightBackground is luminance above 0.5', () => {
    expect(LIGHT_BACKGROUND_LUMINANCE).toBe(0.5)
    expect(isLightBackground('#ffffff')).toBe(true)
    expect(isLightBackground('#131722')).toBe(false)
    expect(isLightBackground('#808080')).toBe(true) // 128 / 255 = 0.502
    expect(isLightBackground('#7f7f7f')).toBe(false) // 127 / 255 = 0.498
  })
})

describe('decision table: no light/dark decision changed', () => {
  it('overlay light flag: both theme backgrounds decide as under Rec. 709', () => {
    const rows = [DARK, LIGHT].map((p) => ({
      background: p.background,
      old: oldOverlayLuminance(p.background) > 0.5,
      now: isLightBackground(p.background),
    }))
    expect(rows).toEqual([
      { background: '#131722', old: false, now: false },
      { background: '#ffffff', old: true, now: true },
    ])
    // Far from the threshold under either formula (no rounding edge case).
    for (const p of [DARK, LIGHT]) {
      const l = luminance(p.background) as number
      expect(Math.abs(l - 0.5)).toBeGreaterThan(0.4)
      expect(Math.abs(oldOverlayLuminance(p.background) - 0.5)).toBeGreaterThan(0.4)
    }
    expect(marketStructureColorsFor(DARK).light).toBe(false)
    expect(marketStructureColorsFor(LIGHT).light).toBe(true)
    expect(sessionRangesColorsFor(DARK).light).toBe(false)
    expect(sessionRangesColorsFor(LIGHT).light).toBe(true)
  })

  it('handle stroke and label text: every swatch on both themes', () => {
    for (const color of SWATCHES) {
      expect(contrastText(color)).toBe(oldContrastText(color))
      for (const handles of HANDLES) {
        expect(handleStroke(color, handles)).toBe(oldHandleStroke(color, handles))
      }
    }
    // The table itself: light swatches get dark label text and, on the light theme, the ring.
    const text = Object.fromEntries(SWATCHES.slice(0, 11).map((c) => [c, contrastText(c)]))
    expect(
      Object.entries(text)
        .filter(([, t]) => t === '#131722')
        .map(([c]) => c),
    ).toEqual(['#ffeb3b', '#ff9800', '#d1d4dc'])
    const ringed = (h: (typeof HANDLES)[number]) =>
      DRAWING_PALETTE.map((p) => p.color).filter((c) => handleStroke(c, h) !== c)
    expect(ringed(HANDLES[0])).toEqual([])
    expect(ringed(HANDLES[1])).toEqual(['#ffeb3b', '#d1d4dc'])
  })

  it('Rec. 709 would have flipped a swatch (why Rec. 601 was kept)', () => {
    // Purple against the dark handle fill: 0.351 - 0.090 under Rec. 601 (own colour), but
    // 0.289 - 0.090 < 0.2 under Rec. 709 (the ring).
    const purple = '#9c27b0'
    const fill = HANDLES[0].fill
    expect(Math.abs((luminance(purple) as number) - (luminance(fill) as number))).toBeGreaterThan(
      0.2,
    )
    expect(Math.abs(oldOverlayLuminance(purple) - oldOverlayLuminance(fill))).toBeLessThan(0.2)
    expect(handleStroke(purple, HANDLES[0])).toBe(purple)
  })

  it('handle stroke and label text: a grid over every channel, the greys and the thresholds', () => {
    const probes: number[] = []
    for (let r = 0; r < 256; r += 5)
      for (let g = 0; g < 256; g += 5)
        for (let b = 0; b < 256; b += 5) probes.push((r << 16) | (g << 8) | b)
    for (let v = 0; v < 256; v++) probes.push((v << 16) | (v << 8) | v)
    // Colours within one step of the 0.6 label threshold and of the handle thresholds.
    const near = (l: number) =>
      Math.abs(l - 0.6) < 0.004 ||
      Math.abs(Math.abs(l - (luminance(HANDLES[0].fill) as number)) - 0.2) < 0.004 ||
      Math.abs(Math.abs(l - 1) - 0.2) < 0.004
    for (let r = 0; r < 256; r += 3)
      for (let g = 0; g < 256; g += 3)
        for (let b = 0; b < 256; b += 17) {
          const v = (r << 16) | (g << 8) | b
          if (near(luminance(hex(v)) as number)) probes.push(v)
        }
    let flips = 0
    for (const v of probes) {
      const c = hex(v)
      if (contrastText(c) !== oldContrastText(c)) flips++
      for (const h of HANDLES) if (handleStroke(c, h) !== oldHandleStroke(c, h)) flips++
      if (luminance(c) !== oldHexLuminance(c)) flips++
    }
    expect(probes.length).toBeGreaterThan(140_000)
    expect(flips).toBe(0)
  })
})
