// One luminance for every light/dark decision the chart makes (Phase 19 DRY): the light-theme
// flag of the market-structure and session overlays (chart background), the selection-handle
// stroke against the handle fill, and the price-label text on a drawing colour.
//
// Rec. 601 luma (0.299 R + 0.587 G + 0.114 B) is used everywhere. The handle stroke and the
// label text already used it and take any '#rrggbb' the colour picker allows, so keeping it
// leaves every one of their decisions as it was; the overlays used Rec. 709 but only ever see
// the two theme backgrounds, which are far from their 0.5 threshold under either formula
// (see color.test.ts for the decision table).

/** Exactly '#rrggbb' (any case); surrounding whitespace is ignored. */
const HEX6 = /^#([0-9a-f]{6})$/i

/** 0xrrggbb of a '#rrggbb' colour, or null for any other string. */
export function parseHex6(color: string): number | null {
  const m = HEX6.exec(color.trim())
  return m === null ? null : parseInt(m[1], 16)
}

/** Perceived brightness of '#rrggbb' in [0, 1] (Rec. 601 luma); null for any other string. */
export function luminance(color: string): number | null {
  const v = parseHex6(color)
  if (v === null) return null
  return (0.299 * ((v >> 16) & 255) + 0.587 * ((v >> 8) & 255) + 0.114 * (v & 255)) / 255
}

/** Backgrounds brighter than this are light (the overlays use fainter fills on them). */
export const LIGHT_BACKGROUND_LUMINANCE = 0.5

/** A light chart background; anything that is not '#rrggbb' counts as dark. */
export function isLightBackground(background: string): boolean {
  const l = luminance(background)
  return l !== null && l > LIGHT_BACKGROUND_LUMINANCE
}
