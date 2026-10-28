import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  HEADER_CSS_HEIGHT,
  REVOKE_DELAY_MS,
  captureChart,
  composeScreenshot,
  downloadCanvas,
  screenshotFilename,
} from './screenshot'
import { DEFAULT_APPEARANCE, paletteFor } from './theme'

// jsdom is not installed and Node has no canvas: a recording fake stands in for both.

interface Op {
  op: string
  args: unknown[]
  fillStyle?: unknown
  font?: unknown
}

function fakeContext() {
  const ops: Op[] = []
  const ctx = {
    fillStyle: '' as unknown,
    font: '',
    textAlign: '',
    textBaseline: '',
    fillRect: (...args: unknown[]) => ops.push({ op: 'fillRect', args, fillStyle: ctx.fillStyle }),
    fillText: (...args: unknown[]) =>
      ops.push({ op: 'fillText', args, fillStyle: ctx.fillStyle, font: ctx.font }),
    drawImage: (...args: unknown[]) => ops.push({ op: 'drawImage', args }),
  }
  return { ctx, ops }
}

function fakeCanvas(width: number, height: number, withContext = true) {
  const { ctx, ops } = fakeContext()
  const canvas = {
    width,
    height,
    getContext: (kind: string) => (withContext && kind === '2d' ? ctx : null),
  }
  return { canvas: canvas as unknown as HTMLCanvasElement, ops }
}

function fakeDoc(ratio: number, withContext = true) {
  const created: { canvas: HTMLCanvasElement; ops: Op[] }[] = []
  const doc = {
    defaultView: { devicePixelRatio: ratio },
    createElement: (tag: string) => {
      if (tag !== 'canvas') throw new Error(`unexpected <${tag}>`)
      const c = fakeCanvas(0, 0, withContext)
      created.push(c)
      return c.canvas
    },
  }
  return { doc: doc as unknown as Document, created }
}

const palette = paletteFor({ ...DEFAULT_APPEARANCE, theme: 'light' })
const header = { title: 'BTCUSDT · 1h · Binance', subtitle: '2026-10-25 14:03 UTC', palette }

describe('composeScreenshot', () => {
  it('adds a header strip above the chart at the device pixel ratio', () => {
    const chart = fakeCanvas(1600, 900).canvas
    const { doc, created } = fakeDoc(2)
    const out = composeScreenshot(chart, header, doc)
    expect(out).toBe(created[0].canvas)
    expect(out.width).toBe(1600)
    expect(out.height).toBe(900 + HEADER_CSS_HEIGHT * 2)
    const ops = created[0].ops
    // Background first, in the palette's background colour, covering everything.
    expect(ops[0]).toEqual({
      op: 'fillRect',
      args: [0, 0, 1600, 900 + HEADER_CSS_HEIGHT * 2],
      fillStyle: palette.background,
    })
    // The chart image sits right below the header.
    expect(ops.at(-1)).toEqual({ op: 'drawImage', args: [chart, 0, HEADER_CSS_HEIGHT * 2] })
    const texts = ops.filter((o) => o.op === 'fillText')
    expect(texts.map((o) => o.args[0])).toEqual([header.title, header.subtitle])
    expect(texts[0].fillStyle).toBe(palette.text)
    expect(texts[1].fillStyle).toBe(palette.mutedText)
    expect(String(texts[0].font)).toContain('28px')
  })

  it('uses a 1x header without a devicePixelRatio', () => {
    const chart = fakeCanvas(800, 400).canvas
    const { doc } = fakeDoc(0)
    expect(composeScreenshot(chart, header, doc).height).toBe(400 + HEADER_CSS_HEIGHT)
  })

  it('returns the chart canvas unchanged when no 2D context is available', () => {
    const chart = fakeCanvas(800, 400).canvas
    const { doc } = fakeDoc(1, false)
    expect(composeScreenshot(chart, header, doc)).toBe(chart)
  })
})

describe('screenshotFilename', () => {
  it('is symbol_interval_UTC-date_time Z.png', () => {
    expect(screenshotFilename('BTCUSDT', '1h', 1_792_936_987)).toBe(
      'BTCUSDT_1h_2026-10-25_14-03-07Z.png',
    )
    expect(screenshotFilename('ETHUSD', '1M', 1_767_225_600.9)).toBe(
      'ETHUSD_1M_2026-01-01_00-00-00Z.png',
    )
  })

  it('strips characters that are unsafe in file names', () => {
    expect(screenshotFilename('BTC/USD ../x', '4h', 0)).toBe('BTCUSDx_4h_1970-01-01_00-00-00Z.png')
    expect(screenshotFilename('', '', 0)).toBe('chart_chart_1970-01-01_00-00-00Z.png')
  })
})

describe('downloadCanvas', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  function setup(blob: Blob | null) {
    const clicks: { href: string; download: string }[] = []
    const anchor = {
      href: '',
      download: '',
      rel: '',
      style: { display: '' },
      click: () => clicks.push({ href: anchor.href, download: anchor.download }),
      remove: vi.fn(),
    }
    const body = { appendChild: vi.fn() }
    const doc = { createElement: () => anchor, body } as unknown as Document
    const canvas = {
      toBlob: (cb: (b: Blob | null) => void, type: string) => {
        expect(type).toBe('image/png')
        cb(blob)
      },
    } as unknown as HTMLCanvasElement
    return { doc, canvas, clicks, anchor, body }
  }

  it('clicks a temporary download link for the PNG, then revokes the URL', async () => {
    vi.useFakeTimers()
    const create = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:shot')
    const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    const { doc, canvas, clicks, anchor, body } = setup(new Blob(['png'], { type: 'image/png' }))
    await downloadCanvas(canvas, 'BTCUSDT_1h.png', doc)
    expect(create).toHaveBeenCalledOnce()
    expect(clicks).toEqual([{ href: 'blob:shot', download: 'BTCUSDT_1h.png' }])
    expect(body.appendChild).toHaveBeenCalledWith(anchor)
    expect(anchor.remove).toHaveBeenCalledOnce()
    expect(revoke).not.toHaveBeenCalled()
    vi.advanceTimersByTime(REVOKE_DELAY_MS)
    expect(revoke).toHaveBeenCalledWith('blob:shot')
  })

  it('rejects when the canvas cannot be encoded', async () => {
    const { doc, canvas, clicks } = setup(null)
    await expect(downloadCanvas(canvas, 'x.png', doc)).rejects.toThrow(/PNG/)
    expect(clicks).toEqual([])
  })
})

describe('captureChart', () => {
  // Models LWC 5.2.1's takeScreenshot: the main canvases always; the top canvases (series
  // primitives with zOrder 'top', i.e. the drawings, plus the crosshair) only with
  // addTopLayer; the crosshair only with addTopLayer && includeCrosshair. Both default false.
  function fakeChart() {
    const takeScreenshot = vi.fn((addTopLayer = false, includeCrosshair = false) => {
      const layers = ['candles']
      if (addTopLayer) layers.push('drawings')
      if (addTopLayer && includeCrosshair) layers.push('crosshair')
      return { layers } as unknown as HTMLCanvasElement
    })
    return { takeScreenshot }
  }
  const layersOf = (c: HTMLCanvasElement) => (c as unknown as { layers: string[] }).layers

  it('asks for the top layer (drawings) and leaves the crosshair out', () => {
    const chart = fakeChart()
    const shot = captureChart(chart)
    expect(chart.takeScreenshot).toHaveBeenCalledTimes(1)
    expect(chart.takeScreenshot).toHaveBeenCalledWith(true, false)
    expect(layersOf(shot)).toEqual(['candles', 'drawings'])
  })

  it('a bare takeScreenshot() would lose the drawings (why captureChart exists)', () => {
    expect(layersOf(fakeChart().takeScreenshot())).toEqual(['candles'])
  })

  it('returns the chart canvas unchanged, ready for composeScreenshot', () => {
    const canvas = fakeCanvas(800, 600).canvas
    const chart = { takeScreenshot: vi.fn(() => canvas) }
    expect(captureChart(chart)).toBe(canvas)
  })
})
