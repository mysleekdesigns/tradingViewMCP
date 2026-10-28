// Chart PNG export. `chart.takeScreenshot()` draws only the canvases (the legend is HTML), so a
// header strip with the symbol, interval, venue, and capture time is painted above the image.

import type { IChartApi } from 'lightweight-charts'
import { fontFamily, type ChartPalette } from './theme'

/**
 * The chart image for a PNG export, drawings included. Drawings are series primitives with
 * zOrder 'top' (drawings/primitives.ts), which LWC paints on each pane's top canvas; a bare
 * `takeScreenshot()` copies only the main canvases (addTopLayer defaults to false) and would
 * drop every drawing. `addTopLayer: true` (LWC 5.0.9+) adds the top canvases. The crosshair
 * also lives there, so it is hidden for the capture (`includeCrosshair: false`): it would only
 * show where the mouse was when the user clicked Export.
 */
export function captureChart(chart: Pick<IChartApi, 'takeScreenshot'>): HTMLCanvasElement {
  return chart.takeScreenshot(true, false)
}

export interface ScreenshotHeader {
  /** e.g. "BTCUSDT · 1h · Binance · local server" */
  title: string
  /** e.g. "O 67,010.00 H … · 2026-10-25 14:03 UTC" */
  subtitle: string
  palette: ChartPalette
}

/** Header strip height in CSS px (scaled by devicePixelRatio, like the chart's own canvas). */
export const HEADER_CSS_HEIGHT = 46
const PAD_X = 14
const TITLE_PX = 14
const SUBTITLE_PX = 12

/**
 * A new canvas: header strip on top, the chart image below, same width as the chart canvas
 * (bitmap pixels). Without a 2D context (very old browsers, tests) the chart canvas is returned
 * as is.
 */
export function composeScreenshot(
  chartCanvas: HTMLCanvasElement,
  header: ScreenshotHeader,
  doc: Document = document,
): HTMLCanvasElement {
  const ratio = Math.max(1, doc.defaultView?.devicePixelRatio ?? 1)
  const headerHeight = Math.round(HEADER_CSS_HEIGHT * ratio)
  const width = chartCanvas.width
  const out = doc.createElement('canvas')
  out.width = width
  out.height = chartCanvas.height + headerHeight
  const ctx = out.getContext('2d')
  if (!ctx) return chartCanvas

  const p = header.palette
  ctx.fillStyle = p.background
  ctx.fillRect(0, 0, width, out.height)

  const x = Math.round(PAD_X * ratio)
  const maxText = Math.max(0, width - 2 * x)
  ctx.textAlign = 'left'
  ctx.textBaseline = 'alphabetic'
  ctx.fillStyle = p.text
  ctx.font = `600 ${Math.round(TITLE_PX * ratio)}px ${fontFamily}`
  ctx.fillText(header.title, x, Math.round(20 * ratio), maxText)
  ctx.fillStyle = p.mutedText
  ctx.font = `${Math.round(SUBTITLE_PX * ratio)}px ${fontFamily}`
  ctx.fillText(header.subtitle, x, Math.round(37 * ratio), maxText)

  // Hairline between the header and the chart, in the scale border colour.
  ctx.fillStyle = p.border
  ctx.fillRect(
    0,
    headerHeight - Math.max(1, Math.round(ratio)),
    width,
    Math.max(1, Math.round(ratio)),
  )

  ctx.drawImage(chartCanvas, 0, headerHeight)
  return out
}

const pad2 = (n: number) => String(n).padStart(2, '0')

/** "BTCUSDT_1h_2026-10-25_14-03-07Z.png" (UTC, so names sort and never collide across zones). */
export function screenshotFilename(symbol: string, interval: string, nowSec: number): string {
  const clean = (s: string) => s.replace(/[^A-Za-z0-9-]+/g, '') || 'chart'
  const d = new Date(Math.floor(nowSec) * 1000)
  const date = `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())}`
  const time = `${pad2(d.getUTCHours())}-${pad2(d.getUTCMinutes())}-${pad2(d.getUTCSeconds())}`
  return `${clean(symbol)}_${clean(interval)}_${date}_${time}Z.png`
}

/** How long the object URL lives after the click (Firefox needs it past the click handler). */
export const REVOKE_DELAY_MS = 1000

/** Save a canvas as a PNG: toBlob → object URL → a temporary `<a download>` click → revoke. */
export function downloadCanvas(
  canvas: HTMLCanvasElement,
  filename: string,
  doc: Document = document,
): Promise<void> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (!blob) {
        reject(new Error('Could not encode the chart as PNG'))
        return
      }
      const url = URL.createObjectURL(blob)
      try {
        const a = doc.createElement('a')
        a.href = url
        a.download = filename
        a.rel = 'noopener'
        a.style.display = 'none'
        doc.body.appendChild(a)
        a.click()
        a.remove()
        resolve()
      } catch (err) {
        reject(err instanceof Error ? err : new Error(String(err)))
      } finally {
        setTimeout(() => URL.revokeObjectURL(url), REVOKE_DELAY_MS)
      }
    }, 'image/png')
  })
}
